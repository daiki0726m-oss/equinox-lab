#!/usr/bin/env python3
"""#84: keiba.db を commit する前に、origin/main の「未来レース」を local に退避マージする。

## 背景 (2026-06-17 の事故)
keiba.db.gz は git 管理の binary 正本だが、9+ workflow が並行で read-modify-write する。
push 時の `git pull --rebase -X theirs` (ローカル優先) や、run 作成時に固定される stale checkout
により、**古い DB が新しい DB を巻き戻す**事故が起きる。
実際に 6/17 朝に登録した 6/20-21 レースが昼までに消え (09:25 の run が古い DB を commit)、
レース選定が過去フォールバックして先週の函館スプリントS(6/13) を今週予告として誤投稿した。

## 対策 (構造的遮断)
commit 直前 (db_pack.sh) に origin/main の keiba.db.gz を取り出し、local に**欠けている未来レース**
(race_date >= 今日JST) を補填する。純加算 (INSERT) で既存行は一切変更しないため、
「未来レースが origin より減った DB を commit する」ことを**構造的に不可能**にする。
→ どんな git 競合 (stale checkout / -X theirs / 並行 push) が起きても未来レースは消えない。

## 安全設計
- local に**完全に存在しないレースのみ**補填 (results に (race_id,horse_id) UNIQUE が無く
  部分マージは重複を生むため、レース単位の all-or-nothing にする)。
- results は id(AUTOINCREMENT) を除いて挿入 (local の id 連番と衝突させない)。
- 失敗しても deploy を止めないよう、全例外を握りつぶして exit 0。
- CI (GITHUB_ACTIONS) 以外では何もしない (db_pack.sh 側でガード)。
"""
import sys
import gzip
import sqlite3
import subprocess
from datetime import datetime, timezone, timedelta

ORIGIN_TMP = "/tmp/_origin_keiba_guard.db"


def merge_future_races(local_path: str, origin_path: str, today_jst: str) -> list:
    """origin_path の未来レースのうち local_path に無いものを補填。補填したレース一覧を返す。"""
    conn = sqlite3.connect(local_path, timeout=30)
    try:
        conn.execute(f"ATTACH '{origin_path}' AS o")
        # local に存在しない未来レース (race_date >= today) を特定
        missing = [r[0] for r in conn.execute(
            "SELECT race_id FROM o.races WHERE race_date >= ? "
            "AND race_id NOT IN (SELECT race_id FROM races)", (today_jst,))]
        if not missing:
            return []
        ph = ",".join("?" * len(missing))
        # races 本体 (同一スキーマなので SELECT *)
        conn.execute(
            f"INSERT OR IGNORE INTO races SELECT * FROM o.races WHERE race_id IN ({ph})", missing)
        # results は id を除いて挿入 (race_id,horse_id に UNIQUE が無いので missing レース限定で重複回避)
        rcols = [c[1] for c in conn.execute("PRAGMA table_info(results)").fetchall() if c[1] != 'id']
        rcols_sql = ",".join(rcols)
        conn.execute(
            f"INSERT INTO results ({rcols_sql}) "
            f"SELECT {rcols_sql} FROM o.results WHERE race_id IN ({ph})", missing)
        # horses (出走馬マスタ) も補填
        conn.execute(
            f"INSERT OR IGNORE INTO horses SELECT * FROM o.horses WHERE horse_id IN "
            f"(SELECT DISTINCT horse_id FROM o.results WHERE race_id IN ({ph}))", missing)
        conn.commit()
        rows = conn.execute(
            f"SELECT race_date, race_name FROM races WHERE race_id IN ({ph}) ORDER BY race_date, race_name",
            missing).fetchall()
        return rows
    finally:
        conn.close()


def main():
    today_jst = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime('%Y-%m-%d')
    # origin/main の keiba.db.gz を取り出す (db_pack.sh 側で git fetch 済み前提)
    try:
        blob = subprocess.run(['git', 'show', 'origin/main:keiba.db.gz'],
                              capture_output=True, timeout=120).stdout
    except Exception as e:
        print(f"🛡 future-race guard: origin DB 取得不可 ({e}) → skip")
        return
    if not blob:
        print("🛡 future-race guard: origin DB 空/未取得 → skip")
        return
    try:
        with open(ORIGIN_TMP, 'wb') as f:
            f.write(gzip.decompress(blob))
    except Exception as e:
        print(f"🛡 future-race guard: 展開失敗 ({e}) → skip")
        return
    try:
        added = merge_future_races('keiba.db', ORIGIN_TMP, today_jst)
    except Exception as e:
        print(f"🛡 future-race guard: マージ失敗 ({e}) → skip (deploy は継続)")
        return
    if not added:
        print("🛡 future-race guard: 欠損なし (local は origin の未来レースを完全保持)")
    else:
        print(f"🛡 future-race guard: origin から未来レース {len(added)}件を退避マージ (巻き戻し阻止):")
        for d, n in added:
            print(f"    + {d} {n}")


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print(f"🛡 future-race guard: 予期せぬエラー ({e}) → skip")
    sys.exit(0)  # 絶対に deploy を止めない
