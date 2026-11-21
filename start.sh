#!/bin/bash
# 🏇 AI競馬予測ダッシュボード 起動スクリプト
# PC再起動後にこれを実行するだけでOK

cd "$(dirname "$0")"

echo "🏇 AI競馬予測ダッシュボードを起動中..."

# 1. Flaskサーバーをバックグラウンドで起動
source venv/bin/activate
python app.py &
FLASK_PID=$!
echo "✅ Flaskサーバー起動 (PID: $FLASK_PID)"

# Flaskが起動するまで少し待つ
sleep 3

# 2. ngrokトンネル起動（フォアグラウンド）
echo "🌐 ngrokトンネルを起動中..."
echo ""
echo "======================================"
echo "  表示されるURLをスマホで開いてください"
echo "  URL末尾に /predict を追加！"
echo "======================================"
echo ""
ngrok http 5001

# ngrokを閉じたらFlaskも停止
kill $FLASK_PID 2>/dev/null
echo "🛑 停止しました"
