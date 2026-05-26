"""tweet の DB クロスファクトチェック (2026-05-25 新設)

post_x.py の既存 fact_check_tweet() は regex パターン検証のみ。
本モジュールは「数値/馬名クレームが DB と整合しているか」を実 SQL で確認する。

設計思想:
- AI が「ダノンデサイル ← 青葉賞」のような捏造を出すリスクを最小化
- 致命: DB に存在しない馬名が複数 / サンプル不足を隠したクレーム → ブロック
- 警告: 確証取れないが疑わしい → ログのみ

呼び出し:
    from tweet_fact_check import db_fact_check
    passed, issues = db_fact_check(tweet_text)
    if not passed:
        # 投稿ブロック
"""

import os
import re
import sqlite3
from typing import List, Tuple, Set
from datetime import datetime


DB_PATH = os.path.join(os.path.dirname(__file__), "keiba.db")


# カタカナで一見「馬名候補」に見えるが実は一般用語/レース名/競馬用語
# DB 照合で誤検出を避けるための除外リスト
_NON_HORSE_KATAKANA: Set[str] = {
    # レース格
    "クラシック", "ハンデキャップ", "ハンデ", "オープン", "ステークス",
    # コース/施設
    "ターフ", "ダート", "コース", "ゲート", "パドック", "ターン", "ペース",
    # 競馬用語
    "クラブ", "メインレース", "ジャパン", "メイン", "ベスト", "ワースト",
    "クロス", "リスト", "サンプル", "テンプレ", "アルゴリズム", "ロジック",
    "データベース", "プレースホルダー", "ファクト", "クエリ", "セクション",
    "アタリ", "オッズ", "プラス", "マイナス", "ボックス", "フォーメーション",
    "クォリティ", "クオリティ", "シルバー", "ゴールド", "ブロンズ",
    # 抽象名詞 (固有名でない)
    "パターン", "ロジック", "スタイル", "ストーリー", "テーマ", "シリーズ",
    "メソッド", "シナリオ", "ステップ", "プロセス", "システム", "ベース",
    "ポイント", "リスク", "チャンス", "アタック", "アクセル", "ブレーキ",
    "コンテンツ", "ソリューション", "アクション", "メッセージ",
    "アナリシス", "リポート", "レビュー", "サマリー", "プレビュー",
    # 役割語
    "ライバル", "オーナー", "トレーナー", "ジョッキー", "オーナーズ",
    # 種牡馬系 (horses テーブルにない可能性)
    "ディープ系", "ノーザン", "サンデー", "サンデーサイレンス",
    # 騎手系統 (jockeys テーブル別)
    "ルメール", "クリスチャン", "デムーロ", "モレイラ",
    # 競馬用語のハッシュタグになりがちなもの
    "ダービー", "オークス", "ジャパンカップ", "ジャパンC",
    "ヴィクトリアマイル", "ヴィクトリア", "皐月賞", "菊花賞",
    "天皇賞", "有馬記念", "宝塚記念", "桜花賞", "クイーン", "高松宮",
    "セントウル", "セントライト", "スプリンター",
    # ヘッダー語
    "アナウンス", "ピックアップ", "ハイライト", "ランキング",
    "プレミアム", "ライト", "ヘビー", "セッション",
}


def _get_known_grade_races() -> Set[str]:
    """重賞 (G1/G2/G3) のレース名を DB から拾って除外リスト用に返す"""
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute(
            "SELECT DISTINCT race_name FROM races WHERE grade IN ('G1','G2','G3','GI','GII','GIII','J.G1','J.G2','J.G3') AND race_name IS NOT NULL"
        )
        names = {row[0] for row in cur.fetchall() if row[0]}
        conn.close()
        return names
    except Exception:
        return set()


def _is_horse_in_db(conn, name: str) -> bool:
    """horse_name または sire/damsire が DB に存在するか (完全/部分一致)。

    2026-05-26 拡張: 種牡馬 (horses.sire) や母父 (horses.damsire) も
    投稿テンプレに正当に出るので、これらも照合対象に追加。
    """
    cur = conn.cursor()
    # 完全一致 (馬名 / 種牡馬 / 母父)
    cur.execute(
        "SELECT 1 FROM horses "
        "WHERE horse_name = ? OR sire = ? OR damsire = ? LIMIT 1",
        (name, name, name),
    )
    if cur.fetchone():
        return True
    # 部分一致 (海外馬の英字付き "エピファネイアEpiphaneia(米)" 等を救済)
    cur.execute(
        "SELECT 1 FROM horses "
        "WHERE horse_name LIKE ? OR sire LIKE ? OR damsire LIKE ? LIMIT 1",
        (f"%{name}%", f"%{name}%", f"%{name}%"),
    )
    return bool(cur.fetchone())


def check_horse_names(tweet_text: str) -> List[str]:
    """tweet 内のカタカナ3字以上が DB の horses に存在するか確認。

    存在しない候補が複数あれば捏造のリスク高 → critical 扱い。
    """
    issues = []
    # 3字以上のカタカナ語を抽出
    candidates = set(re.findall(r"[ァ-ヴー]{3,}", tweet_text))
    if not candidates:
        return issues

    # 除外: 一般用語
    candidates -= _NON_HORSE_KATAKANA
    # 重賞レース名も除外
    grade_races = _get_known_grade_races()
    candidates -= grade_races

    if not candidates:
        return issues

    try:
        conn = sqlite3.connect(DB_PATH)
        unknown = []
        for name in candidates:
            if not _is_horse_in_db(conn, name):
                unknown.append(name)
        conn.close()
        # 1件は誤検出余地あるので警告のみ。2件以上で critical 扱い
        if len(unknown) >= 2:
            issues.append(f"🚫 DB に存在しない馬名候補が{len(unknown)}件: {unknown}")
        elif len(unknown) == 1:
            issues.append(f"⚠️ DB に未確認の馬名候補: {unknown[0]} (固有名でなければ無視)")
    except Exception as e:
        issues.append(f"ℹ️ 馬名チェック skip (DB 接続失敗): {e}")
    return issues


def check_past_year_sample(tweet_text: str) -> List[str]:
    """「過去N年」と書いてある時、DB に N 年分のデータが揃っているか確認。

    例: 「過去6年」と書いてあるのに DB に 2021-2023 のレースがない → 警告
    """
    issues = []
    m = re.search(r"過去\s*(\d+)\s*年", tweet_text)
    if not m:
        return issues
    years = int(m.group(1))
    if years < 2 or years > 20:
        return issues

    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        current_year = datetime.now().year
        sparse_years = []
        for y in range(current_year - years, current_year):
            cur.execute(
                "SELECT COUNT(*) FROM races WHERE substr(race_date,1,4) = ?",
                (str(y),),
            )
            count = cur.fetchone()[0]
            # 1年あたり100R 未満 = 「年として代表性がない」
            if count < 100:
                sparse_years.append(f"{y}年({count}R)")
        conn.close()
        if sparse_years:
            issues.append(
                f"🚫 「過去{years}年」と書かれているが DB はサンプル不足: {sparse_years}"
            )
    except Exception as e:
        issues.append(f"ℹ️ 年別サンプルチェック skip: {e}")
    return issues


def check_xy_ratio_claims(tweet_text: str) -> List[str]:
    """「X勝/Y」「N件中M件」のような数値クレームの妥当性を検証。

    Y が 3 未満 (サンプル不足) なのに、それを明示せずに % や率を書いてる場合に警告。
    """
    issues = []
    # 「N/M」または「N勝/M」または「M件中N件」パターン
    ratios = re.findall(r"(\d+)\s*[勝/]\s*(\d+)", tweet_text)
    for n_str, m_str in ratios:
        try:
            n, m = int(n_str), int(m_str)
            if m < 3 and "サンプル不足" not in tweet_text and "(参考)" not in tweet_text:
                issues.append(
                    f"⚠️ {n}/{m} のサンプル数が少なすぎ (3件未満) — 「サンプル不足」明記推奨"
                )
        except ValueError:
            continue
    return issues


def db_fact_check(tweet_text: str) -> Tuple[bool, List[str]]:
    """tweet の DB クロスチェック。

    Returns:
        (passed, issues)
        passed=True なら投稿OK。critical issue があれば False (ブロック)。
    """
    all_issues = []
    all_issues += check_horse_names(tweet_text)
    all_issues += check_past_year_sample(tweet_text)
    all_issues += check_xy_ratio_claims(tweet_text)

    # 🚫 始まりが critical (ブロック)、⚠️ ℹ️ は警告
    critical = any(issue.startswith("🚫") for issue in all_issues)
    return (not critical, all_issues)


if __name__ == "__main__":
    # コマンドラインから tweet 内容を渡して確認可能
    import sys
    text = sys.stdin.read() if not sys.stdin.isatty() else (sys.argv[1] if len(sys.argv) > 1 else "")
    if not text:
        print("Usage: echo 'tweet text' | python3 tweet_fact_check.py")
        print("   or: python3 tweet_fact_check.py 'tweet text'")
        sys.exit(1)
    passed, issues = db_fact_check(text)
    print(f"\n{'=' * 60}")
    print(f"判定: {'✅ 通過' if passed else '🚫 ブロック'}")
    print(f"{'=' * 60}")
    for issue in issues:
        print(f"  {issue}")
    sys.exit(0 if passed else 1)
