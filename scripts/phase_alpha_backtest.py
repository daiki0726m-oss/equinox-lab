#!/usr/bin/env python3
"""Phase α 効果計測バックテスト

historical_backtest.py の拡張版。
Baseline (Phase α なし) vs Phase α 全機能 (体重補正・馬場バイアス・whitelist) を
同一データで比較し、ROI 改善幅を計測。

Phase α 機能:
  - 馬体重変化補正 (±6kg+: 0.90, ±10kg+: 0.80)
  - 馬場バイアス検出 (同日 R1-R8 → R9+ post_position 補正)
  - EV>1.2 厳格化 (betting.py で実装済 → 自動適用)
  - whitelist mode (オプション)

Usage:
    python3 scripts/phase_alpha_backtest.py --year 2025
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


def classify_race(name):
    if not name: return "OP"
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


def wc_factor(wc):
    """Phase α-3: 馬体重変化補正"""
    if wc is None or wc == 0: return 1.0
    absw = abs(wc)
    if absw <= 2: return 1.00
    if absw <= 5: return 0.97
    if absw <= 9: return 0.90
    return 0.80


def detect_track_bias_simple(race_data_list, target_race):
    """Phase α-4: 同日同 venue の R1-R8 から馬場バイアス検出"""
    same_day = [
        rd for rd in race_data_list
        if rd["ri"].get("race_date") == target_race["ri"].get("race_date")
        and rd["ri"].get("venue") == target_race["ri"].get("venue")
        and (rd["ri"].get("race_number") or 99) <= 8
    ]
    if len(same_day) < 3:
        return None
    inside_wins = outside_wins = total = 0
    for rd in same_day:
        # 3着内の post_position
        top3_horses = [rd["win_h"], rd["p2_h"], rd["p3_h"]]
        for hn in top3_horses:
            p = next((x for x in rd["all_preds"] if x["horse_number"] == hn), None)
            if not p: continue
            pp = p.get("post_position") or 0
            if pp <= 0: continue
            total += 1
            if pp <= 3: inside_wins += 1
            elif pp >= 6: outside_wins += 1
    if total == 0: return None
    ir, or_ = inside_wins / total, outside_wins / total
    if ir >= 0.48 and ir > or_ + 0.10:
        return {"frame_bias": "inside", "inside_rate": ir, "outside_rate": or_}
    if or_ >= 0.48 and or_ > ir + 0.10:
        return {"frame_bias": "outside", "inside_rate": ir, "outside_rate": or_}
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--year", type=int, default=2025)
    ap.add_argument("--output", default="docs/analysis/phase_alpha_results.json")
    args = ap.parse_args()

    print(f"📊 Phase α 効果計測 backtest (year={args.year})")
    print("=" * 60)
    init_db()

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

    # weight_change を horse_number -> wc に
    weight_change_by_race = {}
    for rid, group in results_grouped.items():
        m = {}
        for _, r in group.iterrows():
            hn = r.get("horse_number")
            wc = r.get("weight_change")
            if hn is not None and wc is not None:
                try: m[int(hn)] = int(wc)
                except (TypeError, ValueError): pass
        weight_change_by_race[rid] = m

    # post_position by race
    pp_by_race = {}
    for rid, group in results_grouped.items():
        m = {}
        for _, r in group.iterrows():
            hn = r.get("horse_number")
            pp = r.get("post_position")
            if hn is not None and pp is not None:
                try: m[int(hn)] = int(pp)
                except (TypeError, ValueError): pass
        pp_by_race[rid] = m

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

    # race_data を全件構築
    race_data = []
    for race_id, group in df.groupby("race_id"):
        ri = race_info.get(race_id, {})
        group = group.sort_values("rank_score", ascending=False).copy()
        rank_exp = np.exp(group["rank_score"] - group["rank_score"].max())
        group["pred_win"] = rank_exp / rank_exp.sum()

        predictions = []
        for _, row in group.iterrows():
            odds = row["odds"]
            if odds <= 0: continue
            hn = int(row["horse_number"])
            predictions.append({
                "horse_number": hn,
                "pred_win": float(row["pred_win"]),
                "pred_top3": float(row["pred_top3"]),
                "odds_win": float(odds),
                "popularity": int(row["popularity"]) if row.get("popularity") else 0,
                "finish_position": int(row["finish_position"]) if row.get("finish_position") else 0,
                "post_position": pp_by_race.get(race_id, {}).get(hn, 0),
            })
        if len(predictions) < 5: continue
        win_h = next((p["horse_number"] for p in predictions if p["finish_position"] == 1), None)
        p2_h = next((p["horse_number"] for p in predictions if p["finish_position"] == 2), None)
        p3_h = next((p["horse_number"] for p in predictions if p["finish_position"] == 3), None)
        if not all([win_h, p2_h, p3_h]): continue
        race_data.append({
            "race_id": race_id, "ri": ri,
            "all_preds": predictions,
            "top3_set": {win_h, p2_h, p3_h},
            "win_h": win_h, "p2_h": p2_h, "p3_h": p3_h,
        })

    print(f"📊 有効 race: {len(race_data)}")
    strategy = BettingStrategy()

    # ── 2 モード並列計算 ──
    # baseline: confidence + race_volatility (現状)
    # alpha: + 馬体重補正 + 馬場バイアス + (whitelist は別計測)
    results = {
        "baseline": defaultdict(lambda: {"spend": 0, "return": 0, "hits": 0, "races": 0}),
        "alpha": defaultdict(lambda: {"spend": 0, "return": 0, "hits": 0, "races": 0}),
        "whitelist": defaultdict(lambda: {"spend": 0, "return": 0, "hits": 0, "races": 0}),
    }
    counts = {"baseline": defaultdict(int), "alpha": defaultdict(int), "whitelist": defaultdict(int)}

    for rd in race_data:
        ri = rd["ri"]
        race_id = rd["race_id"]
        race_name = ri.get("race_name", "") or ""
        race_class = classify_race(race_name)

        # ─── baseline: 既存ロジック ───
        preds_baseline = [dict(p) for p in rd["all_preds"]]  # copy
        sorted_b = sorted(preds_baseline, key=lambda x: -x["pred_win"])
        top1_b = sorted_b[0]
        top2_b = sorted_b[1] if len(sorted_b) >= 2 else top1_b
        c_b = eval_confidence(
            top_win_pct=top1_b["pred_win"] * 100,
            n_horses=len(preds_baseline),
            top3_sum_pct=sum(p["pred_win"] * 100 for p in sorted_b[:3]),
            grade=ri.get("grade"),
            second_win_pct=top2_b["pred_win"] * 100,
            top_top3_pct=top1_b["pred_top3"] * 100,
            top_popularity=top1_b.get("popularity") if top1_b.get("popularity") else None,
            top_odds=top1_b["odds_win"],
        )
        conf_b = c_b["confidence"]
        vol_b = compute_race_volatility(ri)
        if vol_b.get("conf_adjust", 0) < 0:
            conf_b = downgrade(conf_b, -vol_b["conf_adjust"])

        # ─── alpha: 馬体重補正 + 馬場バイアス ───
        preds_alpha = [dict(p) for p in rd["all_preds"]]  # copy

        # 馬体重補正
        wc_map = weight_change_by_race.get(race_id, {})
        for p in preds_alpha:
            wc = wc_map.get(p["horse_number"])
            f = wc_factor(wc)
            if f != 1.0:
                p["pred_win"] *= f
                p["pred_top3"] *= f

        # 馬場バイアス補正 (R9+)
        rn = ri.get("race_number") or 0
        if rn >= 9:
            bias = detect_track_bias_simple(race_data, rd)
            if bias:
                fb = bias["frame_bias"]
                for p in preds_alpha:
                    pp = p.get("post_position", 0)
                    factor = 1.0
                    if fb == "inside":
                        if pp <= 3: factor = 1.08
                        elif pp >= 6: factor = 0.92
                    elif fb == "outside":
                        if pp >= 6: factor = 1.08
                        elif pp <= 3: factor = 0.92
                    if factor != 1.0:
                        p["pred_win"] *= factor

        # 再正規化
        tw = sum(p["pred_win"] for p in preds_alpha)
        if tw > 0:
            for p in preds_alpha: p["pred_win"] /= tw

        sorted_a = sorted(preds_alpha, key=lambda x: -x["pred_win"])
        top1_a = sorted_a[0]
        top2_a = sorted_a[1] if len(sorted_a) >= 2 else top1_a
        c_a = eval_confidence(
            top_win_pct=top1_a["pred_win"] * 100,
            n_horses=len(preds_alpha),
            top3_sum_pct=sum(p["pred_win"] * 100 for p in sorted_a[:3]),
            grade=ri.get("grade"),
            second_win_pct=top2_a["pred_win"] * 100,
            top_top3_pct=top1_a["pred_top3"] * 100,
            top_popularity=top1_a.get("popularity") if top1_a.get("popularity") else None,
            top_odds=top1_a["odds_win"],
        )
        conf_a = c_a["confidence"]
        vol_a = compute_race_volatility(ri)
        if vol_a.get("conf_adjust", 0) < 0:
            conf_a = downgrade(conf_a, -vol_a["conf_adjust"])

        # 各 mode で should_bet 判定 + 三連複◎軸 5頭流し + 単勝
        def run_bets(mode_name, sorted_p, confidence, strict_whitelist=False):
            should_bet, _ = strategy.should_bet_race(
                [{"pred_win": p["pred_win"], "odds_win": p["odds_win"]} for p in sorted_p],
                confidence=confidence, race_info=ri,
                strict_whitelist=strict_whitelist,
            )
            if not should_bet:
                return
            counts[mode_name][confidence] += 1
            results[mode_name]["counts"]["races"] = results[mode_name].get("counts", {"races": 0}).get("races", 0) + 1

            # 三連複◎軸 (10点)
            top6 = [p["horse_number"] for p in sorted_p[:6]]
            first = top6[0]
            rest = top6[1:6]
            for a, b in combinations(rest, 2):
                spend = 100
                ret = 0
                if {first, a, b} == rd["top3_set"]:
                    key = (race_id, "三連複", "-".join(sorted(str(x) for x in [first, a, b])))
                    ret = payouts_lookup.get(key, 0)
                results[mode_name]["三連複◎軸"]["spend"] += spend
                results[mode_name]["三連複◎軸"]["return"] += ret
                if ret > 0: results[mode_name]["三連複◎軸"]["hits"] += 1
            results[mode_name]["三連複◎軸"]["races"] += 1

            # 単勝
            results[mode_name]["単勝◎"]["spend"] += 100
            if first == rd["win_h"]:
                key = (race_id, "単勝", str(first))
                results[mode_name]["単勝◎"]["return"] += payouts_lookup.get(key, 0)
                results[mode_name]["単勝◎"]["hits"] += 1
            results[mode_name]["単勝◎"]["races"] += 1

        run_bets("baseline", sorted_b, conf_b)
        run_bets("alpha", sorted_a, conf_a)
        run_bets("whitelist", sorted_a, conf_a, strict_whitelist=True)

    # ── レポート ──
    print(f"\n{'='*60}")
    print(f"📊 Phase α 効果計測 結果 ({len(race_data)} races, {args.year})")
    print(f"{'='*60}")

    for mode in ["baseline", "alpha", "whitelist"]:
        print(f"\n── {mode.upper()} mode ──")
        for bet_type in ["単勝◎", "三連複◎軸"]:
            d = results[mode][bet_type]
            if d["spend"] == 0:
                print(f"  {bet_type:10}: spend ¥0 (該当 race なし)")
                continue
            roi = d["return"] / d["spend"] * 100
            flag = " 🟢" if roi >= 100 else (" 🟡" if roi >= 80 else "")
            print(f"  {bet_type:10}: races={d['races']:>5} spend=¥{d['spend']:>10,} "
                  f"return=¥{int(d['return']):>10,} hits={d['hits']:>4} ROI={roi:>5.1f}%{flag}")

    print(f"\n── 比較 ──")
    for bet_type in ["単勝◎", "三連複◎軸"]:
        baseline_roi = (results["baseline"][bet_type]["return"] / results["baseline"][bet_type]["spend"] * 100
                        if results["baseline"][bet_type]["spend"] else 0)
        alpha_roi = (results["alpha"][bet_type]["return"] / results["alpha"][bet_type]["spend"] * 100
                     if results["alpha"][bet_type]["spend"] else 0)
        wl_roi = (results["whitelist"][bet_type]["return"] / results["whitelist"][bet_type]["spend"] * 100
                  if results["whitelist"][bet_type]["spend"] else 0)
        print(f"  {bet_type}: baseline {baseline_roi:.1f}% → alpha {alpha_roi:.1f}% (Δ{alpha_roi-baseline_roi:+.1f}pt) / whitelist {wl_roi:.1f}%")

    # JSON 出力
    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    out = {
        "year": args.year,
        "n_races": len(race_data),
        "modes": {},
    }
    for mode in ["baseline", "alpha", "whitelist"]:
        out["modes"][mode] = {}
        for bet_type in ["単勝◎", "三連複◎軸"]:
            d = results[mode][bet_type]
            roi = d["return"] / d["spend"] * 100 if d["spend"] else 0
            out["modes"][mode][bet_type] = {**d, "roi": round(roi, 1)}
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2, default=str)
    print(f"\n📝 JSON 出力: {args.output}")


if __name__ == "__main__":
    main()
