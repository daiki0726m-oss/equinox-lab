"""
ペース分析モジュール
レースの展開予測と各馬のペース適性を分析
"""

from database import get_db


class PaceAnalyzer:
    """
    ペース分析

    1. 各馬の逃げ/先行率からレースの予想ペースを算出
    2. ペース別の各馬の適性を評価
    3. ハイペース → 差し/追込有利、スローペース → 逃げ/先行有利
    """

    def get_horse_running_tendency(self, horse_id, n_races=5):
        """馬の脚質傾向を取得"""
        with get_db() as conn:
            rows = conn.execute("""
                SELECT r.passing_order, ra.horse_count, r.finish_position,
                       r.last_3f, r.finish_time_seconds, ra.distance
                FROM results r
                JOIN races ra ON r.race_id = ra.race_id
                WHERE r.horse_id = ?
                  AND r.finish_position > 0
                  AND r.passing_order != ''
                ORDER BY ra.race_date DESC
                LIMIT ?
            """, (horse_id, n_races)).fetchall()

        if not rows:
            return {
                "front_rate": 0.5,
                "avg_first_pos_ratio": 0.5,
                "avg_last_3f": 0,
                "style": "不明",
                "count": 0,
            }

        front_count = 0
        pos_ratios = []
        last_3fs = []

        for row in rows:
            po = row["passing_order"]
            hc = row["horse_count"] or 18
            try:
                positions = [int(p) for p in po.replace("-", ",").split(",") if p.strip().isdigit()]
                if positions:
                    first_pos = positions[0]
                    ratio = first_pos / max(hc, 1)
                    pos_ratios.append(ratio)
                    if ratio <= 0.35:
                        front_count += 1
            except ValueError:
                continue

            if row["last_3f"] and row["last_3f"] > 0:
                last_3fs.append(row["last_3f"])

        total = len(rows)
        avg_ratio = sum(pos_ratios) / len(pos_ratios) if pos_ratios else 0.5
        front_rate = front_count / total if total > 0 else 0.5

        # 脚質分類
        if avg_ratio <= 0.15:
            style = "逃げ"
        elif avg_ratio <= 0.35:
            style = "先行"
        elif avg_ratio <= 0.65:
            style = "差し"
        else:
            style = "追込"

        return {
            "front_rate": round(front_rate, 2),
            "avg_first_pos_ratio": round(avg_ratio, 2),
            "avg_last_3f": round(sum(last_3fs) / len(last_3fs), 1) if last_3fs else 0,
            "style": style,
            "count": total,
        }

    def predict_pace(self, horse_ids):
        """レースの予想ペースを算出"""
        tendencies = {}
        front_runners = 0
        total_horses = len(horse_ids)

        for horse_id in horse_ids:
            t = self.get_horse_running_tendency(horse_id)
            tendencies[horse_id] = t
            if t["front_rate"] >= 0.5:
                front_runners += 1

        # 先行馬の割合でペースを予測
        front_ratio = front_runners / max(total_horses, 1)

        if front_ratio >= 0.4:
            pace = "ハイ"
            pace_score = 80 + (front_ratio - 0.4) * 100
        elif front_ratio >= 0.25:
            pace = "ミドル"
            pace_score = 50
        else:
            pace = "スロー"
            pace_score = 20 - (0.25 - front_ratio) * 100

        pace_score = max(0, min(100, pace_score))

        return {
            "predicted_pace": pace,
            "pace_score": round(pace_score, 1),  # 高い=ハイペース
            "front_runners": front_runners,
            "total_horses": total_horses,
            "front_ratio": round(front_ratio, 2),
            "tendencies": tendencies,
        }

    def analyze_horse_pace_fit(self, horse_id, predicted_pace):
        """馬のペース適性スコア"""
        tendency = self.get_horse_running_tendency(horse_id)
        score = 50

        style = tendency["style"]

        # ペースとの相性
        pace_style_matrix = {
            "ハイ": {"逃げ": -15, "先行": -5, "差し": 10, "追込": 15},
            "ミドル": {"逃げ": 5, "先行": 5, "差し": 0, "追込": -5},
            "スロー": {"逃げ": 15, "先行": 10, "差し": -5, "追込": -15},
        }

        if predicted_pace in pace_style_matrix and style in pace_style_matrix[predicted_pace]:
            score += pace_style_matrix[predicted_pace][style]

        # 上がり3Fが速い馬はハイペースで有利
        if tendency["avg_last_3f"] > 0:
            if tendency["avg_last_3f"] < 34.0:
                score += 5  # 上がりが速い
            elif tendency["avg_last_3f"] > 36.0:
                score -= 3  # 上がりが遅い

        score = max(0, min(100, score))

        return {
            "score": round(score, 1),
            "tendency": tendency,
            "predicted_pace": predicted_pace,
            "pace_fit": "◎" if score >= 65 else "○" if score >= 55 else "△" if score >= 45 else "▲",
        }
