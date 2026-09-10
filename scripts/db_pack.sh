#!/bin/bash
# keiba.db (作業ファイル) → keiba.db.gz (git管理用) に圧縮 (#64)
# -n: gzip ヘッダから timestamp を除去 = 内容が同一ならバイト同一 (no-op commit 防止)
set -e

# 🛡 #84: commit 直前に origin/main の「未来レース」を local に退避マージ。
#   9+ workflow が並行で keiba.db.gz を read-modify-write し、push 時の -X theirs
#   (ローカル優先) や run 作成時固定の stale checkout により、古い DB が新しい DB を
#   巻き戻す事故が起きる (2026-06-17: 朝登録の 6/20-21 レースが昼までに消え、
#   先週の函館スプリントS=6/13 を今週予告として誤投稿)。
#   全 db_pack 呼び出し (= 全 commit 経路) でガードを通し、「未来レースが origin より
#   減った DB を commit する」ことを構造的に不可能にする。純加算 (local に無い未来レース
#   のみ補填) なので既存データは不変。CI でのみ実行 (ローカル開発では no-op)。
if [ -n "$GITHUB_ACTIONS" ]; then
  git fetch origin main --depth 1 >/dev/null 2>&1 || true
  python3 scripts/db_guard_future_races.py || true
fi

sqlite3 keiba.db "PRAGMA wal_checkpoint(TRUNCATE);" 2>/dev/null || true

# 📉 #153 (2026-09-10): 索引を落として VACUUM した**コピー**を固める。
#   索引は派生データで、展開側で 0.6 秒で貼り直せる。実測 120MB 中 38MB が索引で、
#   gz は 33.1MB → 21.4MB (-35%)。リポジトリが 79.4GB に達し GitHub が容量警告を
#   返しているため、1コミットあたりのバイト数を直接削る。
#   作業ファイル keiba.db 自体は触らない (同じ job の後続ステップが索引付きのまま使える)。
#   落とした索引の DDL は _pack_meta に入れて運ぶので、展開側が元どおり復元できる。
PACKED=""
if python3 - <<'PY'
import os, shutil, sqlite3, sys
src, tmp = "keiba.db", "keiba.db.pack.tmp"
if not os.path.exists(src):
    sys.exit(1)
try:
    shutil.copyfile(src, tmp)
    c = sqlite3.connect(tmp)
    idx = list(c.execute(
        "SELECT name, sql FROM sqlite_master "
        "WHERE type='index' AND sql IS NOT NULL AND name NOT LIKE 'sqlite_%'"))
    c.execute("CREATE TABLE IF NOT EXISTS _pack_meta (name TEXT PRIMARY KEY, sql TEXT)")
    c.execute("DELETE FROM _pack_meta")
    c.executemany("INSERT INTO _pack_meta (name, sql) VALUES (?, ?)", idx)
    for name, _ in idx:
        c.execute(f'DROP INDEX IF EXISTS "{name}"')
    c.commit()
    c.execute("VACUUM")
    c.close()
    print(f"🗜 索引 {len(idx)} 件を退避して VACUUM (展開時に復元)")
except Exception as e:
    print(f"⚠️ 索引退避に失敗 → 従来どおり丸ごと固める: {e}")
    if os.path.exists(tmp):
        os.remove(tmp)
    sys.exit(1)
PY
then
  PACKED="keiba.db.pack.tmp"
fi

if [ -n "$PACKED" ] && [ -s "$PACKED" ]; then
  gzip -9nc "$PACKED" > keiba.db.gz
  rm -f "$PACKED"
else
  gzip -9nc keiba.db > keiba.db.gz
fi
echo "📦 keiba.db → keiba.db.gz ($(du -h keiba.db.gz | cut -f1))"
