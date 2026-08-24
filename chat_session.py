#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Терминал-чат по сессии Claude Code (для работы с ПК в несколько окон).

Печатаешь сообщение → уходит в ту же сессию, что и Telegram-бот → ответ
печатается здесь живьём. Открой несколько окон с разными сессиями —
получишь свой мультиоконный режим, синхронный с ботом (общие файлы сессий).

Запуск:
    python3 /root/tg-claude-bot/chat_session.py <session_id>   # продолжить сессию
    python3 /root/tg-claude-bot/chat_session.py new            # новая сессия

session_id бери из бота: команда /id в нужном топике.
"""

import os
import sys
import json
import subprocess
from pathlib import Path

SESS_DIR = Path("/root/.claude/projects/-root")
CLAUDE = os.environ.get("CLAUDE_BIN", "/root/.local/bin/claude")
ALLOWED = ("Bash Read Edit Write MultiEdit Glob Grep WebFetch WebSearch "
           "Task TodoWrite NotebookEdit")
MODEL = os.environ.get("MODEL", "opus")
CWD = os.environ.get("CWD", "/root")

C_YOU = "\033[1;36m"
C_AI = "\033[1;33m"
C_DIM = "\033[90m"
C_OFF = "\033[0m"


def text_of(o):
    c = (o.get("message") or {}).get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        parts = [b.get("text") for b in c
                 if isinstance(b, dict) and b.get("type") == "text" and b.get("text")]
        return " ".join(parts) if parts else None
    return None


def show_recap(sid):
    p = SESS_DIR / f"{sid}.jsonl"
    if not p.exists():
        return
    try:
        lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()[-12:]
    except Exception:
        return
    for line in lines:
        try:
            o = json.loads(line)
        except Exception:
            continue
        t = o.get("type")
        if t not in ("user", "assistant"):
            continue
        txt = text_of(o)
        if not txt or txt.strip().startswith("<"):
            continue
        who = f"{C_YOU}ТЫ{C_OFF}" if t == "user" else f"{C_AI}CLAUDE{C_OFF}"
        print(f"{who}: {txt.strip()[:220]}")


def run(msg, sid):
    cmd = [CLAUDE, "-p", msg, "--output-format", "stream-json", "--verbose",
           "--permission-mode", "acceptEdits", "--allowedTools", ALLOWED,
           "--model", MODEL]
    if sid:
        cmd += ["--resume", sid]
    proc = subprocess.Popen(cmd, cwd=CWD, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, bufsize=1)
    new_sid = sid
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        if o.get("session_id"):
            new_sid = o["session_id"]
        if o.get("type") == "assistant":
            for b in (o.get("message") or {}).get("content", []):
                if b.get("type") == "text" and b.get("text"):
                    print(b["text"], end="", flush=True)
                elif b.get("type") == "tool_use":
                    print(f"\n{C_DIM}🔧 {b.get('name')}: "
                          f"{str(b.get('input'))[:110]}{C_OFF}", flush=True)
        elif o.get("type") == "result":
            print(f"\n{C_DIM}— {o.get('total_cost_usd', 0):.4f}$ · "
                  f"сессия {(new_sid or '')[:8]}{C_OFF}")
    proc.wait()
    if proc.returncode not in (0, None) and new_sid == sid:
        err = proc.stderr.read()[:400]
        print(f"{C_DIM}(ошибка claude: {err}){C_OFF}")
    return new_sid


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else "new"
    sid = None if arg == "new" else arg
    if sid:
        if not (SESS_DIR / f"{sid}.jsonl").exists():
            for f in sorted(SESS_DIR.glob(f"{sid}*.jsonl")):
                sid = f.stem
                break
        print(f"{C_AI}💬 Терминал-чат · сессия {sid}{C_OFF}  "
              f"(пустая строка + Enter — выход)")
        show_recap(sid)
    else:
        print(f"{C_AI}💬 Терминал-чат · новая сессия{C_OFF}  "
              f"(пустая строка + Enter — выход)")
    print("─" * 60)
    try:
        while True:
            msg = input(f"\n{C_YOU}ты>{C_OFF} ").strip()
            if not msg:
                break
            print(f"{C_AI}claude>{C_OFF} ", end="", flush=True)
            sid = run(msg, sid)
    except (KeyboardInterrupt, EOFError):
        pass
    print(f"\nпока 👋  продолжить где угодно: claude --resume {sid or ''}")


if __name__ == "__main__":
    main()
