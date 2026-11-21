"""v4 信頼度 vs ROI 検証スクリプト

predictions_cache の predictions_json から ◎ 情報を取り出し、
confidence.evaluate() を v4 ロジックで再評価。
結果 (results) + 払戻 (payouts) と紐付けて、信頼度別の ROI を集計。

Usage:
    python3 scripts/backtest_v4_confidence.py --from 2026-05-09 --to 2026-05-24
"""

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

# allow importing from repo root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from confidence import evaluate  # type: ignore


def fetch_races(conn, date_from, date_to):
    c = conn.cursor()
    c.execute(
        """
        SELECT r.race_id, r.race_date, r.race_name, r.grade,
               pc.predictions_json, pc.confidence as old_conf
        FROM predictions_cache pc
        JOIN races r ON pc.race_id = r.race_id
        WHERE r.race_date BETWEEN ? AND ?
        ORDER BY r.race_date, r.race_id
        """,
        (date_from, date_to),
    )
    return c.fetchall()


def get_results(conn, race_id):
    c = conn.cursor()
    c.execute(
        "SELECT horse_number, finish_position FROM results WHERE race_id=? AND finish_position>0 ORDER BY finish_position",
        (race_id,),
    )
    return c.fetchall()


def get_payouts(conn, race_id):
    c = conn.cursor()
    c.execute(
        "SELECT bet_type, combination, payout_amount FROM payouts WHERE race_id=?",
        (race_id,),
    )
    out = defaultdict(dict)
    for bt, combo, amt in c.fetchall():
        out[bt][combo] = amt
    return out


def _norm_combo(nums):
    """[3, 5, 8] → '3-5-8' (昇順)"""
    return "-".join(str(n) for n in sorted(nums))


def sim_bets(top_horses, results, payouts):
    """既定買い目: 馬連流し / ワイド流し / 三連複フォメ で ROI を計算。

    Args:
        top_horses: ◎○▲△×注 の horse_number リスト (6頭、None あり)
        results: [(horse_number, finish_position), ...]
        payouts: {bet_type: {combo: amount}}

    Returns:
        {bet_type: (spend, return, hit_count, bet_count)}
    """
    out = {}
    if not top_horses or len(top_horses) < 5 or any(h is None for h in top_horses[:5]):
        return out

    fin_pos = {h: p for h, p in results}
    top1 = top_horses[0]
    if top1 not in fin_pos:
        return out

    # 1着・2着・3着 馬番
    win_h = next((h for h, p in results if p == 1), None)
    p2_h = next((h for h, p in results if p == 2), None)
    p3_h = next((h for h, p in results if p == 3), None)

    # 馬連流し ◎-相手5頭 (5点)
    others = [h for h in top_horses[1:6] if h is not None]
    umaren_spend = len(others) * 100
    umaren_return = 0
    umaren_hit = 0
    if win_h and p2_h:
        winning = {win_h, p2_h}
        for o in others:
            if {top1, o} == winning:
                key1 = _norm_combo([top1, o])
                amt = payouts.get("馬連", {}).get(key1, 0)
                umaren_return += amt
                if amt > 0:
                    umaren_hit += 1
                break
    out["馬連流し(5点)"] = (umaren_spend, umaren_return, umaren_hit, 1)

    # ワイド流し ◎-相手5頭 (5点)
    wide_spend = len(others) * 100
    wide_return = 0
    wide_hit = 0
    top3_set = {win_h, p2_h, p3_h}
    if top1 in top3_set:
        for o in others:
            if o in top3_set:
                pair = sorted([top1, o])
                key = f"{pair[0]}-{pair[1]}"
                amt = payouts.get("ワイド", {}).get(key, 0)
                wide_return += amt
                if amt > 0:
                    wide_hit += 1
    out["ワイド流し(5点)"] = (wide_spend, wide_return, wide_hit, 1)

    # 三連複 ◎軸-2頭/3頭 (10点) — ◎ × {○▲△×注 から 2頭組合せ}
    relays = [h for h in top_horses[1:6] if h is not None]
    trio_spend = 0
    trio_return = 0
    trio_hit = 0
    from itertools import combinations
    for a, b in combinations(relays, 2):
        trio_spend += 100
        if {top1, a, b} == top3_set:
            key = _norm_combo([top1, a, b])
            amt = payouts.get("三連複", {}).get(key, 0)
            trio_return += amt
            if amt > 0:
                trio_hit += 1
    out["三連複◎軸(10点)"] = (trio_spend, trio_return, trio_hit, 1)

    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="date_from", default="2026-05-01")
    ap.add_argument("--to", dest="date_to", default="2026-05-24")
    ap.add_argument("--db", default="keiba.db")
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    races = fetch_races(conn, args.date_from, args.date_to)
    print(f"━━━━━━ v4 Confidence Backtest {args.date_from}〜{args.date_to} ━━━━━━")
    print(f"対象 race 数 (cache あり): {len(races)}")

    # 集計バケット: rating -> {bet_type -> [spend, return, hit, race_count]}
    buckets = defaultdict(lambda: defaultdict(lambda: [0, 0, 0, 0]))
    rating_counts = defaultdict(int)
    no_result_count = 0

    for race_id, race_date, race_name, grade, preds_json, old_conf in races:
        preds = json.loads(preds_json)
        if not preds:
            continue
        sorted_p = sorted(preds, key=lambda x: x.get("pred_win_pct", 0), reverse=True)
        top1 = sorted_p[0]

        # v4 confidence
        top_win = top1.get("pred_win_pct", 0)
        top_top3 = top1.get("pred_top3_pct", 0)
        top3_sum = sum(p.get("pred_win_pct", 0) for p in sorted_p[:3])
        top_odds = top1.get("odds_win", 0) or 0
        top_pop = top1.get("popularity") or 0

        r = evaluate(
            top_win_pct=top_win,
            n_horses=len(preds),
            top3_sum_pct=top3_sum,
            grade=grade,
            second_win_pct=sorted_p[1].get("pred_win_pct", 0) if len(sorted_p) > 1 else 0,
            top_top3_pct=top_top3,
            top_popularity=top_pop,
            top_odds=top_odds,
        )
        rating = r["confidence"]
        rating_counts[rating] += 1

        # 結果取得
        results = get_results(conn, race_id)
        if not results:
            no_result_count += 1
            continue
        payouts = get_payouts(conn, race_id)

        # 印馬: mark='◎'→pred_win 1位, '○'→2位 etc. でも mark 列に依存
        # ここではシンプルに pred_win 順で上位6頭を ◎○▲△×注 と仮定
        top_horses = [p.get("horse_number") for p in sorted_p[:6]]

        # 各券種のシミュ
        bet_results = sim_bets(top_horses, results, payouts)
        for bt, (sp, ret, hit, rc) in bet_results.items():
            buckets[rating][bt][0] += sp
            buckets[rating][bt][1] += ret
            buckets[rating][bt][2] += hit
            buckets[rating][bt][3] += rc

    print(f"結果あり race: {len(races) - no_result_count}\n")

    # 信頼度分布
    print("━━━━━━ v4 信頼度分布 ━━━━━━")
    total = sum(rating_counts.values())
    for k in ["S", "A", "B", "C", "D"]:
        n = rating_counts.get(k, 0)
        pct = 100 * n / total if total else 0
        print(f"  {k}: {n:>3} ({pct:.1f}%)")

    # 信頼度別 ROI (券種ごと)
    print(f"\n━━━━━━ 信頼度別 ROI (各券種, 100円) ━━━━━━")
    print(f"{'rating':<8} {'券種':<22} {'R数':>4} {'的中':>4} {'投資':>10} {'回収':>11} {'ROI':>7}")
    print("-" * 75)
    for k in ["S", "A", "B", "C", "D"]:
        if k not in buckets:
            print(f"{k:<8} (該当 race なし)")
            continue
        # 総合
        for bt in ["馬連流し(5点)", "ワイド流し(5点)", "三連複◎軸(10点)"]:
            d = buckets[k].get(bt)
            if not d:
                continue
            sp, ret, hit, rc = d
            if sp == 0:
                continue
            roi = ret / sp * 100
            print(f"{k:<8} {bt:<22} {rc:>4} {hit:>4} {sp:>9.0f}円 {ret:>10.0f}円 {roi:>6.1f}%")
        print()

    # 全合計 (rating 横断)
    print("━━━━━━ 券種別 全体 ROI ━━━━━━")
    print(f"{'券種':<22} {'R数':>4} {'的中':>4} {'投資':>10} {'回収':>11} {'ROI':>7}")
    print("-" * 70)
    for bt in ["馬連流し(5点)", "ワイド流し(5点)", "三連複◎軸(10点)"]:
        sp = ret = hit = rc = 0
        for k in buckets:
            d = buckets[k].get(bt)
            if d:
                sp += d[0]; ret += d[1]; hit += d[2]; rc += d[3]
        if sp == 0:
            continue
        roi = ret / sp * 100
        print(f"{bt:<22} {rc:>4} {hit:>4} {sp:>9.0f}円 {ret:>10.0f}円 {roi:>6.1f}%")


if __name__ == "__main__":
    main()
