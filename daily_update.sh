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

# 3. モデル自動再学習（7日以上古い場合）
echo ""
echo "🧠 モデル状態確認..."
NEED_TRAIN=$(python -c "
import os
from datetime import datetime
model_path = os.path.join('models', 'model_rank.pkl')
if os.path.exists(model_path):
    mtime = os.path.getmtime(model_path)
    dt = datetime.fromtimestamp(mtime)
    days = (datetime.now() - dt).days
    print(f'  モデル最終更新: {dt.strftime(\"%Y-%m-%d %H:%M\")} ({days}日前)')
    if days > 7:
        print('  ⚠️  7日超 → 自動再学習を実行します')
        print('RETRAIN')
    else:
        print(f'  ✅ {days}日前に更新済み → スキップ')
else:
    print('  ❌ モデル未学習 → 自動学習を実行します')
    print('RETRAIN')
")

if echo "$NEED_TRAIN" | grep -q "RETRAIN"; then
  echo ""
  echo "🔄 モデル再学習開始..."
  python predict.py train
  echo "✅ モデル再学習完了"
fi

# 4. DB統計
echo ""
echo "📊 DB統計..."
python -c "
from database import get_db, init_db
init_db()
with get_db() as conn:
    races = conn.execute('SELECT COUNT(*) as c FROM races').fetchone()['c']
    results = conn.execute('SELECT COUNT(*) as c FROM results').fetchone()['c']
    sire_count = conn.execute(\"SELECT COUNT(*) as c FROM horses WHERE sire IS NOT NULL AND sire != ''\").fetchone()['c']
    total_horses = conn.execute('SELECT COUNT(*) as c FROM horses').fetchone()['c']
    print(f'  レース: {races:,}')
    print(f'  出走データ: {results:,}')
    print(f'  血統データ: {sire_count:,}/{total_horses:,}馬 ({sire_count/total_horses*100:.0f}%)')
"

echo ""
echo "✅ 日次更新完了: $(date '+%Y-%m-%d %H:%M:%S')"
echo "=========================================="
