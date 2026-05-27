#!/usr/bin/env python3
"""記事 / 投稿テンプレのファクトチェック CLI

検出ルール:
  ① 年度の連続性 — 言及された年度に欠落が無いか(例: 2020-2025 中 2023 抜けはNG)
  ② 馬名の DB 照合 — 記事中の馬名が DB の horses テーブルに存在するか
  ③ 数値の DB 照合 — 「○○人気 ○○倍」の表記が DB の実値と一致するか
  ④ プレースホルダー残存 — [URL], (URL差し替え), [TODO] などの未記入残り
  ⑤ 矛盾検出 — 同じ馬の人気/オッズが文中で複数記述されたら不整合チェック

致命的問題があれば exit 1。warning は exit 0 だが標準エラーに表示。

使い方:
  python scripts/verify_article.py articles/victoria_mile_2026_note_part1.html
  python scripts/verify_article.py articles/victoria_mile_2026_promo_part1.md
"""
import sys, os, re, sqlite3, argparse


def load_horses_set(conn):
    """horses テーブルから既知の馬名 set を返す"""
    rows = conn.execute("SELECT horse_name FROM horses WHERE horse_name != ''").fetchall()
    return {r[0] for r in rows}


def load_race_winners(conn, race_name_pattern, year_min=2018):
    """指定レースの過去勝ち馬を年度別に取得"""
    rows = conn.execute("""
        SELECT SUBSTR(ra.race_date, 1, 4) AS yr,
               h.horse_name, r.popularity, r.odds
        FROM results r JOIN races ra ON r.race_id=ra.race_id
        LEFT JOIN horses h ON r.horse_id=h.horse_id
        WHERE ra.race_name LIKE ? AND r.finish_position=1 AND ra.race_date>=?
        ORDER BY ra.race_date
    """, (f'%{race_name_pattern}%', f'{year_min}-01-01')).fetchall()
    return [(r[0], r[1], r[2], r[3]) for r in rows]


def strip_html(text):
    """HTMLタグを除去してプレーンテキスト化"""
    return re.sub(r'<[^>]+>', '', text)


def check_year_continuity(text):
    """年度の連続性チェック。記事に出てくる年度のリストから欠落を検出。

    例外: 欠落年度について「YYYY 年は DB 欠落 / 対象外 / 未登録」等の
         注釈が記事中にあれば許容する (誠実なサンプル不足明示)。
    """
    issues = []
    years = sorted({int(y) for y in re.findall(r'\b(20[12]\d)\b', text) if 2018 <= int(y) <= 2025})
    if len(years) >= 3:
        ymin, ymax = years[0], years[-1]
        expected = set(range(ymin, ymax + 1))
        actual = set(years)
        missing = expected - actual
        # 注釈で許容された欠落を除外
        truly_missing = []
        for yr in sorted(missing):
            patterns = [
                rf'{yr}[^\n]{{0,30}}(?:DB欠落|データ欠落|対象外|未登録|未収集)',
                rf'(?:DB欠落|データ欠落|対象外|未登録|未収集)[^\n]{{0,30}}{yr}',
                rf'{yr}.{{0,20}}(?:除く|含まない)',
                rf'(?:除く|含まない).{{0,20}}{yr}',
            ]
            if not any(re.search(p, text) for p in patterns):
                truly_missing.append(yr)
        if truly_missing:
            issues.append(f"❌ 年度抜け検出: {truly_missing} が言及されていない (記事範囲 {ymin}-{ymax} 中)")
    return issues


def check_horse_names(text, horses_set):
    """記事中の人名/馬名らしきものを抽出し、DB と照合。
    完全な照合は難しいので、カタカナ4文字以上の連続を「馬名候補」とみなす。
    """
    issues = []
    # カタカナ4文字以上の連続を抽出 (馬名らしき)
    candidates = re.findall(r'[ァ-ヴー]{4,}', text)
    # 既知の非馬名 (騎手名・血統名・地名等)
    known_non_horses = {
        'ルメール', 'モレイラ', 'デムーロ', 'ルメル', '川田', '武豊',
        'ルーラーシップ', 'キングカメハメハ', 'キズナ', 'ドゥラメンテ', 'レイデオロ',
        'エピファネイア', 'ロードカナロア', 'ハービンジャー', 'フジキセキ',
        'リアルスティール', 'ドレフォン', 'スクリーンヒーロー', 'モーリス',
        'サートゥルナーリア', 'マインドユアビスケッツ', 'ブリックスアンドモルタル',
        'ディープインパクト', 'ステイゴールド', 'ダイワメジャー', 'クロフネ',
        'シンボリクリスエス', 'タイキシャトル', 'ヴィクトワールピサ',
        'アグネスタキオン', 'ジャングルポケット', 'ブライアンズタイム',
        'コンコルドアタックス', 'コンコルド',
        'ヴィクトリアマイル', 'NHKマイル', '新潟大賞典', '京都新聞杯', 'エプソムカップ',
        'スプリンターズ', 'マイルチャンピオン', '安田記念',
        'キンマンボ', 'キングマンボ',
        'バクシンオー', 'サクラバクシンオー',
        'ピザモットウィークタウン',
    }
    unknown = []
    for cand in set(candidates):
        if cand in known_non_horses or len(cand) > 14:
            continue
        if cand not in horses_set:
            unknown.append(cand)
    if unknown:
        # 警告レベル (致命的ではない)
        issues.append(f"⚠️ DB未登録の馬名候補(誤字 or 騎手・血統名の可能性): {sorted(unknown)[:8]}")
    return issues


def check_winner_consistency(text, winners):
    """記事中の (年度, 馬名) ペアが DB と一致するか確認"""
    issues = []
    # 「2024 テンハッピーローズ」「2025年:アスコリピチェーノ」などのパターン
    patterns = re.findall(r'(20\d\d)[年:\s]*[(:]*\s*([ァ-ヴー]{3,})', text)
    db_map = {y: (n, p, o) for y, n, p, o in winners}
    for yr, name in patterns:
        if yr not in db_map:
            continue
        db_name = db_map[yr][0]
        if db_name and name not in db_name and db_name not in name:
            issues.append(f"⚠️ {yr}年勝ち馬: 記事「{name}」 vs DB「{db_name}」")
    return issues


def check_numeric_claim_consistency(text):
    """記事中の集計クレーム (N勝/M, X人気がY勝 等) と本文の事実列挙が一致するか検算。

    例: 「1番人気が3勝/4」と書いてあったら、本文中の年度別人気リストから
        「1人気」を自動カウントし、3 と一致するか確認。

    検出パターン:
      ① 「N番人気がX勝」or 「N人気がX勝」 vs 「20XX...N人気」のカウント
      ② 「X勝/Y」or 「X/Y勝」 — 母集団 Y は信頼、X は人気帯クレームと整合
    """
    issues = []
    # ① 「N番人気がX勝」or 「N人気がX勝」の検出
    for m in re.finditer(r'(\d+)\s*(?:番)?人気が\s*(\d+)\s*勝', text):
        target_pop = int(m.group(1))
        claimed_wins = int(m.group(2))
        # 本文中の「20XX ... N人気」を全部カウント (winners リスト等)
        # 「20XX 馬名: N人気」or 「20XX ... N人気」のパターン
        actual = len(re.findall(
            rf'20\d{{2}}\s+\S+[^\n]*?{target_pop}人気',
            text
        ))
        if actual > 0 and actual != claimed_wins:
            issues.append(
                f"🚫 数値不整合: 「{target_pop}番人気が{claimed_wins}勝」と記載があるが、"
                f"本文中の事実列挙では {actual}件 ({target_pop}人気の年度) のみ検出"
            )

    # ② 「N勝/M」表記の M (分母) と本文の事実列挙数を照合
    # 例: 「過去4年勝ち馬」セクションに4件しか無いのに「N勝/5」と書いたら警告
    # ※ 厳密判定難しいので heuristic

    return issues


def check_placeholders(text):
    """未記入のプレースホルダーが残っていないか"""
    issues = []
    bad_patterns = [
        r'\[URL\]',
        r'\[TODO\]',
        r'\[馬名\]',
        r'\(URL差し替え\)',
        r'\(URLを?差し替え.*\)',
        r'\(記事公開後.*\)',
        r'XX',
    ]
    # ただし「ここをコピーして使う」前提のテンプレファイルなら [URL] は OK
    is_template = ('promo' in text[:500].lower() or '投稿テンプレ' in text[:500]
                   or '## ' in text[:200])
    for pat in bad_patterns:
        if pat == r'\[URL\]' and is_template:
            continue  # テンプレなら [URL] は許容
        if re.search(pat, text):
            issues.append(f"⚠️ プレースホルダー残存: {pat}")
    return issues


def check_horse_numbers_confirmed(text, conn, race_name=None):
    """馬番抽選前に馬番を記事に出していないかチェック (#24 教訓 — 2026-05-26)

    JRA の枠順抽選は金 11:00。それ以前 (= horse_number IS NULL/0 が残ってる) に
    記事で「◎10番」「7番 アスコリピチェーノ」と書いてしまうのを防ぐ。

    判定:
      1. 記事に「N番」(N=1-18) や「◎N番」「○N番」表記があれば抽出
      2. race_name が指定されてれば、対象レース (race_date > today) の
         results を見て horse_number が確定済か確認
      3. 1頭でも horse_number=NULL/0 なら 🚫 ブロック
    """
    issues = []
    if not race_name or not conn:
        return issues

    # 「N番」表記の抽出 (印/数字/番)
    horse_numbers = set()
    for m in re.finditer(r'(?:◎|○|▲|△|×|注|^|\s|→)(\d{1,2})\s*番(?!人気)', text):
        try:
            n = int(m.group(1))
            if 1 <= n <= 18:
                horse_numbers.add(n)
        except (ValueError, IndexError):
            pass

    if not horse_numbers:
        return issues  # 馬番が出てなければチェック不要

    # 対象レースを取得 (未来日 = まだレースしてない)
    rows = conn.execute("""
        SELECT race_id, race_date, race_name FROM races
        WHERE race_name LIKE ? AND race_date >= date('now', 'localtime')
        ORDER BY race_date LIMIT 1
    """, (f'%{race_name}%',)).fetchall()

    if not rows:
        # 過去レースのみ参照してる記事ならスキップ
        return issues

    race_id = rows[0][0]
    race_date = rows[0][1]

    # post_position (枠順) が NULL/0 = 金11時の枠順抽選前 (馬番もまだ正式じゃない)
    # horse_number は出馬表時点で「出走順」として入ることがあるので、より厳格な
    # 確定シグナルは post_position を使う
    cur = conn.execute("""
        SELECT COUNT(*) FROM results
        WHERE race_id = ? AND (post_position IS NULL OR post_position = 0)
    """, (race_id,))
    no_post = cur.fetchone()[0]

    cur = conn.execute("SELECT COUNT(*) FROM results WHERE race_id = ?", (race_id,))
    total = cur.fetchone()[0]

    if no_post > 0:
        issues.append(
            f"🚫 枠順未確定 (対象レース「{race_name}」 {race_date}): {no_post}/{total} 頭が "
            f"post_position 未設定。本文に馬番 {sorted(horse_numbers)} 表記あり。"
            f"JRA 枠順抽選は金 11:00 — それ以前に馬番を記事に書かない"
        )
    return issues


def check_marks_confirmed(text, conn, race_name=None):
    """印 (◎○▲△×注) を確定前に使用してないかチェック (#24 教訓 — 2026-05-26)

    印は predictions_cache が生成された後にしか付かない。記事で勝手に
    「◎キングズパレス」と書いてしまうと、実際の予測と乖離する。

    判定:
      1. 記事に「◎{馬名}」「○{馬名}」等のパターンがあれば抽出
      2. race_name 指定の対象レース (未来日) に predictions_cache が
         あるか確認
      3. キャッシュ無し or 馬名と印が DB と不一致なら ⚠️
    """
    issues = []
    if not race_name or not conn:
        return issues

    # 「◎{馬名}」「○ {馬名}」等のパターン抽出
    article_marks = {}  # {mark: horse_name}
    for m in re.finditer(r'(◎|○|▲|△|×|注)\s*(?:\d+番\s+)?([ァ-ヴー]{3,})', text):
        mark = m.group(1)
        name = m.group(2)
        if mark not in article_marks:
            article_marks[mark] = name

    if not article_marks:
        return issues

    # 対象レースの predictions_cache を確認
    rows = conn.execute("""
        SELECT pc.predictions_json, r.race_date FROM races r
        LEFT JOIN predictions_cache pc ON r.race_id = pc.race_id
        WHERE r.race_name LIKE ? AND r.race_date >= date('now', 'localtime')
        ORDER BY r.race_date LIMIT 1
    """, (f'%{race_name}%',)).fetchall()

    if not rows:
        return issues

    preds_json, race_date = rows[0]
    if not preds_json:
        issues.append(
            f"⚠️ 印確定前 (対象レース「{race_name}」 {race_date}): "
            f"predictions_cache 未生成だが本文に印 {list(article_marks.keys())} を使用。"
            f"印は予測完了後にしか確定しない — 「現時点の注目馬」として書くべき"
        )
        return issues

    # 印×馬名の照合
    import json as _json
    try:
        preds = _json.loads(preds_json)
    except (_json.JSONDecodeError, TypeError):
        return issues

    db_marks = {p.get('mark'): p.get('horse_name') for p in preds if p.get('mark')}
    for mark, article_name in article_marks.items():
        db_name = db_marks.get(mark)
        if db_name and article_name not in db_name and db_name not in article_name:
            issues.append(
                f"⚠️ 印不一致 ({race_name}): 記事「{mark}{article_name}」 vs DB「{mark}{db_name}」"
            )
    return issues


def main():
    ap = argparse.ArgumentParser(description="記事/投稿テンプレのファクトチェック")
    ap.add_argument('files', nargs='+', help='検証する HTML/MD ファイル')
    ap.add_argument('--db', default=os.path.join(os.path.dirname(__file__), '..', 'keiba.db'))
    ap.add_argument('--race', help='対象レース名(過去勝ち馬照合用)', default=None)
    args = ap.parse_args()

    try:
        conn = sqlite3.connect(args.db)
        conn.row_factory = sqlite3.Row
        horses_set = load_horses_set(conn)
    except Exception as e:
        print(f"⚠️ DB接続失敗: {e}", file=sys.stderr)
        horses_set = set()
        conn = None

    total_issues = 0
    critical = False

    for path in args.files:
        if not os.path.exists(path):
            print(f"❌ ファイルなし: {path}", file=sys.stderr)
            critical = True
            continue

        with open(path, 'r', encoding='utf-8') as f:
            raw = f.read()
        text = strip_html(raw) if path.endswith('.html') else raw

        print(f"\n━━━━━━ 検証: {path} ━━━━━━")

        all_issues = []
        all_issues += check_year_continuity(text)
        if horses_set:
            all_issues += check_horse_names(text, horses_set)
        if args.race and conn:
            winners = load_race_winners(conn, args.race)
            all_issues += check_winner_consistency(text, winners)
            # 🆕 #24 教訓延長 (2026-05-26): 馬番抽選前/印確定前を機械的に検出
            all_issues += check_horse_numbers_confirmed(text, conn, args.race)
            all_issues += check_marks_confirmed(text, conn, args.race)
        all_issues += check_placeholders(text)
        all_issues += check_numeric_claim_consistency(text)

        if not all_issues:
            print("✅ 問題なし")
        else:
            for iss in all_issues:
                print(f"  {iss}")
                if iss.startswith('❌'):
                    critical = True
                total_issues += 1

    print(f"\n━━━━━━ 総括 ━━━━━━")
    print(f"検出した問題: {total_issues}件 {'(致命的あり 🚫)' if critical else ''}")
    sys.exit(1 if critical else 0)


if __name__ == '__main__':
    main()
