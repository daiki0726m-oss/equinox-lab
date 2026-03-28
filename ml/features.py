"""
特徴量エンジニアリング v2
オッズリーク排除 + 新特徴量追加 + LambdaRank対応
"""

import numpy as np
import pandas as pd
from database import get_db
from analyzers.speed_index import SpeedIndexCalculator
from analyzers.pedigree import PedigreeAnalyzer
from analyzers.jockey_trainer import JockeyTrainerAnalyzer
from analyzers.track_bias import TrackBiasAnalyzer
from analyzers.pace import PaceAnalyzer


class FeatureBuilder:
    """
    各分析エンジンの出力を統合して特徴量ベクトルを構築 (v2)

    特徴量カテゴリ:
    1. スピード指数系 (6次元)
    2. 血統系 (3次元)
    3. 騎手・調教師系 (5次元)
    4. 馬場バイアス系 (2次元)
    5. ペース系 (3次元)
    6. 馬の実績系 (12次元)
    7. コンテキスト系 (5次元)
    8. 天候・馬場系 (5次元)
    9. 年齢系 (2次元)
    10. コース別枠順 (2次元) ← 新規
    11. グレード×距離×年齢 (1次元) ← 新規
    12. 同距離限定成績 (2次元) ← 新規
    ────────────────
    合計: 約48次元
    """

    def __init__(self):
        self.speed_calc = SpeedIndexCalculator()
        self.pedigree_analyzer = PedigreeAnalyzer()
        self.jt_analyzer = JockeyTrainerAnalyzer()
        self.bias_analyzer = TrackBiasAnalyzer()
        self.pace_analyzer = PaceAnalyzer()

    def build_features_for_horse(self, horse_id, jockey_id, trainer_id,
                                  horse_number, horse_count,
                                  venue, distance, surface, track_condition,
                                  weight=0, weight_change=0, impost=0,
                                  odds=0, popularity=0,
                                  race_date=None, race_id=None,
                                  weather=None):
        """1頭分の特徴量を構築"""
        features = {}

        # ── 1. スピード指数系 (6次元) ──
        si_stats = self.speed_calc.get_horse_stats(horse_id, n_races=5)
        features["si_avg"] = si_stats["avg"]
        features["si_max"] = si_stats["max"]
        features["si_min"] = si_stats["min"]
        features["si_std"] = si_stats["std"]
        features["si_latest"] = si_stats["latest"]
        features["si_count"] = si_stats["count"]

        # ── 2. 血統系 (3次元) ──
        pedigree = self.pedigree_analyzer.analyze_horse(
            horse_id, distance, surface, track_condition, venue
        )
        features["pedigree_score"] = pedigree["score"]
        sire_dist = pedigree["details"].get("sire_distance", {})
        features["sire_top3_rate"] = sire_dist.get("top3_rate", 0)
        features["sire_sample_size"] = min(sire_dist.get("total", 0), 500) / 500

        # ── 3. 騎手・調教師系 (5次元) ──
        jt = self.jt_analyzer.analyze(
            jockey_id, trainer_id, venue, distance, surface, track_condition
        )
        features["jt_score"] = jt["score"]
        j_cond = jt["details"].get("jockey_condition", {})
        features["jockey_cond_top3"] = j_cond.get("top3_rate", 0)
        features["jockey_cond_win"] = j_cond.get("win_rate", 0)
        t_cond = jt["details"].get("trainer_condition", {})
        features["trainer_cond_top3"] = t_cond.get("top3_rate", 0)
        combo = jt["details"].get("combo", {})
        features["combo_top3"] = combo.get("top3_rate", 0)

        # ── 4. 馬場バイアス系 (2次元) ──
        passing_orders = self._get_passing_orders(horse_id)
        bias = self.bias_analyzer.analyze(
            horse_number, horse_count, passing_orders,
            venue, surface, distance, track_condition
        )
        features["bias_score"] = bias["score"]
        features["post_position_ratio"] = horse_number / max(horse_count, 1)

        # ── 5. ペース系 (3次元) ──
        tendency = self.pace_analyzer.get_horse_running_tendency(horse_id)
        features["front_rate"] = tendency["front_rate"]
        features["avg_pos_ratio"] = tendency["avg_first_pos_ratio"]
        features["avg_last_3f"] = tendency["avg_last_3f"]

        # ── 6. 馬の実績系 (12次元) ── ※v2で大幅強化
        features["horse_count"] = horse_count
        features["weight"] = weight / 500 if weight else 0
        features["weight_change"] = weight_change / 20 if weight_change else 0
        features["impost"] = impost
        features["distance_cat"] = self._encode_distance(distance)
        features["surface_turf"] = 1 if surface == "芝" else 0

        # 休養日数
        rest_days = self._get_rest_days(horse_id, race_date)
        features["rest_days"] = min(rest_days, 365) / 365 if rest_days else 0.5

        # --- 新特徴量: 過去成績ベース ---
        past_stats = self._get_past_performance(horse_id, race_date)
        features["avg_finish_5r"] = past_stats["avg_finish_5r"]
        features["win_rate_10r"] = past_stats["win_rate_10r"]
        features["top3_rate_10r"] = past_stats["top3_rate_10r"]
        features["finish_trend"] = past_stats["finish_trend"]
        features["race_experience"] = past_stats["race_experience"]

        # --- 新特徴量: コンテキスト ---
        ctx = self._get_context_features(horse_id, distance, surface, venue, jockey_id, race_date)
        features["distance_diff"] = ctx["distance_diff"]
        features["jockey_change"] = ctx["jockey_change"]
        features["course_top3_rate"] = ctx["course_top3_rate"]
        features["last_3f_best"] = ctx["last_3f_best"]
        features["weight_trend"] = ctx["weight_trend"]

        # ── 8. 天候・馬場系 (5次元) ── ※新規
        tc = track_condition or ""
        w = weather or ""
        tc_map = {"良": 0, "稍": 1, "稍重": 1, "重": 2, "不": 3, "不良": 3}
        wt_map = {"晴": 0, "曇": 1, "小雨": 2, "雨": 2, "小雪": 3, "雪": 3}
        features["track_cond_code"] = tc_map.get(tc, 0)
        features["weather_code"] = wt_map.get(w, 0)
        features["is_heavy_track"] = 1 if tc in ("重", "不", "不良") else 0

        # 重馬場時のパフォーマンス
        wet_stats = self._get_wet_track_stats(horse_id, race_date)
        features["horse_wet_win_rate"] = wet_stats["win_rate"]
        features["horse_wet_top3_rate"] = wet_stats["top3_rate"]

        # ── 9. 年齢系 (2次元) ── ※新規
        horse_age = self._get_horse_age(horse_id, race_date)
        features["horse_age"] = horse_age / 10.0  # 正規化 (0〜1)
        # ピーク年齢フラグ: スプリント(≤1400m)は4-5歳、中長距離は4-6歳
        if distance and distance <= 1400:
            features["is_peak_age"] = 1 if 4 <= horse_age <= 5 else 0
        else:
            features["is_peak_age"] = 1 if 4 <= horse_age <= 6 else 0

        # ── 10. コース別枠順バイアス (2次元) ── ※新規
        post_bias = self._get_course_post_position_bias(
            horse_number, venue, distance, surface, race_date
        )
        features["post_win_rate_course"] = post_bias["win_rate"]
        features["post_top3_rate_course"] = post_bias["top3_rate"]

        # ── 11. グレード×距離帯の年齢成績 (1次元) ── ※新規
        grade = self._get_race_grade(race_id) if race_id else ""
        age_perf = self._get_age_performance_by_class(
            horse_age, distance, grade, race_date
        )
        features["age_class_top3_rate"] = age_perf

        # ── 12. 同距離限定の過去成績 (2次元) ── ※新規
        dist_stats = self._get_distance_specific_stats(
            horse_id, distance, surface, race_date
        )
        features["dist_win_rate"] = dist_stats["win_rate"]
        features["dist_top3_rate"] = dist_stats["top3_rate"]

        return features

    def build_features_for_race(self, race_id):
        """レース全頭の特徴量を構築（学習データ用）"""
        with get_db() as conn:
            race = conn.execute(
                "SELECT * FROM races WHERE race_id = ?", (race_id,)
            ).fetchone()
            if not race:
                return pd.DataFrame()

            results = conn.execute(
                "SELECT * FROM results WHERE race_id = ? AND finish_position >= 0",
                (race_id,)
            ).fetchall()

        if not results:
            return pd.DataFrame()

        race_date = race["race_date"] if race else None

        rows = []
        for r in results:
            features = self.build_features_for_horse(
                horse_id=r["horse_id"],
                jockey_id=r["jockey_id"] or "",
                trainer_id=r["trainer_id"] or "",
                horse_number=r["horse_number"],
                horse_count=race["horse_count"] or len(results),
                venue=race["venue"],
                distance=race["distance"],
                surface=race["surface"],
                track_condition=race["track_condition"] or "良",
                weight=r["weight"],
                weight_change=r["weight_change"],
                impost=r["impost"],
                odds=r["odds"],
                popularity=r["popularity"],
                race_date=race_date,
                race_id=race_id,
                weather=race["weather"] if "weather" in race.keys() else None,
            )

            # ターゲット変数
            features["target_win"] = 1 if r["finish_position"] == 1 else 0
            features["target_top3"] = 1 if 1 <= r["finish_position"] <= 3 else 0
            features["finish_position"] = r["finish_position"]
            features["race_id"] = race_id
            features["horse_id"] = r["horse_id"]
            features["horse_number"] = r["horse_number"]

            # LambdaRank用: relevance label (着順の逆)
            hc = race["horse_count"] or len(results)
            if r["finish_position"] > 0:
                features["relevance"] = max(0, hc - r["finish_position"] + 1)
            else:
                features["relevance"] = 0

            rows.append(features)

        return pd.DataFrame(rows)

    def build_training_data(self, limit=None):
        """全レースの学習データを構築"""
        with get_db() as conn:
            # 確定結果のあるレースのみ対象
            query = """
                SELECT DISTINCT r.race_id FROM races r
                INNER JOIN results res ON r.race_id = res.race_id
                WHERE res.finish_position > 0
                ORDER BY r.race_date
            """
            if limit:
                query += f" LIMIT {limit}"
            race_ids = [row["race_id"] for row in conn.execute(query).fetchall()]

        print(f"📊 {len(race_ids)}レースの特徴量を構築中...")
        all_data = []

        for i, race_id in enumerate(race_ids):
            if (i + 1) % 100 == 0:
                print(f"  [{i+1}/{len(race_ids)}] 処理中...")
            df = self.build_features_for_race(race_id)
            if not df.empty and df["finish_position"].max() > 0:
                all_data.append(df)

        if not all_data:
            return pd.DataFrame()

        result = pd.concat(all_data, ignore_index=True)
        print(f"✅ 特徴量構築完了: {len(result)}行 × {len(result.columns)}列")
        return result

    # ── 新しいヘルパーメソッド ──

    def _get_past_performance(self, horse_id, race_date=None):
        """馬の過去成績を集計"""
        with get_db() as conn:
            if race_date:
                rows = conn.execute("""
                    SELECT r.finish_position
                    FROM results r
                    JOIN races ra ON r.race_id = ra.race_id
                    WHERE r.horse_id = ?
                      AND r.finish_position > 0
                      AND ra.race_date < ?
                    ORDER BY ra.race_date DESC
                    LIMIT 10
                """, (horse_id, race_date)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT r.finish_position
                    FROM results r
                    JOIN races ra ON r.race_id = ra.race_id
                    WHERE r.horse_id = ?
                      AND r.finish_position > 0
                    ORDER BY ra.race_date DESC
                    LIMIT 10
                """, (horse_id,)).fetchall()

        if not rows:
            return {
                "avg_finish_5r": 0,
                "win_rate_10r": 0,
                "top3_rate_10r": 0,
                "finish_trend": 0,
                "race_experience": 0,
            }

        positions = [r["finish_position"] for r in rows]
        last5 = positions[:5]
        total = len(positions)

        # 着順トレンド（直近3走の傾き: マイナス=上昇基調）
        trend = 0
        if len(last5) >= 3:
            recent3 = last5[:3]
            trend = (recent3[0] - recent3[2]) / 2  # 最新 - 3走前

        return {
            "avg_finish_5r": sum(last5) / len(last5) / 18,  # 正規化
            "win_rate_10r": sum(1 for p in positions if p == 1) / total,
            "top3_rate_10r": sum(1 for p in positions if p <= 3) / total,
            "finish_trend": np.clip(trend / 10, -1, 1),  # -1〜1
            "race_experience": min(total, 10) / 10,
        }

    def _get_context_features(self, horse_id, distance, surface, venue, jockey_id, race_date=None):
        """コンテキスト（条件変化）特徴量"""
        with get_db() as conn:
            date_filter = f"AND ra.race_date < '{race_date}'" if race_date else ""

            # 前走情報
            prev = conn.execute(f"""
                SELECT ra.distance, r.jockey_id, r.last_3f, r.weight, ra.surface
                FROM results r
                JOIN races ra ON r.race_id = ra.race_id
                WHERE r.horse_id = ?
                  AND r.finish_position > 0
                  {date_filter}
                ORDER BY ra.race_date DESC
                LIMIT 1
            """, (horse_id,)).fetchone()

            # 直近5走の上がり3F
            last3fs = conn.execute(f"""
                SELECT r.last_3f
                FROM results r
                JOIN races ra ON r.race_id = ra.race_id
                WHERE r.horse_id = ?
                  AND r.last_3f > 0
                  {date_filter}
                ORDER BY ra.race_date DESC
                LIMIT 5
            """, (horse_id,)).fetchall()

            # 同コースの成績
            course_stats = conn.execute(f"""
                SELECT COUNT(*) as total,
                       SUM(CASE WHEN r.finish_position <= 3 THEN 1 ELSE 0 END) as top3
                FROM results r
                JOIN races ra ON r.race_id = ra.race_id
                WHERE r.horse_id = ?
                  AND ra.venue = ? AND ra.surface = ?
                  AND ra.distance BETWEEN ? AND ?
                  AND r.finish_position > 0
                  {date_filter}
            """, (horse_id, venue, surface, distance - 200, distance + 200)).fetchone()

            # 直近3走の馬体重推移
            weights = conn.execute(f"""
                SELECT r.weight
                FROM results r
                JOIN races ra ON r.race_id = ra.race_id
                WHERE r.horse_id = ?
                  AND r.weight > 0
                  {date_filter}
                ORDER BY ra.race_date DESC
                LIMIT 3
            """, (horse_id,)).fetchall()

        ctx = {
            "distance_diff": 0,
            "jockey_change": 0,
            "course_top3_rate": 0,
            "last_3f_best": 0,
            "weight_trend": 0,
        }

        if prev:
            ctx["distance_diff"] = (distance - (prev["distance"] or distance)) / 400  # 正規化
            ctx["jockey_change"] = 1 if prev["jockey_id"] != jockey_id else 0

        if last3fs:
            vals = [r["last_3f"] for r in last3fs]
            ctx["last_3f_best"] = min(vals) / 40  # 正規化 (33-40秒の範囲)

        if course_stats and course_stats["total"] > 0:
            ctx["course_top3_rate"] = course_stats["top3"] / course_stats["total"]

        if len(weights) >= 2:
            ws = [r["weight"] for r in weights]
            ctx["weight_trend"] = (ws[0] - ws[-1]) / 20  # 正規化

        return ctx

    def _get_passing_orders(self, horse_id, n=5):
        """馬の直近N走の通過順を取得"""
        with get_db() as conn:
            rows = conn.execute("""
                SELECT r.passing_order
                FROM results r
                JOIN races ra ON r.race_id = ra.race_id
                WHERE r.horse_id = ?
                  AND r.passing_order IS NOT NULL
                  AND r.passing_order != ''
                ORDER BY ra.race_date DESC
                LIMIT ?
            """, (horse_id, n)).fetchall()
        return [row["passing_order"] for row in rows]

    def _get_rest_days(self, horse_id, race_date=None):
        """前走からの休養日数を取得"""
        with get_db() as conn:
            if race_date:
                rows = conn.execute("""
                    SELECT ra.race_date
                    FROM results r
                    JOIN races ra ON r.race_id = ra.race_id
                    WHERE r.horse_id = ?
                      AND ra.race_date < ?
                    ORDER BY ra.race_date DESC
                    LIMIT 1
                """, (horse_id, race_date)).fetchall()
            else:
                rows = conn.execute("""
                    SELECT ra.race_date
                    FROM results r
                    JOIN races ra ON r.race_id = ra.race_id
                    WHERE r.horse_id = ?
                    ORDER BY ra.race_date DESC
                    LIMIT 2
                """, (horse_id,)).fetchall()

        if not rows:
            return None

        try:
            from datetime import datetime
            if race_date:
                d_current = datetime.strptime(race_date, "%Y-%m-%d")
                d_prev = datetime.strptime(rows[0]["race_date"], "%Y-%m-%d")
                return (d_current - d_prev).days
            elif len(rows) >= 2:
                d1 = datetime.strptime(rows[0]["race_date"], "%Y-%m-%d")
                d2 = datetime.strptime(rows[1]["race_date"], "%Y-%m-%d")
                return (d1 - d2).days
        except (ValueError, TypeError):
            return None
        return None

    def _get_wet_track_stats(self, horse_id, race_date=None):
        """重馬場時のパフォーマンスを取得"""
        with get_db() as conn:
            date_filter = f"AND ra.race_date < '{race_date}'" if race_date else ""
            rows = conn.execute(f"""
                SELECT r.finish_position
                FROM results r
                JOIN races ra ON r.race_id = ra.race_id
                WHERE r.horse_id = ?
                  AND r.finish_position > 0
                  AND ra.track_condition IN ('重', '不', '不良')
                  {date_filter}
            """, (horse_id,)).fetchall()

        if not rows:
            return {"win_rate": 0, "top3_rate": 0}

        positions = [r["finish_position"] for r in rows]
        total = len(positions)
        return {
            "win_rate": sum(1 for p in positions if p == 1) / total,
            "top3_rate": sum(1 for p in positions if p <= 3) / total,
        }

    @staticmethod
    def _encode_distance(distance):
        """距離をカテゴリ値に変換"""
        if distance < 1400:
            return 0  # スプリント
        elif distance < 1800:
            return 1  # マイル
        elif distance < 2200:
            return 2  # 中距離
        elif distance < 2800:
            return 3  # 中長距離
        else:
            return 4  # 長距離

    def _get_horse_age(self, horse_id, race_date=None):
        """馬の年齢を取得。horse_idの先頭4桁が生年。競馬の年齢は数え年(1/1加齢)。"""
        try:
            birth_year = int(str(horse_id)[:4])
        except (ValueError, TypeError):
            return 4  # デフォルト

        if race_date:
            try:
                rd = str(race_date).replace('-', '')
                race_year = int(rd[:4])
            except (ValueError, TypeError):
                race_year = 2026
        else:
            race_year = 2026

        # 競馬の年齢: 生まれた年の翌年を2歳とする
        # 例: 2021年生まれ → 2026年で5歳
        age = race_year - birth_year
        return max(age, 2)  # 最低2歳

    def _get_course_post_position_bias(self, horse_number, venue, distance, surface, race_date=None):
        """同会場・同距離・同馬場での枠番別勝率を取得"""
        date_filter = f"AND ra.race_date < '{race_date}'" if race_date else ""
        with get_db() as conn:
            stats = conn.execute(f"""
                SELECT COUNT(*) as total,
                       SUM(CASE WHEN r.finish_position = 1 THEN 1 ELSE 0 END) as wins,
                       SUM(CASE WHEN r.finish_position <= 3 THEN 1 ELSE 0 END) as top3
                FROM results r
                JOIN races ra ON r.race_id = ra.race_id
                WHERE ra.venue = ? AND ra.surface = ?
                  AND ra.distance BETWEEN ? AND ?
                  AND r.horse_number = ?
                  AND r.finish_position > 0
                  {date_filter}
            """, (venue, surface, distance - 100, distance + 100, horse_number)).fetchone()

        if stats and stats["total"] >= 5:
            return {
                "win_rate": stats["wins"] / stats["total"],
                "top3_rate": stats["top3"] / stats["total"],
            }

        # サンプル不足時: 枠番グループで集計 (1-6内枠, 7-12中枠, 13-18外枠)
        if horse_number <= 6:
            hn_min, hn_max = 1, 6
        elif horse_number <= 12:
            hn_min, hn_max = 7, 12
        else:
            hn_min, hn_max = 13, 18

        with get_db() as conn:
            grp = conn.execute(f"""
                SELECT COUNT(*) as total,
                       SUM(CASE WHEN r.finish_position = 1 THEN 1 ELSE 0 END) as wins,
                       SUM(CASE WHEN r.finish_position <= 3 THEN 1 ELSE 0 END) as top3
                FROM results r
                JOIN races ra ON r.race_id = ra.race_id
                WHERE ra.venue = ? AND ra.surface = ?
                  AND ra.distance BETWEEN ? AND ?
                  AND r.horse_number BETWEEN ? AND ?
                  AND r.finish_position > 0
                  {date_filter}
            """, (venue, surface, distance - 100, distance + 100, hn_min, hn_max)).fetchone()

        if grp and grp["total"] > 0:
            return {
                "win_rate": grp["wins"] / grp["total"],
                "top3_rate": grp["top3"] / grp["total"],
            }
        return {"win_rate": 0.08, "top3_rate": 0.25}  # デフォルト

    def _get_race_grade(self, race_id):
        """レースのグレードを取得"""
        if not race_id:
            return ""
        with get_db() as conn:
            row = conn.execute(
                "SELECT grade FROM races WHERE race_id = ?", (race_id,)
            ).fetchone()
        return row["grade"] if row and row["grade"] else ""

    def _get_age_performance_by_class(self, horse_age, distance, grade, race_date=None):
        """同グレード・同距離帯での年齢別複勝率を取得"""
        date_filter = f"AND ra.race_date < '{race_date}'" if race_date else ""

        # グレードフィルタ: G1/G2/G3 はグレードレース、それ以外は一般
        if grade in ("G1", "G2", "G3"):
            grade_filter = "AND ra.grade IN ('G1','G2','G3')"
        else:
            grade_filter = "AND (ra.grade IS NULL OR ra.grade = '' OR ra.grade NOT IN ('G1','G2','G3'))"

        # horse_idの先頭4桁で生年を逆算して同年齢馬を検索
        # 年齢 = race_year - birth_year なので birth_year の範囲を特定
        with get_db() as conn:
            stats = conn.execute(f"""
                SELECT COUNT(*) as total,
                       SUM(CASE WHEN r.finish_position <= 3 THEN 1 ELSE 0 END) as top3
                FROM results r
                JOIN races ra ON r.race_id = ra.race_id
                WHERE ra.distance BETWEEN ? AND ?
                  AND r.finish_position > 0
                  AND (CAST(SUBSTR(ra.race_date, 1, 4) AS INT) - CAST(SUBSTR(r.horse_id, 1, 4) AS INT)) = ?
                  {grade_filter}
                  {date_filter}
            """, (distance - 200, distance + 200, horse_age)).fetchone()

        if stats and stats["total"] >= 10:
            return stats["top3"] / stats["total"]
        return 0.2  # デフォルト

    def _get_distance_specific_stats(self, horse_id, distance, surface, race_date=None):
        """同馬場・同距離(±100m)限定の勝率・複勝率を取得"""
        date_filter = f"AND ra.race_date < '{race_date}'" if race_date else ""
        with get_db() as conn:
            stats = conn.execute(f"""
                SELECT COUNT(*) as total,
                       SUM(CASE WHEN r.finish_position = 1 THEN 1 ELSE 0 END) as wins,
                       SUM(CASE WHEN r.finish_position <= 3 THEN 1 ELSE 0 END) as top3
                FROM results r
                JOIN races ra ON r.race_id = ra.race_id
                WHERE r.horse_id = ?
                  AND ra.surface = ?
                  AND ra.distance BETWEEN ? AND ?
                  AND r.finish_position > 0
                  {date_filter}
            """, (horse_id, surface, distance - 100, distance + 100)).fetchone()

        if stats and stats["total"] > 0:
            return {
                "win_rate": stats["wins"] / stats["total"],
                "top3_rate": stats["top3"] / stats["total"],
            }
        return {"win_rate": 0, "top3_rate": 0}

    @staticmethod
    def get_feature_columns():
        """学習に使用する特徴量カラム一覧 (v4: 枠順・年齢・距離強化)"""
        return [
            # スピード指数 (6)
            "si_avg", "si_max", "si_min", "si_std", "si_latest", "si_count",
            # 血統 (3)
            "pedigree_score", "sire_top3_rate", "sire_sample_size",
            # 騎手・調教師 (5)
            "jt_score", "jockey_cond_top3", "jockey_cond_win",
            "trainer_cond_top3", "combo_top3",
            # 馬場バイアス (2)
            "bias_score", "post_position_ratio",
            # ペース (3)
            "front_rate", "avg_pos_ratio", "avg_last_3f",
            # 馬情報 (7)
            "horse_count", "weight", "weight_change",
            "impost", "distance_cat", "surface_turf", "rest_days",
            # 過去成績 (5)
            "avg_finish_5r", "win_rate_10r", "top3_rate_10r",
            "finish_trend", "race_experience",
            # コンテキスト (5)
            "distance_diff", "jockey_change", "course_top3_rate",
            "last_3f_best", "weight_trend",
            # 天候・馬場 (5)
            "track_cond_code", "weather_code", "is_heavy_track",
            "horse_wet_win_rate", "horse_wet_top3_rate",
            # 年齢 (2)
            "horse_age", "is_peak_age",
            # コース別枠順 (2) ※新規
            "post_win_rate_course", "post_top3_rate_course",
            # グレード×距離×年齢 (1) ※新規
            "age_class_top3_rate",
            # 同距離限定成績 (2) ※新規
            "dist_win_rate", "dist_top3_rate",
        ]

