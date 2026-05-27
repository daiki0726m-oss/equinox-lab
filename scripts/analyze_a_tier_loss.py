#!/usr/bin/env python3
"""A 層 1勝 × ROI 23.9% の loss pattern 分析

historical_backtest と同じデータパイプラインで、A 層に絞って:
  - ◎ の実着順分布
  - 相手 (○▲△×注) の着順分布
  - venue / distance / surface 別
  - ◎ オッズ帯別
  - hit / miss の細分類 (◎着外 vs 相手着外)

を集計する。

Usage:
    python3 scripts/analyze_a_tier_loss.py [--year 2025] [--class 1勝]
"""
from __future__ import annotations
import argparse
import json
import os
import pickle
import sys
import time
from collections import defaultdict
from itertools import combinations
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fast_train import (
    load_all_data, build_horse_history,
    build_jockey_trainer_stats, build_speed_index_cache,
    compute_features_fast, get_feature_columns
)
from strategy.betting import BettingStrategy
from confidence import evaluate as eval_confidence
from volatility import compute_race_volatility
from database import init_db

MODEL_DIR = ROOT / "models"


def classify_race(name: str) -> str:
    if not name:
        return "unknown"
    if "未勝利" in name or "新馬" in name:
        return "未勝利・新馬"
    if "1勝" in name:
        return "1勝"
    if "2勝" in name:
        return "2勝"
    if "3勝" in name:
        return "3勝"
    return "OP/特別"


def downgrade(conf: str, n: int) -> str:
    grades = ["S", "A", "B", "C", "D"]
    if conf not in grades:
        return conf
    idx = grades.index(conf)
    return grades[min(len(grades) - 1, idx + n)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=2025)
    ap.add_argument("--target-class", default="1勝")
    ap.add_argument("--target-conf", default="A")
    args = ap.parse_args()

    print(f"📊 A 層 loss 分析: {args.target_class} × {args.target_conf} (year={args.year})")
    print("=" * 60)
    init_db()

    print("📥 全データロード中...")
    races_df, results_df, payouts_df = load_all_data()
    race_info = races_df.set_index("race_id")[
        ["race_date", "venue", "distance", "surface", "track_condition",
         "horse_count", "race_name", "grade"]
    ].to_dict("index")
    for col in ["race_date", "venue", "distance", "surface", "track_condition",
                "horse_count", "race_name", "grade"]:
        results_df[col] = results_df["race_id"].map(
            lambda rid, c=col: race_info.get(rid, {}).get(c, "")
        )

    horse_history = build_horse_history(results_df, races_df)
    jockey_stats, trainer_stats, combo_stats = build_jockey_trainer_stats(results_df, races_df)
    si_cache = build_speed_index_cache(results_df, races_df)

    test_race_ids = races_df[
        races_df["race_date"].str[:4].astype(str) == str(args.year)
    ]["race_id"].tolist()
    print(f"🎯 テスト対象: {len(test_race_ids)} レース ({args.year})")
    test_race_id_set = set(test_race_ids)

    results_grouped = {rid: g for rid, g in results_df.groupby("race_id") if rid in test_race_id_set}

    print(f"🔧 特徴量計算中... (test races={len(test_race_id_set)})")
    all_rows = []
    for race in races_df.itertuples(index=False):
        if race.race_id not in test_race_id_set:
            continue
        race_results = results_grouped.get(race.race_id)
        if race_results is None or race_results.empty:
            continue
        rows = compute_features_fast(
            race._asdict(), race_results.to_dict("records"),
            horse_history, jockey_stats, trainer_stats, combo_stats, si_cache
        )
        all_rows.extend(rows)

    df = pd.DataFrame(all_rows)
    df["race_date"] = df["race_id"].map(
        lambda rid: race_info.get(rid, {}).get("race_date", "")
    )
    feature_cols = get_feature_columns()

    with open(MODEL_DIR / "model_rank.pkl", "rb") as f:
        model_rank = pickle.load(f)
    with open(MODEL_DIR / "model_top3.pkl", "rb") as f:
        model_top3 = pickle.load(f)

    X_test = df[feature_cols].fillna(0)
    df["rank_score"] = model_rank.predict(X_test)
    df["pred_top3"] = model_top3.predict(X_test)

    strategy = BettingStrategy()

    # payouts lookup
    payouts_lookup = {}
    for r in payouts_df.itertuples():
        key = (r.race_id, r.bet_type, r.combination)
        payouts_lookup[key] = r.payout_amount

    # 集計 buckets
    target_races_data = []  # detailed records for target_class × target_conf

    # patterns
    miss_pattern = defaultdict(int)
    venue_stats = defaultdict(lambda: {"spend": 0, "return": 0, "races": 0})
    distance_band = defaultdict(lambda: {"spend": 0, "return": 0, "races": 0})
    surface_stats = defaultdict(lambda: {"spend": 0, "return": 0, "races": 0})
    odds_band_stats = defaultdict(lambda: {"spend": 0, "return": 0, "races": 0})
    win_pos_dist = defaultdict(int)

    for race_id, group in df.groupby("race_id"):
        ri = race_info.get(race_id, {})
        race_name = ri.get("race_name", "") or ""
        race_class = classify_race(race_name)
        if race_class != args.target_class:
            continue

        group = group.sort_values("rank_score", ascending=False).copy()
        rank_exp = np.exp(group["rank_score"] - group["rank_score"].max())
        group["pred_win"] = rank_exp / rank_exp.sum()

        predictions = []
        for _, row in group.iterrows():
            odds = row["odds"]
            if odds <= 0:
                continue
            predictions.append({
                "horse_number": int(row["horse_number"]),
                "pred_win": float(row["pred_win"]),
                "pred_top3": float(row["pred_top3"]),
                "odds_win": float(odds),
                "popularity": int(row["popularity"]) if row.get("popularity") else 0,
                "finish_position": int(row["finish_position"]) if row.get("finish_position") else 0,
            })
        if len(predictions) < 5:
            continue

        sorted_preds = sorted(predictions, key=lambda x: -x["pred_win"])
        top1 = sorted_preds[0]
        top2 = sorted_preds[1] if len(sorted_preds) >= 2 else top1

        c_result = eval_confidence(
            top_win_pct=top1["pred_win"] * 100,
            n_horses=len(predictions),
            top3_sum_pct=sum(p["pred_win"] * 100 for p in sorted_preds[:3]),
            grade=ri.get("grade"),
            second_win_pct=top2["pred_win"] * 100,
            top_top3_pct=top1["pred_top3"] * 100,
            top_popularity=top1.get("popularity") if top1.get("popularity") else None,
            top_odds=top1["odds_win"],
        )
        confidence = c_result["confidence"]
        vol = compute_race_volatility(ri)
        adj = vol.get("conf_adjust", 0)
        if adj < 0:
            confidence = downgrade(confidence, -adj)
        if confidence != args.target_conf:
            continue

        should_bet, _ = strategy.should_bet_race(predictions, confidence=confidence)
        if not should_bet:
            continue

        # 三連複◎軸 5頭流し
        top6 = [p["horse_number"] for p in sorted_preds[:6]]
        first = top6[0]
        first_pred = sorted_preds[0]
        win_h = next((p["horse_number"] for p in predictions if p["finish_position"] == 1), None)
        p2_h = next((p["horse_number"] for p in predictions if p["finish_position"] == 2), None)
        p3_h = next((p["horse_number"] for p in predictions if p["finish_position"] == 3), None)
        if not all([win_h, p2_h, p3_h]):
            continue
        top3_set = {win_h, p2_h, p3_h}

        # ◎ の着順
        first_finish = next((p["finish_position"] for p in predictions if p["horse_number"] == first), 99)
        win_pos_dist[first_finish if first_finish <= 18 else "圏外"] += 1

        # 賭けと return
        race_spend = 0
        race_return = 0
        for a, b in combinations(top6[1:6], 2):
            race_spend += 100
            if {first, a, b} == top3_set:
                key = (race_id, "三連複", "-".join(sorted(str(x) for x in [first, a, b])))
                race_return += payouts_lookup.get(key, 0)

        # miss パターン分類
        rest_in = len(top3_set & set(top6[1:6]))
        if first in top3_set:
            if rest_in >= 2:
                pat = "✅ HIT (◎+2)"
            elif rest_in == 1:
                pat = "◎+1 (相手不足)"
            else:
                pat = "◎のみ"
        else:
            pat = f"◎{first_finish}着 (◎着外)"
        miss_pattern[pat] += 1

        # venue/distance/surface
        venue = ri.get("venue", "?")
        dist = ri.get("distance", 0) or 0
        surf = ri.get("surface", "?")
        venue_stats[venue]["spend"] += race_spend
        venue_stats[venue]["return"] += race_return
        venue_stats[venue]["races"] += 1

        if dist < 1400: band = "短距離 (~1400m)"
        elif dist < 1800: band = "マイル (1400-1799)"
        elif dist < 2200: band = "中距離 (1800-2199)"
        else: band = "長距離 (2200+)"
        distance_band[band]["spend"] += race_spend
        distance_band[band]["return"] += race_return
        distance_band[band]["races"] += 1

        surface_stats[surf]["spend"] += race_spend
        surface_stats[surf]["return"] += race_return
        surface_stats[surf]["races"] += 1

        # odds band of ◎
        first_odds = first_pred["odds_win"]
        if first_odds < 2.5: ob = "1.x-2.4倍"
        elif first_odds < 4.0: ob = "2.5-3.9倍"
        elif first_odds < 6.0: ob = "4.0-5.9倍"
        elif first_odds < 10.0: ob = "6.0-9.9倍"
        else: ob = "10倍+"
        odds_band_stats[ob]["spend"] += race_spend
        odds_band_stats[ob]["return"] += race_return
        odds_band_stats[ob]["races"] += 1

        target_races_data.append({
            "race_id": race_id,
            "date": ri.get("race_date", ""),
            "venue": venue,
            "name": race_name,
            "dist": dist,
            "surf": surf,
            "first_horse": first,
            "first_odds": first_odds,
            "first_finish": first_finish,
            "actual_top3": sorted(top3_set),
            "spend": race_spend,
            "return": race_return,
            "pattern": pat,
        })

    if not target_races_data:
        print(f"⚠️ 該当データなし ({args.target_class} × {args.target_conf})")
        return

    total_spend = sum(r["spend"] for r in target_races_data)
    total_return = sum(r["return"] for r in target_races_data)
    total_races = len(target_races_data)

    print(f"\n{'='*60}")
    print(f"📊 {args.target_class} × {args.target_conf}: {total_races} races")
    print(f"  spend: ¥{total_spend:,}")
    print(f"  return: ¥{total_return:,}")
    print(f"  ROI: {total_return*100/total_spend:.1f}%")
    print(f"{'='*60}")

    print(f"\n── ◎ の着順分布 ──")
    for pos in sorted(win_pos_dist.keys(), key=lambda x: (isinstance(x, str), x)):
        n = win_pos_dist[pos]
        pct = n * 100 / total_races
        bar = "█" * int(pct / 2)
        print(f"  ◎{pos}着: {n:3} ({pct:>4.1f}%) {bar}")

    print(f"\n── miss/hit パターン ──")
    for pat, n in sorted(miss_pattern.items(), key=lambda x: -x[1]):
        pct = n * 100 / total_races
        print(f"  {pat:25} {n:3} ({pct:>4.1f}%)")

    def print_stats(title, stats):
        print(f"\n── {title} ──")
        rows = []
        for k, d in stats.items():
            if d["spend"] == 0: continue
            roi = d["return"] / d["spend"] * 100
            rows.append((k, d, roi))
        rows.sort(key=lambda x: -x[2])
        for k, d, roi in rows:
            print(f"  {k:20} races={d['races']:>4} spend=¥{d['spend']:>7,} return=¥{d['return']:>7,} ROI={roi:>5.1f}%")

    print_stats("venue 別", venue_stats)
    print_stats("distance 別", distance_band)
    print_stats("surface 別", surface_stats)
    print_stats("◎ odds 帯別", odds_band_stats)


if __name__ == "__main__":
    main()
