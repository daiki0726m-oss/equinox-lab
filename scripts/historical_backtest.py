#!/usr/bin/env python3
"""Historical re-predict バックテスト (2020-2025)

backtest_strategy.py の拡張版。下記の W2 改修を反映:
  - v4 confidence (confidence.evaluate)
  - race_class -2 補正 (volatility.compute_race_volatility)
  - W1 should_bet_race(confidence) — C/D 遮断 + odds band
  - W2 CONFIDENCE_MULTIPLIER (S=2x / A=1.5x / B=1x)
  - 三連複◎軸 5頭流し (10点)

出力: year × race_class × confidence × 三連複ROI のクロス集計 + 全体ROI

Note: ML model は最新 model_*.pkl を使用 (temporal leakage 軽微あり)。
     目的は ML 評価ではなく「strategy/confidence ロジックの有効性検証」なので、
     相対 ROI 比較として用いる前提。

Usage:
    python3 scripts/historical_backtest.py [--year-from 2020] [--year-to 2025]
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

# repo root を sys.path に
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
    if any(g in name for g in ["G1", "G2", "G3"]):
        return "G1-G3"
    return "OP/特別"


def downgrade(conf: str, n: int) -> str:
    grades = ["S", "A", "B", "C", "D"]
    if conf not in grades:
        return conf
    idx = grades.index(conf)
    return grades[min(len(grades) - 1, idx + n)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--year-from", type=int, default=2020)
    ap.add_argument("--year-to", type=int, default=2025)
    args = ap.parse_args()

    print(f"📊 Historical Re-Predict Backtest {args.year_from}-{args.year_to}")
    print("=" * 60)
    t0 = time.time()
    init_db()

    # ── データロード ──
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

    # 🚀 高速化: テスト対象期間 (year_from-year_to) の race のみ feature 計算
    test_race_ids = races_df[
        (races_df["race_date"].str[:4].astype(str) >= str(args.year_from))
        & (races_df["race_date"].str[:4].astype(str) <= str(args.year_to))
    ]["race_id"].tolist()
    print(f"🎯 テスト対象: {len(test_race_ids)} レース ({args.year_from}-{args.year_to})")
    test_race_id_set = set(test_race_ids)

    # results_df を race_id でインデックス化 (groupby 1 回で O(n) lookup)
    results_grouped = {rid: g for rid, g in results_df.groupby("race_id") if rid in test_race_id_set}

    print(f"🔧 特徴量計算中... (test races={len(test_race_id_set)})")
    all_rows = []
    progress_step = max(1, len(test_race_id_set) // 20)
    for n, race in enumerate(races_df.itertuples(index=False)):
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
        if (n + 1) % progress_step == 0:
            print(f"  ... {n+1} processed, {len(all_rows)} feature rows", flush=True)

    df = pd.DataFrame(all_rows)
    df["race_date"] = df["race_id"].map(
        lambda rid: race_info.get(rid, {}).get("race_date", "")
    )
    feature_cols = get_feature_columns()

    # ── モデル読み込み ──
    with open(MODEL_DIR / "model_rank.pkl", "rb") as f:
        model_rank = pickle.load(f)
    with open(MODEL_DIR / "model_top3.pkl", "rb") as f:
        model_top3 = pickle.load(f)
    with open(MODEL_DIR / "model_win.pkl", "rb") as f:
        model_win = pickle.load(f)

    # ── 対象期間のデータ ──
    year_filter = df["race_date"].str[:4].astype(str)
    test = df[
        (df["finish_position"] > 0)
        & (year_filter >= str(args.year_from))
        & (year_filter <= str(args.year_to))
    ].copy()
    print(f"📂 対象データ: {len(test)} 行 ({test['race_id'].nunique()} レース)")

    X_test = test[feature_cols].fillna(0)
    test["rank_score"] = model_rank.predict(X_test)
    test["pred_win_raw"] = model_win.predict(X_test)
    test["pred_top3"] = model_top3.predict(X_test)

    strategy = BettingStrategy()

    # 集計
    bucket = defaultdict(lambda: defaultdict(lambda: {"spend": 0, "return": 0, "hits": 0, "races": 0}))
    grand_total = defaultdict(lambda: {"spend": 0, "return": 0, "hits": 0, "races": 0})
    counts_by_conf = defaultdict(int)
    counts_by_class = defaultdict(int)

    payouts_lookup = {}
    for r in payouts_df.itertuples():
        key = (r.race_id, r.bet_type, r.combination)
        payouts_lookup[key] = r.payout_amount

    races_processed = 0
    races_bet = 0
    races_skipped = 0

    for race_id, group in test.groupby("race_id"):
        ri = race_info.get(race_id, {})
        race_name = ri.get("race_name", "") or ""
        race_class = classify_race(race_name)

        group = group.sort_values("rank_score", ascending=False).copy()
        # softmax 正規化
        rank_exp = np.exp(group["rank_score"] - group["rank_score"].max())
        group["pred_win"] = rank_exp / rank_exp.sum()

        # predictions list
        predictions = []
        for _, row in group.iterrows():
            odds = row["odds"]
            if odds <= 0:
                continue
            predictions.append({
                "horse_number": int(row["horse_number"]),
                "horse_name": "",
                "pred_win": float(row["pred_win"]),
                "pred_top3": float(row["pred_top3"]),
                "odds_win": float(odds),
                "odds_place": max(float(odds) * 0.3, 1.1),
                "popularity": int(row["popularity"]) if row.get("popularity") else 0,
            })
        if len(predictions) < 5:
            races_skipped += 1
            continue

        # confidence 計算
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

        # race_class 補正 (volatility 経由)
        vol = compute_race_volatility(ri)
        adj = vol.get("conf_adjust", 0)
        confidence = downgrade(confidence, -adj if adj < 0 else 0)

        counts_by_conf[confidence] += 1
        counts_by_class[race_class] += 1

        # should_bet 判定
        should_bet, _ = strategy.should_bet_race(predictions, confidence=confidence)
        races_processed += 1
        if not should_bet:
            races_skipped += 1
            continue
        races_bet += 1

        # 三連複◎軸 5頭流し (10点)
        top6 = [p["horse_number"] for p in sorted_preds[:6]]
        win_h = next((int(r.horse_number) for r in group.itertuples() if r.finish_position == 1), None)
        p2_h = next((int(r.horse_number) for r in group.itertuples() if r.finish_position == 2), None)
        p3_h = next((int(r.horse_number) for r in group.itertuples() if r.finish_position == 3), None)
        if not all([win_h, p2_h, p3_h]):
            continue
        top3_set = {win_h, p2_h, p3_h}

        # bet 額 multiplier (W2 confidence-aware)
        mult = strategy.CONFIDENCE_MULTIPLIER.get(confidence, 1.0)
        line_amount = max(100, int(100 * mult / 100) * 100)  # 100 or 200

        first = top6[0]
        rest = top6[1:6]
        for a, b in combinations(rest, 2):
            spend = line_amount
            ret = 0
            if {first, a, b} == top3_set:
                combo_key = (race_id, "三連複", "-".join(sorted(str(x) for x in [first, a, b])))
                ret = payouts_lookup.get(combo_key, 0)
                # 馬券は 100円単位、return も比例
                ret = ret * (spend / 100)

            # 集計
            bucket[race_class][confidence]["spend"] += spend
            bucket[race_class][confidence]["return"] += ret
            grand_total[confidence]["spend"] += spend
            grand_total[confidence]["return"] += ret
            if ret > 0:
                bucket[race_class][confidence]["hits"] += 1
                grand_total[confidence]["hits"] += 1

        bucket[race_class][confidence]["races"] += 1
        grand_total[confidence]["races"] += 1

    elapsed = time.time() - t0

    print(f"\n{'='*60}")
    print(f"📊 結果 ({args.year_from}-{args.year_to})")
    print(f"{'='*60}")
    print(f"  対象レース: {races_processed}")
    print(f"  投資レース: {races_bet}")
    print(f"  スキップ:   {races_skipped} ({races_skipped/(races_processed+races_skipped)*100:.1f}%)")
    print(f"  計算時間:   {elapsed:.1f}秒")

    print(f"\n── confidence 分布 (compute_race_volatility 補正後) ──")
    for c in ["S", "A", "B", "C", "D"]:
        print(f"  {c}: {counts_by_conf[c]}")

    print(f"\n── race_class 分布 ──")
    for cls in ["G1-G3", "OP/特別", "3勝", "2勝", "1勝", "未勝利・新馬"]:
        print(f"  {cls:12}: {counts_by_class[cls]}")

    print(f"\n── 全体 (三連複◎軸 5頭流し) ──")
    print(f"{'conf':>4} | {'races':>6} | {'spend':>9} | {'return':>9} | {'hits':>5} | {'ROI':>6}")
    print("-" * 60)
    sum_spend, sum_return = 0, 0
    for c in ["S", "A", "B", "C", "D"]:
        d = grand_total[c]
        if d["spend"] == 0: continue
        roi = d["return"] / d["spend"] * 100
        print(f"{c:>4} | {d['races']:>6} | {d['spend']:>9,} | {int(d['return']):>9,} | {d['hits']:>5} | {roi:>5.1f}%")
        sum_spend += d["spend"]
        sum_return += d["return"]
    if sum_spend > 0:
        print("-" * 60)
        print(f"{'計':>4} |       | {sum_spend:>9,} | {int(sum_return):>9,} |       | {sum_return/sum_spend*100:>5.1f}%")

    print(f"\n── race_class × confidence × 三連複ROI ──")
    print(f"{'class':12} | {'conf':4} | {'races':>5} | {'spend':>7} | {'return':>7} | {'ROI':>6}")
    print("-" * 70)
    for cls in ["G1-G3", "OP/特別", "3勝", "2勝", "1勝", "未勝利・新馬"]:
        for c in ["S", "A", "B", "C", "D"]:
            d = bucket[cls][c]
            if d["spend"] == 0: continue
            roi = d["return"] / d["spend"] * 100
            print(f"{cls:12} | {c:4} | {d['races']:>5} | {d['spend']:>7,} | {int(d['return']):>7,} | {roi:>5.1f}%")


if __name__ == "__main__":
    main()
