#!/usr/bin/env bash
# Запуск бота вручную (для теста). Прод — через systemd (tg-claude-bot.service).
cd /root/tg-claude-bot || exit 1
exec python3 bot.py
