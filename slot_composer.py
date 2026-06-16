"""投稿テンプレ slot composer (Phase 2, 2026-05-25)

post_sections.py の sec_* 関数を組み合わせて、各 slot 用の
220-260字 (X 280字以内) tweet を生成する。

設計: docs/POST_TEMPLATE_DESIGN.md

使い方:
    from slot_composer import build_slot_post
    tweet, meta = build_slot_post('morning', race_dict, conn)
    # tweet: 投稿文字列
    # meta: {'sections_used': [...], 'char_count': N, 'samples': {...}}
"""

from __future__ import annotations
import re
import sqlite3
from typing import Tuple, Optional
from datetime import datetime

from post_sections import (
    sec_historical_winners,
    sec_pop_trust_trend,
    sec_sire_course_cross,
    sec_prev_race_pattern,
    sec_outlier_year,
    sec_pace_decisive,
    sec_jockey_recent_form,
    sec_post_position_bias,
    sec_dangerous_favorites,
    sec_pattern_discovery,
    sec_rotation_pattern,
    sec_age_pattern,
    sec_workout_focus,
    sec_entry_pedigree_match,
    sec_pedigree_deep,
    sec_eight_axis_top,
    sec_top3_with_reasons,
    sec_attention_top,
    sec_handpicked_top,
    sec_notable_horses,
)


# X 文字数カウント (日本語/絵文字 = 2, ASCII = 1)
def _x_len(text: str) -> int:
    return sum(2 if ord(c) > 127 else 1 for c in text)


def _short_grade(grade: Optional[str]) -> str:
    if not grade:
        return ""
    return f"({grade})"


def _race_label(race: dict) -> str:
    """レース名 + グレード短縮"""
    name = race.get("race_name", "")
    grade = _short_grade(race.get("grade"))
    return f"{name}{grade}".strip()


def _is_post_position_drawn(race: dict, today: Optional[datetime] = None) -> bool:
    """枠順抽選 (金11時 JST) が済んでいるかを判定。

    JRA の出走スケジュール:
    - 月: 特別登録
    - 木: 出走馬確定 + 斤量
    - 金 11:00: 枠順抽選 → 馬番確定
    - 土・日: レース当日

    抽選日 = レース日の最初の前金曜:
    - 土曜レース (weekday=5) → 抽選は前日 (金曜)
    - 日曜レース (weekday=6) → 抽選は前々日 (金曜)
    - 平日レース → 簡略化のため当日 11時以降で True

    今日 > 抽選日: True
    今日 < 抽選日: False
    今日 = 抽選日: 11時以降のみ True
    """
    from datetime import timedelta as _td
    today = today or datetime.now()
    race_date_str = race.get("race_date", "")
    try:
        if "-" in race_date_str:
            race_d = datetime.strptime(race_date_str[:10], "%Y-%m-%d").date()
        else:
            race_d = datetime.strptime(race_date_str[:8], "%Y%m%d").date()
    except (ValueError, TypeError):
        return False

    today_d = today.date()
    # 過去/当日: 確定済
    if race_d <= today_d:
        return True

    # 抽選日を計算 (レース日の前金曜)
    wd = race_d.weekday()  # 0=月 ... 5=土 6=日
    if wd == 5:  # 土曜レース: 前日金曜が抽選日
        draw_date = race_d - _td(days=1)
    elif wd == 6:  # 日曜レース: 前々日金曜が抽選日
        draw_date = race_d - _td(days=2)
    else:
        # 平日レース (祝日等の特殊): レース当日11時以降のみ
        draw_date = race_d

    if today_d > draw_date:
        return True
    if today_d < draw_date:
        return False
    # 抽選日当日 — 11時以降のみ True
    return today.hour >= 11


def _horse_label(horse: dict, race: dict, today: Optional[datetime] = None) -> str:
    """馬ラベル: 抽選前は馬名のみ、後は「N番 馬名」"""
    name = horse.get("horse_name", "?")
    hn = horse.get("horse_number")
    if not hn or hn == 0:
        return name
    if _is_post_position_drawn(race, today):
        return f"{hn}番 {name}"
    return name


def _day_phrase(race: dict, today: Optional[datetime] = None) -> str:
    """5/31(日) のような日付フレーズ (短縮版)"""
    today = today or datetime.now()
    race_date_str = race.get("race_date", "")
    try:
        if "-" in race_date_str:
            d = datetime.strptime(race_date_str[:10], "%Y-%m-%d")
        else:
            d = datetime.strptime(race_date_str[:8], "%Y%m%d")
    except (ValueError, TypeError):
        return ""
    weekday = ["月", "火", "水", "木", "金", "土", "日"][d.weekday()]
    diff_days = (d.date() - today.date()).days
    when = ""
    if diff_days == 0:
        when = "本日"
    elif diff_days == 1:
        when = "明日"
    # "今週末" は冗長なので削除 (5/31(日) のみで十分)
    return f"{when}{d.month}/{d.day}({weekday})" if when else f"{d.month}/{d.day}({weekday})"


def _make_section(title: str, lines: list) -> str:
    """セクションを 1ブロックに整形"""
    if not lines:
        return ""
    body = "\n".join(lines)
    return f"{title}\n{body}"


def _hashtags(race: dict) -> str:
    """ハッシュタグ生成 (X 文字数節約のため2つまで)"""
    tags = ["#AI競馬"]
    name = race.get("race_name", "").strip()
    if name and len(name) <= 8:
        tags.insert(0, f"#{name}")
    return " ".join(tags)


# X 文字数予算 (X 上限 280 ぴったりまで使い切り)
CHAR_BUDGET = 280
MAX_THREAD_TWEETS = 3  # スレッドの最大ツイート数


def _split_to_thread(header: str, sections: list, cta: str, hashtags: str,
                      budget: int = CHAR_BUDGET,
                      max_tweets: int = MAX_THREAD_TWEETS) -> list:
    """セクションをスレッド (複数 tweet) に分割する。

    戦略:
    - 1 tweet に全 sections 入れば単一 tweet
    - 入らなければ「1 section = 1 tweet」で分割
    - 1 section 内が大きすぎる場合は section の末尾行を trim

    Tweet 1: header + section1 + (続く🧵 or cta)
    Tweet 2+: 🧵 (i/N) + section_i + (続く🧵 or cta+hashtags)
    最終 tweet のみ hashtags が付く。
    """
    sections = [s for s in sections if s]
    if not sections:
        return [f"{header}\n\n{cta}\n{hashtags}"]

    # 1 tweet に全部入るか試算
    full_body = "\n\n".join(sections)
    full_tweet = f"{header}\n\n{full_body}\n\n{cta}\n{hashtags}"
    if _x_len(full_tweet) <= budget:
        return [full_tweet]

    # 1 section = 1 tweet で分割
    sections = sections[:max_tweets]  # max を超える section は捨てる
    total = len(sections)
    tweets = []
    for i, section in enumerate(sections):
        is_last = (i == total - 1)
        # ヘッダ
        if i == 0:
            tw_header = header
        else:
            tw_header = f"🧵 ({i+1}/{total})"
        # フッタ
        tw_cta = cta if is_last else "→ 続く🧵"
        tw_tags = f"\n{hashtags}" if is_last else ""

        # tweet 組み立て (section 内が大きすぎる場合は末尾 trim)
        section_lines = section.split("\n")
        while True:
            body = "\n".join(section_lines)
            tw = f"{tw_header}\n\n{body}\n\n{tw_cta}{tw_tags}"
            if _x_len(tw) <= budget or len(section_lines) <= 1:
                break
            section_lines = section_lines[:-1]
        # 🆕 #53: trim の結果セクションが「【見出し】」1行だけ (中身ゼロ) に
        # なった場合、見出しだけの無価値 tweet は出さない。header tweet (i==0) は
        # header 自体に価値があるので残すが、本文が見出しだけなら本文を空にする。
        if len(section_lines) == 1 and section_lines[0].startswith("【"):
            if i == 0:
                tw = f"{tw_header}\n\n{tw_cta}{tw_tags}"
            else:
                continue  # 2nd以降の見出しのみ tweet は丸ごとスキップ
        tweets.append(tw)

    return tweets


def _fit_to_budget(header: str, sections: list, cta: str, hashtags: str,
                    budget: int = CHAR_BUDGET) -> str:
    """セクションを優先度順に削って予算内に収める。

    削除優先順位 (高→低):
    1. **凡例的な出典行のみ** 削除 (「※AI勝率=LightGBM予測」「※netkeiba 追い切り評価より」等)
       → 実質的な洞察 (「※飛んだ馬の前走平均」「※過去6年のA評価馬の複勝率」) は残す
    2. セクション末尾の通常行 — 馬データ等
    3. セクション全体 — 最終手段

    header / hashtags / cta は必須 (削らない)。
    """
    # 「凡例的な出典行」だけを特定 (=と「より」を含む解説文)
    legend_keywords = ("※AI勝率=", "※SI=", "※netkeiba 追い切り評価より",
                       "※父産駒の当コース複勝率")

    def _assemble():
        body = "\n\n".join(s for s in sections if s)
        return f"{header}\n\n{body}\n\n{cta}\n{hashtags}"

    # Phase 1: 凡例的な出典行のみ削除 (洞察 ※ は残す)
    if _x_len(_assemble()) > budget:
        new_sections = []
        for s in sections:
            kept = [
                l for l in s.split("\n")
                if not any(l.startswith(k) for k in legend_keywords)
            ]
            new_sections.append("\n".join(kept))
        sections = new_sections

    # Phase 2: 末尾セクションから 1行ずつ削る
    # ※ 「タイトル【...】」のみ残る空 section は意味なしなので最終削除
    while sections and _x_len(_assemble()) > budget:
        last = sections[-1].split("\n")
        if len(last) == 1:
            # 1行のみ → section ごと削除
            sections.pop()
        else:
            # 末尾の1行を削る
            sections[-1] = "\n".join(last[:-1])
            # title だけ (「【...】」で始まる単独行) になったら section ごと削除
            if sections and "\n" not in sections[-1] and sections[-1].startswith("【"):
                sections.pop()

    return _assemble()


# ─────────────────────────────────────────────────────────
# 朝の曜日別ローテーション (#48 根本対策)
#
# morning slot は毎日「今週末メイン」を扱うため、固定3セクションだと
# 連日同一テキストになり X が "duplicate content" で 403 拒否していた
# (#48 は header に投稿日を入れて暫定回避)。ここで曜日ごとに切り口
# (セクションの組合せ) を変え、毎日違うコンテンツにして根本解決する。
#
# 各 _morning_sec_* は (section_str | None, sample_n) を返す純粋ヘルパー。
# 重要: thread 分割で各セクションは単独 tweet になり得るため、どのセクションも
#   単独で fact_check を通る形 (率%×2 / 率+サンプル数(N/M) / 馬名(年+カナ)) で
#   返すこと。"該当馬なし" 等は fact_check の empty_pattern に当たるので除去する。
def _format_chakudosu(rows):
    """results 行 [(year, finish), ...] から 着度数 (1着-2着-3着-着外) を計算。

    Returns:
        (w, p, s, out, total)
    """
    w = p = s = out = 0
    for _y, fp in rows:
        if fp == 1:
            w += 1
        elif fp == 2:
            p += 1
        elif fp == 3:
            s += 1
        else:
            out += 1
    total = w + p + s + out
    return (w, p, s, out, total)

def _lede_pop_chakudosu(conn, race_name, target_pop, years=6):
    """過去 years 年の同レースで target_pop 番人気の着度数を返す。

    Returns:
        (w, p, s, out, total) or None
    """
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT substr(r.race_date, 1, 4) AS year, res.finish_position
            FROM races r
            JOIN results res ON r.race_id = res.race_id
            WHERE r.race_name = ? AND res.popularity = ?
              AND res.finish_position IS NOT NULL
            ORDER BY r.race_date DESC
            LIMIT ?
            """,
            (race_name, target_pop, years),
        )
        rows = cur.fetchall()
        if len(rows) < 3:  # サンプル不足は煽れない
            return None
        return _format_chakudosu(rows)
    except Exception:
        return None

def _lede_strong_sires_count(conn, venue, surface, distance, race_id=None, years=6, threshold=50.0):
    """指定コースで過去 years 年・複勝率 threshold% 超の種牡馬数を返す。

    race_id 指定時は「今週その race に出走馬の父にいる種牡馬」のみを集計対象とする。
    出走しない種牡馬の数字を出して誤誘導しないため (CLAUDE.md 絶対ルール: 今週レース起点)。

    Returns:
        (count, sample_size_total, top3_sample_total) or None
    """
    if not venue or not surface or not distance:
        return None
    try:
        cur = conn.cursor()
        from_date = f"{datetime.now().year - years}-01-01"

        # 今週の出走馬の父 (race_id 指定時のみフィルタ)
        entry_sires = None
        if race_id:
            rows = cur.execute(
                """
                SELECT DISTINCT h.sire FROM results res
                JOIN horses h ON res.horse_id = h.horse_id
                WHERE res.race_id = ? AND h.sire IS NOT NULL AND h.sire != ''
                """,
                (race_id,),
            ).fetchall()
            entry_sires = {r[0] for r in rows if r[0]}
            if not entry_sires:
                return None  # 出走馬未確定 or 血統データ無し → lede 出さない

        cur.execute(
            """
            SELECT h.sire, COUNT(*) AS n,
                   SUM(CASE WHEN res.finish_position BETWEEN 1 AND 3 THEN 1 ELSE 0 END) AS top3
            FROM races r
            JOIN results res ON r.race_id = res.race_id
            JOIN horses h ON res.horse_id = h.horse_id
            WHERE r.surface = ? AND r.distance = ? AND r.venue = ?
              AND r.race_date >= ?
              AND res.finish_position > 0
              AND h.sire IS NOT NULL AND h.sire != ''
            GROUP BY h.sire
            HAVING n >= 4
            """,
            (surface, distance, venue, from_date),
        )
        rows = cur.fetchall()
        if not rows:
            return None
        # 複勝率 threshold% 超 かつ (race_id 指定時) 今週出走馬の父
        strong = [(sire, n, t) for sire, n, t in rows
                  if (100.0 * t / n) >= threshold
                  and (entry_sires is None or sire in entry_sires)]
        if not strong:
            return None
        total_n = sum(n for _, n, _ in strong)
        total_t = sum(t for _, _, t in strong)
        return (len(strong), total_t, total_n)
    except Exception:
        return None

def _lede_top_jockey(conn, venue, surface, distance, race_id=None, years=3, min_runs=5):
    """指定コースで複勝率 TOP の騎手 (jockey, pct, n, top3) を返す。

    race_id が与えられた場合は「**今週その race に出走する騎手**」のうち TOP を返す。
    出走馬の騎手と紐付かない汎用 TOP(モレイラ等)を lede に出して
    「今週騎乗してないのに?」となる事故を防ぐ。

    Returns:
        (jockey_name, pct, top3, n) or None
    """
    if not venue or not surface or not distance:
        return None
    try:
        cur = conn.cursor()
        from_date = f"{datetime.now().year - years}-01-01"

        # 今週レースの出走馬の騎手一覧を取得 (race_id が与えられた場合のみ)
        entry_jockeys = None
        if race_id:
            rows = cur.execute(
                """
                SELECT DISTINCT j.jockey_name
                FROM results res
                JOIN jockeys j ON res.jockey_id = j.jockey_id
                WHERE res.race_id = ? AND j.jockey_name IS NOT NULL AND j.jockey_name != ''
                """,
                (race_id,),
            ).fetchall()
            entry_jockeys = {r[0] for r in rows if r[0]}
            if not entry_jockeys:
                # 出走馬の騎手が未確定 → 汎用 TOP は誤誘導なので lede 出さない
                return None

        # コース内の騎手別複勝率 (上位を多めに取って出走騎手と交差)
        cur.execute(
            """
            SELECT j.jockey_name, COUNT(*) AS n,
                   SUM(CASE WHEN res.finish_position BETWEEN 1 AND 3 THEN 1 ELSE 0 END) AS top3
            FROM races r
            JOIN results res ON r.race_id = res.race_id
            JOIN jockeys j ON res.jockey_id = j.jockey_id
            WHERE r.surface = ? AND r.distance = ? AND r.venue = ?
              AND r.race_date >= ?
              AND res.finish_position > 0
              AND j.jockey_name IS NOT NULL
            GROUP BY j.jockey_name
            HAVING n >= ?
            ORDER BY (1.0 * top3 / n) DESC, n DESC
            LIMIT 30
            """,
            (surface, distance, venue, from_date, min_runs),
        )
        rows = cur.fetchall()
        if not rows:
            return None
        # entry_jockeys と交差 (race_id 指定時のみ)
        for jockey, n, top3 in rows:
            if entry_jockeys is not None and jockey not in entry_jockeys:
                continue
            if not jockey or n < min_runs:
                continue
            pct = 100.0 * top3 / n
            return (jockey, pct, top3, n)
        return None
    except Exception:
        return None

def _lede_pattern_count(conn, venue, surface, distance, race_id=None, years=6, min_runs=5, threshold=55.0):
    """sec_pattern_discovery 相当: 複勝 threshold% 超のパターン数を返す。

    race_id 指定時は「今週その race の出走馬が父系として該当する」パターンのみ集計
    (出走馬がいない種牡馬のパターンを出して誤誘導しないため。CLAUDE.md 絶対ルール)

    Returns:
        (count, best_pct, best_sample_total_top3, best_sample_n) or None
    """
    if not venue or not surface or not distance:
        return None
    try:
        cur = conn.cursor()
        from_date = f"{datetime.now().year - years}-01-01"

        # 今週の出走馬の父 (race_id 指定時のみ)
        entry_sires = None
        if race_id:
            rows = cur.execute(
                """
                SELECT DISTINCT h.sire FROM results res
                JOIN horses h ON res.horse_id = h.horse_id
                WHERE res.race_id = ? AND h.sire IS NOT NULL AND h.sire != ''
                """,
                (race_id,),
            ).fetchall()
            entry_sires = {r[0] for r in rows if r[0]}
            if not entry_sires:
                return None  # 出走馬未確定 or 血統未登録 → lede 出さない

        cur.execute(
            """
            SELECT h.sire,
                   CASE
                       WHEN res.post_position BETWEEN 1 AND 3 THEN '内枠'
                       WHEN res.post_position BETWEEN 4 AND 5 THEN '中枠'
                       WHEN res.post_position BETWEEN 6 AND 8 THEN '外枠'
                       ELSE '大外'
                   END AS waku_band,
                   COUNT(*) AS n,
                   SUM(CASE WHEN res.finish_position BETWEEN 1 AND 3 THEN 1 ELSE 0 END) AS top3
            FROM races r
            JOIN results res ON r.race_id = res.race_id
            JOIN horses h ON res.horse_id = h.horse_id
            WHERE r.surface = ? AND r.distance = ? AND r.venue = ?
              AND r.race_date >= ?
              AND res.finish_position > 0
              AND h.sire IS NOT NULL AND h.sire != ''
              AND res.post_position > 0
            GROUP BY h.sire, waku_band
            HAVING n >= ? AND (1.0 * top3 / n) >= ?
            ORDER BY (1.0 * top3 / n) DESC, n DESC
            """,
            (surface, distance, venue, from_date, min_runs, threshold / 100.0),
        )
        rows = cur.fetchall()
        # 今週の出走馬の父にフィルタ (race_id 指定時)
        if entry_sires is not None:
            rows = [r for r in rows if r[0] in entry_sires]
        if not rows:
            return None
        cnt = len(rows)
        _, _, n0, t0 = rows[0]
        best_pct = 100.0 * t0 / n0
        return (cnt, best_pct, t0, n0)
    except Exception:
        return None

def _lede_outlier(conn, race_name, years=6):
    """過去 years 年で最高人気外 (5番人気以下) 勝利、または最高配当を返す。

    Returns:
        (year, horse_name, pop, odds, upset_count, sample_n) or None
    """
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT substr(r.race_date, 1, 4) AS year, h.horse_name,
                   res.popularity, res.odds
            FROM races r
            JOIN results res ON r.race_id = res.race_id AND res.finish_position = 1
            JOIN horses h ON res.horse_id = h.horse_id
            WHERE r.race_name = ?
            ORDER BY r.race_date DESC
            LIMIT ?
            """,
            (race_name, years),
        )
        rows = cur.fetchall()
        if len(rows) < 3:
            return None
        # 1) 人気外 (5番人気以下) 勝利を集計
        outliers = [(y, hn, p, o) for y, hn, p, o in rows if p and p >= 5]
        if outliers:
            # 最も人気薄の年 (or 最高オッズ年) を返す
            outliers.sort(key=lambda x: (-(x[2] or 0), -(x[3] or 0)))
            year, hn, pop, odds = outliers[0]
            return (year, hn, pop, odds, len(outliers), len(rows))
        return None
    except Exception:
        return None

def _lede_pace_decisive(conn, venue, surface, distance, years=6):
    """指定コースで上り3F最速馬の複勝率を返す。

    Returns:
        (pct, top3, n) or None
    """
    if not venue or not surface or not distance:
        return None
    try:
        cur = conn.cursor()
        from_date = f"{datetime.now().year - years}-01-01"
        cur.execute(
            """
            WITH fastest AS (
                SELECT r.race_id, MIN(res.last_3f) AS min_3f
                FROM races r
                JOIN results res ON r.race_id = res.race_id
                WHERE r.surface = ? AND r.distance = ? AND r.venue = ?
                  AND r.race_date >= ?
                  AND res.last_3f IS NOT NULL AND res.last_3f > 0
                GROUP BY r.race_id
            )
            SELECT COUNT(*) AS n,
                   SUM(CASE WHEN res.finish_position BETWEEN 1 AND 3 THEN 1 ELSE 0 END) AS top3
            FROM fastest f
            JOIN results res ON f.race_id = res.race_id AND res.last_3f = f.min_3f
            WHERE res.finish_position > 0
            """,
            (surface, distance, venue, from_date),
        )
        row = cur.fetchone()
        if not row or not row[0]:
            return None
        n, top3 = row
        if n < 10:  # サンプル少なすぎは煽れない
            return None
        pct = 100.0 * top3 / n
        return (pct, top3, n)
    except Exception:
        return None

def _build_morning_lede(dow, conn, race):
    """曜日別の強い見出し1行を返す。データ無ければ None。

    dow: 0=月 1=火 2=水 3=木 4=金 5=土 6=日

    fact_check 通過のため lede は必ず「率 + サンプル(N/M)」形式 or 着度数 (N-N-N-N) を含む。
    50字以下 (X 全角換算) を目安にする。
    """
    if conn is None or not isinstance(race, dict):
        return None
    race_name = race.get("race_name", "")
    venue = race.get("venue", "")
    surface = race.get("surface", "")
    try:
        distance = int(race.get("distance") or 0)
    except (TypeError, ValueError):
        distance = 0

    # 月: 1番人気着度数 (race_name 必須)
    if dow == 0:
        if not race_name:
            return None
        result = _lede_pop_chakudosu(conn, race_name, target_pop=1, years=6)
        if not result:
            return None
        w, p, s, o, total = result
        # 評価フレーズ: 勝率で判定
        if w == 0:
            tag = "未勝利の伏兵レース"
        elif w >= total * 0.5:
            tag = "鉄板の1人気"
        elif w >= 2:
            tag = "1人気健闘も鉄板ならず"
        else:
            tag = "飛ばないが勝ち切らない"
        return f"💥 過去{total}年・1番人気は ({w}-{p}-{s}-{o}) — {tag}"

    # 火: 種牡馬複勝50%超の数 (※ 今週出走馬の父にいる種牡馬のみ集計)
    if dow == 1:
        race_id = race.get("race_id")
        result = _lede_strong_sires_count(conn, venue, surface, distance, race_id=race_id, years=6, threshold=50.0)
        if not result:
            return None
        cnt, t, n = result
        return f"💥 出走馬の父に当コース複勝50%超 {cnt}系統 (うち{t}走複勝圏) — 血統が決め手"

    # 水: 騎手 TOP 複勝率 (※ 今週レースに出走する騎手のみ対象 = race_id を渡す)
    if dow == 2:
        race_id = race.get("race_id")
        result = _lede_top_jockey(conn, venue, surface, distance, race_id=race_id, years=3, min_runs=5)
        if not result:
            return None
        jockey, pct, top3, n = result
        course = f"{venue}{surface}{distance}m" if venue and surface and distance else "当コース"
        return f"💥 鞍上{jockey} {course}複勝{pct:.1f}% — 今週も騎乗"

    # 木: 4番人気着度数 (race_name 必須)
    if dow == 3:
        if not race_name:
            return None
        result = _lede_pop_chakudosu(conn, race_name, target_pop=4, years=6)
        if not result:
            return None
        w, p, s, o, total = result
        if w >= 2:
            tag = "伏兵の主役"
        elif w + p + s >= total * 0.5:
            tag = "ヒモには必須"
        elif w + p + s == 0:
            tag = "完全な脇役"
        else:
            tag = "侮れない一頭"
        return f"💥 過去{total}年・4番人気は ({w}-{p}-{s}-{o}) — {tag}"

    # 金: パターン発掘数 (※ 今週出走馬の父が該当するパターンのみ集計)
    if dow == 4:
        race_id = race.get("race_id")
        result = _lede_pattern_count(conn, venue, surface, distance, race_id=race_id, years=6, threshold=55.0)
        if not result:
            return None
        cnt, best_pct, t0, n0 = result
        return f"💥 出走馬該当パターン{cnt}件 (最高{best_pct:.0f}%)"

    # 土: 過去最高配当 or 大波乱年 (race_name 必須)
    if dow == 5:
        if not race_name:
            return None
        result = _lede_outlier(conn, race_name, years=6)
        if not result:
            return None
        year, hn, pop, odds, upset_cnt, sample_n = result
        return f"💥 {year}年{pop}人気{hn}({odds:.1f}倍)勝利 — 過去{sample_n}年で{upset_cnt}度の波乱"

    # 日: 末脚最速馬複勝率
    if dow == 6:
        result = _lede_pace_decisive(conn, venue, surface, distance, years=6)
        if not result:
            return None
        pct, top3, n = result
        if pct >= 70:
            tag = "上がり最速がほぼ着順"
        elif pct >= 50:
            tag = "末脚優位コース"
        else:
            tag = "前残り注意"
        return f"💥 上がり3F最速馬の複勝率 {pct:.0f}% — {tag}"

    return None


# X 文字数カウント (日本語/絵文字 = 2, ASCII = 1)

# ─────────────────────────────────────────────────────────
def _clean_sire_match(line: str) -> str:
    """sec_sire_course_cross の行から fact_check NG な「該当馬なし」を除去し、
    「該当: X Y」は「→ X他N頭」に圧縮。率%(N/M) は残すので単独 tweet 安全。"""
    m = re.match(
        r"^(🥇|🥈|🥉|🏅)([^\s]+)\s+(\d+%)\s+\(\d+/\d+\)\s+→\s+該当:\s+(.+)$", line)
    if m:
        medal, sire, pct, horses = m.groups()
        names = horses.strip().split()
        disp = names[0] if len(names) == 1 else f"{names[0]}他{len(names)-1}頭"
        return f"{medal}{sire}産駒({pct}) → {disp}"
    # 「→ 該当馬なし」を落として率データのみ残す
    return re.sub(r"\s*→\s*該当馬なし$", "", line)


def _morning_sec_notable(conn, ctx, mode):
    """今週の出走馬から「注目馬」を実名+データで抽出 (#53 中身ゼロ撲滅の主軸)。

    枠順抽選 (金11時) 前は馬番を伏せて馬名のみ表示する (JRA ルール遵守)。
    出走馬さえ確定していれば常に実名+数値根拠が出るので、朝投稿が
    「歴代勝ち馬」「コース統計」だけの抽象的な内容になるのを防ぐ。
    """
    drawn = _is_post_position_drawn(
        {"race_date": ctx.get("race_date", "")}, ctx.get("today"))
    title, lines, n = sec_notable_horses(
        conn, ctx["race_id"], ctx["venue"], ctx["surface"], ctx["distance"],
        mode=mode, top=3, show_number=drawn)
    if not (lines and n > 0):
        return None, n
    return _make_section(title, lines), n


def _morning_sec_notable_combined(conn, ctx):
    return _morning_sec_notable(conn, ctx, "combined")


def _morning_sec_notable_blood(conn, ctx):
    return _morning_sec_notable(conn, ctx, "blood")


def _morning_sec_notable_jockey(conn, ctx):
    return _morning_sec_notable(conn, ctx, "jockey")


def _notable_lead(conn, race, mode="combined", today=None):
    """昼/夜ビルダー共通: 今週の出走馬から注目馬セクション文字列を返す (#53)。

    枠順抽選前は馬番を伏せる。出走馬さえ確定していれば実名+数値根拠が出るので、
    どの slot も「具体的な馬名ゼロ」を構造的に防ぐ。該当馬無しなら None。
    """
    drawn = _is_post_position_drawn(
        {"race_date": race.get("race_date", "")}, today)
    title, lines, n = sec_notable_horses(
        conn, race.get("race_id", ""), race.get("venue", ""),
        race.get("surface", ""), race.get("distance", 0),
        mode=mode, top=3, show_number=drawn)
    if lines and n > 0:
        return _make_section(title, lines)
    return None


def _morning_sec_historical(conn, ctx):
    _t, lines, n = sec_historical_winners(conn, ctx["race_name"], years=6)
    if lines and n > 0:
        return _make_section("【歴代勝ち馬】", lines[:3]), n
    return None, n


def _morning_sec_pop_trust(conn, ctx):
    _t, lines, n = sec_pop_trust_trend(
        conn, ctx["venue"], ctx["surface"], ctx["distance"],
        grade=ctx["grade"], years=6)
    if lines and n >= 3:
        relevant = [l for l in lines if "複勝率" in l or "→" in l]
        return _make_section("【1人気の信頼度】", relevant[:2]), n
    return None, n


def _morning_sec_sires_compact(conn, ctx):
    _t, lines, n = sec_sire_course_cross(
        conn, ctx["venue"], ctx["surface"], ctx["distance"],
        top=2, years=6, race_id=ctx["race_id"])
    if not (lines and n > 0):
        return None, n
    parts = []
    for l in lines[:2]:
        base = re.sub(r"\s*→\s*該当(馬なし|:.*)$", "", l)
        base = (base.replace("🥇", "").replace("🥈", "")
                .replace("🥉", "").replace("🏅", "").strip())
        if base:
            parts.append(base)
    if not parts:
        return None, n
    return f"【種牡馬】 {' / '.join(parts)}", n


def _morning_sec_sires_detailed(conn, ctx):
    _t, lines, n = sec_sire_course_cross(
        conn, ctx["venue"], ctx["surface"], ctx["distance"],
        top=3, years=6, race_id=ctx["race_id"])
    if not (lines and n > 0):
        return None, n
    cleaned = [_clean_sire_match(l) for l in lines[:3]]
    cleaned = [l for l in cleaned if l and "該当馬なし" not in l]
    if not cleaned:
        return None, n
    return _make_section("【コース実績ある種牡馬】", cleaned), n


def _morning_sec_pattern(conn, ctx):
    """金朝「AI独自パターン分析」用セクション。

    🆕 改修 (2026-06-04 ユーザー指摘「意味のわからない投稿」):
    旧版は「リアルスティール×内枠 75% → レーベンスティール」「同×外枠 71% → 同馬」
    のように同種牡馬・異枠の擬似独立パターンを別行で並べ、同じ馬を3行繰り返していた。
    これは統計的に「リアルスティールが強い」を切り口だけ変えた水増しで無意味。

    新版: 馬中心に集約。
      ✅レーベンスティール: 父リアルスティール×コース複勝65% (枠不問で好相性)
    1頭1行、最強根拠1つ。同種牡馬の異枠は集約して「枠不問」と明示。
    枠順抽選後は実枠を「(8枠該当)」のように付記。
    """
    import re as _re
    _t, lines, n = sec_pattern_discovery(
        conn, ctx["venue"], ctx["surface"], ctx["distance"],
        years=6, target_top3_pct=55.0, race_id=ctx["race_id"])
    if not (lines and n > 0):
        return None, n

    # 出走馬の枠順が抽選済か (post_position が1以上の馬があれば抽選済)
    draw_done = False
    try:
        if ctx.get("race_id"):
            cur = conn.execute(
                "SELECT MAX(post_position) FROM results WHERE race_id = ?",
                (ctx["race_id"],),
            ).fetchone()
            if cur and cur[0] and int(cur[0]) > 0:
                draw_done = True
    except Exception:
        pass

    # lines をパース → 馬中心に集約 (同一馬は最も複勝率の高いパターン1つだけ)
    # 想定 line 形式: "🔮 {sire}×{waku_label} 複勝{pct}% ({n}走中{top3}複) → 該当: {horse} [{horse}...]"
    horse_best = {}  # horse_name → (pct, sire, waku_label, top3, n_runs)
    for line in lines:
        m = _re.search(r"🔮\s*([^×]+)×([^ ]+)\s+複勝(\d+)%\s+\((\d+)/(\d+)\).*?該当:\s*(.+?)$", line)
        if not m:
            continue
        sire, waku, pct, t3, nr, horses = m.groups()
        try:
            pct_i, t3_i, nr_i = int(pct), int(t3), int(nr)
        except ValueError:
            continue
        for hn in horses.strip().split():
            prev = horse_best.get(hn)
            if prev is None or pct_i > prev[0]:
                horse_best[hn] = (pct_i, sire.strip(), waku.strip(), t3_i, nr_i)

    if not horse_best:
        return None, n

    # 複勝率順に上位3頭 (1馬1行)
    top_horses = sorted(horse_best.items(), key=lambda kv: -kv[1][0])[:3]

    # 圧縮表記: 3頭が1 tweet (280字以内) に収まるよう各行40字以下
    medals = ["🥇", "🥈", "🥉"]
    out_lines = []
    for i, (horse, (pct, sire, waku, t3, nr)) in enumerate(top_horses):
        m = medals[i] if i < len(medals) else "🏅"
        # 種牡馬名から外国表記を除去
        sire_clean = sire.split("(")[0].strip()
        # 末尾の英字 (例: "ドレフォンDrefong") も除去
        import re as _re_sire
        sire_clean = _re_sire.sub(r"[A-Za-z]+$", "", sire_clean).strip()
        waku_str = f" {waku}" if draw_done else ""
        out_lines.append(f"{m}{horse} {pct}% {sire_clean}{waku_str}")

    return _make_section("【父×コース複勝率TOP】", out_lines), len(out_lines)


def _morning_sec_age(conn, ctx):
    _t, lines, n = sec_age_pattern(
        conn, ctx["venue"], ctx["surface"], ctx["distance"],
        grade=ctx["grade"], years=6)
    if lines and n > 0:
        return _make_section("【馬齢別 複勝率】", lines[:3]), n
    return None, n


def _morning_sec_jockey(conn, ctx):
    _t, lines, n = sec_jockey_recent_form(
        conn, ctx["venue"], ctx["surface"], ctx["distance"],
        top=4, years=3, race_id=ctx["race_id"])
    if lines and n > 0:
        return _make_section("【コース好相性騎手】", lines[:4]), n
    return None, n


def _morning_sec_pace(conn, ctx):
    _t, lines, n = sec_pace_decisive(
        conn, ctx["venue"], ctx["surface"], ctx["distance"], years=6)
    if lines and n >= 10:
        return _make_section("【末脚の優位性】", lines[:3]), n
    return None, n


def _morning_sec_post_pos(conn, ctx):
    _t, lines, n = sec_post_position_bias(
        conn, ctx["venue"], ctx["surface"], ctx["distance"], years=6)
    if not (lines and len(lines) >= 2):
        return None, n
    best_m = re.search(r"⭐(\d+)枠 複勝率 ([\d.]+)%", lines[0])
    worst_m = re.search(r"⚠️(\d+)枠 複勝率 ([\d.]+)%", lines[1])
    if not (best_m and worst_m):
        return None, n
    bw, bp = best_m.groups()
    ww, wp = worst_m.groups()
    diff_pt = float(bp) - float(wp)
    block = [
        f"⭐{bw}枠 複勝率{bp}% (ベスト)",
        f"⚠️{ww}枠 複勝率{wp}% (ワースト)",
        f"→ {diff_pt:.0f}pt差、枠の有利不利は{'大' if diff_pt >= 10 else '小さめ'}",
    ]
    return _make_section("【枠順の有利不利】", block), n


def _morning_sec_dangerous(conn, ctx):
    _t, lines, n = sec_dangerous_favorites(
        conn, ctx["venue"], ctx["surface"], ctx["distance"],
        grade=ctx["grade"], years=6)
    if not (lines and n >= 3):
        return None, n
    flop_line = next((l for l in lines if "飛んだ" in l), None)
    if not flop_line:
        return None, n
    block = [flop_line]
    m = re.search(r"(\d+)%\s*\((\d+)/(\d+)\)", flop_line)
    if m:
        pct = int(m.group(1))
        if pct <= 15:
            block.append("→ 1番人気は信頼度高")
        elif pct <= 30:
            block.append("→ 1番人気の質を見極めること")
        else:
            block.append("→ 1番人気軸は危険、相手探し優先")
    example = next((l for l in lines if l.startswith("🚨")), None)
    if example:
        block.append(example)
    return _make_section("【1番人気の信頼性】", block), n


def _morning_sec_outlier(conn, ctx):
    _t, lines, n = sec_outlier_year(conn, ctx["race_name"], years=6)
    if not (lines and n > 0):
        return None, n
    outliers = [l for l in lines if l.startswith("🚨")]
    if not outliers:
        # 波乱事例ゼロ = 堅め基調。% も馬名も無い文だけだと単独 tweet で
        # fact_check に落ちるのでこのセクションは出さない (堅め情報は別 slot で扱う)。
        return None, n
    block = outliers[:2]
    rate_line = next((l for l in lines if "5番人気以下勝利" in l), None)
    if rate_line:
        block.append(rate_line.replace("→ ", ""))
    return _make_section("【過去の波乱年】", block), n


# 朝の曜日別ローテーション設定。
# 実運用では水/木/金朝は専用 builder (build_wed/thu/fri_morning_post) に
# ルーティングされる (post_x._resolve_slot_name) ため、build_morning_post が
# 実際に使われるのは月・火・土・日。下記は土日が隣接 / 月火が隣接するので
# その 4 日のセクション集合が重複しないよう設計 (= 同一テキスト tweet を作らない)。
# 水木金は専用 builder へのフォールバックなので独立に成立すれば良い。
_MORNING_ROTATION = {
    0: {  # 月: 週末ラインナップ (今週の注目馬TOP3 + 歴代 + 1人気)
        "emoji": "🏇", "theme": "今週の注目馬",
        "sections": [
            ("notable", _morning_sec_notable_combined),
            ("historical", _morning_sec_historical),
            ("pop_trust", _morning_sec_pop_trust),
        ],
        "cta": "→ 火朝は血統で狙える注目馬を配信🔔",
    },
    1: {  # 火: 血統で狙う注目馬 (CLAUDE.md 平日 slot 表)
        "emoji": "🩸", "theme": "血統で狙う注目馬",
        "sections": [
            ("notable", _morning_sec_notable_blood),
            ("pattern", _morning_sec_pattern),
            ("age", _morning_sec_age),
        ],
        "cta": "→ 水朝は鞍上で狙える注目馬を配信🔔",
    },
    2: {  # 水: 鞍上で狙う注目馬 (通常は build_wed_morning_post / これは fallback)
        "emoji": "🏇", "theme": "鞍上で狙う注目馬",
        "sections": [
            ("notable", _morning_sec_notable_jockey),
            ("jockey", _morning_sec_jockey),
            ("pace", _morning_sec_pace),
        ],
        "cta": "→ 木朝は枠順とコース適性を配信🔔",
    },
    3: {  # 木: コース適性の注目馬 + 枠順 (通常は build_thu_morning_post / fallback)
        "emoji": "📋", "theme": "コース適性の注目馬",
        "sections": [
            ("notable", _morning_sec_notable_combined),
            ("post_pos", _morning_sec_post_pos),
        ],
        "cta": "→ 金朝はAI独自パターンを配信🔔",
    },
    4: {  # 金: AI独自パターン分析 (通常は build_fri_morning_post / fallback)
        "emoji": "🔮", "theme": "AI独自パターン分析",
        "sections": [
            ("pattern", _morning_sec_pattern),
            ("notable", _morning_sec_notable_blood),
        ],
        "cta": "→ 土朝は本番直前データを配信🔔",
    },
    5: {  # 土: 本番直前データ (1番人気の信頼性 + 枠順)
        "emoji": "🎯", "theme": "本番直前データ",
        "sections": [
            ("danger", _morning_sec_dangerous),
            ("post_pos", _morning_sec_post_pos),
        ],
        "cta": "→ 本日10時にAI最終予想+買い目を発表🔔",
    },
    6: {  # 日: 波乱とコース傾向 (波乱年 + 種牡馬 + 末脚)
        "emoji": "⚠️", "theme": "波乱とコース傾向",
        "sections": [
            ("outliers", _morning_sec_outlier),
            ("sires", _morning_sec_sires_detailed),
            ("pace", _morning_sec_pace),
        ],
        "cta": "→ 本日10時にAI最終予想+買い目を発表🔔",
    },
}


def build_morning_post(race: dict, conn, today: Optional[datetime] = None) -> Tuple[list, dict]:
    """平日朝 (7:30): 今週末メインレースの「曜日別」データ tweet。

    #48 根本対策: 曜日ごとに切り口 (セクション集合) を変えて毎日違う
    コンテンツにし、X の duplicate content (403) を構造的に防ぐ。

    Args:
        race: レース dict (race_name/venue/surface/distance/grade/race_id/race_date)
        conn: sqlite3 Connection
        today: 投稿日時 (JST)。省略時は現在 JST。曜日別ローテの選択 + header の
               日付表示に使う。テスト時に曜日を変えて呼ぶために注入可能。
    """
    if today is None:
        import datetime as _dt
        today = _dt.datetime.utcnow() + _dt.timedelta(hours=9)
    dow = today.weekday()
    cfg = _MORNING_ROTATION.get(dow, _MORNING_ROTATION[0])

    ctx = {
        "venue": race.get("venue", ""),
        "surface": race.get("surface", ""),
        "distance": race.get("distance", 0),
        "race_id": race.get("race_id", ""),
        "grade": race.get("grade"),
        "race_name": race.get("race_name", ""),
        "race_date": race.get("race_date", ""),
        "today": today,
    }

    label = _race_label(race)
    day = _day_phrase(race)
    dow_label = ["月", "火", "水", "木", "金", "土", "日"][dow]
    # header: 曜日テーマ + 投稿日 で tweet1 を必ず日替わりにする (#48 belt-and-suspenders)
    header = (f"{cfg['emoji']} {day} {label}\n"
              f"📅{today.month}/{today.day}({dow_label}) {cfg['theme']}")

    # 🆕 強い見出し(lede)を header 直下に追加 (曜日別フック角度で「同じ投稿に見える」を解消)
    # 🆕 #53 (2026-06-08): 先頭が「注目馬 (notable)」セクションの日は lede を省く。
    # 理由: lede は種牡馬/人気のコース統計フックだが、notable セクションが
    # 「実名 + 数値根拠」でより強い内容を持つため lede は冗長。さらに lede が
    # 文字数を食うと _split_to_thread が notable の馬名行を末尾から削り、見出しだけ
    # 残す事故 (火曜の【血統で狙う注目馬】が空) が起きていた。注目馬を主役にする。
    lead_is_notable = cfg["sections"] and cfg["sections"][0][0] == "notable"
    if not lead_is_notable:
        lede = _build_morning_lede(dow, conn, race)
        if lede:
            header += f"\n\n{lede}"

    samples = {}
    sections = []
    used = []
    for key, fn in cfg["sections"]:
        try:
            sec, n = fn(conn, ctx)
        except Exception as e:
            print(f"⚠️ morning section {key} エラー: {e}")
            sec, n = None, 0
        samples[key] = n
        if sec:
            sections.append(sec)
            used.append(key)

    # 安全網: 当日のセクションが全滅 (極端に薄いコース/障害戦等) した場合、
    # ほぼ常に取れる順でフォールバックして「投稿ゼロ」を避ける。先頭 section は
    # 必ず日替わり header と同じ tweet に入るので、隣接日と同一 section になっても
    # tweet 全文は重複しない (#48 の duplicate 回避は header で担保)。
    if not sections:
        for key, fn in (("historical", _morning_sec_historical),
                        ("pop_trust", _morning_sec_pop_trust),
                        ("sires", _morning_sec_sires_compact),
                        ("pace", _morning_sec_pace),
                        ("post_pos", _morning_sec_post_pos)):
            try:
                sec, n = fn(conn, ctx)
            except Exception:
                sec, n = None, 0
            if sec:
                sections.append(sec)
                used.append(f"fallback:{key}")
                samples[f"fallback:{key}"] = n
                break

    # 🆕 (#50): フォールバックでも 1 セクションも埋まらない極薄レースは、
    # header+CTA だけの中身ゼロ投稿 (「📅週末ラインナップ」だけ等) を出さずにスキップする。
    # morning header のテーマ語は fact_check の no_horse_ok に当たり素通りしてしまうため、
    # ここで明示的に return None して空投稿を構造的に防ぐ。
    if not [s for s in sections if s]:
        return None, {"sections_used": [], "samples": samples,
                      "weekday": dow, "skipped": "no_content"}

    hashtags = _hashtags(race)
    tweets = _split_to_thread(header, [s for s in sections if s], cfg["cta"], hashtags)

    char_count = sum(_x_len(t) for t in tweets)
    return tweets, {"sections_used": used, "samples": samples,
                    "char_count": char_count, "weekday": dow}


# ─────────────────────────────────────────────────────────
# 月昼: コース傾向 (種牡馬×枠×異常年)
# ─────────────────────────────────────────────────────────
def build_weekday_post(race: dict, conn) -> Tuple[str, dict]:
    """月昼 (12:30): レースのコース傾向 (種牡馬/枠/異常年)"""
    samples = {}
    sections = []

    label = _race_label(race)
    day = _day_phrase(race)
    venue = race.get("venue", "")
    surface = race.get("surface", "")
    distance = race.get("distance", 0)
    race_id = race.get("race_id", "")
    race_name = race.get("race_name", "")

    header = f"🔍 {day} {label}\n今週の注目馬とコース傾向"
    import re as _re

    # 🆕 #53: 先頭に「今週の注目馬」(実名+データ) を必ず置く
    nb = _notable_lead(conn, race, mode="combined")
    if nb:
        sections.append(nb)

    # 🆕 #78: 母父まで掘る「血統の深層」(出走確定後のみ)。父だけの浅い投稿への対応。
    # 馬番は枠順抽選 (金11時) 後のみ表示 (#27)。
    pt, plines, pn = sec_pedigree_deep(
        conn, race_id, venue, surface, distance, top=3, years=6,
        show_number=_is_post_position_drawn(race))
    if pn > 0 and len(plines) >= 2:  # 父+母父データが取れた時だけ
        sections.append(_make_section(pt, plines))

    # Section 1: 鉄板系統の該当馬 (該当馬なし非表示、複数馬は「他N頭」に圧縮)
    t1, lines1, n1 = sec_sire_course_cross(
        conn, venue, surface, distance, top=3, years=6, race_id=race_id)
    samples["sires"] = n1
    if lines1 and n1 > 0:
        filtered = [l for l in lines1 if "該当馬なし" not in l]
        cleaned = []
        for l in filtered:
            # "🥈フィエールマン 50% (4/8) → 該当: ○○ ××" → "🥈フィエールマン産駒 → ○○他1頭"
            m = _re.match(r"^(🥇|🥈|🥉|🏅)([^\s]+)\s+(\d+%)\s+\(\d+/\d+\)\s+→\s+該当:\s+(.+)$", l)
            if m:
                medal, sire, pct, horses_str = m.groups()
                names = horses_str.strip().split()
                if len(names) == 1:
                    horses_display = names[0]
                else:
                    horses_display = f"{names[0]}他{len(names)-1}頭"
                cleaned.append(f"{medal}{sire}産駒 → {horses_display}")
            else:
                cleaned.append(l)
        if cleaned:
            # タイトル最小限化
            sections.append(_make_section(
                "【血統】複勝50%超 (過去6年)",
                cleaned[:2]
            ))

    # Section 2 + 3 統合: 枠順 + 波乱年 を 1行ずつコンパクトに
    extras = []

    t2, lines2, n2 = sec_post_position_bias(conn, venue, surface, distance, years=6)
    samples["post_pos"] = n2
    if lines2 and len(lines2) >= 2:
        best_m = _re.search(r"⭐(\d+)枠 複勝率 ([\d.]+)%", lines2[0])
        worst_m = _re.search(r"⚠️(\d+)枠 複勝率 ([\d.]+)%", lines2[1])
        if best_m and worst_m:
            bw, bp = best_m.groups()
            ww, wp = worst_m.groups()
            diff_pt = float(bp) - float(wp)
            extras.append(f"枠順: ⭐{bw}枠{bp}% / ⚠️{ww}枠{wp}% ({diff_pt:.0f}pt差)")

    t3, lines3, n3 = sec_outlier_year(conn, race_name, years=6)
    samples["outliers"] = n3
    if lines3 and n3 > 0 and n3 >= 3:
        # 「N年で5番人気以下勝利: X回」から堅め/荒れ型を判定
        rate_line = next((l for l in lines3 if "5番人気以下勝利" in l), None)
        outlier_line = next((l for l in lines3 if l.startswith("🚨")), None)
        upset_cnt = 0
        if rate_line:
            m = _re.search(r"5番人気以下勝利:\s*(\d+)回", rate_line)
            if m: upset_cnt = int(m.group(1))

        upset_rate = upset_cnt / n3 if n3 > 0 else 0
        if upset_rate <= 0.25:  # 25%以下 = 堅め基調
            normal_cnt = n3 - upset_cnt
            # 例外馬を併記 (なら 2024のみ大波乱 を強調)
            if upset_cnt >= 1 and outlier_line:
                pop_m = _re.search(r"\((\d+)人気", outlier_line)
                year_m = _re.match(r"🚨(\d{4})", outlier_line)
                if pop_m and year_m:
                    extras.append(
                        f"傾向: 堅め (4人気以内が過去{n3}年で{normal_cnt}勝) ※例外{year_m.group(1)}({pop_m.group(1)}人気)"
                    )
                else:
                    extras.append(f"傾向: 堅め基調 (5番人気以下の勝利は過去{n3}年で{upset_cnt}回のみ)")
            else:
                extras.append(f"傾向: 完全堅め (5番人気以下勝利ゼロ)")
        elif upset_rate >= 0.5:  # 50%以上 = 荒れ型
            extras.append(f"傾向: 荒れ型 (過去{n3}年で{upset_cnt}回が5番人気以下勝利) → 1人気軸は危険")
        else:
            extras.append(f"傾向: 中庸 (5番人気以下勝利 過去{n3}年で{upset_cnt}回)")

    # extras を別々 sections として append (Phase 2 で 1個ずつ trim 可能)
    # 優先順: 枠順 > 波乱 (枠順は当週の予測に直結、波乱は文脈情報)
    for e in extras:
        sections.append(e)

    cta = "→ 今夜AI注目要素🔔"

    hashtags = _hashtags(race)
    tweets = _split_to_thread(header, [s for s in sections if s], cta, hashtags)

    char_count = sum(_x_len(t) for t in tweets)
    return tweets, {"sections_used": ["sires", "post_pos", "outliers"], "samples": samples, "char_count": char_count}


# ─────────────────────────────────────────────────────────
# 月夜: 注目要素 (末脚×異常年×危険な人気馬)
# ─────────────────────────────────────────────────────────
def build_evening_post(race: dict, conn) -> Tuple[str, dict]:
    """月夜 (20:00): レースの注目要素 (末脚決着型か / 危険な1人気か)"""
    samples = {}
    sections = []

    label = _race_label(race)
    day = _day_phrase(race)
    venue = race.get("venue", "")
    surface = race.get("surface", "")
    distance = race.get("distance", 0)
    grade = race.get("grade")

    header = f"🌙 {day} {label}\n今週の注目馬と傾向"
    import re as _re

    # 🆕 #53: 先頭に「今週の注目馬」(実名+データ) を必ず置く
    nb = _notable_lead(conn, race, mode="combined")
    if nb:
        sections.append(nb)

    # Section 1: 末脚 (1行 + 含意)
    t1, lines1, n1 = sec_pace_decisive(conn, venue, surface, distance, years=6)
    samples["pace"] = n1
    if lines1 and n1 >= 10:
        complot_line = next((l for l in lines1 if "複勝率" in l), None)
        if complot_line:
            pct_m = _re.search(r"複勝率\s*(\d+)%", complot_line)
            if pct_m and int(pct_m.group(1)) >= 70:
                sections.append(f"{complot_line.strip()}\n→ 上がり順位がほぼ着順に直結")
            else:
                sections.append(complot_line)

    # Section 2: 1人気の信頼性 — 飛び率 + 含意 (堅め/危険 判定付き)
    t2, lines2, n2 = sec_dangerous_favorites(
        conn, venue, surface, distance, grade=grade, years=6
    )
    samples["danger"] = n2
    if lines2 and n2 >= 3:
        # 飛び率を抽出して堅め/危険を判定
        flop_line = next((l for l in lines2 if "飛んだ" in l), None)
        if flop_line:
            flop_m = _re.search(r"(\d+)%\s*\((\d+)/(\d+)\)", flop_line)
            if flop_m:
                flop_pct = int(flop_m.group(1))
                flop_cnt = int(flop_m.group(2))
                total_n = int(flop_m.group(3))
                hit_pct = 100 - flop_pct  # 4着以内に来た割合
                if flop_pct <= 20:
                    insight = f"✅複勝圏 {hit_pct}% ({total_n-flop_cnt}/{total_n}) → 1人気は信頼可"
                elif flop_pct <= 30:
                    insight = f"⚠️5着以下 {flop_pct}% ({total_n}走中{flop_cnt}回) → 1人気の質を見極めること"
                else:
                    insight = f"🚨5着以下 {flop_pct}% ({total_n}走中{flop_cnt}回) → 1人気軸は危険、相手探し"
                sections.append(f"【1人気の信頼性】\n{insight}")

    cta = "→ 火朝に血統深掘り🔔"

    hashtags = _hashtags(race)
    tweets = _split_to_thread(header, [s for s in sections if s], cta, hashtags)

    char_count = sum(_x_len(t) for t in tweets)
    return tweets, {"sections_used": ["pace", "danger"], "samples": samples, "char_count": char_count}


# ─────────────────────────────────────────────────────────
# 火夜: 前走パターン + ローテーション
# ─────────────────────────────────────────────────────────
def build_tue_evening_post(race: dict, conn) -> Tuple[str, dict]:
    """火夜: レース詳細 (注目馬 + 前走パターン + ローテ)"""
    samples = {}
    sections = []

    label = _race_label(race)
    day = _day_phrase(race)
    race_name = race.get("race_name", "")
    race_id = race.get("race_id", "")

    header = f"📊 {day} {label}\n注目馬と勝ち馬の傾向"

    # 🆕 #53: 先頭に「今週の注目馬」(実名+データ) を必ず置く
    nb = _notable_lead(conn, race, mode="combined")
    if nb:
        sections.append(nb)

    # Section 1: 前走パターン (新フォーマット = 🎯 + 該当出走馬 を含む)
    t1, lines1, n1 = sec_prev_race_pattern(conn, race_name, years=6, race_id=race_id)
    samples["prev_race"] = n1
    if lines1 and n1 > 0:
        # 出走馬詳細 detail (「20XX 馬名 ← レース」形式) は冗長なのでカット
        # ただし「→ 該当出走馬」は超重要なので保持
        keep_lines = [
            l for l in lines1
            if not (l[:4].isdigit() and "←" in l)
        ]
        sections.append(_make_section("【勝ち馬の前走】", keep_lines[:6]))

    # Section 2: ローテーション
    t2, lines2, n2 = sec_rotation_pattern(conn, race_name, years=6)
    samples["rotation"] = n2
    if lines2 and n2 > 0:
        sections.append(_make_section("【ローテ傾向】", lines2[:3]))

    cta = "→ 木朝に出走確定+血統を配信🔔"

    hashtags = _hashtags(race)
    tweets = _split_to_thread(header, [s for s in sections if s], cta, hashtags)

    char_count = sum(_x_len(t) for t in tweets)
    return tweets, {"sections_used": ["prev_race", "rotation"], "samples": samples, "char_count": char_count}


# ─────────────────────────────────────────────────────────
# 水朝: 騎手×コース + 末脚
# ─────────────────────────────────────────────────────────
def build_wed_morning_post(race: dict, conn) -> Tuple[str, dict]:
    """水朝: 騎手×コース傾向"""
    samples = {}
    sections = []

    label = _race_label(race)
    day = _day_phrase(race)
    venue = race.get("venue", "")
    surface = race.get("surface", "")
    distance = race.get("distance", 0)
    race_id = race.get("race_id", "")

    # 🆕 (#50): ヘッダーのテーマは採用セクションに合わせて動的化。
    # 騎手データの薄いコース (例: 京都芝3200m) では jockey/pace 両方が空になり、
    # 旧コードは「🏇騎手×コースの傾向」+ CTA だけの中身ゼロ投稿を出していた。
    theme = None

    # 🆕 #53: 先頭に「鞍上で狙う注目馬」(実名+データ) を必ず置く
    nb = _notable_lead(conn, race, mode="jockey")
    if nb:
        sections.append(nb)
        theme = "鞍上で狙う注目馬"

    # Section 1: 騎手TOP
    t1, lines1, n1 = sec_jockey_recent_form(
        conn, venue, surface, distance, top=4, years=3, race_id=race_id)
    samples["jockey"] = n1
    if lines1 and n1 > 0:
        sections.append(_make_section("【コース好相性騎手】", lines1[:4]))
        theme = "騎手×コースの傾向"

    # Section 2: 末脚
    t2, lines2, n2 = sec_pace_decisive(conn, venue, surface, distance, years=6)
    samples["pace"] = n2
    if lines2 and n2 >= 10:
        sections.append(_make_section("【末脚優位性】", lines2[:2]))
        if theme is None:
            theme = "末脚の優位性"

    # フォールバック: 騎手も末脚も薄いコース → 歴代勝ち馬 → コース実績種牡馬 で埋める (#50)。
    if not [s for s in sections if s]:
        th, lh, nh = sec_historical_winners(conn, race.get("race_name", ""), years=6)
        if lh and nh > 0:
            sections.append(_make_section("【歴代勝ち馬】", lh[:3]))
            theme = "歴代勝ち馬とコース傾向"
        else:
            ts, ls, ns = sec_sire_course_cross(
                conn, venue, surface, distance, top=3, years=6, race_id=race_id)
            cleaned = [_clean_sire_match(l) for l in (ls or [])[:3]]
            cleaned = [l for l in cleaned if l and "該当馬なし" not in l]
            if cleaned and ns > 0:
                sections.append(_make_section("【コース実績ある種牡馬】", cleaned))
                theme = "コース実績ある種牡馬"

    # それでも空なら投稿スキップ (空投稿禁止)
    if not [s for s in sections if s]:
        return None, {"sections_used": [], "samples": samples, "skipped": "no_content"}

    header = f"🏇 {day} {label}\n{theme}"
    # 🆕 強い見出し (水朝固定 dow=2)
    lede = _build_morning_lede(2, conn, race)
    if lede:
        header += f"\n\n{lede}"
    cta = "→ 今夜は危険な1人気を配信🔔"

    hashtags = _hashtags(race)
    tweets = _split_to_thread(header, [s for s in sections if s], cta, hashtags)

    char_count = sum(_x_len(t) for t in tweets)
    return tweets, {"sections_used": ["jockey", "pace"], "samples": samples, "char_count": char_count}


# ─────────────────────────────────────────────────────────
# 水夜: 危険な人気馬
# ─────────────────────────────────────────────────────────
def build_wed_evening_post(race: dict, conn) -> Tuple[str, dict]:
    """水夜: 危険な人気馬深掘り"""
    samples = {}
    sections = []

    label = _race_label(race)
    day = _day_phrase(race)
    venue = race.get("venue", "")
    surface = race.get("surface", "")
    distance = race.get("distance", 0)
    grade = race.get("grade")
    race_name = race.get("race_name", "")

    header = f"⚠️ {day} {label}\n注目馬と人気馬の落とし穴"

    # 🆕 #53: 先頭に「今週の注目馬」(実名+データ) を必ず置く
    nb = _notable_lead(conn, race, mode="combined")
    if nb:
        sections.append(nb)
    import re as _re

    # Section 1: 1人気 + 2-3人気 で 5着以下になった事例 (具体的馬名で警鐘)
    t1, lines1, n1 = sec_dangerous_favorites(
        conn, venue, surface, distance, grade=grade, years=6
    )
    samples["danger"] = n1
    if lines1 and n1 >= 3:
        # 飛び率 + 例外事例 + 結論
        flop_line = next((l for l in lines1 if "飛んだ" in l), None)
        examples = [l for l in lines1 if l.startswith("🚨")][:2]
        block = []
        if flop_line:
            block.append(flop_line)
        # 含意 (堅め or 警戒)
        if flop_line:
            m = _re.search(r"(\d+)%\s*\((\d+)/(\d+)\)", flop_line)
            if m:
                pct = int(m.group(1))
                if pct <= 20:
                    block.append(f"→ 1人気は基本信頼可、例外時のみ警戒")
                elif pct <= 30:
                    block.append(f"→ 1人気は飛び事例の特徴に注意")
                else:
                    block.append(f"→ 1人気軸は危険、相手探し優先")
        block.extend(examples)
        sections.append(_make_section("【1人気の信頼性 (過去6年)】", block))

    # Section 2: 異常年
    t2, lines2, n2 = sec_outlier_year(conn, race_name, years=6)
    samples["outliers"] = n2
    if lines2 and n2 > 0:
        outliers = [l for l in lines2 if l.startswith("🚨")]
        if outliers:
            sections.append(_make_section("【過去の波乱事例】", outliers[:2]))

    cta = "→ 木朝に出走確定+血統を配信🔔"

    hashtags = _hashtags(race)
    tweets = _split_to_thread(header, [s for s in sections if s], cta, hashtags)

    char_count = sum(_x_len(t) for t in tweets)
    return tweets, {"sections_used": ["danger", "outliers"], "samples": samples, "char_count": char_count}


# ─────────────────────────────────────────────────────────
# 金朝: AI独自パターン発掘
# ─────────────────────────────────────────────────────────
def build_fri_morning_post(race: dict, conn) -> Tuple[str, dict]:
    """金朝: AI独自パターン分析 (種牡馬×枠等の高複勝パターン)"""
    samples = {}
    sections = []

    label = _race_label(race)
    day = _day_phrase(race)
    venue = race.get("venue", "")
    surface = race.get("surface", "")
    distance = race.get("distance", 0)
    race_id = race.get("race_id", "")

    # 🆕 (#50): ヘッダーのテーマは採用セクションに合わせて動的化。
    # パターンが取れた時のみ「AI独自パターン分析」、種牡馬のみの時は「コース実績ある血統」、
    # 両方空 (薄いコース) なら歴代/コース傾向にフォールバックし、それも無ければスキップ。
    theme = None

    # 🆕 #53: 先頭に「血統で狙う注目馬」(実名+データ) を必ず置く
    nb = _notable_lead(conn, race, mode="blood")
    if nb:
        sections.append(nb)
        theme = "血統で狙う注目馬"

    # Section 1: パターン発掘 → 馬中心に集約 (#53: 同種牡馬の異枠を別行で並べる水増しを廃止)
    sec1, n1 = _morning_sec_pattern(conn, {
        "venue": venue, "surface": surface, "distance": distance, "race_id": race_id,
    })
    samples["patterns"] = n1
    horses_in_sec1 = set()
    if sec1:
        sections.append(sec1)
        theme = "AI独自パターン分析"
        # Section 1 に含まれる馬を抽出 (新フォーマット: "🥇ウォーターリヒト ★67% (父..)")
        import re as _re
        for line in sec1.split("\n"):
            m = _re.match(r"^(?:🥇|🥈|🥉|🏅)([^ ★]+)", line)
            if m:
                horses_in_sec1.add(m.group(1).strip())

    # Section 2: 種牡馬 TOP (補助) — Section 1 で既出の馬は除外 (重複防止)
    t2, lines2, n2 = sec_sire_course_cross(
        conn, venue, surface, distance, top=3, years=6, race_id=race_id)
    samples["sires"] = n2
    if lines2 and n2 > 0:
        filtered = [l for l in lines2 if "該当馬なし" not in l]
        if filtered:
            import re as _re2
            cleaned = []
            for l in filtered[:3]:
                m = _re2.match(r"^(🥇|🥈|🥉|🏅)([^\s]+)\s+(\d+%)\s+\(\d+/\d+\)\s+→\s+該当:\s+(.+)$", l)
                if not m:
                    continue
                medal, sire, pct, horses = m.groups()
                names = horses.strip().split()
                # Section 1 既出馬を除外
                fresh = [n for n in names if n not in horses_in_sec1]
                if not fresh:
                    continue
                horses_disp = fresh[0] if len(fresh) == 1 else f"{fresh[0]}他{len(fresh)-1}頭"
                cleaned.append(f"{medal}{sire}産駒({pct}) → {horses_disp}")
                if len(cleaned) >= 2:
                    break
            if cleaned:
                sections.append(_make_section("【他に注目: コース実績ある血統】", cleaned))
                if theme is None:
                    theme = "コース実績ある血統"

    # フォールバック: パターンも種牡馬も薄い → 歴代勝ち馬で埋める (#50)。
    if not [s for s in sections if s]:
        th, lh, nh = sec_historical_winners(conn, race.get("race_name", ""), years=6)
        if lh and nh > 0:
            sections.append(_make_section("【歴代勝ち馬】", lh[:3]))
            theme = "歴代勝ち馬とコース傾向"

    # それでも空なら投稿スキップ (空投稿禁止)
    if not [s for s in sections if s]:
        return None, {"sections_used": [], "samples": samples, "skipped": "no_content"}

    header = f"🔮 {day} {label}\n{theme}"
    # 🆕 強い見出し (金朝固定 dow=4)
    lede = _build_morning_lede(4, conn, race)
    if lede:
        header += f"\n\n{lede}"
    cta = "→ 今夜は翌朝の確定予想告知🔔"

    hashtags = _hashtags(race)
    tweets = _split_to_thread(header, [s for s in sections if s], cta, hashtags)

    char_count = sum(_x_len(t) for t in tweets)
    return tweets, {"sections_used": ["patterns", "sires"], "samples": samples, "char_count": char_count}


# ─────────────────────────────────────────────────────────
# 水昼: 追い切り情報
# ─────────────────────────────────────────────────────────
def build_wed_weekday_post(race: dict, conn) -> Tuple[str, dict]:
    """水昼: 追い切り高評価馬リスト"""
    samples = {}
    sections = []

    label = _race_label(race)
    day = _day_phrase(race)
    venue = race.get("venue", "")
    surface = race.get("surface", "")
    distance = race.get("distance", 0)
    race_id = race.get("race_id", "")

    # Section 1: 追い切り A/B 評価馬 (馬番ガード適用)
    # 🆕 (#50): ヘッダーは「中身」に合わせて動的に決める。追い切りデータが無い未来レースで
    # 「🏋️追い切り評価」と書きながらコース適性をフォールバック表示する「タイトル詐欺」を防ぐ。
    t1, lines1, n1 = sec_workout_focus(conn, race_id, top=4)
    samples["workout"] = n1
    if lines1 and n1 > 0:
        header = f"🏋️ {day} {label}\n追い切り評価"
        cleaned = _strip_horse_number_from_lines(lines1, race)
        sections.append(_make_section(t1, cleaned[:6]))
    else:
        # 追い切り未公開 (未来レースは直前まで非公開) → コース適性の handpicked TOP3。
        # ヘッダーも実内容に合わせ「追い切り」とは書かない (タイトルと中身を一致させる)。
        header = f"🏇 {day} {label}\nコース適性 注目馬TOP"
        t_alt, lines_alt, n_alt = sec_handpicked_top(
            conn, race_id, venue, surface, distance, top=3, years=6
        )
        if lines_alt and n_alt > 0:
            cleaned = _strip_horse_number_from_lines(lines_alt, race)
            sections.append(_make_section(t_alt, cleaned[:4]))
        else:
            # コース適性も取れない極薄レース → 歴代勝ち馬で埋める (#50: 空投稿禁止)。
            th, lh, nh = sec_historical_winners(conn, race.get("race_name", ""), years=6)
            if lh and nh > 0:
                header = f"🏇 {day} {label}\n歴代勝ち馬とコース傾向"
                sections.append(_make_section("【歴代勝ち馬】", lh[:3]))

    # 中身ゼロ (追い切り・コース適性・歴代いずれも空) は投稿スキップ (ヘッダー+CTA 空投稿禁止)
    if not [s for s in sections if s]:
        return None, {"sections_used": [], "samples": samples, "skipped": "no_content"}

    # 🆕 強い見出し (この builder は #52 で SLOT_BUILDERS[thu_morning] に割当 → dow=3)
    lede = _build_morning_lede(3, conn, race)
    if lede:
        header += f"\n\n{lede}"

    # CTA は木朝 slot 用 (#52: 水昼→木朝へ移動。netkeibaの追い切り評価は木以降公開のため)
    cta = "→ 木昼に注目馬TOP3🔔"

    hashtags = _hashtags(race)
    tweets = _split_to_thread(header, [s for s in sections if s], cta, hashtags)
    return tweets, {"sections_used": ["workout"], "samples": samples, "char_count": sum(_x_len(t) for t in tweets)}


# ─────────────────────────────────────────────────────────
# 木朝: 出走確定+血統
# ─────────────────────────────────────────────────────────
def build_thu_morning_post(race: dict, conn) -> Tuple[str, dict]:
    """木朝: 出走確定後、血統がコースに合う馬を抽出"""
    samples = {}
    sections = []

    label = _race_label(race)
    day = _day_phrase(race)
    venue = race.get("venue", "")
    surface = race.get("surface", "")
    distance = race.get("distance", 0)
    race_id = race.get("race_id", "")

    # 🆕 (#50): ヘッダーのテーマは採用セクションに合わせて動的化。
    # 出走馬未確定だと entry_pedigree / sires が両方空 → 旧コードは
    # 「📋出走馬×コース適性」+ CTA だけの中身ゼロ投稿になっていた (#1 最悪パターン)。
    theme = None

    # Section 1: 出走馬の血統がコース複勝35%超 (該当馬のみ)
    t1, lines1, n1 = sec_entry_pedigree_match(
        conn, race_id, venue, surface, distance, top=4, years=6
    )
    samples["entry_pedigree"] = n1
    if lines1 and n1 > 0:
        cleaned = _strip_horse_number_from_lines(lines1, race)
        sections.append(_make_section(
            "【コース複勝35%超 の血統に該当】",
            cleaned[:4]
        ))
        theme = "出走馬×コース適性"

    # Section 2: 補助 — 種牡馬TOP の該当馬 (該当馬なし非表示)
    t2, lines2, n2 = sec_sire_course_cross(
        conn, venue, surface, distance, top=3, years=6, race_id=race_id)
    samples["sires"] = n2
    if lines2 and n2 > 0:
        filtered = [l for l in lines2 if "該当馬なし" not in l]
        if filtered:
            import re as _re
            cleaned = []
            for l in filtered[:2]:
                m = _re.match(r"^(🥇|🥈|🥉|🏅)([^\s]+)\s+(\d+%)\s+\(\d+/\d+\)\s+→\s+該当:\s+(.+)$", l)
                if m:
                    medal, sire, pct, horses = m.groups()
                    names = horses.strip().split()
                    horses_disp = names[0] if len(names) == 1 else f"{names[0]}他{len(names)-1}頭"
                    cleaned.append(f"{medal}{sire}産駒({pct}) → {horses_disp}")
                else:
                    cleaned.append(l)
            if cleaned:
                sections.append(_make_section("【コース実績ある種牡馬の該当馬】", cleaned))
                if theme is None:
                    # 出走馬該当は出せなかったが、コースに実績ある種牡馬は提示できる
                    theme = "コース実績ある血統"

    # フォールバック: 出走馬絡みが全滅 (出走馬未確定 / 該当血統なし) でも、ほぼ常に取れる
    # 歴代勝ち馬 → コース適性 TOP の順で埋めて中身ゼロ投稿を回避する (#50)。
    if not [s for s in sections if s]:
        th, lh, nh = sec_historical_winners(conn, race.get("race_name", ""), years=6)
        if lh and nh > 0:
            sections.append(_make_section("【歴代勝ち馬】", lh[:3]))
            theme = "歴代勝ち馬とコース傾向"
        else:
            t_hp, lines_hp, n_hp = sec_handpicked_top(
                conn, race_id, venue, surface, distance, top=3, years=6)
            if lines_hp and n_hp > 0:
                cleaned = _strip_horse_number_from_lines(lines_hp, race)
                sections.append(_make_section(t_hp, cleaned[:4]))
                theme = "コース適性 注目馬TOP"

    # それでも空なら投稿スキップ (ヘッダー+CTA だけの空投稿を絶対に出さない)
    if not [s for s in sections if s]:
        return None, {"sections_used": [], "samples": samples, "skipped": "no_content"}

    header = f"📋 {day} {label}\n{theme}"
    # 🆕 強い見出し (この builder は #52 で SLOT_BUILDERS[wed_weekday] に割当 → dow=2)
    lede = _build_morning_lede(2, conn, race)
    if lede:
        header += f"\n\n{lede}"
    # CTA は水昼 slot 用 (#52: 木朝→水昼へ移動。出走馬未確定でも歴代→コース適性 fallback)
    cta = "→ 今夜は危険な1人気を配信🔔"

    hashtags = _hashtags(race)
    tweets = _split_to_thread(header, [s for s in sections if s], cta, hashtags)
    return tweets, {"sections_used": ["entry_pedigree", "sires"], "samples": samples, "char_count": sum(_x_len(t) for t in tweets)}


def _strip_horse_number_from_lines(lines: list, race: dict) -> list:
    """sec_* の output 行から「N番」を抽選前なら除去。

    horse_label 共通ヘルパーが理想だが、既存 sec_* が文字列を返すので
    後処理として regex で削る。
    """
    if _is_post_position_drawn(race):
        return lines  # 抽選後はそのまま
    import re as _re
    cleaned = []
    for l in lines:
        # 「{番号}番 {馬名}」を「{馬名}」に置換
        cleaned.append(_re.sub(r"\d{1,2}番\s+", "", l))
    return cleaned


# ─────────────────────────────────────────────────────────
# 木昼: AI注目TOP3 (注目順位、印なし、客観データ)
# ─────────────────────────────────────────────────────────
def build_thu_weekday_post(race: dict, conn) -> Tuple[str, dict]:
    """木昼: 注目TOP3 (AI予測あれば優先、なくても出走馬×統計で生成)"""
    samples = {}
    sections = []

    label = _race_label(race)
    day = _day_phrase(race)
    venue = race.get("venue", "")
    surface = race.get("surface", "")
    distance = race.get("distance", 0)
    race_id = race.get("race_id", "")

    # Section 1: TOP3
    # 🆕 (#50): ヘッダーのテーマは「実際に採用したセクション」に合わせて動的化。
    # AI予測キャッシュ有→「AI注目馬TOP3」/ 無 (コース適性フォールバック)→「コース適性 注目馬TOP」。
    # 固定「注目馬TOP3」だと handpicked path で 【コース適性TOP】 を出し、タイトル詐欺になる。
    theme = None
    t1, lines1, n1 = sec_attention_top(conn, race_id, top=3, include_marks=False)
    if lines1 and n1 > 0 and any(l.startswith(("1️⃣", "2️⃣", "3️⃣")) for l in lines1):
        cleaned = _strip_horse_number_from_lines(lines1, race)
        sections.append(_make_section(t1, cleaned[:5]))
        samples["source"] = "attention"
        theme = "AI注目馬TOP3"
    else:
        t2, lines2, n2 = sec_handpicked_top(
            conn, race_id, venue, surface, distance, top=3, years=6
        )
        if lines2 and n2 > 0:
            cleaned = _strip_horse_number_from_lines(lines2, race)
            sections.append(_make_section(t2, cleaned[:4]))
            theme = "コース適性 注目馬TOP"
        samples["source"] = "handpicked"

    # Section 2: 補助 — 過去6年で「コース好相性パターン」
    t_pat, lines_pat, n_pat = sec_pattern_discovery(
        conn, venue, surface, distance, years=6, target_top3_pct=55.0,
        race_id=race_id,
    )
    if lines_pat and n_pat > 0:
        sections.append(_make_section("【コース好相性パターン】 過去6年", lines_pat[:3]))
        if theme is None:
            theme = "コース好相性パターン"

    # 中身ゼロ (注目馬もパターンも取れない極薄レース) は投稿しない (#50: ヘッダー+CTA 空投稿禁止)
    if not [s for s in sections if s]:
        return None, {"sections_used": [], "samples": samples, "skipped": "no_content"}

    header = f"🎯 {day} {label}\n{theme}"
    cta = "→ 今夜AI最終予想+note告知🔔"

    hashtags = _hashtags(race)
    tweets = _split_to_thread(header, [s for s in sections if s], cta, hashtags)
    return tweets, {"sections_used": ["top3", "pattern"], "samples": samples, "char_count": sum(_x_len(t) for t in tweets)}


# ─────────────────────────────────────────────────────────
# 木夜: AI最終予想 注目TOP3 + note告知
# ─────────────────────────────────────────────────────────
def build_thu_evening_post(race: dict, conn) -> Tuple[str, dict]:
    """木夜: 最終予想 注目馬3頭 + note告知"""
    samples = {}
    sections = []

    label = _race_label(race)
    day = _day_phrase(race)
    venue = race.get("venue", "")
    surface = race.get("surface", "")
    distance = race.get("distance", 0)
    race_id = race.get("race_id", "")

    # 🆕 (#50): ヘッダーのテーマは採用セクションに合わせて動的化 (タイトル詐欺防止)。
    # AI予測あり→「最終予想 注目馬3頭」/ コース適性フォールバック→「コース適性 注目馬TOP」。
    theme = None
    t1, lines1, n1 = sec_attention_top(conn, race_id, top=3, include_marks=False)
    if lines1 and n1 > 0 and any(l.startswith(("1️⃣", "2️⃣", "3️⃣")) for l in lines1):
        cleaned = _strip_horse_number_from_lines(lines1, race)
        sections.append(_make_section(t1, cleaned[:5]))
        samples["source"] = "attention"
        theme = "最終予想 注目馬3頭"
    else:
        t2, lines2, n2 = sec_handpicked_top(
            conn, race_id, venue, surface, distance, top=3, years=6
        )
        if lines2 and n2 > 0:
            cleaned = _strip_horse_number_from_lines(lines2, race)
            sections.append(_make_section(t2, cleaned[:4]))
            theme = "コース適性 注目馬TOP"
        samples["source"] = "handpicked"

    # 中身ゼロ (注目馬が全く取れない) は投稿しない (#50: ヘッダー+CTA 空投稿禁止)
    if not [s for s in sections if s]:
        return None, {"sections_used": [], "samples": samples, "skipped": "no_content"}

    header = f"🌟 {day} {label}\n{theme}"
    cta = "→ 土朝に印 (◎○▲) と買い目を発表🔔\n→ note記事も公開予定📝"

    hashtags = _hashtags(race)
    tweets = _split_to_thread(header, [s for s in sections if s], cta, hashtags)
    return tweets, {"sections_used": ["top3"], "samples": samples, "char_count": sum(_x_len(t) for t in tweets)}


# ─────────────────────────────────────────────────────────
# 金昼: 注目馬TOP3+根拠 (印なし)
# ─────────────────────────────────────────────────────────
def build_fri_weekday_post(race: dict, conn) -> Tuple[str, dict]:
    """金昼: 注目馬3頭 (AI予測あれば優先、なくても出走予定馬から手抜き無く生成)"""
    samples = {}
    sections = []

    label = _race_label(race)
    day = _day_phrase(race)
    venue = race.get("venue", "")
    surface = race.get("surface", "")
    distance = race.get("distance", 0)
    race_id = race.get("race_id", "")

    # 1) AI予測キャッシュがあれば優先
    # 🆕 (#50): ヘッダーのテーマは採用セクションに合わせて動的化 (タイトル詐欺防止)。
    theme = None
    t1, lines1, n1 = sec_attention_top(conn, race_id, top=3, include_marks=False)
    if lines1 and n1 > 0 and any(l.startswith(("1️⃣", "2️⃣", "3️⃣")) for l in lines1):
        cleaned = _strip_horse_number_from_lines(lines1, race)
        sections.append(_make_section(t1, cleaned[:5]))
        samples["source"] = "attention"
        theme = "AI注目馬TOP3"
    else:
        # 2) 無ければ出走予定馬 × コース統計でTOP3抽出 (内部メッセージ無し)
        t2, lines2, n2 = sec_handpicked_top(
            conn, race_id, venue, surface, distance, top=3, years=6
        )
        if lines2 and n2 > 0:
            cleaned = _strip_horse_number_from_lines(lines2, race)
            sections.append(_make_section(t2, cleaned[:4]))
            theme = "コース適性 注目馬TOP"
        samples["source"] = "handpicked"

    # 補助: パターン発掘
    t3, lines3, n3 = sec_pattern_discovery(
        conn, venue, surface, distance, years=6, target_top3_pct=55.0,
        race_id=race_id,
    )
    if lines3 and n3 > 0:
        sections.append(_make_section("【コース好相性パターン】", lines3[:2]))
        if theme is None:
            theme = "コース好相性パターン"

    # 中身ゼロ (注目馬もパターンも取れない極薄レース) は投稿しない (#50)
    if not [s for s in sections if s]:
        return None, {"sections_used": [], "samples": samples, "skipped": "no_content"}

    header = f"🎯 {day} {label}\n{theme}"
    cta = "→ 土朝に印 (◎○▲) と買い目🔔"

    hashtags = _hashtags(race)
    tweets = _split_to_thread(header, [s for s in sections if s], cta, hashtags)
    return tweets, {"sections_used": ["attention/handpicked", "pattern"], "samples": samples, "char_count": sum(_x_len(t) for t in tweets)}


# ─────────────────────────────────────────────────────────
# 金夜: 翌朝配信告知 (最有力1頭の匂わせ)
# ─────────────────────────────────────────────────────────
def build_fri_evening_post(race: dict, conn) -> Tuple[str, dict]:
    """金夜: 翌朝配信告知 (最有力候補1頭の匂わせ)"""
    samples = {}
    sections = []
    import re as _re

    label = _race_label(race)
    day = _day_phrase(race)
    venue = race.get("venue", "")
    surface = race.get("surface", "")
    distance = race.get("distance", 0)
    race_id = race.get("race_id", "")

    header = f"🔔 {day} {label}\n明朝AI予想 配信予定"

    # 最有力 (TOP1 のみ) — AI 予測あれば優先、なければ handpicked
    t1, lines1, n1 = sec_attention_top(conn, race_id, top=1, include_marks=False)
    top_line = None
    if lines1 and n1 > 0:
        cleaned = _strip_horse_number_from_lines(lines1, race)
        first = next((l for l in cleaned if l.startswith("1️⃣")), None)
        if first:
            top_line = _re.sub(r"^[1-4]️?⃣\s*", "", first).strip()

    if not top_line:
        # handpicked にフォールバック
        t2, lines2, n2 = sec_handpicked_top(
            conn, race_id, venue, surface, distance, top=1, years=6
        )
        if lines2 and n2 > 0:
            cleaned = _strip_horse_number_from_lines(lines2, race)
            first = next((l for l in cleaned if l.startswith("1️⃣")), None)
            if first:
                top_line = _re.sub(r"^[1-4]️?⃣\s*", "", first).strip()

    if top_line:
        sections.append(f"【最有力候補】\n{top_line}")

    sections.append("【配信予定】\n🌅 朝7時 — 印別最終予想\n🎯 朝10時 — 確定買い目")

    cta = "→ お楽しみに🔥"

    hashtags = _hashtags(race)
    tweets = _split_to_thread(header, [s for s in sections if s], cta, hashtags)
    return tweets, {"sections_used": ["top1"], "samples": samples, "char_count": sum(_x_len(t) for t in tweets)}


# ─────────────────────────────────────────────────────────
# Dispatcher
# ─────────────────────────────────────────────────────────
SLOT_BUILDERS = {
    # 出走馬未確定時の slot (月-水 朝昼夜) — 歴代データ主体
    "morning": build_morning_post,           # 朝 (曜日別ローテ: 月=ラインナップ/火=血統/土日=直前)
    "weekday": build_weekday_post,           # 月昼 (全曜日昼)
    "evening": build_evening_post,           # 月夜 (全曜日夜)
    "tue_evening": build_tue_evening_post,   # 火夜 (前走パターン特化)
    "wed_morning": build_wed_morning_post,   # 水朝 (騎手特化)
    # #52: 水昼↔木朝 入れ替え。netkeiba 追い切り評価は木以降公開のため、追い切り評価は木朝へ。
    # 水昼は build_thu_morning_post(歴代→コース適性 fallback)を流用 — 水曜は出走馬未確定だが
    # #51 で実装済みのフォールバックで歴代勝ち馬+コース適性を出せる。
    "wed_weekday": build_thu_morning_post,   # 水昼 (歴代→コース適性、出走未確定対応)
    "wed_evening": build_wed_evening_post,   # 水夜 (危険な人気馬)
    # 出走馬確定後の slot (木以降) — predictions_cache 連動
    "thu_morning": build_wed_weekday_post,   # 木朝 (追い切り評価) [#52: 水昼から移動]
    "thu_weekday": build_thu_weekday_post,   # 木昼 (8軸TOP4)
    "thu_evening": build_thu_evening_post,   # 木夜 (◎○▲+note告知)
    "fri_morning": build_fri_morning_post,   # 金朝 (AI独自パターン)
    "fri_weekday": build_fri_weekday_post,   # 金昼 (注目3頭+根拠)
    "fri_evening": build_fri_evening_post,   # 金夜 (翌朝配信告知)
}


def build_slot_post(slot: str, race: dict, conn) -> Tuple[str, dict]:
    """slot 名から該当 builder を呼んで tweet 生成。

    Args:
        slot: 'morning' / 'weekday' / 'evening' / 'tue_evening' / etc
        race: {'race_name', 'venue', 'surface', 'distance', 'grade', 'race_date', ...}
        conn: sqlite3 Connection

    Returns:
        (tweet_text, meta_dict)
        meta は char_count / sections_used / samples を含む
    """
    builder = SLOT_BUILDERS.get(slot)
    if not builder:
        raise ValueError(f"未知の slot: {slot} (valid: {list(SLOT_BUILDERS.keys())})")
    return builder(race, conn)
