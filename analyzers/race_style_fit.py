#!/usr/bin/env python3
"""
レース様式適合 (race style fit) 分析 — #105

記事 (深掘り分析) の照合ロジックの機械化。函館記念2026 記事の軸①
「勝ち型 = 上がり3F順位 × 4コーナー位置」を同名レース一般に拡張する。

  1. 同名レースの過去エディション (最大6・target 日付より前のみ) の
     勝ち馬 (+2着馬は別枠) の
       (a) 上がり3F順位 (レース内, 1=最速)
       (b) 最終コーナー位置を頭数で正規化した比率 (前=0〜後=1)
     をプロファイル化する。
  2. 各出走馬の近走 n 走 (target 日付より前のみ) の同じ2指標と照合し、
     勝ち型への適合度を 0-100 で返す (上がり適合 0-50 + 位置適合 0-50)。

temporal-safe: プロファイル・近走とも race_date < target date (strict) のみ使用。
通過順 (passing_order) が欠損しているエディション/近走は該当成分だけ落とし、
n を明記する (2021-2023 は backfill 状況によって欠損が残る)。

usage:
  python3 analyzers/race_style_fit.py <race_id> [--db PATH] [--n-runs 4]
  python3 analyzers/race_style_fit.py --precompute --start 2023-01-01 \
      --end 2026-06-28 --out style_fit_hist.pkl [--db PATH]
"""

import argparse
import os
import pickle
import re
import sqlite3
import sys
from datetime import datetime, timezone
from statistics import median

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ----------------------------------------------------------------------------
# 母集団規律 (upset_hist #96 と同じ): 同名レースの追跡が意味を持つのは
# 特別戦/OP/重賞のみ。汎用クラス戦 (「3歳以上1勝クラス」等) と障害は除外。
# ----------------------------------------------------------------------------
_GENERIC_NAME_RE = re.compile(
    r"(未勝利|新馬|メイクデビュー"
    r"|^[2-5２-５]歳(以上)?(障害)?[1-3１-３]勝クラス$"
    r"|^[2-5２-５]歳(以上)?(障害)?(OP|ＯＰ|オープン)$"
    r"|^[2-5２-５]歳(以上)?(500|５００|1000|１０００|1600|１６００)万下?$)"
)

# スコア設計パラメータ (単調・説明可能)
AGARI_FLOOR = 3.0      # 勝ち型の上がり順位ターゲットの下限 (「3位以内」基準)
AGARI_DECAY = 5.0      # ターゲット超過1位ごとに -10pt (5位超過で 0)
POS_TOLERANCE = 0.20   # 位置比率: 勝ち型中央値 ±0.20 までは満点
POS_DECAY = 0.40       # 許容帯を超えた分 0.40 で 0pt まで線形減点
MIN_POS_SAMPLES = 2    # プロファイル位置統計に必要な最小サンプル


def is_generic_class_race(race_name):
    """汎用クラス戦 (同名追跡が無意味なレース名) かどうか"""
    if not race_name or not race_name.strip():
        return True
    return bool(_GENERIC_NAME_RE.search(race_name.strip()))


def _parse_last_corner(passing_order):
    """'3-3-2-1' → 1 (最終コーナー位置)。解釈不能なら None"""
    if not passing_order:
        return None
    tokens = [t.strip() for t in str(passing_order).split("-") if t.strip()]
    if not tokens:
        return None
    last = re.sub(r"[^0-9]", "", tokens[-1])
    if not last.isdigit():
        return None
    pos = int(last)
    return pos if pos >= 1 else None


def get_race_style_map(conn, race_id, cache=None):
    """レース内の各馬の (上がり順位, 最終コーナー位置比率) を返す。

    returns: {horse_id: {"agari_rank": int|None, "pos_ratio": float|None,
                         "finish_position": int}}
    上がり順位はレース内で last_3f を昇順に順位化 (1=最速, 同タイムは同順位)。
    位置比率は (最終コーナー位置-1)/(頭数-1) で 前=0〜後=1。
    cache: {race_id: map} の dict を渡すとメモ化する (precompute 用)。
    """
    if cache is not None and race_id in cache:
        return cache[race_id]

    rows = conn.execute(
        """SELECT r.horse_id, r.finish_position, r.last_3f, r.passing_order,
                  ra.horse_count
           FROM results r JOIN races ra ON r.race_id = ra.race_id
           WHERE r.race_id = ? AND r.finish_position > 0""",
        (race_id,),
    ).fetchall()

    style_map = {}
    if rows:
        runners = rows[0]["horse_count"]
        if not runners or runners < len(rows):
            runners = len(rows)
        valid_3f = sorted(r["last_3f"] for r in rows if r["last_3f"] and r["last_3f"] > 0)
        for r in rows:
            agari_rank = None
            if r["last_3f"] and r["last_3f"] > 0 and valid_3f:
                # 同タイムは同順位 (自分より厳密に速い頭数 + 1)
                agari_rank = sum(1 for t in valid_3f if t < r["last_3f"]) + 1
            pos = _parse_last_corner(r["passing_order"])
            pos_ratio = None
            if pos is not None:
                pos_ratio = (min(pos, runners) - 1) / max(runners - 1, 1)
            style_map[r["horse_id"]] = {
                "agari_rank": agari_rank,
                "pos_ratio": pos_ratio,
                "finish_position": r["finish_position"],
            }

    if cache is not None:
        cache[race_id] = style_map
    return style_map


def _summarize(agari_ranks, pos_ratios):
    """勝ち馬 (または2着馬) 群のスタイル統計"""
    out = {
        "agari_rank_median": round(median(agari_ranks), 2) if agari_ranks else None,
        "agari_top3_rate": (
            round(sum(1 for a in agari_ranks if a <= 3) / len(agari_ranks), 3)
            if agari_ranks else None
        ),
        "pos_ratio_median": (
            round(median(pos_ratios), 3) if len(pos_ratios) >= MIN_POS_SAMPLES else None
        ),
        "front_mid_rate": (
            round(sum(1 for p in pos_ratios if p <= 0.5) / len(pos_ratios), 3)
            if len(pos_ratios) >= MIN_POS_SAMPLES else None
        ),
        "n_agari": len(agari_ranks),
        "n_pos": len(pos_ratios),
    }
    return out


def compute_race_style_profile(conn, race_name, race_date, max_editions=6,
                               min_editions=3, cache=None):
    """同名レースの過去エディションから勝ち型プロファイルを作る。

    temporal-safe: race_date (strict <) より前のエディションのみ。
    エディション不足 (< min_editions)、または上がり・位置の両統計が
    欠損した場合は None。位置統計のみ欠損の場合は pos_ratio_median=None の
    まま返す (通過順 backfill 未完の年に対応)。
    2着馬の統計は place2 に別枠で入れる (スコアには使わない)。
    """
    if is_generic_class_race(race_name):
        return None

    editions = conn.execute(
        """SELECT race_id, race_date FROM races
           WHERE race_name = ? AND race_date < ? AND surface != '障害'
           ORDER BY race_date DESC LIMIT ?""",
        (race_name, race_date, max_editions),
    ).fetchall()
    if len(editions) < min_editions:
        return None

    win_agari, win_pos = [], []
    p2_agari, p2_pos = [], []
    edition_info = []
    for ed in editions:
        smap = get_race_style_map(conn, ed["race_id"], cache=cache)
        has_pos = False
        for st in smap.values():
            bucket = None
            if st["finish_position"] == 1:
                bucket = (win_agari, win_pos)
            elif st["finish_position"] == 2:
                bucket = (p2_agari, p2_pos)
            if bucket is None:
                continue
            if st["agari_rank"] is not None:
                bucket[0].append(st["agari_rank"])
            if st["pos_ratio"] is not None:
                bucket[1].append(st["pos_ratio"])
                has_pos = True
        edition_info.append({"race_id": ed["race_id"], "race_date": ed["race_date"],
                             "has_pos": has_pos})

    profile = {
        "race_name": race_name,
        "n_editions": len(editions),
        "editions": edition_info,
    }
    profile.update(_summarize(win_agari, win_pos))
    profile["place2"] = _summarize(p2_agari, p2_pos)

    if profile["agari_rank_median"] is None and profile["pos_ratio_median"] is None:
        return None
    return profile


def compute_entry_style(conn, horse_id, race_date, n_runs=4, cache=None):
    """出走馬の近走 n 走 (race_date より前, strict) の平均スタイル。

    returns: {"agari_rank_avg", "pos_ratio_avg", "n_runs", "n_agari", "n_pos",
              "runs": [走ごとの詳細]} / 近走ゼロなら None
    """
    recent = conn.execute(
        """SELECT r.race_id, ra.race_date
           FROM results r JOIN races ra ON r.race_id = ra.race_id
           WHERE r.horse_id = ? AND ra.race_date < ?
             AND r.finish_position > 0 AND ra.surface != '障害'
           ORDER BY ra.race_date DESC LIMIT ?""",
        (horse_id, race_date, n_runs),
    ).fetchall()
    if not recent:
        return None

    agari, pos, runs = [], [], []
    for row in recent:
        st = get_race_style_map(conn, row["race_id"], cache=cache).get(horse_id)
        if st is None:
            continue
        if st["agari_rank"] is not None:
            agari.append(st["agari_rank"])
        if st["pos_ratio"] is not None:
            pos.append(st["pos_ratio"])
        runs.append({"race_id": row["race_id"], "race_date": row["race_date"],
                     "agari_rank": st["agari_rank"], "pos_ratio": st["pos_ratio"],
                     "finish_position": st["finish_position"]})
    return {
        "agari_rank_avg": round(sum(agari) / len(agari), 2) if agari else None,
        "pos_ratio_avg": round(sum(pos) / len(pos), 3) if pos else None,
        "n_runs": len(recent),
        "n_agari": len(agari),
        "n_pos": len(pos),
        "runs": runs,
    }


def _best2_mean(values):
    """昇順に良い2つ (少なければある分) の平均"""
    best = sorted(values)[:2]
    return sum(best) / len(best) if best else None


def style_fit_score(profile, entry_style):
    """勝ち型プロファイルと近走スタイルの適合度 (0-100)。

    記事の照合は「近走のうち勝ち型に合った走りが何度あるか」を見る
    (例: フィーリウス「上がり3位以内が3回」/ デビットバローズ「12月G3を
    4角4番手・上がり2位」)。平均だと1走の大敗で潰れるため、各成分とも
    近走の **ベスト2平均** で評価する (どの1走が良くなってもスコアは
    単調に改善)。

    上がり適合 (0-50): 近走ベスト2の上がり順位平均が勝ち型ターゲット
      max(勝ち馬上がり順位中央値, 3位) 以内なら満点。超過1位ごとに
      50/AGARI_DECAY pt 減点。勝ち型より鋭い分には減点しない (末脚は武器)。
    位置適合 (0-50): 勝ち馬の4角位置比率中央値との差 |近走比率-中央値| の
      ベスト2平均が POS_TOLERANCE 以内なら満点。超過分に比例して減点
      (前過ぎ/後ろ過ぎの両方向)。
    片成分しか計算できない場合はその成分を2倍して 0-100 に換算し partial=True。
    両成分とも不能なら None。
    """
    if not profile or not entry_style:
        return None

    runs = entry_style.get("runs") or []
    agari_pts = pos_pts = None
    parts = []

    if profile.get("agari_rank_median") is not None:
        agari_vals = [r["agari_rank"] for r in runs if r.get("agari_rank") is not None]
        if not agari_vals and entry_style.get("agari_rank_avg") is not None:
            agari_vals = [entry_style["agari_rank_avg"]]  # runs 詳細なし時のフォールバック
        agari_metric = _best2_mean(agari_vals)
        if agari_metric is not None:
            target_a = max(profile["agari_rank_median"], AGARI_FLOOR)
            excess = max(0.0, agari_metric - target_a)
            agari_pts = round(50.0 * max(0.0, 1.0 - excess / AGARI_DECAY), 1)
            parts.append(
                f"上がり適合 {agari_pts:g}/50 "
                f"(近{len(agari_vals)}走ベスト2平均 {agari_metric:g}位"
                f" vs 勝ち型 {target_a:g}位以内)"
            )

    if profile.get("pos_ratio_median") is not None:
        target_p = profile["pos_ratio_median"]
        pos_diffs = [abs(r["pos_ratio"] - target_p) for r in runs
                     if r.get("pos_ratio") is not None]
        if not pos_diffs and entry_style.get("pos_ratio_avg") is not None:
            pos_diffs = [abs(entry_style["pos_ratio_avg"] - target_p)]
        pos_metric = _best2_mean(pos_diffs)
        if pos_metric is not None:
            over = max(0.0, pos_metric - POS_TOLERANCE)
            pos_pts = round(50.0 * max(0.0, 1.0 - over / POS_DECAY), 1)
            parts.append(
                f"位置適合 {pos_pts:g}/50 "
                f"(近{len(pos_diffs)}走の4角比率ズレのベスト2平均 {pos_metric:.2f}"
                f" vs 勝ち型 {target_p:.2f}±{POS_TOLERANCE:g})"
            )

    if agari_pts is None and pos_pts is None:
        return None

    partial = agari_pts is None or pos_pts is None
    if partial:
        score = round((agari_pts if agari_pts is not None else pos_pts) * 2.0, 1)
        missing = "位置" if pos_pts is None else "上がり"
        parts.append(f"※{missing}データ欠損のため片成分×2で換算")
    else:
        score = round(agari_pts + pos_pts, 1)

    return {
        "score": score,
        "agari_pts": agari_pts,
        "pos_pts": pos_pts,
        "partial": partial,
        "basis": " + ".join(parts),
    }


# ----------------------------------------------------------------------------
# レース単位のスコアリング
# ----------------------------------------------------------------------------

def score_race(conn, race_id, n_runs=4, max_editions=6, cache=None):
    """1レースの全出走馬をスコアリング。

    returns: (race_row, profile, [entry dict]) / レース不明なら (None, None, [])
    profile が作れない (エディション不足等) 場合は (race_row, None, []).
    """
    race = conn.execute(
        """SELECT race_id, race_date, venue, race_number, race_name, grade,
                  distance, surface, horse_count, track_condition
           FROM races WHERE race_id = ?""",
        (race_id,),
    ).fetchone()
    if race is None:
        return None, None, []

    profile = compute_race_style_profile(
        conn, race["race_name"], race["race_date"],
        max_editions=max_editions, cache=cache)
    if profile is None:
        return race, None, []

    entries = conn.execute(
        """SELECT r.horse_id, h.horse_name, r.horse_number, r.popularity,
                  r.odds, r.finish_position
           FROM results r LEFT JOIN horses h ON r.horse_id = h.horse_id
           WHERE r.race_id = ?
           ORDER BY r.horse_number""",
        (race_id,),
    ).fetchall()

    scored = []
    for e in entries:
        est = compute_entry_style(conn, e["horse_id"], race["race_date"],
                                  n_runs=n_runs, cache=cache)
        fit = style_fit_score(profile, est)
        scored.append({
            "horse_id": e["horse_id"],
            "horse_name": e["horse_name"] or e["horse_id"],
            "horse_number": e["horse_number"],
            "popularity": e["popularity"],
            "odds": e["odds"],
            "finish_position": e["finish_position"],
            "entry_style": est,
            "fit": fit,
        })
    return race, profile, scored


# ----------------------------------------------------------------------------
# 事前計算 (検証フェーズ用 pickle)
# ----------------------------------------------------------------------------

def precompute(conn, start_date, end_date, out_path, n_runs=4, max_editions=6,
               progress=True):
    """対象期間の全対象レース (同名3+エディションの特別/OP/重賞) の
    全出走馬スコアを DataFrame にして pickle 保存する。"""
    import pandas as pd

    targets = conn.execute(
        """SELECT race_id, race_name FROM races
           WHERE race_date >= ? AND race_date <= ?
             AND surface != '障害'
             AND race_name IS NOT NULL AND race_name != ''
           ORDER BY race_date, race_id""",
        (start_date, end_date),
    ).fetchall()
    targets = [t for t in targets if not is_generic_class_race(t["race_name"])]

    cache = {}
    rows = []
    n_scored_races = 0
    for i, t in enumerate(targets):
        race, profile, scored = score_race(
            conn, t["race_id"], n_runs=n_runs, max_editions=max_editions,
            cache=cache)
        if profile is None or not scored:
            continue
        n_scored_races += 1
        for s in scored:
            fit = s["fit"] or {}
            est = s["entry_style"] or {}
            rows.append({
                "race_id": race["race_id"],
                "race_date": race["race_date"],
                "race_name": race["race_name"],
                "grade": race["grade"],
                "venue": race["venue"],
                "distance": race["distance"],
                "surface": race["surface"],
                "horse_count": race["horse_count"],
                "horse_id": s["horse_id"],
                "horse_name": s["horse_name"],
                "horse_number": s["horse_number"],
                "popularity": s["popularity"],
                "odds": s["odds"],
                "finish_position": s["finish_position"],
                "score": fit.get("score"),
                "agari_pts": fit.get("agari_pts"),
                "pos_pts": fit.get("pos_pts"),
                "partial": fit.get("partial"),
                "entry_agari_avg": est.get("agari_rank_avg"),
                "entry_pos_avg": est.get("pos_ratio_avg"),
                "entry_n_agari": est.get("n_agari"),
                "entry_n_pos": est.get("n_pos"),
                "prof_n_editions": profile["n_editions"],
                "prof_agari_median": profile["agari_rank_median"],
                "prof_pos_median": profile["pos_ratio_median"],
                "prof_agari_top3_rate": profile["agari_top3_rate"],
                "prof_front_mid_rate": profile["front_mid_rate"],
                "prof_n_agari": profile["n_agari"],
                "prof_n_pos": profile["n_pos"],
            })
        if progress and (i + 1) % 200 == 0:
            print(f"  {i + 1}/{len(targets)} 候補処理 / {n_scored_races} レース採点 "
                  f"/ {len(rows)} 行", flush=True)

    df = pd.DataFrame(rows)
    payload = {
        "meta": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "start": start_date, "end": end_date,
            "n_runs": n_runs, "max_editions": max_editions,
            "agari_floor": AGARI_FLOOR, "agari_decay": AGARI_DECAY,
            "pos_tolerance": POS_TOLERANCE, "pos_decay": POS_DECAY,
            "n_candidate_races": len(targets),
            "n_scored_races": n_scored_races,
        },
        "df": df,
    }
    with open(out_path, "wb") as f:
        pickle.dump(payload, f)
    print(f"💾 保存: {out_path} ({n_scored_races} レース / {len(df)} 行)")
    return payload


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def _connect(db_path=None):
    if db_path:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    from database import get_connection
    return get_connection()


def _print_race(conn, race_id, n_runs, max_editions):
    race, profile, scored = score_race(conn, race_id, n_runs=n_runs,
                                       max_editions=max_editions, cache={})
    if race is None:
        print(f"❌ race_id {race_id} が races に見つかりません")
        return 1
    print(f"🏇 {race['race_date']} {race['venue']}{race['race_number']}R "
          f"{race['race_name']} ({race['grade']} {race['surface']}{race['distance']}m)")
    if profile is None:
        print("⚠️ 勝ち型プロファイルを作れません (同名過去エディション3未満 or データ欠損)")
        return 2

    pos_eds = sum(1 for e in profile["editions"] if e["has_pos"])
    print(f"📐 勝ち型プロファイル (過去{profile['n_editions']}エディション: "
          + ", ".join(e["race_date"][:4] for e in profile["editions"]) + ")")
    print(f"   上がり順位中央値 {profile['agari_rank_median']} / "
          f"上がり3位以内率 {profile['agari_top3_rate']} (n={profile['n_agari']})")
    if profile["pos_ratio_median"] is not None:
        print(f"   4角位置比率中央値 {profile['pos_ratio_median']} / "
              f"前中団率 {profile['front_mid_rate']} "
              f"(n={profile['n_pos']}, 通過順あり{pos_eds}年)")
    else:
        print(f"   4角位置: 通過順データ不足 (あり{pos_eds}年) → 位置成分なし")
    p2 = profile["place2"]
    print(f"   [別枠] 2着馬: 上がり中央値 {p2['agari_rank_median']} "
          f"(n={p2['n_agari']}) / 位置中央値 {p2['pos_ratio_median']} (n={p2['n_pos']})")
    print()

    def sort_key(s):
        return -(s["fit"]["score"] if s["fit"] else -1)

    print(f"{'順':>2} {'馬番':>3} {'馬名':<18} {'人気':>3} {'スコア':>5}  根拠")
    for rank, s in enumerate(sorted(scored, key=sort_key), 1):
        fit, name = s["fit"], s["horse_name"]
        pop = s["popularity"] if s["popularity"] else "-"
        if fit is None:
            print(f"{rank:>2} {s['horse_number']:>3} {name:<18} {pop:>3} {'--':>5}  近走データ不足")
        else:
            print(f"{rank:>2} {s['horse_number']:>3} {name:<18} {pop:>3} "
                  f"{fit['score']:>5g}  {fit['basis']}")
    return 0


def main():
    ap = argparse.ArgumentParser(description="レース様式適合 (勝ち型×近走スタイル照合)")
    ap.add_argument("race_id", nargs="?", help="対象レースID")
    ap.add_argument("--db", help="DBパス (省略時 repo の keiba.db)")
    ap.add_argument("--n-runs", type=int, default=4, help="近走の本数 (default 4)")
    ap.add_argument("--max-editions", type=int, default=6,
                    help="プロファイルに使う過去エディション上限 (default 6)")
    ap.add_argument("--precompute", action="store_true",
                    help="対象期間の全レースを事前計算して pickle 保存")
    ap.add_argument("--start", default="2023-01-01")
    ap.add_argument("--end", default="2026-06-28")
    ap.add_argument("--out", default="style_fit_hist.pkl")
    args = ap.parse_args()

    conn = _connect(args.db)
    try:
        if args.precompute:
            precompute(conn, args.start, args.end, args.out,
                       n_runs=args.n_runs, max_editions=args.max_editions)
            return 0
        if not args.race_id:
            ap.error("race_id か --precompute のどちらかを指定してください")
        return _print_race(conn, args.race_id, args.n_runs, args.max_editions)
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(main())
