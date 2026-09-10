#!/bin/bash
# keiba.db.gz (git管理の正本) → keiba.db (作業ファイル) を展開 (#64)
set -e
if [ -f keiba.db.gz ]; then
  gunzip -c keiba.db.gz > keiba.db
  # 📉 #153 (2026-09-10): db_pack.sh は索引を落として固めている (gz -35%)。
  #   _pack_meta に退避された DDL から元の索引を貼り直す (実測 0.6 秒)。
  #   _pack_meta が無い gz (旧形式) はそのまま = 後方互換。
  python3 - <<'PY' || echo "⚠️ 索引の復元に失敗 (検索が遅くなるだけで動作はする)"
import sqlite3
c = sqlite3.connect("keiba.db")
has = c.execute(
    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='_pack_meta'").fetchone()
if has:
    rows = list(c.execute("SELECT name, sql FROM _pack_meta WHERE sql IS NOT NULL"))
    for _name, sql in rows:
        c.execute(sql)
    c.execute("DROP TABLE _pack_meta")
    c.commit()
    print(f"🔧 索引 {len(rows)} 件を復元")
c.close()
PY
  echo "✅ keiba.db.gz → keiba.db 展開 ($(du -h keiba.db | cut -f1))"
else
  echo "⚠️ keiba.db.gz が見つからない (初回シード or 異常)"
fi
