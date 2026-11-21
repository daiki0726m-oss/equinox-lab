#!/usr/bin/env python3
"""信頼度別 × 券種別 ROI バックテスト。

Usage:
    python3 run_backtest.py --from 2026-03-01 --to 2026-05-31 [--bet-types 馬連,ワイド,三連複]

scripts/backtest_v4_confidence.py の wrapper だが、信頼度別の breakdown も
JSON で吐く。
"""
import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict
from itertools import combinations


def find_db():
    for p in [
        os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "keiba.db"),
        "keiba.db",
    ]:
        if os.path.exists(p):
            return os.path.abspath(p)
    raise FileNotFoundError("keiba.db not found")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="date_from", default="2026-04-01")
    ap.add_argument("--to", dest="date_to", default="2026-05-31")
    ap.add_argument("--db", default=None)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db or find_db())
    cur = conn.cursor()

    # 対象 race + 信頼度 取得
    cur.execute("""
        SELECT pc.race_id, r.race_date, pc.confidence, pc.predictions_json
        FROM predictions_cache pc
        JOIN races r ON pc.race_id = r.race_id
        WHERE r.race_date BETWEEN ? AND ?
        ORDER BY r.race_date
    """, (args.date_from, args.date_to))
    races = cur.fetchall()

    # 信頼度 bucket 別の集計
    buckets = defaultdict(lambda: defaultdict(lambda: {"spend": 0, "return": 0, "hits": 0, "races": 0}))
    # 日別の集計 (S+A 合算で「単日支配バイアス」の検出に使う)
    daily_sa = defaultdict(lambda: defaultdict(lambda: {"spend": 0, "return": 0}))
    rating_count = defaultdict(int)
    resulted = 0

    for race_id, race_date, conf, preds_json in races:
        rating_count[conf] += 1
        preds = json.loads(preds_json)
        if not preds: continue
        sorted_p = sorted(preds, key=lambda x: -x.get("pred_win_pct", 0))
        top6 = [p.get("horse_number") for p in sorted_p[:6]]
        if not all(top6[:5]):
            continue

        # 結果
        cur.execute("SELECT horse_number, finish_position FROM results "
                    "WHERE race_id=? AND finish_position>0 ORDER BY finish_position", (race_id,))
        results = cur.fetchall()
        if not results: continue
        resulted += 1

        win_h = next((h for h, p in results if p == 1), None)
        p2_h = next((h for h, p in results if p == 2), None)
        p3_h = next((h for h, p in results if p == 3), None)
        top3_set = {win_h, p2_h, p3_h}

        cur.execute("SELECT bet_type, combination, payout_amount FROM payouts WHERE race_id=?", (race_id,))
        payouts = defaultdict(dict)
        for bt, combo, amt in cur.fetchall():
            payouts[bt][combo] = amt

        top1 = top6[0]
        others = [h for h in top6[1:6] if h]

        is_sa = conf in ("S", "A")

        # 馬連流し (5点)
        bk = buckets[conf]["馬連流し(5点)"]
        bk["races"] += 1
        for o in others:
            bk["spend"] += 100
            if is_sa: daily_sa[race_date]["馬連流し(5点)"]["spend"] += 100
            if {top1, o} == {win_h, p2_h}:
                key = "-".join(sorted(str(x) for x in [top1, o]))
                amt = payouts["馬連"].get(key, 0)
                bk["return"] += amt
                if is_sa: daily_sa[race_date]["馬連流し(5点)"]["return"] += amt
                if amt > 0: bk["hits"] += 1

        # ワイド流し (5点)
        bk = buckets[conf]["ワイド流し(5点)"]
        bk["races"] += 1
        for o in others:
            bk["spend"] += 100
            if is_sa: daily_sa[race_date]["ワイド流し(5点)"]["spend"] += 100
            if top1 in top3_set and o in top3_set:
                key = "-".join(sorted(str(x) for x in [top1, o]))
                amt = payouts["ワイド"].get(key, 0)
                bk["return"] += amt
                if is_sa: daily_sa[race_date]["ワイド流し(5点)"]["return"] += amt
                if amt > 0: bk["hits"] += 1

        # 三連複 ◎軸 (10点)
        bk = buckets[conf]["三連複◎軸(10点)"]
        bk["races"] += 1
        for a, b in combinations(others, 2):
            bk["spend"] += 100
            if is_sa: daily_sa[race_date]["三連複◎軸(10点)"]["spend"] += 100
            if {top1, a, b} == top3_set:
                key = "-".join(sorted(str(x) for x in [top1, a, b]))
                amt = payouts["三連複"].get(key, 0)
                bk["return"] += amt
                if is_sa: daily_sa[race_date]["三連複◎軸(10点)"]["return"] += amt
                if amt > 0: bk["hits"] += 1

        # 三連複 フォーメーション ◎-○▲-△×注 (2×3=6点) — 1着候補=◎ / 2着候補=印2-3位 / 3着候補=印4-6位
        # 三連複は順序問わずなので、組合せが既に異なる5頭の集合になる前提で6点
        bk = buckets[conf]["三連複フォーメーション(6点)"]
        bk["races"] += 1
        if len(others) >= 5:
            front = others[:2]  # ○▲
            back = others[2:5]  # △×注
            for a in front:
                for b in back:
                    bk["spend"] += 100
                    if {top1, a, b} == top3_set:
                        key = "-".join(sorted(str(x) for x in [top1, a, b]))
                        amt = payouts["三連複"].get(key, 0)
                        bk["return"] += amt
                        if amt > 0: bk["hits"] += 1

    # ROI 計算
    report = {
        "period": {"from": args.date_from, "to": args.date_to},
        "n_races_cached": len(races),
        "n_races_with_results": resulted,
        "rating_distribution": dict(rating_count),
        "by_rating": {},
    }
    for rating in ["S", "A", "B", "C", "D"]:
        if rating not in buckets: continue
        bts = buckets[rating]
        report["by_rating"][rating] = {
            "n_races": rating_count.get(rating, 0),
            "bet_types": {},
        }
        for bt, d in bts.items():
            roi = round(100 * d["return"] / d["spend"], 1) if d["spend"] else 0
            report["by_rating"][rating]["bet_types"][bt] = {
                "spend": d["spend"], "return": d["return"], "hits": d["hits"],
                "races": d["races"], "roi_pct": roi,
            }

    # 全体
    total = defaultdict(lambda: {"spend": 0, "return": 0, "hits": 0, "races": 0})
    for rating, bts in buckets.items():
        for bt, d in bts.items():
            for k in ["spend", "return", "hits", "races"]:
                total[bt][k] += d[k]
    report["overall"] = {
        bt: {**d, "roi_pct": round(100*d["return"]/d["spend"], 1) if d["spend"] else 0}
        for bt, d in total.items()
    }

    # 🆕 単日支配バイアス検出 (S+A 限定、各券種ごと)
    # 各日 spend が全体 spend の >20% なら警告
    # 単日除外後 ROI が全体 ±20pt 以上ぶれたら「単日支配」と判定
    report["single_day_dominance_check"] = {}
    for bt in ["馬連流し(5点)", "ワイド流し(5点)", "三連複◎軸(10点)", "三連複フォーメーション(6点)"]:
        tot_s = sum(d[bt]["spend"] for d in daily_sa.values())
        tot_r = sum(d[bt]["return"] for d in daily_sa.values())
        if tot_s == 0:
            continue
        baseline_roi = tot_r * 100 / tot_s
        per_day = []
        for date in sorted(daily_sa.keys()):
            s, r = daily_sa[date][bt]["spend"], daily_sa[date][bt]["return"]
            if s == 0: continue
            spend_share = s * 100 / tot_s
            day_roi = r * 100 / s
            ex_roi = (tot_r - r) * 100 / (tot_s - s) if tot_s - s > 0 else 0
            flag = "⚠️ 単日支配" if (spend_share > 20 and abs(ex_roi - baseline_roi) > 20) else ""
            per_day.append({
                "date": date,
                "spend": s,
                "return": r,
                "spend_share_pct": round(spend_share, 1),
                "day_roi_pct": round(day_roi, 1),
                "excluded_roi_pct": round(ex_roi, 1),
                "delta_pt": round(ex_roi - baseline_roi, 1),
                "flag": flag,
            })
        report["single_day_dominance_check"][bt] = {
            "baseline_roi_pct": round(baseline_roi, 1),
            "per_day": per_day,
            "warning": any(d["flag"] for d in per_day),
        }

    print(json.dumps(report, ensure_ascii=False, indent=2))
    conn.close()


if __name__ == "__main__":
    main()
