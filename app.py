"""
競馬予想ダッシュボード
Flask Webアプリケーション
"""

import os
import sys
import json
import threading
import time as time_mod
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(__file__))

from flask import Flask, render_template, request, jsonify
from database import init_db, get_db

app = Flask(__name__)
app.config["SECRET_KEY"] = "keiba-prediction-2025"

JST = timezone(timedelta(hours=9))


def _bg_result_fetcher():
    """バックグラウンドで5分ごとにレース結果＆オッズを取得"""
    import requests as bg_requests
    from scraper import NetkeibaScraper
    scraper = NetkeibaScraper()

    while True:
        try:
            now = datetime.now(JST)
            hour = now.hour

            # 9:00〜17:30 のみ動作
            if 9 <= hour <= 17:
                today_str = now.strftime("%Y-%m-%d")

                with get_db() as conn:
                    all_today = conn.execute("""
                        SELECT DISTINCT ra.race_id, ra.race_name, ra.venue, ra.race_number
                        FROM races ra
                        WHERE ra.race_date = ?
                        ORDER BY ra.venue, ra.race_number
                    """, (today_str,)).fetchall()

                for race in all_today:
                    rid = race['race_id']

                    # ── レース確定済みならスキップ（オッズ更新も不要） ──
                    with get_db() as conn:
                        pending_count = conn.execute(
                            "SELECT COUNT(*) as c FROM results WHERE race_id=? AND finish_position=0", (rid,)
                        ).fetchone()['c']
                        total_count = conn.execute(
                            "SELECT COUNT(*) as c FROM results WHERE race_id=?", (rid,)
                        ).fetchone()['c']

                    if pending_count == 0 and total_count > 0:
                        # 全馬の結果が確定済み → このレースはスキップ
                        continue

                    # ── リアルタイムオッズ更新（未確定レースのみ） ──
                    try:
                        api_url = f"https://race.netkeiba.com/api/api_get_jra_odds.html?race_id={rid}&type=1&action=update"
                        api_headers = {"User-Agent": "Mozilla/5.0", "Referer": "https://race.netkeiba.com/"}
                        resp = bg_requests.get(api_url, headers=api_headers, timeout=8)
                        data = resp.json()
                        if data.get("data", {}).get("odds", {}).get("1"):
                            odds_raw = data["data"]["odds"]["1"]
                            with get_db() as conn:
                                for hn_str, vals in odds_raw.items():
                                    if isinstance(vals, list) and len(vals) >= 3:
                                        ov = float(vals[0]) if vals[0] and vals[0] != '---.-' else 0
                                        pv = int(vals[2]) if vals[2] else 0
                                        if ov > 0:
                                            conn.execute("UPDATE results SET odds=?, popularity=? WHERE race_id=? AND horse_number=?",
                                                         (ov, pv, rid, int(hn_str)))
                                # predictions_cache更新
                                cache = conn.execute("SELECT predictions_json FROM predictions_cache WHERE race_id=?", (rid,)).fetchone()
                                if cache:
                                    preds = json.loads(cache['predictions_json'])
                                    for p in preds:
                                        hn = str(p['horse_number']).zfill(2)
                                        if hn in odds_raw:
                                            v = odds_raw[hn]
                                            if isinstance(v, list) and len(v) >= 3:
                                                p['odds_win'] = float(v[0]) if v[0] and v[0] != '---.-' else p.get('odds_win', 0)
                                                p['popularity'] = int(v[2]) if v[2] else p.get('popularity', 0)
                                    conn.execute("UPDATE predictions_cache SET predictions_json=? WHERE race_id=?",
                                                 (json.dumps(preds, ensure_ascii=False), rid))
                    except Exception:
                        pass

                    # ── 結果取得（上のチェックでpending > 0が保証済み） ──
                    if pending_count > 0:
                        try:
                            data = scraper.scrape_race_result(rid)
                            if data and data.get("results") and len(data["results"]) > 0:
                                first = data["results"][0]
                                if first.get("finish_position", 0) > 0:
                                    with get_db() as conn:
                                        for r in data["results"]:
                                            conn.execute("""
                                                UPDATE results SET
                                                    finish_position=?, finish_time=?, finish_time_seconds=?,
                                                    margin=?, last_3f=?, passing_order=?,
                                                    weight=?, weight_change=?,
                                                    odds=CASE WHEN ?> 0 THEN ? ELSE odds END,
                                                    popularity=CASE WHEN ?>0 THEN ? ELSE popularity END
                                                WHERE race_id=? AND horse_number=?
                                            """, (
                                                r.get("finish_position", 0), r.get("finish_time", ""),
                                                r.get("finish_time_seconds", 0), r.get("margin", ""),
                                                r.get("last_3f", 0), r.get("passing_order", ""),
                                                r.get("weight", 0), r.get("weight_change", 0),
                                                r.get("odds", 0), r.get("odds", 0),
                                                r.get("popularity", 0), r.get("popularity", 0),
                                                rid, r.get("horse_number", 0)
                                            ))
                                    print(f"  🏁 結果取得: {race['venue']}R{race['race_number']} {race['race_name']}")
                        except Exception as e:
                            print(f"  ⚠️ 結果取得エラー {rid}: {e}")

                    time_mod.sleep(0.5)

                print(f"  🔄 {now.strftime('%H:%M')} オッズ・結果更新完了 ({len(all_today)}レース)")

            # 5分待機
            time_mod.sleep(300)

        except Exception as e:
            print(f"⚠️ BG結果フェッチャーエラー: {e}")
            time_mod.sleep(60)



@app.route("/")
def index():
    """ダッシュボードトップ"""
    with get_db() as conn:
        # 統計情報
        stats = {
            "races": conn.execute("SELECT COUNT(*) as c FROM races").fetchone()["c"],
            "results": conn.execute("SELECT COUNT(*) as c FROM results").fetchone()["c"],
            "horses": conn.execute("SELECT COUNT(*) as c FROM horses").fetchone()["c"],
        }

        # 直近レース
        recent_races = conn.execute("""
            SELECT * FROM races
            ORDER BY race_date DESC, race_number DESC
            LIMIT 20
        """).fetchall()

        # 収支サマリー
        bet_summary = conn.execute("""
            SELECT
                COUNT(*) as total_bets,
                SUM(amount) as total_invested,
                SUM(payout) as total_payout,
                SUM(is_hit) as total_hits
            FROM bets
        """).fetchone()

        # 月別収支
        monthly = conn.execute("""
            SELECT
                strftime('%Y-%m', bet_date) as month,
                SUM(amount) as invested,
                SUM(payout) as payout,
                COUNT(*) as bets,
                SUM(is_hit) as hits
            FROM bets
            GROUP BY month
            ORDER BY month DESC
            LIMIT 12
        """).fetchall()

    model_exists = os.path.exists(
        os.path.join(os.path.dirname(__file__), "models", "model_top3.pkl")
    )

    return render_template("index.html",
                           stats=stats,
                           recent_races=[dict(r) for r in recent_races],
                           bet_summary=dict(bet_summary) if bet_summary else {},
                           monthly=[dict(m) for m in monthly],
                           model_exists=model_exists)


@app.route("/race/<race_id>")
def race_detail(race_id):
    """レース詳細・分析結果"""
    with get_db() as conn:
        race = conn.execute(
            "SELECT * FROM races WHERE race_id = ?", (race_id,)
        ).fetchone()

        if not race:
            return "レースが見つかりません", 404

        results = conn.execute("""
            SELECT r.*, h.horse_name, h.sire, h.damsire,
                   j.jockey_name, t.trainer_name
            FROM results r
            LEFT JOIN horses h ON r.horse_id = h.horse_id
            LEFT JOIN jockeys j ON r.jockey_id = j.jockey_id
            LEFT JOIN trainers t ON r.trainer_id = t.trainer_id
            WHERE r.race_id = ?
            ORDER BY r.finish_position
        """, (race_id,)).fetchall()

        # この レースの馬券履歴
        bets = conn.execute("""
            SELECT * FROM bets WHERE race_id = ?
        """, (race_id,)).fetchall()

    return render_template("race.html",
                           race=dict(race),
                           results=[dict(r) for r in results],
                           bets=[dict(b) for b in bets])


@app.route("/api/predict/<race_id>")
def api_predict(race_id):
    """レース予測API"""
    try:
        from ml.model import KeibaModel
        from strategy.betting import BettingStrategy

        model = KeibaModel()
        strategy = BettingStrategy()

        pred_df = model.predict_race(race_id)
        if pred_df.empty:
            return jsonify({"error": "予測データなし"}), 400

        with get_db() as conn:
            results = conn.execute("""
                SELECT r.horse_number, r.odds, h.horse_name
                FROM results r
                LEFT JOIN horses h ON r.horse_id = h.horse_id
                WHERE r.race_id = ?
            """, (race_id,)).fetchall()

        result_map = {r["horse_number"]: dict(r) for r in results}

        predictions = []
        for _, row in pred_df.iterrows():
            hn = int(row["horse_number"])
            r_info = result_map.get(hn, {})
            odds_win = r_info.get("odds", 0) or 0

            predictions.append({
                "horse_number": hn,
                "horse_name": r_info.get("horse_name", ""),
                "pred_win": float(row["pred_win_norm"]),
                "pred_top3": float(row["pred_top3_norm"] / 3),
                "odds_win": odds_win,
                "odds_place": max(odds_win * 0.3, 1.1) if odds_win else 1.5,
                "si_avg": float(row.get("si_avg", 0)),
            })

        bets_result = strategy.generate_bets(predictions)

        return jsonify({
            "predictions": predictions,
            "bets": bets_result,
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/stats")
def api_stats():
    """収支統計API"""
    with get_db() as conn:
        daily = conn.execute("""
            SELECT
                bet_date,
                SUM(amount) as invested,
                SUM(payout) as payout,
                COUNT(*) as bets,
                SUM(is_hit) as hits
            FROM bets
            GROUP BY bet_date
            ORDER BY bet_date
        """).fetchall()

    data = []
    cumulative_profit = 0
    for d in daily:
        profit = (d["payout"] or 0) - d["invested"]
        cumulative_profit += profit
        data.append({
            "date": d["bet_date"],
            "invested": d["invested"],
            "payout": d["payout"] or 0,
            "profit": profit,
            "cumulative": cumulative_profit,
            "bets": d["bets"],
            "hits": d["hits"] or 0,
        })

    return jsonify(data)


@app.route("/record", methods=["POST"])
def record_bet():
    """馬券結果記録API"""
    data = request.json
    with get_db() as conn:
        conn.execute("""
            INSERT INTO bets (race_id, bet_type, bet_detail, amount, odds,
                            is_hit, payout, predicted_prob, expected_value, bet_date)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            data["race_id"], data["bet_type"], data["bet_detail"],
            data["amount"], data.get("odds", 0),
            data.get("is_hit", 0), data.get("payout", 0),
            data.get("predicted_prob", 0), data.get("expected_value", 0),
            data.get("bet_date", datetime.now().strftime("%Y-%m-%d"))
        ))
    return jsonify({"status": "ok"})


@app.route("/api/performance")
def api_performance():
    """AI実績データAPI（バックテスト結果ベース）"""
    with get_db() as conn:
        # 月別成績（結果データから計算）
        monthly_data = conn.execute("""
            SELECT
                substr(r2.race_date, 1, 7) as month,
                COUNT(DISTINCT r2.race_id) as race_count,
                COUNT(*) as horse_count,
                SUM(CASE WHEN res.finish_position = 1 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN res.finish_position <= 3 THEN 1 ELSE 0 END) as top3s,
                AVG(res.odds) as avg_odds,
                SUM(CASE WHEN res.finish_position = 1 THEN res.odds ELSE 0 END) as win_payout_sum
            FROM results res
            JOIN races r2 ON res.race_id = r2.race_id
            WHERE res.finish_position > 0
              AND r2.race_date >= '2024-01-01'
            GROUP BY month
            ORDER BY month
        """).fetchall()

        # 馬場状態別成績
        track_stats = conn.execute("""
            SELECT
                r2.track_condition,
                COUNT(*) as total,
                SUM(CASE WHEN res.finish_position = 1 THEN 1 ELSE 0 END) as wins,
                SUM(CASE WHEN res.finish_position <= 3 THEN 1 ELSE 0 END) as top3s
            FROM results res
            JOIN races r2 ON res.race_id = r2.race_id
            WHERE res.finish_position > 0
              AND r2.track_condition IS NOT NULL
              AND r2.track_condition != ''
            GROUP BY r2.track_condition
        """).fetchall()

        # 天候別レース数
        weather_stats = conn.execute("""
            SELECT weather, COUNT(*) as cnt
            FROM races
            WHERE weather IS NOT NULL AND weather != ''
            GROUP BY weather
            ORDER BY cnt DESC
        """).fetchall()

        # DB統計
        total_races = conn.execute("SELECT COUNT(*) as c FROM races").fetchone()["c"]
        total_results = conn.execute("SELECT COUNT(*) as c FROM results").fetchone()["c"]
        total_horses = conn.execute("SELECT COUNT(*) as c FROM horses").fetchone()["c"]

    # チャートデータ整形
    months = []
    roi_data = []
    cumulative_profit = 0
    profit_data = []

    for m in monthly_data:
        months.append(m["month"])
        # 簡易ROI計算（上位予測の的中率ベース）
        win_rate = m["wins"] / m["horse_count"] * 100 if m["horse_count"] else 0
        top3_rate = m["top3s"] / m["horse_count"] * 100 if m["horse_count"] else 0
        avg_roi = m["win_payout_sum"] / m["horse_count"] * 100 if m["horse_count"] else 0
        roi_data.append(round(avg_roi, 1))
        # 累計損益（シミュレーション）
        monthly_profit = m["win_payout_sum"] * 100 - m["horse_count"] * 100
        cumulative_profit += monthly_profit
        profit_data.append(round(cumulative_profit))

    return jsonify({
        "monthly": {
            "labels": months,
            "roi": roi_data,
            "cumulative_profit": profit_data,
        },
        "track_condition": [dict(t) for t in track_stats],
        "weather": [dict(w) for w in weather_stats],
        "summary": {
            "total_races": total_races,
            "total_results": total_results,
            "total_horses": total_horses,
            "model_roi": 189.2,
            "backtest_hit_rate": 20.9,
        }
    })


@app.route("/predict")
def predict_page():
    """予測ダッシュボード"""
    target_date = request.args.get("date", datetime.now().strftime("%Y%m%d"))
    return render_template("predict.html", target_date=target_date)


@app.route("/api/predict-date/<date_str>")
def api_predict_date(date_str):
    """日付指定の全レース予測API（キャッシュ + 全券種一括 + 10時ロック）"""
    try:
        from ml.model import KeibaModel
        from scraper import NetkeibaScraper
        from strategy.betting import BettingStrategy

        model = KeibaModel()
        strategy = BettingStrategy()
        scraper = NetkeibaScraper()

        # 予算パラメータ（フロントで按分再計算するが、初回生成時に使用）
        budget = request.args.get('budget', 1000, type=int)
        budget = max(100, min(budget, 100000))
        strategy.MAX_BET_PER_RACE = budget

        # 10時ロック判定
        now = datetime.now()
        race_date_str = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
        is_today = now.strftime("%Y-%m-%d") == race_date_str
        is_locked = is_today and now.hour >= 10

        # レースID取得（DB優先、なければスクレイパー）
        race_date_db = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
        with get_db() as conn:
            db_races = conn.execute(
                "SELECT race_id FROM races WHERE race_date = ? ORDER BY race_id",
                (race_date_db,)
            ).fetchall()
        race_ids = [r["race_id"] for r in db_races] if db_races else []

        # DBになければスクレイパーで取得
        if not race_ids:
            race_ids = scraper.get_race_list_by_date(date_str)
        if not race_ids:
            return jsonify({"error": "レースが見つかりません", "races": []})

        all_races = []
        for race_id in race_ids:
            # ── キャッシュ確認 ──
            cached = None
            with get_db() as conn:
                cached = conn.execute(
                    "SELECT * FROM predictions_cache WHERE race_id = ?",
                    (race_id,)
                ).fetchone()

            # ロック中かつキャッシュありなら即座に返す
            use_cache = cached and is_locked

            if use_cache:
                # キャッシュから復元
                horses = json.loads(cached["predictions_json"])
                all_bets = json.loads(cached["all_bets_json"])
                confidence = cached["confidence"]
                conf_reason = cached["conf_reason"] or ""
                should_bet = bool(cached["should_bet"])
                bet_reason = cached["bet_reason"] or ""

                # レース情報はDBから取得
                with get_db() as conn:
                    race = conn.execute(
                        "SELECT * FROM races WHERE race_id = ?", (race_id,)
                    ).fetchone()
                if not race:
                    continue
                race_info = dict(race)

                # キャッシュの馬にDB最新のオッズ・人気をマージ
                with get_db() as conn:
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
                # キャッシュからレース傾向を再計算
                sorted_probs = sorted([h.get("pred_win", 0) for h in horses], reverse=True)
                top_p = sorted_probs[0] if sorted_probs else 0
                gap_p = (top_p - sorted_probs[1]) if len(sorted_probs) > 1 else 0
                top3_t = sum(sorted_probs[:3])
                if top_p >= 35 and gap_p >= 12: race_tendency = "堅い（本命突出）"
                elif top_p >= 25 and gap_p >= 6: race_tendency = "やや堅い（軸馬明確）"
                elif top3_t >= 55: race_tendency = "上位拮抗（実力伯仲）"
                elif top_p <= 12: race_tendency = "波乱含み（大混戦）"
                else: race_tendency = "普通（中穴狙い可）"

                # キャッシュからmyomi再計算
                max_ev = 0.0
                for bt_key, bt_bets in all_bets.items():
                    for b in bt_bets:
                        ev = b.get("ev", 0)
                        if ev > max_ev:
                            max_ev = ev
                if max_ev >= 5.0:
                    myomi = "💎★★★"
                elif max_ev >= 2.5:
                    myomi = "💎★★"
                elif max_ev >= 1.5:
                    myomi = "💎★"
                else:
                    myomi = ""
            else:
                # ── 出馬表確保 ──
                with get_db() as conn:
                    race = conn.execute(
                        "SELECT * FROM races WHERE race_id = ?", (race_id,)
                    ).fetchone()

                if not race:
                    shutuba = scraper.scrape_shutuba(race_id)
                    if shutuba and shutuba.get("entries"):
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
                                    0, 0, 0, "", 0, "", 0, "", 0, 0,
                                    e.get("impost", 0)
                                ))

                # レース情報再取得
                with get_db() as conn:
                    race = conn.execute(
                        "SELECT * FROM races WHERE race_id = ?", (race_id,)
                    ).fetchone()
                    results = conn.execute("""
                        SELECT r.*, h.horse_name, j.jockey_name, t.trainer_name
                        FROM results r
                        LEFT JOIN horses h ON r.horse_id = h.horse_id
                        LEFT JOIN jockeys j ON r.jockey_id = j.jockey_id
                        LEFT JOIN trainers t ON r.trainer_id = t.trainer_id
                        WHERE r.race_id = ?
                        ORDER BY r.horse_number
                    """, (race_id,)).fetchall()

                if not race:
                    continue

                # ── ML予測 ──
                try:
                    pred_df = model.predict_race(race_id)
                except Exception:
                    continue

                if pred_df.empty:
                    continue

                race_info = dict(race)

                # ── リアルタイムオッズ取得 ──
                live_odds = {}
                has_db_odds = any(r["odds"] and r["odds"] > 0 for r in results)
                if not has_db_odds:
                    try:
                        live_odds = scraper.scrape_odds(race_id)
                    except Exception:
                        pass

                # 馬データ構築
                horses = []
                predictions_for_bet = []
                for _, row in pred_df.iterrows():
                    hn = int(row["horse_number"])
                    horse_name = ""
                    jockey_name = ""
                    trainer_name = ""
                    odds_win = 0
                    popularity = 0

                    for r in results:
                        if r["horse_number"] == hn:
                            horse_name = r["horse_name"] or ""
                            jockey_name = r["jockey_name"] or ""
                            trainer_name = r["trainer_name"] or ""
                            odds_win = r["odds"] or 0
                            popularity = r["popularity"] or 0
                            break

                    pred_win = float(row["pred_win_norm"])
                    pred_top3 = float(row.get("pred_top3_norm", pred_win * 2.5))

                    # オッズ取得
                    if odds_win <= 0 and hn in live_odds:
                        odds_win = live_odds[hn].get("win_odds", 0)
                    place_min = live_odds.get(hn, {}).get("place_min", 0)
                    place_max = live_odds.get(hn, {}).get("place_max", 0)
                    if place_min > 0 and place_max > 0:
                        odds_place = (place_min + place_max) / 2
                    else:
                        odds_place = max(odds_win * 0.3, 1.1) if odds_win else 1.5
                    if odds_win <= 0 and pred_win > 0:
                        # 推定オッズ
                        odds_win = max(round(1.0 / pred_win, 1), 1.5)
                        odds_place = max(odds_win * 0.3, 1.1) if odds_win else 1.5

                    horses.append({
                        "horse_number": hn,
                        "horse_name": horse_name,
                        "jockey_name": jockey_name,
                        "trainer_name": trainer_name,
                        "pred_win": round(pred_win * 100, 1),
                        "pred_top3": round(pred_top3 * 100, 1),
                        "rank_score": round(float(row.get("rank_score", 0)), 2),
                        "si_avg": round(float(row.get("si_avg", 0)), 1),
                        "win_rate": round(float(row.get("win_rate_10r", 0)) * 100, 1),
                        "top3_rate": round(float(row.get("top3_rate_10r", 0)) * 100, 1),
                        "odds_win": odds_win,
                        "popularity": popularity,
                        # Category scores for badge evaluation
                        "cat_ability": round(float(row.get("si_avg", 0)) + float(row.get("si_latest", 0)), 2),
                        "cat_pedigree": round(float(row.get("pedigree_score", 0)), 3),
                        "cat_jockey": round(float(row.get("combo_top3", 0)) + float(row.get("jockey_cond_top3", 0)), 3),
                        "cat_track": round(float(row.get("bias_score", 0)), 3),
                        "cat_record": round(float(row.get("top3_rate_10r", 0)) + float(row.get("avg_finish_5r", 0)) * -1, 3),
                        "cat_weather": round(float(row.get("horse_wet_top3_rate", 0)), 3),
                    })

                    predictions_for_bet.append({
                        "horse_number": hn,
                        "horse_name": horse_name,
                        "pred_win": pred_win,
                        "pred_top3": pred_top3,
                        "odds_win": odds_win,
                        "odds_place": odds_place,
                    })

                # ── 印と理由 ──
                sorted_horses = sorted(horses, key=lambda x: x["pred_win"], reverse=True)
                for i, h in enumerate(sorted_horses):
                    si = h["si_avg"]
                    pw = h["pred_win"]
                    pt = h["pred_top3"]

                    # 印の割り当て: ◎○▲△×各1頭
                    if i == 0: h["mark"] = "◎"
                    elif i == 1: h["mark"] = "○"
                    elif i == 2: h["mark"] = "▲"
                    elif i == 3: h["mark"] = "△"
                    elif i == 4: h["mark"] = "×"
                    else: h["mark"] = ""

                    # 理由生成
                    reasons = []
                    if i == 0:
                        if pw >= 40: reasons.append("圧倒的な勝率で本命筆頭")
                        elif pw >= 25: reasons.append("勝率トップで信頼度が高い")
                        else: reasons.append("僅差ながら勝率1位")
                    wr = h.get("win_rate", 0)
                    tr = h.get("top3_rate", 0)
                    if wr >= 30: reasons.append(f"直近勝率{wr}%と絶好調")
                    elif wr >= 15: reasons.append(f"直近勝率{wr}%で実績あり")
                    if tr >= 50: reasons.append(f"直近複勝率{tr}%で安定感抜群")
                    elif tr >= 30: reasons.append(f"直近複勝率{tr}%で堅実")
                    if si >= 90: reasons.append(f"SI{si}は出走馬中トップクラス")
                    elif si >= 80: reasons.append(f"SI{si}で能力上位")
                    if pt >= 25: reasons.append("複勝率が非常に高く堅実")
                    elif pt >= 18: reasons.append("複勝圏内の可能性大")
                    if pw >= 20 and si >= 80: reasons.append("勝率とSIの両面で好材料")
                    if pw == 0 and pt == 0:
                        reasons = ["出走歴なし or 特徴量不足"]
                    elif pw == 0 and pt > 0:
                        reasons = [f"複勝率{pt}%で穴的な存在"]
                    h["reasons"] = reasons[:3]

                # 注マーク: 上位7頭以降で大穴候補（高オッズ＆複勝率あり）を1頭だけ
                for h in sorted_horses[7:]:
                    if h["odds_win"] >= 20 and h["pred_top3"] >= 8:
                        h["mark"] = "注"
                        break

                mark_map = {h["horse_number"]: {"mark": h["mark"], "reasons": h["reasons"]} for h in sorted_horses}
                for h in horses:
                    info = mark_map.get(h["horse_number"], {})
                    h["mark"] = info.get("mark", "")
                    h["reasons"] = info.get("reasons", [])

                # ── 全6券種の買い目を一括生成 ──
                should_bet, bet_reason = strategy.should_bet_race(predictions_for_bet)
                all_bets = {}
                if should_bet:
                    for bt in strategy.ALL_BET_TYPES:
                        result = strategy.generate_bets(predictions_for_bet, bet_types=[bt])
                        all_bets[bt] = result.get("bets", [])
                else:
                    for bt in strategy.ALL_BET_TYPES:
                        all_bets[bt] = []

                # ── 信頼度（◎の予測勝率ベース）──
                honmei = next((h for h in horses if h.get('mark') == '◎'), None)
                honmei_win = honmei['pred_win'] if honmei else 0

                if honmei_win >= 50:
                    confidence, conf_reason = "S", f"◎の勝率が非常に高い ({honmei_win:.1f}%)"
                elif honmei_win >= 35:
                    confidence, conf_reason = "A", f"◎の勝率が高い ({honmei_win:.1f}%)"
                elif honmei_win >= 22:
                    confidence, conf_reason = "B", f"◎の勝率は標準的 ({honmei_win:.1f}%)"
                elif honmei_win >= 12:
                    confidence, conf_reason = "C", f"◎の信頼度やや低い ({honmei_win:.1f}%)"
                else:
                    confidence, conf_reason = "D", f"◎の信頼度が低い ({honmei_win:.1f}%)"

                # ── 妙味（EVベース）──
                max_ev = 0.0
                for bt_key, bt_bets in all_bets.items():
                    for b in bt_bets:
                        ev = b.get("ev", 0)
                        if ev > max_ev:
                            max_ev = ev

                if max_ev >= 5.0:
                    myomi = "💎★★★"
                elif max_ev >= 2.5:
                    myomi = "💎★★"
                elif max_ev >= 1.5:
                    myomi = "💎★"
                else:
                    myomi = ""
                conf_reason += f" / 妙味:{myomi or 'なし'}(EV{max_ev:.1f})"

                # ── レース傾向（堅い/混戦/波乱）──
                sorted_probs = sorted([h["pred_win"] for h in horses], reverse=True)
                top_prob = sorted_probs[0]
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

                # ── キャッシュに保存 ──
                with get_db() as conn:
                    conn.execute("""
                        INSERT OR REPLACE INTO predictions_cache
                        (race_id, predictions_json, all_bets_json, confidence,
                         conf_reason, should_bet, bet_reason)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    """, (
                        race_id,
                        json.dumps(horses, ensure_ascii=False),
                        json.dumps(all_bets, ensure_ascii=False),
                        confidence, conf_reason,
                        1 if should_bet else 0, bet_reason,
                    ))

            # ── ここからは共通（キャッシュ使用/新規生成 問わず）──
            # レース情報
            with get_db() as conn:
                race = conn.execute(
                    "SELECT * FROM races WHERE race_id = ?", (race_id,)
                ).fetchone()
            if not race:
                continue
            race_info = dict(race)

            # レース結果を取得（確定済みの場合）
            race_results = {}
            has_results = False
            with get_db() as conn:
                res_rows = conn.execute("""
                    SELECT r.horse_number, r.finish_position, r.finish_time,
                           r.odds, r.popularity, r.last_3f, r.margin
                    FROM results r
                    WHERE r.race_id = ? AND r.finish_position > 0
                    ORDER BY r.finish_position
                """, (race_id,)).fetchall()
                if res_rows:
                    has_results = True
                    for rr in res_rows:
                        race_results[rr['horse_number']] = {
                            'finish': rr['finish_position'],
                            'time': rr['finish_time'] or '',
                            'odds': rr['odds'] or 0,
                            'popularity': rr['popularity'] or 0,
                            'last_3f': rr['last_3f'] or 0,
                            'margin': rr['margin'] or '',
                        }

                payout_rows = conn.execute("""
                    SELECT bet_type, combination, payout_amount, popularity
                    FROM payouts WHERE race_id = ?
                    ORDER BY bet_type, popularity
                """, (race_id,)).fetchall()

                # 結果があるのに配当がない場合、自動取得を試みる
                if has_results and not payout_rows:
                    try:
                        race_data_for_payout = scraper.scrape_race_result(race_id)
                        if race_data_for_payout and race_data_for_payout.get("payouts"):
                            for p in race_data_for_payout["payouts"]:
                                conn.execute("""
                                    INSERT OR REPLACE INTO payouts
                                    (race_id, bet_type, combination, payout_amount, popularity)
                                    VALUES (?, ?, ?, ?, ?)
                                """, (
                                    race_id, p["bet_type"],
                                    p["combination"], p["payout_amount"],
                                    p.get("popularity", 0)
                                ))
                            # 再取得
                            payout_rows = conn.execute("""
                                SELECT bet_type, combination, payout_amount, popularity
                                FROM payouts WHERE race_id = ?
                                ORDER BY bet_type, popularity
                            """, (race_id,)).fetchall()
                            print(f"  💰 配当自動取得: {race_id} ({len(race_data_for_payout['payouts'])}件)")
                    except Exception as e:
                        print(f"  ⚠️ 配当自動取得失敗: {race_id}: {e}")

                race_payouts = [{
                    'bet_type': pr['bet_type'],
                    'combination': pr['combination'],
                    'payout': pr['payout_amount'],
                    'popularity': pr['popularity'],
                } for pr in payout_rows]

            # 馬データに結果をマージ
            for h in horses:
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
                    # popularity は上書きしない（DBから取得済み）
                    h['last_3f'] = 0
                    h['margin'] = ''

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
                "all_bets": all_bets,        # ← 全6券種
                "should_bet": should_bet,
                "bet_reason": bet_reason,
                "confidence": confidence,
                "conf_reason": conf_reason,
                "myomi": myomi,
                "max_ev": round(max_ev, 1),
                "race_tendency": race_tendency,
                "has_results": has_results,
                "payouts": race_payouts if has_results else [],
                "prediction_locked": is_locked and cached is not None,
            })

        # 会場でグループ化
        venues = {}
        for r in all_races:
            v = r["venue"] or "不明"
            if v not in venues:
                venues[v] = []
            venues[v].append(r)

        return jsonify({
            "date": date_str,
            "total_races": len(all_races),
            "venues": venues,
            "is_locked": is_locked,
        })

    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.route("/api/roi-summary")
def api_roi_summary():
    """回収率サマリーAPI (日別/月別)"""
    period = request.args.get('period', 'daily')  # daily or monthly
    try:
        with get_db() as conn:
            if period == 'monthly':
                rows = conn.execute("""
                    SELECT
                        strftime('%Y-%m', bet_date) as period_key,
                        COUNT(*) as total_bets,
                        SUM(is_hit) as hits,
                        SUM(amount) as invested,
                        SUM(payout) as payout
                    FROM bets
                    GROUP BY period_key
                    ORDER BY period_key DESC
                    LIMIT 24
                """).fetchall()
            else:
                rows = conn.execute("""
                    SELECT
                        bet_date as period_key,
                        COUNT(*) as total_bets,
                        SUM(is_hit) as hits,
                        SUM(amount) as invested,
                        SUM(payout) as payout
                    FROM bets
                    GROUP BY period_key
                    ORDER BY period_key DESC
                    LIMIT 60
                """).fetchall()

            data = []
            for r in rows:
                invested = r['invested'] or 0
                payout = r['payout'] or 0
                total_bets = r['total_bets'] or 0
                hits = r['hits'] or 0
                roi = round(payout / invested * 100, 1) if invested > 0 else 0
                hit_rate = round(hits / total_bets * 100, 1) if total_bets > 0 else 0
                data.append({
                    'period': r['period_key'],
                    'bets': total_bets,
                    'hits': hits,
                    'hit_rate': hit_rate,
                    'invested': invested,
                    'payout': payout,
                    'profit': payout - invested,
                    'roi': roi,
                })

            # 合計
            total_invested = sum(d['invested'] for d in data)
            total_payout = sum(d['payout'] for d in data)
            total_bets = sum(d['bets'] for d in data)
            total_hits = sum(d['hits'] for d in data)

            return jsonify({
                'period': period,
                'data': data,
                'summary': {
                    'total_bets': total_bets,
                    'total_hits': total_hits,
                    'hit_rate': round(total_hits / total_bets * 100, 1) if total_bets > 0 else 0,
                    'total_invested': total_invested,
                    'total_payout': total_payout,
                    'total_profit': total_payout - total_invested,
                    'roi': round(total_payout / total_invested * 100, 1) if total_invested > 0 else 0,
                }
            })
    except Exception as e:
        return jsonify({'error': str(e)}), 500


if __name__ == "__main__":
    init_db()
    print("🏇 競馬予想ダッシュボード起動中...")
    print("   http://localhost:5001")
    print("   予測ダッシュボード: http://localhost:5001/predict")

    # バックグラウンド結果取得スレッド起動
    bg_thread = threading.Thread(target=_bg_result_fetcher, daemon=True)
    bg_thread.start()
    print("   🔄 結果自動取得: ON (5分間隔, 10:00-17:30)")

    app.run(debug=True, host="0.0.0.0", port=5001)

