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

照合は2種類のレースで行う:
  - 確定レース (--races): 学習データと同じ状態
  - 発走前レース (--future-races): 本番の予測と同じ状態 (着順0・斤量未発表のことがある)。
    確定レースだけを見ていると、取消や重複登録 (着順0) の扱い・斤量未発表の扱いの
    食い違いが表に出ない (休養日数が「前日の別レースの登録」を前走にしていた件で判明)。
点数表 (ml/scorecard.py: 印 ◎○▲△△ を決める) が使う列は、1頭でも食い違えば印が
変わりうるので **厳密一致 (100%)** を要求する。それ以外の LGB 列は一致率 --tol 以上。

実行: python3 scripts/verify_feature_parity.py [--races 8] [--future-races 4] [--strict]
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
# margin_avg / margin_best は着差の読み方を ml.features.parse_margin / margin_stats の
# 1か所に寄せた (学習側も同じ関数を呼ぶ) ので、相関でなく値の一致で見る。
SI_ROUND_TOL = {"si_avg", "si_latest"}
CORR_ONLY = {"si_avg", "si_max", "si_min", "si_std", "si_latest", "avg_last_3f",
             "jt_score", "combo_top3"}
# LGB の get_feature_columns() には入らないが、点数表 (ml/scorecard.py v2) が使う列。
# 学習側 (fast_train) と推論側 (FeatureBuilder) の両方が出していることも検査する。
# データ無しは NaN で、両方 NaN なら一致とみなす。
EXTRA_COLS = ["form_cls5", "tb_min5", "has_past_runs",
              # #168: 点数表専用の馬場差補正スピード指数 V2d (ml/speed_variant) と芝ダ替わり。
              # si_v_nfb は「V2d が作れず現行の指数で埋めた走の数」(埋めた割合の監視用)。
              "si_avg_v", "si_latest_v", "si_v_nfb", "sf_first_switch", "sf_other_share5"]
# 既知の未解決 skew — 落とさず警告に留める免除リスト。
# #150: #136/#137 で解消済みの9列と、#137 で特徴量から外れた体重系3列を削除。
# 免除を残したままだと「点数表が使う5列 (jockey_cond_top3 / trainer_cond_top3 /
# front_rate / avg_pos_ratio / avg_last_3f) が回帰しても検査が緑」になり、
# #128(数週間)/#133(年単位)/#134(4か月) を初日に捕まえるための唯一の常時検査が
# 最も壊れやすい列を素通しする状態だった。
KNOWN_SKEW = set()


def scorecard_strict_cols():
    """点数表が使う列 (区分項目 FEATS 全部 + 連続項目) — 厳密一致 (100%) を要求する。
    drop される区分項目もここに含める (別の pkl では使われうる)。"""
    from ml.scorecard import FEATS, LIN_FEATS, DEBUT_COL, FLAG_FEATS
    return ({c for c, *_ in FEATS} | {c for c, *_ in LIN_FEATS} | {DEBUT_COL}
            | {c for c, *_ in FLAG_FEATS} | set(EXTRA_COLS))


def _same(a, b, tol=1e-6):
    """値の一致。両方 NaN (データ無し) は一致、片方だけ NaN/None は不一致。"""
    import math
    if a is None or b is None:
        return False
    try:
        fa, fb = float(a), float(b)
    except (TypeError, ValueError):
        return a == b
    if math.isnan(fa) or math.isnan(fb):
        return math.isnan(fa) and math.isnan(fb)
    return abs(fa - fb) <= tol


def _report_si_variant():
    """V2d の基準表 (#168) の版・カットオフ・c・指紋を出す。"""
    try:
        from ml import speed_variant as _sv
        t = _sv.load_table()
    except Exception as e:
        print(f"📐 V2d 基準表: 読めない ({e})")
        return
    if t is None:
        print("📐 V2d 基準表: なし (models/si_variant_table.json) — si_avg_v 等は作られない")
        return
    print(f"📐 V2d 基準表: {t['version']} 指紋 {t['table_hash']} カットオフ {t['cutoff']} "
          f"窓 {t['fit_from']}〜 c={t['dirt_shift_c']:+g} 年齢区分 {t['age_rule']} "
          f"そろった日だけ {t['require_complete']} (作成 {t.get('created_at')})")


def _has_si_table():
    try:
        from ml import speed_variant as _sv
        return _sv.load_table() is not None
    except Exception:
        return False


def _report_fallback(side, rows, keys):
    """V2d が作れず現行の指数で埋めた走の割合 (si_v_nfb / si_count)。"""
    n = k = 0
    for key in keys:
        r = rows.get(key) or {}
        if "si_v_nfb" in r:
            n += int(r.get("si_count") or 0)
            k += int(r.get("si_v_nfb") or 0)
    if n:
        print(f"    {side}: V2d の材料の走 {n} のうち現行の指数で埋めた走 {k} ({100 * k / n:.1f}%)")


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
            # drop した項目は区分点を持たない (v3 の F2 は当てはめ時から外す) ので欠けではない。
            # 除かないと v3 の点数表で毎日「致命」を誤報し、週次の出荷ゲートも毎週落ちる (#168 レビュー)。
            miss = need - have - set(sm.get("drop") or [])
            if miss:
                problems.append(("致命", f"scorecard: コードの特徴量 {sorted(miss)[:4]} が "
                                         f"モデルに無い — 印が黙って従来ロジックに落ちる"))
            if not sm.get("pts"):
                problems.append(("致命", "scorecard: 点数表 (pts) が空"))
            # v2 (連続項目): コードが知らない列を要求していないか
            from ml.scorecard import LIN_DIR as _SC_LIN
            bad_lin = sorted(set(sm.get("lin") or {}) - set(_SC_LIN))
            if bad_lin:
                problems.append(("致命", f"scorecard: 連続項目 {bad_lin} がコード (LIN_FEATS) に無い "
                                         f"— 点数の計算が失敗し印が従来ロジックに落ちる"))
            bad_drop = sorted(set(sm.get("drop") or []) - need)
            if bad_drop:
                problems.append(("警告", f"scorecard: drop {bad_drop} がコードの FEATS に無い"))
            # v3 (#168): 区分の差し替え列・生の値で足す項目をコードが作るか / 基準表が当てはめ時と同じか
            from ml.scorecard import FLAG_KIND as _SC_FLAG, check_si_variant
            from ml import speed_variant as _sv
            known = set(_sv.SV_COLS) | {c for c, *_ in _SC_FEATS}
            bad_map = sorted(set((sm.get("col_map") or {}).values()) - known)
            if bad_map:
                problems.append(("致命", f"scorecard: col_map の列 {bad_map} をコードが作らない "
                                         f"— 点数の計算が失敗し印が従来ロジックに落ちる"))
            bad_flag = sorted(set(sm.get("flags") or {}) - set(_SC_FLAG))
            if bad_flag:
                problems.append(("致命", f"scorecard: 項目 {bad_flag} がコード (FLAG_FEATS) に無い"))
            msg = check_si_variant(sm)
            if msg:
                problems.append(("致命", f"scorecard: {msg}"))
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
    sv = ft.build_si_variant(races_df, results_df)     # #168 (基準表が無ければ None)
    grouped = {rid: g for rid, g in results_df.groupby("race_id")}
    out = {}
    for rid in race_ids:
        race = races_df[races_df.race_id == rid]
        rr = grouped.get(rid)
        if race.empty or rr is None or rr.empty:
            continue
        rows = ft.compute_features_fast(
            race.iloc[0].to_dict(), rr.to_dict("records"), hh, js, ts, cs, si,
            sire_course=ped[0], sire_surface=ped[1], damsire_course=ped[2], damsire_surface=ped[3],
            si_variant=sv)
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
    ap.add_argument("--races", type=int, default=6, help="照合する確定レースの数")
    ap.add_argument("--future-races", type=int, default=4,
                    help="照合する発走前レースの数 (出走馬登録済み・着順未確定。無ければ省略)")
    ap.add_argument("--tol", type=float, default=DEFAULT_TOL, help="一致率の下限")
    ap.add_argument("--strict", action="store_true", help="既知 skew も致命扱いにする")
    ap.add_argument("--models-only", action="store_true", help="モデル健全性だけ検査")
    ap.add_argument("--db", default=None,
                    help="照合に使う DB (既定: リポジトリの keiba.db)。コピーで検査する時に使う")
    ap.add_argument("--race-ids", default=None,
                    help="照合するレースをカンマ区切りで指定 (既定: 直近の確定レース)")
    args = ap.parse_args()
    db_path = args.db or os.path.join(ROOT, "keiba.db")
    if args.db:
        import database
        database.DB_PATH = args.db

    _report_si_variant()
    print("🔍 モデル健全性を検査...")
    problems = check_models()
    for lv, msg in problems:
        print(f"  {'🚨' if lv == '致命' else '⚠️'} {msg}")
    if not problems:
        print("  ✅ 木の本数・特徴量数ともに正常")
    fatal = sum(1 for lv, _ in problems if lv == "致命")
    if args.models_only:
        return 2 if fatal else 0

    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    if args.race_ids:
        ids = [x.strip() for x in args.race_ids.split(",") if x.strip()]
        done = {r[0] for r in conn.execute(
            f"SELECT DISTINCT race_id FROM results WHERE finish_position > 0 "
            f"AND race_id IN ({','.join('?' * len(ids))})", ids)} if ids else set()
        race_ids = [x for x in ids if x in done]
        future_ids = [x for x in ids if x not in done]
    else:
        race_ids = [r[0] for r in conn.execute(
            """SELECT r.race_id FROM races r
               WHERE EXISTS (SELECT 1 FROM results res WHERE res.race_id=r.race_id AND res.finish_position>0)
               ORDER BY r.race_date DESC LIMIT ?""", (args.races,))]
        future_ids = _future_race_ids(conn, args.future_races)
    conn.close()
    if not race_ids and not future_ids:
        print("📭 照合対象のレースが無い")
        return 1
    print(f"\n🔍 学習側 vs 推論側の特徴量パリティ (確定 {len(race_ids)}レース"
          f" + 発走前 {len(future_ids)}レース)...")
    if not future_ids and not args.race_ids:
        print("  ℹ️ 発走前のレース (出走馬登録済み・着順未確定) が DB に無い — 確定レースだけ照合する")
    all_ids = list(dict.fromkeys(race_ids + future_ids))
    train = build_train_side(all_ids)
    serve = build_serve_side(all_ids)
    from fast_train import get_feature_columns
    extra = list(EXTRA_COLS)
    if not _has_si_table():
        # 基準表が無い時は両側とも V2d の列を作らない (= 照合対象が無い)。点数表が V2d を
        # 要求していれば check_models が致命にしている (check_si_variant)。
        from ml.speed_variant import SV_COLS
        extra = [c for c in extra if c not in SV_COLS]
        print(f"  ℹ️ V2d の基準表が無い — {list(SV_COLS)} は照合しない")
    cols = get_feature_columns() + [c for c in extra if c not in get_feature_columns()]
    strict_cols = scorecard_strict_cols()
    bad_fatal, bad_warn = [], []
    any_common = False
    for tag, ids in (("確定", race_ids), ("発走前", future_ids)):
        idset = set(ids)
        common = sorted(k for k in set(train) & set(serve) if k[0] in idset)
        if not ids:
            continue
        if not common:
            print(f"  🚨 [{tag}] 突き合わせ可能な馬が0頭 — どちらかの経路が動いていない")
            bad_fatal.append((f"[{tag}] 全列", "照合0頭", 0.0))
            continue
        any_common = True
        print(f"  [{tag}] 照合対象: {len(common)}頭 × {len(cols)}特徴量"
              f" (LGB {len(get_feature_columns())} + 点数表専用の列 {len(cols) - len(get_feature_columns())})"
              f" — 点数表の列 {len(strict_cols & set(cols))}個は厳密一致")
        _report_fallback("学習側", train, common)
        _report_fallback("推論側", serve, common)
        fa, wa = _compare(train, serve, common, cols, strict_cols, args)
        bad_fatal += [(f"[{tag}] {c}", m, r) for c, m, r in fa]
        bad_warn += [(f"[{tag}] {c}", m, r) for c, m, r in wa]
    if not any_common:
        return 2
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


def _future_race_ids(conn, n):
    """発走前のレース (出走馬の行はあるが着順>0 の行が無い) から n 件を日付・場が散るように選ぶ。"""
    if n <= 0:
        return []
    ids = [r[0] for r in conn.execute(
        """SELECT r.race_id FROM races r
           WHERE r.race_date IS NOT NULL AND r.race_date <> ''
             AND EXISTS (SELECT 1 FROM results x WHERE x.race_id = r.race_id)
             AND NOT EXISTS (SELECT 1 FROM results x WHERE x.race_id = r.race_id
                             AND x.finish_position > 0)
             AND r.race_date >= (SELECT MAX(ra.race_date) FROM races ra
                                 JOIN results y ON y.race_id = ra.race_id
                                 WHERE y.finish_position > 0)
           ORDER BY r.race_date, r.race_id""")]
    if len(ids) <= n:
        return ids
    step = len(ids) / n
    return [ids[int(i * step)] for i in range(n)]


def _compare(train, serve, common, cols, strict_cols, args):
    """列ごとの一致率。点数表の列は厳密一致 (100%)、それ以外は --tol (CORR_ONLY は相関でも可)。
    戻り値: (致命のリスト, 警告のリスト) — 要素は (列, メッセージ, 一致率)。"""
    bad_fatal, bad_warn = [], []
    for c in cols:
        pairs = [(k, train[k].get(c), serve[k].get(c)) for k in common
                 if c in train[k] and c in serve[k]]
        if not pairs:
            # 点数表の列が片側にしか無い = 学習と本番で別物 (または本番で点数0) → 致命
            (bad_fatal if c in strict_cols else bad_warn).append((c, "どちらかに列が無い", 0.0))
            continue
        # #167: SI は学習側 (累積和の平均) と推論側 (SQL AVG) で round(…,1) の境界が 0.1 ずれる走がある
        # (既存のずれ、点数表の区分 = レース内順位にはほぼ効かない)。丸め1単位までは一致とみなす。
        _tol = 0.1 + 1e-9 if c in SI_ROUND_TOL else 1e-6
        same = sum(1 for _, a, b in pairs if _same(a, b, _tol))
        rate = same / len(pairs)
        if c in strict_cols:
            if same == len(pairs):
                continue
            ex = [(k, a, b) for k, a, b in pairs if not _same(a, b, _tol)][:3]
            msg = (f"一致 {same}/{len(pairs)} (点数表の列は100%必須) 例: "
                   + "; ".join(f"{k[0]} {k[1]}番 学習={a!r} 推論={b!r}" for k, a, b in ex))
            (bad_warn if (c in KNOWN_SKEW and not args.strict) else bad_fatal).append((c, msg, rate))
            continue
        if rate >= args.tol:
            continue
        # 相関で見る列は相関が高ければ許容
        if c in CORR_ONLY:
            try:
                import statistics as st
                xs = [float(a or 0) for _, a, _ in pairs]
                ys = [float(b or 0) for _, _, b in pairs]
                if len(set(xs)) > 1 and len(set(ys)) > 1 and st.correlation(xs, ys) >= 0.95:
                    continue
            except Exception:
                pass
        (bad_warn if (c in KNOWN_SKEW and not args.strict) else bad_fatal).append(
            (c, f"一致率 {100*rate:.0f}%", rate))
    return bad_fatal, bad_warn


if __name__ == "__main__":
    sys.exit(main())
