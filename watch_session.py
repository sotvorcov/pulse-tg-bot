#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Живое окно-зеркало сессии Claude Code.

Показывает в терминале новые сообщения сессии в реальном времени — что бы ты ни
писал в Telegram-боте, оно тут же появляется здесь. «Параллельный экран» на ПК,
раз само окно Claude Code не обновляется на лету.

Запуск:
    python3 /root/tg-claude-bot/watch_session.py <session_id>

session_id бери из бота: в нужном топике команда /id
"""

import sys
import json
import time
from pathlib import Path

SESS_DIR = Path("/root/.claude/projects/-root")


def resolve(sid: str):
    p = SESS_DIR / f"{sid}.jsonl"
    if p.exists():
        return p
    for f in sorted(SESS_DIR.glob(f"{sid}*.jsonl")):
        return f
    return None


def text_of(o):
    c = (o.get("message") or {}).get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = [b.get("text") for b in c
                 if isinstance(b, dict) and b.get("type") == "text" and b.get("text")]
        return " ".join(parts) if parts else None
    return None


def show(o):
    t = o.get("type")
    if t not in ("user", "assistant"):
        return
    txt = text_of(o)
    if not txt:
        return
    txt = txt.strip()
    if not txt or txt.startswith("<") or txt.startswith("Caveat:"):
        return
    if t == "user":
        who = "\033[1;36m🧑 ТЫ\033[0m"
    else:
        who = "\033[1;33m🤖 CLAUDE\033[0m"
    print(f"\n{who}  \033[90m{time.strftime('%H:%M:%S')}\033[0m")
    print(txt)


def main():
    if len(sys.argv) < 2:
        print("usage: python3 watch_session.py <session_id>   (id бери из /id в боте)")
        return
    p = resolve(sys.argv[1])
    if not p:
        print("Сессия не найдена в", SESS_DIR)
        return
    print(f"👀 Слежу за {p.name}\n   новые сообщения появляются ниже (Ctrl+C — выход)")
    print("─" * 60)
    # показать последние сообщения для контекста
    try:
        with p.open(encoding="utf-8", errors="ignore") as f:
            tail = f.readlines()[-12:]
        for line in tail:
            try:
                show(json.loads(line))
            except Exception:
                pass
    except Exception:
        pass
    pos = p.stat().st_size
    print("\n" + "─" * 60 + "\n(жду новые сообщения…)")
    try:
        while True:
            time.sleep(1)
            size = p.stat().st_size
            if size < pos:      # файл усечён/пересоздан
                pos = 0
            if size > pos:
                with p.open(encoding="utf-8", errors="ignore") as f:
                    f.seek(pos)
                    chunk = f.read()
                    pos = f.tell()
                for line in chunk.splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        show(json.loads(line))
                    except Exception:
                        pass
    except KeyboardInterrupt:
        print("\nпока 👋")


if __name__ == "__main__":
    main()
