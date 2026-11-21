#!/usr/bin/env python3
"""2軸印 backtest (#72): 現◎(ML/オッズ偏重) vs 能力◎(オッズ非依存) vs 2軸 を
2020-2025 全レースで単勝ROI比較。同一レース集合・年別・単日支配チェック付き。

実行: python3 scripts/backtest_2axis.py [--year-from 2020] [--year-to 2025] [--out PATH]

検証する戦略 (各レース1点・単勝100円):
  ml_top      : ML 1位 (= 現行の◎。オッズ偏重)
  ability_top : 能力モデル 1位 (オッズ非依存)
  agree_only  : 能力1位==市場1人気 のときだけ その馬 (鉄板◎、不一致はスキップ)
  value_only  : 能力1位!=市場1人気 のときだけ 能力1位 (★妙味プレー)
  twoaxis     : 一致時=市場1人気 / 不一致時=能力1位 (2軸◎の素案)
"""
from __future__ import annotations
import argparse, json, pickle, sys, time
from collections import defaultdict
from pathlib import Path
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from fast_train import (
    load_all_data, build_horse_history, build_jockey_trainer_stats,
    build_speed_index_cache, compute_features_fast, get_feature_columns,
)
from database import init_db

MODEL_DIR = ROOT / "models"
MARKET_COLS = ("odds_log", "popularity_norm")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--year-from", type=int, default=2020)
    ap.add_argument("--year-to", type=int, default=2025)
    ap.add_argument("--out", default="docs/analysis/backtest_2axis.json")
    args = ap.parse_args()

    t0 = time.time()
    init_db()
    print(f"📊 2軸印 backtest {args.year_from}-{args.year_to}")
    print("📥 データロード...")
    races_df, results_df, payouts_df = load_all_data()
    race_info = races_df.set_index("race_id")[
        ["race_date", "venue", "distance", "surface", "track_condition",
         "horse_count", "race_name", "grade"]
    ].to_dict("index")
    for col in ["race_date", "venue", "distance", "surface", "track_condition",
                "horse_count", "race_name", "grade"]:
        results_df[col] = results_df["race_id"].map(lambda rid, c=col: race_info.get(rid, {}).get(c, ""))

    horse_history = build_horse_history(results_df, races_df)
    jockey_stats, trainer_stats, combo_stats = build_jockey_trainer_stats(results_df, races_df)
    si_cache = build_speed_index_cache(results_df, races_df)

    yr = races_df["race_date"].str[:4].astype(str)
    test_ids = set(races_df[(yr >= str(args.year_from)) & (yr <= str(args.year_to))]["race_id"])
    print(f"🎯 対象 {len(test_ids)} レース、特徴量計算中...")
    results_grouped = {rid: g for rid, g in results_df.groupby("race_id") if rid in test_ids}
    all_rows = []
    for n, race in enumerate(races_df.itertuples(index=False)):
        if race.race_id not in test_ids:
            continue
        rr = results_grouped.get(race.race_id)
        if rr is None or rr.empty:
            continue
        all_rows.extend(compute_features_fast(
            race._asdict(), rr.to_dict("records"),
            horse_history, jockey_stats, trainer_stats, combo_stats, si_cache))
        if (n + 1) % 2000 == 0:
            print(f"  ... {n+1} 行={len(all_rows)}", flush=True)

    df = pd.DataFrame(all_rows)
    df["race_date"] = df["race_id"].map(lambda rid: race_info.get(rid, {}).get("race_date", ""))
    feat = get_feature_columns()
    abil_feat = [c for c in feat if c not in MARKET_COLS]

    # モデル
    with open(MODEL_DIR / "model_rank.pkl", "rb") as f:
        model_rank = pickle.load(f)
    with open(MODEL_DIR / "model_ability_win.pkl", "rb") as f:
        model_abil = pickle.load(f)
    with open(MODEL_DIR / "calibrator_ability_win.pkl", "rb") as f:
        calib_abil = pickle.load(f)

    test = df[(df["finish_position"] > 0)].copy()
    X = test[feat].fillna(0)
    test["rank_score"] = model_rank.predict(X)
    Xa = test[abil_feat].fillna(0)
    raw = model_abil.predict(Xa, num_iteration=getattr(model_abil, "best_iteration", None))
    test["abil"] = calib_abil.predict(raw)

    # 戦略集計: {strat: {year: {spend,ret,win,n}}}, 単日: {strat: {date: {spend,ret}}}
    strat_year = defaultdict(lambda: defaultdict(lambda: {"spend": 0, "ret": 0, "win": 0, "n": 0}))
    strat_day = defaultdict(lambda: defaultdict(lambda: {"spend": 0, "ret": 0}))
    AMT = 100

    for rid, g in test.groupby("race_id"):
        g = g[g["odds"] > 0]
        if len(g) < 5:
            continue
        year = str(race_info.get(rid, {}).get("race_date", ""))[:4]
        date = str(race_info.get(rid, {}).get("race_date", ""))
        # ML 1位 (softmax 不要、rank_score 最大)
        ml_top = int(g.sort_values("rank_score", ascending=False).iloc[0]["horse_number"])
        # 能力1位 (レース内最大)
        ab_top = int(g.sort_values("abil", ascending=False).iloc[0]["horse_number"])
        # 市場1人気 = 最小オッズ (raw popularity 列は compute_features_fast に無いため、
        # 最終オッズで判定。むしろ最終オッズの方が市場の集合知を正確に反映)
        mk_fav = int(g.sort_values("odds").iloc[0]["horse_number"])
        # 着順・オッズ map
        fin = {int(r.horse_number): int(r.finish_position) for r in g.itertuples()}
        odd = {int(r.horse_number): float(r.odds) for r in g.itertuples()}

        def bet(strat, hn):
            if hn is None:
                return
            s = strat_year[strat][year]; d = strat_day[strat][date]
            s["spend"] += AMT; s["n"] += 1; d["spend"] += AMT
            if fin.get(hn) == 1:
                ret = AMT * odd.get(hn, 0)
                s["ret"] += ret; s["win"] += 1; d["ret"] += ret

        bet("ml_top", ml_top)
        bet("ability_top", ab_top)
        bet("agree_only", ab_top if ab_top == mk_fav else None)
        bet("value_only", ab_top if ab_top != mk_fav else None)
        bet("twoaxis", mk_fav if ab_top == mk_fav else ab_top)

    # レポート
    def roi(d):
        return 100 * d["ret"] / d["spend"] if d["spend"] else 0
    out = {"period": f"{args.year_from}-{args.year_to}", "strategies": {}}
    print("\n" + "=" * 72)
    print(f"{'戦略':14s} {'n':>6s} {'勝率':>6s} {'ROI':>6s} | 年別ROI")
    print("-" * 72)
    for strat in ("ml_top", "ability_top", "agree_only", "value_only", "twoaxis"):
        tot = {"spend": 0, "ret": 0, "win": 0, "n": 0}
        years = {}
        for y, d in sorted(strat_year[strat].items()):
            for k in tot:
                tot[k] += d[k]
            years[y] = round(roi(d))
        wr = 100 * tot["win"] / tot["n"] if tot["n"] else 0
        # 単日支配
        days = strat_day[strat]
        best_day = max(days, key=lambda dd: days[dd]["ret"] - days[dd]["spend"]) if days else None
        ex_s = tot["spend"] - (days[best_day]["spend"] if best_day else 0)
        ex_r = tot["ret"] - (days[best_day]["ret"] if best_day else 0)
        roi_all = roi(tot)
        roi_exbest = 100 * ex_r / ex_s if ex_s else 0
        yr_str = " ".join(f"{y}:{r}%" for y, r in years.items())
        print(f"{strat:14s} {tot['n']:>6d} {wr:>5.0f}% {roi_all:>5.0f}% | {yr_str}")
        print(f"{'':14s} {'最大利益日 '+str(best_day)+' 除外':>30s} ROI {roi_all:.0f}%→{roi_exbest:.0f}%")
        out["strategies"][strat] = {
            "n": tot["n"], "win_rate": round(wr, 1), "roi": round(roi_all, 1),
            "roi_ex_best_day": round(roi_exbest, 1), "best_day": best_day, "by_year": years,
        }
    outp = ROOT / args.out
    outp.parent.mkdir(parents=True, exist_ok=True)
    outp.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"\n💾 {args.out}  ({time.time()-t0:.0f}s)")


if __name__ == "__main__":
    main()
