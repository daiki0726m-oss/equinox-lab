"""投稿テンプレ用「複合分析セクション」関数群 (2026-05-25 新設)

各関数は (title: str, lines: list[str], sample_n: int) を返す純粋関数。
sample_n < 3 なら lines に「(参考)」付与済みで返す。

docs/POST_TEMPLATE_DESIGN.md に従う Phase 1 実装。

呼び出し例:
    from post_sections import sec_historical_winners
    with sqlite3.connect("keiba.db") as conn:
        title, lines, n = sec_historical_winners(conn, "日本ダービー", years=6)

重要原則 (#21 教訓):
- 全数値は DB クエリ結果のみ
- ハードコードな数字/馬名は禁止
- サンプル不足は明示
"""

from __future__ import annotations
from typing import Tuple, List, Optional
from datetime import datetime


def _sample_note(sample_n: int, expected_n: int) -> str:
    """サンプル充足の注記文字列"""
    if sample_n == 0:
        return "(データなし)"
    if sample_n < 3:
        return f"(サンプル{sample_n}件・参考値)"
    if expected_n and sample_n < expected_n:
        return f"({sample_n}件のみ・一部欠落)"
    return ""


# ─────────────────────────────────────────────────────────
# sec_historical_winners
# ─────────────────────────────────────────────────────────
def sec_historical_winners(
    conn,
    race_name: str,
    years: int = 6,
) -> Tuple[str, List[str], int]:
    """過去N年の同レース勝ち馬を返す。

    例:
        🏆2025 クロワデュノール(1人気/2.1倍)
        🏆2024 ダノンデサイル(9人気/46.6倍)
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
            substr(r.race_date, 1, 4) AS year,
            h.horse_name AS winner,
            res.popularity AS pop,
            res.odds AS odds
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
    n = len(rows)
    note = _sample_note(n, years)
    title = f"【{race_name} 歴代勝ち馬{('・' + note) if note else ''}】"

    if n == 0:
        return (title, [f"DB に過去データなし"], 0)

    lines = []
    for row in rows:
        year, winner, pop, odds = row
        parts = [f"🏆{year} {winner}"]
        meta = []
        if pop and pop > 0:
            meta.append(f"{pop}人気")
        if odds and odds > 0:
            meta.append(f"{odds:.1f}倍")
        if meta:
            parts.append(f"({'/'.join(meta)})")
        lines.append(" ".join(parts))
    return (title, lines, n)


# ─────────────────────────────────────────────────────────
# sec_pop_trust_trend
# ─────────────────────────────────────────────────────────
def sec_pop_trust_trend(
    conn,
    venue: str,
    surface: str,
    distance: int,
    grade: Optional[str] = None,
    years: int = 6,
) -> Tuple[str, List[str], int]:
    """過去N年・指定コース/グレードで、1番人気の信頼度トレンドを返す。

    例:
        ✅勝率 38% (3勝/8)
        ✅複勝率 75% (6/8)
        → 1番人気軸が王道
    """
    cur = conn.cursor()
    from_date = f"{datetime.now().year - years}-01-01"
    base_query = """
        SELECT
            COUNT(*) AS n,
            SUM(CASE WHEN res.finish_position = 1 THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN res.finish_position BETWEEN 1 AND 3 THEN 1 ELSE 0 END) AS top3
        FROM races r
        JOIN results res ON r.race_id = res.race_id AND res.popularity = 1
        WHERE r.surface = ? AND r.distance = ? AND r.venue = ?
          AND r.race_date >= ?
    """
    params = [surface, distance, venue, from_date]
    if grade:
        base_query += " AND r.grade = ?"
        params.append(grade)
    cur.execute(base_query, params)
    n, wins, top3 = cur.fetchone()
    n = n or 0
    wins = wins or 0
    top3 = top3 or 0

    grade_label = f"{grade} " if grade else ""
    note = _sample_note(n, 0)
    title = f"【{venue}{surface}{distance}m {grade_label}1人気トレンド{('・' + note) if note else ''}】"

    if n < 3:
        return (title, [f"サンプル{n}件のみ (信頼性低)"], n)

    win_pct = 100 * wins / n
    top3_pct = 100 * top3 / n

    lines = [
        f"✅勝率 {win_pct:.0f}% ({wins}勝/{n})",
        f"✅複勝率 {top3_pct:.0f}% ({top3}/{n})",
    ]
    if win_pct >= 40:
        lines.append("→ 1番人気軸が王道")
    elif win_pct < 25:
        lines.append("→ 1番人気を疑え、相手探し")
    else:
        lines.append("→ 1番人気の質を見極めるレース")
    return (title, lines, n)


# ─────────────────────────────────────────────────────────
# sec_sire_course_cross
# ─────────────────────────────────────────────────────────
def sec_sire_course_cross(
    conn,
    venue: str,
    surface: str,
    distance: int,
    top: int = 3,
    years: int = 6,
    min_runs: int = 4,
) -> Tuple[str, List[str], int]:
    """指定コースで過去N年複勝率の高い種牡馬TOPを返す。

    例:
        🥇ディープインパクト 39% (5/13)
        🥇ハーツクライ 36% (4/11)
    """
    cur = conn.cursor()
    from_date = f"{datetime.now().year - years}-01-01"
    cur.execute(
        """
        SELECT
            h.sire,
            COUNT(*) AS runs,
            SUM(CASE WHEN res.finish_position BETWEEN 1 AND 3 THEN 1 ELSE 0 END) AS top3
        FROM races r
        JOIN results res ON r.race_id = res.race_id
        JOIN horses h ON res.horse_id = h.horse_id
        WHERE r.surface = ? AND r.distance = ? AND r.venue = ?
          AND r.race_date >= ?
          AND res.finish_position > 0
          AND h.sire IS NOT NULL AND h.sire != ''
        GROUP BY h.sire
        HAVING runs >= ?
        ORDER BY (1.0 * top3 / runs) DESC, runs DESC
        LIMIT ?
        """,
        (surface, distance, venue, from_date, min_runs, top),
    )
    rows = cur.fetchall()
    n = len(rows)
    note = _sample_note(n, top)
    title = f"【{venue}{surface}{distance}m 過去{years}年 種牡馬複勝率TOP{top}{('・' + note) if note else ''}】"

    if n == 0:
        return (title, [f"{min_runs}走以上の種牡馬なし"], 0)

    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, (sire, runs, top3) in enumerate(rows):
        pct = 100 * top3 / runs
        m = medals[i] if i < len(medals) else "🏅"
        lines.append(f"{m}{sire} {pct:.0f}% ({top3}/{runs})")
    return (title, lines, n)


# ─────────────────────────────────────────────────────────
# sec_prev_race_pattern
# ─────────────────────────────────────────────────────────
def sec_prev_race_pattern(
    conn,
    race_name: str,
    years: int = 6,
) -> Tuple[str, List[str], int]:
    """同レースの勝ち馬の「前走レース」を集計。

    例:
        🏆皐月賞: 3勝/3 (うち1回は取消経由)
        🏆青葉賞: 0勝/3
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT r.race_date, res.horse_id, h.horse_name
        FROM races r
        JOIN results res ON r.race_id = res.race_id AND res.finish_position = 1
        JOIN horses h ON res.horse_id = h.horse_id
        WHERE r.race_name = ?
        ORDER BY r.race_date DESC
        LIMIT ?
        """,
        (race_name, years),
    )
    winners = cur.fetchall()
    if not winners:
        return (f"【{race_name} 前走パターン】", ["過去データなし"], 0)

    prev_counts = {}
    has_data = 0
    details = []
    for race_date, horse_id, horse_name in winners:
        cur.execute(
            """
            SELECT r2.race_name, res2.finish_position
            FROM results res2
            JOIN races r2 ON res2.race_id = r2.race_id
            WHERE res2.horse_id = ?
              AND r2.race_date < ?
            ORDER BY r2.race_date DESC
            LIMIT 1
            """,
            (horse_id, race_date),
        )
        prev = cur.fetchone()
        if prev:
            prev_name, prev_pos = prev
            prev_counts[prev_name] = prev_counts.get(prev_name, 0) + 1
            has_data += 1
            year = race_date[:4]
            details.append(f"{year} {horse_name} ← {prev_name}({prev_pos}着)")

    n = len(winners)
    note = _sample_note(n, years)
    title = f"【{race_name} 勝ち馬の前走{('・' + note) if note else ''}】"

    if has_data == 0:
        return (title, ["前走データ取得失敗"], 0)

    lines = []
    # 前走別集計 (頻度順)
    for prev_name, cnt in sorted(prev_counts.items(), key=lambda x: -x[1]):
        lines.append(f"🏆{prev_name}: {cnt}勝/{n}")
    # 個別 detail (上から3件のみ)
    if details:
        lines.append("---")
        lines.extend(details[:3])
    return (title, lines, n)


# ─────────────────────────────────────────────────────────
# sec_outlier_year
# ─────────────────────────────────────────────────────────
def sec_outlier_year(
    conn,
    race_name: str,
    years: int = 6,
) -> Tuple[str, List[str], int]:
    """同レースの異常配当年を検出 (5番人気以下勝利 or 万馬券)。

    例:
        🚨2024 9人気ダノンデサイル(46.6倍) — 5番人気以下勝利
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
            substr(r.race_date, 1, 4) AS year,
            h.horse_name AS winner,
            res.popularity AS pop,
            res.odds AS odds
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
    n = len(rows)
    note = _sample_note(n, years)
    title = f"【{race_name} 過去の波乱年{('・' + note) if note else ''}】"

    if n == 0:
        return (title, ["データなし"], 0)

    outliers = []
    sturdy = []
    for year, winner, pop, odds in rows:
        if pop and pop >= 5:
            outliers.append(f"🚨{year} {winner}({pop}人気/{odds:.1f}倍)")
        elif pop == 1:
            sturdy.append(year)

    lines = []
    if outliers:
        lines.extend(outliers)
        lines.append(f"→ 過去{n}年で5番人気以下勝利: {len(outliers)}回")
    else:
        lines.append(f"5番人気以下勝利なし (堅め基調)")

    if sturdy:
        lines.append(f"📊1番人気勝利: {sturdy} 計{len(sturdy)}回")

    return (title, lines, n)


# ─────────────────────────────────────────────────────────
# sec_pace_decisive
# ─────────────────────────────────────────────────────────
def sec_pace_decisive(
    conn,
    venue: str,
    surface: str,
    distance: int,
    years: int = 6,
) -> Tuple[str, List[str], int]:
    """上り3F最速馬の勝率/複勝率を返す。

    例:
        💨上がり最速馬の複勝率 73% (45/62)
    """
    cur = conn.cursor()
    from_date = f"{datetime.now().year - years}-01-01"
    # 各レースの「最速上り3F」馬を特定し、その finish_position を集計
    cur.execute(
        """
        WITH fastest AS (
            SELECT
                r.race_id,
                MIN(res.last_3f) AS min_3f
            FROM races r
            JOIN results res ON r.race_id = res.race_id
            WHERE r.surface = ? AND r.distance = ? AND r.venue = ?
              AND r.race_date >= ?
              AND res.last_3f IS NOT NULL AND res.last_3f > 0
            GROUP BY r.race_id
        )
        SELECT
            COUNT(*) AS n,
            SUM(CASE WHEN res.finish_position = 1 THEN 1 ELSE 0 END) AS wins,
            SUM(CASE WHEN res.finish_position BETWEEN 1 AND 3 THEN 1 ELSE 0 END) AS top3
        FROM fastest f
        JOIN results res ON f.race_id = res.race_id AND res.last_3f = f.min_3f
        WHERE res.finish_position > 0
        """,
        (surface, distance, venue, from_date),
    )
    row = cur.fetchone()
    if not row or not row[0]:
        return (
            f"【{venue}{surface}{distance}m 末脚分析】",
            ["DB に上り3Fデータなし"],
            0,
        )
    n, wins, top3 = row
    n = n or 0
    wins = wins or 0
    top3 = top3 or 0
    note = _sample_note(n, 0)
    title = f"【{venue}{surface}{distance}m 過去{years}年 末脚最速馬{('・' + note) if note else ''}】"

    if n < 3:
        return (title, [f"サンプル{n}件のみ"], n)

    win_pct = 100 * wins / n
    top3_pct = 100 * top3 / n
    lines = [
        f"💨上り最速馬 勝率 {win_pct:.0f}% ({wins}/{n})",
        f"💨上り最速馬 複勝率 {top3_pct:.0f}% ({top3}/{n})",
    ]
    if top3_pct >= 60:
        lines.append("→ 末脚決着型コース、上り注目")
    elif top3_pct < 40:
        lines.append("→ 末脚優位性低、前残り重視")
    return (title, lines, n)


# ─────────────────────────────────────────────────────────
# sec_jockey_recent_form
# ─────────────────────────────────────────────────────────
def sec_jockey_recent_form(
    conn,
    venue: str,
    surface: str,
    distance: int,
    top: int = 3,
    years: int = 3,  # 騎手は直近 3年で十分
    min_runs: int = 5,
) -> Tuple[str, List[str], int]:
    """指定コースで好成績の騎手TOPを返す。

    例:
        🏆ルメール 複勝率 52% (13/25)
        🏆Cデムーロ 複勝率 48% (12/25)
    """
    cur = conn.cursor()
    from_date = f"{datetime.now().year - years}-01-01"
    cur.execute(
        """
        SELECT
            j.jockey_name,
            COUNT(*) AS runs,
            SUM(CASE WHEN res.finish_position BETWEEN 1 AND 3 THEN 1 ELSE 0 END) AS top3
        FROM races r
        JOIN results res ON r.race_id = res.race_id
        JOIN jockeys j ON res.jockey_id = j.jockey_id
        WHERE r.surface = ? AND r.distance = ? AND r.venue = ?
          AND r.race_date >= ?
          AND res.finish_position > 0
          AND j.jockey_name IS NOT NULL
        GROUP BY j.jockey_name
        HAVING runs >= ?
        ORDER BY (1.0 * top3 / runs) DESC, runs DESC
        LIMIT ?
        """,
        (surface, distance, venue, from_date, min_runs, top),
    )
    rows = cur.fetchall()
    n = len(rows)
    note = _sample_note(n, top)
    title = f"【{venue}{surface}{distance}m 過去{years}年 騎手複勝率TOP{top}{('・' + note) if note else ''}】"

    if n == 0:
        return (title, [f"{min_runs}走以上の騎手なし"], 0)

    lines = []
    for jockey, runs, top3 in rows:
        pct = 100 * top3 / runs
        lines.append(f"🏆{jockey} {pct:.0f}% ({top3}/{runs})")
    return (title, lines, n)


# ─────────────────────────────────────────────────────────
# sec_post_position_bias
# ─────────────────────────────────────────────────────────
def sec_post_position_bias(
    conn,
    venue: str,
    surface: str,
    distance: int,
    years: int = 6,
) -> Tuple[str, List[str], int]:
    """枠順 (post_position) ごとの複勝率を集計し、ベスト/ワーストを返す。

    例:
        ⭐1枠 複勝率 30.6% (ベスト)
        ⚠️2枠 複勝率 18.8% (ワースト)
    """
    cur = conn.cursor()
    from_date = f"{datetime.now().year - years}-01-01"
    cur.execute(
        """
        SELECT
            res.post_position AS waku,
            COUNT(*) AS n,
            SUM(CASE WHEN res.finish_position BETWEEN 1 AND 3 THEN 1 ELSE 0 END) AS top3
        FROM races r
        JOIN results res ON r.race_id = res.race_id
        WHERE r.surface = ? AND r.distance = ? AND r.venue = ?
          AND r.race_date >= ?
          AND res.finish_position > 0
          AND res.post_position BETWEEN 1 AND 8
        GROUP BY res.post_position
        HAVING n >= 10
        ORDER BY (1.0 * top3 / n) DESC
        """,
        (surface, distance, venue, from_date),
    )
    rows = cur.fetchall()
    n_buckets = len(rows)
    title = f"【{venue}{surface}{distance}m 過去{years}年 枠順傾向】"

    if n_buckets < 2:
        return (title, [f"枠別データ不足 ({n_buckets}枠のみ)"], n_buckets)

    best = rows[0]
    worst = rows[-1]
    total_n = sum(r[1] for r in rows)
    lines = [
        f"⭐{best[0]}枠 複勝率 {100 * best[2] / best[1]:.1f}% (ベスト/{best[1]}R)",
        f"⚠️{worst[0]}枠 複勝率 {100 * worst[2] / worst[1]:.1f}% (ワースト/{worst[1]}R)",
    ]
    # 偏りが大きいか判定
    best_pct = 100 * best[2] / best[1]
    worst_pct = 100 * worst[2] / worst[1]
    if best_pct - worst_pct >= 10:
        lines.append(f"→ 枠による有利不利 大 ({best_pct - worst_pct:.0f}pt差)")
    return (title, lines, total_n)


# ─────────────────────────────────────────────────────────
# sec_dangerous_favorites
# ─────────────────────────────────────────────────────────
def sec_dangerous_favorites(
    conn,
    venue: str,
    surface: str,
    distance: int,
    grade: Optional[str] = None,
    years: int = 6,
) -> Tuple[str, List[str], int]:
    """同条件で「1番人気が飛んだ」レースの割合 + 直近の事例。

    例:
        ⚠️1番人気の飛び (5着以下): 25% (3/12)
        🚨直近事例: 2024 メイショウタバル(1人気→17着)
    """
    cur = conn.cursor()
    from_date = f"{datetime.now().year - years}-01-01"
    base_where = "r.surface = ? AND r.distance = ? AND r.venue = ? AND r.race_date >= ?"
    params = [surface, distance, venue, from_date]
    if grade:
        base_where += " AND r.grade = ?"
        params.append(grade)

    cur.execute(
        f"""
        SELECT
            COUNT(*) AS n,
            SUM(CASE WHEN res.finish_position >= 5 THEN 1 ELSE 0 END) AS flop
        FROM races r
        JOIN results res ON r.race_id = res.race_id AND res.popularity = 1
        WHERE {base_where}
        """,
        params,
    )
    n, flop = cur.fetchone()
    n = n or 0
    flop = flop or 0
    grade_label = f"{grade} " if grade else ""
    title = f"【{venue}{surface}{distance}m {grade_label}1人気の信頼性 (過去{years}年)】"

    if n < 3:
        return (title, [f"サンプル{n}件のみ (信頼性判定不可)"], n)

    flop_pct = 100 * flop / n
    lines = [
        f"⚠️1人気が飛んだ (5着以下): {flop_pct:.0f}% ({flop}/{n})",
    ]
    if flop_pct >= 30:
        lines.append("→ 1人気軸は危険、相手探し優先")
    elif flop_pct >= 15:
        lines.append("→ 1人気の質を見極める必要あり")
    else:
        lines.append("→ 1人気は信頼度高")

    # 直近の事例 (5着以下になった1人気の馬)
    cur.execute(
        f"""
        SELECT r.race_date, h.horse_name, res.finish_position
        FROM races r
        JOIN results res ON r.race_id = res.race_id AND res.popularity = 1
        JOIN horses h ON res.horse_id = h.horse_id
        WHERE {base_where} AND res.finish_position >= 5
        ORDER BY r.race_date DESC
        LIMIT 2
        """,
        params,
    )
    examples = cur.fetchall()
    for race_date, name, pos in examples:
        year = race_date[:4]
        lines.append(f"🚨{year} {name}(1人気→{pos}着)")
    return (title, lines, n)


# ─────────────────────────────────────────────────────────
# sec_pattern_discovery
# ─────────────────────────────────────────────────────────
def sec_pattern_discovery(
    conn,
    venue: str,
    surface: str,
    distance: int,
    years: int = 6,
    min_runs: int = 5,
    target_top3_pct: float = 60.0,
) -> Tuple[str, List[str], int]:
    """指定コースで「複勝率 target_top3_pct% 超」の好相性パターンを発掘。

    現状は (種牡馬 × 枠順) のクロス集計。複合パターンを動的検出。

    例:
        🔮 ディープ系×内枠(1-3) 複勝70% (7/10)
        🔮 ハーツクライ×外枠(7-8) 複勝64% (9/14)
    """
    cur = conn.cursor()
    from_date = f"{datetime.now().year - years}-01-01"
    cur.execute(
        """
        SELECT
            h.sire,
            CASE
                WHEN res.post_position BETWEEN 1 AND 3 THEN '内枠(1-3)'
                WHEN res.post_position BETWEEN 4 AND 5 THEN '中枠(4-5)'
                WHEN res.post_position BETWEEN 6 AND 8 THEN '外枠(6-8)'
                ELSE '大外'
            END AS waku_band,
            COUNT(*) AS runs,
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
        HAVING runs >= ? AND (1.0 * top3 / runs) >= ?
        ORDER BY (1.0 * top3 / runs) DESC, runs DESC
        LIMIT 5
        """,
        (
            surface,
            distance,
            venue,
            from_date,
            min_runs,
            target_top3_pct / 100,
        ),
    )
    rows = cur.fetchall()
    n = len(rows)
    title = f"【{venue}{surface}{distance}m 過去{years}年 好相性パターン (複勝{target_top3_pct:.0f}%超)】"

    if n == 0:
        return (
            title,
            [f"{min_runs}走以上で {target_top3_pct:.0f}% 超のパターンなし"],
            0,
        )

    lines = []
    for sire, band, runs, top3 in rows:
        pct = 100 * top3 / runs
        lines.append(f"🔮 {sire}×{band} 複勝{pct:.0f}% ({top3}/{runs})")
    return (title, lines, n)


# ─────────────────────────────────────────────────────────
# sec_rotation_pattern
# ─────────────────────────────────────────────────────────
def sec_rotation_pattern(
    conn,
    race_name: str,
    years: int = 6,
) -> Tuple[str, List[str], int]:
    """同レース勝ち馬の「前走からの間隔」(中N週) を集計。

    例:
        🏆中4週: 2勝/3
        🏆中8週: 1勝/3
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT r.race_date, res.horse_id, h.horse_name
        FROM races r
        JOIN results res ON r.race_id = res.race_id AND res.finish_position = 1
        JOIN horses h ON res.horse_id = h.horse_id
        WHERE r.race_name = ?
        ORDER BY r.race_date DESC
        LIMIT ?
        """,
        (race_name, years),
    )
    winners = cur.fetchall()
    if not winners:
        return (f"【{race_name} ローテーション】", ["過去データなし"], 0)

    intervals = []
    for race_date, horse_id, horse_name in winners:
        cur.execute(
            """
            SELECT r2.race_date
            FROM results res2
            JOIN races r2 ON res2.race_id = r2.race_id
            WHERE res2.horse_id = ? AND r2.race_date < ?
            ORDER BY r2.race_date DESC
            LIMIT 1
            """,
            (horse_id, race_date),
        )
        prev = cur.fetchone()
        if prev:
            try:
                d1 = datetime.strptime(race_date[:10], "%Y-%m-%d")
                d2 = datetime.strptime(prev[0][:10], "%Y-%m-%d")
                diff_days = (d1 - d2).days
                week = (diff_days - 7) // 7  # 中N週
                if week <= 0:
                    label = "連闘"
                elif week <= 1:
                    label = "中1週"
                elif week <= 8:
                    label = f"中{week}週"
                else:
                    label = "中9週以上"
                intervals.append((label, horse_name, race_date[:4]))
            except Exception:
                continue

    n = len(winners)
    note = _sample_note(n, years)
    title = f"【{race_name} 勝ち馬のローテ{('・' + note) if note else ''}】"

    if not intervals:
        return (title, ["前走日付取得失敗"], 0)

    # 中N週別に集計
    counts = {}
    for label, _, _ in intervals:
        counts[label] = counts.get(label, 0) + 1
    lines = []
    for label, cnt in sorted(counts.items(), key=lambda x: -x[1]):
        lines.append(f"🏆{label}: {cnt}勝/{n}")
    # ヒント
    if counts:
        top_label = max(counts, key=counts.get)
        lines.append(f"→ {top_label} が最頻パターン")
    return (title, lines, n)


# ─────────────────────────────────────────────────────────
# sec_age_pattern
# ─────────────────────────────────────────────────────────
def sec_age_pattern(
    conn,
    venue: str,
    surface: str,
    distance: int,
    grade: Optional[str] = None,
    years: int = 6,
) -> Tuple[str, List[str], int]:
    """同条件で馬齢別の複勝率を集計。

    クラシック (3歳限定) なら不要だが、古馬混合戦では「4歳が強い」等が出る。

    例:
        🐴4歳 複勝率 38% (16/42)
        🐴5歳 複勝率 30% (12/40)
    """
    cur = conn.cursor()
    from_date = f"{datetime.now().year - years}-01-01"
    base_where = "r.surface = ? AND r.distance = ? AND r.venue = ? AND r.race_date >= ?"
    params = [surface, distance, venue, from_date]
    if grade:
        base_where += " AND r.grade = ?"
        params.append(grade)

    # 馬齢計算: race_date.year - horse.birth_year + 1
    cur.execute(
        f"""
        SELECT
            (CAST(substr(r.race_date, 1, 4) AS INTEGER) - h.birth_year + 1) AS age,
            COUNT(*) AS n,
            SUM(CASE WHEN res.finish_position BETWEEN 1 AND 3 THEN 1 ELSE 0 END) AS top3
        FROM races r
        JOIN results res ON r.race_id = res.race_id
        JOIN horses h ON res.horse_id = h.horse_id
        WHERE {base_where}
          AND res.finish_position > 0
          AND h.birth_year IS NOT NULL AND h.birth_year > 0
        GROUP BY age
        HAVING n >= 10 AND age BETWEEN 3 AND 9
        ORDER BY (1.0 * top3 / n) DESC
        """,
        params,
    )
    rows = cur.fetchall()
    n_buckets = len(rows)
    grade_label = f"{grade} " if grade else ""
    title = f"【{venue}{surface}{distance}m {grade_label}馬齢別 (過去{years}年)】"

    if n_buckets == 0:
        return (title, [f"年齢別データなし"], 0)

    lines = []
    total_n = 0
    for age, runs, top3 in rows[:3]:
        pct = 100 * top3 / runs
        lines.append(f"🐴{age}歳 複勝率 {pct:.0f}% ({top3}/{runs})")
        total_n += runs
    return (title, lines, total_n)
