"""
高速モデル学習スクリプト
- 全データを一括でメモリにロード（個別クエリを排除）
- プリコンパイル済み特徴量をキャッシュ
- 学習 → バックテストまで一気に実行
"""

import sys
import os
import time
import numpy as np
import pandas as pd
import lightgbm as lgb
from collections import defaultdict

sys.path.insert(0, os.path.dirname(__file__))
from database import init_db, get_db

MODEL_DIR = os.path.join(os.path.dirname(__file__), "models")


def load_all_data():
    """全データをメモリに一括ロード"""
    print("📦 全データをメモリにロード中...")
    t0 = time.time()

    with get_db() as conn:
        races_df = pd.read_sql("SELECT * FROM races WHERE race_date IS NOT NULL ORDER BY race_date", conn)
        results_df = pd.read_sql("""
            SELECT r.*, h.horse_name, h.sire, h.damsire, h.birth_year
            FROM results r
            LEFT JOIN horses h ON r.horse_id = h.horse_id
            ORDER BY r.race_id, r.horse_number
        """, conn)
        payouts_df = pd.read_sql("SELECT * FROM payouts", conn)

    elapsed = time.time() - t0
    print(f"  ✅ {len(races_df)}レース, {len(results_df)}件のデータをロード ({elapsed:.1f}秒)")
    return races_df, results_df, payouts_df


def build_horse_history(results_df, races_df):
    """馬ごとの過去レース履歴をプリコンパイル。
    v5: venue/surface/distance/grade/track_condition も merge して
        same_course / graded_exp などの新 feature 計算に必要なフィールドを揃える。
    """
    print("🔧 馬の履歴データを構築中...")
    t0 = time.time()

    # race info を results に merge (v5 新 features 用)
    needed_race_cols = ["race_id", "race_date", "venue", "surface", "distance",
                        "grade", "track_condition", "horse_count"]
    avail = [c for c in needed_race_cols if c in races_df.columns]
    res = results_df.merge(races_df[avail], on="race_id", how="left", suffixes=("", "_r"))
    res = res.sort_values(["horse_id", "race_date"])

    # 馬ごとのデータをグループ化
    history = {}
    for horse_id, group in res.groupby("horse_id"):
        history[horse_id] = group.to_dict("records")

    elapsed = time.time() - t0
    print(f"  ✅ {len(history)}頭の履歴を構築 ({elapsed:.1f}秒)")
    return history


def build_jockey_trainer_stats(results_df, races_df):
    """騎手・調教師の成績統計をプリコンパイル (temporal-safe, v6)。

    ⚠️ v5 までは全期間集計で temporal leakage が発生していた:
        2020年のレースに対する combo_top3 が、2020-2025年の全データから
        計算され、未来のデータがモデル学習に漏れていた。

    v6 では、各 (jockey, trainer) について時系列順に累積カウントを保持し、
    実行時に「対象 race_date 直前の累積値」を取り出すことで leakage を排除。

    Returns:
        jockey_stats: {jid: [(race_date, win_cum, top3_cum, total_cum), ...]}
        trainer_stats: {tid: [(race_date, top3_cum, total_cum), ...]}
        combo_stats: {(jid, tid): [(race_date, top3_cum, total_cum), ...]}

    feature 計算時は `lookup_stat_at()` で対象 race_date より前の最新値を取る。
    """
    print("🔧 騎手・調教師統計を構築中(temporal-safe v6)...")
    t0 = time.time()

    needed_cols = ["venue", "distance", "surface", "track_condition", "race_date"]
    avail = [c for c in needed_cols if c in races_df.columns]
    # race_date が results_df に無い場合は必ず merge する(temporal累積に必須)
    if "race_date" not in results_df.columns:
        res = results_df.merge(races_df[["race_id"] + avail], on="race_id", how="left")
    elif all(c in results_df.columns for c in needed_cols):
        res = results_df.copy()
    else:
        # race_date はあるが他が無い場合のみ merge
        missing = [c for c in avail if c not in results_df.columns]
        if missing:
            res = results_df.merge(races_df[["race_id"] + missing], on="race_id", how="left")
        else:
            res = results_df.copy()
    confirmed = res[res["finish_position"] > 0].copy()
    if "race_date" not in confirmed.columns:
        raise RuntimeError("temporal-safe stat build には race_date が必須")
    confirmed = confirmed.sort_values("race_date")

    # 騎手別: 時系列累積
    jockey_stats = {}
    for jid, g in confirmed.groupby("jockey_id"):
        if not jid:
            continue
        win_cum = 0
        top3_cum = 0
        total_cum = 0
        records = []
        for _, row in g.iterrows():
            # NOTE: ここで「行の値を見る前」に records.append すると、
            # その行はまだ未確定 = race_date 当日まで「行を見ない」状態の累積になる
            records.append((row["race_date"], win_cum, top3_cum, total_cum))
            # この行を反映
            total_cum += 1
            if row["finish_position"] == 1:
                win_cum += 1
            if row["finish_position"] <= 3:
                top3_cum += 1
        jockey_stats[jid] = records

    # 調教師別
    trainer_stats = {}
    for tid, g in confirmed.groupby("trainer_id"):
        if not tid:
            continue
        top3_cum = 0
        total_cum = 0
        records = []
        for _, row in g.iterrows():
            records.append((row["race_date"], top3_cum, total_cum))
            total_cum += 1
            if row["finish_position"] <= 3:
                top3_cum += 1
        trainer_stats[tid] = records

    # 騎手×調教師コンビ
    combo_stats = {}
    for (jid, tid), g in confirmed.groupby(["jockey_id", "trainer_id"]):
        if not jid or not tid:
            continue
        top3_cum = 0
        total_cum = 0
        records = []
        for _, row in g.iterrows():
            records.append((row["race_date"], top3_cum, total_cum))
            total_cum += 1
            if row["finish_position"] <= 3:
                top3_cum += 1
        combo_stats[(jid, tid)] = records

    elapsed = time.time() - t0
    print(f"  ✅ 騎手{len(jockey_stats)}, 調教師{len(trainer_stats)}, "
          f"コンビ{len(combo_stats)} (temporal累積, {elapsed:.1f}秒)")
    return jockey_stats, trainer_stats, combo_stats


def _lookup_at(records, target_date, kind):
    """records (時系列累積) から target_date 直前の累積値を取り出す。

    Args:
        records: jockey_stats[jid] or trainer_stats[tid] or combo_stats[(jid,tid)]
        target_date: 対象レースの race_date (str 'YYYY-MM-DD')
        kind: 'jockey'|'trainer'|'combo'
    Returns:
        dict like {'win_rate':..., 'top3_rate':..., 'total':...} (kind に応じて)
    """
    import bisect
    if not records or not target_date:
        return _empty_stat(kind)
    # records is list of tuples, first element = race_date
    # bisect で target_date 未満の最後のインデックスを探す
    dates = [r[0] for r in records]
    idx = bisect.bisect_left(dates, target_date)
    if idx == 0:
        return _empty_stat(kind)
    rec = records[idx - 1]
    if kind == 'jockey':
        _, win_cum, top3_cum, total_cum = rec
        if total_cum < 5:
            return {'win_rate': 0, 'top3_rate': 0, 'total': total_cum}
        return {'win_rate': win_cum / total_cum, 'top3_rate': top3_cum / total_cum, 'total': total_cum}
    elif kind == 'trainer':
        _, top3_cum, total_cum = rec
        if total_cum < 5:
            return {'top3_rate': 0, 'total': total_cum}
        return {'top3_rate': top3_cum / total_cum, 'total': total_cum}
    else:  # combo
        _, top3_cum, total_cum = rec
        if total_cum < 3:
            return {'top3_rate': 0, 'total': total_cum}
        return {'top3_rate': top3_cum / total_cum, 'total': total_cum}


def _empty_stat(kind):
    if kind == 'jockey':
        return {'win_rate': 0, 'top3_rate': 0, 'total': 0}
    if kind == 'trainer':
        return {'top3_rate': 0, 'total': 0}
    return {'top3_rate': 0, 'total': 0}


def build_pedigree_cross_stats(results_df, races_df):
    """v7: 血統 × コース cross 集計 (temporal-safe)

    旧 sire_top3_rate は「全期間全コースの sire 通算」だったので
    ノイズで重要度0だった。v7 では:

      - sire × (venue, surface, distance) — 最も具体的(その馬の種牡馬は
        このコースで何頭走って何頭3着内?)
      - sire × surface — 芝/ダ別の汎適性(サンプル多め)
      - damsire 同様 2軸

    すべて temporal cumulative (race_date 直前の累積のみ参照)。

    Returns: dict of 4 lookup tables
    """
    print("🔧 血統cross統計を構築中(v7 temporal-safe)...")
    t0 = time.time()

    needed = ["race_date", "venue", "surface", "distance"]
    avail = [c for c in needed if c in races_df.columns]
    if "race_date" not in results_df.columns:
        df = results_df.merge(races_df[["race_id"] + avail], on="race_id", how="left")
    else:
        df = results_df.copy()

    df = df[(df["finish_position"] > 0) & (df["sire"].notna() | df["damsire"].notna())]
    df = df.sort_values("race_date")

    sire_course = {}    # key=(sire, venue, surface, distance) → [(date, t3_cum, total_cum), ...]
    sire_surface = {}   # key=(sire, surface)
    damsire_course = {} # key=(damsire, venue, surface, distance)
    damsire_surface = {}# key=(damsire, surface)

    def _push(d, key, race_date, finish):
        if key not in d:
            d[key] = [(race_date, 0, 0)]
            prev = (0, 0)
        else:
            prev = (d[key][-1][1], d[key][-1][2])
            d[key].append((race_date, prev[0], prev[1]))
        # 当該行を反映 (次の検索で見える累積)
        d[key][-1] = (race_date, prev[0] + (1 if finish <= 3 else 0), prev[1] + 1)

    for row in df.itertuples():
        sire = getattr(row, 'sire', None) or ''
        damsire = getattr(row, 'damsire', None) or ''
        venue = getattr(row, 'venue', None) or ''
        surface = getattr(row, 'surface', None) or ''
        distance = getattr(row, 'distance', None) or 0
        rd = row.race_date
        fp = row.finish_position

        if sire:
            _push(sire_course, (sire, venue, surface, distance), rd, fp)
            _push(sire_surface, (sire, surface), rd, fp)
        if damsire:
            _push(damsire_course, (damsire, venue, surface, distance), rd, fp)
            _push(damsire_surface, (damsire, surface), rd, fp)

    elapsed = time.time() - t0
    print(f"  ✅ sire_course{len(sire_course)}, sire_surface{len(sire_surface)}, "
          f"damsire_course{len(damsire_course)}, damsire_surface{len(damsire_surface)} "
          f"({elapsed:.1f}秒)")
    return sire_course, sire_surface, damsire_course, damsire_surface


def _lookup_pedigree(records, target_date, min_runs=5):
    """血統 cross 累積から target_date 直前の top3 率を取得。
    サンプル数 < min_runs なら 0 を返す(信頼度なし)。
    """
    import bisect
    if not records:
        return 0.0
    dates = [r[0] for r in records]
    idx = bisect.bisect_left(dates, target_date)
    if idx == 0:
        return 0.0
    _, t3_cum, total_cum = records[idx - 1]
    # 当該位置は「その行までを含む累積」だが target_date より前のものなので使ってOK
    if total_cum < min_runs:
        return 0.0
    return t3_cum / total_cum


def build_speed_index_cache(results_df, races_df):
    """スピード指数をプリコンパイル"""
    print("🔧 スピード指数を計算中...")
    t0 = time.time()

    # results_dfにはmain()でrace_infoカラムが追加済みの場合がある
    needed_cols = ["venue", "distance", "surface", "track_condition"]
    if all(c in results_df.columns for c in needed_cols):
        res = results_df.copy()
    else:
        res = results_df.merge(
            races_df[["race_id"] + needed_cols],
            on="race_id", how="left"
        )
    confirmed = res[(res["finish_position"] > 0) & (res["finish_time_seconds"] > 0)].copy()

    # 基準タイム: venue-distance-surface-condition の上位5着平均
    top5 = confirmed[confirmed["finish_position"] <= 5]
    base_times = top5.groupby(["venue", "distance", "surface", "track_condition"])[
        "finish_time_seconds"
    ].mean().to_dict()

    # フォールバック: venue-distance-surface
    base_times_fallback = top5.groupby(["venue", "distance", "surface"])[
        "finish_time_seconds"
    ].mean().to_dict()

    # 距離ファクター
    def get_dist_factor(d):
        if d < 1200: return 15.0
        elif d < 1600: return 12.0
        elif d < 2000: return 10.0
        elif d < 2500: return 8.5
        else: return 7.0

    # 馬場補正
    track_adj = {"良": 0, "稍重": -1.5, "重": -3.0, "不良": -5.0}

    # 各出走のSI計算
    si_cache = {}
    for _, row in confirmed.iterrows():
        key = (row["venue"], row["distance"], row["surface"], row["track_condition"])
        bt = base_times.get(key)
        if bt is None:
            bt = base_times_fallback.get((row["venue"], row["distance"], row["surface"]))
        if bt is None or bt <= 0:
            continue

        time_diff = bt - row["finish_time_seconds"]
        df = get_dist_factor(row["distance"])
        ta = track_adj.get(row["track_condition"], 0)
        si = 80 + (time_diff * df) + ta

        if row["horse_id"] not in si_cache:
            si_cache[row["horse_id"]] = []
        si_cache[row["horse_id"]].append({
            "si": round(si, 1),
            "race_date": row.get("race_date", ""),
        })

    # 日付順にソート
    for hid in si_cache:
        si_cache[hid].sort(key=lambda x: x["race_date"], reverse=True)

    elapsed = time.time() - t0
    print(f"  ✅ {len(si_cache)}頭のSIを計算 ({elapsed:.1f}秒)")
    return si_cache


def compute_features_fast(race, race_results, horse_history, jockey_stats,
                          trainer_stats, combo_stats, si_cache,
                          sire_course=None, sire_surface=None,
                          damsire_course=None, damsire_surface=None):
    """1レース分の特徴量を高速計算

    v7: pedigree_cross 引数を追加(temporal-safe 血統 cross stats)。
    後方互換のため省略可。
    """
    rows = []
    race_date = race["race_date"]
    hc = race["horse_count"] or len(race_results)
    # race-level info (cross 集計の lookup キー)
    venue = race.get("venue", "")
    surface = race.get("surface", "")
    distance = race.get("distance", 0) or 0

    for r in race_results:
        f = {}
        horse_id = r["horse_id"]
        jockey_id = r.get("jockey_id", "")
        trainer_id = r.get("trainer_id", "")

        # --- 過去レースをフィルタ（未来リーク防止）---
        past_races = []
        if horse_id in horse_history:
            past_races = [h for h in horse_history[horse_id]
                         if h.get("race_date", "") < race_date
                         and h.get("finish_position", 0) > 0]

        # === SI系 (6) ===
        si_list = []
        if horse_id in si_cache:
            si_list = [s["si"] for s in si_cache[horse_id]
                      if s["race_date"] < race_date][:5]

        f["si_avg"] = np.mean(si_list) if si_list else 0
        f["si_max"] = max(si_list) if si_list else 0
        f["si_min"] = min(si_list) if si_list else 0
        f["si_std"] = np.std(si_list) if len(si_list) > 1 else 0
        f["si_latest"] = si_list[0] if si_list else 0
        f["si_count"] = len(si_list)

        # === 血統系 (3) ===
        f["pedigree_score"] = 50
        f["sire_top3_rate"] = 0
        f["sire_sample_size"] = 0

        # === 騎手・調教師系 (5) — v6 temporal-safe ===
        # ⚠️ race_date より前の累積のみを使う(leakage 防止)
        js = _lookup_at(jockey_stats.get(jockey_id, []), race_date, 'jockey')
        ts = _lookup_at(trainer_stats.get(trainer_id, []), race_date, 'trainer')
        cs = _lookup_at(combo_stats.get((jockey_id, trainer_id), []), race_date, 'combo')

        jt_score = 50
        if js.get("total", 0) >= 30:
            jt_score += (js["top3_rate"] * 100 - 25) * 0.3
        if ts.get("total", 0) >= 20:
            jt_score += (ts["top3_rate"] * 100 - 25) * 0.15
        jt_score = max(0, min(100, jt_score))

        f["jt_score"] = jt_score
        f["jockey_cond_top3"] = js.get("top3_rate", 0) * 100
        f["jockey_cond_win"] = js.get("win_rate", 0) * 100
        f["trainer_cond_top3"] = ts.get("top3_rate", 0) * 100
        f["combo_top3"] = cs.get("top3_rate", 0) * 100

        # === 馬場バイアス系 (2) ===
        f["bias_score"] = 50
        hn = r.get("horse_number", 1)
        f["post_position_ratio"] = hn / max(hc, 1)

        # === ペース系 (3) ===
        # 過去レースの通過順から脚質推定
        front_count = 0
        pos_ratios = []
        last_3fs = []
        for pr in past_races[:5]:
            po = pr.get("passing_order", "")
            if po:
                try:
                    positions = [int(p) for p in po.replace("-", ",").split(",") if p.strip().isdigit()]
                    if positions:
                        ratio = positions[0] / max(pr.get("horse_count", 14) or 14, 1)
                        pos_ratios.append(ratio)
                        if ratio <= 0.35:
                            front_count += 1
                except ValueError:
                    pass
            if pr.get("last_3f", 0) and pr["last_3f"] > 0:
                last_3fs.append(pr["last_3f"])

        n_past = min(len(past_races[:5]), 1)  # avoid div by 0
        f["front_rate"] = front_count / max(len(past_races[:5]), 1)
        f["avg_pos_ratio"] = np.mean(pos_ratios) if pos_ratios else 0.5
        f["avg_last_3f"] = np.mean(last_3fs) if last_3fs else 0

        # === 馬情報 (7) ===
        f["horse_count"] = hc
        f["weight"] = (r.get("weight", 0) or 0) / 500
        f["weight_change"] = (r.get("weight_change", 0) or 0) / 20
        f["impost"] = r.get("impost", 0) or 0
        dist = race.get("distance", 1600) or 1600
        f["distance_cat"] = (0 if dist < 1400 else 1 if dist < 1800 else
                             2 if dist < 2200 else 3 if dist < 2800 else 4)
        f["surface_turf"] = 1 if race.get("surface") == "芝" else 0

        # 休養日数
        if past_races:
            try:
                from datetime import datetime
                d1 = datetime.strptime(race_date, "%Y-%m-%d")
                d2 = datetime.strptime(past_races[0]["race_date"], "%Y-%m-%d")
                rest = (d1 - d2).days
                f["rest_days"] = min(rest, 365) / 365
            except (ValueError, TypeError):
                f["rest_days"] = 0.5
        else:
            f["rest_days"] = 0.5

        # === 過去成績系 (5) ===
        positions = [pr["finish_position"] for pr in past_races[:10]]
        last5 = positions[:5]
        total_past = len(positions) if positions else 1

        f["avg_finish_5r"] = (sum(last5) / len(last5) / 18) if last5 else 0
        f["win_rate_10r"] = sum(1 for p in positions if p == 1) / total_past if positions else 0
        f["top3_rate_10r"] = sum(1 for p in positions if p <= 3) / total_past if positions else 0

        trend = 0
        if len(last5) >= 3:
            trend = (last5[0] - last5[2]) / 2
        f["finish_trend"] = np.clip(trend / 10, -1, 1)
        f["race_experience"] = min(len(positions), 10) / 10

        # === コンテキスト系 (5) ===
        if past_races:
            prev = past_races[0]
            prev_dist = prev.get("distance", dist) or dist
            f["distance_diff"] = (dist - prev_dist) / 400
            f["jockey_change"] = 1 if prev.get("jockey_id") != jockey_id else 0
        else:
            f["distance_diff"] = 0
            f["jockey_change"] = 0

        f["last_3f_best"] = (min(last_3fs) / 40) if last_3fs else 0

        # 同コース成績
        surface = race.get("surface", "")
        venue = race.get("venue", "")
        course_past = [pr for pr in past_races
                      if pr.get("venue") == venue
                      and pr.get("surface") == surface
                      and abs((pr.get("distance", 0) or 0) - dist) <= 200]
        if course_past:
            f["course_top3_rate"] = sum(1 for p in course_past if p["finish_position"] <= 3) / len(course_past)
        else:
            f["course_top3_rate"] = 0

        # 馬体重推移
        weights = [pr.get("weight", 0) for pr in past_races[:3] if pr.get("weight", 0) > 0]
        f["weight_trend"] = ((weights[0] - weights[-1]) / 20) if len(weights) >= 2 else 0

        # === 天候・馬場系 (5) ===
        tc = race.get("track_condition", "") or ""
        weather = race.get("weather", "") or ""
        tc_map = {"良": 0, "稍": 1, "稍重": 1, "重": 2, "不": 3, "不良": 3}
        wt_map = {"晴": 0, "曇": 1, "小雨": 2, "雨": 2, "小雪": 3, "雪": 3}
        f["track_cond_code"] = tc_map.get(tc, 0)
        f["weather_code"] = wt_map.get(weather, 0)
        f["is_heavy_track"] = 1 if tc in ("重", "不", "不良") else 0

        # 該当馬の重馬場時パフォーマンス
        wet_races = [pr for pr in past_races if pr.get("track_condition", "") in ("重", "不", "不良")]
        if wet_races:
            f["horse_wet_win_rate"] = sum(1 for p in wet_races if p["finish_position"] == 1) / len(wet_races)
            f["horse_wet_top3_rate"] = sum(1 for p in wet_races if p["finish_position"] <= 3) / len(wet_races)
        else:
            f["horse_wet_win_rate"] = 0
            f["horse_wet_top3_rate"] = 0

        # === 年齢系 (2) ===
        birth_year = r.get("birth_year", 0) or 0
        if birth_year and race_date:
            try:
                race_year = int(race_date[:4])
                f["horse_age"] = race_year - birth_year
                f["is_peak_age"] = 1 if 3 <= (race_year - birth_year) <= 5 else 0
            except (ValueError, TypeError):
                f["horse_age"] = 0
                f["is_peak_age"] = 0
        else:
            f["horse_age"] = 0
            f["is_peak_age"] = 0

        # === 枠順コース別成績 (2) ===
        same_post_past = [pr for pr in past_races
                          if pr.get("venue") == venue
                          and pr.get("surface") == surface
                          and abs((pr.get("horse_number", 0) or 0) - hn) <= 2]
        if same_post_past:
            f["post_win_rate_course"] = sum(1 for p in same_post_past if p["finish_position"] == 1) / len(same_post_past)
            f["post_top3_rate_course"] = sum(1 for p in same_post_past if p["finish_position"] <= 3) / len(same_post_past)
        else:
            f["post_win_rate_course"] = 0
            f["post_top3_rate_course"] = 0

        # === 年齢×クラス (1) ===
        age = f["horse_age"]
        age_class_past = [pr for pr in past_races
                          if abs((pr.get("distance", 0) or 0) - dist) <= 400]
        if age_class_past and age > 0:
            f["age_class_top3_rate"] = sum(1 for p in age_class_past if p["finish_position"] <= 3) / len(age_class_past)
        else:
            f["age_class_top3_rate"] = 0

        # === 距離別成績 (2) ===
        dist_past = [pr for pr in past_races
                     if abs((pr.get("distance", 0) or 0) - dist) <= 200]
        if dist_past:
            f["dist_win_rate"] = sum(1 for p in dist_past if p["finish_position"] == 1) / len(dist_past)
            f["dist_top3_rate"] = sum(1 for p in dist_past if p["finish_position"] <= 3) / len(dist_past)
        else:
            f["dist_win_rate"] = 0
            f["dist_top3_rate"] = 0

        # ── v5新規: 着差 (margin) ──
        def _parse_margin(s):
            if not s: return None
            s = str(s).strip()
            jp = {'ハナ':0.02, 'アタマ':0.05, 'クビ':0.15, '同着':0.0, '大差':10.0}
            if s in jp: return jp[s]
            try:
                if ' ' in s:
                    parts = s.split()
                    base = float(parts[0])
                    if '/' in parts[1]:
                        num,den = parts[1].split('/')
                        return base + float(num)/float(den)
                    return base
                if '/' in s:
                    num,den = s.split('/')
                    return float(num)/float(den)
                return float(s)
            except (ValueError, IndexError):
                return None
        margins = []
        for pr in past_races[:5]:
            m = _parse_margin(pr.get('margin'))
            if m is not None:
                if pr['finish_position'] == 1:
                    margins.append(0.0)
                else:
                    margins.append(min(m, 10.0))
        f["margin_avg"] = (sum(margins)/len(margins)) if margins else 5.0
        f["margin_best"] = min(margins) if margins else 5.0

        # ── v5新規: 同コース過去成績 (course familiarity) ──
        same_course_past = [pr for pr in past_races
                            if pr.get('venue') == venue
                            and pr.get('surface') == surface
                            and (pr.get('distance') or 0) == dist]
        if same_course_past:
            f["same_course_top3_rate"] = sum(1 for p in same_course_past if p["finish_position"] <= 3) / len(same_course_past)
            f["same_course_runs"] = min(len(same_course_past), 10) / 10
        else:
            f["same_course_top3_rate"] = 0.0
            f["same_course_runs"] = 0.0

        # ── v5新規: 斤量変化 (前走比) ──
        current_impost = f.get("impost") or 57.0
        try:
            prev_impost = float(past_races[0].get("impost") or 0) if past_races else 0
        except (TypeError, ValueError):
            prev_impost = 0
        f["impost_diff"] = (current_impost - prev_impost) if prev_impost > 0 else 0.0

        # ── v5新規: 重賞経験 + 重賞複勝率 ──
        graded_past = [pr for pr in past_races if pr.get('grade') in ('G1','G2','G3')]
        f["graded_exp"] = min(len(graded_past), 10) / 10
        if graded_past:
            f["graded_top3_rate"] = sum(1 for p in graded_past if p["finish_position"] <= 3) / len(graded_past)
        else:
            f["graded_top3_rate"] = 0.0

        # ── v6新規: 市場シグナル (オッズ・人気) ──
        # 過去6年 1人気の複勝率 65% は実勢でモデル外せない強力シグナル
        try:
            odds_val = float(r.get("odds", 0) or 0)
        except (TypeError, ValueError):
            odds_val = 0.0
        try:
            pop_val = int(r.get("popularity", 0) or 0)
        except (TypeError, ValueError):
            pop_val = 0

        import math
        if odds_val >= 1.0:
            f["odds_log"] = math.log(odds_val)
        else:
            f["odds_log"] = 2.3
        # v7: 人気押さえ型 — sqrt 圧縮で上位/下位人気の差を弱める
        # is_favorite / is_top3_pop は削除 (直接的人気フラグを排除)
        if pop_val > 0 and hc > 0:
            f["popularity_norm"] = math.sqrt(pop_val / hc)
        else:
            f["popularity_norm"] = 0.7

        # ── v7新規: 血統 × コース cross (temporal-safe) ──
        # 旧 sire_top3_rate(全期間集計) は重要度0だった。コース×距離×血統の
        # cross で「東京芝1600m での Lord Kanaloa 産駒」のような具体的 signal に。
        sire = r.get("sire", "") or ""
        damsire = r.get("damsire", "") or ""
        if sire_course is not None and sire:
            f["sire_course_top3_rate"] = _lookup_pedigree(
                sire_course.get((sire, venue, surface, distance)), race_date, min_runs=5)
            f["sire_surface_top3_rate"] = _lookup_pedigree(
                sire_surface.get((sire, surface)), race_date, min_runs=10)
        else:
            f["sire_course_top3_rate"] = 0.0
            f["sire_surface_top3_rate"] = 0.0
        if damsire_course is not None and damsire:
            f["damsire_course_top3_rate"] = _lookup_pedigree(
                damsire_course.get((damsire, venue, surface, distance)), race_date, min_runs=5)
            f["damsire_surface_top3_rate"] = _lookup_pedigree(
                damsire_surface.get((damsire, surface)), race_date, min_runs=10)
        else:
            f["damsire_course_top3_rate"] = 0.0
            f["damsire_surface_top3_rate"] = 0.0

        # === ターゲット ===
        fp = r.get("finish_position", 0) or 0
        f["target_win"] = 1 if fp == 1 else 0
        f["target_top3"] = 1 if 1 <= fp <= 3 else 0
        f["finish_position"] = fp
        f["relevance"] = max(0, hc - fp + 1) if fp > 0 else 0
        f["race_id"] = race["race_id"]
        f["horse_id"] = horse_id
        f["horse_number"] = hn
        f["odds"] = r.get("odds", 0) or 0

        rows.append(f)

    return rows


def get_feature_columns():
    """特徴量カラム一覧 v5 — ml/features.py と完全一致させること

    v5変更:
      削除(重要度0): pedigree_score, sire_top3_rate, sire_sample_size,
                     bias_score, horse_age, is_peak_age, age_class_top3_rate,
                     is_heavy_track, post_win_rate_course
      追加: margin_avg, margin_best, same_course_top3_rate, same_course_runs,
            impost_diff, graded_exp, graded_top3_rate
    """
    return [
        # SI (6)
        "si_avg", "si_max", "si_min", "si_std", "si_latest", "si_count",
        # 騎手/調教師 (5) — 最重要群
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
        # 天気・馬場 (4)
        "track_cond_code", "weather_code",
        "horse_wet_win_rate", "horse_wet_top3_rate",
        # コース別枠順 (1)
        "post_top3_rate_course",
        # 距離別 (2)
        "dist_win_rate", "dist_top3_rate",
        # v5: 着差
        "margin_avg", "margin_best",
        # v5: 同コース familiarity
        "same_course_top3_rate", "same_course_runs",
        # v5: 斤量変化
        "impost_diff",
        # v5: 重賞経験
        "graded_exp", "graded_top3_rate",
        # v6: 市場シグナル(オッズ・人気) ※競馬予想で最も強い signal の一つ
        "odds_log", "popularity_norm",
        # v7: 血統×コース cross (temporal-safe)
        "sire_course_top3_rate", "sire_surface_top3_rate",
        "damsire_course_top3_rate", "damsire_surface_top3_rate",
    ]


def train_models(df, feature_cols, num_boost_round=500, early_stopping_rounds=50):
    """3モデルを学習"""
    import pickle

    RANK_PARAMS = {
        "objective": "lambdarank", "metric": "ndcg",
        "ndcg_eval_at": [1, 3, 5], "boosting_type": "gbdt",
        "num_leaves": 63, "learning_rate": 0.05,
        "feature_fraction": 0.8, "bagging_fraction": 0.8, "bagging_freq": 5,
        "lambda_l1": 0.1, "lambda_l2": 0.1, "min_child_samples": 20,
        "verbose": -1, "n_jobs": -1, "seed": 42,
    }
    BINARY_PARAMS = {
        "objective": "binary", "metric": "binary_logloss",
        "boosting_type": "gbdt", "num_leaves": 63, "learning_rate": 0.05,
        "feature_fraction": 0.8, "bagging_fraction": 0.8, "bagging_freq": 5,
        "lambda_l1": 0.1, "lambda_l2": 0.1, "min_child_samples": 20,
        "verbose": -1, "n_jobs": -1, "seed": 42,
    }

    confirmed = df[df["finish_position"] > 0].copy()
    confirmed = confirmed.sort_values("race_id")
    print(f"\n📊 学習データ: {len(confirmed)}行, {confirmed['race_id'].nunique()}レース")

    X = confirmed[feature_cols].fillna(0)
    y_top3 = confirmed["target_top3"]
    y_win = confirmed["target_win"]
    y_rank = confirmed["relevance"]

    # レースグループ
    group = confirmed.groupby("race_id").size().values

    # 時系列分割 (80% train / 20% val)
    race_ids = confirmed["race_id"].unique()
    split_idx = int(len(race_ids) * 0.8)
    train_races = set(race_ids[:split_idx])
    val_races = set(race_ids[split_idx:])

    train_mask = confirmed["race_id"].isin(train_races)
    val_mask = confirmed["race_id"].isin(val_races)

    X_train, X_val = X[train_mask], X[val_mask]
    train_group = confirmed[train_mask].groupby("race_id").size().values
    val_group = confirmed[val_mask].groupby("race_id").size().values

    print(f"  Train: {train_mask.sum()} / Val: {val_mask.sum()}")

    # === LambdaRank ===
    print("\n🔄 LambdaRank 学習中...")
    ds_rank_t = lgb.Dataset(X_train, label=y_rank[train_mask], group=train_group)
    ds_rank_v = lgb.Dataset(X_val, label=y_rank[val_mask], group=val_group, reference=ds_rank_t)
    model_rank = lgb.train(
        RANK_PARAMS, ds_rank_t, num_boost_round=num_boost_round,
        valid_sets=[ds_rank_v],
        callbacks=[lgb.early_stopping(early_stopping_rounds), lgb.log_evaluation(50)],
    )
    print(f"  Best iteration: {model_rank.best_iteration}")

    # === Binary top3 ===
    print("\n🔄 複勝モデル学習中...")
    ds_t3_t = lgb.Dataset(X_train, label=y_top3[train_mask])
    ds_t3_v = lgb.Dataset(X_val, label=y_top3[val_mask], reference=ds_t3_t)
    model_top3 = lgb.train(
        BINARY_PARAMS, ds_t3_t, num_boost_round=num_boost_round,
        valid_sets=[ds_t3_v],
        callbacks=[lgb.early_stopping(early_stopping_rounds), lgb.log_evaluation(50)],
    )

    from sklearn.metrics import roc_auc_score
    pred_t3 = model_top3.predict(X_val)
    auc_t3 = roc_auc_score(y_top3[val_mask], pred_t3)
    print(f"  AUC(複勝): {auc_t3:.4f}")

    # === Binary win ===
    print("\n🔄 単勝モデル学習中...")
    ds_w_t = lgb.Dataset(X_train, label=y_win[train_mask])
    ds_w_v = lgb.Dataset(X_val, label=y_win[val_mask], reference=ds_w_t)
    model_win = lgb.train(
        BINARY_PARAMS, ds_w_t, num_boost_round=num_boost_round,
        valid_sets=[ds_w_v],
        callbacks=[lgb.early_stopping(early_stopping_rounds), lgb.log_evaluation(50)],
    )

    pred_w = model_win.predict(X_val)
    auc_w = roc_auc_score(y_win[val_mask], pred_w)
    print(f"  AUC(単勝): {auc_w:.4f}")

    # 特徴量重要度
    importance = pd.DataFrame({
        "feature": feature_cols,
        "rank_imp": model_rank.feature_importance(importance_type="gain"),
        "top3_imp": model_top3.feature_importance(importance_type="gain"),
        "win_imp": model_win.feature_importance(importance_type="gain"),
    }).sort_values("rank_imp", ascending=False)
    print("\n📊 特徴量重要度 (Top 15):")
    print(importance.head(15).to_string(index=False))

    # 保存
    os.makedirs(MODEL_DIR, exist_ok=True)
    with open(os.path.join(MODEL_DIR, "model_rank.pkl"), "wb") as f:
        pickle.dump(model_rank, f)
    with open(os.path.join(MODEL_DIR, "model_top3.pkl"), "wb") as f:
        pickle.dump(model_top3, f)
    with open(os.path.join(MODEL_DIR, "model_win.pkl"), "wb") as f:
        pickle.dump(model_win, f)
    print(f"\n💾 モデル保存完了: {MODEL_DIR}")

    # ── Isotonic Regression 校正子を学習 (val set で fit) ──
    # 「本命級が +8.6pt 過小評価」されていた問題を補正。
    # val set (時系列で後ろ20%) は学習時に未使用なので、honest holdout に近い。
    try:
        from sklearn.isotonic import IsotonicRegression
        print("\n🎯 Calibrator 学習中 (isotonic regression)...")
        iso_win = IsotonicRegression(out_of_bounds='clip', y_min=0.0, y_max=1.0)
        iso_win.fit(pred_w, y_win[val_mask])
        iso_t3 = IsotonicRegression(out_of_bounds='clip', y_min=0.0, y_max=1.0)
        iso_t3.fit(pred_t3, y_top3[val_mask])
        with open(os.path.join(MODEL_DIR, "calibrator_win.pkl"), "wb") as f:
            pickle.dump(iso_win, f)
        with open(os.path.join(MODEL_DIR, "calibrator_top3.pkl"), "wb") as f:
            pickle.dump(iso_t3, f)
        # ECE 改善を表示
        import numpy as np
        def _ece(y, p, n_bins=10):
            bins = np.linspace(0, 1, n_bins+1)
            bi = np.clip(np.digitize(p, bins)-1, 0, n_bins-1)
            e = 0
            for b in range(n_bins):
                m = bi == b
                if m.sum() == 0: continue
                e += (m.sum()/len(y)) * abs(p[m].mean() - y[m].mean())
            return e
        e_w0 = _ece(y_win[val_mask].values, pred_w)
        e_w1 = _ece(y_win[val_mask].values, iso_win.predict(pred_w))
        e_t0 = _ece(y_top3[val_mask].values, pred_t3)
        e_t1 = _ece(y_top3[val_mask].values, iso_t3.predict(pred_t3))
        print(f"  ECE(win):  {e_w0*100:.2f}% → {e_w1*100:.2f}%")
        print(f"  ECE(top3): {e_t0*100:.2f}% → {e_t1*100:.2f}%")
        print(f"  ✅ Calibrator 保存完了")
    except Exception as e:
        print(f"  ⚠️ Calibrator 学習失敗: {e}")

    return model_rank, model_top3, model_win, auc_t3, auc_w


def run_backtest(df, models, feature_cols, year=2025):
    """バックテスト"""
    model_rank, model_top3, model_win = models
    test = df[(df["finish_position"] > 0) &
              (df["race_date"].str.startswith(str(year)))].copy()

    if test.empty:
        print(f"❌ {year}年のデータがありません")
        return

    print(f"\n📊 バックテスト ({year}年): {test['race_id'].nunique()}レース, {len(test)}出走")

    X_test = test[feature_cols].fillna(0)
    test["rank_score"] = model_rank.predict(X_test)
    test["pred_win"] = model_win.predict(X_test)
    test["pred_top3"] = model_top3.predict(X_test)

    total_bet = 0
    total_payout = 0
    bet_count = 0
    hit_count = 0
    details = {"単勝": {"bet": 0, "payout": 0, "count": 0, "hit": 0},
               "複勝": {"bet": 0, "payout": 0, "count": 0, "hit": 0}}

    for race_id, group in test.groupby("race_id"):
        group = group.sort_values("rank_score", ascending=False)

        # ランキングスコアを正規化
        rank_exp = np.exp(group["rank_score"] - group["rank_score"].max())
        group = group.copy()
        group["win_prob"] = rank_exp / rank_exp.sum()

        hc = len(group)

        for _, row in group.iterrows():
            wp = row["win_prob"]
            odds = row["odds"]

            if odds <= 0:
                continue

            # 単勝: 期待値 > 1.2 && 予測勝率 > 8%
            ev_win = wp * odds
            if ev_win >= 1.2 and wp >= 0.08:
                amount = 100
                total_bet += amount
                bet_count += 1
                details["単勝"]["bet"] += amount
                details["単勝"]["count"] += 1

                if row["finish_position"] == 1:
                    payout = amount * odds
                    total_payout += payout
                    hit_count += 1
                    details["単勝"]["payout"] += payout
                    details["単勝"]["hit"] += 1

            # 複勝: 期待値 > 1.1 && 予測top3確率 > 20%
            t3_prob = row["pred_top3"]
            place_odds = max(odds * 0.3, 1.1)
            ev_place = t3_prob * place_odds
            if ev_place >= 1.1 and t3_prob >= 0.20:
                amount = 100
                total_bet += amount
                bet_count += 1
                details["複勝"]["bet"] += amount
                details["複勝"]["count"] += 1

                if row["finish_position"] <= 3:
                    payout = amount * place_odds
                    total_payout += payout
                    hit_count += 1
                    details["複勝"]["payout"] += payout
                    details["複勝"]["hit"] += 1

    roi = (total_payout / total_bet * 100) if total_bet > 0 else 0
    profit = total_payout - total_bet

    print(f"\n{'='*50}")
    print(f"📊 バックテスト結果 ({year}年)")
    print(f"{'='*50}")
    print(f"  購入点数:  {bet_count}")
    print(f"  的中数:    {hit_count}")
    print(f"  的中率:    {hit_count/bet_count*100:.1f}%" if bet_count else "  的中率:    -")
    print(f"  総賭け金:  ¥{total_bet:,}")
    print(f"  総払戻金:  ¥{total_payout:,.0f}")
    print(f"  損益:      ¥{profit:,.0f}")
    print(f"  回収率:    {roi:.1f}%")
    print(f"{'='*50}")

    for bt, d in details.items():
        if d["count"] > 0:
            bt_roi = d["payout"] / d["bet"] * 100 if d["bet"] > 0 else 0
            bt_hr = d["hit"] / d["count"] * 100
            print(f"  [{bt}] {d['count']}点 / 的中{d['hit']}({bt_hr:.1f}%) / "
                  f"ROI {bt_roi:.1f}%")

    if roi >= 100:
        print(f"\n  🎉 プラス収支達成!")
    else:
        print(f"\n  📉 マイナス収支")


def build_feature_table():
    """Step 1-3 (データロード → プリコンパイル → 全レース特徴量計算) を実行して
    特徴量 DataFrame を返す。

    #57: main() からの切り出し。scripts/train_ability_model.py (odds 非依存の
    能力モデル学習) からも同一の特徴量テーブルを再利用するための共通化。
    """
    races_df, results_df, payouts_df = load_all_data()

    # race_date をresults_dfに追加（horse_historyビルド用）
    race_info = races_df.set_index("race_id")[["race_date", "venue", "distance",
                                                "surface", "track_condition",
                                                "weather", "horse_count"]].to_dict("index")

    for col in ["race_date", "venue", "distance", "surface", "track_condition", "weather", "horse_count"]:
        results_df[col] = results_df["race_id"].map(
            lambda rid, c=col: race_info.get(rid, {}).get(c, "")
        )

    # Step 2: プリコンパイル
    horse_history = build_horse_history(results_df, races_df)
    jockey_stats, trainer_stats, combo_stats = build_jockey_trainer_stats(results_df, races_df)
    si_cache = build_speed_index_cache(results_df, races_df)
    # v7: 血統 cross stats (temporal-safe)
    sire_course, sire_surface, damsire_course, damsire_surface = \
        build_pedigree_cross_stats(results_df, races_df)

    # Step 3: 特徴量一括計算
    print("\n🔧 全レースの特徴量を一括計算中...")
    t0 = time.time()
    all_rows = []
    total_races = len(races_df)

    for i, race in races_df.iterrows():
        if (i + 1) % 500 == 0:
            elapsed = time.time() - t0
            speed = (i + 1) / elapsed
            remaining = (total_races - i - 1) / speed if speed > 0 else 0
            print(f"  [{i+1}/{total_races}] {elapsed:.0f}秒経過, 残り約{remaining:.0f}秒")

        race_results = results_df[results_df["race_id"] == race["race_id"]]
        if race_results.empty:
            continue

        race_dict = race.to_dict()
        results_list = race_results.to_dict("records")

        rows = compute_features_fast(
            race_dict, results_list, horse_history,
            jockey_stats, trainer_stats, combo_stats, si_cache,
            sire_course=sire_course, sire_surface=sire_surface,
            damsire_course=damsire_course, damsire_surface=damsire_surface,
        )
        all_rows.extend(rows)

    df = pd.DataFrame(all_rows)
    df["race_date"] = df["race_id"].map(lambda rid: race_info.get(rid, {}).get("race_date", ""))

    elapsed = time.time() - t0
    print(f"  ✅ 特徴量計算完了: {len(df)}行 ({elapsed:.1f}秒)")
    return df


def main():
    init_db()
    t_start = time.time()

    # Step 1-3: 特徴量テーブル構築
    df = build_feature_table()

    # Step 4: 学習
    feature_cols = get_feature_columns()
    models = train_models(df, feature_cols)
    model_rank, model_top3, model_win, auc_t3, auc_w = models

    # Step 5: バックテスト
    run_backtest(df, (model_rank, model_top3, model_win), feature_cols, year=2025)

    total_elapsed = time.time() - t_start
    print(f"\n⏱️ 総実行時間: {total_elapsed:.0f}秒 ({total_elapsed/60:.1f}分)")


if __name__ == "__main__":
    main()
