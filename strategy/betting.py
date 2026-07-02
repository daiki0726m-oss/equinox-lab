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
    馬券戦略 (2026-05-27 entertainment モード)

    方針:
    1. 期待値 > MIN_EV (=0.8、夢ロマンモード) の馬券を購入。買い目は多めに提示
    2. 信頼度が低いレースは should_bet=0 (見送りバッジ)、ただし印・買い目は出す
    3. ケリー基準（ハーフケリー）で賭け金を算出
    4. honor_bets (印通り買い目) は EV 制限なしで全提示
    5. USE_WHITELIST=1 で投資特化モード (S×2勝中距離 / 函館ダート 単勝のみ)
    """

    MAX_BET_PER_RACE = 1000  # 1レースの上限(円)
    MIN_EV = 0.8             # 最低期待値 (2026-05-27: 1.2 → 0.8 entertainment モード)
                             # 旧 1.2 は 100% 超 ROI 投資ガチ運用向け。0.8 は「夢とロマン」
                             # スタンスで買い目多めに提示する設定。X 投稿には買い目 detail
                             # は出ないので、内部のみ影響 (entertainment 寄りに調整)。
    # ── MAX_EV band-pass (#95 2026-07-02 ROI監査) ──
    # 「宣言 EV が高いほど実現 ROI が低い」逆相関を実測 (5-6月両月再現):
    #   EV>=2.0 line の実 ROI = 5月 23.8% / 6月 16.4% (確率過大推定の穴馬に偏る)
    #   EV<2.0 に band-pass するだけで全体 ROI +12〜13pt。
    # EV フィルタ買い目は MIN_EV <= ev < MAX_EV のみ通す (honor bets は対象外)。
    MAX_EV = 2.0
    MIN_BET = 100            # 最低賭け金(円)
    KELLY_FRACTION = 0.5     # ハーフケリー（旧0.25→攻めに変更）

    # 全券種を有効化
    ALL_BET_TYPES = ["単勝", "複勝", "ワイド", "馬連", "三連複", "三連単"]

    # ── Phase α (2026-05-27): Whitelist specialization ──
    # 6 年 walk-forward 検証で「全 6 年 ROI ≥ 100%」を維持できた segment は
    # 存在しなかったが、5/6 年以上で安定して 100%+ の 2 segments のみ実用化:
    #   1. 単勝 × confidence=S × 2勝クラス × 中距離 (avg ROI 117.5%)
    #   2. 単勝 × 函館 × ダート (avg ROI 106.5%)
    # WHITELIST_MODE=True で「これ以外の race は全 skip」する specialization 戦略。
    #
    # Format: (segment_name, predicate(race_info, confidence) → bool, allowed_bet_types)
    WHITELIST_SEGMENTS = [
        (
            "S/2勝/中距離 単勝",
            lambda ri, conf: (
                conf == "S"
                and "2勝" in (ri.get("race_name", "") or "")
                and 1800 <= (ri.get("distance", 0) or 0) <= 2199
            ),
            ["単勝"],
        ),
        (
            "函館/ダート 単勝",
            lambda ri, conf: (
                (ri.get("venue", "") or "") == "函館"
                and (ri.get("surface", "") or "") == "ダート"
            ),
            ["単勝"],
        ),
    ]

    def get_whitelist_match(self, race_info, confidence):
        """Whitelist segments のうち該当するものを返す (なければ None)。"""
        if not race_info:
            return None
        for seg_name, pred, bet_types in self.WHITELIST_SEGMENTS:
            try:
                if pred(race_info, confidence):
                    return (seg_name, bet_types)
            except Exception:
                continue
        return None

    @staticmethod
    def get_axis_horse(predictions, sorted_preds=None):
        """買い目の軸馬 = 表示上の ◎ (mark)。無ければ pred_win 1位にフォールバック。

        #95 (2026-07-02 ROI監査): 旧実装は軸 = pred_win 1位で、印 v5 の ◎
        (pred_top3 1位) と乖離し、◎相当馬が相手プールに混ざって「馬連 7-7」等の
        同一馬ペア無効買い目が本番 cache に混入していた。軸は常に表示 ◎ と一致させる。
        """
        for p in predictions or []:
            if p.get('mark') == '◎':
                return p
        if sorted_preds:
            return sorted_preds[0]
        if predictions:
            return max(predictions, key=lambda x: x.get('pred_win', 0))
        return None

    def trio_focus_band(self, predictions, confidence, race_info=None):
        """trio-focus band 判定 (#95 2026-07-02 ROI監査)。

        ◎ 単勝オッズ 2.0-2.9倍 × confidence S/A/B は、三連複◎軸ながし (印相手) が
        ROI 200% (n=100、5月182%/6月205%、単日除外でも161-163%) の検証済み利益層。
        ワイド◎ながしも 119%。一方この帯の馬連◎ながしは 83% の損失層。
        → 該当時: 馬連 honor を生成せず、三連複 honor を2倍額に、ワイドは維持。
        実オッズがある場合のみ発火 (推定オッズは自己予測由来で循環参照になるため)。
        この alpha は印相手 (穴馬スコア系) に由来 — 相手を人気順に替えると 200%→108%
        に消えることを counterfactual で確認済み。印ロジック変更時は要再検証。

        ★逆張りレビュー指摘 (#95 追補): band は should_bet=0 でも投資対象として残る
        例外経路なので、should_bet_race の損失層ガード (未勝利/新馬 #28、障害 ML圏外)
        を race_name で自前に再チェックする。これが無いと「4歳以上障害未勝利 ◎2.5倍
        conf B」のような ML 圏外レースに trio 2倍額が残る (band 候補の47%が該当した)。
        """
        if confidence not in ("S", "A", "B"):
            return False
        race_name = (race_info or {}).get("race_name", "") if race_info else ""
        if race_name and any(k in race_name for k in ("未勝利", "新馬", "障害", "ジャンプ")):
            return False
        axis = self.get_axis_horse(predictions)
        if not axis or not axis.get('_has_real_odds'):
            return False
        odds = axis.get('odds_win') or 0
        return 2.0 <= odds < 3.0

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

    def should_bet_race(self, predictions, confidence=None, race_info=None, strict_whitelist=False):
        """
        レース見送り判定（厳格版）

        回収率を上げるため、以下のレースを見送る:
        - 上位馬が見当たらない（予測が拡散）
        - 最大勝率が低すぎる
        - 本命が堅すぎてオッズに妙味なし
        - 1勝戦×妙味中間オッズ (2.5-3.9倍) は loss layer (2026-05-27 #30)

        strict_whitelist=True の場合: WHITELIST_SEGMENTS に該当しない race を全 skip。
            walk-forward で 100%+ 確認できた niche segments のみで投資する specialization 戦略。
        """
        if not predictions:
            return False, "予測データなし"

        # Phase α (2026-05-27): Whitelist specialization mode
        # 6 年 walk-forward で 5/6 年以上 100%+ の 2 segments だけ通過させる
        if strict_whitelist:
            match = self.get_whitelist_match(race_info, confidence)
            if not match:
                return False, "Whitelist 該当なし (specialization mode)"
            # match した場合は他の判定を skip して直接 OK
            return True, f"Whitelist hit: {match[0]}"

        # ❌ v12 (2026-05-26 ROI最大化): 信頼度 C/D は明示的に見送り
        # 5/9-5/24 backtest: C 三連複 ROI 75% / 馬連 45% / ワイド 80%
        # confidence が明示できる場合は暗黙的判定でなく明示的にスキップ
        if confidence in ("C", "D"):
            return False, f"信頼度{confidence}は損失層 (backtest ROI 75-80%)"

        # 🆕 2026-05-30: 未勝利・新馬は should_bet で見送り (旧は confidence 降格で実現)
        # #28: ML が unknown horse を学習できず未勝利戦の予測 ROI が不安定。
        # confidence は予測自信度 (◎勝率) のまま、投資回避はここで明示。
        race_name = (race_info or {}).get("race_name", "") if race_info else ""
        if race_name and ("未勝利" in race_name or "新馬" in race_name):
            return False, "未勝利・新馬は ML 信頼度低のため投資見送り (印・予想は表示)"

        # 🆕 #95 (2026-07-02 ROI監査): 障害・ジャンプレースは投資見送り
        # ML は平地レースで学習しており障害は学習データ外。6月に障害レース
        # (京都ハイジャンプ等) へ S が付与され S=2.0x で最大額が張られた結果、
        # S 層全体の ROI が 44.6% に汚染された (S<B<A の序列逆転 #10 の再来)。
        if race_name and ("障害" in race_name or "ジャンプ" in race_name):
            return False, "障害レースは ML 学習データ外のため投資見送り (印・予想は表示)"

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
        # #95 (2026-07-02): オッズ帯の判定は「表示上の ◎」(mark) で行う。
        # 旧実装は pred_win 1位で判定しており、印 v5 の ◎ (pred_top3 1位) と乖離していた。
        top_horse = self.get_axis_horse(predictions, sorted_preds) or sorted_preds[0]
        top_odds = top_horse.get("odds_win", 1) or 1
        if top_prob > 0.6 and top_odds < 1.5:
            return False, "本命が堅すぎてオッズに旨味なし"

        # 🆕 v12 (ROI最大化施策): ◎の単勝オッズ妙味バンド外は見送り
        # #95 (2026-07-02): 上限 30.0 → 8.0 に引き締め。
        # 実測 (5/10-6/28): ◎8.0倍以上 (n=17) は単勝/馬連/ワイド/三連複の全券種で
        # ROI 0% (的中ゼロが2ヶ月連続)。5.0-7.9倍帯は confidence -1 降格 (predict.py 側)。
        # ★オッズ帯チェックは実オッズがある場合のみ (#95 レビュー追補)。
        # 推定オッズ = 0.8/pred_win は温度1で本命≈8.4倍となり、木金の事前予測が
        # 一律「8倍以上」で誤遮断される (自己予測由来の循環参照)。実オッズ確定後の
        # 土曜朝 predict で正しく再判定される。
        axis_has_real_odds = bool(top_horse.get("_has_real_odds"))
        if top_odds > 0 and axis_has_real_odds:
            if top_odds < 2.0:
                return False, f"◎オッズ{top_odds:.1f}倍は配当妙味なし"
            if top_odds >= 8.0:
                return False, f"◎オッズ{top_odds:.1f}倍は◎信頼度が低すぎ (8倍以上は全券種ROI 0%)"

        # 🆕 v13 (2026-05-27 #30): 1勝戦の妙味中間オッズ帯は loss layer
        # analyze_a_tier_loss.py の結果: 1勝×A の ◎ オッズ別 ROI:
        #   1.x-2.4倍: 28.3% (n=102) / 2.5-3.9倍: 11.5% (n=26) ← 最底
        # 「やや本命だが堅くもない」中間帯が ROI 最悪。1勝戦で 2.5-3.9倍は見送り。
        race_name = (race_info or {}).get("race_name", "") if race_info else ""
        if race_name and "1勝" in race_name and axis_has_real_odds:
            if 2.5 <= top_odds < 4.0:
                return False, f"1勝戦×◎オッズ{top_odds:.1f}倍は妙味中間帯 (loss layer 11.5%)"

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

    def _honor_bets(self, sorted_preds, enabled, budget, predictions=None, line_amount=None,
                    trio_focus=False):
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

        if trio_focus:
            # #95 trio-focus band: 三連複◎軸 10点を2倍額で全点確保 + ワイド維持。
            # 馬連はこの帯で ROI 83% のため生成しない (下の 馬連 節で skip)。
            # 予算をワイド5点 + 三連複10点×2倍 = 25×amt まで拡張して途中切れを防ぐ。
            budget = max(budget, amt * 25)

        def add(t, detail, hns, odds, prob, name, amount=None):
            nonlocal spent
            amt_i = amount or amt
            sig = (t, tuple(sorted(hns)))
            if sig in signatures:
                return
            if len(set(hns)) != len(hns):
                # #95: 同一馬ペア (馬連 7-7 等) の無効買い目を構造的に遮断
                return
            if spent + amt_i > budget:
                return
            # 2026-05-27 entertainment モード: honor_bets は EV 制限なしで全買い目生成
            # 「印通り 馬連 5点流し / 三連複 10点流し」の完全提示を維持する
            # (Phase α では EV>=MIN_EV を強制していたが、夢ロマン路線では妙味少ない印買い目も提示)
            ev_estimated = prob * odds
            signatures.add(sig)
            spent += amt_i
            bets.append({
                "type": t, "detail": detail, "horse_numbers": hns,
                "amount": amt_i, "odds": round(odds, 1),
                "ev": round(ev_estimated, 2), "prob": round(prob, 3),
                "horse_name": name, "honor": True,
            })

        # 軸 = 表示上の ◎ (#95: 旧 pred_win 1位は印 v5 の ◎ と乖離していた)
        p1 = self.get_axis_horse(predictions, sorted_preds)
        if p1 is None:
            return bets, 0
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
        # #95: 軸馬が相手プールに混入しないよう明示的に除外 (同一馬ペア防止の二重防御)
        partners = [p for p in partners if p["horse_number"] != center][:5]

        # 優先順は ROI 最大化観点で「馬連 → ワイド → 三連複」
        # データから: 馬連207% > 三連複174% > ワイド150% だが、
        # 馬連・ワイドは1人気軸で的中率が高くROIが安定 → 先に確保

        # ─── 主力1: 馬連 ◎-相手 流し (5点) ROI 207% ───
        # trio_focus band 中は生成しない (帯内 馬連 ROI 83%、#95)
        if "馬連" in enabled and not trio_focus:
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
                # trio_focus band 中は2倍額 (帯内 trio ROI 200%、#95)
                add("三連複", f"{nums[0]}-{nums[1]}-{nums[2]}", nums, est_odds, est_prob, names,
                    amount=(amt * 2 if trio_focus else None))

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

    def generate_bets(self, predictions, bankroll=None, bet_types=None, confidence=None,
                      race_info=None, strict_whitelist=False):
        """予測結果から推奨馬券を生成（回収率重視版 + honor bets + confidence-aware）

        strict_whitelist=True: WHITELIST_SEGMENTS に該当する場合のみ、許可された
            bet_types のみを生成 (specialization mode)。
        """
        # Phase α: whitelist specialization mode
        if strict_whitelist:
            match = self.get_whitelist_match(race_info, confidence)
            if not match:
                return {"bets": [], "total_amount": 0, "race_info": {}, "skipped": True,
                        "skip_reason": "Whitelist 該当なし"}
            seg_name, allowed_types = match
            bet_types = allowed_types  # whitelist で指定された券種のみ
            print(f"  🎯 Whitelist hit: {seg_name} (bet_types={allowed_types})")

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

        # ── trio-focus band 判定 (#95 2026-07-02) ──
        band = self.trio_focus_band(predictions, confidence, race_info=race_info)
        if band:
            print("  🎯 trio-focus band (◎2.0-2.9倍×S/A/B): 三連複◎軸2倍額 / 馬連なし / ワイド維持")
            # _honor_bets 内部の band 予算拡張 (25×line_amount) と整合させる。
            # これが無いと total_amount > budget になり、後続フォールバックの
            # min(300, budget - total_amount) が負額 bet を生む (smoke test で検出)。
            budget = max(budget, line_amount * 25)

        # ── 0. 印通り保証買い目 (EV 関係なく必ず含める) ──
        # v2 (2026-05-24): バックテスト結果から「最強の買い目」を主力化:
        #   - ◎軸三連複5頭流し (10点) ROI 174%
        #   - 馬連 ◎-相手5頭 流し (5点) ROI 207%
        #   - ワイド ◎-相手5頭 流し (5点) ROI 150%
        # 旧 honor (◎単勝・◎複勝) は ROI 90%前後で実質マイナスのため廃止。
        # v3 (2026-05-27): line_amount を confidence で重み付け
        honor_list, honor_spent = self._honor_bets(sorted_preds, enabled, budget,
                                                    predictions=predictions,
                                                    line_amount=line_amount,
                                                    trio_focus=band)
        bets = list(honor_list)
        total_amount = honor_spent
        # honor で既に bet した signature を以後の EV bets で重複させない
        honor_signatures = {(b["type"], tuple(sorted(b["horse_numbers"]))) for b in honor_list}

        # 複勝率8%以上（÷3済み値で判定、実質24%以上）の上位馬
        top_horses = [p for p in sorted_preds if p["pred_top3"] >= 0.08][:4]

        # ── 1. 単勝（EVがプラスの馬）──
        # Phase α: EV >= MIN_EV (1.2) で厳格化 (2026-05-27 Phase α)
        if "単勝" in enabled:
            for p in sorted_preds:
                ev = p["pred_win"] * p["odds_win"]
                if self.MIN_EV <= ev < self.MAX_EV and p["odds_win"] >= 2.0 and p["pred_win"] >= 0.08:
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
        # 2026-05-27: entertainment モード MIN_EV=0.8 で統一
        if "複勝" in enabled:
            for p in sorted_preds:
                odds_place = p.get("odds_place", 1.5)
                ev = p["pred_top3"] * odds_place
                if self.MIN_EV <= ev < self.MAX_EV and p["pred_top3"] >= 0.12:
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
                # Phase α: ワイド MIN_EV 厳格化 0.8 → 1.2 / #95: MAX_EV band-pass
                if self.MIN_EV <= ev < self.MAX_EV:
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
                # Phase α: 馬連 MIN_EV 厳格化 0.5 → 1.2 / #95: MAX_EV band-pass
                if self.MIN_EV <= ev < self.MAX_EV:
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
                    # Phase α: 三連複 MIN_EV 厳格化 0.8 → 1.2 / #95: MAX_EV band-pass
                    if self.MIN_EV <= ev < self.MAX_EV:
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
                        # Phase α: 三連単 MIN_EV 厳格化 0.3 → 1.2 / #95: MAX_EV band-pass
                        if self.MIN_EV <= ev < self.MAX_EV:
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
            # #95: remaining >= 100 ガード追加 (他のフォールバックには元からあるのに
            # 単勝だけ欠けており、予算超過時に負額 bet を生んでいた)
            if "単勝" in enabled and "単勝" not in existing_types and top["pred_win"] >= 0.08 \
                    and budget - total_amount >= 100:
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
            # #95: trio-focus band 該当フラグ (sb=0 ゼロ化の例外判定と ROI モニタ用)
            "trio_focus_band": band,
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
