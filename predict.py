"""
競馬予測メインスクリプト
データ収集 → 分析 → 予測 → 推奨馬券出力 の全フローを実行
"""

import argparse
import json
import sys
import os
from datetime import datetime

# プロジェクトルートをパスに追加
sys.path.insert(0, os.path.dirname(__file__))

from database import init_db, get_db
from scraper import NetkeibaScraper
from ml.model import KeibaModel
from ml.features import FeatureBuilder
from strategy.betting import BettingStrategy
from analyzers.speed_index import SpeedIndexCalculator
from analyzers.odds_value import OddsValueAnalyzer


def cmd_collect(args):
    """データ収集コマンド"""
    scraper = NetkeibaScraper()

    if args.date:
        # 特定日のレースを収集
        print(f"🏇 {args.date} のレースデータを収集...")
        race_ids = scraper.get_race_list_by_date(args.date)
        for i, rid in enumerate(race_ids):
            print(f"  [{i+1}/{len(race_ids)}] {rid}")
            data = scraper.scrape_race_result(rid)
            if data and data.get("results"):
                scraper.save_race_to_db(data)
            else:
                # 未来のレース: 出馬表から取得
                shutuba = scraper.scrape_shutuba(rid)
                if shutuba and shutuba.get("entries"):
                    with get_db() as conn:
                        conn.execute("""
                            INSERT OR REPLACE INTO races
                            (race_id, race_date, venue, race_number, race_name, grade,
                             distance, surface, direction, weather, track_condition, horse_count, start_time)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (
                            rid, shutuba.get("race_date", ""),
                            shutuba.get("venue", ""), shutuba.get("race_number", 0),
                            shutuba.get("race_name", ""), shutuba.get("grade", ""),
                            shutuba.get("distance", 0), shutuba.get("surface", ""),
                            shutuba.get("direction", ""), shutuba.get("weather", ""),
                            shutuba.get("track_condition", ""),
                            len(shutuba.get("entries", [])),
                            shutuba.get("start_time", "")
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
                                INSERT OR IGNORE INTO results
                                (race_id, horse_id, jockey_id, trainer_id,
                                 post_position, horse_number, odds, popularity,
                                 finish_position, finish_time, finish_time_seconds,
                                 margin, last_3f, passing_order, weight, weight_change, impost)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """, (
                                rid, e.get("horse_id", ""),
                                e.get("jockey_id", ""), e.get("trainer_id", ""),
                                e.get("post_position", 0), e.get("horse_number", 0),
                                0, 0, 0, "", 0, "", 0, "", 0, 0, e.get("impost", 0)
                            ))
                    print(f"  📋 出馬表保存: {len(shutuba['entries'])}頭")
                else:
                    print(f"  ⚠️ データ取得失敗: {rid}")
    else:
        # 期間指定で収集
        start_y = args.start_year or 2024
        start_m = args.start_month or 1
        end_y = args.end_year or start_y
        end_m = args.end_month or 12
        scraper.collect_range(start_y, start_m, end_y, end_m)

    # 収集結果を表示
    with get_db() as conn:
        race_count = conn.execute("SELECT COUNT(*) as c FROM races").fetchone()["c"]
        result_count = conn.execute("SELECT COUNT(*) as c FROM results").fetchone()["c"]
        print(f"\n📊 DB状況: {race_count}レース / {result_count}出走データ")


def cmd_train(args):
    """モデル学習コマンド（fast_train.pyに委譲）"""
    import subprocess
    script = os.path.join(os.path.dirname(__file__), "fast_train.py")
    print("🧠 高速学習パイプラインを起動...")
    print("   (fast_train.py に委譲)\n")
    result = subprocess.run(
        [sys.executable, script],
        cwd=os.path.dirname(__file__)
    )
    sys.exit(result.returncode)


def cmd_predict(args):
    """レース予測コマンド"""
    model = KeibaModel()
    strategy = BettingStrategy()
    speed_calc = SpeedIndexCalculator()

    if args.race_id:
        race_ids = [args.race_id]
    elif args.date:
        scraper = NetkeibaScraper()
        race_ids = scraper.get_race_list_by_date(args.date)
    else:
        print("❌ --race-id か --date を指定してください")
        return

    for race_id in race_ids:
        print(f"\n{'='*60}")
        print(f"🏇 レース予測: {race_id}")
        print(f"{'='*60}")

        # レース情報取得
        with get_db() as conn:
            race = conn.execute(
                "SELECT * FROM races WHERE race_id = ?", (race_id,)
            ).fetchone()
            results = conn.execute("""
                SELECT r.*, h.horse_name, j.jockey_name
                FROM results r
                LEFT JOIN horses h ON r.horse_id = h.horse_id
                LEFT JOIN jockeys j ON r.jockey_id = j.jockey_id
                WHERE r.race_id = ?
                ORDER BY r.horse_number
            """, (race_id,)).fetchall()

        if not race:
            # 未来レース: 出馬表をスクレイピングして保存
            print(f"  📡 出馬表を取得中...")
            scraper = NetkeibaScraper()
            shutuba = scraper.scrape_shutuba(race_id)
            if shutuba and shutuba.get("entries"):
                # レース情報をDBに保存
                with get_db() as conn:
                    conn.execute("""
                        INSERT OR REPLACE INTO races
                        (race_id, race_date, venue, race_number, race_name, grade,
                         distance, surface, direction, weather, track_condition, horse_count, start_time)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        race_id, shutuba.get("race_date", ""),
                        shutuba.get("venue", ""), shutuba.get("race_number", 0),
                        shutuba.get("race_name", ""), shutuba.get("grade", ""),
                        shutuba.get("distance", 0), shutuba.get("surface", ""),
                        shutuba.get("direction", ""), shutuba.get("weather", ""),
                        shutuba.get("track_condition", ""),
                        len(shutuba.get("entries", [])),
                        shutuba.get("start_time", "")
                    ))

                    # 各馬のエントリーをresultsテーブルに保存（finish_position=0で未確定）
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

                # 保存後に再取得
                with get_db() as conn:
                    race = conn.execute(
                        "SELECT * FROM races WHERE race_id = ?", (race_id,)
                    ).fetchone()
                    results = conn.execute("""
                        SELECT r.*, h.horse_name, j.jockey_name
                        FROM results r
                        LEFT JOIN horses h ON r.horse_id = h.horse_id
                        LEFT JOIN jockeys j ON r.jockey_id = j.jockey_id
                        WHERE r.race_id = ?
                        ORDER BY r.horse_number
                    """, (race_id,)).fetchall()
                print(f"  ✅ {len(results)}頭の出馬表を取得")
            else:
                print(f"  ⚠️ 出馬表を取得できませんでした")
                continue

        if not race:
            print(f"  ⚠️ レースデータが見つかりません")
            continue

        race_info = dict(race)

        # 予測
        try:
            pred_df = model.predict_race(race_id)
        except ValueError as e:
            print(f"  ⚠️ {e}")
            continue

        if pred_df.empty:
            print(f"  ⚠️ 予測データを構築できません")
            continue

        # 予測結果表示
        print(f"\n📊 予測結果: {race_info.get('race_name', '')} "
              f"({race_info['venue']} {race_info['race_number']}R "
              f"{race_info['surface']}{race_info['distance']}m)")
        print(f"{'─'*60}")
        print(f"{'馬番':>4} {'馬名':<12} {'勝率':>7} {'複勝率':>7} {'SI':>6}")
        print(f"{'─'*60}")

        predictions = []
        for _, row in pred_df.iterrows():
            # 馬名取得
            horse_name = ""
            for r in results:
                if r["horse_number"] == row["horse_number"]:
                    horse_name = r["horse_name"] or ""
                    break

            print(f"{int(row['horse_number']):>4} {horse_name:<12} "
                  f"{row['pred_win_norm']:>6.1%} {row['pred_top3_norm']/3:>6.1%} "
                  f"{row.get('si_avg', 0):>6.1f}")

            # 推奨馬券生成用データ
            odds_win = 0
            odds_place = 0
            for r in results:
                if r["horse_number"] == row["horse_number"]:
                    odds_win = r["odds"] or 0
                    odds_place = max(odds_win * 0.3, 1.1) if odds_win else 1.5
                    break

            # オッズがない場合（未来レース）→ 予測確率から推定
            has_real_odds = odds_win > 0
            if odds_win <= 0 and row["pred_win_norm"] > 0:
                # 推定オッズ = 0.8 / 予測勝率（JRAの控除率20%を考慮）
                odds_win = max(round(0.8 / row["pred_win_norm"], 1), 1.2)
                odds_place = max(round(0.8 / (row["pred_top3_norm"] / 3), 1), 1.1)

            predictions.append({
                "horse_number": int(row["horse_number"]),
                "horse_name": horse_name,
                "pred_win": row["pred_win_norm"],
                "pred_top3": row["pred_top3_norm"] / 3,
                "odds_win": odds_win,
                "odds_place": odds_place,
                "_has_real_odds": has_real_odds,
                "si_avg": round(row.get("si_avg", 0), 1),
                "jockey_name": next((r["jockey_name"] for r in results if r["horse_number"] == int(row["horse_number"]) and "jockey_name" in r.keys()), ""),
                # カテゴリスコア
                "cat_ability": round(row.get("si_avg", 0), 1),
                "cat_pedigree": round(row.get("pedigree_score", 0), 2),
                "cat_jockey": round(row.get("jt_score", 0), 2),
                "cat_track": round(row.get("bias_score", 0), 2),
                "cat_record": round(row.get("top3_rate_10r", 0) * 100, 1),
                "cat_weather": round(row.get("horse_wet_top3_rate", 0) * 100, 1),
                "win_rate": round(row.get("win_rate_10r", 0) * 100, 1),
                "top3_rate": round(row.get("top3_rate_10r", 0) * 100, 1),
            })

        # predictions_cache に保存
        sorted_preds = sorted(predictions, key=lambda x: x["pred_win"], reverse=True)
        # 人気順: 実オッズがある場合のみ使用（推定オッズは循環参照になるため除外）
        has_real_odds = any(p.get("_has_real_odds") for p in sorted_preds)
        if has_real_odds:
            by_odds = sorted(sorted_preds, key=lambda x: x.get("odds_win", 999))
            popularity_map = {p["horse_number"]: i+1 for i, p in enumerate(by_odds)}
        else:
            popularity_map = {p["horse_number"]: 0 for p in sorted_preds}

        # 印を付与（AI勝率順）
        mark_labels = ['◎', '○', '▲', '△', '×']
        for i, p in enumerate(sorted_preds):
            if i < 5:
                p["mark"] = mark_labels[i]
            else:
                p["mark"] = ""
        # 6番目以降で妙味のある馬に「注」
        for p in sorted_preds[5:]:
            pop = popularity_map.get(p["horse_number"], 99)
            if pop >= 6 and p["pred_win"] * 100 >= 5.0:
                p["mark"] = "注"
                break

        cache_json = json.dumps([{
            "horse_number": p["horse_number"],
            "horse_name": p["horse_name"],
            "mark": p.get("mark", ""),
            "pred_win_pct": round(p["pred_win"] * 100, 1),
            "pred_top3_pct": round(p["pred_top3"] * 100, 1),
            "odds_win": round(p.get("odds_win", 0), 1),
            "popularity": popularity_map.get(p["horse_number"], 0),
            "si_avg": p.get("si_avg", 0),
            "jockey_name": p.get("jockey_name", ""),
            "cat_ability": p.get("cat_ability", 0),
            "cat_pedigree": p.get("cat_pedigree", 0),
            "cat_jockey": p.get("cat_jockey", 0),
            "cat_track": p.get("cat_track", 0),
            "cat_record": p.get("cat_record", 0),
            "cat_weather": p.get("cat_weather", 0),
            "win_rate": p.get("win_rate", 0),
            "top3_rate": p.get("top3_rate", 0),
        } for p in sorted_preds], ensure_ascii=False)

        with get_db() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO predictions_cache
                (race_id, predictions_json, all_bets_json, confidence, should_bet, created_at)
                VALUES (?, ?, '{}', 'C', 0, datetime('now'))
            """, (race_id, cache_json))

        # 馬券推奨
        should_bet, reason = strategy.should_bet_race(predictions)
        confidence = 'C'

        # 常にgenerate_betsを実行（EV・妙味計算のため）
        bets_result = strategy.generate_bets(predictions)
        # 券種別にグループ化して保存（generate_noteが期待する形式）
        bets_by_type = {}
        for b in bets_result.get('bets', []):
            bt = b.get('type', '単勝')
            if bt not in bets_by_type:
                bets_by_type[bt] = []
            bets_by_type[bt].append(b)
        all_bets_json = json.dumps(bets_by_type, ensure_ascii=False)

        if should_bet:
            print(strategy.format_recommendation(bets_result, race_info))
        else:
            print(f"\n❌ このレースは見送り推奨: {reason}")

        # confidence計算（◎の勝率ベース — 850Rバックテスト検証済み閾値）
        # S: 30%↑ → 実勝率64% / 複勝率100%
        # A: 20%↑ → 実勝率39% / 複勝率83%
        # B: 15%↑ → 実勝率28% / 複勝率71%
        # C: 10%↑ → 実勝率22% / 複勝率56%
        # D: 10%↓ → 実勝率25% / 複勝率68%（混戦で読みにくい）
        top_win = sorted_preds[0]["pred_win"] * 100 if sorted_preds else 0
        if top_win >= 30:
            confidence = "S"
        elif top_win >= 20:
            confidence = "A"
        elif top_win >= 15:
            confidence = "B"
        elif top_win >= 10:
            confidence = "C"
        else:
            confidence = "D"

        # キャッシュ更新（買い目・confidence・should_bet）
        with get_db() as conn:
            conn.execute("""
                UPDATE predictions_cache
                SET all_bets_json = ?, confidence = ?, should_bet = ?
                WHERE race_id = ?
            """, (all_bets_json, confidence, 1 if should_bet else 0, race_id))
        print(f"  💾 予測キャッシュを保存")


def cmd_backtest(args):
    """バックテストコマンド（fast_train.pyに委譲）"""
    import subprocess
    script = os.path.join(os.path.dirname(__file__), "fast_train.py")
    print("📊 高速バックテストを起動...")
    print("   (fast_train.py に委譲)\n")
    result = subprocess.run(
        [sys.executable, script],
        cwd=os.path.dirname(__file__)
    )
    sys.exit(result.returncode)


def cmd_status(args):
    """DB状況確認コマンド"""
    with get_db() as conn:
        races = conn.execute("SELECT COUNT(*) as c FROM races").fetchone()["c"]
        results = conn.execute("SELECT COUNT(*) as c FROM results").fetchone()["c"]
        horses = conn.execute("SELECT COUNT(*) as c FROM horses").fetchone()["c"]
        jockeys = conn.execute("SELECT COUNT(*) as c FROM jockeys").fetchone()["c"]

        if races > 0:
            date_range = conn.execute(
                "SELECT MIN(race_date) as min_d, MAX(race_date) as max_d FROM races"
            ).fetchone()
            print(f"📅 期間: {date_range['min_d']} 〜 {date_range['max_d']}")

        bets = conn.execute("SELECT COUNT(*) as c FROM bets").fetchone()["c"]
        if bets > 0:
            bet_stats = conn.execute("""
                SELECT SUM(amount) as total_bet,
                       SUM(payout) as total_payout,
                       SUM(is_hit) as hits
                FROM bets
            """).fetchone()
            print(f"\n💰 馬券実績:")
            print(f"  購入数: {bets}件")
            print(f"  的中数: {bet_stats['hits']}件")
            print(f"  投資額: ¥{bet_stats['total_bet']:,}")
            print(f"  回収額: ¥{bet_stats['total_payout']:,}")

    print(f"\n📊 データベース状況:")
    print(f"  レース数:   {races:,}")
    print(f"  出走データ: {results:,}")
    print(f"  馬:         {horses:,}")
    print(f"  騎手:       {jockeys:,}")

    # モデル状態
    model_path = os.path.join(os.path.dirname(__file__), "models", "model_top3.pkl")
    rank_path = os.path.join(os.path.dirname(__file__), "models", "model_rank.pkl")
    if os.path.exists(model_path):
        has_rank = "✅" if os.path.exists(rank_path) else "⚠️ なし"
        print(f"\n🧠 モデル: ✅ 学習済み (LambdaRank: {has_rank})")
    else:
        print(f"\n🧠 モデル: ⚠️ 未学習 (python predict.py train で学習してください)")


def main():
    parser = argparse.ArgumentParser(
        description="🏇 競馬予想AI — プラス収支を目指す統合予測システム",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用例:
  # ステップ1: データ収集 (2024年のデータ)
  python predict.py collect --start-year 2024 --end-month 12

  # ステップ2: モデルを学習
  python predict.py train

  # ステップ3: 予測
  python predict.py predict --date 20250315

  # バックテスト
  python predict.py backtest --year 2024

  # DB状況確認
  python predict.py status
        """
    )

    subparsers = parser.add_subparsers(dest="command")

    # collect
    p_collect = subparsers.add_parser("collect", help="レースデータを収集")
    p_collect.add_argument("--date", help="日付 (YYYYMMDD)")
    p_collect.add_argument("--start-year", type=int)
    p_collect.add_argument("--start-month", type=int, default=1)
    p_collect.add_argument("--end-year", type=int)
    p_collect.add_argument("--end-month", type=int, default=12)

    # train
    p_train = subparsers.add_parser("train", help="予測モデルを学習")

    # predict
    p_predict = subparsers.add_parser("predict", help="レースを予測")
    p_predict.add_argument("--race-id", help="レースID")
    p_predict.add_argument("--date", help="日付 (YYYYMMDD)")

    # backtest
    p_backtest = subparsers.add_parser("backtest", help="バックテスト実行")
    p_backtest.add_argument("--year", type=int)
    p_backtest.add_argument("--month", type=int)

    # status
    p_status = subparsers.add_parser("status", help="DB状況確認")

    args = parser.parse_args()

    init_db()

    if args.command == "collect":
        cmd_collect(args)
    elif args.command == "train":
        cmd_train(args)
    elif args.command == "predict":
        cmd_predict(args)
    elif args.command == "backtest":
        cmd_backtest(args)
    elif args.command == "status":
        cmd_status(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
