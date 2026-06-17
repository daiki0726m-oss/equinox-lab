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
gzip -9nc keiba.db > keiba.db.gz
echo "📦 keiba.db → keiba.db.gz ($(du -h keiba.db.gz | cut -f1))"
