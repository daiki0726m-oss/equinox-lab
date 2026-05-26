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


def _get_entries(conn, race_id: str) -> list:
    """出走馬リスト取得 (predictions_cache 優先、なければ results から)。

    Returns:
        [{"horse_id", "horse_name", "horse_number", "sire", "jockey_name"}, ...]
        空リストなら「未確定」と扱う。
    """
    if not race_id:
        return []
    cur = conn.cursor()
    # ① predictions_cache から (確実)
    cur.execute("SELECT predictions_json FROM predictions_cache WHERE race_id=?", (race_id,))
    row = cur.fetchone()
    if row and row[0]:
        try:
            import json as _json
            preds = _json.loads(row[0])
            if preds:
                # sire を horses テーブルから補完
                entries = []
                for p in preds:
                    name = p.get("horse_name", "")
                    cur.execute("SELECT horse_id, sire FROM horses WHERE horse_name = ? LIMIT 1", (name,))
                    h = cur.fetchone()
                    entries.append({
                        "horse_id": h[0] if h else None,
                        "horse_name": name,
                        "horse_number": p.get("horse_number") or 0,
                        "sire": h[1] if h else None,
                        "jockey_name": p.get("jockey_name", ""),
                    })
                return entries
        except Exception:
            pass

    # ② results から (finish_position=0 の出走予定エントリ)
    cur.execute("""
        SELECT res.horse_id, h.horse_name, res.horse_number, h.sire, j.jockey_name
        FROM results res
        JOIN horses h ON res.horse_id = h.horse_id
        LEFT JOIN jockeys j ON res.jockey_id = j.jockey_id
        WHERE res.race_id = ?
        ORDER BY res.horse_number
    """, (race_id,))
    rows = cur.fetchall()
    return [
        {"horse_id": r[0], "horse_name": r[1], "horse_number": r[2],
         "sire": r[3], "jockey_name": r[4]}
        for r in rows
    ]


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
    race_id: str = None,
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

    # 出走馬リスト (race_id 指定時のみ) — 該当馬抽出用
    entries = _get_entries(conn, race_id) if race_id else []
    entries_by_sire = {}  # sire (前方一致用 prefix) → [horse names...]
    for e in entries:
        s = (e.get("sire") or "").split("(")[0].strip()
        if s:
            entries_by_sire.setdefault(s, []).append(e)

    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, (sire, runs, top3) in enumerate(rows):
        pct = 100 * top3 / runs
        m = medals[i] if i < len(medals) else "🏅"
        sire_clean = sire.split("(")[0].strip()
        # 該当出走馬を抽出
        matched = entries_by_sire.get(sire_clean, [])
        match_str = ""
        if entries:  # 出走馬データあり = 該当を表示
            if matched:
                names = [e["horse_name"] for e in matched[:2]]
                match_str = f" → 該当: {' '.join(names)}"
            else:
                match_str = " → 該当馬なし"
        lines.append(f"{m}{sire} {pct:.0f}% ({top3}/{runs}){match_str}")
    return (title, lines, n)


# ─────────────────────────────────────────────────────────
# sec_prev_race_pattern
# ─────────────────────────────────────────────────────────
def sec_prev_race_pattern(
    conn,
    race_name: str,
    years: int = 6,
    race_id: str = None,
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
    sorted_prev = sorted(prev_counts.items(), key=lambda x: -x[1])
    top_prev_name, top_prev_cnt = sorted_prev[0]
    coverage_pct = 100 * top_prev_cnt / n

    if coverage_pct == 100:
        lines.append(f"🎯 過去{n}年全勝ち馬が【{top_prev_name}】経由")
        lines.append(f"  → 他ローテで勝った馬: 0頭")
    elif coverage_pct >= 75:
        lines.append(f"🎯 {top_prev_name}経由 が {top_prev_cnt}/{n}勝 ({coverage_pct:.0f}%)")
        other = n - top_prev_cnt
        lines.append(f"  → 他ローテ計 {other}頭のみ ({100-coverage_pct:.0f}%)")
    else:
        for prev_name, cnt in sorted_prev[:3]:
            lines.append(f"🏆{prev_name}: {cnt}勝/{n}")

    # 出走馬の中で「top_prev_name 経由」の馬を特定
    if race_id:
        entries = _get_entries(conn, race_id)
        if entries:
            matched_horses = []
            for e in entries:
                hid = e.get("horse_id")
                if not hid:
                    continue
                # 各出走馬の前走レース名を取得
                cur.execute(
                    """
                    SELECT r2.race_name FROM results res2
                    JOIN races r2 ON res2.race_id = r2.race_id
                    WHERE res2.horse_id = ? AND r2.race_date < (
                      SELECT race_date FROM races WHERE race_id = ?
                    )
                    ORDER BY r2.race_date DESC LIMIT 1
                    """,
                    (hid, race_id),
                )
                prev_row = cur.fetchone()
                if prev_row and prev_row[0] == top_prev_name:
                    matched_horses.append(e["horse_name"])
            if matched_horses:
                lines.append(f"  → 該当出走馬: {' '.join(matched_horses[:3])}")
            else:
                lines.append(f"  → 該当出走馬なし ({top_prev_name}組ゼロ)")

    # 行動指針
    if coverage_pct >= 75:
        lines.append(f"→ 今年も {top_prev_name}経由馬を軸候補に")

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
        f"💨上がり最速馬 勝率 {win_pct:.0f}% ({wins}/{n})",
        f"💨上がり最速馬 複勝率 {top3_pct:.0f}% ({top3}/{n})",
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
    years: int = 3,
    min_runs: int = 5,
    race_id: str = None,
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

    # 出走馬の騎手リスト (race_id 指定時)
    entries = _get_entries(conn, race_id) if race_id else []
    entry_jockeys = set()
    for e in entries:
        jn = e.get("jockey_name") or ""
        if jn:
            # 騎手名は短縮形 (例: "ルメール" / "C.デムーロ") なので前方一致系
            entry_jockeys.add(jn.strip())

    lines = []
    for jockey, runs, top3 in rows:
        pct = 100 * top3 / runs
        match_str = ""
        if entries:
            # jockey 名と出走馬騎手名の部分一致 (姓のみ照合)
            jockey_short = jockey.replace(" ", "").strip()
            matched = any(
                jockey_short[:3] in ej or ej[:3] in jockey_short
                for ej in entry_jockeys
            )
            match_str = " ★今回騎乗" if matched else ""
        lines.append(f"🏆{jockey} {pct:.0f}% ({top3}/{runs}){match_str}")
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

    # 飛んだ1人気馬の前走着順 + 種牡馬 を抽出して「共通点」を分析
    cur.execute(
        f"""
        SELECT r.race_date, h.horse_name, h.horse_id, res.finish_position, h.sire
        FROM races r
        JOIN results res ON r.race_id = res.race_id AND res.popularity = 1
        JOIN horses h ON res.horse_id = h.horse_id
        WHERE {base_where} AND res.finish_position >= 5
        ORDER BY r.race_date DESC
        """,
        params,
    )
    flop_horses = cur.fetchall()

    if not flop_horses:
        return (title, lines, n)

    # 飛んだ1人気の特徴 (前走着順, 種牡馬) を抽出
    flop_features = []
    for race_date, name, hid, pos, sire in flop_horses:
        cur.execute(
            "SELECT res2.finish_position FROM results res2 JOIN races r2 ON res2.race_id=r2.race_id "
            "WHERE res2.horse_id=? AND r2.race_date<? AND res2.finish_position>0 "
            "ORDER BY r2.race_date DESC LIMIT 1",
            (hid, race_date),
        )
        prev_row = cur.fetchone()
        flop_features.append({
            "date": race_date, "name": name, "hid": hid, "pos": pos,
            "sire": sire, "prev_pos": prev_row[0] if prev_row else None,
        })

    # 全 1人気の母集団ベースラインを取得
    cur.execute(
        f"""
        SELECT res.horse_id, r.race_date, res.finish_position, h2.sire
        FROM races r
        JOIN results res ON r.race_id = res.race_id AND res.popularity = 1
        JOIN horses h2 ON res.horse_id = h2.horse_id
        WHERE {base_where}
        """,
        params,
    )
    all_records = []
    for hid, race_date, pos, sire in cur.fetchall():
        cur.execute(
            "SELECT res2.finish_position FROM results res2 JOIN races r2 ON res2.race_id=r2.race_id "
            "WHERE res2.horse_id=? AND r2.race_date<? AND res2.finish_position>0 "
            "ORDER BY r2.race_date DESC LIMIT 1",
            (hid, race_date),
        )
        prev_row = cur.fetchone()
        all_records.append({
            "pos": pos, "sire": sire,
            "prev_pos": prev_row[0] if prev_row else None,
            "flopped": pos >= 5,
        })

    # 母集団との比較で「特異な凡走パターン」を発見 (因果ではなく相関)
    # 重要: N=2件の共通点は意味なし。母集団との比率差で判断する。
    insights = []
    overall_flop_rate = flop_pct / 100  # 全体飛び率

    def _bucket_prev(p):
        if p is None: return None
        if p == 1: return "前走1着"
        if p <= 3: return "前走2-3着"
        if p <= 9: return "前走4-9着"
        return "前走10着+"

    from collections import defaultdict
    bucket_stats = defaultdict(lambda: {"total": 0, "flop": 0})
    for r in all_records:
        b = _bucket_prev(r["prev_pos"])
        if b:
            bucket_stats[b]["total"] += 1
            if r["flopped"]:
                bucket_stats[b]["flop"] += 1

    # 全体+15pt以上 凡走率が高い前走パターンを「警戒区分」として提示 (サンプル 3件以上)
    for bucket, stat in bucket_stats.items():
        if stat["total"] >= 3:
            bucket_rate = stat["flop"] / stat["total"]
            diff_pt = (bucket_rate - overall_flop_rate) * 100
            if diff_pt >= 15:
                insights.append(
                    f"※【{bucket}】の1人気は凡走率{int(bucket_rate*100)}% "
                    f"(全体{int(overall_flop_rate*100)}%より+{int(diff_pt)}pt)"
                )

    # 特異な傾向が見つからない場合は誠実に明示
    if not insights and flop > 0 and n >= 5:
        insights.append("※飛んだ事例に共通の傾向なし — 個別要因の凡走、再現性低い")

    # 共通点 (insight) を先に出す
    for ins in insights:
        lines.append(ins)

    # アクション指針
    if any("凡走率" in ins for ins in insights):
        lines.append("→ 該当する前走パターンの1人気は割引判断を")

    # 直近の事例 (末尾、budget 超過時に最初に trim)
    for f in flop_features[:2]:
        year = f["date"][:4]
        prev_info = f"※前走{f['prev_pos']}着" if f["prev_pos"] else ""
        lines.append(f"🚨{year} {f['name']}({f['pos']}着) {prev_info}".rstrip())

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
    race_id: str = None,
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

    # 出走馬の血統 × 枠順 でクロスマッチ (race_id 指定時)
    entries_by_sire = {}
    if race_id:
        entries = _get_entries(conn, race_id)
        for e in entries:
            s = (e.get("sire") or "").split("(")[0].strip()
            if s:
                entries_by_sire.setdefault(s, []).append(e)

    lines = []
    for sire, band, runs, top3 in rows:
        pct = 100 * top3 / runs
        sire_clean = sire.split("(")[0].strip()
        match_str = ""
        if entries_by_sire and sire_clean in entries_by_sire:
            names = [e["horse_name"] for e in entries_by_sire[sire_clean]]
            if len(names) <= 2:
                match_str = f" → 該当: {' '.join(names)}"
            else:
                match_str = f" → 該当: {names[0]}他{len(names)-1}頭"
        lines.append(f"🔮 {sire}×{band} 複勝{pct:.0f}% ({top3}/{runs}){match_str}")
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

    # 中N週別に集計 — 「N勝/M」だけでなく「含意」を併記
    counts = {}
    for label, _, _ in intervals:
        counts[label] = counts.get(label, 0) + 1

    sorted_counts = sorted(counts.items(), key=lambda x: -x[1])
    top_label, top_cnt = sorted_counts[0]
    coverage_pct = 100 * top_cnt / n

    lines = []
    if coverage_pct == 100:
        lines.append(f"🎯 過去{n}年全勝ち馬が【{top_label}】")
        lines.append(f"  → 他間隔の勝ち馬: 0頭")
        lines.append(f"→ {top_label} 以外のローテは不利")
    elif coverage_pct >= 75:
        lines.append(f"🎯 {top_label} が {top_cnt}/{n}勝 ({coverage_pct:.0f}%)")
        # その他のローテも併記
        others = [f"{l}{c}" for l, c in sorted_counts[1:]]
        if others:
            lines.append(f"  他: {' / '.join(others)}")
        lines.append(f"→ {top_label} 軸が王道")
    else:
        # 上位3つを並列表示
        for label, cnt in sorted_counts[:3]:
            lines.append(f"🏆{label}: {cnt}勝/{n}")

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


# ─────────────────────────────────────────────────────────
# sec_workout_focus (水昼: 追い切り情報) — v2 改修
# ─────────────────────────────────────────────────────────
def sec_workout_focus(
    conn,
    race_id: str,
    top: int = 4,
) -> Tuple[str, List[str], int]:
    """同レースの追い切り評価が高い馬 (A/B grade) を抽出。

    出典: netkeiba 追い切り評価 (専門編集部による総合的な動き評価)。
    タイムだけでなく、馬の気配・コーナリング・反応速度等を加味した
    5段階の編集部評価 (A=非常に良い 〜 C=並)。

    v2 追加: 過去6年の A評価馬の実複勝率を併記して、評価の信頼性を
    数値で示す。

    例:
        🥇ドリームコア A (気配抜群)
        🥇ラフターラインズ A (文句なし)
        ※追い切り評価 A: 過去6年で複勝率 38% (n=420)
        ※出典: netkeiba 追い切り評価
    """
    cur = conn.cursor()
    # 該当レース情報を取得 (race_date)
    cur.execute("SELECT race_date FROM races WHERE race_id = ?", (race_id,))
    rd_row = cur.fetchone()
    race_date = rd_row[0] if rd_row else None

    cur.execute(
        """
        SELECT w.horse_number, h.horse_name, w.evaluation_grade, w.evaluation_text
        FROM workouts w
        JOIN horses h ON w.horse_id = h.horse_id
        WHERE w.race_id = ?
          AND w.evaluation_grade IN ('A', 'B')
        ORDER BY
          CASE w.evaluation_grade WHEN 'A' THEN 0 WHEN 'B' THEN 1 ELSE 2 END,
          w.horse_number
        LIMIT ?
        """,
        (race_id, top),
    )
    rows = cur.fetchall()
    n = len(rows)
    title = "【追い切り評価 (netkeiba 編集部)】"
    if n == 0:
        return (title, ["追い切り評価データなし"], 0)

    # 馬リスト (馬番は呼び出し側で _horse_label を使うべきだが、
    # 互換のため horse_number + name を返す)
    lines = []
    for hn, name, grade, text in rows:
        emoji = "🥇" if grade == "A" else "🥈"
        text_short = (text or "").strip()[:8]
        # 馬番は呼出側で剥がせる形 (構造化)
        lines.append({"emoji": emoji, "hn": hn, "name": name, "grade": grade, "text": text_short})

    # A評価馬の過去6年複勝率を計算 (信頼性メトリック)
    cur.execute(
        """
        SELECT
          COUNT(*) AS total,
          SUM(CASE WHEN res.finish_position BETWEEN 1 AND 3 THEN 1 ELSE 0 END) AS top3
        FROM workouts w
        JOIN results res ON w.race_id = res.race_id AND w.horse_id = res.horse_id
        WHERE w.evaluation_grade = 'A'
          AND res.finish_position > 0
        """
    )
    a_stat = cur.fetchone()
    a_top3_pct = None
    a_n = 0
    if a_stat and a_stat[0]:
        a_n, a_top3 = a_stat
        if a_n >= 30:
            a_top3_pct = 100 * a_top3 / a_n

    # 統計コンテキスト (行頭、trim されにくい位置に)
    str_lines = []
    if a_top3_pct is not None:
        str_lines.append(f"※過去6年のA評価馬の複勝率 {a_top3_pct:.0f}% (n={a_n})")

    # 馬リスト
    for l in lines:
        str_lines.append(f"{l['emoji']}{l['hn']}番 {l['name']} {l['grade']}({l['text']})")

    return (title, str_lines, n)


# ─────────────────────────────────────────────────────────
# sec_entry_pedigree_match (木朝: 出走馬の血統×コース実績)
# ─────────────────────────────────────────────────────────
def sec_entry_pedigree_match(
    conn,
    race_id: str,
    venue: str,
    surface: str,
    distance: int,
    top: int = 4,
    years: int = 6,
    min_runs: int = 3,
) -> Tuple[str, List[str], int]:
    """出走馬のうち、種牡馬がこのコースで複勝率高い馬を抽出。

    predictions_cache から出走馬を取り、各馬の sire を horses テーブルで lookup、
    その sire が venue/surface/distance で過去6年複勝何%か計算。

    例:
        🩸17番 シャインローズ (フィエールマン産駒 50%)
        🩸03番 ロイヤルクラウン (コントレイル産駒 50%)
    """
    cur = conn.cursor()
    # 出走馬リスト (predictions_cache 経由)
    cur.execute(
        "SELECT predictions_json FROM predictions_cache WHERE race_id = ?",
        (race_id,),
    )
    row = cur.fetchone()
    if not row:
        return ("【出走馬×血統】", ["出走馬データなし(予測キャッシュ無し)"], 0)
    import json as _json
    try:
        horses_list = _json.loads(row[0])
    except Exception:
        return ("【出走馬×血統】", ["出走馬データ読み込み失敗"], 0)

    if not horses_list:
        return ("【出走馬×血統】", ["出走馬なし"], 0)

    from_date = f"{datetime.now().year - years}-01-01"

    # 出走馬の horse_number → name → sire を引いて、種牡馬の複勝率を計算
    matches = []
    for h in horses_list:
        hn = h.get("horse_number")
        name = h.get("horse_name", "")
        if not name:
            continue
        cur.execute(
            "SELECT sire FROM horses WHERE horse_name = ? LIMIT 1",
            (name,),
        )
        sire_row = cur.fetchone()
        if not sire_row or not sire_row[0]:
            continue
        sire = sire_row[0]
        # この種牡馬がこのコースで何回走って複勝何%か
        cur.execute(
            """
            SELECT COUNT(*) AS runs,
                   SUM(CASE WHEN res.finish_position BETWEEN 1 AND 3 THEN 1 ELSE 0 END) AS top3
            FROM races r
            JOIN results res ON r.race_id = res.race_id
            JOIN horses h2 ON res.horse_id = h2.horse_id
            WHERE r.surface = ? AND r.distance = ? AND r.venue = ?
              AND r.race_date >= ?
              AND res.finish_position > 0
              AND h2.sire = ?
            """,
            (surface, distance, venue, from_date, sire),
        )
        stat = cur.fetchone()
        if not stat or not stat[0]:
            continue
        runs, top3 = stat
        if runs < min_runs:
            continue
        pct = 100 * top3 / runs
        if pct >= 35:  # 複勝率 35% 以上のみ
            matches.append((pct, hn, name, sire, runs, top3))

    matches.sort(reverse=True)
    title = f"【出走馬×血統 (このコース複勝率35%超)】"
    if not matches:
        return (title, ["該当馬なし"], 0)

    # 出典 + 客観値を行頭に
    lines = ["※父産駒の当コース複勝率 (過去6年)"]
    for pct, hn, name, sire, runs, top3 in matches[:top]:
        sire_short = sire.split("(")[0][:10]  # "(米)" 等を削る、長め確保
        lines.append(f"🩸{hn}番 {name} — 父{sire_short}産駒 {pct:.0f}%複勝 ({top3}/{runs}件)")
    return (title, lines, len(matches))


# ─────────────────────────────────────────────────────────
# sec_attention_top (木昼/木夜/金昼: 注目馬 + 客観データ) — v2
# ─────────────────────────────────────────────────────────
def sec_attention_top(
    conn,
    race_id: str,
    top: int = 3,
    include_marks: bool = False,
) -> Tuple[str, List[str], int]:
    """注目馬 + 客観データ (AI予測勝率 / SI / 直近成績 / 父産駒のコース実績)。

    各馬で表示するデータ:
    - AI予測勝率 (LightGBM 出力、過去レース結果から学習)
    - SI: Speed Index 過去レース平均 (netkeiba 算出のスピード指数)
    - 直近5走の3着内率
    - 父産駒の当該コース複勝率 (DB 集計)

    include_marks=False: 「1️⃣ 2️⃣ 3️⃣」表記 (注目馬として)
    include_marks=True: 「◎ ○ ▲」表記 (確定買い目)

    例:
        1️⃣ スターアニス (SI93、AI勝率18% / 父エピファネイア 当コース40%)
        2️⃣ ドリームコア (SI85、AI勝率14% / 直近5走3着内80%)
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT predictions_json FROM predictions_cache WHERE race_id = ?",
        (race_id,),
    )
    row = cur.fetchone()
    title = "【AI注目馬】"
    if not row:
        return (title, ["予測キャッシュなし"], 0)
    import json as _json
    try:
        horses_list = _json.loads(row[0])
    except Exception:
        return (title, ["予測データ読み込み失敗"], 0)
    if not horses_list:
        return (title, ["馬データなし"], 0)

    # レース条件 (種牡馬照合用)
    cur.execute(
        "SELECT venue, surface, distance FROM races WHERE race_id = ?",
        (race_id,),
    )
    race_row = cur.fetchone()
    venue = race_row[0] if race_row else ""
    surface = race_row[1] if race_row else ""
    distance = race_row[2] if race_row else 0

    # AI予測勝率順に並べる
    sorted_h = sorted(
        horses_list, key=lambda x: -(x.get("pred_win_pct", 0) or 0)
    )[:top]

    rank_marks = ["1️⃣", "2️⃣", "3️⃣", "4️⃣"]
    legacy_marks = ["◎", "○", "▲", "△"]

    lines = []
    for i, h in enumerate(sorted_h):
        name = h.get("horse_name", "?")
        hn = h.get("horse_number") or 0
        pred_win = h.get("pred_win_pct", 0) or 0
        si = h.get("si_avg", 0) or 0
        top3_rate = h.get("top3_rate", 0) or 0  # 直近10R 複勝率
        mark = legacy_marks[i] if include_marks else rank_marks[i]

        # 客観データ 2 つに厳選 (文字数節約のため):
        # - AI勝率は必須 (最重要)
        # - 第二指標: 父産駒のコース実績 > SI > 直近複勝率 の優先順
        facts = []
        facts.append(f"AI勝率{pred_win:.0f}%")

        # 第二指標を選定
        cur.execute(
            "SELECT sire FROM horses WHERE horse_name = ? LIMIT 1", (name,)
        )
        sire_row = cur.fetchone()
        second_fact = None
        if sire_row and sire_row[0] and venue and distance:
            sire = sire_row[0]
            cur.execute(
                """
                SELECT COUNT(*) runs, SUM(CASE WHEN res.finish_position BETWEEN 1 AND 3 THEN 1 ELSE 0 END) top3
                FROM races r
                JOIN results res ON r.race_id = res.race_id
                JOIN horses h2 ON res.horse_id = h2.horse_id
                WHERE r.surface = ? AND r.distance = ? AND r.venue = ?
                  AND r.race_date >= ?
                  AND h2.sire = ?
                  AND res.finish_position > 0
                """,
                (surface, distance, venue, f"{datetime.now().year - 6}-01-01", sire),
            )
            ss = cur.fetchone()
            if ss and ss[0] and ss[0] >= 4:
                runs, top3 = ss
                pct = 100 * top3 / runs
                if pct >= 30:  # 30% 以上のみ意味あり
                    sire_short = sire.split("(")[0][:7]
                    second_fact = f"父{sire_short}産駒{pct:.0f}%"

        if not second_fact and si > 0:
            second_fact = f"SI{si:.0f}"
        elif not second_fact and top3_rate >= 50:
            second_fact = f"直近複勝{top3_rate:.0f}%"

        if second_fact:
            facts.append(second_fact)

        lines.append({"mark": mark, "hn": hn, "name": name, "facts": facts})

    # 文字列化 (出典は行頭に置いて trim されにくくする)
    str_lines = ["※AI勝率=LightGBM予測 / SI=過去レース勝率指数"]
    for l in lines:
        label = f"{l['hn']}番 {l['name']}" if l['hn'] else l['name']
        facts_str = " / ".join(l['facts'])
        str_lines.append(f"{l['mark']} {label} ({facts_str})")

    return (title if not include_marks else "【AI最終予想 ◎○▲】", str_lines, len(lines))


# 互換: 旧名 sec_eight_axis_top を別名で残す (deprecated)
def sec_eight_axis_top(conn, race_id, top=4):
    """[非推奨] sec_attention_top を使用してください (より客観的データ)"""
    return sec_attention_top(conn, race_id, top=min(top, 3), include_marks=False)


# ─────────────────────────────────────────────────────────
# sec_top3_with_reasons (木夜/金昼: ◎○▲ と決定的理由)
# ─────────────────────────────────────────────────────────
def sec_top3_with_reasons(
    conn,
    race_id: str,
) -> Tuple[str, List[str], int]:
    """予測キャッシュから ◎○▲ と各馬の reasons を抽出。

    例:
        ◎17番 シャインローズ — 勝率トップで信頼度高
        ○05番 タスティエーラ — 直近複勝80%で堅実
        ▲03番 ロイヤルクラウン — 末脚最速で末脚決着型
    """
    cur = conn.cursor()
    cur.execute(
        "SELECT predictions_json FROM predictions_cache WHERE race_id = ?",
        (race_id,),
    )
    row = cur.fetchone()
    if not row:
        return ("【AI印 ◎○▲】", ["予測キャッシュなし"], 0)
    import json as _json
    try:
        horses_list = _json.loads(row[0])
    except Exception:
        return ("【AI印 ◎○▲】", ["予測データ読み込み失敗"], 0)
    if not horses_list:
        return ("【AI印 ◎○▲】", ["馬データなし"], 0)

    # ◎○▲ の馬を取得
    title = "【AI印 ◎○▲】"
    lines = []
    for mk in ["◎", "○", "▲"]:
        h = next((x for x in horses_list if x.get("mark") == mk), None)
        if not h:
            continue
        hn = h.get("horse_number", 0)
        name = h.get("horse_name", "?")[:9]
        reasons = h.get("reasons", []) or []
        reason = reasons[0] if reasons else "AI印馬"
        # 文字数節約
        reason_short = reason[:14]
        lines.append(f"{mk}{hn}番 {name} — {reason_short}")
    if not lines:
        return (title, ["印馬未確定"], 0)
    return (title, lines, len(lines))
