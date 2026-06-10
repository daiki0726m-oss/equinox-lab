#!/bin/bash
# keiba.db.gz (git管理の正本) → keiba.db (作業ファイル) を展開 (#64)
set -e
if [ -f keiba.db.gz ]; then
  gunzip -c keiba.db.gz > keiba.db
  echo "✅ keiba.db.gz → keiba.db 展開 ($(du -h keiba.db | cut -f1))"
else
  echo "⚠️ keiba.db.gz が見つからない (初回シード or 異常)"
fi
