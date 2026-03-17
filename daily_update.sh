#!/bin/bash
# 競馬AI 日次自動更新スクリプト
# crontab -e で以下を登録:
# 0 6 * * * /Users/daikimorimoto/Desktop/keiba/daily_update.sh >> /Users/daikimorimoto/Desktop/keiba/logs/daily.log 2>&1

set -e
cd /Users/daikimorimoto/Desktop/keiba
source venv/bin/activate

LOG_DIR="logs"
mkdir -p "$LOG_DIR"

echo "=========================================="
echo "🏇 日次更新開始: $(date '+%Y-%m-%d %H:%M:%S')"
echo "=========================================="

# 1. 今週末のレースデータを収集
echo ""
echo "📦 レースデータ収集..."
TODAY=$(date '+%Y%m%d')
# 今日〜7日後のデータを収集
for i in $(seq 0 7); do
  TARGET_DATE=$(date -v+${i}d '+%Y%m%d' 2>/dev/null || date -d "+${i} days" '+%Y%m%d' 2>/dev/null)
  if [ -n "$TARGET_DATE" ]; then
    python predict.py collect --date "$TARGET_DATE" 2>/dev/null || true
  fi
done

# 2. 予測キャッシュクリア（新データ反映のため）
echo ""
echo "🗑️  予測キャッシュクリア..."
sqlite3 keiba.db "DELETE FROM predictions_cache WHERE created_at < datetime('now', '-1 day');" 2>/dev/null || true

# 3. モデル状態確認
echo ""
echo "🧠 モデル状態確認..."
python -c "
import os
model_path = os.path.join('models', 'model_rank.pkl')
if os.path.exists(model_path):
    import os.path
    mtime = os.path.getmtime(model_path)
    from datetime import datetime
    dt = datetime.fromtimestamp(mtime)
    print(f'  モデル最終更新: {dt.strftime(\"%Y-%m-%d %H:%M\")}')
    days = (datetime.now() - dt).days
    if days > 7:
        print(f'  ⚠️  {days}日前のモデルです。再学習を推奨。')
    else:
        print(f'  ✅ {days}日前に更新済み')
else:
    print('  ❌ モデル未学習')
"

# 4. DB統計
echo ""
echo "📊 DB統計..."
python -c "
from database import get_db, init_db
init_db()
with get_db() as conn:
    races = conn.execute('SELECT COUNT(*) as c FROM races').fetchone()['c']
    results = conn.execute('SELECT COUNT(*) as c FROM results').fetchone()['c']
    pedigree = conn.execute('SELECT COUNT(*) as c FROM pedigree').fetchone()['c']
    print(f'  レース: {races:,}')
    print(f'  出走データ: {results:,}')
    print(f'  血統データ: {pedigree:,}')
"

echo ""
echo "✅ 日次更新完了: $(date '+%Y-%m-%d %H:%M:%S')"
echo "=========================================="
