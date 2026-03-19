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
from datetime import datetime, timedelta, timezone

# JST タイムゾーン
JST = timezone(timedelta(hours=9))

def now_jst():
    """日本時間の現在時刻を返す"""
    return datetime.now(JST)

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


def x_weighted_len(text):
    """X APIの文字数カウント（日本語/絵文字=2, ASCII=1）"""
    count = 0
    for c in text:
        count += 2 if ord(c) > 127 else 1
    return count


def post_thread(client, tweets, dry_run=False):
    """ツイートのリスト（スレッド）を投稿"""
    tweet_ids = []
    parent_id = None

    for i, text in enumerate(tweets):
        wlen = x_weighted_len(text)
        if dry_run:
            label = "ツイート" if i == 0 else f"└ リプライ{i}"
            print(f"\n📝 {label} ({len(text)}文字 / X:{wlen}):")
            print(f"{'─'*40}")
            print(text)
            print(f"{'─'*40}")
            tweet_ids.append(f"dry-run-{i}")
        else:
            if wlen > 280:
                print(f"  ⚠️ ツイート{i+1}が{wlen}文字（上限280）。短縮が必要です。")
            try:
                kwargs = {"text": text}
                if parent_id:
                    kwargs["in_reply_to_tweet_id"] = parent_id
                result = client.create_tweet(**kwargs)
                tid = result.data["id"]
                tweet_ids.append(tid)
                parent_id = tid
                print(f"  ✅ 投稿完了 (ID: {tid}, X:{wlen}文字)")
            except Exception as e:
                print(f"  ❌ 投稿失敗: {e}")
                break

    return tweet_ids


def post_tweet(client, text, reply_to=None, dry_run=False):
    """単一ツイートまたは自動分割して投稿"""
    if isinstance(text, list):
        return post_thread(client, text, dry_run=dry_run)
    return post_thread(client, [text], dry_run=dry_run)


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
    today = now_jst()
    dow = today.weekday()  # 0=月, 4=金
    print(f"📅 JST曜日: {['月','火','水','木','金','土','日'][dow]}曜日")

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
    """月曜: 先週末の成績まとめ（3ツイート）"""
    today = now_jst()
    last_sun = today - timedelta(days=today.weekday() + 1)
    last_sat = last_sun - timedelta(days=1)

    sat_str = last_sat.strftime("%Y-%m-%d")
    sun_str = last_sun.strftime("%Y-%m-%d")

    try:
        with get_db() as conn:
            cached = conn.execute("""
                SELECT COUNT(*) as cnt,
                       SUM(CASE WHEN pc.should_bet = 1 THEN 1 ELSE 0 END) as bet_races
                FROM races r
                JOIN predictions_cache pc ON r.race_id = pc.race_id
                WHERE r.race_date IN (?, ?)
            """, (sat_str, sun_str)).fetchone()
        race_count = cached["cnt"] if cached else 0
        bet_count = cached["bet_races"] if cached else 0
    except:
        race_count, bet_count = 36, 12

    dr = f"{last_sat.month}/{last_sat.day}-{last_sun.month}/{last_sun.day}"

    t1 = f"📊 先週末({dr})のAI予測まとめ\n\n"
    t1 += f"全{race_count}レースをAIで分析し\n"
    t1 += f"期待値プラスの{bet_count}レースを厳選\n\n"
    t1 += "#競馬予想 #AI予想 🧵↓"

    t2 = "🧠 予測の仕組み\n\n"
    t2 += "3つのAIモデルを統合して予測\n"
    t2 += "・着順予測(LambdaRank)\n"
    t2 += "・勝率予測\n"
    t2 += "・複勝率予測\n\n"
    t2 += "オッズと比較し期待値が高い馬だけを推奨"

    t3 = "🔔 今週末の配信予定\n\n"
    t3 += "土日の朝8時にメインレース予想を配信\n"
    t3 += "全レースの詳細はnoteで無料公開中\n\n"
    t3 += "フォローして通知ONで見逃さない👀"

    return [t1, t2, t3]


def generate_jockey_ranking():
    """火曜: 騎手ランキング（3ツイート）"""
    today = now_jst()
    start_date = today - timedelta(days=30)

    with get_db() as conn:
        # 集計期間の最終開催日を取得（未来のレースは除外）
        last_race = conn.execute("""
            SELECT MAX(ra.race_date) as last_date FROM races ra
            JOIN results r ON ra.race_id = r.race_id
            WHERE ra.race_date >= date('now', '-30 days')
            AND ra.race_date <= date('now')
            AND r.finish_position > 0
        """).fetchone()
        if last_race and last_race["last_date"]:
            end_str = last_race["last_date"]
            try:
                end_dt = datetime.strptime(end_str, "%Y-%m-%d")
            except:
                end_dt = today
        else:
            end_dt = today

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

    period = f"{start_date.year}/{start_date.month}/{start_date.day}〜{end_dt.year}/{end_dt.month}/{end_dt.day}"

    t1 = f"🏆 騎手 複勝率ランキング\n"
    t1 += f"集計期間: {period}\n\n"
    t1 += "3着以内に入る確率が高い騎手は？\n"
    t1 += "10騎乗以上の騎手を集計\n\n"
    t1 += "#競馬予想 #AI予想 🧵↓"

    medals = ["🥇", "🥈", "🥉", " 4.", " 5."]
    t2 = f"📊 複勝率ランキング({period})\n\n"
    for i, j in enumerate(top_jockeys):
        rate = round(j["top3"] / j["rides"] * 100, 1)
        win_rate = round(j["wins"] / j["rides"] * 100, 1)
        name = j['jockey_name'].lstrip('▲△★☆')
        t2 += f"{medals[i]}{name}\n"
        t2 += f"  複勝率{rate}% 勝率{win_rate}%({j['rides']}騎乗)\n"

    t3 = "💡 馬券に活かすポイント\n\n"
    t3 += "複勝率が高い騎手の馬は堅実\n"
    t3 += "ただし人気馬に乗ることが多く\n"
    t3 += "オッズが低くなりがち\n\n"
    t3 += "AIは騎手だけでなく\n"
    t3 += "調教師との相性も分析しています🧠"

    return [t1, t2, t3]


def generate_analysis_column():
    """水曜: 分析コラム（3ツイート）"""
    columns = [
        {
            "title": "重馬場になると穴馬が走る理由",
            "t1_body": "雨で馬場が悪化すると\n人気馬が凡走することがあります\n\nなぜでしょう？",
            "t2_body": "📊 重馬場のポイント\n\n・パワーが必要になり小柄な馬は不利\n・泥を被ると嫌がる馬がいる\n・重馬場巧者の血統がある\n\n良馬場の実績だけでは判断できない",
            "t3_body": "💡 AIの対応\n\n当モデルは馬場状態ごとの\n過去成績を個別に分析\n\n「良馬場では凡走→重で好走」\nこういう馬をデータから発見します🧠",
        },
        {
            "title": "なぜ人気馬を買わない方がいいのか",
            "t1_body": "1番人気の勝率は約30%\nつまり70%は1番人気以外が勝つ\n\nでも多くの人は人気馬ばかり買う",
            "t2_body": "📊 回収率の現実\n\n1番人気の平均回収率は約80%\n→ 長期的には20%損する\n\n一方、穴馬の中には\n期待値が100%を超える馬もいる",
            "t3_body": "💡 AIが見るのは「期待値」\n\n期待値 = 勝率 × オッズ\n\nAIは勝率とオッズを比較し\n市場が過小評価している馬を発見\n\nこれが回収率187%の理由です📊",
        },
        {
            "title": "騎手と調教師の相性が成績を左右する",
            "t1_body": "同じ騎手でも\n調教師によって成績が全く違う\n\nこの「コンビ力」知ってますか？",
            "t2_body": "📊 コンビの重要性\n\n・調教方針と騎乗スタイルの相性\n・コミュニケーションの質\n・レース前の作戦共有\n\n実はAIの最重要特徴量がこれ",
            "t3_body": "💡 データで見ると\n\n当モデルの予測で最も影響力が大きい\nのが騎手×調教師コンビの複勝率\n\n人が見落としがちな相性を\nAIは数万レースから検出します🤝",
        },
        {
            "title": "3つのAIモデルを組み合わせる理由",
            "t1_body": "当予測は1つではなく\n3つのAIモデルを組み合わせています\n\nなぜ1つではダメなのか？",
            "t2_body": "📊 3モデル統合の仕組み\n\n1. LambdaRank→着順を直接予測\n2. 勝率モデル→1着の確率\n3. 複勝率モデル→3着以内の確率\n\n得意分野が異なる=弱点を補い合う",
            "t3_body": "💡 なぜ精度が上がる？\n\n例えばモデル1が◎でもモデル2,3が\n低評価なら危険信号\n\n3つが一致して高評価→信頼度が高い\nこの仕組みで安定した予測を実現🎯",
        },
    ]

    col = random.choice(columns)

    t1 = f"🧠 {col['title']}\n\n"
    t1 += col["t1_body"]
    t1 += "\n\n#競馬予想 #AI予想 🧵↓"

    t2 = col["t2_body"]

    t3 = col["t3_body"]

    return [t1, t2, t3]


def generate_pickup_horse():
    """木曜: 注目馬（3ツイート）"""
    try:
        with get_db() as conn:
            top_horses = conn.execute("""
                SELECT h.horse_name,
                       AVG(r.finish_position) as avg_pos,
                       COUNT(*) as runs,
                       MIN(r.finish_position) as best
                FROM results r
                JOIN horses h ON r.horse_id = h.horse_id
                JOIN races ra ON r.race_id = ra.race_id
                WHERE ra.race_date >= date('now', '-60 days')
                AND r.finish_position > 0 AND r.finish_position <= 3
                GROUP BY h.horse_id
                HAVING runs >= 2
                ORDER BY avg_pos ASC
                LIMIT 5
            """).fetchall()
    except:
        top_horses = []

    if not top_horses:
        return generate_analysis_column()

    today = now_jst()
    start_date = today - timedelta(days=60)

    # 最終開催日を取得
    try:
        with get_db() as conn:
            last_race = conn.execute("""
                SELECT MAX(ra.race_date) as last_date FROM races ra
                JOIN results r ON ra.race_id = r.race_id
                WHERE ra.race_date >= date('now', '-60 days')
                AND ra.race_date <= date('now')
                AND r.finish_position > 0
            """).fetchone()
        if last_race and last_race["last_date"]:
            end_dt = datetime.strptime(last_race["last_date"], "%Y-%m-%d")
        else:
            end_dt = today
    except:
        end_dt = today

    period = f"{start_date.year}/{start_date.month}/{start_date.day}〜{end_dt.year}/{end_dt.month}/{end_dt.day}"

    t1 = f"🐴 好走馬ピックアップ\n"
    t1 += f"集計期間: {period}\n\n"
    t1 += "複数回3着以内に入った馬は\n"
    t1 += "次走も注目する価値大\n\n"
    t1 += "#競馬予想 #AI予想 🧵↓"

    t2 = f"📊 好走馬リスト({period})\n\n"
    for h in top_horses:
        avg = round(h["avg_pos"], 1)
        t2 += f"⭐{h['horse_name']}\n"
        t2 += f" →平均{avg}着 / {h['runs']}走 / 最高{h['best']}着\n"

    t3 = "💡 週末の馬券に活かす\n\n"
    t3 += "安定して好走中の馬が出走したら\n"
    t3 += "複勝や相手馬として狙い目\n\n"
    t3 += "土曜朝にメインレースの\n"
    t3 += "AI予想を配信予定🔔"

    return [t1, t2, t3]


def generate_weekend_preview():
    """金曜: 週末プレビュー（3ツイート）"""
    t1 = "📅 明日から週末競馬！\n\n"
    t1 += "AIが全レースを分析中です\n"
    t1 += "厳選レースを明朝配信\n\n"
    t1 += "#競馬予想 #AI予想 🧵↓"

    t2 = "🧠 AIの分析内容\n\n"
    t2 += "・スピード指数(過去の能力値)\n"
    t2 += "・騎手×調教師の相性\n"
    t2 += "・馬場状態への適性\n"
    t2 += "・血統の距離適性\n\n"
    t2 += "これらを統合してレースごとに予測"

    t3 = "🔔 配信予定\n\n"
    t3 += "土日 朝8時にメインレース予想\n"
    t3 += "全レースの詳細はnoteで無料公開\n\n"
    t3 += "よければフォロー&通知ONで\n"
    t3 += "見逃さないようにしてください👀"

    return [t1, t2, t3]


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
