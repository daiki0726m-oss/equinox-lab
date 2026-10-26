#!/usr/bin/env python3
"""馬場差補正スピード指数 V2d の基準表を作る (#168)。定義は ml/speed_variant.py。

  python3 scripts/build_si_variant.py --db keiba.db --cutoff 2025-05-10 \\
      --select-c --feature-cache feat_2020.pkl --out models/si_variant_table.json

手順
  1. レース表 (P3 = −距離係数 × 1〜3着の平均時計、コース・クラス・日) を DB から作る
  2. 2020-01-01〜カットオフ日の非障害の JRA 平地レースで P3 = μ[コース] + κ[クラス] + δ[日] を
     交互平均で当てはめ、κ̂ をレース数重みで芝ダごとに平均0に正規化 (μ̂ に同じ量を足す)
  3. ダートの走に足すずらし幅 c を選ぶ (--select-c)。**規則は事前に決めたもの**:
       窓 = [--c-fit-from (既定 2020-01-01 = 基準表の窓の初日。出荷した基準表の規則), カットオフ日] の点数表の行
           (確定・非障害・6頭以上) で、si_avg / si_latest を V2d(c) に差し替えた点数表を
           当てはめ直す (区分点 = ロジスティック回帰、連続項目+JRA初出走 = レース内ソフトマックス、
           どちらも同じ窓) → その窓のレース内ソフトマックスの対数尤度が最大の c。
       格子 = −10〜+30 の2刻み。同点なら |c| の小さい方 (同じなら小さい方)。
     評価期間 (カットオフ日より後) は一切見ない。--c で固定値も渡せる (規則は表に記録)。
  4. 基準表 (μ̂・κ̂・件数・c・カットオフ・窓・作った日・時期区分・距離係数・指紋) を JSON に書く。
     学習側 (fast_train) と推論側 (ml/features) は同じファイルを読む。

おまけ
  --augment-cache IN --augment-out OUT : 既存の特徴量テーブル (fast_train.build_feature_table の
      キャッシュ) に si_avg_v / si_latest_v / si_v_nfb / sf_first_switch / sf_other_share5 を
      足す (fast_train.augment_scorecard_columns = compute_features_fast と同じ関数)。
  --compat-verified : 検証時の版 (年齢区分は出走馬の生年だけ / そろっていない日も使う) で作る。
      検証済みの値 (h_combine/si_comb.pkl) との一致確認用。本番には使わない。
"""
import argparse
import json
import os
import sys
import time

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

from ml import speed_variant as sv  # noqa: E402

C_RULE = ("基準表の窓 [c_fit_from, cutoff] の点数表の行 (確定・非障害・6頭以上) で、"
          "si_avg/si_latest を V2d(c) に差し替えた点数表を同じ窓で当てはめ直し "
          "(区分点=ロジスティック回帰 C=1、連続項目 form_cls5/tb_min5 と JRA初出走=レース内ソフトマックス)、"
          "その窓のレース内ソフトマックスの対数尤度が最大の c。同点は |c| の小さい方。")


def load_frames():
    """fast_train と同じ読み方 (results_df に race 情報と birth_year)。"""
    import fast_train as ft
    races_df, results_df, _ = ft.load_all_data()
    cols = ["race_date", "venue", "distance", "surface", "track_condition", "weather", "horse_count"]
    info = races_df.set_index("race_id")[cols].to_dict("index")
    for col in cols:
        results_df[col] = results_df["race_id"].map(lambda rid, c=col: info.get(rid, {}).get(c, ""))
    return races_df, results_df


def build_table(races_df, results_df, cutoff, fit_from, age_rule, require_complete, c=0.0, extra=None):
    t0 = time.time()
    rt = sv.race_table(races_df, results_df, age_rule=age_rule, apply_from=fit_from)
    fitd = sv.fit_table(rt, cutoff, fit_from=fit_from)
    table = sv.make_table(fitd, cutoff, c=c, age_rule=age_rule, require_complete=require_complete,
                          fit_from=fit_from, extra=extra)
    flat = rt[rt["flat"]]
    print(f"  レース表 {len(rt)} (平地 {len(flat)}, 時計そろう {int(flat['has_t3'].sum())}, "
          f"そろっていない日の平地レース {int((~flat['complete']).sum())})")
    print(f"  当てはめ {fitd['fit_races']}レース ({fitd['fit_first_date']}〜{fitd['fit_last_date']}) "
          f"コース {len(fitd['mu'])} / クラス {len(fitd['kap'])} / 残差sd {fitd['resid_sd']:.2f} "
          f"/ κ̂ の芝ダ平均 (正規化前) {fitd['kap_shift']} ({time.time() - t0:.1f}秒)")
    return table, rt, fitd


def row_components(df_rows, si_cache, lookup_v, dirt_map):
    """行ごとの最大5走の [値 (c=0), ずらす走か]。値は ml.speed_variant.row_components と同じ規則。"""
    n = len(df_rows)
    V = np.full((n, sv.WINDOW), np.nan)
    SH = np.zeros((n, sv.WINDOW))
    for i, (hid, T) in enumerate(zip(df_rows["horse_id"].values, df_rows["race_date"].values)):
        runs = [s for s in si_cache.get(hid, []) if s["race_date"] < T][:sv.WINDOW]
        comp = sv.row_components(hid, runs, lookup_v, lambda s, _T=T: si_cache.si(s, _T))
        for j, ((v, used), s) in enumerate(zip(comp, runs)):
            V[i, j] = v
            SH[i, j] = 1.0 if (used and dirt_map.get((hid, s["race_id"]), False)) else 0.0
    return V, SH


def rows_at_c(V, SH, c):
    M = V + c * SH
    has = np.isfinite(M[:, 0])
    cnt = np.isfinite(M).sum(1)
    with np.errstate(invalid="ignore", divide="ignore"):
        avg = np.where(has, np.nansum(M, axis=1) / np.maximum(cnt, 1), 0.0)
    lat = np.where(has, M[:, 0], 0.0)
    return avg, lat


def select_c(table, races_df, results_df, feature_cache, c_fit_from, cutoff, grid, drop, lin_cols, col_map):
    import fast_train as ft
    import train_scorecard as ts
    t0 = time.time()
    print(f"📦 特徴量テーブル: {feature_cache}")
    df = pd.read_pickle(feature_cache)
    d = ts.prep_lin_frame(df)
    d = ts.window_rows(d, c_fit_from, cutoff)
    if d.empty:
        raise SystemExit(f"c を選ぶ窓 {c_fit_from}〜{cutoff} に行が無い")
    print(f"  c を選ぶ窓 {c_fit_from}〜{cutoff}: {d['race_id'].nunique()}レース / {len(d)}頭")
    si_cache = ft.build_speed_index_cache(results_df, races_df)
    lk = sv.build_run_lookup(races_df, results_df, table, c=0.0, with_dirt=True)
    lookup_v = {k: v for k, (v, _) in lk.items()}
    dirt_map = {k: dd for k, (_, dd) in lk.items()}
    V, SH = row_components(d, si_cache, lookup_v, dirt_map)
    n_run = int(np.isfinite(V).sum())
    n_v = sum(1 for hid, T in zip(d["horse_id"].values, d["race_date"].values)
              for s in [x for x in si_cache.get(hid, []) if x["race_date"] < T][:sv.WINDOW]
              if (hid, s["race_id"]) in lookup_v)
    print(f"  行の材料を作成 ({time.time() - t0:.0f}秒) — 行の走 {n_run} のうち V2d {n_v} "
          f"(現行の指数で埋めた走 {n_run - n_v}, {100 * (n_run - n_v) / max(n_run, 1):.1f}%) / "
          f"ずらす (ダートの V2d) 走 {int(SH.sum())}")
    tgt = {"si_avg": col_map.get("si_avg", "si_avg_v"), "si_latest": col_map.get("si_latest", "si_latest_v")}
    ll = {}
    for c in grid:
        a, l = rows_at_c(V, SH, float(c))
        d[tgt["si_avg"]] = a
        d[tgt["si_latest"]] = l
        st1 = ts.fit_pts_stage(d, drop, col_map)
        _, llc, _ = ts.fit_lin_stage(d, st1, drop, lin_cols)
        ll[c] = llc
        print(f"    c={c:+3d}  対数尤度 {llc:.2f}  ({time.time() - t0:.0f}秒)", flush=True)
    best = max(grid, key=lambda c: (round(ll[c], 6), -abs(c), -c))
    print(f"  → c = {best:+d} (c=0 との差 {ll[best] - ll.get(0, np.nan):+.2f})")
    return best, ll, {"rows": int(len(d)), "races": int(d["race_id"].nunique())}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=None, help="使う DB (既定: リポジトリの keiba.db)")
    ap.add_argument("--cutoff", default="2025-05-10", help="基準表の窓の最終日 (点数表の区分点の窓の終わり)")
    ap.add_argument("--fit-from", default=sv.FIT_FROM)
    ap.add_argument("--out", default=sv.TABLE_PATH)
    ap.add_argument("--c", type=float, default=None, help="ダートのずらし幅を固定値で与える")
    ap.add_argument("--select-c", action="store_true", help="事前に決めた規則で c を選ぶ (要 --feature-cache)")
    ap.add_argument("--feature-cache", default=None, help="特徴量テーブルの pickle (c-fit-from 以降を含む)")
    ap.add_argument("--c-fit-from", default="2020-01-01",
                    help="c を選ぶ窓の初日 (窓の終わり = --cutoff)。既定は基準表の窓の初日 (事前登録の規則)")
    ap.add_argument("--c-grid", default="-10:30:2", help="c の格子 (開始:終了:刻み)")
    ap.add_argument("--drop", default="avg_finish_5r,margin_best")
    ap.add_argument("--lin", default="form_cls5,tb_min5")
    ap.add_argument("--col-map", default="si_avg=si_avg_v,si_latest=si_latest_v")
    ap.add_argument("--compat-verified", action="store_true",
                    help="検証時の版 (年齢区分=生年だけ / そろっていない日も使う)。一致確認用")
    ap.add_argument("--augment-cache", default=None, help="この特徴量テーブルに点数表専用の列を足す")
    ap.add_argument("--augment-out", default=None, help="--augment-cache の出力先")
    ap.add_argument("--augment-only", action="store_true",
                    help="基準表を作らず、--out にある既存の基準表で --augment-cache だけ行う")
    ap.add_argument("--report", default=None, help="c の選択の記録を JSON で書く")
    args = ap.parse_args()
    if args.db:
        import database
        database.DB_PATH = args.db
    import train_scorecard as ts

    if args.augment_only:
        if not args.augment_cache:
            raise SystemExit("--augment-only には --augment-cache が必要")
        return augment(args.augment_cache, args.augment_out, args.out)

    age_rule = "birth" if args.compat_verified else "name_then_birth"
    require_complete = not args.compat_verified
    print(f"🔧 基準表 (カットオフ {args.cutoff}, 窓 {args.fit_from}〜, 年齢区分 {age_rule}, "
          f"そろった日だけ {require_complete})")
    races_df, results_df = load_frames()
    table, _, fitd = build_table(races_df, results_df, args.cutoff, args.fit_from, age_rule, require_complete)

    extra = {}
    if args.select_c:
        if not args.feature_cache:
            raise SystemExit("--select-c には --feature-cache が必要")
        if args.c_fit_from > args.cutoff:
            raise SystemExit("--c-fit-from がカットオフより後")
        a, b, s = (int(x) for x in args.c_grid.split(":"))
        grid = list(range(a, b + 1, s))
        col_map = ts.parse_col_map(args.col_map)
        drop = [c for c in args.drop.split(",") if c]
        lin_cols = [c for c in args.lin.split(",") if c]
        c, ll, info = select_c(table, races_df, results_df, args.feature_cache, args.c_fit_from,
                               args.cutoff, grid, drop, lin_cols, col_map)
        extra = {"c_rule": {"rule": C_RULE, "window": [args.c_fit_from, args.cutoff], "grid": grid,
                            "drop": drop, "lin": lin_cols, "col_map": col_map,
                            "ll": {str(k): v for k, v in ll.items()}, "chosen": c, **info,
                            "selected_at": time.strftime("%Y-%m-%d %H:%M:%S")}}
    elif args.c is not None:
        c = float(args.c)
        extra = {"c_rule": {"rule": "固定値 (--c で指定)", "chosen": c,
                            "selected_at": time.strftime("%Y-%m-%d %H:%M:%S")}}
    else:
        c = 0.0
        print("  ⚠️ c を選んでいない (--select-c も --c も無い) → c=0")
        extra = {"c_rule": {"rule": "未選択 (c=0)", "chosen": 0.0}}
    if args.compat_verified:
        extra["compat_verified"] = True
    table = sv.make_table(fitd, args.cutoff, c=c, age_rule=age_rule, require_complete=require_complete,
                          fit_from=args.fit_from, extra=extra)
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(table, f, ensure_ascii=False, indent=1)
    print(f"💾 {args.out} (c={c:+g}, 指紋 {table['table_hash']})")
    if args.report:
        with open(args.report, "w", encoding="utf-8") as f:
            json.dump(extra, f, ensure_ascii=False, indent=1)

    if args.augment_cache:
        augment(args.augment_cache, args.augment_out, args.out)


def augment(cache, out, table_path):
    import fast_train as ft
    out = out or cache.replace(".pkl", "_sv.pkl")
    if os.path.abspath(out) == os.path.abspath(cache):
        raise SystemExit("--augment-out が入力と同じ (上書きしない)")
    df = pd.read_pickle(cache)
    df = ft.augment_scorecard_columns(df, table_path=table_path)
    df.to_pickle(out)
    print(f"💾 {out} ({len(df)}行, 基準表 {df.attrs.get('si_variant_table_hash')}, "
          f"現行の指数で埋めた走の割合 {sv.fallback_share(df)})")


if __name__ == "__main__":
    main()
