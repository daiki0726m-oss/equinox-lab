---
description: ngrokで外出先からダッシュボードにアクセスする
---

# ngrok起動手順

## 前提
- ngrokアカウント登録済み
- authtokenが設定済み

## 起動方法

// turbo-all

1. Flaskサーバーが動いていることを確認
```bash
lsof -i :5001
```

2. ngrokトンネル起動
```bash
ngrok http 5001
```

3. 表示される `Forwarding` のURLがスマホからアクセスできるURL
   例: `https://xxxx-xxxx.ngrok-free.app`

4. スマホブラウザで `上記URL/predict` にアクセス

## 停止
- ngrokのターミナルで `Ctrl+C`
