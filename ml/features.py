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
        features["weight"] = (weight or 0) / 500
        features["weight_change"] = (weight_change or 0) / 20
        # impostがNULL/None/'' でも float に強制変換(出馬表時点で未取得な場合あり)
        try:
            features["impost"] = float(impost) if impost is not None and impost != '' else 57.0
        except (TypeError, ValueError):
            features["impost"] = 57.0
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

        # ── 13. v5新規: 着差 (前走の走行品質) ──
        margin_stats = self._get_margin_avg(horse_id, race_date, n=5)
        features["margin_avg"] = margin_stats["margin_avg"]
        features["margin_best"] = margin_stats["margin_best"]

        # ── 14. v5新規: 同コース過去成績 (course familiarity) ──
        same_course = self._get_same_course_stats(horse_id, venue, surface, distance, race_date)
        features["same_course_top3_rate"] = same_course["same_course_top3_rate"]
        features["same_course_runs"] = same_course["same_course_runs"]

        # ── 15. v5新規: 斤量変化 (前走比) ──
        impost_d = self._get_impost_diff(horse_id, features.get("impost", 57.0), race_date)
        features["impost_diff"] = impost_d["impost_diff"]

        # ── 16. v5新規: 重賞経験 + 重賞複勝率 ──
        ge = self._get_grade_experience(horse_id, race_date)
        features["graded_exp"] = ge["graded_exp"]
        features["graded_top3_rate"] = ge["graded_top3_rate"]

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
        """馬の過去成績を集計（DB→馬ページ フォールバック付き）"""
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

        # DB過去走が少ない場合は馬ページからキャリア成績で補完
        db_total = len(rows) if rows else 0
        if db_total < 3:
            try:
                from scraper import NetkeibaScraper
                if not hasattr(self, '_scraper'):
                    self._scraper = NetkeibaScraper()
                career = self._scraper.scrape_horse_career(horse_id)
                if career and career['total_races'] > db_total:
                    # 馬ページの通算成績を使用（より完全なデータ）
                    if db_total > 0:
                        # DB着順データがある場合はトレンド計算に使う
                        positions = [r["finish_position"] for r in rows]
                        last5 = positions[:5]
                        trend = 0
                        if len(last5) >= 3:
                            recent3 = last5[:3]
                            trend = (recent3[0] - recent3[2]) / 2
                        avg_finish = sum(last5) / len(last5) / 18
                    else:
                        trend = 0
                        avg_finish = 0
                    return {
                        "avg_finish_5r": avg_finish,
                        "win_rate_10r": career['win_rate'],
                        "top3_rate_10r": career['top3_rate'],
                        "finish_trend": np.clip(trend / 10, -1, 1),
                        "race_experience": min(career['total_races'], 10) / 10,
                    }
            except Exception:
                pass

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

    # ─── v5 新規 features (機能していない features を置換) ──

    @staticmethod
    def _parse_margin(margin_str):
        """results.margin (テキスト) を着差(馬身)に変換。
        例: 'クビ'→0.15, 'アタマ'→0.05, 'ハナ'→0.02, '1/2'→0.5,
             '3/4'→0.75, '1'→1.0, '1 1/2'→1.5, '大差'→10.0
        """
        if not margin_str:
            return None
        s = str(margin_str).strip()
        if not s:
            return None
        japanese_map = {'ハナ': 0.02, 'アタマ': 0.05, 'クビ': 0.15,
                        '同着': 0.0, '大差': 10.0}
        if s in japanese_map:
            return japanese_map[s]
        # 1 1/2 のような表記
        try:
            if ' ' in s:
                parts = s.split()
                base = float(parts[0])
                if '/' in parts[1]:
                    num, den = parts[1].split('/')
                    return base + float(num) / float(den)
                return base
            if '/' in s:
                num, den = s.split('/')
                return float(num) / float(den)
            return float(s)
        except (ValueError, IndexError):
            return None

    def _get_margin_avg(self, horse_id, race_date=None, n=5):
        """直近 N 走の平均着差 (1着馬からの差・馬身)。
        着差が小さいほど高品質な走り。"""
        with get_db() as conn:
            date_filter = "AND ra.race_date < ?" if race_date else ""
            params = (horse_id,) + ((race_date,) if race_date else ())
            rows = conn.execute(f"""
                SELECT r.margin, r.finish_position
                FROM results r
                JOIN races ra ON r.race_id = ra.race_id
                WHERE r.horse_id = ? AND r.finish_position > 0 {date_filter}
                ORDER BY ra.race_date DESC
                LIMIT ?
            """, params + (n,)).fetchall()
        if not rows:
            return {'margin_avg': 5.0, 'margin_best': 5.0}
        margins = []
        for r in rows:
            m = self._parse_margin(r['margin'])
            if m is None:
                continue
            # 1着馬は margin が空のことが多い → 0 として扱う
            if r['finish_position'] == 1:
                margins.append(0.0)
            else:
                margins.append(min(m, 10.0))
        if not margins:
            return {'margin_avg': 5.0, 'margin_best': 5.0}
        return {
            'margin_avg': sum(margins) / len(margins),
            'margin_best': min(margins),
        }

    def _get_same_course_stats(self, horse_id, venue, surface, distance, race_date=None):
        """同馬の同コース(venue+surface+distance) 過去成績。"""
        with get_db() as conn:
            date_filter = "AND ra.race_date < ?" if race_date else ""
            params = (horse_id, venue, surface, distance)
            if race_date:
                params = params + (race_date,)
            stats = conn.execute(f"""
                SELECT COUNT(*) as total,
                       SUM(CASE WHEN r.finish_position = 1 THEN 1 ELSE 0 END) as wins,
                       SUM(CASE WHEN r.finish_position <= 3 THEN 1 ELSE 0 END) as top3
                FROM results r
                JOIN races ra ON r.race_id = ra.race_id
                WHERE r.horse_id = ? AND ra.venue = ? AND ra.surface = ?
                  AND ra.distance = ? AND r.finish_position > 0 {date_filter}
            """, params).fetchone()
        if stats and stats['total'] > 0:
            return {
                'same_course_top3_rate': stats['top3'] / stats['total'],
                'same_course_runs': min(stats['total'], 10) / 10,
            }
        return {'same_course_top3_rate': 0.0, 'same_course_runs': 0.0}

    def _get_impost_diff(self, horse_id, current_impost, race_date=None):
        """前走比 斤量変化(kg)。重くなれば負担増→不利、軽くなれば有利。"""
        if not current_impost:
            return {'impost_diff': 0.0}
        with get_db() as conn:
            date_filter = "AND ra.race_date < ?" if race_date else ""
            params = (horse_id,) + ((race_date,) if race_date else ())
            row = conn.execute(f"""
                SELECT r.impost
                FROM results r
                JOIN races ra ON r.race_id = ra.race_id
                WHERE r.horse_id = ? AND r.finish_position > 0 {date_filter}
                ORDER BY ra.race_date DESC LIMIT 1
            """, params).fetchone()
        if row and row['impost']:
            return {'impost_diff': float(current_impost) - float(row['impost'])}
        return {'impost_diff': 0.0}

    def _get_grade_experience(self, horse_id, race_date=None):
        """重賞(G1/G2/G3) 出走経験 + 複勝経験。"""
        with get_db() as conn:
            date_filter = "AND ra.race_date < ?" if race_date else ""
            params = (horse_id,) + ((race_date,) if race_date else ())
            row = conn.execute(f"""
                SELECT
                  SUM(CASE WHEN ra.grade IN ('G1','G2','G3') THEN 1 ELSE 0 END) as graded_runs,
                  SUM(CASE WHEN ra.grade IN ('G1','G2','G3') AND r.finish_position <= 3
                           THEN 1 ELSE 0 END) as graded_top3
                FROM results r
                JOIN races ra ON r.race_id = ra.race_id
                WHERE r.horse_id = ? AND r.finish_position > 0 {date_filter}
            """, params).fetchone()
        if row:
            runs = row['graded_runs'] or 0
            top3 = row['graded_top3'] or 0
            return {
                'graded_exp': min(runs, 10) / 10,
                'graded_top3_rate': (top3 / runs) if runs > 0 else 0.0,
            }
        return {'graded_exp': 0.0, 'graded_top3_rate': 0.0}

    @staticmethod
    def get_feature_columns():
        """学習に使用する特徴量カラム一覧 (v5: 役立たない9個を削除 + 5個新規追加)

        v4 → v5 変更:
          削除(モデルで重要度0だった): pedigree_score, sire_top3_rate, sire_sample_size,
                                        bias_score, horse_age, is_peak_age,
                                        age_class_top3_rate, is_heavy_track, post_win_rate_course

          追加(機会損失だった): margin_avg, margin_best, same_course_top3_rate,
                                same_course_runs, impost_diff, graded_exp, graded_top3_rate
        """
        return [
            # スピード指数 (6)
            "si_avg", "si_max", "si_min", "si_std", "si_latest", "si_count",
            # 騎手・調教師 (5) ← 最重要群
            "jt_score", "jockey_cond_top3", "jockey_cond_win",
            "trainer_cond_top3", "combo_top3",
            # 枠順 (1)
            "post_position_ratio",
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
            # 天候・馬場 (3)
            "track_cond_code", "weather_code",
            "horse_wet_win_rate", "horse_wet_top3_rate",
            # コース別枠順 (1)
            "post_top3_rate_course",
            # 同距離限定成績 (2)
            "dist_win_rate", "dist_top3_rate",
            # v5新規 - 着差(前走の質を示す強シグナル)
            "margin_avg", "margin_best",
            # v5新規 - 同馬の同コース過去成績(course familiarity)
            "same_course_top3_rate", "same_course_runs",
            # v5新規 - 斤量変化
            "impost_diff",
            # v5新規 - 重賞経験 + 重賞複勝率
            "graded_exp", "graded_top3_rate",
        ]

