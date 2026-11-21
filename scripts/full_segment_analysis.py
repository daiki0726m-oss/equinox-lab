#!/usr/bin/env python3
"""全軸 segment ROI 分析 — 100% 超えるための niche 発掘

各 race を 8 軸で分類し、すべての軸ごと + 重要な 2軸 cross の ROI を計算。
n ≥ 20 で ROI ≥ 100% の segment を抽出。

軸:
  1. race_class (未勝利/1勝/2勝/3勝/OP/特別/G1-G3)
  2. venue (10場)
  3. surface (芝/ダート)
  4. distance band (短/マイル/中/長)
  5. confidence (S/A/B/C/D, volatility 補正後)
  6. ML range (<2% / 2-5% / 5-10% / 10%+)
  7. ◎ popularity (1 / 2-3 / 4-7 / 8+)
  8. ◎ odds band

券種: 三連複◎軸 5頭流し (10点)
    + 馬連流し (5点) も並行集計

Usage:
    python3 scripts/full_segment_analysis.py [--year 2025]
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
from confidence import evaluate as eval_confidence
from volatility import compute_race_volatility
from database import init_db

MODEL_DIR = ROOT / "models"


def classify_race(name):
    if not name:
        return "unknown"
    if "未勝利" in name or "新馬" in name:
        return "未勝利"
    if "1勝" in name:
        return "1勝"
    if "2勝" in name:
        return "2勝"
    if "3勝" in name:
        return "3勝"
    return "OP/特別"


def downgrade(conf, n):
    grades = ["S", "A", "B", "C", "D"]
    if conf not in grades: return conf
    idx = grades.index(conf)
    return grades[min(len(grades) - 1, idx + n)]


def distance_band(d):
    if not d: return "?"
    if d < 1400: return "短"
    if d < 1800: return "M"
    if d < 2200: return "中"
    return "長"


def ml_range_band(r):
    if r < 0.02: return "<2%"
    if r < 0.05: return "2-5%"
    if r < 0.10: return "5-10%"
    return "10%+"


def pop_band(p):
    if p == 1: return "1人気"
    if p <= 3: return "2-3人気"
    if p <= 7: return "4-7人気"
    return "8+人気"


def odds_band(o):
    if o < 2.5: return "1.x-2.4"
    if o < 4.0: return "2.5-3.9"
    if o < 6.0: return "4-5.9"
    if o < 10.0: return "6-9.9"
    return "10+"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=2025)
    args = ap.parse_args()

    print(f"📊 全軸 segment 分析 (year={args.year})")
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
    print(f"🎯 テスト対象: {len(test_race_ids)} レース")
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

    # payouts lookup
    payouts_lookup = {}
    for r in payouts_df.itertuples():
        key = (r.race_id, r.bet_type, r.combination)
        payouts_lookup[key] = r.payout_amount

    # 各 race の segment + 結果を保存
    records = []

    for race_id, group in df.groupby("race_id"):
        ri = race_info.get(race_id, {})
        race_name = ri.get("race_name", "") or ""
        race_class = classify_race(race_name)

        group = group.sort_values("rank_score", ascending=False).copy()
        rank_exp = np.exp(group["rank_score"] - group["rank_score"].max())
        group["pred_win"] = rank_exp / rank_exp.sum()

        predictions = []
        for _, row in group.iterrows():
            odds = row["odds"]
            if odds <= 0: continue
            predictions.append({
                "horse_number": int(row["horse_number"]),
                "pred_win": float(row["pred_win"]),
                "pred_top3": float(row["pred_top3"]),
                "odds_win": float(odds),
                "popularity": int(row["popularity"]) if row.get("popularity") else 0,
                "finish_position": int(row["finish_position"]) if row.get("finish_position") else 0,
            })
        if len(predictions) < 5: continue

        sorted_preds = sorted(predictions, key=lambda x: -x["pred_win"])
        top1 = sorted_preds[0]
        top2 = sorted_preds[1] if len(sorted_preds) >= 2 else top1

        # confidence
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
        if vol.get("conf_adjust", 0) < 0:
            confidence = downgrade(confidence, -vol["conf_adjust"])

        # 結果
        win_h = next((p["horse_number"] for p in predictions if p["finish_position"] == 1), None)
        p2_h = next((p["horse_number"] for p in predictions if p["finish_position"] == 2), None)
        p3_h = next((p["horse_number"] for p in predictions if p["finish_position"] == 3), None)
        if not all([win_h, p2_h, p3_h]): continue
        top3_set = {win_h, p2_h, p3_h}

        # ML range
        ml_r = sorted_preds[0]["pred_win"] - sorted_preds[-1]["pred_win"]

        # 三連複◎軸 5頭流し
        top6 = [p["horse_number"] for p in sorted_preds[:6]]
        first = top6[0]
        first_pred = sorted_preds[0]

        spend_trio = 0
        return_trio = 0
        for a, b in combinations(top6[1:6], 2):
            spend_trio += 100
            if {first, a, b} == top3_set:
                key = (race_id, "三連複", "-".join(sorted(str(x) for x in [first, a, b])))
                return_trio += payouts_lookup.get(key, 0)

        # 馬連 5頭流し
        spend_uma = 0
        return_uma = 0
        for o in top6[1:6]:
            spend_uma += 100
            if {first, o} == {win_h, p2_h}:
                key = (race_id, "馬連", "-".join(sorted(str(x) for x in [first, o])))
                return_uma += payouts_lookup.get(key, 0)

        # 単勝 (◎)
        spend_tan = 100
        return_tan = 0
        if first == win_h:
            key = (race_id, "単勝", str(first))
            return_tan = payouts_lookup.get(key, 0)

        # 複勝 (◎)
        spend_fuku = 100
        return_fuku = 0
        if first in top3_set:
            key = (race_id, "複勝", str(first))
            return_fuku = payouts_lookup.get(key, 0)

        records.append({
            "race_id": race_id,
            "race_class": race_class,
            "venue": ri.get("venue", "?"),
            "surface": ri.get("surface", "?"),
            "dist_band": distance_band(ri.get("distance", 0)),
            "confidence": confidence,
            "ml_range_band": ml_range_band(ml_r),
            "pop_band": pop_band(first_pred.get("popularity", 0)),
            "odds_band": odds_band(first_pred["odds_win"]),
            "first_finish": next((p["finish_position"] for p in predictions if p["horse_number"] == first), 99),
            "spend_trio": spend_trio,
            "return_trio": return_trio,
            "spend_uma": spend_uma,
            "return_uma": return_uma,
            "spend_tan": spend_tan,
            "return_tan": return_tan,
            "spend_fuku": spend_fuku,
            "return_fuku": return_fuku,
        })

    print(f"\n📋 records 数: {len(records)}")

    # 各軸単独の ROI
    def agg(records, key_fn, bet="trio"):
        d = defaultdict(lambda: {"spend": 0, "return": 0, "n": 0})
        for r in records:
            k = key_fn(r)
            d[k]["spend"] += r[f"spend_{bet}"]
            d[k]["return"] += r[f"return_{bet}"]
            d[k]["n"] += 1
        rows = []
        for k, v in d.items():
            if v["spend"] == 0: continue
            roi = v["return"] / v["spend"] * 100
            rows.append((k, v["n"], v["spend"], v["return"], roi))
        rows.sort(key=lambda x: -x[4])
        return rows

    def show(title, rows, min_n=10):
        print(f"\n── {title} ──")
        print(f"  {'key':30} {'n':>4} {'spend':>9} {'return':>9} {'ROI':>7}")
        for k, n, s, ret, roi in rows:
            if n < min_n: continue
            flag = " 🟢" if roi >= 100 else (" 🟡" if roi >= 75 else "")
            print(f"  {str(k):30} {n:>4} {s:>9,} {int(ret):>9,} {roi:>6.1f}%{flag}")

    # 各券種で全 race 集計
    print(f"\n{'='*60}")
    print(f"📊 券種別 全体 ROI ({len(records)} races, year={args.year})")
    print(f"{'='*60}")
    for bet, label in [("trio", "三連複◎軸(10点)"), ("uma", "馬連流し(5点)"), ("tan", "単勝◎"), ("fuku", "複勝◎")]:
        spend = sum(r[f"spend_{bet}"] for r in records)
        ret = sum(r[f"return_{bet}"] for r in records)
        if spend == 0: continue
        roi = ret / spend * 100
        print(f"  {label:18}: spend ¥{spend:>10,} return ¥{int(ret):>10,} ROI {roi:>5.1f}%")

    # 各軸単独 (三連複)
    print(f"\n{'='*60}")
    print(f"📊 三連複◎軸(10点) — 各軸単独 ROI")
    print(f"{'='*60}")
    for label, key in [
        ("race_class", lambda r: r["race_class"]),
        ("venue", lambda r: r["venue"]),
        ("surface", lambda r: r["surface"]),
        ("dist_band", lambda r: r["dist_band"]),
        ("confidence", lambda r: r["confidence"]),
        ("ml_range_band", lambda r: r["ml_range_band"]),
        ("pop_band (◎)", lambda r: r["pop_band"]),
        ("odds_band (◎)", lambda r: r["odds_band"]),
    ]:
        show(label, agg(records, key, "trio"), min_n=10)

    # 単勝も同様 (controlled bet なので analytic 価値高い)
    print(f"\n{'='*60}")
    print(f"📊 単勝◎ — 各軸単独 ROI")
    print(f"{'='*60}")
    for label, key in [
        ("pop_band (◎)", lambda r: r["pop_band"]),
        ("odds_band (◎)", lambda r: r["odds_band"]),
        ("confidence", lambda r: r["confidence"]),
        ("race_class", lambda r: r["race_class"]),
    ]:
        show(f"単勝 × {label}", agg(records, key, "tan"), min_n=20)

    # 重要 2軸 cross — confidence × race_class (triple)
    print(f"\n{'='*60}")
    print(f"📊 三連複◎軸 — 2軸 cross (高 ROI segment 抽出)")
    print(f"{'='*60}")
    cross_pairs = [
        (("confidence", "race_class"), lambda r: (r["confidence"], r["race_class"])),
        (("confidence", "ml_range_band"), lambda r: (r["confidence"], r["ml_range_band"])),
        (("confidence", "pop_band"), lambda r: (r["confidence"], r["pop_band"])),
        (("confidence", "odds_band"), lambda r: (r["confidence"], r["odds_band"])),
        (("race_class", "surface"), lambda r: (r["race_class"], r["surface"])),
        (("race_class", "pop_band"), lambda r: (r["race_class"], r["pop_band"])),
        (("pop_band", "odds_band"), lambda r: (r["pop_band"], r["odds_band"])),
        (("ml_range_band", "race_class"), lambda r: (r["ml_range_band"], r["race_class"])),
    ]
    for (a, b), key in cross_pairs:
        show(f"{a} × {b}", agg(records, key, "trio"), min_n=20)

    # 🏆 100%超 segment 抽出 (n ≥ 20、全 cross 探索)
    print(f"\n{'='*60}")
    print(f"🏆 ROI > 100% segment (n ≥ 20)")
    print(f"{'='*60}")
    profitable = []
    # 3軸 cross も探索
    triple_keys = [
        ("confidence", "race_class", "surface", lambda r: (r["confidence"], r["race_class"], r["surface"])),
        ("confidence", "race_class", "pop_band", lambda r: (r["confidence"], r["race_class"], r["pop_band"])),
        ("confidence", "race_class", "dist_band", lambda r: (r["confidence"], r["race_class"], r["dist_band"])),
        ("confidence", "pop_band", "surface", lambda r: (r["confidence"], r["pop_band"], r["surface"])),
        ("race_class", "venue", "surface", lambda r: (r["race_class"], r["venue"], r["surface"])),
    ]
    for bet_type, label in [("trio", "三連複"), ("uma", "馬連"), ("tan", "単勝"), ("fuku", "複勝")]:
        for axes in triple_keys:
            keys, key_fn = axes[:-1], axes[-1]
            rows = agg(records, key_fn, bet_type)
            for k, n, s, ret, roi in rows:
                if n >= 20 and roi >= 100:
                    profitable.append((bet_type, label, list(keys), k, n, s, ret, roi))

    profitable.sort(key=lambda x: -x[7])
    print(f"  {'券種':>4} {'axes':50} {'n':>4} {'spend':>8} {'return':>8} {'ROI':>7}")
    for bt, lab, keys, k, n, s, ret, roi in profitable[:30]:
        ax_str = "/".join(keys)
        k_str = "/".join(str(x) for x in k) if isinstance(k, tuple) else str(k)
        print(f"  {lab:>4} {ax_str+'='+k_str:50} {n:>4} {s:>8,} {int(ret):>8,} {roi:>6.1f}%")

    if not profitable:
        print("  (該当なし — 全 segment が 100% 未満)")

    print(f"\n計算時間: {time.time():.0f}s")


if __name__ == "__main__":
    main()
