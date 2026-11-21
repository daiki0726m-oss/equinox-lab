"""
馬場バイアス分析モジュール
当日の脚質別成績・枠順別成績から馬場の偏りを検出
"""

from database import get_db


class TrackBiasAnalyzer:
    """
    馬場バイアス（馬場の偏り）を分析

    分析項目:
    1. 脚質別成績（逃げ/先行/差し/追込）
    2. 枠順別成績（内枠/中枠/外枠）
    3. 当日バイアス（同日の他レース結果から傾向を推定）
    """

    def classify_running_style(self, passing_order, horse_count):
        """通過順から脚質を分類"""
        if not passing_order or not horse_count:
            return "不明"

        try:
            positions = [int(p) for p in passing_order.replace("-", ",").split(",") if p.strip().isdigit()]
            if not positions:
                return "不明"

            first_pos = positions[0]
            ratio = first_pos / max(horse_count, 1)

            if ratio <= 0.15 or first_pos == 1:
                return "逃げ"
            elif ratio <= 0.4:
                return "先行"
            elif ratio <= 0.7:
                return "差し"
            else:
                return "追込"
        except (ValueError, ZeroDivisionError):
            return "不明"

    def get_running_style_stats(self, venue, surface, distance=None, track_condition=None, recent_days=60):
        """脚質別の成績統計を取得"""
        with get_db() as conn:
            query = """
                SELECT r.passing_order, r.finish_position, ra.horse_count
                FROM results r
                JOIN races ra ON r.race_id = ra.race_id
                WHERE ra.venue = ? AND ra.surface = ?
                  AND r.finish_position > 0
                  AND r.passing_order != ''
                  AND ra.race_date >= date('now', ?)
            """
            params = [venue, surface, f"-{recent_days} days"]

            if distance:
                query += " AND ra.distance BETWEEN ? AND ?"
                params.extend([distance - 200, distance + 200])
            if track_condition:
                query += " AND ra.track_condition = ?"
                params.append(track_condition)

            rows = conn.execute(query, params).fetchall()

        styles = {"逃げ": [], "先行": [], "差し": [], "追込": []}

        for row in rows:
            style = self.classify_running_style(row["passing_order"], row["horse_count"])
            if style in styles:
                styles[style].append(row["finish_position"])

        stats = {}
        for style, positions in styles.items():
            if not positions:
                stats[style] = {"win_rate": 0, "top3_rate": 0, "avg_pos": 0, "total": 0}
                continue

            total = len(positions)
            wins = sum(1 for p in positions if p == 1)
            top3 = sum(1 for p in positions if p <= 3)
            stats[style] = {
                "win_rate": round(wins / total * 100, 1),
                "top3_rate": round(top3 / total * 100, 1),
                "avg_pos": round(sum(positions) / total, 1),
                "total": total,
            }

        return stats

    def get_post_position_stats(self, venue, surface, distance=None, track_condition=None, recent_days=60):
        """枠順別の成績統計（内枠/中枠/外枠）"""
        with get_db() as conn:
            query = """
                SELECT r.post_position, r.horse_number, r.finish_position, ra.horse_count
                FROM results r
                JOIN races ra ON r.race_id = ra.race_id
                WHERE ra.venue = ? AND ra.surface = ?
                  AND r.finish_position > 0
                  AND ra.race_date >= date('now', ?)
            """
            params = [venue, surface, f"-{recent_days} days"]

            if distance:
                query += " AND ra.distance BETWEEN ? AND ?"
                params.extend([distance - 200, distance + 200])
            if track_condition:
                query += " AND ra.track_condition = ?"
                params.append(track_condition)

            rows = conn.execute(query, params).fetchall()

        groups = {"内枠": [], "中枠": [], "外枠": []}

        for row in rows:
            hc = row["horse_count"] or 18
            hn = row["horse_number"] or 0
            if hn == 0:
                continue

            ratio = hn / hc
            if ratio <= 0.33:
                group = "内枠"
            elif ratio <= 0.66:
                group = "中枠"
            else:
                group = "外枠"

            groups[group].append(row["finish_position"])

        stats = {}
        for group, positions in groups.items():
            if not positions:
                stats[group] = {"win_rate": 0, "top3_rate": 0, "avg_pos": 0, "total": 0}
                continue

            total = len(positions)
            wins = sum(1 for p in positions if p == 1)
            top3 = sum(1 for p in positions if p <= 3)
            stats[group] = {
                "win_rate": round(wins / total * 100, 1),
                "top3_rate": round(top3 / total * 100, 1),
                "avg_pos": round(sum(positions) / total, 1),
                "total": total,
            }

        return stats

    def analyze(self, horse_number, horse_count, passing_order_history,
                venue, surface, distance, track_condition="良"):
        """馬場バイアスを考慮した馬のスコアを算出"""
        score = 50

        # 脚質別バイアス
        style_stats = self.get_running_style_stats(venue, surface, distance, track_condition)

        # この馬の予想脚質を過去の通過順から推定
        horse_style = "不明"
        if passing_order_history:
            style_counts = {"逃げ": 0, "先行": 0, "差し": 0, "追込": 0}
            for po in passing_order_history:
                s = self.classify_running_style(po, horse_count)
                if s in style_counts:
                    style_counts[s] += 1
            horse_style = max(style_counts, key=style_counts.get) if any(style_counts.values()) else "差し"

        # 有利な脚質にボーナス
        if horse_style in style_stats and style_stats[horse_style]["total"] >= 5:
            style_top3 = style_stats[horse_style]["top3_rate"]
            # 平均的な複勝率(約25%)との差をスコアに反映
            score += (style_top3 - 25) * 0.5

        # 枠順バイアス
        post_stats = self.get_post_position_stats(venue, surface, distance, track_condition)
        ratio = horse_number / max(horse_count, 1)
        if ratio <= 0.33:
            post_group = "内枠"
        elif ratio <= 0.66:
            post_group = "中枠"
        else:
            post_group = "外枠"

        if post_group in post_stats and post_stats[post_group]["total"] >= 5:
            post_top3 = post_stats[post_group]["top3_rate"]
            score += (post_top3 - 25) * 0.3

        score = max(0, min(100, score))

        return {
            "score": round(score, 1),
            "horse_style": horse_style,
            "post_group": post_group,
            "style_bias": style_stats,
            "post_bias": post_stats,
        }
