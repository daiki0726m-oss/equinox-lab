"""
スピード指数計算モジュール
走破タイムを馬場・距離・コース形状で補正し統一的な能力指数を算出
"""

import json
import os

import pandas as pd
import numpy as np
from database import get_db


# ══════════════════════════════════════════════════════════════════════
# #169: 馬場補正表のキー不一致 (LGB の si_* 特徴量)
# ══════════════════════════════════════════════════════════════════════
# DB の races.track_condition は「良/稍/重/不」の1文字表記 (race_meta.normalize_track_condition。
# 2020-2026 の全 23,600 レースがこの4種)。旧来の補正表のキーは「稍重/不良」なので、
# **稍 (-1.5) と 不 (-5.0) の補正は導入以来一度もかかっていない** (キーが一致する 重 -3.0 だけ効く)。
# 学習側 (fast_train._SpeedIndexCache) と推論側 (この SpeedIndexCalculator) が同じ表を
# 共有しているので train/serve skew にはなっていない — 「両側とも稍・不は補正なし」で一致している。
#
# 正しいキーの表 (CANONICAL) を推論側だけに入れると、補正なしで学習した現行モデルに
# 別物の si_* を渡す skew になる。そこで「どちらの表で学習したか」をモデルと一緒に
# models/train_boundary.json の "si_track_adj" に保存し、推論側はそれに従う:
#   - フラグ無し (#169 より前に学習したモデル) / "legacy" → 旧表 (稍・不は補正なし)
#   - "canonical" → 正しいキーの表
# 学習側の既定 SI_TRACK_ADJ_TRAIN_DEFAULT は OOS A/B (下記) の結果で決めた。
#
# OOS A/B (2026-11-05): 学習 race_date < 2025-06-23 (scripts/oos_window の rank 系境界、18,932R)、
# 評価 2025-06-28〜2026-11-01 (4,731R / 63,900頭)。fast_train と同じパラメータ・手順 (時系列 80/20 で
# early stopping)、seed 42 と 7、レース単位 bootstrap 500回。canonical で si_* が変わる行は 49%。
# canonical − legacy (seed42 / seed7、[]は95%区間):
#   AUC(複勝)            -0.00002 [-0.0003,+0.0003] / +0.00016 [-0.0001,+0.0005]
#   AUC(単勝)            -0.00005 [-0.0007,+0.0007] / +0.00008 [-0.0006,+0.0007]
#   複勝モデル1位の3着内率 +0.49pt [-0.08,+1.08]    / +0.25pt [-0.23,+0.80]
#   上位5頭の3着内捕捉数   -0.0015 [-0.008,+0.004]   / +0.0004 [-0.005,+0.007]
#   単勝モデル1位の勝率    +0.28pt [-0.20,+0.74]    / -0.47pt [-0.99,+0.06] (seed で符号が逆)
#   (seed だけ変えた legacy 同士の差: AUC 0.0001-0.0002 / 上位5捕捉 0.005 / rank 1位勝率 0.8pt)
# → どれも区間が0をまたぎ、seed のブレと同じ大きさ。基準タイムが既に「場×距離×芝ダ×馬場」別なので、
#   馬場で一律に引く補正は情報を足さない。**補正表 (稍・不) は効いておらず、効かせても改善しない
#   ので使わない** (既定は legacy のまま。仕組みは残すので、canonical にするなら A/B をやり直してから)。
# 点数表 (ml/scorecard、V2d の穴埋め) が使う ml.speed_variant.legacy_si は対象外
# (点数表は旧表で当てはめ済み。変えるなら点数表の当てはめ直しとセットで行う)。
# 能力モデル (models/model_ability_win.pkl、weekly_retrain の対象外) も FeatureBuilder の si_* を
# 読むので、canonical に切り替える時は能力モデルも同じ表で学習し直すこと。
TRACK_ADJUST_LEGACY = {"良": 0, "稍重": -1.5, "重": -3.0, "不良": -5.0}
TRACK_ADJUST_CANONICAL = {"良": 0, "稍": -1.5, "稍重": -1.5, "重": -3.0, "不": -5.0, "不良": -5.0}
SI_TRACK_ADJ_MODES = ("legacy", "canonical")
SI_TRACK_ADJ_TRAIN_DEFAULT = "legacy"
_BOUNDARY_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                              "models", "train_boundary.json")


def si_track_adjust(mode):
    """mode ("legacy"/"canonical") に対応する馬場補正表を返す。不明な値は legacy。"""
    return dict(TRACK_ADJUST_CANONICAL if mode == "canonical" else TRACK_ADJUST_LEGACY)


def train_si_track_mode():
    """学習側 (fast_train) が LGB の si_* を作る時の馬場補正の方式。
    環境変数 SI_TRACK_ADJ=legacy|canonical で上書きできる (A/B 用)。"""
    m = (os.environ.get("SI_TRACK_ADJ") or "").strip().lower() or SI_TRACK_ADJ_TRAIN_DEFAULT
    return m if m in SI_TRACK_ADJ_MODES else "legacy"


def model_si_track_mode(boundary_path=None):
    """出荷中の LGB モデルが学習時に使った馬場補正の方式 (models/train_boundary.json)。
    フラグが無い・読めない時は legacy (= #169 より前に学習したモデルと同じ扱い)。"""
    try:
        with open(boundary_path or _BOUNDARY_PATH, encoding="utf-8") as f:
            m = (json.load(f) or {}).get("si_track_adj") or "legacy"
    except Exception:
        m = "legacy"
    return m if m in SI_TRACK_ADJ_MODES else "legacy"


class SpeedIndexCalculator:
    """
    スピード指数 = (基準タイム - 走破タイム) × 距離補正 + 馬場差補正 + ベース値(80)

    基準タイム: 各競馬場・距離・馬場状態の平均走破タイム
    距離補正: 短距離ほど1秒の価値が大きい
    馬場差補正: 重馬場等の影響を数値化
    """

    BASE_INDEX = 80  # 指数の基準値

    # 馬場状態の補正値 (良を基準)
    # #169: キーが「稍重/不良」で DB の「稍/不」と一致しない旧表 (稍・不は補正なし)。
    # 既定はこの旧表のまま。正しいキーの表を使うかは track_adjust 引数で渡す (上の説明)。
    TRACK_CONDITION_ADJUST = dict(TRACK_ADJUST_LEGACY)

    # 距離帯別の1秒あたり指数変動値
    DISTANCE_FACTOR = {
        (0, 1200): 15.0,      # スプリント
        (1200, 1600): 12.0,   # マイル
        (1600, 2000): 10.0,   # 中距離
        (2000, 2500): 8.5,    # 中長距離
        (2500, 9999): 7.0,    # 長距離
    }

    def __init__(self, track_adjust=None):
        self.base_times = {}  # キャッシュ
        # #169: LGB の特徴量を作る FeatureBuilder は、出荷中のモデルが学習時に使った表
        # (model_si_track_mode) を渡す。None は従来どおり旧表。
        self.track_adjust = (dict(track_adjust) if track_adjust is not None
                             else dict(self.TRACK_CONDITION_ADJUST))

    def _get_distance_factor(self, distance):
        """距離帯に応じた補正係数を取得"""
        for (low, high), factor in self.DISTANCE_FACTOR.items():
            if low <= distance < high:
                return factor
        return 10.0

    def _get_base_time(self, venue, distance, surface, track_condition="良", before=None):
        """基準タイムを取得 (DBから平均タイムを計算)。

        before (YYYY-MM-DD): その日より前のレースだけで基準タイムを作る (#165)。
        予想するレースの日付を渡せば「予想時点で分かっていたデータ」だけになる。
        学習側 (fast_train.build_speed_index_cache) も同じ規則 (同じ件数の閾値・同じ
        フォールバック) で作るので、学習と本番で同じ値になる (パリティ検査 #135)。
        before=None は従来どおり全期間 (表示用の calculate_race など)。
        """
        key = (venue, distance, surface, track_condition, before)
        if key in self.base_times:
            return self.base_times[key]

        date_cond = "AND ra.race_date < ?" if before else ""
        date_arg = (before,) if before else ()
        with get_db() as conn:
            row = conn.execute(f"""
                SELECT AVG(r.finish_time_seconds) as avg_time,
                       COUNT(*) as cnt
                FROM results r
                JOIN races ra ON r.race_id = ra.race_id
                WHERE ra.venue = ? AND ra.distance = ? AND ra.surface = ?
                  AND ra.track_condition = ?
                  AND r.finish_time_seconds > 0
                  AND r.finish_position BETWEEN 1 AND 5
                  {date_cond}
            """, (venue, distance, surface, track_condition) + date_arg).fetchone()

            if row and row["cnt"] >= 5:
                base_time = row["avg_time"]
            else:
                # データ不足の場合は馬場状態を問わず平均を使用
                row2 = conn.execute(f"""
                    SELECT AVG(r.finish_time_seconds) as avg_time,
                           COUNT(*) as cnt
                    FROM results r
                    JOIN races ra ON r.race_id = ra.race_id
                    WHERE ra.venue = ? AND ra.distance = ? AND ra.surface = ?
                      AND r.finish_time_seconds > 0
                      AND r.finish_position BETWEEN 1 AND 5
                      {date_cond}
                """, (venue, distance, surface) + date_arg).fetchone()

                if row2 and row2["cnt"] >= 3:
                    base_time = row2["avg_time"]
                else:
                    # さらにデータ不足の場合は距離のみで計算
                    row3 = conn.execute(f"""
                        SELECT AVG(r.finish_time_seconds) as avg_time
                        FROM results r
                        JOIN races ra ON r.race_id = ra.race_id
                        WHERE ra.distance = ? AND ra.surface = ?
                          AND r.finish_time_seconds > 0
                          AND r.finish_position BETWEEN 1 AND 5
                          {date_cond}
                    """, (distance, surface) + date_arg).fetchone()
                    base_time = row3["avg_time"] if row3 and row3["avg_time"] else distance * 0.06

        self.base_times[key] = base_time
        return base_time

    def calculate(self, finish_time_seconds, venue, distance, surface, track_condition="良",
                  before=None):
        """スピード指数を1頭分計算 (before: 基準タイムをその日より前のデータで作る #165)"""
        if finish_time_seconds <= 0:
            return 0

        base_time = self._get_base_time(venue, distance, surface, track_condition, before)
        if not base_time or base_time <= 0:
            return 0

        # タイム差(秒)
        time_diff = base_time - finish_time_seconds

        # 距離ファクター
        dist_factor = self._get_distance_factor(distance)

        # 馬場差補正
        track_adjust = self.track_adjust.get(track_condition, 0)

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

    def get_horse_indices(self, horse_id, n_races=5, race_date=None):
        """馬の直近N走のスピード指数を取得。

        #135 (2026-09-01): race_date を受けて **それより前のレースだけ**を見る。
        旧実装は日付フィルタが無く、過去レースを再予測すると**そのレース以降の
        走破時計まで混入**していた (未走の未来レースは finish_time_seconds=0 で
        弾かれるため当日予測は無害だが、過去日の再 predict では実害)。
        #26 で「5/9 の ROI 339% は lucky day」とした異常値も、この経路で説明がつく。
        """
        date_filter = "AND ra.race_date < ?" if race_date else ""
        params = [horse_id] + ([race_date] if race_date else []) + [n_races]
        with get_db() as conn:
            rows = conn.execute(f"""
                SELECT r.*, ra.venue, ra.distance, ra.surface, ra.track_condition
                FROM results r
                JOIN races ra ON r.race_id = ra.race_id
                WHERE r.horse_id = ?
                  AND r.finish_time_seconds > 0
                  {date_filter}
                ORDER BY ra.race_date DESC
                LIMIT ?
            """, params).fetchall()

        indices = []
        for row in rows:
            # #165: 基準タイムも予想するレースの日付より前のデータだけで作る
            idx = self.calculate(
                row["finish_time_seconds"],
                row["venue"], row["distance"],
                row["surface"], row["track_condition"] or "良",
                before=race_date,
            )
            indices.append(idx)

        return indices

    def get_horse_stats(self, horse_id, n_races=5, race_date=None):
        """馬のスピード指数の統計値を取得 (#135: race_date で未来を遮断)"""
        indices = self.get_horse_indices(horse_id, n_races, race_date=race_date)
        if not indices:
            return {"avg": 0, "max": 0, "min": 0, "std": 0, "latest": 0, "count": 0}

        # #135: 学習側 (fast_train) は np.mean をそのまま使うため、ここで丸めると
        # 同じ特徴量名に別の値が入る (パリティ検査で一致率0%として検出された)。
        return {
            "avg": float(np.mean(indices)),
            "max": float(max(indices)),
            "min": float(min(indices)),
            "std": float(np.std(indices)) if len(indices) > 1 else 0,
            "latest": indices[0],
            "count": len(indices),
        }
