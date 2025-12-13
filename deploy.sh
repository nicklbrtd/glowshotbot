#!/bin/bash
set -e

echo "🚀 Deploy GlowShot..."

cd ~/glowshotbot

echo "🔄 Обновляю код (git pull)..."
git pull --quiet || { echo "❌ git pull сломался"; exit 1; }

echo "📦 Обновляю зависимости (тихо)..."
source venv/bin/activate
pip install -r requirements.txt -q || { echo "❌ pip install сломался"; exit 1; }

echo "🤖 Перезапуск основного бота (tmux: glowshot)..."
tmux kill-session -t glowshot 2>/dev/null || true
tmux new-session -d -s glowshot "cd ~/glowshotbot && source venv/bin/activate && python bot.py"

echo "💬 Перезапуск бота поддержки (tmux: glowshot_support)..."
tmux kill-session -t glowshot_support 2>/dev/null || true
tmux new-session -d -s glowshot_support "cd ~/glowshotbot && source venv/bin/activate && python support_bot.py"

echo "💸 Перезапуск Robokassa webhook (tmux: glowshot_pay)..."
tmux kill-session -t glowshot_pay 2>/dev/null || true
tmux new-session -d -s glowshot_pay "cd ~/glowshotbot && source venv/bin/activate && uvicorn robokassa_webhook:app --host 127.0.0.1 --port 8000"

echo "✅ Deploy завершён."
