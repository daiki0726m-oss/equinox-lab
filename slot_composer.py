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
    sec_workout_focus,
    sec_entry_pedigree_match,
    sec_eight_axis_top,
    sec_top3_with_reasons,
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


def _day_phrase(race: dict, today: Optional[datetime] = None) -> str:
    """5/31(日) のような日付フレーズ"""
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
        when = "今日"
    elif diff_days == 1:
        when = "明日"
    elif diff_days < 7:
        when = "今週末"
    return f"{when}{d.month}/{d.day}({weekday})"


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


# X 文字数予算 (220-260 目安、上限 280)
CHAR_BUDGET = 260


def _fit_to_budget(header: str, sections: list, cta: str, hashtags: str,
                    budget: int = CHAR_BUDGET) -> str:
    """セクションを順に削って予算内に収める。

    優先度: header / hashtags / cta は必須、sections は末尾から削る。
    """
    while sections:
        body = "\n\n".join(sections)
        tweet = f"{header}\n\n{body}\n\n{cta}\n{hashtags}"
        if _x_len(tweet) <= budget:
            return tweet
        # 末尾セクションから 1行ずつ削る
        last = sections[-1].split("\n")
        if len(last) <= 2:
            # ヘッダ行+1行しか無い → セクションごと削る
            sections.pop()
        else:
            # 末尾の1行を削る
            sections[-1] = "\n".join(last[:-1])
    # セクション全削除でも超過 → header + cta + hashtags のみ
    return f"{header}\n\n{cta}\n{hashtags}"


# ─────────────────────────────────────────────────────────
# 月朝: 週末ラインナップ (歴代×1人気×種牡馬)
# ─────────────────────────────────────────────────────────
def build_morning_post(race: dict, conn) -> Tuple[str, dict]:
    """月朝 (7:30): 週末メインレースのラインナップ + 歴代データ + 1人気信頼度"""
    samples = {}
    sections = []

    label = _race_label(race)
    day = _day_phrase(race)
    venue = race.get("venue", "")
    surface = race.get("surface", "")
    distance = race.get("distance", 0)
    grade = race.get("grade")
    race_name = race.get("race_name", "")

    header = f"🏇 {day} {label}"

    # Section 1: 歴代勝ち馬 (3行のみ、短縮形 "2025 馬名 1人気/2.1倍")
    t1, lines1, n1 = sec_historical_winners(conn, race_name, years=6)
    samples["historical"] = n1
    if lines1 and n1 > 0:
        # 短縮: "🏆2025 クロワデュノール (1人気/2.1倍)" → そのまま3行
        sections.append(_make_section("【歴代勝ち馬】", lines1[:3]))

    # Section 2: 1人気トレンド (2行に圧縮)
    t2, lines2, n2 = sec_pop_trust_trend(
        conn, venue, surface, distance, grade=grade, years=6
    )
    samples["pop_trust"] = n2
    if lines2 and n2 >= 3:
        # 「複勝率」だけ + 結論で2行に
        relevant = [l for l in lines2 if "複勝率" in l or "→" in l]
        sections.append(_make_section("【1人気の信頼度】", relevant[:2]))

    # Section 3: 種牡馬 TOP2 → 1行に圧縮
    t3, lines3, n3 = sec_sire_course_cross(
        conn, venue, surface, distance, top=2, years=6
    )
    samples["sires"] = n3
    if lines3 and n3 > 0:
        compact = " / ".join(l.replace("🥇", "").replace("🥈", "").strip() for l in lines3[:2])
        sections.append(f"【種牡馬】 {compact}")

    # CTA (短く)
    cta = "→ 火朝に前走パターン🔔"

    hashtags = _hashtags(race)
    tweet = _fit_to_budget(header, [s for s in sections if s], cta, hashtags)

    char_count = _x_len(tweet)
    return tweet, {"sections_used": ["historical", "pop_trust", "sires"], "samples": samples, "char_count": char_count}


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
    race_name = race.get("race_name", "")

    header = f"🔍 {day} {label}"

    # Section 1: 種牡馬 TOP3 (各行短縮)
    t1, lines1, n1 = sec_sire_course_cross(
        conn, venue, surface, distance, top=3, years=6
    )
    samples["sires"] = n1
    if lines1 and n1 > 0:
        sections.append(_make_section(f"【{venue}{surface}{distance}m 種牡馬TOP3】", lines1[:3]))

    # Section 2: 枠順 (ベスト+ワーストの2行のみ)
    t2, lines2, n2 = sec_post_position_bias(conn, venue, surface, distance, years=6)
    samples["post_pos"] = n2
    if lines2 and n2 > 0:
        sections.append(_make_section("【枠順】", lines2[:2]))

    # Section 3: 異常年 (1行のみ)
    t3, lines3, n3 = sec_outlier_year(conn, race_name, years=6)
    samples["outliers"] = n3
    if lines3 and n3 > 0:
        outlier_line = next((l for l in lines3 if l.startswith("🚨")), None)
        if outlier_line:
            sections.append(f"【波乱】 {outlier_line}")

    cta = "→ 今夜AI注目要素🔔"

    hashtags = _hashtags(race)
    tweet = _fit_to_budget(header, [s for s in sections if s], cta, hashtags)

    char_count = _x_len(tweet)
    return tweet, {"sections_used": ["sires", "post_pos", "outliers"], "samples": samples, "char_count": char_count}


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

    header = f"🌙 {day} {label}"

    # Section 1: 末脚 (2行: 複勝率 + 結論)
    t1, lines1, n1 = sec_pace_decisive(conn, venue, surface, distance, years=6)
    samples["pace"] = n1
    if lines1 and n1 >= 10:
        relevant = [l for l in lines1 if "複勝率" in l or "→" in l]
        sections.append(_make_section("【末脚分析】", relevant[:2]))

    # Section 2: 1人気の信頼性 (3行 + 直近1事例)
    t2, lines2, n2 = sec_dangerous_favorites(
        conn, venue, surface, distance, grade=grade, years=6
    )
    samples["danger"] = n2
    if lines2 and n2 >= 3:
        # 飛び率 + 結論 + 直近1事例
        block = lines2[:3]
        sections.append(_make_section("【1人気の信頼性】", block))

    cta = "→ 火朝に血統深掘り🔔"

    hashtags = _hashtags(race)
    tweet = _fit_to_budget(header, [s for s in sections if s], cta, hashtags)

    char_count = _x_len(tweet)
    return tweet, {"sections_used": ["pace", "danger"], "samples": samples, "char_count": char_count}


# ─────────────────────────────────────────────────────────
# 火夜: 前走パターン + ローテーション
# ─────────────────────────────────────────────────────────
def build_tue_evening_post(race: dict, conn) -> Tuple[str, dict]:
    """火夜: レース詳細 (前走パターン + ローテ)"""
    samples = {}
    sections = []

    label = _race_label(race)
    day = _day_phrase(race)
    race_name = race.get("race_name", "")

    header = f"📊 {day} {label}\n勝ち馬の傾向"

    # Section 1: 前走パターン
    t1, lines1, n1 = sec_prev_race_pattern(conn, race_name, years=6)
    samples["prev_race"] = n1
    if lines1 and n1 > 0:
        # 集計部分のみ抜粋 (個別 detail は冗長なので2件まで)
        agg_lines = [l for l in lines1 if l.startswith("🏆")]
        detail_lines = [l for l in lines1 if not l.startswith("🏆") and not l.startswith("---")]
        block = agg_lines[:4]
        if detail_lines:
            block.append("---")
            block.extend(detail_lines[:2])
        sections.append(_make_section("【勝ち馬の前走】", block))

    # Section 2: ローテーション
    t2, lines2, n2 = sec_rotation_pattern(conn, race_name, years=6)
    samples["rotation"] = n2
    if lines2 and n2 > 0:
        sections.append(_make_section("【ローテ傾向】", lines2[:3]))

    cta = "→ 木朝に出走確定+血統を配信🔔"

    hashtags = _hashtags(race)
    tweet = _fit_to_budget(header, [s for s in sections if s], cta, hashtags)

    char_count = _x_len(tweet)
    return tweet, {"sections_used": ["prev_race", "rotation"], "samples": samples, "char_count": char_count}


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

    header = f"🏇 {day} {label}\n騎手×コースの傾向"

    # Section 1: 騎手TOP
    t1, lines1, n1 = sec_jockey_recent_form(
        conn, venue, surface, distance, top=4, years=3
    )
    samples["jockey"] = n1
    if lines1 and n1 > 0:
        sections.append(_make_section("【コース好相性騎手】", lines1[:4]))

    # Section 2: 末脚
    t2, lines2, n2 = sec_pace_decisive(conn, venue, surface, distance, years=6)
    samples["pace"] = n2
    if lines2 and n2 >= 10:
        sections.append(_make_section("【末脚優位性】", lines2[:2]))

    cta = "→ 今夜は危険な1人気を配信🔔"

    hashtags = _hashtags(race)
    tweet = _fit_to_budget(header, [s for s in sections if s], cta, hashtags)

    char_count = _x_len(tweet)
    return tweet, {"sections_used": ["jockey", "pace"], "samples": samples, "char_count": char_count}


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

    header = f"⚠️ {day} {label}\n危険な人気馬の予兆"

    # Section 1: 1人気の信頼性
    t1, lines1, n1 = sec_dangerous_favorites(
        conn, venue, surface, distance, grade=grade, years=6
    )
    samples["danger"] = n1
    if lines1 and n1 >= 3:
        sections.append(_make_section("【1人気の信頼性】", lines1[:4]))

    # Section 2: 異常年
    t2, lines2, n2 = sec_outlier_year(conn, race_name, years=6)
    samples["outliers"] = n2
    if lines2 and n2 > 0:
        outliers = [l for l in lines2 if l.startswith("🚨")]
        if outliers:
            sections.append(_make_section("【過去の波乱事例】", outliers[:2]))

    cta = "→ 木朝に出走確定+血統を配信🔔"

    hashtags = _hashtags(race)
    tweet = _fit_to_budget(header, [s for s in sections if s], cta, hashtags)

    char_count = _x_len(tweet)
    return tweet, {"sections_used": ["danger", "outliers"], "samples": samples, "char_count": char_count}


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

    header = f"🔮 {day} {label}\nAI独自パターン分析"

    # Section 1: パターン発掘
    t1, lines1, n1 = sec_pattern_discovery(
        conn, venue, surface, distance, years=6, target_top3_pct=55.0
    )
    samples["patterns"] = n1
    if lines1 and n1 > 0:
        sections.append(_make_section("【AIが発掘した好相性パターン】", lines1[:3]))

    # Section 2: 種牡馬 TOP (補助)
    t2, lines2, n2 = sec_sire_course_cross(
        conn, venue, surface, distance, top=3, years=6
    )
    samples["sires"] = n2
    if lines2 and n2 > 0:
        sections.append(_make_section("【総合 種牡馬 複勝率】", lines2[:3]))

    cta = "→ 今夜は翌朝の確定予想告知🔔"

    hashtags = _hashtags(race)
    tweet = _fit_to_budget(header, [s for s in sections if s], cta, hashtags)

    char_count = _x_len(tweet)
    return tweet, {"sections_used": ["patterns", "sires"], "samples": samples, "char_count": char_count}


# ─────────────────────────────────────────────────────────
# 水昼: 追い切り情報
# ─────────────────────────────────────────────────────────
def build_wed_weekday_post(race: dict, conn) -> Tuple[str, dict]:
    """水昼: 追い切り高評価馬リスト"""
    samples = {}
    sections = []

    label = _race_label(race)
    day = _day_phrase(race)
    race_id = race.get("race_id", "")

    header = f"🏋️ {day} {label}\n追い切り評価"

    # Section 1: 追い切り A/B 評価馬
    t1, lines1, n1 = sec_workout_focus(conn, race_id, top=4)
    samples["workout"] = n1
    if lines1 and n1 > 0:
        sections.append(_make_section("【A/B 評価馬】", lines1[:4]))

    cta = "→ 今夜は危険な1人気を配信🔔"

    hashtags = _hashtags(race)
    tweet = _fit_to_budget(header, [s for s in sections if s], cta, hashtags)
    return tweet, {"sections_used": ["workout"], "samples": samples, "char_count": _x_len(tweet)}


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

    header = f"📋 {day} {label}\n出走確定+血統マッチ"

    # Section 1: 出走馬×血統 (このコース複勝35%超)
    t1, lines1, n1 = sec_entry_pedigree_match(
        conn, race_id, venue, surface, distance, top=4, years=6
    )
    samples["entry_pedigree"] = n1
    if lines1 and n1 > 0:
        sections.append(_make_section("【血統がコースに合う出走馬】", lines1[:4]))

    # Section 2: コース全体の種牡馬TOP (補助)
    t2, lines2, n2 = sec_sire_course_cross(
        conn, venue, surface, distance, top=2, years=6
    )
    samples["sires"] = n2
    if lines2 and n2 > 0:
        sections.append(_make_section("【総合 種牡馬TOP】", lines2[:2]))

    cta = "→ 今夜AI最終TOP4🔔"

    hashtags = _hashtags(race)
    tweet = _fit_to_budget(header, [s for s in sections if s], cta, hashtags)
    return tweet, {"sections_used": ["entry_pedigree", "sires"], "samples": samples, "char_count": _x_len(tweet)}


# ─────────────────────────────────────────────────────────
# 木昼: 8軸最終TOP4
# ─────────────────────────────────────────────────────────
def build_thu_weekday_post(race: dict, conn) -> Tuple[str, dict]:
    """木昼: 8軸スコア最終TOP4"""
    samples = {}
    sections = []

    label = _race_label(race)
    day = _day_phrase(race)
    race_id = race.get("race_id", "")

    header = f"🎯 {day} {label}\n8軸スコア 最終TOP4"

    # Section 1: 8軸TOP4
    t1, lines1, n1 = sec_eight_axis_top(conn, race_id, top=4)
    samples["axis"] = n1
    if lines1 and n1 > 0:
        sections.append(_make_section("【8軸スコア】", lines1[:4]))

    cta = "→ 今夜AI最終3頭+note告知🔔"

    hashtags = _hashtags(race)
    tweet = _fit_to_budget(header, [s for s in sections if s], cta, hashtags)
    return tweet, {"sections_used": ["axis"], "samples": samples, "char_count": _x_len(tweet)}


# ─────────────────────────────────────────────────────────
# 木夜: 最終3頭+note告知
# ─────────────────────────────────────────────────────────
def build_thu_evening_post(race: dict, conn) -> Tuple[str, dict]:
    """木夜: ◎○▲ + 各馬の決定的理由 + note告知"""
    samples = {}
    sections = []

    label = _race_label(race)
    day = _day_phrase(race)
    race_id = race.get("race_id", "")

    header = f"🌟 {day} {label}\nAI最終予想 ◎○▲"

    # Section 1: ◎○▲ + 理由
    t1, lines1, n1 = sec_top3_with_reasons(conn, race_id)
    samples["top3"] = n1
    if lines1 and n1 > 0:
        sections.append(_make_section("【AI印 ◎○▲】", lines1[:3]))

    cta = "→ 明朝に最終確定+買い目🔔\n→ note記事公開予定📝"

    hashtags = _hashtags(race)
    tweet = _fit_to_budget(header, [s for s in sections if s], cta, hashtags)
    return tweet, {"sections_used": ["top3"], "samples": samples, "char_count": _x_len(tweet)}


# ─────────────────────────────────────────────────────────
# 金昼: 注目馬3頭+根拠
# ─────────────────────────────────────────────────────────
def build_fri_weekday_post(race: dict, conn) -> Tuple[str, dict]:
    """金昼: 注目馬3頭+根拠のみ (買い目は出さない、teaser)"""
    samples = {}
    sections = []

    label = _race_label(race)
    day = _day_phrase(race)
    race_id = race.get("race_id", "")

    header = f"🎯 {day} {label}\n注目馬 3頭"

    # Section 1: 注目馬3頭 + 各馬の根拠 (買い目組合せは出さない)
    t1, lines1, n1 = sec_top3_with_reasons(conn, race_id)
    samples["top3"] = n1
    if lines1 and n1 > 0:
        sections.append(_make_section("【AIが注目する3頭】", lines1[:3]))

    cta = "→ 明朝に印別買い目を配信🔔"

    hashtags = _hashtags(race)
    tweet = _fit_to_budget(header, [s for s in sections if s], cta, hashtags)
    return tweet, {"sections_used": ["top3"], "samples": samples, "char_count": _x_len(tweet)}


# ─────────────────────────────────────────────────────────
# 金夜: 翌朝配信告知
# ─────────────────────────────────────────────────────────
def build_fri_evening_post(race: dict, conn) -> Tuple[str, dict]:
    """金夜: 翌朝配信告知 (ティーザー、◎だけ匂わせ)"""
    samples = {}
    sections = []

    label = _race_label(race)
    day = _day_phrase(race)
    race_id = race.get("race_id", "")

    header = f"🔔 {day} {label}\n明朝AI予想 配信予定"

    # Section 1: ◎だけ匂わせ (フルリストは出さない)
    t1, lines1, n1 = sec_top3_with_reasons(conn, race_id)
    samples["top3"] = n1
    if lines1 and n1 > 0:
        # ◎の行だけ出して、○▲は隠す
        honmei_line = next((l for l in lines1 if l.startswith("◎")), None)
        if honmei_line:
            sections.append(f"【◎】 {honmei_line[1:].strip()}")

    sections.append("【配信予定】\n🌅 朝7時 — 印別最終予想\n🎯 朝10時 — 確定買い目")

    cta = "→ お楽しみに🔥"

    hashtags = _hashtags(race)
    tweet = _fit_to_budget(header, [s for s in sections if s], cta, hashtags)
    return tweet, {"sections_used": ["top3"], "samples": samples, "char_count": _x_len(tweet)}


# ─────────────────────────────────────────────────────────
# Dispatcher
# ─────────────────────────────────────────────────────────
SLOT_BUILDERS = {
    # 出走馬未確定時の slot (月-水 朝昼夜) — 歴代データ主体
    "morning": build_morning_post,           # 月朝 (全曜日朝の汎用)
    "weekday": build_weekday_post,           # 月昼 (全曜日昼)
    "evening": build_evening_post,           # 月夜 (全曜日夜)
    "tue_evening": build_tue_evening_post,   # 火夜 (前走パターン特化)
    "wed_morning": build_wed_morning_post,   # 水朝 (騎手特化)
    "wed_weekday": build_wed_weekday_post,   # 水昼 (追い切り)
    "wed_evening": build_wed_evening_post,   # 水夜 (危険な人気馬)
    # 出走馬確定後の slot (木以降) — predictions_cache 連動
    "thu_morning": build_thu_morning_post,   # 木朝 (出走確定+血統)
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
