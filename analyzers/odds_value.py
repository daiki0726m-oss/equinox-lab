"""
オッズ・期待値分析モジュール
モデル予測確率とオッズから期待値を算出し、過小評価馬を検出
"""

from database import get_db


class OddsValueAnalyzer:
    """
    期待値分析

    期待値(EV) = 予測勝率 × オッズ
    EV > 1.0 → 長期的にプラス（過小評価）
    EV < 1.0 → 長期的にマイナス（過大評価）

    最低でも EV > 1.2 を購入基準とすることで、
    モデル誤差のマージンを確保する
    """

    MIN_EV_THRESHOLD = 1.2  # 購入最低期待値

    def calculate_expected_value(self, predicted_prob, odds):
        """期待値を計算"""
        if odds <= 0 or predicted_prob <= 0:
            return 0
        return round(predicted_prob * odds, 3)

    def calculate_fair_odds(self, predicted_prob):
        """適正オッズを計算（予測確率から）"""
        if predicted_prob <= 0:
            return 999
        return round(1.0 / predicted_prob, 1)

    def analyze_race_value(self, predictions):
        """
        レース全体の期待値分析

        predictions: [
            {
                "horse_number": 1,
                "horse_name": "...",
                "predicted_prob": 0.25,  # モデル予測の勝率
                "predicted_top3_prob": 0.55,  # 複勝確率
                "odds_win": 3.5,      # 単勝オッズ
                "odds_place": 1.8,    # 複勝オッズ(概算)
            }, ...
        ]
        """
        results = []

        for p in predictions:
            horse = {
                "horse_number": p["horse_number"],
                "horse_name": p.get("horse_name", ""),
                "predicted_win_prob": p.get("predicted_prob", 0),
                "predicted_top3_prob": p.get("predicted_top3_prob", 0),
                "odds_win": p.get("odds_win", 0),
                "odds_place": p.get("odds_place", 0),
            }

            # 単勝期待値
            horse["ev_win"] = self.calculate_expected_value(
                horse["predicted_win_prob"], horse["odds_win"]
            )

            # 複勝期待値
            horse["ev_place"] = self.calculate_expected_value(
                horse["predicted_top3_prob"], horse["odds_place"]
            )

            # 適正オッズ
            horse["fair_odds_win"] = self.calculate_fair_odds(horse["predicted_win_prob"])
            horse["fair_odds_place"] = self.calculate_fair_odds(horse["predicted_top3_prob"])

            # 過小評価度（オッズ / 適正オッズ）
            if horse["fair_odds_win"] > 0:
                horse["value_ratio"] = round(horse["odds_win"] / horse["fair_odds_win"], 2)
            else:
                horse["value_ratio"] = 0

            # 評価ランク
            if horse["ev_win"] >= 2.0:
                horse["rank"] = "◎ 超お宝"
            elif horse["ev_win"] >= 1.5:
                horse["rank"] = "○ お宝"
            elif horse["ev_win"] >= self.MIN_EV_THRESHOLD:
                horse["rank"] = "▲ 狙い目"
            elif horse["ev_win"] >= 1.0:
                horse["rank"] = "△ ボーダー"
            else:
                horse["rank"] = "✕ 見送り"

            results.append(horse)

        # 期待値の高い順にソート
        results.sort(key=lambda x: x["ev_win"], reverse=True)

        return results

    def find_value_bets(self, predictions):
        """期待値が閾値以上の馬券を抽出"""
        analyzed = self.analyze_race_value(predictions)
        value_bets = []

        for horse in analyzed:
            bets = []

            # 単勝で期待値が高い場合
            if horse["ev_win"] >= self.MIN_EV_THRESHOLD:
                bets.append({
                    "type": "単勝",
                    "horse_number": horse["horse_number"],
                    "ev": horse["ev_win"],
                    "odds": horse["odds_win"],
                    "prob": horse["predicted_win_prob"],
                })

            # 複勝で期待値が高い場合
            if horse["ev_place"] >= 1.1:  # 複勝はやや低い閾値
                bets.append({
                    "type": "複勝",
                    "horse_number": horse["horse_number"],
                    "ev": horse["ev_place"],
                    "odds": horse["odds_place"],
                    "prob": horse["predicted_top3_prob"],
                })

            if bets:
                value_bets.extend(bets)

        return value_bets

    def detect_odds_anomaly(self, race_id):
        """過去のオッズ傾向から異常を検出（人気と実力の乖離）"""
        with get_db() as conn:
            rows = conn.execute("""
                SELECT r.horse_number, r.horse_id, r.odds, r.popularity,
                       r.finish_position, ra.horse_count
                FROM results r
                JOIN races ra ON r.race_id = ra.race_id
                WHERE r.race_id = ?
                  AND r.finish_position > 0
                ORDER BY r.popularity
            """, (race_id,)).fetchall()

        if not rows:
            return []

        anomalies = []
        for row in rows:
            # 人気と着順の乖離を検出
            if row["popularity"] and row["finish_position"]:
                gap = row["popularity"] - row["finish_position"]
                if gap >= 5:  # 人気より5着以上上の成績
                    anomalies.append({
                        "horse_number": row["horse_number"],
                        "horse_id": row["horse_id"],
                        "popularity": row["popularity"],
                        "finish_position": row["finish_position"],
                        "odds": row["odds"],
                        "type": "穴馬激走",
                    })
                elif gap <= -5:  # 人気より5着以上下の成績
                    anomalies.append({
                        "horse_number": row["horse_number"],
                        "horse_id": row["horse_id"],
                        "popularity": row["popularity"],
                        "finish_position": row["finish_position"],
                        "odds": row["odds"],
                        "type": "人気馬凡走",
                    })

        return anomalies
