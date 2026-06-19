#!/usr/bin/env python3
"""#86: 投稿セクションの「血統は父+母父」「%はラベル付き」を機械的に保証するガード。

## 背景
ユーザーから繰り返し「父だけで判断するな・母父まで出せ」「なんの%か分からん」と指摘され、
その都度 1 section だけ直して他を漏らし「何回も修正したのに治らない」を繰り返した (#86)。
「気をつける」では再発するので、出走馬を実名で出す全 section が
  (1) 母父 (母父データがある馬では) を表示し、
  (2) %の意味 (当コース複勝率/3着内率) を legend で明示する
ことをコードレベルで検査し、違反なら exit 1 で止める。

## 使い方
    python3 scripts/verify_post_sections.py [--date YYYY-MM-DD]
出走馬+母父データが揃った直近の重賞を 1 つ選び、対象 section を生成して検査する。
母父データが DB に無い (血統未補完) 場合は検査をスキップ (False negative 回避)。

CI / commit 前に実行する想定。返り値 0=OK / 1=違反。
"""
import sys
import sqlite3
import argparse

sys.path.insert(0, ".")
from database import get_db  # noqa: E402
import post_sections as ps  # noqa: E402

# %の意味を示す legend に必ず含まれるべき語 (どれか1つ)
RATE_WORDS = ("複勝率", "3着内率", "3着以内")
PEDIGREE_LABEL = "母父"


def _pick_race(conn):
    """出走馬に母父データが2頭以上ある直近の重賞を1つ返す (検査対象)。"""
    rows = conn.execute(
        """
        SELECT r.race_id, r.race_name, r.venue, r.surface, r.distance, r.race_date,
               SUM(CASE WHEN h.damsire IS NOT NULL AND h.damsire != '' THEN 1 ELSE 0 END) AS dam_n
        FROM races r
        JOIN results res ON res.race_id = r.race_id
        JOIN horses h ON h.horse_id = res.horse_id
        WHERE r.race_number = 11
        GROUP BY r.race_id
        HAVING dam_n >= 2
        ORDER BY r.race_date DESC
        LIMIT 1
        """
    ).fetchone()
    return dict(row_to_dict(rows)) if rows else None


def row_to_dict(row):
    return {k: row[k] for k in row.keys()} if row else None


def _check_section(label, title, lines, require_pedigree):
    """1 section の (title, lines) を検査。違反メッセージのリストを返す。"""
    problems = []
    blob = title + "\n" + "\n".join(lines)
    has_father = "父" in blob
    # require_pedigree=True の section は、父を出すなら母父も必ず出す
    if require_pedigree and has_father and PEDIGREE_LABEL not in blob:
        problems.append(
            f"🚫 [{label}] 父は出しているのに母父({PEDIGREE_LABEL})が無い "
            f"— 『父だけで判断するな』違反 (#86)")
    # %を出しているのに、その意味の legend が無い
    if "%" in blob and not any(w in blob for w in RATE_WORDS):
        problems.append(
            f"🚫 [{label}] %を出しているのに『複勝率/3着内率』の説明が無い "
            f"— 『なんの%か分からん』違反 (#86)")
    return problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", help="対象レース日 (省略時は母父データのある直近重賞)")
    args = ap.parse_args()

    with get_db() as conn:
        conn.row_factory = sqlite3.Row
        race = _pick_race(conn)
        if not race:
            print("ℹ️ 母父データが揃った重賞が無い (血統未補完) → 検査スキップ")
            return 0
        rid, v, s, d = race["race_id"], race["venue"], race["surface"], race["distance"]
        print(f"🔎 検査対象: {race['race_date']} {race['venue']} {race['race_name']} ({v}{s}{d}m)")

        problems = []
        # コース適性TOP (#87: 鞍上主役に転換、母父は際立つ時のみ → 母父必須にしない)
        t, lines, _ = ps.sec_handpicked_top(conn, rid, v, s, d, top=3)
        problems += _check_section("コース適性TOP", t, lines, require_pedigree=False)
        # 注目馬: blood=血統テーマなので母父必須 / combined・jockey は母父任意 (#87:
        #   総合まで母父リードにすると「血統ばっかり」になるため、母父深掘りは blood に集約)。
        for mode, req in (("combined", False), ("blood", True), ("jockey", False)):
            t, lines, _ = ps.sec_notable_horses(conn, rid, v, s, d, mode=mode, top=3)
            problems += _check_section(f"注目馬:{mode}", t, lines, require_pedigree=req)
        # 血統深層 (元から母父あり、回帰監視)
        t, lines, _ = ps.sec_pedigree_deep(conn, rid, v, s, d, top=3)
        problems += _check_section("血統深層", t, lines, require_pedigree=True)

    if problems:
        print("\n".join(problems))
        print(f"\n❌ {len(problems)}件の違反 — commit 前に修正してください")
        return 1
    print("✅ 全 section で『父+母父』『%ラベル』を確認")
    return 0


if __name__ == "__main__":
    sys.exit(main())
