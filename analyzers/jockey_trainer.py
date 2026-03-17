"""
騎手・調教師分析モジュール
競馬場別・距離帯別・馬場状態別の成績とコンビ相性を分析
"""

from database import get_db


class JockeyTrainerAnalyzer:
    """
    騎手と調教師の条件別成績を分析

    分析項目:
    1. 騎手の条件別（競馬場/距離/馬場）勝率・複勝率
    2. 調教師の条件別成績
    3. 騎手×調教師コンビの過去成績
    4. リーディング上位の加点
    """

    def get_jockey_stats(self, jockey_id, venue=None, distance=None,
                         surface=None, track_condition=None):
        """騎手の条件別成績"""
        with get_db() as conn:
            query = """
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN r.finish_position = 1 THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN r.finish_position <= 2 THEN 1 ELSE 0 END) as top2,
                    SUM(CASE WHEN r.finish_position <= 3 THEN 1 ELSE 0 END) as top3,
                    AVG(r.finish_position) as avg_pos,
                    AVG(CASE WHEN r.odds > 0 THEN
                        CASE WHEN r.finish_position = 1 THEN r.odds * 100 ELSE 0 END
                    END) as avg_return_win
                FROM results r
                JOIN races ra ON r.race_id = ra.race_id
                WHERE r.jockey_id = ?
                  AND r.finish_position > 0
            """
            params = [jockey_id]

            if venue:
                query += " AND ra.venue = ?"
                params.append(venue)
            if distance:
                # 距離帯で絞り込み (±200m)
                query += " AND ra.distance BETWEEN ? AND ?"
                params.extend([distance - 200, distance + 200])
            if surface:
                query += " AND ra.surface = ?"
                params.append(surface)
            if track_condition:
                query += " AND ra.track_condition = ?"
                params.append(track_condition)

            row = conn.execute(query, params).fetchone()

        if not row or row["total"] == 0:
            return {"win_rate": 0, "top2_rate": 0, "top3_rate": 0,
                    "avg_pos": 0, "total": 0, "roi_win": 0}

        total = row["total"]
        return {
            "win_rate": round(row["wins"] / total * 100, 1),
            "top2_rate": round(row["top2"] / total * 100, 1),
            "top3_rate": round(row["top3"] / total * 100, 1),
            "avg_pos": round(row["avg_pos"], 1),
            "total": total,
            "roi_win": round(row["avg_return_win"], 1) if row["avg_return_win"] else 0,
        }

    def get_trainer_stats(self, trainer_id, venue=None, distance=None, surface=None):
        """調教師の条件別成績"""
        with get_db() as conn:
            query = """
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN r.finish_position = 1 THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN r.finish_position <= 3 THEN 1 ELSE 0 END) as top3,
                    AVG(r.finish_position) as avg_pos
                FROM results r
                JOIN races ra ON r.race_id = ra.race_id
                WHERE r.trainer_id = ?
                  AND r.finish_position > 0
            """
            params = [trainer_id]

            if venue:
                query += " AND ra.venue = ?"
                params.append(venue)
            if distance:
                query += " AND ra.distance BETWEEN ? AND ?"
                params.extend([distance - 200, distance + 200])
            if surface:
                query += " AND ra.surface = ?"
                params.append(surface)

            row = conn.execute(query, params).fetchone()

        if not row or row["total"] == 0:
            return {"win_rate": 0, "top3_rate": 0, "avg_pos": 0, "total": 0}

        total = row["total"]
        return {
            "win_rate": round(row["wins"] / total * 100, 1),
            "top3_rate": round(row["top3"] / total * 100, 1),
            "avg_pos": round(row["avg_pos"], 1),
            "total": total,
        }

    def get_combo_stats(self, jockey_id, trainer_id):
        """騎手×調教師コンビの成績"""
        with get_db() as conn:
            row = conn.execute("""
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN r.finish_position = 1 THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN r.finish_position <= 3 THEN 1 ELSE 0 END) as top3,
                    AVG(r.finish_position) as avg_pos
                FROM results r
                WHERE r.jockey_id = ? AND r.trainer_id = ?
                  AND r.finish_position > 0
            """, (jockey_id, trainer_id)).fetchone()

        if not row or row["total"] == 0:
            return {"win_rate": 0, "top3_rate": 0, "avg_pos": 0, "total": 0}

        total = row["total"]
        return {
            "win_rate": round(row["wins"] / total * 100, 1),
            "top3_rate": round(row["top3"] / total * 100, 1),
            "avg_pos": round(row["avg_pos"], 1),
            "total": total,
        }

    def analyze(self, jockey_id, trainer_id, venue, distance, surface, track_condition="良"):
        """騎手・調教師の総合スコア"""
        score = 50  # 基準

        details = {}

        # 騎手の全体成績
        j_all = self.get_jockey_stats(jockey_id)
        details["jockey_overall"] = j_all
        if j_all["total"] >= 30:
            score += (j_all["top3_rate"] - 25) * 0.3

        # 騎手の条件別成績
        j_cond = self.get_jockey_stats(jockey_id, venue=venue, distance=distance,
                                        surface=surface, track_condition=track_condition)
        details["jockey_condition"] = j_cond
        if j_cond["total"] >= 5:
            score += (j_cond["top3_rate"] - 25) * 0.4

        # 調教師の全体成績
        t_all = self.get_trainer_stats(trainer_id)
        details["trainer_overall"] = t_all
        if t_all["total"] >= 20:
            score += (t_all["top3_rate"] - 25) * 0.15

        # 調教師の条件別成績
        t_cond = self.get_trainer_stats(trainer_id, venue=venue, surface=surface)
        details["trainer_condition"] = t_cond
        if t_cond["total"] >= 5:
            score += (t_cond["top3_rate"] - 25) * 0.2

        # コンビ成績
        combo = self.get_combo_stats(jockey_id, trainer_id)
        details["combo"] = combo
        if combo["total"] >= 3:
            score += (combo["top3_rate"] - 30) * 0.2

        score = max(0, min(100, score))

        return {
            "score": round(score, 1),
            "details": details,
        }
