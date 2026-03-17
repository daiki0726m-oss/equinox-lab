"""
血統分析モジュール
種牡馬・母父の距離適性・馬場適性をスコア化
"""

from database import get_db


class PedigreeAnalyzer:
    """
    血統から距離適性・馬場適性・コース適性を分析

    分析項目:
    1. 種牡馬(父)の距離帯別成績
    2. 種牡馬の馬場状態別成績
    3. 母父の距離帯別成績
    4. 種牡馬のコース(競馬場)別成績
    """

    DISTANCE_CATEGORIES = {
        "sprint": (0, 1400),        # スプリント
        "mile": (1400, 1800),       # マイル
        "middle": (1800, 2200),     # 中距離
        "long": (2200, 9999),       # 長距離
    }

    def _get_distance_category(self, distance):
        """距離カテゴリを返す"""
        for cat, (low, high) in self.DISTANCE_CATEGORIES.items():
            if low <= distance < high:
                return cat
        return "middle"

    def get_sire_stats(self, sire_name, distance=None, surface=None,
                       track_condition=None, venue=None):
        """種牡馬の成績統計を取得"""
        with get_db() as conn:
            query = """
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN r.finish_position = 1 THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN r.finish_position <= 2 THEN 1 ELSE 0 END) as top2,
                    SUM(CASE WHEN r.finish_position <= 3 THEN 1 ELSE 0 END) as top3,
                    AVG(r.finish_position) as avg_pos
                FROM results r
                JOIN horses h ON r.horse_id = h.horse_id
                JOIN races ra ON r.race_id = ra.race_id
                WHERE h.sire = ?
                  AND r.finish_position > 0
            """
            params = [sire_name]

            if distance:
                cat = self._get_distance_category(distance)
                low, high = self.DISTANCE_CATEGORIES[cat]
                query += " AND ra.distance >= ? AND ra.distance < ?"
                params.extend([low, high])

            if surface:
                query += " AND ra.surface = ?"
                params.append(surface)

            if track_condition:
                query += " AND ra.track_condition = ?"
                params.append(track_condition)

            if venue:
                query += " AND ra.venue = ?"
                params.append(venue)

            row = conn.execute(query, params).fetchone()

        if not row or row["total"] == 0:
            return {"win_rate": 0, "top2_rate": 0, "top3_rate": 0, "avg_pos": 0, "total": 0}

        total = row["total"]
        return {
            "win_rate": round(row["wins"] / total * 100, 1),
            "top2_rate": round(row["top2"] / total * 100, 1),
            "top3_rate": round(row["top3"] / total * 100, 1),
            "avg_pos": round(row["avg_pos"], 1),
            "total": total,
        }

    def get_damsire_stats(self, damsire_name, distance=None, surface=None):
        """母父の成績統計を取得"""
        with get_db() as conn:
            query = """
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN r.finish_position = 1 THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN r.finish_position <= 3 THEN 1 ELSE 0 END) as top3,
                    AVG(r.finish_position) as avg_pos
                FROM results r
                JOIN horses h ON r.horse_id = h.horse_id
                JOIN races ra ON r.race_id = ra.race_id
                WHERE h.damsire = ?
                  AND r.finish_position > 0
            """
            params = [damsire_name]

            if distance:
                cat = self._get_distance_category(distance)
                low, high = self.DISTANCE_CATEGORIES[cat]
                query += " AND ra.distance >= ? AND ra.distance < ?"
                params.extend([low, high])

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

    def analyze_horse(self, horse_id, distance, surface, track_condition="良", venue=""):
        """馬の血統適性を総合スコア化"""
        with get_db() as conn:
            horse = conn.execute(
                "SELECT * FROM horses WHERE horse_id = ?", (horse_id,)
            ).fetchone()

        if not horse:
            return {"score": 50, "details": {}}

        sire = horse["sire"] or ""
        damsire = horse["damsire"] or ""

        details = {}
        score = 50  # 基準スコア

        # 種牡馬の距離適性
        if sire:
            sire_dist = self.get_sire_stats(sire, distance=distance, surface=surface)
            details["sire_distance"] = sire_dist
            if sire_dist["total"] >= 10:
                # 複勝率が高いほどプラス
                score += (sire_dist["top3_rate"] - 30) * 0.3

            # 種牡馬の馬場適性
            sire_track = self.get_sire_stats(sire, surface=surface, track_condition=track_condition)
            details["sire_track_cond"] = sire_track
            if sire_track["total"] >= 10:
                score += (sire_track["top3_rate"] - 30) * 0.2

            # 種牡馬のコース適性
            if venue:
                sire_venue = self.get_sire_stats(sire, venue=venue)
                details["sire_venue"] = sire_venue
                if sire_venue["total"] >= 5:
                    score += (sire_venue["top3_rate"] - 30) * 0.15

        # 母父の距離適性
        if damsire:
            ds_dist = self.get_damsire_stats(damsire, distance=distance, surface=surface)
            details["damsire_distance"] = ds_dist
            if ds_dist["total"] >= 10:
                score += (ds_dist["top3_rate"] - 30) * 0.15

        # スコアを0-100に正規化
        score = max(0, min(100, score))

        return {
            "score": round(score, 1),
            "sire": sire,
            "damsire": damsire,
            "details": details,
        }
