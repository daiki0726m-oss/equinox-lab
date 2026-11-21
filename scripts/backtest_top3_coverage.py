#!/usr/bin/env python3
"""top-3 捕捉 backtest (#74): 「6頭の印に、実際の3着内3頭が何頭入るか」を最大化する観点で
ML選定 vs 能力モデル(オッズ非依存) vs ブレンド を比較。

ユーザー指示 (2026-06-15):
  - JRA控除は無視、純粋に投資→回収
  - 単勝1点・平場掛けは本質でない
  - 6頭の印のうち、3着内に入る馬の捕捉確率を最大化したい

指標:
  - capture_avg : 6頭の印が実際の top-3 を平均何頭捕捉 (0-3)
  - full_rate   : top-3 を 3頭すべて捕捉した率 (= 三連複が当たる下地)
  - axis_rate   : ◎(各手法の1位)が3着内 かつ 残り2頭も6頭内 (◎軸三連複の的中率)

特徴量は /tmp ではなく ~/.cctmp にキャッシュ (再実験を高速化)。
実行: python3 scripts/backtest_top3_coverage.py [--year-from 2020] [--year-to 2025] [--rebuild]
"""
from __future__ import annotations
import argparse, json, os, pickle, sys, time
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
CACHE = Path(os.path.expanduser("~/.cctmp/top3_scored.pkl"))


def build_scored(year_from, year_to):
    """特徴量 + ML rank + 能力スコア + odds + finish を計算して返す (重い)。"""
    init_db()
    print("📥 データロード...", flush=True)
    races_df, results_df, payouts_df = load_all_data()
    race_info = races_df.set_index("race_id")[
        ["race_date", "venue", "distance", "surface", "track_condition",
         "horse_count", "race_name", "grade"]].to_dict("index")
    for col in ["race_date", "venue", "distance", "surface", "track_condition",
                "horse_count", "race_name", "grade"]:
        results_df[col] = results_df["race_id"].map(lambda rid, c=col: race_info.get(rid, {}).get(c, ""))
    hh = build_horse_history(results_df, races_df)
    js, ts, cs = build_jockey_trainer_stats(results_df, races_df)
    si = build_speed_index_cache(results_df, races_df)
    yr = races_df["race_date"].str[:4].astype(str)
    ids = set(races_df[(yr >= str(year_from)) & (yr <= str(year_to))]["race_id"])
    grouped = {rid: g for rid, g in results_df.groupby("race_id") if rid in ids}
    print(f"🔧 特徴量計算 {len(ids)}レース...", flush=True)
    rows = []
    for n, race in enumerate(races_df.itertuples(index=False)):
        if race.race_id not in ids:
            continue
        rr = grouped.get(race.race_id)
        if rr is None or rr.empty:
            continue
        rows.extend(compute_features_fast(race._asdict(), rr.to_dict("records"), hh, js, ts, cs, si))
        if (n + 1) % 3000 == 0:
            print(f"  ... {n+1}", flush=True)
    df = pd.DataFrame(rows)
    df["race_date"] = df["race_id"].map(lambda rid: race_info.get(rid, {}).get("race_date", ""))
    feat = get_feature_columns()
    abil_feat = [c for c in feat if c not in MARKET_COLS]
    with open(MODEL_DIR / "model_rank.pkl", "rb") as f:
        model_rank = pickle.load(f)
    with open(MODEL_DIR / "model_top3.pkl", "rb") as f:
        model_top3 = pickle.load(f)
    with open(MODEL_DIR / "model_ability_win.pkl", "rb") as f:
        model_abil = pickle.load(f)
    with open(MODEL_DIR / "calibrator_ability_win.pkl", "rb") as f:
        calib_abil = pickle.load(f)
    t = df[df["finish_position"] > 0].copy()
    t["rank_score"] = model_rank.predict(t[feat].fillna(0))
    t["pred_top3"] = model_top3.predict(t[feat].fillna(0))  # 複勝率モデル (3着内確率)
    raw = model_abil.predict(t[abil_feat].fillna(0), num_iteration=getattr(model_abil, "best_iteration", None))
    t["abil"] = calib_abil.predict(raw)
    keep = ["race_id", "race_date", "horse_number", "finish_position", "odds",
            "rank_score", "pred_top3", "abil"]
    return t[keep].copy()


def coverage(scored, score_col, k=6):
    """score_col 上位k頭が 実際の top-3 を何頭捕捉するか + ◎軸的中。年別集計。"""
    by_year = defaultdict(lambda: {"races": 0, "cap": 0, "full": 0, "axis": 0})
    for rid, g in scored.groupby("race_id"):
        if len(g) < k:
            continue
        year = str(g["race_date"].iloc[0])[:4]
        g = g.sort_values(score_col, ascending=False)
        top_k = set(int(h) for h in g.head(k)["horse_number"])
        axis = int(g.iloc[0]["horse_number"])  # ◎ = 各手法の1位
        actual_top3 = set(int(r.horse_number) for r in g.itertuples() if r.finish_position in (1, 2, 3))
        if len(actual_top3) < 3:
            continue
        cap = len(top_k & actual_top3)
        b = by_year[year]
        b["races"] += 1
        b["cap"] += cap
        b["full"] += 1 if cap == 3 else 0
        # ◎軸三連複的中 = ◎が3着内 かつ top-3 全部が top_k 内
        b["axis"] += 1 if (axis in actual_top3 and actual_top3 <= top_k) else 0
    return by_year


def report(name, by_year):
    tot = {"races": 0, "cap": 0, "full": 0, "axis": 0}
    yr_full = {}
    for y, b in sorted(by_year.items()):
        for kk in tot:
            tot[kk] += b[kk]
        yr_full[y] = round(100 * b["full"] / b["races"]) if b["races"] else 0
    r = tot["races"] or 1
    print(f"{name:18s} 捕捉{tot['cap']/r:.2f}/3  完全捕捉{100*tot['full']/r:4.1f}%  "
          f"◎軸的中{100*tot['axis']/r:4.1f}%  (n={tot['races']})")
    print(f"{'':18s} 年別完全捕捉%: " + " ".join(f"{y}:{v}" for y, v in yr_full.items()))
    return {"races": tot["races"], "capture_avg": round(tot["cap"] / r, 3),
            "full_rate": round(100 * tot["full"] / r, 1), "axis_rate": round(100 * tot["axis"] / r, 1),
            "by_year_full": yr_full}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--year-from", type=int, default=2020)
    ap.add_argument("--year-to", type=int, default=2025)
    ap.add_argument("--rebuild", action="store_true")
    ap.add_argument("--out", default="docs/analysis/backtest_top3_coverage.json")
    args = ap.parse_args()
    t0 = time.time()

    if CACHE.exists() and not args.rebuild:
        print(f"📦 キャッシュ利用: {CACHE}")
        scored = pickle.load(open(CACHE, "rb"))
    else:
        scored = build_scored(args.year_from, args.year_to)
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        pickle.dump(scored, open(CACHE, "wb"))
        print(f"💾 スコア済みデータをキャッシュ ({len(scored)}行)")

    # 市場・能力・ブレンドのスコア列を用意
    # 市場の暗黙勝率 (レース内正規化、控除無視で純粋に 1/odds)
    def norm(s):
        v = s.clip(lower=1e-9)
        inv = 1.0 / v
        return inv / inv.sum()
    scored = scored.copy()
    scored["mkt"] = scored.groupby("race_id")["odds"].transform(norm)
    scored["abil_n"] = scored.groupby("race_id")["abil"].transform(lambda s: s / s.sum() if s.sum() else s)
    # OOS最適 stacked 重み (#73): 市場:能力 ≈ 1.33:0.42 ≈ 0.76:0.24
    scored["blend"] = 0.76 * scored["mkt"] + 0.24 * scored["abil_n"]

    print("\n" + "=" * 78)
    print(f"6頭の印が「実際の3着内3頭」を捕捉する力 ({args.year_from}-{args.year_to})")
    print("=" * 78)
    out = {}
    out["ml_rank"] = report("ML rank/勝(現行)", coverage(scored, "rank_score"))
    out["pred_top3"] = report("複勝率モデル", coverage(scored, "pred_top3"))
    out["market_odds"] = report("市場(オッズ)", coverage(scored, "mkt"))
    out["ability"] = report("能力モデル", coverage(scored, "abil"))
    out["blend_76_24"] = report("ブレンド76:24", coverage(scored, "blend"))
    Path(ROOT / args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(ROOT / args.out).write_text(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"\n💾 {args.out}  ({time.time()-t0:.0f}s)")


if __name__ == "__main__":
    main()
