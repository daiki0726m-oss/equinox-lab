#!/usr/bin/env python3
"""
predictions_cache → docs/data/predictions_{date}.json に書き出し
GitHub Pages用の静的データ生成スクリプト
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from database import get_db

JST = timezone(timedelta(hours=9))


def export_predictions(date_str=None):
    """指定日の予測データをJSONファイルに書き出し"""

    if not date_str:
        # 今週末の土日を自動判定
        now = datetime.now(JST)
        weekday = now.weekday()  # 0=Mon
        # 次の土曜を計算
        days_until_sat = (5 - weekday) % 7
        if days_until_sat == 0 and now.hour >= 18:
            days_until_sat = 7
        sat = now + timedelta(days=days_until_sat)
        sun = sat + timedelta(days=1)
        dates = [sat.strftime("%Y%m%d"), sun.strftime("%Y%m%d")]
    else:
        dates = [date_str]

    output_dir = os.path.join(os.path.dirname(__file__), "docs", "data")
    os.makedirs(output_dir, exist_ok=True)

    exported = []

    for ds in dates:
        race_date_hyphen = f"{ds[:4]}-{ds[4:6]}-{ds[6:8]}"

        with get_db() as conn:
            # レース一覧
            races = conn.execute("""
                SELECT * FROM races
                WHERE race_date = ? OR race_date = ?
                ORDER BY venue, race_number
            """, (ds, race_date_hyphen)).fetchall()

            if not races:
                print(f"  ⏭️ {ds}: レースなし")
                continue

            all_races = []
            for race in races:
                race_id = race["race_id"]
                race_info = dict(race)

                # 予測キャッシュ取得
                cached = conn.execute(
                    "SELECT * FROM predictions_cache WHERE race_id = ?",
                    (race_id,)
                ).fetchone()

                if not cached:
                    print(f"  ⏭️ {race_id}: キャッシュなし")
                    continue

                horses_raw = json.loads(cached["predictions_json"])
                # 馬番重複・馬番0・馬番19以上・同名馬を除去
                seen_nums = set()
                seen_names = set()
                horses = []
                for h in horses_raw:
                    num = h.get('horse_number', 0)
                    name = h.get('horse_name', '')
                    if num == 0 or num > 18:
                        continue
                    if num in seen_nums:
                        continue
                    if name and name in seen_names:
                        continue
                    seen_nums.add(num)
                    if name:
                        seen_names.add(name)
                    horses.append(h)
                all_bets = json.loads(cached["all_bets_json"])
                confidence = cached["confidence"]
                conf_reason = cached["conf_reason"] or ""
                should_bet = bool(cached["should_bet"])
                bet_reason = cached["bet_reason"] or ""

                # 結果データ取得
                res_rows = conn.execute("""
                    SELECT r.horse_number, r.finish_position, r.finish_time,
                           r.odds, r.popularity, r.last_3f, r.margin
                    FROM results r
                    WHERE r.race_id = ? AND r.finish_position > 0
                    ORDER BY r.finish_position
                """, (race_id,)).fetchall()

                has_results = len(res_rows) > 0
                race_results = {}
                for rr in res_rows:
                    race_results[rr['horse_number']] = {
                        'finish': rr['finish_position'],
                        'time': rr['finish_time'] or '',
                        'odds': rr['odds'] or 0,
                        'popularity': rr['popularity'] or 0,
                        'last_3f': rr['last_3f'] or 0,
                        'margin': rr['margin'] or '',
                    }

                # 最新のオッズ・人気をマージ
                db_results = conn.execute(
                    "SELECT horse_number, odds, popularity FROM results WHERE race_id = ?",
                    (race_id,)
                ).fetchall()
                odds_map = {r['horse_number']: r for r in db_results}
                for h in horses:
                    db_r = odds_map.get(h['horse_number'])
                    if db_r:
                        if db_r['odds'] and db_r['odds'] > 0:
                            h['odds_win'] = db_r['odds']
                        if db_r['popularity'] and db_r['popularity'] > 0:
                            h['popularity'] = db_r['popularity']
                    # 結果をマージ
                    res = race_results.get(h['horse_number'])
                    if res:
                        h['finish'] = res['finish']
                        h['time'] = res['time']
                        h['actual_odds'] = res['odds']
                        if res['popularity'] and res['popularity'] > 0:
                            h['popularity'] = res['popularity']
                        h['last_3f'] = res['last_3f']
                        h['margin'] = res['margin']
                    else:
                        h['finish'] = 0
                        h['time'] = ''
                        h['actual_odds'] = 0
                        h['last_3f'] = 0
                        h['margin'] = ''

                # 配当
                payout_rows = conn.execute("""
                    SELECT bet_type, combination, payout_amount, popularity
                    FROM payouts WHERE race_id = ?
                    ORDER BY bet_type, popularity
                """, (race_id,)).fetchall()
                race_payouts = [{
                    'bet_type': pr['bet_type'],
                    'combination': pr['combination'],
                    'payout': pr['payout_amount'],
                    'popularity': pr['popularity'],
                } for pr in payout_rows]

                # 妙味計算
                # 実オッズがあるかチェック（推定オッズかどうか）
                has_real_odds = any(h.get('popularity', 0) > 0 for h in horses)
                
                if has_real_odds:
                    # 実オッズベース: ベットのEVを使用
                    max_ev = 0.0
                    for bt_key, bt_bets in all_bets.items():
                        for b in bt_bets:
                            ev = b.get("ev", 0)
                            if ev > max_ev:
                                max_ev = ev
                else:
                    # 推定オッズのみ: AI確信度ベースで妙味スコアを算出
                    # ◎の勝率、上位集中度、穴馬の存在で判断
                    win_pcts = sorted([h.get("pred_win_pct", 0) for h in horses], reverse=True)
                    top1 = win_pcts[0] if win_pcts else 0
                    top3_sum = sum(win_pcts[:3])
                    gap = (win_pcts[0] - win_pcts[1]) if len(win_pcts) >= 2 else 0
                    # 混戦度 = 上位が拮抗しているほど妙味あり
                    entropy = -sum(p/100 * __import__('math').log2(max(p/100, 0.001)) for p in win_pcts if p > 0)
                    # スコア: 確信度高い（本命明確）→低妙味、混戦→高妙味
                    if top1 >= 25 and gap >= 10:
                        max_ev = 1.0  # 堅いレース（妙味低い）
                    elif top3_sum >= 45:
                        max_ev = 2.0  # やや堅い
                    elif entropy >= 3.5:
                        max_ev = 5.0  # 大混戦（高妙味）
                    elif entropy >= 3.0:
                        max_ev = 3.5  # 混戦（妙味あり）
                    else:
                        max_ev = 2.5  # 普通

                if max_ev >= 5.0:
                    myomi = "💎★★★"
                elif max_ev >= 3.0:
                    myomi = "💎★★"
                elif max_ev >= 1.8:
                    myomi = "💎★"
                else:
                    myomi = ""

                # レース傾向
                sorted_probs = sorted([h.get("pred_win", 0) for h in horses], reverse=True)
                top_prob = sorted_probs[0] if sorted_probs else 0
                second_prob = sorted_probs[1] if len(sorted_probs) > 1 else 0
                gap = top_prob - second_prob
                top3_total = sum(sorted_probs[:3])
                if top_prob >= 35 and gap >= 12:
                    race_tendency = "堅い（本命突出）"
                elif top_prob >= 25 and gap >= 6:
                    race_tendency = "やや堅い（軸馬明確）"
                elif top3_total >= 55:
                    race_tendency = "上位拮抗（実力伯仲）"
                elif top_prob <= 12:
                    race_tendency = "波乱含み（大混戦）"
                else:
                    race_tendency = "普通（中穴狙い可）"

                all_races.append({
                    "race_id": race_id,
                    "venue": race_info.get("venue", ""),
                    "race_number": race_info.get("race_number", 0),
                    "race_name": race_info.get("race_name", ""),
                    "grade": race_info.get("grade", ""),
                    "distance": race_info.get("distance", 0),
                    "surface": race_info.get("surface", ""),
                    "track_condition": race_info.get("track_condition", "良"),
                    "start_time": race_info.get("start_time", ""),
                    "horse_count": len(horses),
                    "horses": horses,
                    "all_bets": all_bets,
                    "should_bet": should_bet,
                    "bet_reason": bet_reason,
                    "confidence": confidence,
                    "conf_reason": conf_reason,
                    "myomi": myomi,
                    "max_ev": round(max_ev, 1),
                    "race_tendency": race_tendency,
                    "has_results": has_results,
                    "payouts": race_payouts if has_results else [],
                    "prediction_locked": False,
                })

            if not all_races:
                print(f"  ⏭️ {ds}: 予測データなし")
                continue

            # 妙味再計算（相対パーセンタイル、同値グループ均等分配）
            if len(all_races) >= 2:
                # EVでソートし、各レースに順位を付与
                sorted_races = sorted(all_races, key=lambda r: r["max_ev"])
                n = len(sorted_races)
                for i, r in enumerate(sorted_races):
                    pct = i / (n - 1) if n > 1 else 0.5
                    if pct >= 0.80:
                        r["myomi"] = "💎★★★"
                    elif pct >= 0.50:
                        r["myomi"] = "💎★★"
                    elif pct >= 0.20:
                        r["myomi"] = "💎★"
                    else:
                        r["myomi"] = ""

            # 会場グループ化
            venues = {}
            for r in all_races:
                v = r["venue"] or "不明"
                if v not in venues:
                    venues[v] = []
                venues[v].append(r)

            output = {
                "date": ds,
                "total_races": len(all_races),
                "venues": venues,
                "is_locked": False,
                "exported_at": datetime.now(JST).isoformat(),
            }

            out_path = os.path.join(output_dir, f"predictions_{ds}.json")
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump(output, f, ensure_ascii=False, separators=(',', ':'))

            print(f"  ✅ {ds}: {len(all_races)}レース → {out_path}")
            exported.append(ds)

    # 最新日付のインデックスを作成（既存とマージ）
    if exported:
        idx_path = os.path.join(output_dir, "index.json")

        # 既存のインデックスを読み込み
        existing_dates = []
        if os.path.exists(idx_path):
            try:
                with open(idx_path, "r", encoding="utf-8") as f:
                    existing = json.load(f)
                    existing_dates = existing.get("dates", [])
            except Exception:
                pass

        # マージして重複排除・ソート
        all_dates = sorted(set(existing_dates + exported))

        index = {
            "latest": all_dates[-1],
            "dates": all_dates,
            "updated_at": datetime.now(JST).isoformat(),
        }
        with open(idx_path, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False, indent=2)
        print(f"  📋 インデックス更新: {idx_path}")

    return exported


if __name__ == "__main__":
    from database import init_db
    init_db()

    if len(sys.argv) > 1:
        date_arg = sys.argv[1]
        export_predictions(date_arg)
    else:
        export_predictions()
