"""
馬券戦略エンジン v2
回収率最大化を目指す期待値ベース戦略
- 厳格なレース見送り判定
- EV上位馬券への集中投資
- ケリー基準 (ハーフケリー) で攻めた資金配分
"""

import math
from itertools import combinations


class BettingStrategy:
    """
    回収率最大化のための馬券戦略

    方針:
    1. 期待値 > 1.2 の馬券のみ購入（複勝は > 1.1）
    2. 信頼度が低いレースは積極的に見送り
    3. ケリー基準（ハーフケリー）で賭け金を算出
    4. EV上位の馬券に集中投資
    5. 回収率 > 的中率 を常に優先
    """

    MAX_BET_PER_RACE = 1000  # 1レースの上限(円)
    MIN_EV = 1.2             # 最低期待値
    MIN_BET = 100            # 最低賭け金(円)
    KELLY_FRACTION = 0.5     # ハーフケリー（旧0.25→攻めに変更）

    # 全馬券種の定義
    ALL_BET_TYPES = ["単勝", "複勝", "ワイド", "馬連", "三連複", "三連単"]

    def kelly_criterion(self, prob, odds):
        """ケリー基準で最適賭け比率を計算"""
        if prob <= 0 or odds <= 1:
            return 0
        b = odds - 1
        f = (prob * (b + 1) - 1) / b
        if f <= 0:
            return 0
        return f * self.KELLY_FRACTION

    def calculate_bet_amount(self, prob, odds, bankroll=None):
        """賭け金を計算（100円単位）"""
        budget = bankroll or self.MAX_BET_PER_RACE
        kelly = self.kelly_criterion(prob, odds)
        if kelly <= 0:
            return 0
        amount = budget * kelly
        amount = max(self.MIN_BET, math.floor(amount / 100) * 100)
        amount = min(amount, self.MAX_BET_PER_RACE)
        return int(amount)

    def should_bet_race(self, predictions):
        """
        レース見送り判定（厳格版）

        回収率を上げるため、以下のレースを見送る:
        - 上位馬が見当たらない（予測が拡散）
        - 最大勝率が低すぎる
        - 本命が堅すぎてオッズに妙味なし
        """
        if not predictions:
            return False, "予測データなし"

        top_prob = max(p["pred_win"] for p in predictions)
        sorted_preds = sorted(predictions, key=lambda x: x["pred_win"], reverse=True)

        # 最上位馬の勝率が10%未満 → 信頼度が低い
        if top_prob < 0.10:
            return False, f"予測確率が低い (最大{top_prob:.1%})"

        # 上位3頭の合計勝率が30%未満 → 分散しすぎ
        top3_sum = sum(p["pred_win"] for p in sorted_preds[:3])
        if top3_sum < 0.30:
            return False, f"上位3頭の合計勝率{top3_sum:.1%}で混戦"

        # 本命が堅すぎてオッズに旨味なし
        top_horse = sorted_preds[0]
        if top_prob > 0.6 and top_horse.get("odds_win", 1) < 1.5:
            return False, "本命が堅すぎてオッズに旨味なし"

        # 最大EVチェック（オッズがある場合）
        max_ev = 0
        for p in predictions:
            odds = p.get("odds_win", 0)
            if odds > 0:
                ev = p["pred_win"] * odds
                max_ev = max(max_ev, ev)
        if max_ev > 0 and max_ev < 0.9:
            return False, f"期待値が低い (最大EV: {max_ev:.2f})"

        return True, "OK"

    def generate_bets(self, predictions, bankroll=None, bet_types=None):
        """予測結果から推奨馬券を生成（回収率重視版）"""
        budget = bankroll or self.MAX_BET_PER_RACE
        enabled = set(bet_types) if bet_types else set(self.ALL_BET_TYPES)
        bets = []
        total_amount = 0

        # 勝率順でソート
        sorted_preds = sorted(predictions, key=lambda x: x["pred_win"], reverse=True)

        # 複勝率8%以上（÷3済み値で判定、実質24%以上）の上位馬
        top_horses = [p for p in sorted_preds if p["pred_top3"] >= 0.08][:4]

        # ── 1. 単勝（EVがプラスの馬）──
        if "単勝" in enabled:
            for p in sorted_preds:
                ev = p["pred_win"] * p["odds_win"]
                # EV >= 1.0 かつ オッズ >= 2.0 かつ 勝率 >= 8%
                if ev >= 1.0 and p["odds_win"] >= 2.0 and p["pred_win"] >= 0.08:
                    amount = self.calculate_bet_amount(p["pred_win"], p["odds_win"], budget)
                    if amount > 0 and total_amount + amount <= budget:
                        bets.append({
                            "type": "単勝",
                            "detail": f"{p['horse_number']}",
                            "horse_numbers": [p["horse_number"]],
                            "amount": amount,
                            "odds": p["odds_win"],
                            "ev": round(ev, 2),
                            "prob": round(p["pred_win"], 3),
                            "horse_name": p.get("horse_name", ""),
                        })
                        total_amount += amount

        # ── 2. 複勝（安定的に回収率を上げる柱）──
        if "複勝" in enabled:
            for p in sorted_preds:
                odds_place = p.get("odds_place", 1.5)
                ev = p["pred_top3"] * odds_place
                # 複勝率 >= 12% かつ EV >= 1.0
                if ev >= 1.0 and p["pred_top3"] >= 0.12:
                    amount = self.calculate_bet_amount(
                        p["pred_top3"], odds_place, budget - total_amount
                    )
                    if amount > 0 and total_amount + amount <= budget:
                        bets.append({
                            "type": "複勝",
                            "detail": f"{p['horse_number']}",
                            "horse_numbers": [p["horse_number"]],
                            "amount": amount,
                            "odds": odds_place,
                            "ev": round(ev, 2),
                            "prob": round(p["pred_top3"], 3),
                            "horse_name": p.get("horse_name", ""),
                        })
                        total_amount += amount

        # ── 3. ワイド ──
        if "ワイド" in enabled and len(top_horses) >= 2:
            for h1, h2 in combinations(top_horses[:3], 2):
                # ワイド確率: 両馬がtop3に入る確率（pred_top3は÷3済みなので×3で戻す）
                t3_1 = min(h1["pred_top3"] * 3, 0.9)
                t3_2 = min(h2["pred_top3"] * 3, 0.9)
                wide_prob = t3_1 * t3_2 * 0.8
                wide_prob = min(wide_prob, 0.5)
                wide_odds = max(
                    (h1.get("odds_win", 5) + h2.get("odds_win", 5)) * 0.3, 1.5
                )
                ev = wide_prob * wide_odds
                if ev >= 0.8:
                    amount = self.calculate_bet_amount(wide_prob, wide_odds, budget - total_amount)
                    if amount > 0 and total_amount + amount <= budget:
                        bets.append({
                            "type": "ワイド",
                            "detail": f"{h1['horse_number']}-{h2['horse_number']}",
                            "horse_numbers": [h1["horse_number"], h2["horse_number"]],
                            "amount": amount,
                            "odds": round(wide_odds, 1),
                            "ev": round(ev, 2),
                            "prob": round(wide_prob, 3),
                            "horse_name": f"{h1.get('horse_name', '')}-{h2.get('horse_name', '')}",
                        })
                        total_amount += amount

        # ── 4. 馬連 ──
        if "馬連" in enabled and len(top_horses) >= 2:
            for h1, h2 in combinations(top_horses[:3], 2):
                t3_1 = min(h1["pred_top3"] * 3, 0.9)
                t3_2 = min(h2["pred_top3"] * 3, 0.9)
                umaren_prob = (h1["pred_win"] * t3_2 +
                               h2["pred_win"] * t3_1) * 0.6
                umaren_odds = max(h1.get("odds_win", 5) * h2.get("odds_win", 5) * 0.4, 3.0)
                ev = umaren_prob * umaren_odds
                if ev >= 0.5:
                    amount = self.calculate_bet_amount(umaren_prob, umaren_odds, budget - total_amount)
                    if amount > 0 and total_amount + amount <= budget:
                        bets.append({
                            "type": "馬連",
                            "detail": f"{h1['horse_number']}-{h2['horse_number']}",
                            "horse_numbers": [h1["horse_number"], h2["horse_number"]],
                            "amount": amount,
                            "odds": round(umaren_odds, 1),
                            "ev": round(ev, 2),
                            "prob": round(umaren_prob, 3),
                            "horse_name": f"{h1.get('horse_name', '')}-{h2.get('horse_name', '')}",
                        })
                        total_amount += amount

        # ── 5. 三連複 ──
        if "三連複" in enabled and len(sorted_preds) >= 5 and len(top_horses) >= 2:
            dark_horses = [p for p in sorted_preds
                          if p["pred_top3"] >= 0.06
                          and p.get("odds_win", 1) >= 5][:3]
            for dh in dark_horses:
                for h1, h2 in combinations(top_horses[:2], 2):
                    t3_1 = min(h1["pred_top3"] * 3, 0.9)
                    t3_2 = min(h2["pred_top3"] * 3, 0.9)
                    t3_d = min(dh["pred_top3"] * 3, 0.9)
                    trio_prob = t3_1 * t3_2 * t3_d * 2
                    trio_prob = min(trio_prob, 0.3)
                    trio_odds = max(
                        h1.get("odds_win", 3) * h2.get("odds_win", 3) * dh.get("odds_win", 10) * 0.02,
                        5.0
                    )
                    ev = trio_prob * trio_odds
                    if ev >= 0.8:
                        amount = min(self.MIN_BET, budget - total_amount)
                        if amount >= self.MIN_BET and total_amount + amount <= budget:
                            bets.append({
                                "type": "三連複",
                                "detail": f"{h1['horse_number']}-{h2['horse_number']}-{dh['horse_number']}",
                                "horse_numbers": sorted([h1["horse_number"], h2["horse_number"], dh["horse_number"]]),
                                "amount": amount,
                                "odds": round(trio_odds, 1),
                                "ev": round(ev, 2),
                                "prob": round(trio_prob, 3),
                                "horse_name": f"{h1.get('horse_name', '')}-{h2.get('horse_name', '')}-{dh.get('horse_name', '')}",
                            })
                            total_amount += amount
                            break
                if total_amount >= budget:
                    break

        # ── 6. 三連単 (フォーメーション) ──
        if "三連単" in enabled and len(sorted_preds) >= 5:
            first_cands = sorted_preds[:2]
            second_cands = sorted_preds[:3]
            third_cands = sorted_preds[:5]

            sanrentan_bets = []
            for h1 in first_cands:
                for h2 in second_cands:
                    if h2["horse_number"] == h1["horse_number"]:
                        continue
                    for h3 in third_cands:
                        if h3["horse_number"] in (h1["horse_number"], h2["horse_number"]):
                            continue
                        prob = h1["pred_win"] * h2["pred_top3"] * h3["pred_top3"] * 0.5
                        prob = min(prob, 0.3)
                        odds = max(
                            h1.get("odds_win", 3) * h2.get("odds_win", 3) * h3.get("odds_win", 5) * 0.05,
                            10.0
                        )
                        ev = prob * odds
                        if ev >= self.MIN_EV * 2.0:
                            sanrentan_bets.append({
                                "type": "三連単",
                                "detail": f"{h1['horse_number']}→{h2['horse_number']}→{h3['horse_number']}",
                                "horse_numbers": [h1["horse_number"], h2["horse_number"], h3["horse_number"]],
                                "amount": self.MIN_BET,
                                "odds": round(odds, 1),
                                "ev": round(ev, 2),
                                "prob": round(prob, 3),
                                "horse_name": f"{h1.get('horse_name', '')}→{h2.get('horse_name', '')}→{h3.get('horse_name', '')}",
                            })
            sanrentan_bets.sort(key=lambda x: x["ev"], reverse=True)
            for bet in sanrentan_bets[:3]:
                if total_amount + bet["amount"] <= budget:
                    bets.append(bet)
                    total_amount += bet["amount"]

        # ── 券種別フォールバック（推奨が0の券種に確率ベースで追加）──
        if predictions and len(sorted_preds) >= 2:
            existing_types = {b["type"] for b in bets}
            top = sorted_preds[0]
            top2 = sorted_preds[1]
            top3 = sorted_preds[2] if len(sorted_preds) >= 3 else top2

            # 単勝フォールバック
            if "単勝" in enabled and "単勝" not in existing_types and top["pred_win"] >= 0.08:
                bets.append({
                    "type": "単勝", "detail": f"{top['horse_number']}",
                    "horse_numbers": [top["horse_number"]],
                    "amount": min(300, budget - total_amount),
                    "odds": top.get("odds_win", 3.0),
                    "ev": round(top["pred_win"] * top.get("odds_win", 3.0), 2),
                    "prob": round(top["pred_win"], 3),
                    "horse_name": top.get("horse_name", ""),
                })
                total_amount += bets[-1]["amount"]

            # 複勝フォールバック
            if "複勝" in enabled and "複勝" not in existing_types:
                for p in sorted_preds[:2]:
                    remaining = budget - total_amount
                    if remaining >= 100 and p["pred_top3"] >= 0.05:
                        odds_place = p.get("odds_place", 1.5)
                        bets.append({
                            "type": "複勝", "detail": f"{p['horse_number']}",
                            "horse_numbers": [p["horse_number"]],
                            "amount": min(200, remaining),
                            "odds": odds_place,
                            "ev": round(min(p["pred_top3"] * 3, 0.9) * odds_place, 2),
                            "prob": round(p["pred_top3"], 3),
                            "horse_name": p.get("horse_name", ""),
                        })
                        total_amount += bets[-1]["amount"]

            # ワイドフォールバック（上位2頭の組み合わせ）
            if "ワイド" in enabled and "ワイド" not in existing_types:
                t3_1 = min(top["pred_top3"] * 3, 0.9)
                t3_2 = min(top2["pred_top3"] * 3, 0.9)
                wide_odds = max((top.get("odds_win", 5) + top2.get("odds_win", 5)) * 0.3, 1.5)
                remaining = budget - total_amount
                if remaining >= 100:
                    bets.append({
                        "type": "ワイド",
                        "detail": f"{top['horse_number']}-{top2['horse_number']}",
                        "horse_numbers": [top["horse_number"], top2["horse_number"]],
                        "amount": min(200, remaining),
                        "odds": round(wide_odds, 1),
                        "ev": round(t3_1 * t3_2 * 0.8 * wide_odds, 2),
                        "prob": round(t3_1 * t3_2 * 0.8, 3),
                        "horse_name": f"{top.get('horse_name', '')}-{top2.get('horse_name', '')}",
                    })
                    total_amount += bets[-1]["amount"]

            # 馬連フォールバック
            if "馬連" in enabled and "馬連" not in existing_types:
                t3_1 = min(top["pred_top3"] * 3, 0.9)
                t3_2 = min(top2["pred_top3"] * 3, 0.9)
                umaren_odds = max(top.get("odds_win", 5) * top2.get("odds_win", 5) * 0.4, 3.0)
                umaren_prob = (top["pred_win"] * t3_2 + top2["pred_win"] * t3_1) * 0.6
                remaining = budget - total_amount
                if remaining >= 100:
                    bets.append({
                        "type": "馬連",
                        "detail": f"{top['horse_number']}-{top2['horse_number']}",
                        "horse_numbers": [top["horse_number"], top2["horse_number"]],
                        "amount": min(200, remaining),
                        "odds": round(umaren_odds, 1),
                        "ev": round(umaren_prob * umaren_odds, 2),
                        "prob": round(umaren_prob, 3),
                        "horse_name": f"{top.get('horse_name', '')}-{top2.get('horse_name', '')}",
                    })
                    total_amount += bets[-1]["amount"]

            # 三連複フォールバック（上位3頭）
            if "三連複" in enabled and "三連複" not in existing_types and len(sorted_preds) >= 3:
                t3_1 = min(top["pred_top3"] * 3, 0.9)
                t3_2 = min(top2["pred_top3"] * 3, 0.9)
                t3_3 = min(top3["pred_top3"] * 3, 0.9)
                trio_odds = max(top.get("odds_win", 3) * top2.get("odds_win", 3) * top3.get("odds_win", 5) * 0.03, 5.0)
                trio_prob = t3_1 * t3_2 * t3_3 * 2
                remaining = budget - total_amount
                if remaining >= 100:
                    bets.append({
                        "type": "三連複",
                        "detail": f"{top['horse_number']}-{top2['horse_number']}-{top3['horse_number']}",
                        "horse_numbers": sorted([top["horse_number"], top2["horse_number"], top3["horse_number"]]),
                        "amount": min(100, remaining),
                        "odds": round(trio_odds, 1),
                        "ev": round(trio_prob * trio_odds, 2),
                        "prob": round(trio_prob, 3),
                        "horse_name": f"{top.get('horse_name', '')}-{top2.get('horse_name', '')}-{top3.get('horse_name', '')}",
                    })
                    total_amount += bets[-1]["amount"]

            # 三連単フォールバック（1位→2位→3位）
            if "三連単" in enabled and "三連単" not in existing_types and len(sorted_preds) >= 3:
                stan_prob = top["pred_win"] * top2["pred_top3"] * 3 * top3["pred_top3"] * 3 * 0.3
                stan_odds = max(top.get("odds_win", 3) * top2.get("odds_win", 3) * top3.get("odds_win", 5) * 0.08, 10.0)
                remaining = budget - total_amount
                if remaining >= 100:
                    bets.append({
                        "type": "三連単",
                        "detail": f"{top['horse_number']}→{top2['horse_number']}→{top3['horse_number']}",
                        "horse_numbers": [top["horse_number"], top2["horse_number"], top3["horse_number"]],
                        "amount": min(100, remaining),
                        "odds": round(stan_odds, 1),
                        "ev": round(stan_prob * stan_odds, 2),
                        "prob": round(stan_prob, 3),
                        "horse_name": f"{top.get('horse_name', '')}→{top2.get('horse_name', '')}→{top3.get('horse_name', '')}",
                    })
                    total_amount += bets[-1]["amount"]

        # EV順にソート
        bets.sort(key=lambda x: x["ev"], reverse=True)

        return {
            "bets": bets,
            "total_amount": total_amount,
            "budget": budget,
            "remaining": budget - total_amount,
            "bet_count": len(bets),
        }

    def format_recommendation(self, bets_result, race_info=None):
        """推奨馬券を整形して出力"""
        lines = []

        if race_info:
            lines.append(f"{'='*50}")
            lines.append(f"🏇 {race_info.get('race_name', '')} ({race_info.get('venue', '')} {race_info.get('race_number', '')}R)")
            lines.append(f"   {race_info.get('surface', '')} {race_info.get('distance', '')}m / {race_info.get('track_condition', '')}")
            lines.append(f"{'='*50}")

        lines.append(f"\n💰 予算: ¥{bets_result['budget']:,} / 合計: ¥{bets_result['total_amount']:,}")
        lines.append(f"📋 買い目: {bets_result['bet_count']}点\n")

        for i, bet in enumerate(bets_result["bets"], 1):
            ev_emoji = "🔥" if bet["ev"] >= 2.0 else "⭐" if bet["ev"] >= 1.5 else "✅"
            lines.append(
                f"  {ev_emoji} {i}. 【{bet['type']}】{bet['detail']} "
                f"¥{bet['amount']:,} (EV:{bet['ev']:.2f} | "
                f"確率:{bet['prob']:.1%} | オッズ:{bet['odds']})"
            )
            if bet.get("horse_name"):
                lines.append(f"     ({bet['horse_name']})")

        if not bets_result["bets"]:
            lines.append("  ❌ このレースは見送りが推奨されます")

        return "\n".join(lines)
