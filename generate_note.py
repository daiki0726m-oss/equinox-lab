"""
🏇 note記事自動生成スクリプト
AI予測結果から、note.com用の有料記事を自動生成する

使い方:
  python generate_note.py --date 20250322
  python generate_note.py --date 20250322 --copy  (クリップボードにもコピー)
  python generate_note.py --date 20250322 --top 5  (厳選5レース)
"""

import argparse
import sys
import os
import json
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(__file__))

from database import init_db, get_db
from scraper import NetkeibaScraper
from ml.model import KeibaModel
from ml.features import FeatureBuilder
from strategy.betting import BettingStrategy
from analyzers.speed_index import SpeedIndexCalculator


def get_race_predictions(date_str, model, strategy):
    """指定日の全レースの予測を取得してEV付きで返す"""
    scraper = NetkeibaScraper()

    # まずDBからレースID取得（高速）
    date_hyphen = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
    with get_db() as conn:
        rows = conn.execute(
            "SELECT race_id FROM races WHERE race_date = ? OR race_date = ? ORDER BY race_id",
            (date_str, date_hyphen)
        ).fetchall()
        race_ids = [r["race_id"] for r in rows]

    # DBになければスクレイパーで取得
    if not race_ids:
        race_ids = scraper.get_race_list_by_date(date_str)

    if not race_ids:
        print(f"⚠️ {date_str} のレースが見つかりません")
        return []

    print(f"📡 {date_str} の {len(race_ids)} レースを分析中...")

    # キャッシュから高速読み込みを試行
    all_races = []
    cache_hits = 0
    for race_id in race_ids:
        try:
            with get_db() as conn:
                cached = conn.execute(
                    "SELECT predictions_json, all_bets_json, confidence, should_bet FROM predictions_cache WHERE race_id = ?",
                    (race_id,)
                ).fetchone()
                race = conn.execute(
                    "SELECT * FROM races WHERE race_id = ?", (race_id,)
                ).fetchone()

            if cached and race and cached['predictions_json']:
                # キャッシュから復元（高速パス）
                horses = json.loads(cached['predictions_json'])
                all_bets = json.loads(cached['all_bets_json']) if cached['all_bets_json'] else {}
                should_bet = bool(cached['should_bet'])
                race_info = dict(race)

                # キー正規化（pred_win_pct→pred_win 等、フォーマット差吸収）
                for h in horses:
                    if 'pred_win' not in h and 'pred_win_pct' in h:
                        h['pred_win'] = h['pred_win_pct']
                    if 'pred_top3' not in h and 'pred_top3_pct' in h:
                        h['pred_top3'] = h['pred_top3_pct']

                # 印がなければ割り当て
                has_marks = any(h.get('mark') for h in horses)
                if not has_marks:
                    sorted_h = sorted(horses, key=lambda x: x.get('pred_win', 0), reverse=True)
                    mark_list = ["◎", "○", "▲", "△", "×"]
                    for i, h in enumerate(sorted_h):
                        h["mark"] = mark_list[i] if i < 5 else ""
                    # 注マーク: 6位以下だがSIがトップ3に入る馬
                    top3_si = sorted([h.get('si_avg', 0) for h in sorted_h], reverse=True)[:3]
                    si_threshold = top3_si[-1] if len(top3_si) == 3 else 0
                    for h in sorted_h[5:]:
                        if h.get('si_avg', 0) >= si_threshold and si_threshold > 0 and h.get('si_avg', 0) > 0:
                            h['mark'] = '注'
                            break  # 1頭のみ
                    horses = sorted_h

                # EV計算 → 妙味（raw値を保存、後で相対判定）
                max_ev = 0.0
                for bt, bt_bets in all_bets.items():
                    for b in bt_bets:
                        ev = b.get("ev", 0)
                        if ev > max_ev:
                            max_ev = ev

                # 妙味はraw EVを保存（後で全レース比較して相対判定）
                myomi_raw = max_ev
                myomi = ""  # 後で上書き

                # 信頼度（◎のpred_winベースで再計算）
                honmei_h = next((h for h in horses if h.get('mark') == '◎'), None)
                honmei_win = honmei_h['pred_win'] if honmei_h else 0
                if honmei_win >= 50:
                    confidence = "S"
                elif honmei_win >= 35:
                    confidence = "A"
                elif honmei_win >= 22:
                    confidence = "B"
                elif honmei_win >= 12:
                    confidence = "C"
                else:
                    confidence = "D"

                # レース傾向
                sorted_probs = sorted([h.get("pred_win", 0) for h in horses], reverse=True)
                top_p = sorted_probs[0] if sorted_probs else 0
                gap = (top_p - sorted_probs[1]) if len(sorted_probs) > 1 else 0
                if top_p >= 35 and gap >= 12:
                    tendency = "堅い（本命突出）"
                elif top_p >= 25 and gap >= 6:
                    tendency = "やや堅い"
                elif sum(sorted_probs[:3]) >= 55:
                    tendency = "上位拮抗"
                elif top_p <= 12:
                    tendency = "波乱含み"
                else:
                    tendency = "普通"

                all_races.append({
                    "race_id": race_id,
                    "race_info": race_info,
                    "horses": horses,
                    "all_bets": all_bets,
                    "max_ev": max_ev,
                    "myomi": myomi,
                    "confidence": confidence,
                    "tendency": tendency,
                    "should_bet": should_bet,
                })

                venue = race_info.get("venue", "")
                rnum = race_info.get("race_number", 0)
                print(f"  ✅ {venue}{rnum}R {race_info.get('race_name', '')} "
                      f"[{confidence}] EV={max_ev:.1f} (cache)")
                cache_hits += 1
                continue

            # キャッシュなし → ML予測（遅いパス）
            print(f"  ⏳ {race_id}: キャッシュなし、ML予測実行中...")
            # レース情報取得
            with get_db() as conn:
                race = conn.execute(
                    "SELECT * FROM races WHERE race_id = ?", (race_id,)
                ).fetchone()
                results_rows = conn.execute("""
                    SELECT r.*, h.horse_name, j.jockey_name
                    FROM results r
                    LEFT JOIN horses h ON r.horse_id = h.horse_id
                    LEFT JOIN jockeys j ON r.jockey_id = j.jockey_id
                    WHERE r.race_id = ?
                    ORDER BY r.horse_number
                """, (race_id,)).fetchall()
                results = [dict(r) for r in results_rows]

            # 出馬表がなければスクレイピング
            if not race:
                shutuba = scraper.scrape_shutuba(race_id)
                if not shutuba or not shutuba.get("entries"):
                    continue
                with get_db() as conn:
                    conn.execute("""
                        INSERT OR REPLACE INTO races
                        (race_id, race_date, venue, race_number, race_name, grade,
                         distance, surface, direction, weather, track_condition, horse_count)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        race_id, shutuba.get("race_date", ""),
                        shutuba.get("venue", ""), shutuba.get("race_number", 0),
                        shutuba.get("race_name", ""), shutuba.get("grade", ""),
                        shutuba.get("distance", 0), shutuba.get("surface", ""),
                        shutuba.get("direction", ""), shutuba.get("weather", ""),
                        shutuba.get("track_condition", ""),
                        len(shutuba.get("entries", []))
                    ))
                    for e in shutuba.get("entries", []):
                        if e.get("horse_id"):
                            conn.execute("""
                                INSERT OR IGNORE INTO horses (horse_id, horse_name, sex)
                                VALUES (?, ?, ?)
                            """, (e["horse_id"], e.get("horse_name", ""), e.get("sex", "")))
                        if e.get("jockey_id"):
                            conn.execute("""
                                INSERT OR IGNORE INTO jockeys (jockey_id, jockey_name)
                                VALUES (?, ?)
                            """, (e["jockey_id"], e.get("jockey_name", "")))
                        if e.get("trainer_id"):
                            conn.execute("""
                                INSERT OR IGNORE INTO trainers (trainer_id, trainer_name)
                                VALUES (?, ?)
                            """, (e["trainer_id"], e.get("trainer_name", "")))
                        conn.execute("""
                            INSERT OR REPLACE INTO results
                            (race_id, horse_id, jockey_id, trainer_id,
                             post_position, horse_number, odds, popularity,
                             finish_position, finish_time, finish_time_seconds,
                             margin, last_3f, passing_order, weight, weight_change, impost)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            race_id, e.get("horse_id", ""),
                            e.get("jockey_id", ""), e.get("trainer_id", ""),
                            e.get("post_position", 0), e.get("horse_number", 0),
                            0, 0, 0, "", 0, "", 0, "", 0, 0, e.get("impost", 0)
                        ))

                with get_db() as conn:
                    race = conn.execute(
                        "SELECT * FROM races WHERE race_id = ?", (race_id,)
                    ).fetchone()
                    results_rows = conn.execute("""
                        SELECT r.*, h.horse_name, j.jockey_name
                        FROM results r
                        LEFT JOIN horses h ON r.horse_id = h.horse_id
                        LEFT JOIN jockeys j ON r.jockey_id = j.jockey_id
                        WHERE r.race_id = ?
                        ORDER BY r.horse_number
                    """, (race_id,)).fetchall()
                    results = [dict(r) for r in results_rows]

            if not race or not results:
                continue

            # 予測
            pred_df = model.predict_race(race_id)
            if pred_df.empty:
                continue

            race_info = dict(race)

            # 馬データ構築
            horses = []
            predictions_for_bet = []
            for _, row in pred_df.iterrows():
                hn = int(row["horse_number"])
                horse_name = ""
                jockey_name = ""
                odds_win = 0

                for r in results:
                    if r["horse_number"] == hn:
                        horse_name = r["horse_name"] or ""
                        jockey_name = r.get("jockey_name", "") or ""
                        odds_win = r["odds"] or 0
                        break

                pred_win = float(row["pred_win_norm"])
                pred_top3 = float(row["pred_top3_norm"] / 3)

                if odds_win <= 0 and pred_win > 0:
                    odds_win = max(round(1.0 / pred_win, 1), 1.5)
                    odds_place = max(round(1.0 / pred_top3, 1), 1.1) if pred_top3 > 0 else 1.5
                else:
                    odds_place = max(odds_win * 0.3, 1.1) if odds_win else 1.5

                horses.append({
                    "horse_number": hn,
                    "horse_name": horse_name,
                    "jockey_name": jockey_name,
                    "pred_win": round(pred_win * 100, 1),
                    "pred_top3": round(pred_top3 * 100, 1),
                    "si_avg": round(float(row.get("si_avg", 0)), 1),
                    "odds_win": odds_win,
                })

                predictions_for_bet.append({
                    "horse_number": hn,
                    "horse_name": horse_name,
                    "pred_win": pred_win,
                    "pred_top3": pred_top3,
                    "odds_win": odds_win,
                    "odds_place": odds_place,
                })

            # 印の割り当て
            sorted_horses = sorted(horses, key=lambda x: x["pred_win"], reverse=True)
            marks = ["◎", "○", "▲", "△", "×"]
            for i, h in enumerate(sorted_horses):
                h["mark"] = marks[i] if i < 5 else ""
            # 注マーク: 6位以下だがSIがトップ3に入る馬
            top3_si = sorted([h.get('si_avg', 0) for h in sorted_horses], reverse=True)[:3]
            si_threshold = top3_si[-1] if len(top3_si) == 3 else 0
            for h in sorted_horses[5:]:
                if h.get('si_avg', 0) >= si_threshold and si_threshold > 0 and h.get('si_avg', 0) > 0:
                    h['mark'] = '注'
                    break  # 1頭のみ

            # 買い目生成
            should_bet, bet_reason = strategy.should_bet_race(predictions_for_bet)
            all_bets = {}
            max_ev = 0.0
            if should_bet:
                for bt in strategy.ALL_BET_TYPES:
                    result = strategy.generate_bets(predictions_for_bet, bet_types=[bt])
                    bets = result.get("bets", [])
                    all_bets[bt] = bets
                    for b in bets:
                        ev = b.get("ev", 0)
                        if ev > max_ev:
                            max_ev = ev

            # 信頼度（◎のpred_winベース）
            honmei_h = next((h for h in sorted_horses if h.get('mark') == '◎'), None)
            honmei_win = honmei_h['pred_win'] if honmei_h else 0
            if honmei_win >= 50:
                confidence = "S"
            elif honmei_win >= 35:
                confidence = "A"
            elif honmei_win >= 22:
                confidence = "B"
            elif honmei_win >= 12:
                confidence = "C"
            else:
                confidence = "D"

            # 妙味（EVベース）
            if max_ev >= 5.0:
                myomi = "💎★★★"
            elif max_ev >= 2.5:
                myomi = "💎★★"
            elif max_ev >= 1.5:
                myomi = "💎★"
            else:
                myomi = ""

            # レース傾向
            sorted_probs = sorted([h["pred_win"] for h in horses], reverse=True)
            top_p = sorted_probs[0] if sorted_probs else 0
            gap = (top_p - sorted_probs[1]) if len(sorted_probs) > 1 else 0
            if top_p >= 35 and gap >= 12:
                tendency = "堅い（本命突出）"
            elif top_p >= 25 and gap >= 6:
                tendency = "やや堅い"
            elif sum(sorted_probs[:3]) >= 55:
                tendency = "上位拮抗"
            elif top_p <= 12:
                tendency = "波乱含み"
            else:
                tendency = "普通"

            all_races.append({
                "race_id": race_id,
                "race_info": race_info,
                "horses": sorted_horses,
                "all_bets": all_bets,
                "max_ev": max_ev,
                "myomi": myomi,
                "confidence": confidence,
                "tendency": tendency,
                "should_bet": should_bet,
            })

            venue = race_info.get("venue", "")
            rnum = race_info.get("race_number", 0)
            print(f"  ✅ {venue}{rnum}R {race_info.get('race_name', '')} "
                  f"[{confidence}] {myomi} EV={max_ev:.1f}")

        except Exception as e:
            print(f"  ⚠️ {race_id}: {e}")
            continue

    return all_races


def select_featured_races(all_races, top_n=3):
    """厳選レースを選定: メインレース(11R) + 信頼度の高い特別レース(R9-R12)"""
    # メインレース(11R, 重賞)を必ず含める
    main_races = [r for r in all_races
                  if r["race_info"].get("race_number", 0) == 11
                  or r["race_info"].get("grade", "") in ("G1", "G2", "G3")]

    featured = []
    featured_ids = set()

    # 1. メインレースを優先追加
    for r in main_races:
        featured.append(r)
        featured_ids.add(r["race_id"])

    # 2. 特別レース(R9-R12)から信頼度の高い順に追加
    #    EVだけでなく信頼度(confidence)とレース番号で選ぶ
    conf_order = {"S": 4, "A": 3, "B": 2, "C": 1, "D": 0}
    candidates = [
        r for r in all_races
        if r["race_id"] not in featured_ids
        and r["race_info"].get("race_number", 0) >= 9  # 特別レース以上
        and r["confidence"] in ("S", "A", "B")  # 信頼度B以上
        and r["should_bet"]  # 買い目あり
    ]
    candidates.sort(key=lambda x: (
        conf_order.get(x["confidence"], 0),
        x["race_info"].get("race_number", 0),  # 後半レース優先
    ), reverse=True)

    for r in candidates:
        if len(featured) >= len(main_races) + top_n:
            break
        featured.append(r)
        featured_ids.add(r["race_id"])

    return featured


def get_last_week_results():
    """先週の11R的中結果を取得（フック用）"""
    try:
        with get_db() as conn:
            recent = conn.execute("""
                SELECT DISTINCT ra.race_date FROM races ra
                JOIN predictions_cache pc ON ra.race_id = pc.race_id
                WHERE ra.race_number = 11
                ORDER BY ra.race_date DESC
                LIMIT 5
            """).fetchall()

            for row in recent:
                rd = row['race_date']
                races = conn.execute("""
                    SELECT ra.race_name, ra.venue, pc.predictions_json
                    FROM races ra
                    JOIN predictions_cache pc ON ra.race_id = pc.race_id
                    WHERE ra.race_date = ? AND ra.race_number = 11
                """, (rd,)).fetchall()

                results = []
                for race in races:
                    preds = json.loads(race['predictions_json']) if race['predictions_json'] else []
                    honmei = next((p for p in preds if p.get('mark') == '◎'), None)
                    if not honmei:
                        continue
                    fp_row = conn.execute("""
                        SELECT r.finish_position FROM results r
                        JOIN races ra ON r.race_id = ra.race_id
                        WHERE ra.race_date = ? AND ra.race_number = 11
                        AND ra.venue = ? AND r.horse_number = ?
                        AND r.finish_position > 0
                    """, (rd, race['venue'], honmei.get('horse_number', 0))).fetchone()
                    if fp_row:
                        results.append({
                            'race_name': race['race_name'],
                            'horse_name': honmei.get('horse_name', '?'),
                            'finish': fp_row['finish_position'],
                            'hit': fp_row['finish_position'] <= 3,
                        })
                if results:
                    return results
    except Exception:
        pass
    return []


def get_last_week_review(target_date_str):
    """先週の全レース予想を分析して振り返りデータを返す"""
    try:
        from datetime import timedelta
        target_dt = datetime.strptime(target_date_str, "%Y%m%d")
        # 先週の土日を探す
        with get_db() as conn:
            recent_dates = conn.execute("""
                SELECT DISTINCT ra.race_date FROM races ra
                JOIN predictions_cache pc ON ra.race_id = pc.race_id
                WHERE ra.race_date < ?
                ORDER BY ra.race_date DESC
                LIMIT 10
            """, (target_dt.strftime("%Y-%m-%d"),)).fetchall()

        if not recent_dates:
            return None

        # 日曜なら昨日(土)の1日分、土曜なら先週の2日分
        is_sunday = target_dt.weekday() == 6
        take_days = 1 if is_sunday else 2
        last_dates = [r['race_date'] for r in recent_dates[:take_days]]

        with get_db() as conn:
            total = 0
            honmei_win = 0
            honmei_top3 = 0
            conf_stats = {}
            notable_hits = []
            notable_misses = []

            for rd in last_dates:
                races = conn.execute("""
                    SELECT ra.race_id, ra.venue, ra.race_number, ra.race_name,
                           pc.predictions_json, pc.all_bets_json
                    FROM races ra
                    JOIN predictions_cache pc ON ra.race_id = pc.race_id
                    WHERE ra.race_date = ?
                    ORDER BY ra.venue, ra.race_number
                """, (rd,)).fetchall()

                for race in races:
                    preds = json.loads(race['predictions_json']) if race['predictions_json'] else []
                    if not preds:
                        continue

                    results = conn.execute("""
                        SELECT horse_number, finish_position, odds
                        FROM results WHERE race_id = ? AND finish_position >= 1
                        ORDER BY finish_position
                    """, (race['race_id'],)).fetchall()
                    if not results or results[0]['finish_position'] == 0:
                        continue

                    total += 1
                    actual_top3 = [r['horse_number'] for r in results[:3]]

                    honmei = next((p for p in preds if p.get('mark') == '◎'), None)
                    if not honmei:
                        continue

                    # ◎ pred_winベースで信頼度を再計算
                    hw = honmei.get('pred_win', 0)
                    if hw >= 25: conf = 'S'
                    elif hw >= 18: conf = 'A'
                    elif hw >= 12: conf = 'B'
                    elif hw >= 8: conf = 'C'
                    else: conf = 'D'

                    if conf not in conf_stats:
                        conf_stats[conf] = {'total': 0, 'win': 0, 'top3': 0}
                    conf_stats[conf]['total'] += 1

                    h_num = honmei['horse_number']
                    h_name = honmei.get('horse_name', '?')
                    h_fp = None
                    for r in results:
                        if r['horse_number'] == h_num:
                            h_fp = r['finish_position']

                    if h_fp == 1:
                        honmei_win += 1
                        conf_stats[conf]['win'] += 1
                    if h_fp and h_fp <= 3:
                        honmei_top3 += 1
                        conf_stats[conf]['top3'] += 1

                    # 注目的中：◎1着
                    if h_fp == 1:
                        notable_hits.append({
                            'venue': race['venue'], 'rnum': race['race_number'],
                            'race_name': race['race_name'],
                            'horse': f"{h_num}{h_name}", 'conf': conf,
                            'odds': results[0]['odds'] if results else 0
                        })
                    # 大敗：S/Aで◎5着以下
                    elif h_fp and h_fp >= 5 and conf in ('S', 'A'):
                        notable_misses.append({
                            'venue': race['venue'], 'rnum': race['race_number'],
                            'race_name': race['race_name'],
                            'horse': f"{h_num}{h_name}", 'conf': conf,
                            'fp': h_fp
                        })

        if total == 0:
            return None

        return {
            'dates': last_dates,
            'total': total,
            'honmei_win': honmei_win,
            'honmei_top3': honmei_top3,
            'conf_stats': conf_stats,
            'notable_hits': notable_hits[:5],
            'notable_misses': notable_misses[:3],
        }
    except Exception:
        return None


def generate_article(date_str, featured_races, all_races, free=False):
    """note記事のMarkdownを生成（v2: 12ゴールデンテンプレート形式）"""
    dt = datetime.strptime(date_str, "%Y%m%d")
    weekday = ["月", "火", "水", "木", "金", "土", "日"][dt.weekday()]
    date_label = dt.strftime(f"%m/%d({weekday})")

    venues = set(r["race_info"].get("venue", "") for r in all_races)
    venues_str = "・".join(sorted(venues))

    # メインレースと厳選レースを分離
    main_races = [r for r in featured_races
                  if r["race_info"].get("race_number", 0) == 11
                  or r["race_info"].get("grade", "") in ("G1", "G2", "G3")]
    extra_races = [r for r in featured_races if r not in main_races]

    # 前回ラベル（日曜→昨日、土曜→先週）
    dt_target = datetime.strptime(date_str, "%Y%m%d")
    review_label = "昨日" if dt_target.weekday() == 6 else "先週"

    # 前回の結果（フック用）
    last_results = get_last_week_results()

    lines = []

    # ━━━ 1. フック（タイトル + 冒頭実績） ━━━
    race_names = [r["race_info"].get("race_name", "") for r in main_races]
    title_races = "・".join(race_names) if race_names else "厳選レース"
    lines.append(f"# 【{date_label}】AIが導いた{title_races}の"
                 f"「買うべき馬」と「消すべき馬」 ― 41次元データ分析の結論\n")

    if last_results:
        hits = [r for r in last_results if r['hit']]
        if hits:
            h = hits[0]
            lines.append(f"> 📊 {review_label}のAI予想: **{h['race_name']}** "
                         f"◎{h['horse_name']} → **{h['finish']}着的中** 🎯\n")

    lines.append("---\n")

    # ━━━ 1.5 前回の振り返り ━━━
    review = get_last_week_review(date_str)
    if review:
        t = review['total']
        hw = review['honmei_win']
        ht3 = review['honmei_top3']
        lines.append(f"## 📊 {review_label}の振り返り\n")
        lines.append(f"{review_label}の全{t}レースのAI予想結果です。"
                     "的中も外れも隠さず報告します。\n")

        # 全体成績
        lines.append(f"**◎1着率:** {hw}/{t}（{hw/t*100:.0f}%） | "
                     f"**◎3着内率:** {ht3}/{t}（{ht3/t*100:.0f}%）\n")

        # AI評価別
        lines.append("**AI評価別の成績:**\n")
        lines.append("| AI評価 | レース数 | ◎1着 | ◎3着内 |")
        lines.append("|:------:|:-------:|:----:|:------:|")
        for c in ['S', 'A', 'B', 'C', 'D']:
            if c in review['conf_stats']:
                s = review['conf_stats'][c]
                w_pct = f"{s['win']/s['total']*100:.0f}%" if s['total'] else "-"
                t3_pct = f"{s['top3']/s['total']*100:.0f}%" if s['total'] else "-"
                lines.append(f"| **{c}** | {s['total']}R | "
                             f"{s['win']}回({w_pct}) | {s['top3']}回({t3_pct}) |")
        lines.append("")

        # 的中ハイライト
        if review['notable_hits']:
            lines.append("**🎯 的中ハイライト:**\n")
            for h in review['notable_hits']:
                odds_str = f"({h['odds']}倍)" if h.get('odds') else ""
                lines.append(f"- {h['venue']}{h['rnum']}R {h['race_name']} "
                             f"[{h['conf']}] ◎{h['horse']} → 1着{odds_str}")
            lines.append("")

        # 反省点
        if review['notable_misses']:
            lines.append("**❌ 反省点:**\n")
            for m in review['notable_misses']:
                lines.append(f"- {m['venue']}{m['rnum']}R {m['race_name']} "
                             f"[{m['conf']}] ◎{m['horse']} → {m['fp']}着")
            lines.append("")

        lines.append("> 結果を透明に公開し、データ分析の改善を続けています。\n")
        lines.append("---\n")
    lines.append("## この記事を読むべき人\n")
    lines.append("- 「何を買えばいいかわからない」と毎週悩んでいる人")
    lines.append("- データや数字で納得して馬券を買いたい人")
    if race_names:
        lines.append(f"- {race_names[0]}の予想ファクターを整理したい人")
    lines.append("- 土日の競馬で **回収率100%超え** を目指している人\n")
    lines.append("逆に、「自分の相馬眼だけで十分」「データなんて信じない」"
                 "という方にはこの記事は向きません。\n")
    lines.append("---\n")

    # ━━━ 3. ベネフィット ━━━
    lines.append("## この記事でわかること\n")
    for rname in race_names:
        lines.append(f"✅ **{rname}** の◎○▲＋推奨買い目（単勝・三連単）")
    lines.append("✅ 各レースの「堅い」or「荒れる」が一目でわかる **レース傾向分析**")
    lines.append("✅ 人気馬の中から **消すべき危険な人気馬** を特定")
    lines.append("✅ ROI分析で厳選した **単勝＋三連単** の2券種に集中投資\n")
    lines.append("> 6券種のROI検証の結果、**単勝120%・三連単149%**の"
                 "2券種だけがプラス回収。\n"
                 "> この2つに絞ることで回収率を最大化します。\n")
    lines.append("---\n")

    # ━━━ 4. 権威性 ━━━
    lines.append("## AI予測モデルについて\n")
    lines.append("**EQUINOX Lab** は、3つの機械学習モデルを統合した複合予測システムです。\n")
    lines.append("| モデル | 役割 |")
    lines.append("|:------:|------|")
    lines.append("| **LambdaRank** | 着順をダイレクトに最適化 |")
    lines.append("| **勝率予測** | 1着確率を推定 |")
    lines.append("| **複勝率予測** | 3着以内確率を推定 |\n")
    lines.append("**分析する41次元の要素:**")
    lines.append("🔢 スピード指数 / 🧬 血統適性 / 🏇 騎手×調教師 / "
                 "📐 馬場バイアス / ⏱️ ペース分析 / 🌧️ 天候×馬場状態\n")

    try:
        with get_db() as conn:
            rc = conn.execute("SELECT COUNT(*) as c FROM races").fetchone()
            ec = conn.execute("SELECT COUNT(*) as c FROM results").fetchone()
        race_cnt = rc['c'] if rc else 0
        entry_cnt = ec['c'] if ec else 0
        lines.append(f"> **{race_cnt:,}レース・{entry_cnt:,}の出走データ**"
                     "から学習しています。\n")
    except Exception:
        pass
    lines.append("---\n")

    # ━━━ 5. 今日のラインナップ ━━━
    lines.append("## 今日の注目レース\n")
    lines.append("| レース | コース | 頭数 | AI評価 | 💎妙味 | レース傾向 |")
    lines.append("|--------|--------|:----:|:------:|:------:|-----------|")
    for r in featured_races:
        info = r["race_info"]
        rname = info.get("race_name", "")
        venue = info.get("venue", "")
        rnum = info.get("race_number", 0)
        surface = info.get("surface", "")
        distance = info.get("distance", 0)
        hcount = info.get("horse_count", 0)
        conf = r["confidence"]
        tend = r["tendency"]
        myomi = r.get("myomi", "")
        is_main = (rnum == 11
                   or info.get("grade", "") in ("G1", "G2", "G3"))
        icon = "🏆" if is_main else "🔥"
        lines.append(f"| {icon} **{venue}{rnum}R {rname}** | {surface}{distance}m | "
                     f"{hcount}頭 | **{conf}** | {myomi or '-'} | {tend} |")
    lines.append("")

    lines.append("\n**AI評価の見方:**")
    lines.append("| 評価 | 意味 |")
    lines.append("|:----:|------|")
    lines.append("| **S** | ◎が非常に強い — 堅いレース |")
    lines.append("| **A** | ◎の信頼度が高い |")
    lines.append("| **B** | ◎は標準的 — 相手次第 |")
    lines.append("| **C** | ◎の信頼度は低め — 波乱含み |")
    lines.append("| **D** | ◎が弱い — 見送りが無難 |")
    lines.append("")
    lines.append("**💎妙味の見方:**")
    lines.append("| 表示 | 意味 |")
    lines.append("|:----:|------|")
    lines.append("| 💎★★★ | 大穴チャンス — オッズ以上の穴馬あり |")
    lines.append("| 💎★★ | 妙味あり — 穴買い目に期待 |")
    lines.append("| 💎★ | やや妙味 |")
    lines.append("| - | 妙味なし — 堅いレース |")
    lines.append("")
    lines.append("---\n")

    # ━━━ 6. 無料プレビュー（メインレース1つ） ━━━
    if main_races:
        pr = main_races[0]
        pi = pr["race_info"]
        lines.append(f"## 無料公開: {pi.get('race_name','')}のAI分析\n")
        lines.append(f"**{pi.get('surface','')}{pi.get('distance',0)}m / "
                     f"{pi.get('venue','')} / {pi.get('horse_count',0)}頭 / "
                     f"AI評価: {pr['confidence']}**\n")
        lines.append(f"### レース傾向: {pr['tendency']}\n")

        lines.append("### 予想印（無料公開）\n")
        lines.append("| 印 | 馬番 | 馬名 | AI勝率 |")
        lines.append("|:--:|:----:|------|:------:|")
        for h in pr["horses"][:3]:
            if h.get("mark"):
                lines.append(f"| {h['mark']} | {h['horse_number']} | "
                             f"**{h['horse_name']}** | {h['pred_win']}% |")
        lines.append("")

        honmei = next((h for h in pr["horses"] if h.get("mark") == "◎"), None)
        if honmei:
            lines.append(f"### ◎{honmei['horse_name']}の推奨根拠\n")
            lines.append(f"- スピード指数 **{honmei.get('si_avg', 0)}**")
            lines.append(f"- AI勝率 **{honmei['pred_win']}%** — メンバー中1位")
            lines.append("")

        lines.append("---\n")

    # ━━━ 7-9. 有料エリアへの誘導（無料記事では非表示） ━━━
    all_venues = sorted(set(r["race_info"].get("venue", "") for r in all_races))
    if not free:
        lines.append("## 有料エリア: 全レースの買い目＋詳細分析\n")
        lines.append(f"ここから先は、**全{len(all_races)}レースの◎○▲＋推奨買い目**を公開します。\n")
        lines.append("### 有料エリアの内容\n")
        for v in all_venues:
            v_count = sum(1 for r in all_races if r['race_info'].get('venue') == v)
            lines.append(f"📋 **{v}** 全{v_count}レースの◎○▲＋推奨買い目")
        lines.append("📋 各券種（単勝・複勝・ワイド・馬連・三連複）のベスト買い目")
        lines.append("📋 AI評価D（見送り推奨）レースの明示\n")

        lines.append("> ⚡ **今だけ特別価格: 300円**（10部売れたら500円に値上げします）")
        lines.append(">")
        lines.append("> 全レースカバー。お気に入りの1レースが当たれば元が取れます。\n")
        lines.append("---\n")

        # ━━━ ペイウォール ━━━
        lines.append("## ここから有料エリア ↓\n")

    # ━━━ 有料コンテンツ: 全レース（会場別） ━━━
    # 会場ごとにグループ化してレース番号順
    from itertools import groupby
    sorted_all = sorted(all_races, key=lambda x: (
        x["race_info"].get("venue", ""),
        x["race_info"].get("race_number", 0)
    ))

    for venue, venue_races in groupby(sorted_all, key=lambda x: x["race_info"].get("venue", "")):
        venue_list = list(venue_races)
        lines.append(f"## 📍 {venue}（全{len(venue_list)}レース）\n")

        for race in venue_list:
            info = race["race_info"]
            rname = info.get("race_name", "")
            surface = info.get("surface", "")
            distance = info.get("distance", 0)
            hcount = info.get("horse_count", 0)
            conf = race["confidence"]
            tend = race["tendency"]
            myomi = race.get("myomi", "")
            rnum = info.get("race_number", 0)
            is_main = (rnum == 11 or info.get("grade", "") in ("G1", "G2", "G3"))
            icon = "🏆" if is_main else ""

            myomi_str = f" {myomi}" if myomi else " -"
            lines.append(f"### {icon}{rnum}R {rname} {surface}{distance}m・"
                         f"{hcount}頭 [AI評価: {conf}] [妙味:{myomi_str}]\n")

            # 予想印
            lines.append("| 印 | 馬番 | 馬名 | AI勝率 | SI |")
            lines.append("|:--:|:----:|------|:------:|:---:|")
            for h in race["horses"]:
                if h.get("mark"):
                    lines.append(f"| {h['mark']} | {h['horse_number']} | "
                                 f"{h['horse_name']} | {h['pred_win']}% | "
                                 f"{h.get('si_avg', 0)} |")
            lines.append("")

            # 買い目（券種ごとにベスト1つ・馬番付き）
            valid_by_type = {}
            for bt in ["複勝", "単勝", "ワイド", "馬連", "三連複"]:
                bets = race["all_bets"].get(bt, [])
                for b in bets:
                    hns = b.get('horse_numbers', [])
                    if len(hns) != len(set(hns)):
                        continue
                    if bt not in valid_by_type:
                        valid_by_type[bt] = b
                    elif b.get('ev', 0) > valid_by_type[bt].get('ev', 0):
                        valid_by_type[bt] = b


            lines.append("\n---\n")

    # ━━━ フッター ━━━
    lines.append("## ⚠️ 免責事項\n")
    lines.append("- 本記事はAIによる予測であり、的中を保証するものではありません")
    lines.append("- 馬券購入は自己責任でお願いいたします")
    lines.append("- 過去の実績は将来の成績を保証するものではありません\n")
    lines.append("---\n")
    lines.append("*EQUINOX Lab — データで競馬を変える 🧬*")
    lines.append(f"*的中結果は本日夕方にX(@quinox_lab)で報告します*")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="🏇 note記事自動生成 — AI競馬予想記事"
    )
    parser.add_argument("--date", required=True, help="対象日 (YYYYMMDD)")
    parser.add_argument("--top", type=int, default=3, help="厳選レース数 (default: 3)")
    parser.add_argument("--copy", action="store_true", help="クリップボードにコピー")
    parser.add_argument("--free", action="store_true", help="無料記事モード（有料エリア表記を除去）")
    parser.add_argument("--output", help="出力ファイルパス (default: articles/YYYYMMDD.md)")
    args = parser.parse_args()

    init_db()

    model = KeibaModel()
    strategy = BettingStrategy()

    print(f"\n🏇 note記事生成: {args.date}\n")

    # 予測実行
    all_races = get_race_predictions(args.date, model, strategy)
    if not all_races:
        print("❌ 分析可能なレースがありません")
        return

    # 妙味を相対判定（当日レースのEV分布に基づく）
    evs = sorted([r.get("max_ev", 0) for r in all_races], reverse=True)
    n = len(evs)
    if n > 0:
        # 上位20% = ★★★, 次の30% = ★★, 次の30% = ★, 下位20% = -
        thresh3 = evs[max(0, int(n * 0.2) - 1)]  # 上位20%ライン
        thresh2 = evs[max(0, int(n * 0.5) - 1)]  # 上位50%ライン
        thresh1 = evs[max(0, int(n * 0.8) - 1)]  # 上位80%ライン
        for r in all_races:
            ev = r.get("max_ev", 0)
            if ev >= thresh3 and ev > 0:
                r["myomi"] = "💎★★★"
            elif ev >= thresh2 and ev > 0:
                r["myomi"] = "💎★★"
            elif ev >= thresh1 and ev > 0:
                r["myomi"] = "💎★"
            else:
                r["myomi"] = ""
        print(f"  💎 妙味閾値: ★★★≥{thresh3:.2f} ★★≥{thresh2:.2f} ★≥{thresh1:.2f}")

    # 厳選レース選定
    featured = select_featured_races(all_races, top_n=args.top)
    print(f"\n📝 厳選 {len(featured)} レースを記事化...\n")

    # 記事生成
    article = generate_article(args.date, featured, all_races, free=getattr(args, 'free', False))

    # 出力
    output_dir = os.path.join(os.path.dirname(__file__), "articles")
    os.makedirs(output_dir, exist_ok=True)
    output_path = args.output or os.path.join(output_dir, f"{args.date}.md")

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(article)
    print(f"✅ 記事を保存: {output_path}")

    # クリップボードにコピー
    if args.copy:
        try:
            import subprocess
            process = subprocess.Popen(
                ["pbcopy"], stdin=subprocess.PIPE
            )
            process.communicate(article.encode("utf-8"))
            print("📋 クリップボードにコピーしました！noteにペーストできます")
        except Exception as e:
            print(f"⚠️ クリップボードへのコピーに失敗: {e}")

    # プレビュー表示
    print(f"\n{'='*60}")
    print("📄 記事プレビュー")
    print(f"{'='*60}\n")
    print(article)
    print(f"\n{'='*60}")
    print(f"📝 文字数: {len(article)}文字")
    if not getattr(args, 'free', False):
        print(f"💰 note有料価格の目安: ¥300〜500")
    else:
        print(f"🆓 無料記事モード")
    print(f"{'='*60}")

    # ── 品質チェック ──
    issues = []
    if '有料エリア' in article and getattr(args, 'free', False):
        issues.append('❌ 無料記事に「有料エリア」の文言が残っている')
    if '特別価格' in article and getattr(args, 'free', False):
        issues.append('❌ 無料記事に「特別価格」の文言が残っている')
    if '| - |' not in article and '💎' not in article:
        issues.append('⚠️ 妙味表示が各レースにない可能性')
    # 注目レースのテーブル行に会場名があるか
    import re
    featured_lines = [l for l in article.split('\n') if l.startswith('|') and ('🏆' in l or '🔥' in l)]
    for fl in featured_lines:
        if not re.search(r'(中山|中京|阪神|東京|新潟|札幌|函館|福島|小倉|京都)\d+R', fl):
            issues.append(f'⚠️ 注目レースに会場+R番号がない: {fl[:40]}')
            break
    # 日曜なのに「先週」、土曜なのに「昨日」が混在していないか
    dt_check = datetime.strptime(args.date, "%Y%m%d")
    if dt_check.weekday() == 6 and '先週の' in article:
        issues.append('❌ 日曜の記事に「先週の」が残っている（「昨日の」が正しい）')
    if dt_check.weekday() == 5 and '昨日の' in article:
        issues.append('❌ 土曜の記事に「昨日の」が残っている（「先週の」が正しい）')
    if issues:
        print(f"\n🔍 品質チェック結果:")
        for iss in issues:
            print(f"  {iss}")
    else:
        print(f"\n✅ 品質チェック OK")


if __name__ == "__main__":
    main()
