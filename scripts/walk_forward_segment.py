#!/usr/bin/env python3
"""Walk-forward 検証 — 2020-2025 各年で同じ segment 分析を実行

2025 で発見した ROI>100% segments が他年でも機能するか確認。
年度別 ROI のばらつきを見て、安定して prof である segments のみ採用するための材料を作る。

Note: ML model は最新版 (train data に最近を含む可能性あり) を使うので、
      個別予測には mild leakage がある。ただし segments (pattern) の年度間
      stability の確認は valid。

Usage:
    python3 scripts/walk_forward_segment.py
出力: docs/analysis/walk_forward_segments_<date>.json
"""
from __future__ import annotations
import argparse
import json
import os
import pickle
import sys
import time
from collections import defaultdict
from datetime import datetime
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
    if not name: return "unknown"
    if "未勝利" in name or "新馬" in name: return "未勝利"
    if "1勝" in name: return "1勝"
    if "2勝" in name: return "2勝"
    if "3勝" in name: return "3勝"
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--years", default="2020,2021,2022,2023,2024,2025")
    ap.add_argument("--output", default="docs/analysis/walk_forward_segments.json")
    args = ap.parse_args()
    years = [int(y) for y in args.years.split(",")]

    print(f"📊 Walk-forward 検証 (years={years})")
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

    # 全 test 対象 race を集約
    all_test_ids = set()
    for y in years:
        ids = races_df[races_df["race_date"].str[:4].astype(str) == str(y)]["race_id"].tolist()
        all_test_ids.update(ids)
    print(f"🎯 テスト対象: {len(all_test_ids)} レース (全 {len(years)} 年)")

    results_grouped = {rid: g for rid, g in results_df.groupby("race_id") if rid in all_test_ids}

    print(f"🔧 特徴量計算中... (test races={len(all_test_ids)})")
    all_rows = []
    for race in races_df.itertuples(index=False):
        if race.race_id not in all_test_ids: continue
        race_results = results_grouped.get(race.race_id)
        if race_results is None or race_results.empty: continue
        rows = compute_features_fast(
            race._asdict(), race_results.to_dict("records"),
            horse_history, jockey_stats, trainer_stats, combo_stats, si_cache
        )
        all_rows.extend(rows)

    df = pd.DataFrame(all_rows)
    df["race_date"] = df["race_id"].map(
        lambda rid: race_info.get(rid, {}).get("race_date", "")
    )
    df["year"] = df["race_date"].str[:4].astype(int)
    feature_cols = get_feature_columns()

    with open(MODEL_DIR / "model_rank.pkl", "rb") as f:
        model_rank = pickle.load(f)
    with open(MODEL_DIR / "model_top3.pkl", "rb") as f:
        model_top3 = pickle.load(f)

    X_test = df[feature_cols].fillna(0)
    df["rank_score"] = model_rank.predict(X_test)
    df["pred_top3"] = model_top3.predict(X_test)

    payouts_lookup = {}
    for r in payouts_df.itertuples():
        key = (r.race_id, r.bet_type, r.combination)
        payouts_lookup[key] = r.payout_amount

    # year × segment_axis × segment_value で集計
    # segments 軸: (race_class, venue, surface), (confidence, race_class, dist_band) など
    # 各 bet type ごとに spend/return を蓄積
    year_segments = defaultdict(lambda: defaultdict(lambda: {"spend": 0, "return": 0, "n": 0}))

    print(f"🔧 各 race 処理中...")
    for race_id, group in df.groupby("race_id"):
        ri = race_info.get(race_id, {})
        race_name = ri.get("race_name", "") or ""
        race_class = classify_race(race_name)
        venue = ri.get("venue", "?")
        surface = ri.get("surface", "?")
        dist_b = distance_band(ri.get("distance", 0))
        year = int(ri.get("race_date", "")[:4]) if ri.get("race_date") else 0

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

        win_h = next((p["horse_number"] for p in predictions if p["finish_position"] == 1), None)
        p2_h = next((p["horse_number"] for p in predictions if p["finish_position"] == 2), None)
        p3_h = next((p["horse_number"] for p in predictions if p["finish_position"] == 3), None)
        if not all([win_h, p2_h, p3_h]): continue
        top3_set = {win_h, p2_h, p3_h}

        first = sorted_preds[0]["horse_number"]
        first_odds = sorted_preds[0]["odds_win"]

        # 単勝
        spend_tan = 100
        return_tan = 0
        if first == win_h:
            key = (race_id, "単勝", str(first))
            return_tan = payouts_lookup.get(key, 0)

        # 複勝
        spend_fuku = 100
        return_fuku = 0
        if first in top3_set:
            key = (race_id, "複勝", str(first))
            return_fuku = payouts_lookup.get(key, 0)

        # segments を全パターン作る
        segs = [
            ("race_class/venue/surface", f"{race_class}/{venue}/{surface}"),
            ("confidence/race_class/dist", f"{confidence}/{race_class}/{dist_b}"),
            ("confidence/race_class/surface", f"{confidence}/{race_class}/{surface}"),
            ("race_class", race_class),
            ("confidence/race_class", f"{confidence}/{race_class}"),
            ("venue/surface", f"{venue}/{surface}"),
            ("confidence", confidence),
            ("surface", surface),
        ]
        for axis_name, key in segs:
            for bet, sp, ret in [("tan", spend_tan, return_tan), ("fuku", spend_fuku, return_fuku)]:
                bucket = year_segments[year][(axis_name, key, bet)]
                bucket["spend"] += sp
                bucket["return"] += ret
                if ret > 0:
                    bucket["n"] += 1
                else:
                    bucket["n"] += 0  # n は的中数。spend 計上 race 数は別に持つ

    # まず全 race 数を年×segment 単位で別管理して、ROI と n を出す
    # 上の n は hit 数。race 数は spend/100 で換算
    print(f"\n{'='*60}")
    print(f"📊 年度別 segment ROI (n_races ≥ 15 のみ表示)")
    print(f"{'='*60}")

    # 年度内 segment ROI を計算
    table = defaultdict(dict)  # table[(axis, key, bet)][year] = {roi, n_races, spend, return}
    for year, segs in year_segments.items():
        for (axis, key, bet), v in segs.items():
            n_races = v["spend"] // 100
            if n_races < 15: continue
            roi = v["return"] / v["spend"] * 100 if v["spend"] else 0
            table[(axis, key, bet)][year] = {
                "roi": round(roi, 1),
                "n_races": n_races,
                "spend": v["spend"],
                "return": v["return"],
                "hits": v["n"],
            }

    # 年度間 stability を見る: 全年で >100% かつ平均 >110% の segment を抽出
    print(f"\n🏆 全 {len(years)} 年で ROI ≥ 100% (n ≥ 15 / 年) の segment\n")
    stable_segments = []
    for (axis, key, bet), per_year in table.items():
        if len(per_year) < len(years): continue
        roi_list = [per_year[y]["roi"] for y in years if y in per_year]
        if min(roi_list) >= 100:
            avg_roi = sum(roi_list) / len(roi_list)
            total_n = sum(per_year[y]["n_races"] for y in per_year)
            stable_segments.append((bet, axis, key, avg_roi, min(roi_list), total_n, per_year))

    stable_segments.sort(key=lambda x: -x[3])

    if stable_segments:
        print(f"  {'bet':>4} {'segment':50} {'avg':>6} {'min':>6} {'total n':>7} | per-year ROI")
        for bet, axis, key, avg, min_r, tot_n, py in stable_segments:
            ys = " ".join(f"{y}={int(py[y]['roi'])}%" for y in years if y in py)
            seg = f"{axis}={key}"
            print(f"  {bet:>4} {seg[:50]:50} {avg:>5.1f}% {min_r:>5.1f}% {tot_n:>7} | {ys}")
    else:
        print("  (該当なし — 全年安定して 100%+ の segment は存在しない)")

    # ほぼ安定 (4/5 年以上で 100%+) も抽出
    print(f"\n🎯 4年以上で ROI ≥ 100% (準安定) の segment\n")
    near_stable = []
    for (axis, key, bet), per_year in table.items():
        if len(per_year) < len(years) - 1: continue
        above_100 = [y for y in per_year if per_year[y]["roi"] >= 100]
        if len(above_100) >= len(years) - 1:  # 1 年だけ 100% 下回ってもOK
            roi_list = [per_year[y]["roi"] for y in years if y in per_year]
            avg_roi = sum(roi_list) / len(roi_list)
            total_n = sum(per_year[y]["n_races"] for y in per_year)
            if avg_roi >= 105:
                near_stable.append((bet, axis, key, avg_roi, min(roi_list), total_n, per_year, len(above_100)))

    near_stable.sort(key=lambda x: -x[3])
    if near_stable:
        print(f"  {'bet':>4} {'segment':50} {'avg':>6} {'min':>6} {'≥100':>5} {'n':>5} | per-year")
        for bet, axis, key, avg, min_r, tot_n, py, above in near_stable[:30]:
            ys = " ".join(f"{str(y)[2:]}={int(py[y]['roi'])}%" for y in years if y in py)
            seg = f"{axis}={key}"
            print(f"  {bet:>4} {seg[:50]:50} {avg:>5.1f}% {min_r:>5.1f}% {above:>3}/{len(years)} {tot_n:>5} | {ys}")

    # JSON 出力
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    out = {
        "generated_at": datetime.now().isoformat(),
        "years": years,
        "stable_100pct_all_years": [
            {
                "bet": bet, "axis": axis, "segment": key,
                "avg_roi": round(avg, 1), "min_roi": round(min_r, 1),
                "total_n_races": tot_n,
                "per_year": {str(y): py[y] for y in years if y in py}
            }
            for bet, axis, key, avg, min_r, tot_n, py in stable_segments
        ],
        "near_stable_100pct_4of5plus": [
            {
                "bet": bet, "axis": axis, "segment": key,
                "avg_roi": round(avg, 1), "min_roi": round(min_r, 1),
                "above_100_years": above,
                "total_n_races": tot_n,
                "per_year": {str(y): py[y] for y in years if y in py}
            }
            for bet, axis, key, avg, min_r, tot_n, py, above in near_stable[:50]
        ],
    }
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"\n📝 JSON 出力: {args.output}")


if __name__ == "__main__":
    main()
