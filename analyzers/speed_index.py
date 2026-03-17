"""
スピード指数計算モジュール
走破タイムを馬場・距離・コース形状で補正し統一的な能力指数を算出
"""

import pandas as pd
import numpy as np
from database import get_db


class SpeedIndexCalculator:
    """
    スピード指数 = (基準タイム - 走破タイム) × 距離補正 + 馬場差補正 + ベース値(80)

    基準タイム: 各競馬場・距離・馬場状態の平均走破タイム
    距離補正: 短距離ほど1秒の価値が大きい
    馬場差補正: 重馬場等の影響を数値化
    """

    BASE_INDEX = 80  # 指数の基準値

    # 馬場状態の補正値 (良を基準)
    TRACK_CONDITION_ADJUST = {
        "良": 0,
        "稍重": -1.5,
        "重": -3.0,
        "不良": -5.0,
    }

    # 距離帯別の1秒あたり指数変動値
    DISTANCE_FACTOR = {
        (0, 1200): 15.0,      # スプリント
        (1200, 1600): 12.0,   # マイル
        (1600, 2000): 10.0,   # 中距離
        (2000, 2500): 8.5,    # 中長距離
        (2500, 9999): 7.0,    # 長距離
    }

    def __init__(self):
        self.base_times = {}  # キャッシュ

    def _get_distance_factor(self, distance):
        """距離帯に応じた補正係数を取得"""
        for (low, high), factor in self.DISTANCE_FACTOR.items():
            if low <= distance < high:
                return factor
        return 10.0

    def _get_base_time(self, venue, distance, surface, track_condition="良"):
        """基準タイムを取得 (DBから平均タイムを計算)"""
        key = (venue, distance, surface, track_condition)
        if key in self.base_times:
            return self.base_times[key]

        with get_db() as conn:
            row = conn.execute("""
                SELECT AVG(r.finish_time_seconds) as avg_time,
                       COUNT(*) as cnt
                FROM results r
                JOIN races ra ON r.race_id = ra.race_id
                WHERE ra.venue = ? AND ra.distance = ? AND ra.surface = ?
                  AND ra.track_condition = ?
                  AND r.finish_time_seconds > 0
                  AND r.finish_position BETWEEN 1 AND 5
            """, (venue, distance, surface, track_condition)).fetchone()

            if row and row["cnt"] >= 5:
                base_time = row["avg_time"]
            else:
                # データ不足の場合は馬場状態を問わず平均を使用
                row2 = conn.execute("""
                    SELECT AVG(r.finish_time_seconds) as avg_time,
                           COUNT(*) as cnt
                    FROM results r
                    JOIN races ra ON r.race_id = ra.race_id
                    WHERE ra.venue = ? AND ra.distance = ? AND ra.surface = ?
                      AND r.finish_time_seconds > 0
                      AND r.finish_position BETWEEN 1 AND 5
                """, (venue, distance, surface)).fetchone()

                if row2 and row2["cnt"] >= 3:
                    base_time = row2["avg_time"]
                else:
                    # さらにデータ不足の場合は距離のみで計算
                    row3 = conn.execute("""
                        SELECT AVG(r.finish_time_seconds) as avg_time
                        FROM results r
                        JOIN races ra ON r.race_id = ra.race_id
                        WHERE ra.distance = ? AND ra.surface = ?
                          AND r.finish_time_seconds > 0
                          AND r.finish_position BETWEEN 1 AND 5
                    """, (distance, surface)).fetchone()
                    base_time = row3["avg_time"] if row3 and row3["avg_time"] else distance * 0.06

        self.base_times[key] = base_time
        return base_time

    def calculate(self, finish_time_seconds, venue, distance, surface, track_condition="良"):
        """スピード指数を1頭分計算"""
        if finish_time_seconds <= 0:
            return 0

        base_time = self._get_base_time(venue, distance, surface, track_condition)
        if not base_time or base_time <= 0:
            return 0

        # タイム差(秒)
        time_diff = base_time - finish_time_seconds

        # 距離ファクター
        dist_factor = self._get_distance_factor(distance)

        # 馬場差補正
        track_adjust = self.TRACK_CONDITION_ADJUST.get(track_condition, 0)

        # スピード指数計算
        speed_index = self.BASE_INDEX + (time_diff * dist_factor) + track_adjust

        return round(speed_index, 1)

    def calculate_race(self, race_id):
        """レース全体のスピード指数を計算"""
        with get_db() as conn:
            race = conn.execute(
                "SELECT * FROM races WHERE race_id = ?", (race_id,)
            ).fetchone()
            if not race:
                return {}

            results = conn.execute(
                "SELECT * FROM results WHERE race_id = ? ORDER BY finish_position",
                (race_id,)
            ).fetchall()

        indices = {}
        for r in results:
            if r["finish_time_seconds"] and r["finish_time_seconds"] > 0:
                idx = self.calculate(
                    r["finish_time_seconds"],
                    race["venue"], race["distance"],
                    race["surface"], race["track_condition"] or "良"
                )
                indices[r["horse_number"]] = {
                    "horse_id": r["horse_id"],
                    "speed_index": idx,
                    "finish_position": r["finish_position"],
                }

        return indices

    def get_horse_indices(self, horse_id, n_races=5):
        """馬の直近N走のスピード指数を取得"""
        with get_db() as conn:
            rows = conn.execute("""
                SELECT r.*, ra.venue, ra.distance, ra.surface, ra.track_condition
                FROM results r
                JOIN races ra ON r.race_id = ra.race_id
                WHERE r.horse_id = ?
                  AND r.finish_time_seconds > 0
                ORDER BY ra.race_date DESC
                LIMIT ?
            """, (horse_id, n_races)).fetchall()

        indices = []
        for row in rows:
            idx = self.calculate(
                row["finish_time_seconds"],
                row["venue"], row["distance"],
                row["surface"], row["track_condition"] or "良"
            )
            indices.append(idx)

        return indices

    def get_horse_stats(self, horse_id, n_races=5):
        """馬のスピード指数の統計値を取得"""
        indices = self.get_horse_indices(horse_id, n_races)
        if not indices:
            return {"avg": 0, "max": 0, "min": 0, "std": 0, "latest": 0, "count": 0}

        return {
            "avg": round(np.mean(indices), 1),
            "max": round(max(indices), 1),
            "min": round(min(indices), 1),
            "std": round(np.std(indices), 1) if len(indices) > 1 else 0,
            "latest": indices[0],
            "count": len(indices),
        }
