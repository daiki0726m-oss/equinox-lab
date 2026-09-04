#!/usr/bin/env python3
"""学習側と推論側で特徴量が一致するかを機械検査する (#135)

背景: 54特徴量を fast_train.py (学習) と ml/features.py (推論) で**二重実装**しており、
列名の一致は get_feature_columns() で強制しているのに **中身の定義を照合する仕組みが無かった**。
そのため同型のバグが繰り返し出た:
  #128 dist_top3_rate  — 学習±200m馬場不問 / 推論±100m同馬場
  #133 past_races[0]   — 学習は**デビュー戦**を「前走」として使用 (10特徴量が別物)
  #134 通過順・調教師   — netkeiba 列名改称で4か月間データが空
いずれも「同じ馬・同じレースで両経路を計算して突き合わせる」だけで初日に検出できた。

さらにモデル出荷前の健全性 (木の本数・特徴量数) も検査する:
  #134 model_rank が **木3本** で出荷され、意思決定チャネルが潰れて買い目が停止していた。

実行: python3 scripts/verify_feature_parity.py [--races 8] [--strict]
終了コード: 0=OK / 1=警告 / 2=致命 (CI を落とす)
"""
import argparse
import os
import pickle
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# 一致率がこれを下回ったら異常とみなす列ごとの閾値 (0-1)
DEFAULT_TOL = 0.90
# 実装差で微小にズレうる列は緩める (相関で見る)
CORR_ONLY = {"si_avg", "si_max", "si_min", "si_std", "si_latest", "avg_last_3f",
             "margin_avg", "margin_best", "jt_score", "combo_top3"}
# 既知の未解決 skew — 落とさず警告に留める免除リスト。
# #150: #136/#137 で解消済みの9列と、#137 で特徴量から外れた体重系3列を削除。
# 免除を残したままだと「点数表が使う5列 (jockey_cond_top3 / trainer_cond_top3 /
# front_rate / avg_pos_ratio / avg_last_3f) が回帰しても検査が緑」になり、
# #128(数週間)/#133(年単位)/#134(4か月) を初日に捕まえるための唯一の常時検査が
# 最も壊れやすい列を素通しする状態だった。
KNOWN_SKEW = set()


def check_models():
    """出荷前のモデル健全性 (#134: 木3本で出荷された事故の再発防止)。"""
    problems = []
    from fast_train import get_feature_columns
    n_code = len(get_feature_columns())
    mdir = os.path.join(ROOT, "models")
    for name, min_trees in (("model_rank", 20), ("model_top3", 20), ("model_win", 20)):
        path = os.path.join(mdir, f"{name}.pkl")
        if not os.path.exists(path):
            problems.append(("致命", f"{name}.pkl が存在しない"))
            continue
        with open(path, "rb") as f:
            m = pickle.load(f)
        b = getattr(m, "booster_", m)
        trees, nfeat = b.num_trees(), b.num_feature()
        if trees < min_trees:
            problems.append(("致命", f"{name}: 木が {trees} 本しかない "
                                     f"(<{min_trees}) — 予測が潰れる (#134)"))
        if nfeat != n_code:
            problems.append(("致命", f"{name}: モデル {nfeat}列 vs コード {n_code}列 "
                                     f"— predict が全滅する (#107)"))
    # #150: 点数表 (印の選定に使う) の健全性検査。
    # models/scorecard.pkl はヘルスチェックも再学習も通知も無く、
    # #134 の能力モデルと同じ silent 無効化 (読めない → sc_points=0 →
    # 印が黙って従来ロジックに落ちる) に無防備だった。
    sp = os.path.join(mdir, "scorecard.pkl")
    if os.path.exists(sp):
        try:
            with open(sp, "rb") as f:
                sm = pickle.load(f)
            from ml.scorecard import FEATS as _SC_FEATS
            need = {c for c, *_ in _SC_FEATS}
            have = {k.split("=")[0] for k in sm.get("cols", [])} | set(sm.get("spec", {}))
            miss = need - have
            if miss:
                problems.append(("致命", f"scorecard: コードの特徴量 {sorted(miss)[:4]} が "
                                         f"モデルに無い — 印が黙って従来ロジックに落ちる"))
            if not sm.get("pts"):
                problems.append(("致命", "scorecard: 点数表 (pts) が空"))
        except Exception as e:
            problems.append(("致命", f"scorecard.pkl 読込失敗: {e} "
                                     f"— MARKS_SCORECARD=1 が silent 無効化される"))
    else:
        problems.append(("警告", "models/scorecard.pkl が無い "
                                 "(MARKS_SCORECARD=1 なら印が従来ロジックになる)"))

    # 能力モデルは列数が違ってよい (市場特徴量を除くため) が、読み込めることは必須
    ap = os.path.join(mdir, "model_ability_win.pkl")
    if os.path.exists(ap):
        try:
            with open(ap, "rb") as f:
                am = pickle.load(f)
            ab = getattr(am, "booster_", am)
            # #134: predict.py はモデル自身の feature_name() を使う実装になっている
            if not list(ab.feature_name()):
                problems.append(("警告", "model_ability_win: 特徴量名が取れない "
                                         "— MARKS_ABILITY_W が silent 無効化される (#134)"))
        except Exception as e:
            problems.append(("警告", f"model_ability_win 読込失敗: {e}"))
    return problems


def build_train_side(race_ids):
    """学習側 (fast_train) の特徴量を計算して {(race_id, horse_number): {col: val}} を返す。"""
    import pandas as pd
    from database import init_db
    import fast_train as ft
    init_db()
    races_df, results_df, _ = ft.load_all_data()
    cols = ["race_date", "venue", "distance", "surface", "track_condition",
            "horse_count", "race_name", "grade"]
    info = races_df.set_index("race_id")[cols].to_dict("index")
    for c in cols:
        results_df[c] = results_df["race_id"].map(lambda rid, k=c: info.get(rid, {}).get(k, ""))
    hh = ft.build_horse_history(results_df, races_df)
    js, ts, cs = ft.build_jockey_trainer_stats(results_df, races_df)
    si = ft.build_speed_index_cache(results_df, races_df)
    ped = ft.build_pedigree_cross_stats(results_df, races_df)
    grouped = {rid: g for rid, g in results_df.groupby("race_id")}
    out = {}
    for rid in race_ids:
        race = races_df[races_df.race_id == rid]
        rr = grouped.get(rid)
        if race.empty or rr is None or rr.empty:
            continue
        rows = ft.compute_features_fast(
            race.iloc[0].to_dict(), rr.to_dict("records"), hh, js, ts, cs, si,
            sire_course=ped[0], sire_surface=ped[1], damsire_course=ped[2], damsire_surface=ped[3])
        for r in rows:
            out[(rid, int(r.get("horse_number", 0)))] = r
    return out


def build_serve_side(race_ids):
    """推論側 (ml/features.FeatureBuilder) の特徴量。"""
    from ml.features import FeatureBuilder
    fb = FeatureBuilder()
    out = {}
    for rid in race_ids:
        try:
            df = fb.build_features_for_race(rid)
        except Exception as e:
            print(f"  ⚠️ 推論側の計算に失敗 {rid}: {e}")
            continue
        if df is None or df.empty:
            continue
        for r in df.to_dict("records"):
            out[(rid, int(r.get("horse_number", 0)))] = r
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--races", type=int, default=6, help="照合するレース数")
    ap.add_argument("--tol", type=float, default=DEFAULT_TOL, help="一致率の下限")
    ap.add_argument("--strict", action="store_true", help="既知 skew も致命扱いにする")
    ap.add_argument("--models-only", action="store_true", help="モデル健全性だけ検査")
    args = ap.parse_args()

    print("🔍 モデル健全性を検査...")
    problems = check_models()
    for lv, msg in problems:
        print(f"  {'🚨' if lv == '致命' else '⚠️'} {msg}")
    if not problems:
        print("  ✅ 木の本数・特徴量数ともに正常")
    fatal = sum(1 for lv, _ in problems if lv == "致命")
    if args.models_only:
        return 2 if fatal else 0

    conn = sqlite3.connect(os.path.join(ROOT, "keiba.db"))
    race_ids = [r[0] for r in conn.execute(
        """SELECT r.race_id FROM races r
           WHERE EXISTS (SELECT 1 FROM results res WHERE res.race_id=r.race_id AND res.finish_position>0)
           ORDER BY r.race_date DESC LIMIT ?""", (args.races,))]
    if not race_ids:
        print("📭 照合対象のレースが無い")
        return 1
    print(f"\n🔍 学習側 vs 推論側の特徴量パリティ ({len(race_ids)}レース)...")
    train = build_train_side(race_ids)
    serve = build_serve_side(race_ids)
    common = sorted(set(train) & set(serve))
    if not common:
        print("  🚨 突き合わせ可能な馬が0頭 — どちらかの経路が動いていない")
        return 2
    from fast_train import get_feature_columns
    cols = get_feature_columns()
    print(f"  照合対象: {len(common)}頭 × {len(cols)}特徴量\n")
    bad_fatal, bad_warn = [], []
    for c in cols:
        pairs = [(train[k].get(c), serve[k].get(c)) for k in common
                 if c in train[k] and c in serve[k]]
        if not pairs:
            bad_warn.append((c, "どちらかに列が無い", 0.0))
            continue
        same = sum(1 for a, b in pairs
                   if a is not None and b is not None and abs(float(a) - float(b)) <= 1e-6)
        rate = same / len(pairs)
        if rate >= args.tol:
            continue
        # 相関で見る列は相関が高ければ許容
        if c in CORR_ONLY:
            try:
                import statistics as st
                xs = [float(a or 0) for a, _ in pairs]
                ys = [float(b or 0) for _, b in pairs]
                if len(set(xs)) > 1 and len(set(ys)) > 1 and st.correlation(xs, ys) >= 0.95:
                    continue
            except Exception:
                pass
        (bad_warn if (c in KNOWN_SKEW and not args.strict) else bad_fatal).append(
            (c, f"一致率 {100*rate:.0f}%", rate))
    if bad_fatal:
        print("🚨 学習と推論で値が食い違う特徴量 (新規):")
        for c, msg, _ in bad_fatal:
            print(f"   - {c}: {msg}")
    if bad_warn:
        print(f"⚠️ 既知の未解決 skew ({len(bad_warn)}件、#134 で記録済み):")
        print("   " + ", ".join(c for c, _, _ in bad_warn))
    if not bad_fatal and not bad_warn:
        print("✅ 全特徴量が一致")
    return 2 if (fatal or bad_fatal) else (1 if bad_warn else 0)


if __name__ == "__main__":
    sys.exit(main())
