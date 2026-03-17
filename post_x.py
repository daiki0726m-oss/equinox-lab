"""
🐦 X (Twitter) 自動投稿スクリプト
EQUINOX Lab — AI競馬予測の自動配信

使い方:
  # レース当日の予想投稿（メインレース）
  python post_x.py predict --date 20260322

  # 的中結果報告
  python post_x.py results --date 20260322

  # 平日コンテンツ（自動選択）
  python post_x.py weekday

  # テスト（投稿せずにプレビュー）
  python post_x.py predict --date 20260322 --dry-run
"""

import argparse
import sys
import os
import json
import random
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(__file__))

from database import init_db, get_db

# X API (tweepy)
try:
    import tweepy
    HAS_TWEEPY = True
except ImportError:
    HAS_TWEEPY = False


def load_x_client():
    """X APIクライアントを初期化"""
    env_path = os.path.join(os.path.dirname(__file__), ".env")
    env_vars = {}
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    k, v = line.split("=", 1)
                    env_vars[k.strip()] = v.strip().strip('"').strip("'")

    api_key = env_vars.get("X_API_KEY") or os.environ.get("X_API_KEY")
    api_secret = env_vars.get("X_API_SECRET") or os.environ.get("X_API_SECRET")
    access_token = env_vars.get("X_ACCESS_TOKEN") or os.environ.get("X_ACCESS_TOKEN")
    access_secret = env_vars.get("X_ACCESS_SECRET") or os.environ.get("X_ACCESS_SECRET")

    if not all([api_key, api_secret, access_token, access_secret]):
        print("❌ X APIキーが設定されていません")
        print("   .env ファイルに以下を設定してください:")
        print("   X_API_KEY=xxx")
        print("   X_API_SECRET=xxx")
        print("   X_ACCESS_TOKEN=xxx")
        print("   X_ACCESS_SECRET=xxx")
        return None

    client = tweepy.Client(
        consumer_key=api_key,
        consumer_secret=api_secret,
        access_token=access_token,
        access_token_secret=access_secret
    )
    return client


def post_tweet(client, text, reply_to=None, dry_run=False):
    """ツイートを投稿（280文字超えの場合はスレッド化）"""
    MAX_LEN = 270  # 余裕を持たせる

    # テキストをチャンクに分割
    if len(text) <= MAX_LEN:
        chunks = [text]
    else:
        chunks = split_text(text, MAX_LEN)

    tweet_ids = []
    parent_id = reply_to

    for i, chunk in enumerate(chunks):
        if dry_run:
            label = "ツイート" if i == 0 else f"└ リプライ{i}"
            print(f"\n📝 {label} ({len(chunk)}文字):")
            print(f"{'─'*40}")
            print(chunk)
            print(f"{'─'*40}")
            tweet_ids.append(f"dry-run-{i}")
        else:
            try:
                kwargs = {"text": chunk}
                if parent_id:
                    kwargs["in_reply_to_tweet_id"] = parent_id
                result = client.create_tweet(**kwargs)
                tid = result.data["id"]
                tweet_ids.append(tid)
                parent_id = tid
                print(f"  ✅ 投稿完了 (ID: {tid}, {len(chunk)}文字)")
            except Exception as e:
                print(f"  ❌ 投稿失敗: {e}")
                break

    return tweet_ids


def split_text(text, max_len):
    """テキストを行単位で分割"""
    lines = text.split("\n")
    chunks = []
    current = ""

    for line in lines:
        test = current + line + "\n" if current else line + "\n"
        if len(test) > max_len and current:
            chunks.append(current.strip())
            current = line + "\n"
        else:
            current = test

    if current.strip():
        chunks.append(current.strip())

    return chunks if chunks else [text[:max_len]]


# ─── レース当日: メインレース予想 ───
def cmd_predict(args):
    """メインレース(11R)の予想ツイートを生成・投稿"""
    from ml.model import KeibaModel
    from strategy.betting import BettingStrategy
    from scraper import NetkeibaScraper

    date_str = args.date
    dt = datetime.strptime(date_str, "%Y%m%d")
    weekday = ["月", "火", "水", "木", "金", "土", "日"][dt.weekday()]
    date_label = f"{dt.month}/{dt.day}({weekday})"

    model = KeibaModel()
    strategy = BettingStrategy()
    scraper = NetkeibaScraper()

    # レース取得
    race_ids = scraper.get_race_list_by_date(date_str)
    if not race_ids:
        date_hyphen = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
        with get_db() as conn:
            rows = conn.execute(
                "SELECT race_id FROM races WHERE race_date = ? OR race_date = ? ORDER BY race_id",
                (date_str, date_hyphen)
            ).fetchall()
            race_ids = [r["race_id"] for r in rows]

    if not race_ids:
        print(f"❌ {date_str} のレースが見つかりません")
        return

    # メインレース(11R)のみ抽出
    main_ids = [r for r in race_ids if r.endswith("11")]
    if not main_ids:
        print("❌ メインレース(11R)が見つかりません")
        return

    print(f"🏇 {date_label} メインレース {len(main_ids)}件の予想を生成...\n")

    client = None
    if not args.dry_run:
        client = load_x_client()
        if not client:
            return

    for race_id in main_ids:
        try:
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

            race_info = dict(race)
            pred_df = model.predict_race(race_id)
            if pred_df.empty:
                continue

            # 馬データ構築
            horses = []
            for _, row in pred_df.iterrows():
                hn = int(row["horse_number"])
                hname = ""
                for r in results:
                    if r["horse_number"] == hn:
                        hname = r.get("horse_name", "") or ""
                        break
                horses.append({
                    "horse_number": hn,
                    "horse_name": hname,
                    "pred_win": round(float(row["pred_win_norm"]) * 100, 1),
                })

            sorted_h = sorted(horses, key=lambda x: x["pred_win"], reverse=True)
            marks = ["◎", "○", "▲", "△", "×"]
            for i, h in enumerate(sorted_h):
                h["mark"] = marks[i] if i < 5 else ""

            # 買い目生成
            predictions_for_bet = []
            for _, row in pred_df.iterrows():
                hn = int(row["horse_number"])
                odds_win = 0
                for r in results:
                    if r["horse_number"] == hn:
                        odds_win = r.get("odds", 0) or 0
                        break
                pw = float(row["pred_win_norm"])
                pt = float(row["pred_top3_norm"] / 3)
                if odds_win <= 0 and pw > 0:
                    odds_win = max(round(1.0 / pw, 1), 1.5)
                predictions_for_bet.append({
                    "horse_number": hn,
                    "pred_win": pw, "pred_top3": pt,
                    "odds_win": odds_win,
                    "odds_place": max(odds_win * 0.3, 1.1) if odds_win else 1.5,
                })

            should_bet, _ = strategy.should_bet_race(predictions_for_bet)
            bet_lines = []
            if should_bet:
                for bt in ["単勝", "複勝", "馬連", "三連複"]:
                    res = strategy.generate_bets(predictions_for_bet, bet_types=[bt])
                    for b in res.get("bets", [])[:2]:
                        detail = b.get("detail", "")
                        bet_lines.append(f"{bt} {detail}")

            # ツイート構成
            venue = race_info.get("venue", "")
            rname = race_info.get("race_name", "")
            grade = race_info.get("grade", "")
            surface = race_info.get("surface", "")
            distance = race_info.get("distance", 0)
            condition = race_info.get("track_condition", "良")

            tweet = f"🏇 {date_label} AI予想\n"
            tweet += f"━━━━━━━━━━━━\n"
            tweet += f"📍 {venue}11R {rname}"
            if grade:
                tweet += f" [{grade}]"
            tweet += f"\n{surface}{distance}m / {condition}\n\n"

            for h in sorted_h[:5]:
                if h["mark"]:
                    tweet += f"{h['mark']} {h['horse_number']:>2} {h['horse_name']}\n"

            if bet_lines:
                tweet += f"\n💰 推奨\n"
                for bl in bet_lines[:4]:
                    tweet += f"・{bl}\n"

            # ハッシュタグ
            race_tag = rname.replace(" ", "").replace("　", "")
            tweet += f"\n#競馬予想 #AI予想 #{race_tag}"

            # noteリンク（あれば）
            tweet += f"\n\n📊 全レース詳細はプロフリンクから"

            post_tweet(client, tweet, dry_run=args.dry_run)

        except Exception as e:
            print(f"  ⚠️ {race_id}: {e}")
            continue


# ─── レース当日: 的中結果報告 ───
def cmd_results(args):
    """的中結果を報告するツイートを生成・投稿"""
    date_str = args.date
    dt = datetime.strptime(date_str, "%Y%m%d")
    weekday = ["月", "火", "水", "木", "金", "土", "日"][dt.weekday()]
    date_label = f"{dt.month}/{dt.day}({weekday})"

    date_hyphen = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"

    with get_db() as conn:
        # 当日のキャッシュから予測を取得
        races = conn.execute("""
            SELECT r.race_id, r.venue, r.race_number, r.race_name,
                   pc.predictions_json, pc.all_bets_json
            FROM races r
            JOIN predictions_cache pc ON r.race_id = pc.race_id
            WHERE (r.race_date = ? OR r.race_date = ?)
            AND r.race_number = 11
            ORDER BY r.race_id
        """, (date_str, date_hyphen)).fetchall()

        if not races:
            print(f"❌ {date_str} の結果データがありません")
            return

    total_invest = 0
    total_return = 0
    hit_lines = []
    miss_lines = []

    for race in races:
        venue = race["venue"]
        rname = race["race_name"]
        # 払戻データ確認
        with get_db() as conn:
            payouts = conn.execute(
                "SELECT * FROM payouts WHERE race_id = ?",
                (race["race_id"],)
            ).fetchall()

        if not payouts:
            miss_lines.append(f"❌ {venue}11R {rname} — 結果未取得")
            continue

        bets = json.loads(race["all_bets_json"] or "{}")
        for bt, bt_bets in bets.items():
            for b in bt_bets:
                amount = b.get("amount", 100)
                total_invest += amount
                # 的中判定は簡易的に
                detail = b.get("detail", "")
                hit = False
                for p in payouts:
                    if p["bet_type"] == bt and detail in (p.get("combination", "") or ""):
                        payout = p["payout"] * (amount / 100)
                        total_return += payout
                        hit_lines.append(
                            f"✅ {venue}11R {bt} {detail} 的中！({p['payout']:,}円)")
                        hit = True
                        break
                if not hit:
                    pass  # 不的中は個別表示しない

    roi = round(total_return / total_invest * 100) if total_invest > 0 else 0

    tweet = f"📊 {date_label} 本日の結果\n"
    tweet += f"━━━━━━━━━━━━\n\n"

    if hit_lines:
        for line in hit_lines[:5]:
            tweet += f"{line}\n"
    else:
        tweet += "本日は的中なし 😔\n"

    if miss_lines:
        for line in miss_lines[:3]:
            tweet += f"{line}\n"

    tweet += f"\n💰 投資 ¥{total_invest:,} → 回収 ¥{total_return:,.0f}\n"
    tweet += f"📈 本日ROI: {roi}%"
    if roi >= 100:
        tweet += " 🔥"
    tweet += f"\n\n#競馬予想 #AI予想 #競馬結果"

    client = None
    if not args.dry_run:
        client = load_x_client()
        if not client:
            return

    post_tweet(client, tweet, dry_run=args.dry_run)


# ─── 平日コンテンツ ───
def cmd_weekday(args):
    """平日用の自動コンテンツを生成・投稿"""
    today = datetime.now()
    dow = today.weekday()  # 0=月, 4=金

    if dow == 0:
        tweet = generate_weekly_summary()
    elif dow == 1:
        tweet = generate_jockey_ranking()
    elif dow == 2:
        tweet = generate_analysis_column()
    elif dow == 3:
        tweet = generate_pickup_horse()
    elif dow == 4:
        tweet = generate_weekend_preview()
    else:
        tweet = generate_analysis_column()

    if not tweet:
        print("❌ コンテンツ生成に失敗")
        return

    client = None
    if not args.dry_run:
        client = load_x_client()
        if not client:
            return

    post_tweet(client, tweet, dry_run=args.dry_run)


def generate_weekly_summary():
    """月曜: 先週末の成績まとめ"""
    today = datetime.now()
    # 直近の土日を計算
    last_sun = today - timedelta(days=today.weekday() + 1)
    last_sat = last_sun - timedelta(days=1)

    sat_str = last_sat.strftime("%Y-%m-%d")
    sun_str = last_sun.strftime("%Y-%m-%d")

    with get_db() as conn:
        # 先週末のキャッシュ予測から結果を集計
        cached = conn.execute("""
            SELECT COUNT(*) as cnt,
                   SUM(CASE WHEN pc.should_bet = 1 THEN 1 ELSE 0 END) as bet_races
            FROM races r
            JOIN predictions_cache pc ON r.race_id = pc.race_id
            WHERE r.race_date IN (?, ?)
        """, (sat_str, sun_str)).fetchone()

    race_count = cached["cnt"] if cached else 0
    bet_count = cached["bet_races"] if cached else 0

    date_range = f"{last_sat.month}/{last_sat.day}-{last_sun.month}/{last_sun.day}"

    today_str = datetime.now().strftime("%m/%d %H:%M")
    tweet = f"📊 先週末({date_range})の振り返り\n"
    tweet += f"━━━━━━━━━━━━\n\n"
    tweet += f"🏇 分析レース数: {race_count}R\n"
    tweet += f"💰 推奨レース数: {bet_count}R\n\n"
    tweet += f"今週末も厳選レースをAI分析します。\n"
    tweet += f"メイン予想は土日朝8時に配信 🔔\n\n"
    tweet += f"📅 {today_str}\n#競馬予想 #AI予想"

    return tweet


def generate_jockey_ranking():
    """火曜: 騎手ランキング"""
    with get_db() as conn:
        top_jockeys = conn.execute("""
            SELECT j.jockey_name,
                   COUNT(*) as rides,
                   SUM(CASE WHEN r.finish_position = 1 THEN 1 ELSE 0 END) as wins,
                   SUM(CASE WHEN r.finish_position <= 3 THEN 1 ELSE 0 END) as top3
            FROM results r
            JOIN jockeys j ON r.jockey_id = j.jockey_id
            JOIN races ra ON r.race_id = ra.race_id
            WHERE ra.race_date >= date('now', '-30 days')
            AND r.finish_position > 0
            GROUP BY j.jockey_id
            HAVING rides >= 10
            ORDER BY CAST(top3 AS FLOAT) / rides DESC
            LIMIT 5
        """).fetchall()

    if not top_jockeys:
        return generate_analysis_column()

    today_str = datetime.now().strftime("%m/%d %H:%M")
    tweet = f"🏆 直近30日 騎手複勝率ランキング\n"
    tweet += f"━━━━━━━━━━━━\n\n"
    for i, j in enumerate(top_jockeys, 1):
        medal = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣"][i-1]
        rate = round(j["top3"] / j["rides"] * 100, 1)
        tweet += f"{medal} {j['jockey_name']} — {rate}% ({j['top3']}/{j['rides']})\n"
    tweet += f"\n※ 出走10回以上が対象\n"
    tweet += f"当モデルでは騎手×調教師コンビの実績を重点分析しています 🧠\n\n"
    tweet += f"📅 {today_str}\n#競馬予想 #AI予想 #騎手成績"

    return tweet


def generate_analysis_column():
    """水曜: 分析コラム"""
    columns = [
        {
            "title": "重馬場で浮上する血統とは？",
            "body": (
                "重馬場になると成績が激変する馬がいます。\n\n"
                "当モデルでは天候・馬場状態を特徴量に組み込み、\n"
                "重馬場時の過去複勝率を個別に評価。\n\n"
                "「良馬場では凡走、重馬場で突然好走」\n"
                "こうした馬を見逃さない仕組みです 🧠"
            ),
        },
        {
            "title": "スピード指数(SI)の読み方",
            "body": (
                "SI = 過去走の走破タイムを距離・馬場で補正した能力値。\n\n"
                "目安:\n"
                "・50前後 → 平均的\n"
                "・70以上 → かなり強い\n"
                "・90以上 → 重賞級\n\n"
                "ただしSIが高くてもオッズが低ければ\n"
                "期待値はマイナス。AIは期待値で判断します 📊"
            ),
        },
        {
            "title": "なぜ本命馬を買わないのか",
            "body": (
                "「1番人気を外して穴馬を買う」\n"
                "一見無謀に見えますが、これが回収率の鍵。\n\n"
                "期待値(EV) = 勝率 × オッズ\n\n"
                "人気馬はオッズが低すぎて\n"
                "当たっても利益が出ないことが多い。\n"
                "AIは「勝てる馬」ではなく「儲かる馬」を選びます 💡"
            ),
        },
        {
            "title": "騎手×調教師コンビの威力",
            "body": (
                "当モデルの特徴量重要度ランキング1位は\n"
                "「騎手×調教師コンビの複勝率」。\n\n"
                "同じ騎手でも、誰の馬に乗るかで\n"
                "成績が大きく変わります。\n\n"
                "相性の良いコンビを統計的に検出し、\n"
                "予測精度を大幅に向上させています 🤝"
            ),
        },
        {
            "title": "3つのAIモデルを統合する理由",
            "body": (
                "当予測は3つの異なるモデルを統合:\n\n"
                "1️⃣ LambdaRank — 着順最適化\n"
                "2️⃣ 勝率モデル — 1着確率\n"
                "3️⃣ 複勝率モデル — 3着内確率\n\n"
                "単一モデルだと得意・不得意がありますが、\n"
                "統合することで安定した精度を実現 🎯"
            ),
        },
    ]

    col = random.choice(columns)
    today_str = datetime.now().strftime("%m/%d %H:%M")
    tweet = f"🧠 AI競馬コラム\n"
    tweet += f"━━━━━━━━━━━━\n"
    tweet += f"【{col['title']}】\n\n"
    tweet += col["body"]
    tweet += f"\n\n📅 {today_str}\n#競馬予想 #AI予想 #競馬コラム"

    return tweet


def generate_pickup_horse():
    """木曜: 注目馬ピックアップ"""
    with get_db() as conn:
        # SI上位の最近好走した馬
        top_horses = conn.execute("""
            SELECT h.horse_name,
                   AVG(r.finish_position) as avg_pos,
                   COUNT(*) as runs
            FROM results r
            JOIN horses h ON r.horse_id = h.horse_id
            JOIN races ra ON r.race_id = ra.race_id
            WHERE ra.race_date >= date('now', '-60 days')
            AND r.finish_position > 0
            AND r.finish_position <= 3
            GROUP BY h.horse_id
            HAVING runs >= 2
            ORDER BY avg_pos ASC
            LIMIT 5
        """).fetchall()

    if not top_horses:
        return generate_analysis_column()

    today_str = datetime.now().strftime("%m/%d %H:%M")
    tweet = f"🐴 直近60日 好走馬ピックアップ\n"
    tweet += f"━━━━━━━━━━━━\n\n"
    for i, h in enumerate(top_horses, 1):
        avg = round(h["avg_pos"], 1)
        tweet += f"⭐ {h['horse_name']} — 平均着順 {avg}位 ({h['runs']}走)\n"
    tweet += f"\n次走で注目したい馬たちです 👀\n"
    tweet += f"週末の出走情報は金曜に配信予定\n\n"
    tweet += f"📅 {today_str}\n#競馬予想 #AI予想 #注目馬"

    return tweet


def generate_weekend_preview():
    """金曜: 週末プレビュー"""
    tweet = f"📅 今週末のレースプレビュー\n"
    tweet += f"━━━━━━━━━━━━\n\n"
    tweet += f"🧠 AIモデルによる全レース分析を実施中...\n\n"
    tweet += f"明日朝8時にメインレースの予想を配信します 🔔\n\n"
    tweet += f"全レースの詳細予想はプロフリンクのnoteから！\n\n"
    tweet += f"#競馬予想 #AI予想 #週末競馬"

    return tweet


def main():
    parser = argparse.ArgumentParser(
        description="🐦 EQUINOX Lab — X自動投稿"
    )
    subparsers = parser.add_subparsers(dest="command")

    # predict
    p_pred = subparsers.add_parser("predict", help="メインレース予想を投稿")
    p_pred.add_argument("--date", required=True, help="対象日 (YYYYMMDD)")
    p_pred.add_argument("--dry-run", action="store_true", help="投稿せずプレビュー")

    # results
    p_res = subparsers.add_parser("results", help="的中結果を投稿")
    p_res.add_argument("--date", required=True, help="対象日 (YYYYMMDD)")
    p_res.add_argument("--dry-run", action="store_true", help="投稿せずプレビュー")

    # weekday
    p_week = subparsers.add_parser("weekday", help="平日コンテンツを投稿")
    p_week.add_argument("--dry-run", action="store_true", help="投稿せずプレビュー")

    args = parser.parse_args()
    init_db()

    if not HAS_TWEEPY and not getattr(args, "dry_run", False):
        print("❌ tweepy がインストールされていません")
        print("   pip install tweepy")
        return

    if args.command == "predict":
        cmd_predict(args)
    elif args.command == "results":
        cmd_results(args)
    elif args.command == "weekday":
        cmd_weekday(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
