#!/bin/bash
set -e

echo "🚀 Deploy GlowShot..."

cd ~/glowshotbot

echo "🔄 Обновляю код (git pull)..."
git pull --quiet || { echo "❌ git pull сломался"; exit 1; }

echo "📦 Обновляю зависимости (тихо)..."
source venv/bin/activate
pip install -r requirements.txt -q || { echo "❌ pip install сломался"; exit 1; }

echo "🤖 Перезапуск основного бота (systemd: glowshot-bot)..."
sudo systemctl restart glowshot-bot

echo "💬 Перезапуск бота поддержки (systemd: glowshot-support)..."
sudo systemctl restart glowshot-support

echo "📋 Статус сервисов..."
sudo systemctl --no-pager status glowshot-bot || true
sudo systemctl --no-pager status glowshot-support || true

echo "✅ Deploy завершён."
