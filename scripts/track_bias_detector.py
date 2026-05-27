#!/usr/bin/env python3
"""Phase α-4: 馬場バイアス検出 (Track 1 完成版)

同日 R1-R8 までの結果から、その日の馬場傾向を推定し、後続 R9-R12 の予測に補正をかける。

検出する馬場バイアス:
  - 内伸び / 外伸び (枠順 × 着順)
  - 前残り / 差し決まる (上がり3F × 着順)
  - 重馬場時の脚質バイアス

Usage (analysis):
    python3 scripts/track_bias_detector.py --date 2025-05-25 --venue 東京

Usage (runtime, called from predict.py):
    from track_bias_detector import detect_track_bias
    bias = detect_track_bias(conn, date, venue)
    # bias = {"frame_bias": "inside" / "outside" / "neutral",
    #         "pace_bias": "front" / "stalk" / "closing",
    #         "confidence": "high" / "low",
    #         "n_races": 6}
"""
from __future__ import annotations
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from database import get_db, init_db


def detect_track_bias(conn, date_iso, venue):
    """同日 venue 内 R1-R8 から馬場バイアスを検出。

    Returns: dict with keys:
        - frame_bias: "inside" (1-3枠が来やすい) / "outside" (6-8枠) / "neutral"
        - pace_bias: "front" (先行有利) / "closing" (差し決まる) / "neutral"
        - n_races: 集計対象 race 数
        - confidence: "high" (n >= 6) / "low"
        - details: 詳細統計
    """
    cur = conn.execute("""
        SELECT r.race_id, r.race_number,
               res.horse_number, res.post_position, res.finish_position,
               res.last_3f, res.passing_order
        FROM races r
        JOIN results res ON r.race_id = res.race_id
        WHERE r.race_date = ? AND r.venue = ?
          AND r.race_number <= 8 AND res.finish_position > 0
        ORDER BY r.race_number, res.finish_position
    """, (date_iso, venue))
    rows = cur.fetchall()

    if not rows:
        return None

    # race ごとに分割
    by_race = {}
    for r in rows:
        rid = r["race_id"]
        by_race.setdefault(rid, []).append(dict(r))

    n_races = len(by_race)
    if n_races < 3:
        return {"n_races": n_races, "confidence": "low",
                "frame_bias": "neutral", "pace_bias": "neutral",
                "details": "サンプル不足"}

    # ── 1. 枠順 (post_position) バイアス ──
    # 着順 1-3 (3着内) の post_position 分布
    inside_wins = 0  # post_position 1-3
    outside_wins = 0  # post_position 6-8
    total_top3 = 0
    for rid, results in by_race.items():
        for r in results[:3]:
            pp = r.get("post_position") or 0
            if pp <= 0: continue
            total_top3 += 1
            if pp <= 3: inside_wins += 1
            elif pp >= 6: outside_wins += 1

    if total_top3 > 0:
        inside_rate = inside_wins / total_top3
        outside_rate = outside_wins / total_top3
        # 期待値: 内外それぞれ 3/8 ≈ 37.5%
        if inside_rate >= 0.48 and inside_rate > outside_rate + 0.10:
            frame_bias = "inside"
        elif outside_rate >= 0.48 and outside_rate > inside_rate + 0.10:
            frame_bias = "outside"
        else:
            frame_bias = "neutral"
    else:
        frame_bias = "neutral"
        inside_rate = outside_rate = 0

    # ── 2. ペースバイアス (passing_order の先頭位置と着順) ──
    # 1着馬の passing_order を見て「逃げ・先行」なのか「差し・追込」なのかを判定
    front_wins = 0  # 1着が逃げ or 先行 (1st corner ≤ 3)
    closing_wins = 0  # 1着が差し or 追込 (1st corner ≥ 7)
    n_pace_known = 0
    for rid, results in by_race.items():
        winner = next((r for r in results if r["finish_position"] == 1), None)
        if not winner: continue
        po = winner.get("passing_order", "")
        if not po: continue
        # passing_order は "1-2-3-4" のような形式 (1コーナー→4コーナー)
        try:
            first_corner = int(po.split("-")[0])
            n_pace_known += 1
            if first_corner <= 3:
                front_wins += 1
            elif first_corner >= 7:
                closing_wins += 1
        except (ValueError, IndexError):
            continue

    if n_pace_known >= 3:
        front_rate = front_wins / n_pace_known
        closing_rate = closing_wins / n_pace_known
        if front_rate >= 0.55:
            pace_bias = "front"  # 前残り
        elif closing_rate >= 0.40:
            pace_bias = "closing"  # 差し決まる
        else:
            pace_bias = "neutral"
    else:
        pace_bias = "neutral"
        front_rate = closing_rate = 0

    return {
        "n_races": n_races,
        "confidence": "high" if n_races >= 6 else "medium",
        "frame_bias": frame_bias,
        "pace_bias": pace_bias,
        "details": {
            "inside_rate": round(inside_rate, 2),
            "outside_rate": round(outside_rate, 2),
            "front_rate": round(front_rate, 2),
            "closing_rate": round(closing_rate, 2),
            "n_pace_known": n_pace_known,
        }
    }


def apply_bias_to_predictions(predictions, bias):
    """検出されたバイアスを predictions に反映 (post-prediction adjustment)。

    predictions: [{horse_number, pred_win, pred_top3, post_position, ...}, ...]
    bias: detect_track_bias の返り値
    """
    if not bias or bias.get("confidence") == "low":
        return predictions, "バイアス信頼度低 (補正なし)"

    fb = bias.get("frame_bias", "neutral")
    pb = bias.get("pace_bias", "neutral")
    if fb == "neutral" and pb == "neutral":
        return predictions, "バイアス検出なし"

    notes = []
    for p in predictions:
        pp = p.get("post_position") or 0
        factor = 1.0

        if fb == "inside":
            if pp <= 3: factor *= 1.08  # 内枠 +8%
            elif pp >= 6: factor *= 0.92  # 外枠 -8%
        elif fb == "outside":
            if pp >= 6: factor *= 1.08
            elif pp <= 3: factor *= 0.92

        # 脚質補正は当面 placeholder (馬の脚質情報が必要)
        # pace_bias × 馬の脚質 で具体化できるが今は未実装

        if factor != 1.0:
            p["pred_win"] *= factor
            p["pred_top3"] *= factor
            p["bias_factor"] = round(factor, 2)

    if fb != "neutral":
        notes.append(f"枠順バイアス: {fb} ({bias['details'].get('inside_rate', 0)*100:.0f}% vs {bias['details'].get('outside_rate', 0)*100:.0f}%)")
    if pb != "neutral":
        notes.append(f"ペースバイアス: {pb}")

    # 再正規化
    total_win = sum(p["pred_win"] for p in predictions)
    if total_win > 0:
        for p in predictions:
            p["pred_win"] /= total_win
    return predictions, " / ".join(notes)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="YYYY-MM-DD")
    ap.add_argument("--venue", required=True, help="venue 名 (東京/中山/etc)")
    args = ap.parse_args()

    init_db()
    with get_db() as conn:
        bias = detect_track_bias(conn, args.date, args.venue)
    if bias is None:
        print(f"⚠️ データなし: {args.date} {args.venue}")
        return
    print(f"📊 馬場バイアス: {args.date} {args.venue}")
    print(f"  対象 race: {bias['n_races']} (信頼度: {bias['confidence']})")
    print(f"  枠順バイアス: {bias['frame_bias']}")
    print(f"  ペースバイアス: {bias['pace_bias']}")
    print(f"  詳細: {bias.get('details')}")


if __name__ == "__main__":
    main()
