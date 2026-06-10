#!/bin/bash
# keiba.db (作業ファイル) → keiba.db.gz (git管理用) に圧縮 (#64)
# -n: gzip ヘッダから timestamp を除去 = 内容が同一ならバイト同一 (no-op commit 防止)
set -e
sqlite3 keiba.db "PRAGMA wal_checkpoint(TRUNCATE);" 2>/dev/null || true
gzip -9nc keiba.db > keiba.db.gz
echo "📦 keiba.db → keiba.db.gz ($(du -h keiba.db.gz | cut -f1))"
