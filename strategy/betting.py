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

    # 全券種を有効化
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

    def should_bet_race(self, predictions, confidence=None):
        """
        レース見送り判定（厳格版）

        回収率を上げるため、以下のレースを見送る:
        - 上位馬が見当たらない（予測が拡散）
        - 最大勝率が低すぎる
        - 本命が堅すぎてオッズに妙味なし
        """
        if not predictions:
            return False, "予測データなし"

        # ❌ v12 (2026-05-26 ROI最大化): 信頼度 C/D は明示的に見送り
        # 5/9-5/24 backtest: C 三連複 ROI 75% / 馬連 45% / ワイド 80%
        # confidence が明示できる場合は暗黙的判定でなく明示的にスキップ
        if confidence in ("C", "D"):
            return False, f"信頼度{confidence}は損失層 (backtest ROI 75-80%)"

        top_prob = max(p["pred_win"] for p in predictions)
        sorted_preds = sorted(predictions, key=lambda x: x["pred_win"], reverse=True)

        # 最上位馬の勝率が12%未満 → 信頼度が低い
        # v11 (2026-05-24): 8% → 12% に厳格化。閾値感度分析で
        # ≥12%/≥30% にすると推奨レース ROI 132% → 141% に改善が判明。
        # 印・買い目は常に出すが、推奨フラグだけ厳しくして「見送り」表示。
        # UI 上で「✅ 推奨 / ⏭️ 見送り」バッジを通じて投資判断を補助。
        if top_prob < 0.12:
            return False, f"予測確率が低い (最大{top_prob:.1%})"

        # 上位3頭の合計勝率が30%未満 → 分散しすぎ
        # v11 (2026-05-24): 23% → 30% に厳格化。
        # 「推奨だけ買えば ROI 141%、全レースだと 132%」の感度分析結果から。
        # 印付け・買い目生成自体は動く設計で、ここは「推奨判定」のみ厳格化。
        top3_sum = sum(p["pred_win"] for p in sorted_preds[:3])
        if top3_sum < 0.30:
            return False, f"上位3頭の合計勝率{top3_sum:.1%}で混戦"

        # 本命が堅すぎてオッズに旨味なし
        top_horse = sorted_preds[0]
        top_odds = top_horse.get("odds_win", 1) or 1
        if top_prob > 0.6 and top_odds < 1.5:
            return False, "本命が堅すぎてオッズに旨味なし"

        # 🆕 v12 (ROI最大化施策): ◎の単勝オッズ妙味バンド外は見送り
        # confidence v4 で odds_pot は評価軸だが、should_bet にも明示的に反映:
        # 2.0倍未満 = 配当低すぎてROI出ない、15倍超 = ◎自体が信頼度低い大穴
        if top_odds > 0:
            if top_odds < 2.0:
                return False, f"◎オッズ{top_odds:.1f}倍は配当妙味なし"
            if top_odds > 15.0:
                return False, f"◎オッズ{top_odds:.1f}倍は◎信頼度が低すぎ"

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

    def _honor_bets(self, sorted_preds, enabled, budget, predictions=None, line_amount=None):
        """印通り保証買い目を生成 — v3 (2026-05-27 confidence-aware)

        3-5月 7000R バックテストで判明した「最強の買い方」を主力化:
          - ◎軸三連複 5頭流し (10点) ROI 174%
          - 馬連 ◎-○▲△× 流し (4点) ROI 207%
          - ワイド ◎-○▲△× 流し (4点) ROI 150%

        旧 honor の問題点:
          - ◎単勝 ROI 86% (微マイナス)、◎複勝 ROI 94% (微マイナス)
          - ◎○ペア1点のみ → 相手当たっても外す
        新 honor の効果: ROI 100% 以上が見込める主力買い目を必ず含める。

        相手の選定:
          predictions から mark=○▲△×注 の馬を取得して使用。
          predictions が無い場合は sorted_preds[1:6] で代替。

        v3 (2026-05-27): confidence-aware weighting
          line_amount を指定可能 (S=150円 / A=120円 / B=100円 等)。
          指定が無ければ MIN_BET (100円) を使用。
        """
        bets = []
        if len(sorted_preds) < 1:
            return bets, 0

        amt = line_amount or self.MIN_BET
        spent = 0
        signatures = set()

        def add(t, detail, hns, odds, prob, name):
            nonlocal spent
            sig = (t, tuple(sorted(hns)))
            if sig in signatures:
                return
            if spent + amt > budget:
                return
            signatures.add(sig)
            spent += amt
            bets.append({
                "type": t, "detail": detail, "horse_numbers": hns,
                "amount": amt, "odds": round(odds, 1),
                "ev": round(prob * odds, 2), "prob": round(prob, 3),
                "horse_name": name, "honor": True,
            })

        p1 = sorted_preds[0]  # ◎
        center = p1["horse_number"]
        center_name = p1.get("horse_name", "")

        # 相手 (○▲△×注) の取得 — mark フィールド優先、なければ ML 順
        partners = []
        if predictions:
            mark_priority = {'○': 1, '▲': 2, '△': 3, '×': 4, '注': 5}
            partner_candidates = [
                (mark_priority.get(p.get('mark',''), 99), p)
                for p in predictions
                if p.get('mark') in ('○','▲','△','×','注')
            ]
            partner_candidates.sort(key=lambda x: x[0])
            partners = [p for _, p in partner_candidates][:5]
        if len(partners) < 2:
            # フォールバック: ML 順 上位5
            partners = sorted_preds[1:6]

        # 優先順は ROI 最大化観点で「馬連 → ワイド → 三連複」
        # データから: 馬連207% > 三連複174% > ワイド150% だが、
        # 馬連・ワイドは1人気軸で的中率が高くROIが安定 → 先に確保

        # ─── 主力1: 馬連 ◎-相手 流し (5点) ROI 207% ───
        if "馬連" in enabled:
            for partner in partners[:5]:
                nums = sorted([center, partner["horse_number"]])
                ow1 = p1.get("odds_win", 3) or 3
                ow2 = partner.get("odds_win", 5) or 5
                est_odds = max(3.0, ow1 * ow2 * 0.45)
                est_prob = (p1.get("pred_win", 0) + partner.get("pred_win", 0)) * 0.4
                names = f"{center_name}-{partner.get('horse_name','')}"
                add("馬連", f"{nums[0]}-{nums[1]}", nums, est_odds, est_prob, names)

        # ─── 主力2: ワイド ◎-相手 流し (5点) ROI 150% ───
        if "ワイド" in enabled:
            for partner in partners[:5]:
                nums = sorted([center, partner["horse_number"]])
                ow1 = p1.get("odds_win", 3) or 3
                ow2 = partner.get("odds_win", 5) or 5
                est_odds = max(1.5, (ow1 + ow2) * 0.25)
                est_prob = min(p1.get("pred_top3", 0.2) * 3, 0.85) * \
                           min(partner.get("pred_top3", 0.15) * 3, 0.7) * 0.85
                names = f"{center_name}-{partner.get('horse_name','')}"
                add("ワイド", f"{nums[0]}-{nums[1]}", nums, est_odds, est_prob, names)

        # ─── 主力3: ◎軸三連複 5頭流し (5頭から2頭 = 10点) ROI 174% ───
        if "三連複" in enabled and len(partners) >= 2:
            from itertools import combinations
            for pair in combinations(partners[:5], 2):
                nums = sorted([center, pair[0]["horse_number"], pair[1]["horse_number"]])
                ow1 = p1.get("odds_win", 3) or 3
                ow2 = pair[0].get("odds_win", 5) or 5
                ow3 = pair[1].get("odds_win", 8) or 8
                est_odds = max(8.0, ow1 * ow2 * ow3 * 0.06)
                est_prob = min(p1.get("pred_top3", 0.2) * 3, 0.85) * \
                           min(pair[0].get("pred_top3", 0.15) * 3, 0.7) * \
                           min(pair[1].get("pred_top3", 0.1) * 3, 0.55) * 0.5
                est_prob = min(est_prob, 0.25)
                names = f"{center_name}-{pair[0].get('horse_name','')}-{pair[1].get('horse_name','')}"
                add("三連複", f"{nums[0]}-{nums[1]}-{nums[2]}", nums, est_odds, est_prob, names)

        return bets, spent

    # v3 (2026-05-27): confidence 別 bet 額ウェイト
    # S/A はマイナス層 (#26 単日支配バイアス問題) でも相対的にマシ。
    # 「線数」(=信頼に応じた合計投資額) と「線単価」(=確信度に応じた1点額) の両方をスケール:
    #   - budget スケール: 高信頼ほど合計投資額を増やして of  カバー率拡大
    #   - line_amount スケール: 高信頼ほど 1点 200円 にして payout を大きく
    # C/D は should_bet=False で既に遮断済。
    CONFIDENCE_MULTIPLIER = {
        'S': 2.0,  # 高信頼 → budget 2000円 / 1点 200円
        'A': 1.5,  # 上位   → budget 1500円 / 1点 100円 (端数切り捨て)
        'B': 1.0,  # 標準   → budget 1000円 / 1点 100円
        'C': 0.0,  # 念のため (実運用では should_bet=False で遮断)
        'D': 0.0,
    }

    def generate_bets(self, predictions, bankroll=None, bet_types=None, confidence=None):
        """予測結果から推奨馬券を生成（回収率重視版 + honor bets + confidence-aware）"""
        # confidence ウェイトで budget と line_amount をスケール
        mult = self.CONFIDENCE_MULTIPLIER.get(confidence, 1.0) if confidence else 1.0
        if mult == 0.0:
            # C/D 等 (理論上 should_bet=False で遮断されてるはず)
            return {"bets": [], "total_amount": 0, "race_info": {}, "skipped": True}
        budget = int((bankroll or self.MAX_BET_PER_RACE) * mult)
        # line_amount: mult を 100円単位に丸める (S=200, A/B=100)
        line_amount = max(self.MIN_BET, int(self.MIN_BET * mult / 100) * 100)
        enabled = set(bet_types) if bet_types else set(self.ALL_BET_TYPES)

        # 勝率順でソート
        sorted_preds = sorted(predictions, key=lambda x: x["pred_win"], reverse=True)

        # ── 0. 印通り保証買い目 (EV 関係なく必ず含める) ──
        # v2 (2026-05-24): バックテスト結果から「最強の買い目」を主力化:
        #   - ◎軸三連複5頭流し (10点) ROI 174%
        #   - 馬連 ◎-相手5頭 流し (5点) ROI 207%
        #   - ワイド ◎-相手5頭 流し (5点) ROI 150%
        # 旧 honor (◎単勝・◎複勝) は ROI 90%前後で実質マイナスのため廃止。
        # v3 (2026-05-27): line_amount を confidence で重み付け
        honor_list, honor_spent = self._honor_bets(sorted_preds, enabled, budget,
                                                    predictions=predictions,
                                                    line_amount=line_amount)
        bets = list(honor_list)
        total_amount = honor_spent
        # honor で既に bet した signature を以後の EV bets で重複させない
        honor_signatures = {(b["type"], tuple(sorted(b["horse_numbers"]))) for b in honor_list}

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
            top_horse_nums = {h["horse_number"] for h in top_horses[:2]}
            dark_horses = [p for p in sorted_preds
                          if p["pred_top3"] >= 0.06
                          and p.get("odds_win", 1) >= 5
                          and p["horse_number"] not in top_horse_nums][:3]
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
                            h1.get("odds_win", 3) * h2.get("odds_win", 3) * h3.get("odds_win", 5) * 0.3,
                            30.0
                        )
                        ev = prob * odds
                        if ev >= 0.3:
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

            # 三連複フォールバック（上位3頭、馬番重複チェック付き）
            if "三連複" in enabled and "三連複" not in existing_types and len(sorted_preds) >= 3:
                # top3が重複しないように選ぶ
                trio_cands = [top, top2]
                for p in sorted_preds[2:]:
                    if p["horse_number"] not in (top["horse_number"], top2["horse_number"]):
                        trio_cands.append(p)
                        break
                if len(trio_cands) == 3:
                    t3_1 = min(trio_cands[0]["pred_top3"] * 3, 0.9)
                    t3_2 = min(trio_cands[1]["pred_top3"] * 3, 0.9)
                    t3_3 = min(trio_cands[2]["pred_top3"] * 3, 0.9)
                    trio_odds = max(trio_cands[0].get("odds_win", 3) * trio_cands[1].get("odds_win", 3) * trio_cands[2].get("odds_win", 5) * 0.03, 5.0)
                    trio_prob = t3_1 * t3_2 * t3_3 * 2
                    remaining = budget - total_amount
                    if remaining >= 100:
                        bets.append({
                            "type": "三連複",
                            "detail": f"{trio_cands[0]['horse_number']}-{trio_cands[1]['horse_number']}-{trio_cands[2]['horse_number']}",
                            "horse_numbers": sorted([trio_cands[0]["horse_number"], trio_cands[1]["horse_number"], trio_cands[2]["horse_number"]]),
                            "amount": min(100, remaining),
                            "odds": round(trio_odds, 1),
                            "ev": round(trio_prob * trio_odds, 2),
                            "prob": round(trio_prob, 3),
                            "horse_name": f"{trio_cands[0].get('horse_name', '')}-{trio_cands[1].get('horse_name', '')}-{trio_cands[2].get('horse_name', '')}",
                        })
                        total_amount += bets[-1]["amount"]

            # 三連単フォールバック（1位→2位→3位、馬番重複チェック付き）
            if "三連単" in enabled and "三連単" not in existing_types and len(sorted_preds) >= 3:
                stan_cands = [top, top2]
                for p in sorted_preds[2:]:
                    if p["horse_number"] not in (top["horse_number"], top2["horse_number"]):
                        stan_cands.append(p)
                        break
                if len(stan_cands) == 3:
                    stan_prob = stan_cands[0]["pred_win"] * stan_cands[1]["pred_top3"] * 3 * stan_cands[2]["pred_top3"] * 3 * 0.3
                    stan_odds = max(stan_cands[0].get("odds_win", 3) * stan_cands[1].get("odds_win", 3) * stan_cands[2].get("odds_win", 5) * 0.5, 30.0)
                    remaining = budget - total_amount
                    if remaining >= 100:
                        bets.append({
                            "type": "三連単",
                            "detail": f"{stan_cands[0]['horse_number']}→{stan_cands[1]['horse_number']}→{stan_cands[2]['horse_number']}",
                            "horse_numbers": [stan_cands[0]["horse_number"], stan_cands[1]["horse_number"], stan_cands[2]["horse_number"]],
                            "amount": min(100, remaining),
                            "odds": round(stan_odds, 1),
                            "ev": round(stan_prob * stan_odds, 2),
                            "prob": round(stan_prob, 3),
                            "horse_name": f"{stan_cands[0].get('horse_name', '')}→{stan_cands[1].get('horse_name', '')}→{stan_cands[2].get('horse_name', '')}",
                        })
                        total_amount += bets[-1]["amount"]

        # ── 重複除去 (honor bet と同じ組合せの EV bet は除外) ──
        # honor bet を優先的に残し、EV bet で同じ印通り組合せを重複させない。
        deduped = []
        seen = set()
        for b in bets:
            sig = (b["type"], tuple(sorted(b.get("horse_numbers", []))))
            if sig in seen:
                # 重複 → honor を残すために、後出を捨てる
                continue
            seen.add(sig)
            deduped.append(b)
        bets = deduped

        # ── ソート: honor bet を上に、その下を EV 降順 ──
        bets.sort(key=lambda x: (-1 if x.get("honor") else 0, -x["ev"]))

        # total_amount を実態(deduped 後)に再計算
        total_amount = sum(b["amount"] for b in bets)

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
