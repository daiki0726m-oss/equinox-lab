#!/usr/bin/env python3
"""全 5 トラック並列分析 — 100% 超え路線の即効性検証

データロード 1 回で以下を一気に分析:
  Track 1: 馬場バイアス (同日 R1-R8 → R9-R12 効果)
  Track 4: 券種別 ROI (三連単フォーメーション、ワイド box、馬連 etc)
  Track 5: 1人気過剰人気時の 2-3 人気が割得か

Track 2 (追切): 別スクリプト (collect_workout.py)
Track 3 (Elo): 別スクリプト (elo_rating.py)

Usage:
    python3 scripts/all_tracks_analysis.py --year 2025
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
from itertools import combinations, permutations
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=2025)
    ap.add_argument("--output", default="docs/analysis/all_tracks.json")
    args = ap.parse_args()

    print(f"📊 全 トラック分析 (year={args.year})")
    print("=" * 60)
    init_db()

    # ── データロード ──
    print("📥 全データロード中...")
    races_df, results_df, payouts_df = load_all_data()
    race_info = races_df.set_index("race_id")[
        ["race_date", "venue", "distance", "surface", "track_condition",
         "horse_count", "race_name", "grade", "race_number"]
    ].to_dict("index")
    for col in ["race_date", "venue", "distance", "surface", "track_condition",
                "horse_count", "race_name", "grade", "race_number"]:
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

    print(f"🔧 特徴量計算中...")
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

    payouts_lookup = {}
    for r in payouts_df.itertuples():
        key = (r.race_id, r.bet_type, r.combination)
        payouts_lookup[key] = r.payout_amount

    # 各 race のデータをまとめる
    race_data = []  # (race_id, ri, predictions_sorted, top3_set, win_h, p2_h, p3_h)
    for race_id, group in df.groupby("race_id"):
        ri = race_info.get(race_id, {})
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
        win_h = next((p["horse_number"] for p in predictions if p["finish_position"] == 1), None)
        p2_h = next((p["horse_number"] for p in predictions if p["finish_position"] == 2), None)
        p3_h = next((p["horse_number"] for p in predictions if p["finish_position"] == 3), None)
        if not all([win_h, p2_h, p3_h]): continue
        race_data.append({
            "race_id": race_id,
            "ri": ri,
            "sorted_preds": sorted_preds,
            "all_preds": predictions,
            "top3_set": {win_h, p2_h, p3_h},
            "win_h": win_h,
            "p2_h": p2_h,
            "p3_h": p3_h,
        })

    print(f"📊 有効 race: {len(race_data)}")
    output = {"year": args.year, "n_races": len(race_data)}

    # ─────────────────────────────────────────────────────
    # Track 5: 市場非効率 — 1人気過剰人気時の 2-3 人気 ROI
    # ─────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"🎯 Track 5: 市場非効率検証 (1人気過剰人気)")
    print(f"{'='*60}")

    # 1人気の人気 odds を抽出
    bands = [
        ("1人気<1.5倍", lambda o: o < 1.5),
        ("1人気1.5-1.9倍", lambda o: 1.5 <= o < 2.0),
        ("1人気2.0-2.9倍", lambda o: 2.0 <= o < 3.0),
        ("1人気3.0-4.9倍", lambda o: 3.0 <= o < 5.0),
        ("1人気5.0倍+", lambda o: 5.0 <= o),
    ]
    pop_strats = ["1人気単勝", "2人気単勝", "3人気単勝", "1人気複勝", "2人気複勝", "3人気複勝"]

    track5_results = {}
    for band_name, band_fn in bands:
        stats = defaultdict(lambda: {"spend": 0, "return": 0, "n": 0})
        for rd in race_data:
            # 人気順に並べた pred (sorted by popularity)
            by_pop = sorted([p for p in rd["all_preds"] if p["popularity"] > 0], key=lambda x: x["popularity"])
            if len(by_pop) < 3: continue
            fav = by_pop[0]
            if not band_fn(fav["odds_win"]): continue

            for strat in pop_strats:
                pop_n = int(strat[0])  # 1, 2, 3
                target = by_pop[pop_n - 1]
                if "単勝" in strat:
                    spend, ret = 100, 0
                    if target["horse_number"] == rd["win_h"]:
                        key = (rd["race_id"], "単勝", str(target["horse_number"]))
                        ret = payouts_lookup.get(key, 0)
                else:  # 複勝
                    spend, ret = 100, 0
                    if target["horse_number"] in rd["top3_set"]:
                        key = (rd["race_id"], "複勝", str(target["horse_number"]))
                        ret = payouts_lookup.get(key, 0)
                stats[strat]["spend"] += spend
                stats[strat]["return"] += ret
                stats[strat]["n"] += 1

        track5_results[band_name] = {}
        print(f"\n── {band_name} ──")
        print(f"  {'戦略':12} {'n':>5} {'spend':>7} {'return':>7} {'ROI':>7}")
        for strat in pop_strats:
            d = stats[strat]
            if d["spend"] == 0: continue
            roi = d["return"] / d["spend"] * 100
            flag = " 🟢" if roi >= 100 else (" 🟡" if roi >= 90 else "")
            print(f"  {strat:12} {d['n']:>5} {d['spend']:>7,} {int(d['return']):>7,} {roi:>5.1f}%{flag}")
            track5_results[band_name][strat] = {"n": d["n"], "spend": d["spend"], "return": d["return"], "roi": round(roi, 1)}

    output["track5_market_inefficiency"] = track5_results

    # ─────────────────────────────────────────────────────
    # Track 4: 券種別 ROI 比較
    # ─────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"🎯 Track 4: 券種別 ROI 比較 (◎ベース)")
    print(f"{'='*60}")

    track4_results = {}
    bet_strategies = {
        "単勝◎": lambda rd: [(100, 100 if rd["sorted_preds"][0]["horse_number"] == rd["win_h"] else 0,
                              ("単勝", str(rd["sorted_preds"][0]["horse_number"])))],
        "複勝◎": lambda rd: [(100, 100 if rd["sorted_preds"][0]["horse_number"] in rd["top3_set"] else 0,
                              ("複勝", str(rd["sorted_preds"][0]["horse_number"])))],
    }

    # 馬連 ◎-2,3,4,5 (4点)
    def uma_4ten(rd):
        bets = []
        first = rd["sorted_preds"][0]["horse_number"]
        for p in rd["sorted_preds"][1:5]:
            o = p["horse_number"]
            key = (rd["race_id"], "馬連", "-".join(sorted(str(x) for x in [first, o])))
            hit = {first, o} == {rd["win_h"], rd["p2_h"]}
            bets.append((100, hit, key))
        return bets

    # ワイド ◎-2,3,4 (3点)
    def wide_3ten(rd):
        bets = []
        first = rd["sorted_preds"][0]["horse_number"]
        for p in rd["sorted_preds"][1:4]:
            o = p["horse_number"]
            key = (rd["race_id"], "ワイド", "-".join(sorted(str(x) for x in [first, o])))
            hit = first in rd["top3_set"] and o in rd["top3_set"]
            bets.append((100, hit, key))
        return bets

    # 三連複 ◎-相手2,3 (3点) — フォーメーション形式
    def trio_form(rd):
        bets = []
        first = rd["sorted_preds"][0]["horse_number"]
        others = [p["horse_number"] for p in rd["sorted_preds"][1:4]]
        for a, b in combinations(others, 2):
            key = (rd["race_id"], "三連複", "-".join(sorted(str(x) for x in [first, a, b])))
            hit = {first, a, b} == rd["top3_set"]
            bets.append((100, hit, key))
        return bets

    # 三連単 1着固定 ◎ / 2,3 着 = ML 2-4 位 (6点)
    def trifecta_first(rd):
        bets = []
        first = rd["sorted_preds"][0]["horse_number"]
        others = [p["horse_number"] for p in rd["sorted_preds"][1:4]]
        for a, b in permutations(others, 2):
            key = (rd["race_id"], "三連単", "-".join(str(x) for x in [first, a, b]))
            hit = (first == rd["win_h"]) and (a == rd["p2_h"]) and (b == rd["p3_h"])
            bets.append((100, hit, key))
        return bets

    # 三連単 BOX ◎○▲ (6点)
    def trifecta_box3(rd):
        bets = []
        top3 = [p["horse_number"] for p in rd["sorted_preds"][:3]]
        for a, b, c in permutations(top3, 3):
            key = (rd["race_id"], "三連単", "-".join(str(x) for x in [a, b, c]))
            hit = (a == rd["win_h"]) and (b == rd["p2_h"]) and (c == rd["p3_h"])
            bets.append((100, hit, key))
        return bets

    # 馬単 ◎-相手 4点
    def umatan_4ten(rd):
        bets = []
        first = rd["sorted_preds"][0]["horse_number"]
        for p in rd["sorted_preds"][1:5]:
            o = p["horse_number"]
            key = (rd["race_id"], "馬単", "-".join(str(x) for x in [first, o]))
            hit = (first == rd["win_h"]) and (o == rd["p2_h"])
            bets.append((100, hit, key))
        return bets

    bet_strategies.update({
        "馬連流し(4点)": uma_4ten,
        "ワイド流し(3点)": wide_3ten,
        "三連複フォーメ(3点)": trio_form,
        "三連単1着固定(6点)": trifecta_first,
        "三連単BOX 3頭(6点)": trifecta_box3,
        "馬単流し(4点)": umatan_4ten,
    })

    for strat_name, strat_fn in bet_strategies.items():
        total_spend = 0
        total_return = 0
        total_hits = 0
        for rd in race_data:
            for spend, hit_or_amount, key in strat_fn(rd):
                if isinstance(hit_or_amount, bool):
                    total_spend += spend
                    if hit_or_amount:
                        amt = payouts_lookup.get(key, 0)
                        total_return += amt
                        if amt > 0:
                            total_hits += 1
                else:  # 単勝/複勝 case is (spend, return_amount, key)
                    total_spend += spend
                    if hit_or_amount > 0:
                        # hit_or_amount is non-zero means hit
                        amt = payouts_lookup.get(key, 0)
                        total_return += amt
                        if amt > 0:
                            total_hits += 1

        roi = total_return / total_spend * 100 if total_spend else 0
        flag = " 🟢" if roi >= 100 else (" 🟡" if roi >= 90 else "")
        print(f"  {strat_name:25} spend ¥{total_spend:>10,} return ¥{int(total_return):>10,} hits={total_hits:>5} ROI={roi:>5.1f}%{flag}")
        track4_results[strat_name] = {"spend": total_spend, "return": total_return, "hits": total_hits, "roi": round(roi, 1)}

    output["track4_bet_types"] = track4_results

    # ─────────────────────────────────────────────────────
    # Track 1: 馬場バイアス (同日内 R1-R8 → R9-R12)
    # ─────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"🎯 Track 1: 馬場バイアス分析")
    print(f"{'='*60}")

    # 同日 venue 単位で R1-R8 の傾向を集計、R9+ の予測に補正
    # まず race_data を (date, venue) で group
    by_day_venue = defaultdict(list)
    for rd in race_data:
        d = rd["ri"].get("race_date", "")
        v = rd["ri"].get("venue", "?")
        by_day_venue[(d, v)].append(rd)

    # 各日 venue の R1-R8 から「先行有利度」を集計
    # 簡易化: 1人気が来た rate
    day_bias = {}
    for (d, v), rds in by_day_venue.items():
        rds_sorted = sorted(rds, key=lambda x: x["ri"].get("race_number", 0) or 0)
        early = [r for r in rds_sorted if (r["ri"].get("race_number", 99) or 99) <= 8]
        if len(early) < 4: continue
        # 1人気の単勝率
        n_fav_win = 0
        for r in early:
            by_pop = sorted([p for p in r["all_preds"] if p["popularity"] > 0], key=lambda x: x["popularity"])
            if not by_pop: continue
            if by_pop[0]["horse_number"] == r["win_h"]:
                n_fav_win += 1
        fav_rate = n_fav_win / len(early)
        day_bias[(d, v)] = {"n_early": len(early), "fav_win_rate": fav_rate}

    # 後続 R9+ の 1人気単勝 ROI を、当日 bias で層別
    track1_results = defaultdict(lambda: {"spend": 0, "return": 0, "n": 0})
    for rd in race_data:
        d = rd["ri"].get("race_date", "")
        v = rd["ri"].get("venue", "?")
        rn = rd["ri"].get("race_number", 0) or 0
        if rn < 9: continue
        bias = day_bias.get((d, v))
        if not bias: continue
        by_pop = sorted([p for p in rd["all_preds"] if p["popularity"] > 0], key=lambda x: x["popularity"])
        if not by_pop: continue
        fav = by_pop[0]
        # bias 帯別に分類
        fr = bias["fav_win_rate"]
        if fr >= 0.5: band = "本命堅め日 (R1-8で1人気率≥50%)"
        elif fr >= 0.3: band = "中庸日 (30-49%)"
        else: band = "波乱日 (1人気率<30%)"

        bucket = track1_results[band]
        bucket["n"] += 1
        bucket["spend"] += 100
        if fav["horse_number"] == rd["win_h"]:
            key = (rd["race_id"], "単勝", str(fav["horse_number"]))
            bucket["return"] += payouts_lookup.get(key, 0)

    print(f"\n馬場バイアス検出 (R9+ の 1人気単勝 ROI):")
    print(f"  {'帯':40} {'n':>4} {'spend':>6} {'return':>7} {'ROI':>7}")
    for band, d in sorted(track1_results.items()):
        roi = d["return"] / d["spend"] * 100 if d["spend"] else 0
        flag = " 🟢" if roi >= 100 else (" 🟡" if roi >= 90 else "")
        print(f"  {band:40} {d['n']:>4} {d['spend']:>6,} {int(d['return']):>7,} {roi:>5.1f}%{flag}")

    output["track1_track_bias"] = {k: dict(v) for k, v in track1_results.items()}

    # JSON 出力
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n📝 JSON 出力: {args.output}")


if __name__ == "__main__":
    main()
