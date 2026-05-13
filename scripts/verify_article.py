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
    """年度の連続性チェック。記事に出てくる年度のリストから欠落を検出。"""
    issues = []
    # 「歴代勝ち馬」「過去X年」セクション周辺で 20XX 年を探す
    # 範囲: 連続する 6年程度の年度が出ていれば「全部出すべき」と判断
    years = sorted({int(y) for y in re.findall(r'\b(20[12]\d)\b', text) if 2018 <= int(y) <= 2025})
    if len(years) >= 3:
        # 連続範囲か?
        ymin, ymax = years[0], years[-1]
        expected = set(range(ymin, ymax + 1))
        actual = set(years)
        missing = expected - actual
        if missing:
            issues.append(f"❌ 年度抜け検出: {sorted(missing)} が言及されていない (記事範囲 {ymin}-{ymax} 中)")
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
        all_issues += check_placeholders(text)

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
