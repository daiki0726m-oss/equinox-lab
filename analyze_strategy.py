#!/usr/bin/env python3
"""
信頼度×券種の最適戦略分析スクリプト
predictions_cache のデータを使って、どの信頼度のレースで
どの券種を買えば最も回収率が高いかを分析する
"""
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from database import init_db, get_db

JST = timezone(timedelta(hours=9))


def analyze_strategy():
    """信頼度×券種のバックテスト分析"""

    with get_db() as conn:
        # 予測キャッシュ + レース結果を取得
        caches = conn.execute("""
            SELECT pc.race_id, pc.confidence, pc.all_bets_json, pc.predictions_json,
                   pc.should_bet,
                   r.race_name, r.venue, r.race_number, r.race_date, r.grade
            FROM predictions_cache pc
            JOIN races r ON pc.race_id = r.race_id
            ORDER BY r.race_date, r.venue, r.race_number
        """).fetchall()

        if not caches:
            print("❌ predictions_cache にデータがありません")
            return

        # 信頼度×券種の集計
        stats = defaultdict(lambda: {
            "races": 0, "bets": 0, "invested": 0, "payout": 0,
            "hits": 0, "details": []
        })

        # 全体サマリ
        overall = defaultdict(lambda: {
            "races": 0, "bets": 0, "invested": 0, "payout": 0, "hits": 0
        })

        # ◎の着順統計
        honmei_stats = defaultdict(lambda: {
            "total": 0, "win": 0, "top2": 0, "top3": 0
        })

        total_races = 0
        races_with_results = 0

        for cache in caches:
            race_id = cache["race_id"]
            conf = cache["confidence"]
            preds = json.loads(cache["predictions_json"]) if cache["predictions_json"] else []
            all_bets = json.loads(cache["all_bets_json"]) if cache["all_bets_json"] else {}

            # 結果取得
            finishes = conn.execute("""
                SELECT horse_number, finish_position, odds
                FROM results WHERE race_id = ? AND finish_position > 0
                ORDER BY finish_position
            """, (race_id,)).fetchall()
            finish_map = {f["horse_number"]: f["finish_position"] for f in finishes}
            odds_map = {f["horse_number"]: f["odds"] for f in finishes}

            if not finish_map:
                total_races += 1
                continue

            total_races += 1
            races_with_results += 1

            # ◎の成績
            honmei = next((p for p in preds if p.get("mark") == "◎"), None)
            if honmei:
                hn = honmei["horse_number"]
                f = finish_map.get(hn, 99)
                honmei_stats[conf]["total"] += 1
                if f == 1: honmei_stats[conf]["win"] += 1
                if f <= 2: honmei_stats[conf]["top2"] += 1
                if f <= 3: honmei_stats[conf]["top3"] += 1

            # 配当取得
            payouts = conn.execute("""
                SELECT bet_type, combination, payout_amount
                FROM payouts WHERE race_id = ?
            """, (race_id,)).fetchall()
            payout_map = {}
            for p in payouts:
                key = (p["bet_type"], p["combination"])
                payout_map[key] = p["payout_amount"]

            # 各券種ごとに的中判定
            for bt, bt_bets in all_bets.items():
                if not bt_bets:
                    continue

                key = (conf, bt)
                stats[key]["races"] += 1
                overall[bt]["races"] += 1

                for b in bt_bets:
                    amount = b.get("amount", 100)
                    hns = b.get("horse_numbers", [])
                    detail_str = b.get("detail", b.get("bet_detail", ""))

                    stats[key]["bets"] += 1
                    stats[key]["invested"] += amount
                    overall[bt]["bets"] += 1
                    overall[bt]["invested"] += amount

                    is_hit = False
                    actual_payout = 0

                    # 的中判定
                    if bt == "単勝" and len(hns) >= 1:
                        if finish_map.get(hns[0], 99) == 1:
                            is_hit = True
                            actual_payout = payout_map.get(("単勝", str(hns[0])), 0)

                    elif bt == "複勝" and len(hns) >= 1:
                        if finish_map.get(hns[0], 99) <= 3:
                            is_hit = True
                            actual_payout = payout_map.get(("複勝", str(hns[0])), 0)

                    elif bt == "ワイド" and len(hns) >= 2:
                        combo = "-".join(str(h) for h in sorted(hns))
                        if all(finish_map.get(h, 99) <= 3 for h in hns):
                            is_hit = True
                            actual_payout = payout_map.get(("ワイド", combo), 0)

                    elif bt == "馬連" and len(hns) >= 2:
                        combo = "-".join(str(h) for h in sorted(hns))
                        top2 = sorted([h for h, f in finish_map.items() if f <= 2])
                        if sorted(hns) == top2:
                            is_hit = True
                            actual_payout = payout_map.get(("馬連", combo), 0)

                    elif bt == "三連複" and len(hns) >= 3:
                        combo = "-".join(str(h) for h in sorted(hns[:3]))
                        top3 = sorted([h for h, f in finish_map.items() if f <= 3])
                        if sorted(hns[:3]) == top3:
                            is_hit = True
                            actual_payout = payout_map.get(("三連複", combo), 0)

                    elif bt == "三連単" and len(hns) >= 3:
                        top3_ordered = [h for h, f in sorted(finish_map.items(), key=lambda x: x[1]) if f <= 3]
                        if hns[:3] == top3_ordered[:3]:
                            is_hit = True
                            combo = "→".join(str(h) for h in hns[:3])
                            actual_payout = payout_map.get(("三連単", combo), 0)

                    if is_hit and actual_payout > 0:
                        payout_val = int(actual_payout * (amount / 100))
                        stats[key]["payout"] += payout_val
                        stats[key]["hits"] += 1
                        overall[bt]["payout"] += payout_val
                        overall[bt]["hits"] += 1
                        stats[key]["details"].append({
                            "race": f'{cache["venue"]}{cache["race_number"]}R {cache["race_name"]}',
                            "type": bt,
                            "detail": detail_str,
                            "invested": amount,
                            "payout": payout_val,
                        })

        # ── 結果表示 ──
        print("=" * 70)
        print("  📊 信頼度×券種 バックテスト分析")
        print("=" * 70)
        print(f"  対象: {races_with_results}レース (全{total_races}レース中)")
        print()

        # ◎の着順統計
        print("─" * 70)
        print("  🏇 ◎(本命) の成績")
        print("─" * 70)
        print(f"  {'信頼度':>6} {'出走':>6} {'1着':>6} {'勝率':>8} {'2着内':>6} {'連対率':>8} {'3着内':>6} {'複勝率':>8}")
        print("  " + "-" * 62)

        all_total = all_win = all_top2 = all_top3 = 0
        for conf in ["S", "A", "B", "C", "D"]:
            s = honmei_stats.get(conf)
            if not s or s["total"] == 0:
                continue
            t, w, t2, t3 = s["total"], s["win"], s["top2"], s["top3"]
            all_total += t; all_win += w; all_top2 += t2; all_top3 += t3
            print(f"  {conf:>6} {t:>6} {w:>6} {w/t*100:>7.1f}% {t2:>6} {t2/t*100:>7.1f}% {t3:>6} {t3/t*100:>7.1f}%")
        if all_total > 0:
            print(f"  {'合計':>6} {all_total:>6} {all_win:>6} {all_win/all_total*100:>7.1f}% {all_top2:>6} {all_top2/all_total*100:>7.1f}% {all_top3:>6} {all_top3/all_total*100:>7.1f}%")

        # 信頼度×券種
        print()
        print("─" * 70)
        print("  💰 信頼度×券種 回収率マトリクス")
        print("─" * 70)
        print(f"  {'信頼度':>6} {'券種':>8} {'R数':>5} {'買目':>5} {'投資':>10} {'回収':>10} {'ROI':>8} {'的中':>5} {'的中率':>7}")
        print("  " + "-" * 68)

        # ROIランキング用
        roi_ranking = []

        for conf in ["S", "A", "B", "C", "D"]:
            for bt in ["単勝", "複勝", "ワイド", "馬連", "三連複", "三連単"]:
                key = (conf, bt)
                s = stats.get(key)
                if not s or s["invested"] == 0:
                    continue

                roi = s["payout"] / s["invested"] * 100 if s["invested"] > 0 else 0
                hit_rate = s["hits"] / s["bets"] * 100 if s["bets"] > 0 else 0
                marker = " 🔥" if roi >= 100 else " ⚠️" if roi < 50 else ""

                print(f"  {conf:>6} {bt:>8} {s['races']:>5} {s['bets']:>5} {s['invested']:>9,}円 {s['payout']:>9,}円 {roi:>7.1f}%{marker} {s['hits']:>5} {hit_rate:>6.1f}%")
                roi_ranking.append({
                    "conf": conf, "bt": bt, "roi": roi,
                    "invested": s["invested"], "payout": s["payout"],
                    "hits": s["hits"], "bets": s["bets"],
                    "hit_rate": hit_rate, "races": s["races"]
                })

        # 全体概要
        print()
        print("─" * 70)
        print("  📈 券種別の全体回収率")
        print("─" * 70)
        for bt in ["単勝", "複勝", "ワイド", "馬連", "三連複", "三連単"]:
            s = overall.get(bt)
            if not s or s["invested"] == 0:
                continue
            roi = s["payout"] / s["invested"] * 100
            hit_rate = s["hits"] / s["bets"] * 100 if s["bets"] > 0 else 0
            print(f"  {bt:>8}: ROI {roi:>7.1f}% | 的中率 {hit_rate:>6.1f}% | 投資 {s['invested']:>10,}円 → 回収 {s['payout']:>10,}円")

        # 最適戦略
        if roi_ranking:
            print()
            print("─" * 70)
            print("  🏆 ROI上位の最適戦略 (投資額1000円以上)")
            print("─" * 70)
            top = sorted([r for r in roi_ranking if r["invested"] >= 1000], key=lambda x: x["roi"], reverse=True)
            for i, r in enumerate(top[:10]):
                emoji = "🥇" if i == 0 else "🥈" if i == 1 else "🥉" if i == 2 else f"  {i+1}."
                print(f"  {emoji} [{r['conf']}] {r['bt']}: ROI {r['roi']:.1f}% (的中 {r['hits']}/{r['bets']} = {r['hit_rate']:.1f}%) 投資{r['invested']:,}円→回収{r['payout']:,}円")

        # 的中した具体例
        all_details = []
        for key, s in stats.items():
            all_details.extend(s["details"])
        if all_details:
            print()
            print("─" * 70)
            print("  🎯 的中レース一覧")
            print("─" * 70)
            top_hits = sorted(all_details, key=lambda x: x["payout"], reverse=True)
            for d in top_hits[:20]:
                roi = d["payout"] / d["invested"] * 100
                print(f"  {d['race']} | {d['type']} {d['detail']} | {d['invested']:,}→{d['payout']:,}円 (ROI {roi:.0f}%)")

        print()
        print("=" * 70)


if __name__ == "__main__":
    init_db()
    analyze_strategy()
