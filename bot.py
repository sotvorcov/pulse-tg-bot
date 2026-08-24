#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
TG Claude Bot — пульт от Claude Code в Telegram.

Идея: супергруппа с включёнными Топиками. Каждый топик = отдельная сессия
Claude Code на этом сервере (как вкладка в VS Code). Пишешь текст/голос в топик —
оно уходит в ту самую сессию, ответы и прогресс сыплются обратно.

Движок: shell к `claude` CLI (тот же аккаунт/подписка, что и в VS Code),
headless + stream-json + резюм сессий по ID. Полный автопилот
(--dangerously-skip-permissions) — как AUTO MODE в текущем чате.
"""

import os
import sys
import re
import json
import html
import time
import random
import asyncio
import sqlite3
import logging
from pathlib import Path
from datetime import datetime

# ── простой загрузчик .env (без внешних зависимостей) ─────────────────────────
BASE = Path(__file__).resolve().parent
_envfile = BASE / ".env"
if _envfile.exists():
    for _line in _envfile.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _v = _line.split("=", 1)
        os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()
CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "/root/.local/bin/claude")
# Автопилот под root: acceptEdits + широкий allowlist (Bash = любые команды).
# Легальный путь; --dangerously-skip-permissions под root заблокирован CLI.
ALLOWED_TOOLS = os.environ.get(
    "ALLOWED_TOOLS",
    "Bash Read Edit Write MultiEdit Glob Grep WebFetch WebSearch Task TodoWrite NotebookEdit",
)
DEFAULT_CWD = os.environ.get("DEFAULT_CWD", "/root")
SESS_DIR = Path(os.environ.get("SESS_DIR", "/root/.claude/projects/-root"))
# Секрет активации: без него нельзя стать владельцем (защита от захвата чужим).
OWNER_SETUP_SECRET = os.environ.get("OWNER_SETUP_SECRET", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_ASR_MODEL = os.environ.get("GEMINI_ASR_MODEL", "gemini-2.5-flash")
VERTEX_SA = os.environ.get("VERTEX_SA_PATH", "/root/.gcp-vibe-vertex.json")
VERTEX_LOCATION = os.environ.get("VERTEX_LOCATION", "us-central1")
VERTEX_MODEL = os.environ.get("VERTEX_MODEL", "gemini-2.5-flash")
DEEPGRAM_API_KEY = os.environ.get("DEEPGRAM_API_KEY", "").strip()
# Яндекс SpeechKit (RU-нативное распознавание) — второй живой провайдер, страховка Deepgram
YANDEX_API_KEY = os.environ.get("YANDEX_API_KEY", "").strip()
YANDEX_FOLDER_ID = os.environ.get("YANDEX_FOLDER_ID", "").strip()
# голос включён, если доступен хоть один провайдер распознавания
VOICE_ON = bool(GEMINI_API_KEY or OPENAI_API_KEY or DEEPGRAM_API_KEY
                or (YANDEX_API_KEY and YANDEX_FOLDER_ID)
                or os.path.exists(VERTEX_SA))
DB_PATH = BASE / "bot.db"
IMG_DIR = BASE / "_img"
IMG_DIR.mkdir(exist_ok=True)

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ChatAction
from telegram.error import BadRequest, RetryAfter, Forbidden, TimedOut, NetworkError
from telegram.ext import (
    Application,
    AIORateLimiter,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("tgclaude")

# ── модели ────────────────────────────────────────────────────────────────────
MODELS = {
    "opus":   ("Opus 4.8",       "opus"),
    "sonnet": ("Sonnet 5",       "sonnet"),
    "haiku":  ("Haiku 4.5",      "haiku"),
    "fable":  ("Fable 5",        "claude-fable-5"),
    "auto":   ("Авто (дефолт)",  None),
}
DEFAULT_MODEL_KEY = "opus"

VOICE_LABELS = {
    "auto": "Авто (с фолбэком)",
    "vertex": "Vertex (Gemini)",
    "gemini": "Gemini API-ключ",
    "openai": "OpenAI Whisper",
    "deepgram": "Deepgram",
    "yandex": "Яндекс SpeechKit",
}

# стили индикатора «думает…» (крутятся, пока нет ответа)
THINK_STYLES = {
    "claude": ["Transmuting…", "Cogitating…", "Puzzling…", "Conjuring…", "Percolating…",
               "Noodling…", "Finagling…", "Brewing…", "Manifesting…", "Tinkering…",
               "Vibing…", "Computing…"],
    "ru": ["Думаю…", "Химичу…", "Колдую…", "Собираю мысли…", "Варю…", "Мозгую…",
           "Кручу шестерёнки…", "Прикидываю…", "Ворожу…", "Мастерю…", "Разгоняюсь…",
           "Копаю…"],
    "hacker": ["> compiling…", "> hacking…", "> injecting…", "> spinning up…",
               "> crunching…", "> decrypting…", "> loading matrix…", "> rooting…"],
    "min": ["⏳ Обрабатываю…"],
    "dots": ["думаю ⠋", "думаю ⠙", "думаю ⠹", "думаю ⠸", "думаю ⠼",
             "думаю ⠴", "думаю ⠦", "думаю ⠧", "думаю ⠇", "думаю ⠏"],
    "emoji": ["⏳ Думаю…"],
}
THINK_LABELS = {"claude": "Как Claude (англ.)", "ru": "Русский прикольный",
                "hacker": "Хакер", "min": "Минимал", "dots": "Спиннер ⠿",
                "emoji": "Анимир. эмодзи ⏳"}

VERB_LABELS = {"full": "Всё (с кодом)", "progress": "Прогресс (без кода)",
               "final": "Только ответ"}

# кастом-эмодзи Pulse — ОПЦИОНАЛЬНЫ. Если рядом лежит pulse_icons.json —
# бот подставит наши брендовые анимир-иконки; без файла везде обычные эмодзи
# (open-source по умолчанию универсален, без привязки к чужому паку).
def _load_icons():
    p = BASE / "pulse_icons.json"
    if p.exists():
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            return dict(d.get("think", {})), dict(d.get("ce", {}))
        except Exception as e:
            logging.warning("pulse_icons.json не прочитан: %s", e)
    return {}, {}

THINK_EMOJI, CE_MAP = _load_icons()
THINK_EMOJI_LABELS = {"none": "Без иконки", "pulse": "⚡ Пульс-волна",
                      "sonar": "📡 Сонар", "breath": "💛 Дыхание", "spin": "🔄 Спиннер"}


def ce(emoji):
    """Наш custom-эмодзи, если включено; иначе обычный."""
    eid = CE_MAP.get(emoji)
    if eid and cfg_get("custom_emoji", "1") == "1":
        return f'<tg-emoji emoji-id="{eid}">{emoji}</tg-emoji>'
    return emoji


def indicator_text():
    """Единый текст индикатора «думает» (анимир-эмодзи + слово) — и для бота, и
    для зеркала, по одним настройкам /thinking + /thinkicon."""
    words = THINK_STYLES.get(cfg_get("think_style", "claude"), THINK_STYLES["claude"])
    word = random.choice(words) if words else "⏳"
    eid = THINK_EMOJI.get(cfg_get("think_emoji", "pulse"))
    if eid:
        return f'<tg-emoji emoji-id="{eid}">⚡</tg-emoji> {html.escape(word)}', "HTML"
    return word, None

# ── база (SQLite) ──────────────────────────────────────────────────────────────
db = sqlite3.connect(DB_PATH)
db.row_factory = sqlite3.Row
db.execute("CREATE TABLE IF NOT EXISTS config(key TEXT PRIMARY KEY, value TEXT)")
db.execute(
    """CREATE TABLE IF NOT EXISTS topics(
        id TEXT PRIMARY KEY,            -- 'chat_id:thread_id'
        chat_id INTEGER,
        thread_id INTEGER,
        session_id TEXT,
        model_key TEXT DEFAULT 'opus',
        cwd TEXT,
        ultra INTEGER DEFAULT 0,
        name TEXT,
        cost REAL DEFAULT 0,
        in_tok INTEGER DEFAULT 0,
        out_tok INTEGER DEFAULT 0
    )"""
)
db.commit()
# миграция: колонка mirror (зеркалирование ПК→топик), если её ещё нет
try:
    db.execute("ALTER TABLE topics ADD COLUMN mirror INTEGER DEFAULT 0")
    db.commit()
except Exception:
    pass
try:
    db.execute("ALTER TABLE topics ADD COLUMN locked INTEGER DEFAULT 0")
    db.commit()
except Exception:
    pass


def cfg_get(key, default=None):
    r = db.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def cfg_set(key, value):
    db.execute(
        "INSERT INTO config(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    db.commit()


def tkey(chat_id, thread_id):
    return f"{chat_id}:{thread_id or 0}"


def _wm_set(key, mid):
    WORK_MSG[key] = mid
    cfg_set(f"wm_{key}", str(mid))       # переживает перезапуск (чистится на старте)


def _wm_clear(key):
    WORK_MSG.pop(key, None)
    try:
        db.execute("DELETE FROM config WHERE key=?", (f"wm_{key}",))
        db.commit()
    except Exception:
        pass


def _sm_set(key, mid):
    # id первого сообщения Стримера («думает…/🔧 Bash…») — чтобы убрать призрак,
    # если ответ оборвался рестартом (чистится при finalize или на старте бота)
    cfg_set(f"sm_{key}", str(mid))


def _sm_clear(key):
    try:
        db.execute("DELETE FROM config WHERE key=?", (f"sm_{key}",))
        db.commit()
    except Exception:
        pass


def topic_get(chat_id, thread_id):
    return db.execute(
        "SELECT * FROM topics WHERE id=?", (tkey(chat_id, thread_id),)
    ).fetchone()


def topic_upsert(chat_id, thread_id, **fields):
    key = tkey(chat_id, thread_id)
    row = db.execute("SELECT id FROM topics WHERE id=?", (key,)).fetchone()
    if row is None:
        db.execute(
            "INSERT INTO topics(id,chat_id,thread_id,cwd,model_key) VALUES(?,?,?,?,?)",
            (key, chat_id, thread_id or 0, DEFAULT_CWD, DEFAULT_MODEL_KEY),
        )
    if fields:
        cols = ", ".join(f"{k}=?" for k in fields)
        db.execute(
            f"UPDATE topics SET {cols} WHERE id=?", (*fields.values(), key)
        )
    db.commit()
    return topic_get(chat_id, thread_id)


# ── доступ: только владелец ────────────────────────────────────────────────────
def is_owner(update: Update) -> bool:
    owner = cfg_get("owner_id")
    uid = update.effective_user.id if update.effective_user else None
    return owner is not None and str(uid) == str(owner)


# ── реестр запущенных процессов (для /stop) ─────────────────────────────────────
RUNNING = {}  # tkey -> asyncio.subprocess.Process
MIRROR_POS = {}  # tkey -> байт-позиция файла сессии, до которой уже отзеркалено
MIRROR_POLL = 2  # период опроса файлов сессий, сек (снапнее живой апдейт)
IMPORT_RUNNING = set()  # chat_id, где сейчас идёт /import (защита от дублей)
QUEUES = {}  # tkey -> [тексты]: задачи, докинутые пока идёт текущая
ALBUM = {}   # media_group_id -> собираемый альбом фото (одно сообщение = 1 фото)
WORK_MSG = {}   # tkey -> id сообщения-индикатора «работает…» в зеркале
LAST_SIZE = {}  # tkey -> прошлый размер файла сессии (детект активности)
LAST_ACTIVE = {}  # tkey -> время последней активности (незамигающий индикатор)


# ── превью сессии из .jsonl (первое сообщение пользователя, без чтения всего файла)
def session_preview(path: Path) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as f:
            for i, line in enumerate(f):
                if i > 80:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if obj.get("type") != "user":
                    continue
                content = (obj.get("message") or {}).get("content")
                text = None
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    for b in content:
                        if isinstance(b, dict) and b.get("type") == "text":
                            text = b.get("text")
                            break
                if not text:
                    continue
                text = text.strip()
                # пропускаем служебные/системные вставки
                if text.startswith("<") or text.startswith("Caveat:"):
                    continue
                return re.sub(r"\s+", " ", text)[:70]
    except Exception as e:
        log.warning("preview fail %s: %s", path, e)
    return "(без превью)"


def session_gist(path):
    """Первое осмысленное сообщение пользователя (до ~700 симв) — для AI-названия."""
    try:
        with path.open(encoding="utf-8", errors="ignore") as f:
            for i, line in enumerate(f):
                if i > 120:
                    break
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                if o.get("type") != "user":
                    continue
                c = (o.get("message") or {}).get("content")
                if isinstance(c, str):
                    raw = c
                elif isinstance(c, list):
                    raw = " ".join(b.get("text", "") for b in c
                                   if isinstance(b, dict) and b.get("type") == "text")
                else:
                    raw = ""
                txt = _strip_system(raw)
                if txt:
                    return re.sub(r"\s+", " ", txt)[:700]
    except Exception:
        pass
    return ""


def session_last_messages(path: Path, n=6, tail_bytes=1_200_000):
    """Последние n текстовых сообщений (юзер+ассистент) из хвоста файла —
    без чтения целого .jsonl (может весить сотни МБ)."""
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > tail_bytes:
                f.seek(size - tail_bytes)
                f.readline()  # выкинуть обрезанную первую строку
            data = f.read().decode("utf-8", "ignore")
    except Exception:
        return []
    msgs = []
    for line in data.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        t = o.get("type")
        if t not in ("user", "assistant"):
            continue
        content = (o.get("message") or {}).get("content")
        text = None
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts = [
                b.get("text") for b in content
                if isinstance(b, dict) and b.get("type") == "text" and b.get("text")
            ]
            text = " ".join(parts) if parts else None
        if not text:
            continue
        text = re.sub(r"\s+", " ", text).strip()
        if not text or text.startswith("<") or text.startswith("Caveat:"):
            continue
        msgs.append((t, text))
    return msgs[-n:]


def list_sessions(limit=14, offset=0):
    files = sorted(
        SESS_DIR.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    total = len(files)
    out = []
    for p in files[offset:offset + limit]:
        sid = p.stem
        when = datetime.fromtimestamp(p.stat().st_mtime).strftime("%d.%m %H:%M")
        out.append((sid, when, session_preview(p)))
    return out, total


def short_input(name, inp):
    if not isinstance(inp, dict):
        return ""
    if name in ("Bash",) and inp.get("command"):
        return str(inp["command"])[:160]
    if inp.get("file_path"):
        return str(inp["file_path"])
    if inp.get("path"):
        return str(inp["path"])
    if inp.get("pattern"):
        return str(inp["pattern"])[:120]
    try:
        s = json.dumps(inp, ensure_ascii=False)
    except Exception:
        s = str(inp)
    return s[:120]


def clean_name(text, n=36):
    """Короткое читаемое имя вкладки: домен из ссылки, либо первые слова."""
    t = re.sub(r"\s+", " ", (text or "").strip())
    m = re.search(r"https?://([^/\s]+)", t)
    if m:
        return m.group(1).replace("www.", "")[:n]
    if not t:
        return "сессия"
    if len(t) <= n:
        return t
    cut = t[:n].rsplit(" ", 1)[0]
    return (cut or t[:n]).rstrip(" ,.:;—-") + "…"


_ICON_RULES = [
    (("http", "www", ".ru", ".com", ".pro", ".su", "сайт", "лендинг", "landing"), "🌐"),
    (("дизайн", " ui", "вёрст", "верст", "css", "figma", "стиль"), "🎨"),
    (("бот", "telegram", "телеграм", "bot"), "🤖"),
    (("дашборд", "график", "аналитик", "метрик", "отчёт", "отчет", "chart"), "📊"),
    (("постгрес", "postgres", "sql", "миграц", "база данных", "бд "), "🗄"),
    (("деплой", "сервер", "docker", "nginx", "systemd", "деплой", "ci/cd"), "🚀"),
    (("api", "бэкенд", "backend", "endpoint", "интеграц"), "🔌"),
    (("тест", "баг", "фикс", "ошибк", "debug", "bug", "падает"), "🐞"),
    (("голос", "аудио", "voice", "asr", "whisper"), "🎙"),
    (("видео", "рендер", " 3d", "анимац", "avatar", "аватар"), "🎬"),
    (("пиво", "пивмастер", "beer"), "🍺"),
    (("деньг", "оплат", "платёж", "касс", "эквайр", "юkassa", "pay"), "💳"),
    (("тз", "документ", "стать", "пост", "текст", "лендинг-текст"), "📝"),
    (("astro", "астро", "натальн", "гороскоп"), "🔮"),
]


def pick_icon(text):
    t = (text or "").lower()
    for keys, emo in _ICON_RULES:
        if any(k in t for k in keys):
            return emo
    return "💬"


def titled(preview):
    """Имя вкладки с тематической иконкой: «🌐 riplo.ai»."""
    return f"{pick_icon(preview)} {clean_name(preview)}"


# цветная иконка ТЕМЫ (из разрешённого Telegram-набора; наши custom нельзя — Premium)
TOPIC_ICON = {
    "🎨": "5310039132297242441", "🤖": "5309832892262654231",
    "🎙": "5377544228505134960", "💬": "5417915203100613993",
    "📊": "5237889595894414384", "🚀": "5312241539987020022",
    "🎬": "5368653135101310687", "📝": "5373251851074415873",
    "🔮": "5350367161514732241",
}
DEFAULT_TOPIC_ICON = "5350554349074391003"  # 💻


def topic_icon_id(text):
    return TOPIC_ICON.get(pick_icon(text), DEFAULT_TOPIC_ICON)


async def smart_title(sid):
    """Имя вкладки: если включены AI-названия и есть Vertex — Gemini опишет суть
    сессии; иначе эвристика titled()."""
    p = SESS_DIR / f"{sid}.jsonl"
    prev = session_preview(p)
    if cfg_get("smart_titles", "1") != "1" or not os.path.exists(VERTEX_SA):
        return titled(prev)
    gist = session_gist(p)
    if not gist:
        return titled(prev)
    try:
        raw = await _vertex_text(
            "Придумай очень короткое название вкладки (2–4 слова, по-русски, без "
            "кавычек и финальной точки) для этой задачи разработчика. Верни ТОЛЬКО "
            f"название.\n\nЗадача: {gist}")
        lines = [x for x in (raw or "").strip().strip('"«»').splitlines() if x.strip()]
        title = re.sub(r"\s+", " ", lines[0]).strip()[:40] if lines else ""
        if title:
            return f"{pick_icon(gist)} {title}"
    except Exception as e:
        log.warning("smart_title: %s", e)
    return titled(prev)


# ── markdown Claude → Telegram HTML ─────────────────────────────────────────────
_MD_LINK = re.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")


def md_to_tg_html(text: str) -> str:
    """Грубая, но безопасная конвертация markdown в Telegram HTML
    (жирный, код, ссылки, заголовки, маркеры списка). При сбое — просто экранирует."""
    try:
        stash = []

        def keep(rendered):
            stash.append(rendered)
            return f"\x00{len(stash) - 1}\x00"

        def _codeblock(m):
            lang = (m.group(1) or "").strip()
            body = html.escape(m.group(2))
            if lang:   # с языком → блок Telegram с подсветкой и кнопкой «копировать»
                return keep(f'<pre><code class="language-{lang}">{body}</code></pre>')
            return keep(f"<pre>{body}</pre>")

        text = re.sub(
            r"```([a-zA-Z0-9_+.-]*)\n?(.*?)```",
            _codeblock,
            text, flags=re.S,
        )
        text = re.sub(
            r"`([^`\n]+)`",
            lambda m: keep(f"<code>{html.escape(m.group(1))}</code>"),
            text,
        )
        text = html.escape(text)
        text = _MD_LINK.sub(
            lambda m: keep(f'<a href="{m.group(2)}">{m.group(1)}</a>'), text
        )
        text = re.sub(r"(?m)^\s{0,3}#{1,6}\s*(.+?)\s*#*$", r"<b>\1</b>", text)
        text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.S)
        text = re.sub(r"__(.+?)__", r"<b>\1</b>", text, flags=re.S)
        text = re.sub(r"(?m)^(\s*)[-*]\s+", r"\1• ", text)
        if cfg_get("custom_emoji", "1") == "1":
            for e, eid in CE_MAP.items():
                if e in text:
                    text = text.replace(e, f'<tg-emoji emoji-id="{eid}">{e}</tg-emoji>')
        text = re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], text)
        return text
    except Exception:
        return html.escape(text)


# ── стример: живой вывод в топик, авто-разбивка длинных сообщений ────────────────
class Streamer:
    """Живой вывод в топик с Telegram-разметкой. Весь ответ режется на куски
    (не рвя ```-блоки), каждый кусок рендерится в HTML — форматирование не
    теряется даже у длинных ответов, разбитых на несколько сообщений."""
    LIMIT = 2600      # запас: после HTML-разметки кусок не должен вылезти за 4096 TG

    def __init__(self, bot, chat_id, thread_id):
        self.bot = bot
        self.chat_id = chat_id
        self.thread_id = thread_id
        self.msgs = []        # отправленные сообщения (по одному на кусок)
        self.rendered = []    # последний HTML каждого куска (чтобы не редактировать зря)
        self.text = ""        # весь markdown-текст
        self.last = 0.0
        self.thinking = True  # крутить индикатор «думает…», пока нет ответа
        self._words = None

    def _think_text(self):
        return indicator_text()

    async def start(self, initial=None):
        self._words = THINK_STYLES.get(
            cfg_get("think_style", "claude"), THINK_STYLES["claude"])
        if initial:
            txt, pm = initial, None
        else:
            txt, pm = self._think_text()
        m = await self.bot.send_message(
            self.chat_id, txt, message_thread_id=self.thread_id, parse_mode=pm)
        self.msgs = [m]
        _sm_set(tkey(self.chat_id, self.thread_id), m.message_id)   # для авто-уборки призраков

    async def rotate(self):
        """Крутит индикатор (анимир-эмодзи + слово), пока не пришёл ответ."""
        try:
            while self.thinking and not self.text:
                await asyncio.sleep(2.6)
                if not (self.thinking and not self.text and self.msgs):
                    break
                txt, pm = self._think_text()
                try:
                    await self.bot.edit_message_text(
                        txt, chat_id=self.chat_id,
                        message_id=self.msgs[0].message_id, parse_mode=pm)
                except Exception:
                    pass
        except Exception:
            pass

    @staticmethod
    def _split_md(text, limit=3500):
        chunks, cur, curlen, fence = [], [], 0, False
        for ln in text.split("\n"):
            if ln.lstrip().startswith("```"):
                fence = not fence
            add = len(ln) + 1
            if curlen + add > limit and cur and not fence:
                chunks.append("\n".join(cur))
                cur, curlen = [], 0
            cur.append(ln)
            curlen += add
        if cur:
            chunks.append("\n".join(cur))
        out = []
        for c in chunks:                       # страховка от сверхдлинной строки
            while len(c) > 4096:
                out.append(c[:4000]); c = c[4000:]
            out.append(c)
        return out or [""]

    async def _put(self, idx, html_text):
        html_text = html_text if html_text.strip() else "…"
        try:
            if idx < len(self.msgs):
                await self.bot.edit_message_text(
                    html_text, chat_id=self.chat_id,
                    message_id=self.msgs[idx].message_id, parse_mode="HTML")
            else:
                m = await self.bot.send_message(
                    self.chat_id, html_text, message_thread_id=self.thread_id,
                    parse_mode="HTML")
                self.msgs.append(m)
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after + 0.5)
        except BadRequest as e:
            if "not modified" in str(e).lower():
                return
            plain = re.sub(r"<[^>]+>", "", html_text)   # разметка сломалась → без HTML
            try:
                if idx < len(self.msgs):
                    await self.bot.edit_message_text(
                        plain, chat_id=self.chat_id,
                        message_id=self.msgs[idx].message_id)
                else:
                    m = await self.bot.send_message(
                        self.chat_id, plain, message_thread_id=self.thread_id)
                    self.msgs.append(m)
            except Exception:
                pass
        except (TimedOut, NetworkError):
            pass

    async def _render(self):
        chunks = self._split_md(self.text, self.LIMIT)
        for i, ch in enumerate(chunks):
            h = md_to_tg_html(ch)
            if i < len(self.rendered) and self.rendered[i] == h:
                continue
            await self._put(i, h)
            if i < len(self.rendered):
                self.rendered[i] = h
            else:
                self.rendered.append(h)

    async def feed(self, s, force=False):
        if s:
            self.thinking = False       # пошёл ответ — гасим индикатор
        self.text += s
        if not self.msgs:
            await self.start()
        now = asyncio.get_running_loop().time()
        if force or now - self.last > 1.8:   # реже правок в стриме — плавно и без 429
            self.last = now
            await self._render()

    async def flush(self):
        await self.feed("", force=True)

    async def finalize(self):
        await self.feed("", force=True)
        _sm_clear(tkey(self.chat_id, self.thread_id))   # ответ дорисован — плашка не призрак


# ── запуск claude по сессии топика ──────────────────────────────────────────────
AUTO_COMPACT_TOK = 150_000   # активный контекст выше этого → авто-/compact перед ответом


async def _run_compact(bot, topic):
    """Тихо сжимает сессию топика штатным /compact — нить сохраняется, контекст падает."""
    key = tkey(topic["chat_id"], topic["thread_id"])
    try:
        await bot.send_message(
            topic["chat_id"], "🗜 Контекст подрос — сжимаю (нить сохранится)…",
            message_thread_id=topic["thread_id"])
    except Exception:
        pass
    cmd = [CLAUDE_BIN, "-p", "/compact", "--output-format", "stream-json", "--verbose",
           "--permission-mode", "acceptEdits"]
    if topic["session_id"]:
        cmd += ["--resume", topic["session_id"]]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=topic["cwd"] or DEFAULT_CWD,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=64 * 1024 * 1024)
    except Exception as e:
        log.warning("auto-compact spawn: %s", e)
        return
    new_sid = topic["session_id"]
    try:
        while True:
            raw = await asyncio.wait_for(proc.stdout.readline(), timeout=180)
            if not raw:
                break
            try:
                obj = json.loads(raw.decode("utf-8", "ignore").strip())
            except Exception:
                continue
            if obj.get("session_id"):
                new_sid = obj["session_id"]
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
    except Exception:
        pass
    try:
        await proc.wait()
    except Exception:
        pass
    if new_sid and new_sid != topic["session_id"]:
        topic_upsert(topic["chat_id"], topic["thread_id"], session_id=new_sid)
    cfg_set(f"ctx_{key}", "0")   # после сжатия контекст сброшен


async def run_claude(bot, topic, prompt_text):
    key = tkey(topic["chat_id"], topic["thread_id"])
    cur = RUNNING.get(key)
    if cur is not None and cur != "starting":   # реально живой процесс — не плодим второй
        await bot.send_message(
            topic["chat_id"],
            "⚠️ Уже работаю в этой сессии. /stop чтобы прервать.",
            message_thread_id=topic["thread_id"],
        )
        return
    RUNNING[key] = "starting"   # синхронный claim до любого await — закрывает гонку двойного запуска
    cfg_set(f"pending_{key}", prompt_text)   # если рестарт прервёт — возобновим на старте

    # авто-сжатие: контекст перевалил порог → сперва /compact (нить сохраняем, сессию не плодим)
    if prompt_text != "/compact" and cfg_get("auto_compact", "0") == "1" and topic["session_id"]:
        try:
            prev_ctx = int(cfg_get(f"ctx_{key}", "0") or 0)
        except Exception:
            prev_ctx = 0
        if prev_ctx > AUTO_COMPACT_TOK:
            if prev_ctx > 300_000:
                # мусорный сигнал от старого бага (суммарные токены за ход), реальное окно ≤ ~200k → игнор
                cfg_set(f"ctx_{key}", "0")
            else:
                await _run_compact(bot, topic)
                topic = topic_get(topic["chat_id"], topic["thread_id"]) or topic

    model_cli = MODELS.get(topic["model_key"], MODELS["opus"])[1]
    if topic["ultra"]:
        prompt_text = "ultracode\n\n" + prompt_text

    base_cmd = [
        CLAUDE_BIN, "-p", prompt_text,
        "--output-format", "stream-json",
        "--verbose",
        "--permission-mode", "acceptEdits",
        "--allowedTools", ALLOWED_TOOLS,
    ]
    if topic["session_id"]:
        base_cmd += ["--resume", topic["session_id"]]

    cwd = topic["cwd"] or DEFAULT_CWD
    st = Streamer(bot, topic["chat_id"], topic["thread_id"])
    rot = None

    verb = cfg_get("verbosity", "full")   # full / progress / final
    new_sid = topic["session_id"]
    cost = 0.0
    in_tok = out_tok = win_tok = 0
    got_any = False
    err_text = ""
    proc = None
    # транзиентные ошибки сервера Клода — по ним повторяем, а не сдаёмся
    TRANSIENT = ("500", "internal server error", "overloaded", "529", "502", "503",
                 "connection closed", "connection error", "read timeout", "timed out",
                 "econnreset", "rate limit", "try again")
    MAX_TRIES = 3
    IDLE_LIMIT = 180   # сек тишины от claude = зависание (обычно распухший контекст)

    try:
        await st.start()
        rot = asyncio.create_task(st.rotate())
        for attempt in range(MAX_TRIES):
            # на повторах после перегрузки — фолбэк на более доступную модель
            m = model_cli
            if attempt > 0 and model_cli in (None, "opus"):
                m = "sonnet"
            cmd = list(base_cmd) + (["--model", m] if m else [])
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd, cwd=cwd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=64 * 1024 * 1024,   # большие строки stream-json
                )
            except Exception as e:
                err_text = f"не смог запустить claude: {e}"
                break
            RUNNING[key] = proc
            header_shown = False
            while True:
                try:
                    raw = await asyncio.wait_for(proc.stdout.readline(), timeout=IDLE_LIMIT)
                except asyncio.TimeoutError:
                    err_text = ("завис — 3 минуты нет ответа от claude (обычно распухший "
                                "контекст). Сожми топик командой /compact.")
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    break
                if not raw:          # EOF — процесс завершился
                    break
                line = raw.decode("utf-8", "ignore").strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except Exception:
                    continue
                if obj.get("session_id"):
                    new_sid = obj["session_id"]
                t = obj.get("type")

                if t == "system" and obj.get("subtype") == "init" and not header_shown:
                    header_shown = True
                    continue

                if t == "assistant":
                    # реальный размер окна модели ЭТОГО вызова (не суммарный по ходу):
                    um = (obj.get("message") or {}).get("usage") or {}
                    w = ((um.get("input_tokens") or 0)
                         + (um.get("cache_read_input_tokens") or 0)
                         + (um.get("cache_creation_input_tokens") or 0))
                    if w > win_tok:
                        win_tok = w
                    for b in (obj.get("message") or {}).get("content", []):
                        bt = b.get("type")
                        if bt == "text" and b.get("text"):
                            got_any = True
                            if verb != "final":
                                await st.feed(b["text"])
                        elif bt == "tool_use":
                            got_any = True
                            if verb == "full":
                                nm = b.get("name", "tool")
                                arg = short_input(nm, b.get("input"))
                                if nm == "Bash":
                                    await st.feed(f"\n\n🔧 **Bash**\n```bash\n{arg}\n```\n")
                                else:
                                    await st.feed(f"\n\n🔧 **{nm}** `{arg}`\n")
                            elif verb == "progress":
                                await st.feed(f"\n⚙️ {b.get('name','tool')}…")

                elif t == "result":
                    cost = obj.get("total_cost_usd") or 0.0
                    u = obj.get("usage") or {}
                    in_tok = (u.get("input_tokens") or 0) + (
                        u.get("cache_read_input_tokens") or 0
                    ) + (u.get("cache_creation_input_tokens") or 0)
                    out_tok = u.get("output_tokens") or 0
                    rtext = obj.get("result") or ""
                    if obj.get("is_error") or "error" in str(obj.get("subtype") or ""):
                        err_text = rtext or str(obj.get("subtype") or "error")
                    elif verb == "final" and rtext:
                        st.text = ""
                        st.rendered = []
                        await st.feed(rtext, force=True)
                        got_any = True
                    elif not got_any and rtext:
                        await st.feed(rtext)
                        got_any = True

            await proc.wait()
            if proc.returncode not in (0, None) and not got_any and not err_text:
                err_text = (await proc.stderr.read()).decode("utf-8", "ignore")[:1500]

            if got_any and not err_text:
                break   # успех (или частичный вывод) — не повторяем
            # ответа нет и ошибка транзиентная → пауза и повтор
            if attempt < MAX_TRIES - 1 and not got_any and \
                    any(p in err_text.lower() for p in TRANSIENT):
                st.thinking = True
                await asyncio.sleep(2.5 * (attempt + 1))
                err_text = ""
                continue
            break
    finally:
        st.thinking = False
        if rot:
            rot.cancel()
        try:
            if proc:
                await proc.wait()
        except Exception:
            pass
        RUNNING.pop(key, None)
        db.execute("DELETE FROM config WHERE key=?", (f"pending_{key}",))  # задача дошла — не возобновлять
        db.commit()

    if not got_any and err_text:
        if any(p in err_text.lower() for p in TRANSIENT):
            await st.feed(
                f"\n⏳ Сервер Клода вернул временную ошибку (перегрузка/500) и не ответил "
                f"после {MAX_TRIES} попыток. Повтори запрос через минуту — обычно проходит.",
                force=True)
        else:
            await st.feed(f"\n❌ claude завершился с ошибкой:\n{err_text}", force=True)

    # обновляем sid + накопленные токены
    row = topic_get(topic["chat_id"], topic["thread_id"])
    topic_upsert(
        topic["chat_id"], topic["thread_id"],
        session_id=new_sid,
        cost=(row["cost"] or 0) + cost,
        in_tok=(row["in_tok"] or 0) + in_tok,
        out_tok=(row["out_tok"] or 0) + out_tok,
    )
    if win_tok:
        cfg_set(f"ctx_{key}", str(win_tok))   # реальный размер окна модели (пик по assistant) — сигнал авто-компакта
    mname = MODELS.get(topic["model_key"], MODELS["opus"])[0]
    sid_short = (new_sid or "?")[:8]
    if cfg_get("show_footer", "0") == "1":
        await st.feed(
            f"\n\n— — —\n📊 токенов: вход {in_tok} · выход {out_tok} · {mname}\n"
            f"≈{cost:.2f}$ по API-тарифу (по подписке НЕ списывается) · сессия {sid_short}",
            force=True,
        )
    await st.finalize()
    # сдвигаем позицию зеркала к концу — свой же ответ не дублируем в топик
    try:
        MIRROR_POS[key] = (SESS_DIR / f"{new_sid}.jsonl").stat().st_size
    except Exception:
        pass
    # авто-подстройка имени по контексту (если включено и вкладка не зафиксирована)
    try:
        if cfg_get("auto_rename", "0") == "1" and topic["thread_id"]:
            row2 = topic_get(topic["chat_id"], topic["thread_id"])
            if row2 and not row2["locked"]:
                nn = await smart_title(new_sid)
                if nn and nn != (row2["name"] or ""):
                    await bot.edit_forum_topic(topic["chat_id"], topic["thread_id"], name=nn)
                    topic_upsert(topic["chat_id"], topic["thread_id"], name=nn)
    except Exception:
        pass
    # очередь: разбираем докинутое — общим заходом или по одной (см. /queuemode)
    q = QUEUES.get(key)
    if q:
        if cfg_get("queue_mode", "batch") == "each":
            nxt_text = q.pop(0)
            if not q:
                QUEUES.pop(key, None)
        else:
            nxt_text = "\n\n".join(q)
            QUEUES.pop(key, None)
        nxt = topic_get(topic["chat_id"], topic["thread_id"])
        if nxt:
            await run_claude(bot, nxt, nxt_text)


# ── клавиатуры ──────────────────────────────────────────────────────────────────
# ── i18n: язык интерфейса (RU по умолчанию, переключатель /lang) ──────────────
LANGS = {"ru": "Русский", "en": "English"}


def curlang():
    v = cfg_get("lang", "ru")
    return v if v in LANGS else "ru"


STRINGS = {
    "ru": {
        "btn_new": "➕ Новая сессия",
        "btn_sessions": "📂 История сессий",
        "btn_model": "⚙️ Модель",
        "btn_cost": "💰 Токены",
        "start_locked": "🔒 Бот не активирован. Владелец активирует так:\n/start <секрет>",
        "start_owner_ok": "✅ Ты владелец бота.",
        "start_private": (
            "✅ Активирован — ты владелец.\n\n"
            "Я работаю в <b>супергруппе с Темами</b>, а не в личке "
            "(в личке Telegram не даёт создавать вкладки-сессии).\n"
            "Открой свою группу и жми там «➕ Новая сессия» / «📂 История», "
            "либо команды /new, /sessions."),
        "start_group": (
            "🎛 <b>Пульт Claude Code</b>\n\n"
            "Каждый топик группы = отдельная сессия на сервере.\n"
            "• «➕ Новая сессия» — создаю топик и запускаю свежую сессию\n"
            "• «📂 История сессий» — открыть прошлую сессию (те самые ветки)\n"
            "• пиши текст или <b>голосом</b> прямо в топик — это уйдёт в его сессию\n\n"
            "Команды: /new /sessions /model /cwd /cost /ultra /stop /help"),
        "help_text": (
            "Команды:\n"
            "/new [имя] — новый топик+сессия\n"
            "/sessions — список прошлых сессий (с пагинацией)\n"
            "/import [N] — сразу создать вкладки для N последних сессий\n"
            "/id — id сессии + команда продолжить на ПК\n"
            "/mirror — зеркалить работу из VS Code в этот топик\n"
            "/automirror — авто-зеркало ВСЕХ сессий (создаёт вкладки сам)\n"
            "/dedup — удалить дубли-вкладки\n"
            "/voice — провайдер распознавания голоса\n"
            "/lang — язык интерфейса (RU/EN)\n"
            "/settings — все настройки (тихий режим, подробность)\n"
            "/thinking — стиль индикатора «думает…»\n"
            "/verbose — подробность: всё / прогресс без кода / только ответ\n"
            "/queuemode — как обрабатывать докинутые задачи\n"
            "/restart — перезапустить бота\n"
            "/rename <имя> — переименовать этот топик\n"
            "/model — выбрать модель для этого топика\n"
            "/cwd <путь> — рабочая папка сессии (по умолч. /root)\n"
            "/ultra — вкл/выкл режим ultracode\n"
            "/cost — потрачено токенов в этом топике\n"
            "/usage — суммарный расход + про лимиты\n"
            "/footer — подпись со статистикой под ответом вкл/выкл\n"
            "/stop — прервать текущий запуск\n"
            "/whoami — твой id и владелец бота"),
        "settings_title": (
            "⚙️ <b>Настройки</b>\nЖми, чтобы переключать. По умолчанию бот работает "
            "тихо — как Claude, без служебных уведомлений."),
        "set_verbosity": "👁 Подробность",
        "set_indicator": "💭 Индикатор",
        "set_queue": "🔀 Очередь",
        "queue_each": "по одной",
        "queue_batch": "общим",
        "set_smart_titles": "AI-названия вкладок",
        "set_auto_rename": "Подстраивать имя по ходу",
        "set_mirror_activity": "Индикатор «работает» в зеркале",
        "set_custom_emoji": "Наши иконки в ответах",
        "set_auto_compact": "Авто-сжатие контекста (по умолч. выкл)",
        "set_default_ultra": "Новые сессии — сразу ultracode",
        "set_notify_queue": "Уведомлять об очереди",
        "set_notify_voice": "Показывать расшифровку голоса",
        "set_language": "🌐 Язык / Language",
        "lang_pick": "🌐 Выбери язык интерфейса:",
        "lang_set": "✅ Язык интерфейса: Русский",
        "donate_text": (
            "🙏 <b>Поддержать автора</b>\n\n"
            "Pulse — бесплатный и открытый. Если он полезен, можно поблагодарить "
            "(по желанию, адреса тап-копи):\n\n"
            "• USDT (TRC-20):\n<code>TCq4uGpFcKCJU4fFAZbYzDfLCwG1yAPhN7</code>\n"
            "• TON:\n<code>UQD3FL-hS5xziU9AW0qL4WnJ13MGCGxZvyANiO4PZE-RcsXR</code>\n"
            "• Telegram: @naworkal\n\n"
            "Спасибо, что пользуешься Pulse! 💛"),
    },
    "en": {
        "btn_new": "➕ New session",
        "btn_sessions": "📂 Session history",
        "btn_model": "⚙️ Model",
        "btn_cost": "💰 Tokens",
        "start_locked": "🔒 Bot is not activated. The owner activates it like this:\n/start <secret>",
        "start_owner_ok": "✅ You are the bot owner.",
        "start_private": (
            "✅ Activated — you are the owner.\n\n"
            "I work in a <b>supergroup with Topics</b>, not in a private chat "
            "(Telegram doesn't allow creating session tabs in DMs).\n"
            "Open your group and tap «➕ New session» / «📂 History» there, "
            "or use /new, /sessions."),
        "start_group": (
            "🎛 <b>Claude Code remote</b>\n\n"
            "Each group topic = a separate session on the server.\n"
            "• «➕ New session» — I create a topic and start a fresh session\n"
            "• «📂 Session history» — open a past session (the same threads)\n"
            "• send text or a <b>voice message</b> right into a topic — it goes to that session\n\n"
            "Commands: /new /sessions /model /cwd /cost /ultra /stop /help"),
        "help_text": (
            "Commands:\n"
            "/new [name] — new topic + session\n"
            "/sessions — list past sessions (paginated)\n"
            "/import [N] — create tabs for the N latest sessions at once\n"
            "/id — session id + command to continue on your PC\n"
            "/mirror — mirror work from VS Code into this topic\n"
            "/automirror — auto-mirror ALL sessions (creates tabs itself)\n"
            "/dedup — remove duplicate tabs\n"
            "/voice — speech recognition provider\n"
            "/lang — interface language (RU/EN)\n"
            "/settings — all settings (quiet mode, verbosity)\n"
            "/thinking — style of the “thinking…” indicator\n"
            "/verbose — verbosity: all / progress without code / reply only\n"
            "/queuemode — how to handle queued tasks\n"
            "/restart — restart the bot\n"
            "/rename <name> — rename this topic\n"
            "/model — pick the model for this topic\n"
            "/cwd <path> — session working dir (default /root)\n"
            "/ultra — toggle ultracode mode\n"
            "/cost — tokens spent in this topic\n"
            "/usage — total spend + limits info\n"
            "/footer — stats footer under replies on/off\n"
            "/stop — interrupt the current run\n"
            "/whoami — your id and the bot owner"),
        "settings_title": (
            "⚙️ <b>Settings</b>\nTap to toggle. By default the bot runs "
            "quietly — like Claude, without service notifications."),
        "set_verbosity": "👁 Verbosity",
        "set_indicator": "💭 Indicator",
        "set_queue": "🔀 Queue",
        "queue_each": "one by one",
        "queue_batch": "batched",
        "set_smart_titles": "AI tab names",
        "set_auto_rename": "Adjust name as it goes",
        "set_mirror_activity": "“Working” indicator in mirror",
        "set_custom_emoji": "Our icons in replies",
        "set_auto_compact": "Auto context compaction (off by default)",
        "set_default_ultra": "New sessions start in ultracode",
        "set_notify_queue": "Notify about the queue",
        "set_notify_voice": "Show voice transcription",
        "set_language": "🌐 Язык / Language",
        "lang_pick": "🌐 Choose interface language:",
        "lang_set": "✅ Interface language: English",
        "donate_text": (
            "🙏 <b>Support the author</b>\n\n"
            "Pulse is free and open-source. If it's useful, you can say thanks "
            "(optional, addresses are tap-to-copy):\n\n"
            "• USDT (TRC-20):\n<code>TCq4uGpFcKCJU4fFAZbYzDfLCwG1yAPhN7</code>\n"
            "• TON:\n<code>UQD3FL-hS5xziU9AW0qL4WnJ13MGCGxZvyANiO4PZE-RcsXR</code>\n"
            "• Telegram: @naworkal\n\n"
            "Thanks for using Pulse! 💛"),
    },
}


def L(key, **kw):
    d = STRINGS.get(curlang(), STRINGS["ru"])
    s = d.get(key) or STRINGS["ru"].get(key, key)
    return s.format(**kw) if kw else s


def lang_kb():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(("✅ " if k == curlang() else "") + name,
                               callback_data=f"setlang:{k}")]
         for k, name in LANGS.items()])


async def cmd_lang(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    await update.effective_message.reply_text(L("lang_pick"), reply_markup=lang_kb())


async def cmd_donate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    await update.effective_message.reply_text(L("donate_text"), parse_mode="HTML")


def main_kb():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(L("btn_new"), callback_data="new"),
                InlineKeyboardButton(L("btn_sessions"), callback_data="sessions"),
            ],
            [
                InlineKeyboardButton(L("btn_model"), callback_data="model"),
                InlineKeyboardButton(L("btn_cost"), callback_data="cost"),
            ],
        ]
    )


def model_kb():
    rows = []
    for k, (label, _) in MODELS.items():
        rows.append([InlineKeyboardButton(label, callback_data=f"setmodel:{k}")])
    return InlineKeyboardMarkup(rows)


# ── хендлеры ────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    owner = cfg_get("owner_id")
    if owner is None:
        provided = " ".join(context.args).strip() if context.args else ""
        if OWNER_SETUP_SECRET and provided != OWNER_SETUP_SECRET:
            await update.effective_message.reply_text(L("start_locked"))
            return
        cfg_set("owner_id", uid)
        owner = uid
        await update.effective_message.reply_text(L("start_owner_ok"))
    if str(uid) != str(owner):
        return  # тихо игнорим чужих
    if update.effective_chat.type == "private":
        await update.effective_message.reply_text(L("start_private"), parse_mode="HTML")
        return
    await update.effective_message.reply_text(
        L("start_group"), parse_mode="HTML", reply_markup=main_kb())


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    await update.effective_message.reply_text(L("help_text"))


async def cmd_whoami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        f"твой id: {update.effective_user.id}\nвладелец: {cfg_get('owner_id')}"
    )


async def post_recap(context, chat_id, thread_id, sid):
    """Постит в топик последние сообщения сессии — «продолжаем отсюда»."""
    p = SESS_DIR / f"{sid}.jsonl"
    msgs = session_last_messages(p) if p.exists() else []
    if not msgs:
        return
    budget, chosen = 3500, []
    for role, text in reversed(msgs):
        who = "🧑 ты" if role == "user" else "🤖 Клод"
        block = f"\n\n<b>{who}:</b> {html.escape(text[:500])}"
        if chosen and budget - len(block) < 0:
            break
        budget -= len(block)
        chosen.append(block)
    chosen.reverse()
    await context.bot.send_message(
        chat_id,
        "🧷 <b>Последние сообщения — продолжаем отсюда:</b>" + "".join(chosen),
        message_thread_id=thread_id, parse_mode="HTML",
    )


async def do_new_topic(context, chat_id, name=None):
    name = (name or "сессия")[:120]
    try:
        topic = await context.bot.create_forum_topic(
            chat_id, name=name, icon_custom_emoji_id=topic_icon_id(name))
    except Exception:
        topic = await context.bot.create_forum_topic(chat_id, name=name)
    tid = topic.message_thread_id
    topic_upsert(chat_id, tid, name=name, session_id=None,
                 ultra=int(cfg_get("default_ultra", "0")))
    await context.bot.send_message(
        chat_id,
        f"🆕 <b>{html.escape(name)}</b>\nНовая сессия. Пиши задачу текстом или голосом — я начну.",
        message_thread_id=tid,
        parse_mode="HTML",
    )
    return tid


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    if not getattr(update.effective_chat, "is_forum", False):
        await update.effective_message.reply_text(
            "⚠️ /new работает в супергруппе с включёнными Темами — не в личке. "
            "Открой группу и вызови там."
        )
        return
    name = " ".join(context.args) if context.args else None
    try:
        await do_new_topic(context, update.effective_chat.id, name)
    except (BadRequest, Forbidden) as e:
        await update.effective_message.reply_text(
            f"Не смог создать топик: {e}\n"
            "Проверь: это супергруппа с включёнными Топиками и бот — админ с правом «Управление темами»."
        )


async def cmd_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    await send_sessions_list(context, update.effective_chat.id,
                             update.effective_message.message_thread_id)


async def send_sessions_list(context, chat_id, thread_id, offset=0):
    PAGE = 14
    items, total = list_sessions(limit=PAGE, offset=offset)
    if not items:
        await context.bot.send_message(chat_id, "Прошлых сессий не нашёл.",
                                       message_thread_id=thread_id)
        return
    rows = [[InlineKeyboardButton(f"{when} · {prev}", callback_data=f"open:{sid}")]
            for sid, when, prev in items]
    if offset + PAGE < total:
        rows.append([InlineKeyboardButton(
            f"⬇️ Ещё ({total - offset - PAGE})",
            callback_data=f"sessions:{offset + PAGE}")])
    head = f"📂 Сессии {offset + 1}–{offset + len(items)} из {total} — выбери:"
    await context.bot.send_message(
        chat_id, head,
        message_thread_id=thread_id, reply_markup=InlineKeyboardMarkup(rows),
    )


async def cmd_import(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    if not getattr(update.effective_chat, "is_forum", False):
        await update.effective_message.reply_text(
            "⚠️ /import работает в супергруппе с Темами."
        )
        return
    try:
        n = int(context.args[0]) if context.args else 10
    except Exception:
        n = 10
    n = max(1, min(n, 25))
    chat_id = update.effective_chat.id
    if chat_id in IMPORT_RUNNING:
        await update.effective_message.reply_text("⏳ Уже создаю вкладки, подожди…")
        return
    IMPORT_RUNNING.add(chat_id)
    try:
        bound = {
            r["session_id"] for r in db.execute(
                "SELECT session_id FROM topics WHERE session_id IS NOT NULL"
            ).fetchall()
        }
        items, _total = list_sessions(limit=n + len(bound) + 5, offset=0)
        await update.effective_message.reply_text(
            f"⏳ Создаю вкладки для последних сессий (до {n})…"
        )
        created = 0
        for sid, when, prev in items:
            if created >= n:
                break
            if sid in bound:
                continue
            try:
                new_tid = await do_new_topic(context, chat_id, await smart_title(sid))
            except (BadRequest, Forbidden) as e:
                await update.effective_message.reply_text(f"Остановился: {e}")
                break
            except (TimedOut, NetworkError, RetryAfter):
                await asyncio.sleep(2)
                continue
            topic_upsert(chat_id, new_tid, session_id=sid)
            bound.add(sid)
            await post_recap(context, chat_id, new_tid, sid)
            created += 1
            await asyncio.sleep(1.2)
        await update.effective_message.reply_text(
            f"✅ Готово: создано вкладок — {created}."
        )
    finally:
        IMPORT_RUNNING.discard(chat_id)


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    tid = update.effective_message.message_thread_id
    t = topic_get(update.effective_chat.id, tid)
    if not t or not t["session_id"]:
        await update.effective_message.reply_text(
            "В этом топике ещё нет сессии — напиши что-нибудь, и она создастся."
        )
        return
    sid = t["session_id"]
    await update.effective_message.reply_text(
        f"🧩 <b>Сессия топика:</b> <code>{sid}</code>\n"
        f"<i>(тапни по строчкам ниже — скопируются)</i>\n\n"
        f"▶️ <b>Продолжить в Claude (VS Code/терминал):</b>\n"
        f"<code>claude --resume {sid}</code>\n\n"
        f"💬 <b>Живой чат на ПК (печатаешь + ответ тут же):</b>\n"
        f"<code>python3 /root/tg-claude-bot/chat_session.py {sid}</code>\n\n"
        f"👀 <b>Только смотреть живьём:</b>\n"
        f"<code>python3 /root/tg-claude-bot/watch_session.py {sid}</code>\n\n"
        f"Терминал в VS Code: меню <b>Terminal → New Terminal</b>. "
        f"Это всё по желанию — для обычной работы через Телеграм терминал не нужен.",
        parse_mode="HTML",
    )


async def cmd_ctx(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает размер контекста топика + статус/рекомендацию (видимость окна)."""
    if not is_owner(update):
        return
    tid = update.effective_message.message_thread_id
    t = topic_get(update.effective_chat.id, tid)
    if not t or not t["session_id"]:
        await update.effective_message.reply_text(
            "В этом топике ещё нет сессии — напиши что-нибудь, и она создастся.")
        return
    p = SESS_DIR / f"{t['session_id']}.jsonl"
    try:
        mb = p.stat().st_size / (1024 * 1024)
    except Exception:
        mb = 0
    if mb < 15:
        badge, tip = "🟢 в норме", "Всё ок, сжимать не нужно."
    elif mb < 60:
        badge, tip = "🟡 крупный", "Уже большой. Если начнёт ловить ошибки/тормозить — жми /compact."
    else:
        badge, tip = "🔴 распухший", ("Очень большой — вероятны 500/таймауты. "
                                      "Рекомендую /compact (сжать) или /new (свежая сессия).")
    await update.effective_message.reply_text(
        f"🧠 <b>Контекст этого топика</b>\n"
        f"Размер сессии: <b>{mb:.0f} МБ</b> — {badge}\n\n{tip}\n\n"
        f"• /compact — сжать диалог (суммирует, нить сохранится)\n"
        f"• /new — начать свежую лёгкую сессию (старая история не пропадёт)",
        parse_mode="HTML")


async def cmd_compact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Сжимает контекст сессии топика через штатный /compact Claude Code."""
    if not is_owner(update):
        return
    chat_id = update.effective_chat.id
    tid = update.effective_message.message_thread_id
    t = topic_get(chat_id, tid)
    if not t or not t["session_id"]:
        await update.effective_message.reply_text("В этом топике ещё нет сессии — сжимать нечего.")
        return
    if tkey(chat_id, tid) in RUNNING:
        await update.effective_message.reply_text(
            "Сейчас идёт задача — дождись ответа и повтори /compact.")
        return
    await update.effective_message.reply_text(
        "🗜 Сжимаю контекст: суммирую диалог, чтобы уменьшить окно и убрать 500-е. Нить сохранится…")
    await run_claude(context.bot, t, "/compact")


async def cmd_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    cur = cfg_get("voice_provider", "auto")
    avail = _voice_providers()
    rows = [[InlineKeyboardButton(
        ("✅ " if cur == "auto" else "") + VOICE_LABELS["auto"],
        callback_data="setvoice:auto")]]
    for prov in ("deepgram", "yandex", "gemini", "openai", "vertex"):
        mark = "✅ " if prov == cur else ""
        tail = "" if prov in avail else " — нет ключа"
        rows.append([InlineKeyboardButton(
            f"{mark}{VOICE_LABELS[prov]}{tail}", callback_data=f"setvoice:{prov}")])
    await update.effective_message.reply_text(
        f"🎙 Провайдер распознавания голоса.\n"
        f"Сейчас: <b>{VOICE_LABELS.get(cur, cur)}</b>\n"
        f"Доступны: {', '.join(avail) or 'нет'}\n"
        f"«Авто» = пробует по очереди с фолбэком (если у одного кончились деньги — берёт следующий).",
        parse_mode="HTML", reply_markup=InlineKeyboardMarkup(rows),
    )


async def cmd_rename(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    tid = update.effective_message.message_thread_id
    if not tid:
        await update.effective_message.reply_text("/rename работает внутри топика.")
        return
    if not context.args:
        await update.effective_message.reply_text("Как назвать? Пример: /rename ПивМастер — задача")
        return
    name = " ".join(context.args)[:120]
    try:
        await context.bot.edit_forum_topic(update.effective_chat.id, tid, name=name)
    except Exception as e:
        await update.effective_message.reply_text(f"Не смог переименовать: {e}")
        return
    topic_upsert(update.effective_chat.id, tid, name=name, locked=1)
    await update.effective_message.reply_text(
        f"✅ Тема переименована: {name}\n🔒 Зафиксировано — авто-переименование её больше не тронет.")


async def on_topic_edited(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    msg = update.effective_message
    edited = getattr(msg, "forum_topic_edited", None)
    if edited and edited.name:
        topic_upsert(update.effective_chat.id, msg.message_thread_id,
                     name=edited.name, locked=1)


async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    await update.effective_message.reply_text("Модель для этого топика:",
                                              reply_markup=model_kb())


async def cmd_cwd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    tid = update.effective_message.message_thread_id
    if not context.args:
        t = topic_get(update.effective_chat.id, tid)
        cur = t["cwd"] if t else DEFAULT_CWD
        await update.effective_message.reply_text(f"текущая папка: {cur}\nсменить: /cwd /root/pivmaster-dashboard")
        return
    path = context.args[0]
    if not Path(path).is_dir():
        await update.effective_message.reply_text(f"нет такой папки: {path}")
        return
    topic_upsert(update.effective_chat.id, tid, cwd=path)
    await update.effective_message.reply_text(f"✅ рабочая папка: {path}")


async def cmd_ultra(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    tid = update.effective_message.message_thread_id
    t = topic_upsert(update.effective_chat.id, tid)
    newv = 0 if t["ultra"] else 1
    topic_upsert(update.effective_chat.id, tid, ultra=newv)
    await update.effective_message.reply_text(
        f"ultracode: {'ВКЛ 🔥' if newv else 'выкл'}"
    )


async def cmd_mirror(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    tid = update.effective_message.message_thread_id
    t = topic_upsert(update.effective_chat.id, tid)
    newv = 0 if t["mirror"] else 1
    topic_upsert(update.effective_chat.id, tid, mirror=newv)
    key = tkey(update.effective_chat.id, tid)
    if newv and t["session_id"]:
        try:
            MIRROR_POS[key] = (SESS_DIR / f"{t['session_id']}.jsonl").stat().st_size
        except Exception:
            pass
    await update.effective_message.reply_text(
        "🖥 Зеркало ПК→топик: ВКЛ. Теперь работа по этой сессии в VS Code/терминале "
        "будет прилетать сюда живьём (токенов не тратит)."
        if newv else "🖥 Зеркало ПК→топик: выкл."
    )


async def cmd_automirror(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    if not getattr(update.effective_chat, "is_forum", False):
        await update.effective_message.reply_text(
            "⚠️ Включай в супергруппе с Темами."
        )
        return
    newv = "0" if cfg_get("automirror", "0") == "1" else "1"
    cfg_set("automirror", newv)
    if newv == "1":
        cfg_set("automirror_chat", str(update.effective_chat.id))
        cfg_set("automirror_since", str(time.time()))
        msg = ("🪞 Авто-зеркало ВСЕХ сессий: ВКЛ.\n"
               "Любая сессия, в которой поработаешь (VS Code/терминал), сама появится "
               "здесь вкладкой с живым зеркалом и сводкой. Старые не тащу — только "
               "новую активность. Выключить — /automirror ещё раз.")
    else:
        msg = "🪞 Авто-зеркало всех сессий: выкл."
    await update.effective_message.reply_text(msg)


async def cmd_dedup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    chat_id = update.effective_chat.id
    rows = db.execute(
        "SELECT thread_id,session_id FROM topics "
        "WHERE session_id IS NOT NULL AND chat_id=? ORDER BY thread_id", (chat_id,)
    ).fetchall()
    seen, removed = set(), 0
    for r in rows:
        if r["session_id"] in seen:
            try:
                await context.bot.delete_forum_topic(chat_id, r["thread_id"])
            except Exception:
                pass
            db.execute("DELETE FROM topics WHERE id=?", (tkey(chat_id, r["thread_id"]),))
            db.commit()
            removed += 1
        else:
            seen.add(r["session_id"])
    await update.effective_message.reply_text(f"🧹 Удалил дублей-вкладок: {removed}.")


async def cmd_tidy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    chat_id = update.effective_chat.id
    rows = db.execute(
        "SELECT thread_id,session_id FROM topics "
        "WHERE session_id IS NOT NULL AND chat_id=? AND thread_id>0 "
        "AND (locked IS NULL OR locked=0)", (chat_id,)
    ).fetchall()
    await update.effective_message.reply_text(
        f"🧹 Навожу порядок в названиях ({len(rows)}, зафиксированные не трогаю)…")
    done = 0
    for r in rows:
        p = SESS_DIR / f"{r['session_id']}.jsonl"
        if not p.exists():
            continue
        name = await smart_title(r["session_id"])
        try:
            await context.bot.edit_forum_topic(chat_id, r["thread_id"], name=name)
            topic_upsert(chat_id, r["thread_id"], name=name)
            done += 1
        except Exception:
            pass
        await asyncio.sleep(0.5)
    await update.effective_message.reply_text(f"✅ Переименовано вкладок: {done}.")


async def cmd_cost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    tid = update.effective_message.message_thread_id
    t = topic_get(update.effective_chat.id, tid)
    if not t:
        await update.effective_message.reply_text("В этом топике ещё нет сессии.")
        return
    await update.effective_message.reply_text(
        f"📊 в этом топике: вход {t['in_tok']} · выход {t['out_tok']} токенов\n"
        f"≈{t['cost']:.2f}$ по API-тарифу (по подписке не списывается)"
    )


async def cmd_usage(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    r = db.execute(
        "SELECT COALESCE(SUM(in_tok),0) i, COALESCE(SUM(out_tok),0) o, "
        "COALESCE(SUM(cost),0) c FROM topics"
    ).fetchone()
    await update.effective_message.reply_text(
        f"📊 <b>Расход через бота</b> (все топики суммарно):\n"
        f"вход {r['i']} · выход {r['o']} токенов\n"
        f"≈{r['c']:.2f}$ по API-тарифу (по подписке НЕ списывается)\n\n"
        f"⚠️ Точный <b>остаток</b> лимита (недельный/суточный) Anthropic наружу не "
        f"отдаёт — ни в CLI, ни по API. Единственный надёжный сигнал: упрёшься — Claude "
        f"сам напишет «лимит исчерпан, сброс через N часов», и это придёт сюда.",
        parse_mode="HTML",
    )


async def cmd_footer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    newv = "0" if cfg_get("show_footer", "0") == "1" else "1"
    cfg_set("show_footer", newv)
    await update.effective_message.reply_text(
        f"📊 Подпись со статистикой под ответом: {'ВКЛ' if newv == '1' else 'ВЫКЛ'}."
    )


async def cmd_queuemode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    newv = "each" if cfg_get("queue_mode", "batch") == "batch" else "batch"
    cfg_set("queue_mode", newv)
    await update.effective_message.reply_text(
        "🔀 Докинутые задачи: ПО ОЧЕРЕДИ — каждая свой ответ, одна за другой."
        if newv == "each" else
        "🔀 Докинутые задачи: ОБЩИМ заходом — все вместе, один ответ "
        "(прогресс по шагам виден в процессе)."
    )


async def cmd_verbose(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    order = ["full", "progress", "final"]
    cur = cfg_get("verbosity", "full")
    nxt = order[(order.index(cur) + 1) % 3] if cur in order else "full"
    cfg_set("verbosity", nxt)
    await update.effective_message.reply_text(f"👁 Подробность: {VERB_LABELS[nxt]}")


async def cmd_restart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    await update.effective_message.reply_text("♻️ Перезапускаюсь с новым кодом…")
    os.execv(sys.executable, [sys.executable, os.path.abspath(__file__)])


async def cmd_thinking(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    cur = cfg_get("think_style", "claude")
    rows = [[InlineKeyboardButton(
        ("✅ " if k == cur else "") + v, callback_data=f"setthink:{k}")]
        for k, v in THINK_LABELS.items()]
    await update.effective_message.reply_text(
        f"💭 Стиль индикатора «думает…». Сейчас: {THINK_LABELS.get(cur, cur)}",
        reply_markup=InlineKeyboardMarkup(rows))


async def cmd_thinkicon(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    cur = cfg_get("think_emoji", "pulse")
    rows = [[InlineKeyboardButton(("✅ " if k == cur else "") + v,
                                  callback_data=f"seticon:{k}")]
            for k, v in THINK_EMOJI_LABELS.items()]
    await update.effective_message.reply_text(
        "⚡ Анимир-иконка индикатора «думает» (анимация видна с Telegram Premium):",
        reply_markup=InlineKeyboardMarkup(rows))


def settings_kb():
    def m(key, label, default="0"):
        return ("✅ " if cfg_get(key, default) == "1" else "▫️ ") + label
    verb = VERB_LABELS.get(cfg_get("verbosity", "full"), "?")
    ts = THINK_LABELS.get(cfg_get("think_style", "claude"), "?")
    qm = L("queue_each") if cfg_get("queue_mode", "batch") == "each" else L("queue_batch")
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{L('set_verbosity')}: {verb}", callback_data="cyc:verbosity")],
        [InlineKeyboardButton(f"{L('set_indicator')}: {ts}", callback_data="cyc:think_style")],
        [InlineKeyboardButton(f"{L('set_queue')}: {qm}", callback_data="cyc:queue_mode")],
        [InlineKeyboardButton(m("smart_titles", L("set_smart_titles"), "1"),
                              callback_data="tog:smart_titles")],
        [InlineKeyboardButton(m("auto_rename", L("set_auto_rename")),
                              callback_data="tog:auto_rename")],
        [InlineKeyboardButton(m("mirror_activity", L("set_mirror_activity"), "1"),
                              callback_data="tog:mirror_activity")],
        [InlineKeyboardButton(m("custom_emoji", L("set_custom_emoji"), "1"),
                              callback_data="tog:custom_emoji")],
        [InlineKeyboardButton(m("auto_compact", L("set_auto_compact"), "0"),
                              callback_data="tog:auto_compact")],
        [InlineKeyboardButton(m("default_ultra", L("set_default_ultra")),
                              callback_data="tog:default_ultra")],
        [InlineKeyboardButton(m("notify_queue", L("set_notify_queue")),
                              callback_data="tog:notify_queue")],
        [InlineKeyboardButton(m("notify_voice", L("set_notify_voice")),
                              callback_data="tog:notify_voice")],
        [InlineKeyboardButton(L("set_language"), callback_data="lang")],
    ])


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    await update.effective_message.reply_text(
        L("settings_title"), parse_mode="HTML", reply_markup=settings_kb())


async def cmd_stop(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    tid = update.effective_message.message_thread_id
    key = tkey(update.effective_chat.id, tid)
    q = QUEUES.pop(key, None)
    proc = RUNNING.pop(key, None)   # снимаем флаг занятости (в т.ч. зависший ghost) — топик свободен
    if proc and proc != "starting":
        try:
            proc.terminate()
        except Exception:
            pass
        await update.effective_message.reply_text(
            "⏹ Прервал." + (f" Очередь очищена ({len(q)})." if q else "")
        )
    elif proc == "starting":
        await update.effective_message.reply_text(
            "⏹ Снял зависший флаг — топик свободен, пиши заново."
            + (f" Очередь очищена ({len(q)})." if q else ""))
    elif q:
        await update.effective_message.reply_text(f"🗑 Очередь очищена ({len(q)}).")
    else:
        await update.effective_message.reply_text("Здесь ничего не выполняется.")


async def cmd_retry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Повторить последний запрос топика (удобно после 500/обрыва)."""
    if not is_owner(update):
        return
    chat_id = update.effective_chat.id
    tid = update.effective_message.message_thread_id
    key = tkey(chat_id, tid)
    if key in RUNNING:
        await update.effective_message.reply_text("Сейчас идёт задача — дождись ответа.")
        return
    text = LAST_PROMPT.get(key)
    if not text:
        await update.effective_message.reply_text(
            "В этом топике ещё нет запроса для повтора.")
        return
    await dispatch(context.bot, chat_id, tid, text)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Что выполняется сейчас (по всем топикам) + размер контекста этого топика."""
    if not is_owner(update):
        return
    busy = []
    for k, v in list(RUNNING.items()):
        state = "запускается" if v == "starting" else "работает"
        qn = len(QUEUES.get(k, []))
        busy.append(f"• <code>{k}</code> — {state}" + (f", очередь {qn}" if qn else ""))
    t = topic_get(update.effective_chat.id, update.effective_message.message_thread_id)
    ctx_line = ""
    if t and t["session_id"]:
        try:
            mb = (SESS_DIR / f"{t['session_id']}.jsonl").stat().st_size / (1024 * 1024)
            ctx_line = f"\n\n🧠 Контекст этого топика: <b>{mb:.0f} МБ</b>"
        except Exception:
            pass
    mode_line = ""
    if t:
        mname = MODELS.get(t["model_key"], MODELS["opus"])[0]
        um = "🚀 ультракод" if t["ultra"] else "обычный"
        vb = VERB_LABELS.get(cfg_get("verbosity", "full"), cfg_get("verbosity", "full"))
        mode_line = (f"\n\n🎛 <b>Режим топика:</b> {mname} · {um}\n"
                     f"Подробность: {vb} · /model /ultra /verbose — сменить")
    body = ("🟢 Сейчас ничего не выполняется." if not busy
            else "⚙️ <b>Выполняется:</b>\n" + "\n".join(busy))
    await update.effective_message.reply_text(body + mode_line + ctx_line, parse_mode="HTML")


async def cmd_stopall(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Прервать все задачи во всех топиках и очистить очереди."""
    if not is_owner(update):
        return
    n = 0
    for proc in list(RUNNING.values()):
        if proc and proc != "starting":
            try:
                proc.terminate()
                n += 1
            except Exception:
                pass
    RUNNING.clear()   # снять и зависшие флаги (ghost) — все топики разблокируются
    QUEUES.clear()
    try:
        db.execute("DELETE FROM config WHERE key LIKE 'pending_%'")
        db.commit()
    except Exception:
        pass
    await update.effective_message.reply_text(
        f"⏹ Прервал все задачи ({n}), снял зависшие флаги и очистил очереди.")


async def cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показать очередь текущего топика."""
    if not is_owner(update):
        return
    key = tkey(update.effective_chat.id, update.effective_message.message_thread_id)
    q = QUEUES.get(key, [])
    if not q:
        await update.effective_message.reply_text("Очередь этого топика пуста.")
        return
    lines = "\n".join(f"{i + 1}. {t[:60]}" for i, t in enumerate(q[:10]))
    more = f"\n…и ещё {len(q) - 10}" if len(q) > 10 else ""
    await update.effective_message.reply_text(f"➕ В очереди ({len(q)}):\n{lines}{more}")


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_owner(update):
        await q.answer("нет доступа", show_alert=False)
        return
    await q.answer()
    data = q.data or ""
    chat_id = q.message.chat.id
    tid = q.message.message_thread_id

    if (data == "new" or data.startswith("open:")) and not getattr(
        q.message.chat, "is_forum", False
    ):
        await context.bot.send_message(
            chat_id,
            "⚠️ Вкладки-сессии создаются только в супергруппе с включёнными Темами — "
            "не в личке. Открой свою группу и жми кнопки там.",
        )
        return

    if data == "new":
        try:
            await do_new_topic(context, chat_id)
        except (BadRequest, Forbidden) as e:
            await context.bot.send_message(chat_id, f"Не смог создать топик: {e}")
    elif data == "sessions" or data.startswith("sessions:"):
        off = int(data.split(":", 1)[1]) if ":" in data else 0
        await send_sessions_list(context, chat_id, tid, offset=off)
    elif data == "model":
        await context.bot.send_message(chat_id, "Модель для этого топика:",
                                       message_thread_id=tid, reply_markup=model_kb())
    elif data == "cost":
        t = topic_get(chat_id, tid)
        txt = (f"💰 {t['cost']:.4f}$ · вход {t['in_tok']} · выход {t['out_tok']}"
               if t else "В этом топике ещё нет сессии.")
        await context.bot.send_message(chat_id, txt, message_thread_id=tid)
    elif data.startswith("setmodel:"):
        mk = data.split(":", 1)[1]
        if mk in MODELS:
            topic_upsert(chat_id, tid, model_key=mk)
            await context.bot.send_message(
                chat_id, f"✅ модель: {MODELS[mk][0]}", message_thread_id=tid
            )
    elif data.startswith("setvoice:"):
        prov = data.split(":", 1)[1]
        cfg_set("voice_provider", prov)
        await context.bot.send_message(
            chat_id, f"🎙 Распознавание: {VOICE_LABELS.get(prov, prov)}",
            message_thread_id=tid,
        )
    elif data.startswith("setthink:"):
        k = data.split(":", 1)[1]
        if k in THINK_STYLES:
            cfg_set("think_style", k)
            await context.bot.send_message(
                chat_id, f"💭 Индикатор: {THINK_LABELS.get(k, k)}",
                message_thread_id=tid)
    elif data.startswith("seticon:"):
        k = data.split(":", 1)[1]
        cfg_set("think_emoji", k)
        await context.bot.send_message(
            chat_id, f"⚡ Иконка: {THINK_EMOJI_LABELS.get(k, k)}",
            message_thread_id=tid)
    elif data.startswith("tog:"):
        k = data.split(":", 1)[1]
        cfg_set(k, "0" if cfg_get(k, "0") == "1" else "1")
        try:
            await q.edit_message_reply_markup(reply_markup=settings_kb())
        except Exception:
            pass
    elif data.startswith("cyc:"):
        k = data.split(":", 1)[1]
        order = {"verbosity": ["full", "progress", "final"],
                 "think_style": list(THINK_STYLES.keys()),
                 "queue_mode": ["batch", "each"]}.get(k, [])
        if order:
            cur = cfg_get(k, order[0])
            nxt = order[(order.index(cur) + 1) % len(order)] if cur in order else order[0]
            cfg_set(k, nxt)
        try:
            await q.edit_message_reply_markup(reply_markup=settings_kb())
        except Exception:
            pass
    elif data == "lang":
        await context.bot.send_message(chat_id, L("lang_pick"),
                                       message_thread_id=tid, reply_markup=lang_kb())
    elif data.startswith("setlang:"):
        lk = data.split(":", 1)[1]
        if lk in LANGS:
            cfg_set("lang", lk)
        try:
            await q.edit_message_text(L("lang_set"), reply_markup=lang_kb())
        except Exception:
            pass
    elif data.startswith("open:"):
        sid = data.split(":", 1)[1]
        prev = "сессия"
        p = SESS_DIR / f"{sid}.jsonl"
        if p.exists():
            prev = session_preview(p)
        try:
            new_tid = await do_new_topic(context, chat_id, await smart_title(sid))
        except (BadRequest, Forbidden) as e:
            await context.bot.send_message(chat_id, f"Не смог открыть сессию: {e}")
            return
        topic_upsert(chat_id, new_tid, session_id=sid)
        await context.bot.send_message(
            chat_id,
            f"↩️ подключил сессию <code>{sid[:8]}</code> с полной историей.\n"
            "Пиши — продолжим с того места.",
            message_thread_id=new_tid, parse_mode="HTML",
        )
        await post_recap(context, chat_id, new_tid, sid)


def _vertex_token_and_project():
    import json as _json
    from google.oauth2 import service_account
    import google.auth.transport.requests as _gart
    with open(VERTEX_SA) as f:
        info = _json.load(f)
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/cloud-platform"])
    creds.refresh(_gart.Request())
    return creds.token, info.get("project_id")


async def _asr_vertex(audio: bytes) -> str:
    import base64, httpx
    token, project = await asyncio.to_thread(_vertex_token_and_project)
    url = (f"https://{VERTEX_LOCATION}-aiplatform.googleapis.com/v1/projects/{project}"
           f"/locations/{VERTEX_LOCATION}/publishers/google/models/"
           f"{VERTEX_MODEL}:generateContent")
    body = {"contents": [{"role": "user", "parts": [
        {"inlineData": {"mimeType": "audio/ogg",
                        "data": base64.b64encode(audio).decode()}},
        {"text": "Транскрибируй это голосовое сообщение дословно. "
                 "Верни ТОЛЬКО распознанный текст, без комментариев и кавычек."},
    ]}]}
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(
            url, headers={"Authorization": f"Bearer {token}"}, json=body)
    if r.status_code == 200:
        for c in r.json().get("candidates", []):
            for p in (c.get("content") or {}).get("parts", []):
                if p.get("text"):
                    return p["text"].strip()
        return ""
    log.warning("vertex asr %s: %s", r.status_code, r.text[:200])
    return ""


async def _vertex_text(prompt: str) -> str:
    import httpx
    token, project = await asyncio.to_thread(_vertex_token_and_project)
    url = (f"https://{VERTEX_LOCATION}-aiplatform.googleapis.com/v1/projects/{project}"
           f"/locations/{VERTEX_LOCATION}/publishers/google/models/"
           f"{VERTEX_MODEL}:generateContent")
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}]}
    async with httpx.AsyncClient(timeout=40) as client:
        r = await client.post(url, headers={"Authorization": f"Bearer {token}"}, json=body)
    if r.status_code == 200:
        for c in r.json().get("candidates", []):
            for pt in (c.get("content") or {}).get("parts", []):
                if pt.get("text"):
                    return pt["text"].strip()
    log.warning("vertex text %s: %s", r.status_code, r.text[:150])
    return ""


async def _asr_gemini(audio: bytes) -> str:
    import base64, httpx
    url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
           f"{GEMINI_ASR_MODEL}:generateContent?key={GEMINI_API_KEY}")
    body = {"contents": [{"parts": [
        {"inline_data": {"mime_type": "audio/ogg",
                         "data": base64.b64encode(audio).decode()}},
        {"text": "Транскрибируй это голосовое сообщение дословно. "
                 "Верни ТОЛЬКО распознанный текст, без комментариев и кавычек."},
    ]}]}
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(url, json=body)
    if r.status_code == 200:
        for c in r.json().get("candidates", []):
            for p in (c.get("content") or {}).get("parts", []):
                if p.get("text"):
                    return p["text"].strip()
        return ""
    log.warning("gemini asr %s: %s", r.status_code, r.text[:200])
    return ""


async def _asr_openai(audio: bytes) -> str:
    import httpx
    async with httpx.AsyncClient(timeout=120) as client:
        r = await client.post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
            files={"file": ("voice.ogg", audio, "audio/ogg")},
            data={"model": "whisper-1"},
        )
    if r.status_code == 200:
        return (r.json().get("text") or "").strip()
    log.warning("whisper http %s: %s", r.status_code, r.text[:200])
    return ""


async def _asr_deepgram(audio: bytes) -> str:
    import httpx
    # Deepgram — единственный живой ASR (Vertex 403, OpenAI без ключа), поэтому
    # упорно ретраим: сеть/таймаут, 5xx И пустой транскрипт на 200 (транзиентный сбой).
    # Таймаут короткий (connect 10s / read 30s), чтобы зависшее соединение всплывало
    # быстро и мы успевали переспросить, а не висели 2 минуты.
    last = ""
    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
                r = await client.post(
                    "https://api.deepgram.com/v1/listen?model=nova-2&language=ru&smart_format=true",
                    headers={"Authorization": f"Token {DEEPGRAM_API_KEY}",
                             "Content-Type": "audio/ogg"},
                    content=audio,
                )
        except Exception as e:
            log.warning("deepgram try%s net: %r", attempt, e)
            if attempt < 2:
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            return ""
        if r.status_code == 200:
            try:
                last = r.json()["results"]["channels"][0]["alternatives"][0]["transcript"].strip()
            except Exception:
                last = ""
            if last:
                return last
            log.warning("deepgram try%s empty transcript", attempt)
            if attempt < 2:
                await asyncio.sleep(0.5 * (attempt + 1))
                continue
            return ""
        log.warning("deepgram %s: %s", r.status_code, r.text[:200])
        if r.status_code >= 500 and attempt < 2:
            await asyncio.sleep(0.5 * (attempt + 1))
            continue
        return ""
    return last


async def _asr_yandex(audio: bytes) -> str:
    import httpx
    if not (YANDEX_API_KEY and YANDEX_FOLDER_ID):
        return ""
    # SpeechKit v1 short-audio: OggOpus напрямую, ≤30с/1МБ; для коротких голосовых достаточно
    params = {"topic": "general", "lang": "ru-RU",
              "folderId": YANDEX_FOLDER_ID, "format": "oggopus"}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
            r = await client.post(
                "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize",
                params=params,
                headers={"Authorization": f"Api-Key {YANDEX_API_KEY}"},
                content=audio,
            )
    except Exception as e:
        log.warning("yandex asr net: %r", e)
        return ""
    if r.status_code == 200:
        return (r.json().get("result") or "").strip()
    log.warning("yandex asr %s: %s", r.status_code, r.text[:200])
    return ""


_ASR = {"vertex": _asr_vertex, "gemini": _asr_gemini,
        "openai": _asr_openai, "deepgram": _asr_deepgram,
        "yandex": _asr_yandex}


def _voice_providers():
    """Доступные провайдеры ASR в порядке приоритета.
    Vertex — последним: сейчас 403 (биллинг off), нельзя ставить его перед рабочим движком."""
    avail = []
    if GEMINI_API_KEY:
        avail.append("gemini")
    if OPENAI_API_KEY:
        avail.append("openai")
    if DEEPGRAM_API_KEY:
        avail.append("deepgram")
    if YANDEX_API_KEY and YANDEX_FOLDER_ID:
        avail.append("yandex")   # RU-страховка Deepgram: если тот икнёт, добираем Яндексом
    if os.path.exists(VERTEX_SA):
        avail.append("vertex")
    return avail


async def transcribe_voice(context, file_id) -> str:
    avail = _voice_providers()
    if not avail:
        return ""
    tmp = BASE / f"_voice_{file_id[:16]}.oga"
    f = await context.bot.get_file(file_id)
    await f.download_to_drive(str(tmp))
    try:
        audio = tmp.read_bytes()
        pref = cfg_get("voice_provider", "auto")
        # выбранный провайдер — первым, остальные как авто-фолбэк
        order = ([pref] if pref in avail else []) + [p for p in avail if p != pref]
        for prov in order:
            try:
                txt = await _ASR[prov](audio)
            except Exception as e:
                log.warning("asr %s: %s", prov, e)
                txt = ""
            if txt:
                return txt
        return ""
    finally:
        try:
            tmp.unlink()
        except Exception:
            pass


LAST_PROMPT = {}   # tkey -> последний отправленный запрос (для /retry)


async def dispatch(bot, chat_id, tid, text, name_hint=None):
    """Отправить текст/задачу в сессию топика (с учётом очереди)."""
    key = tkey(chat_id, tid)
    LAST_PROMPT[key] = text                  # для /retry после ошибки
    if key in RUNNING:                      # занят или уже claimed → в очередь (не вторая сессия)
        QUEUES.setdefault(key, []).append(text)
        if cfg_get("notify_queue", "0") == "1":
            try:
                await bot.send_message(chat_id, f"➕ В очередь ({len(QUEUES[key])}).",
                                       message_thread_id=tid)
            except Exception:
                pass
        return
    RUNNING[key] = "starting"               # синхронный claim ДО любого await — закрывает race двух сообщений
    t = topic_get(chat_id, tid)
    if t is None:
        t = topic_upsert(chat_id, tid, name=titled(name_hint or text))
    await run_claude(bot, t, text)


def _image_prompt(paths, cap):
    lst = "\n".join(paths)
    task = cap or "Посмотри изображение(я) и разбери по смыслу."
    return (f"Пользователь прислал изображени(я) через Telegram — открой их через Read "
            f"и учти в работе:\n{lst}\n\nЗадача: {task}")


async def _flush_album(bot, mg):
    await asyncio.sleep(1.6)
    alb = ALBUM.pop(mg, None)
    if not alb:
        return
    await dispatch(bot, alb["chat"], alb["tid"],
                   _image_prompt(alb["paths"], alb["cap"]),
                   name_hint=alb["cap"] or "фото")


async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    msg = update.effective_message
    chat_id = update.effective_chat.id
    tid = msg.message_thread_id
    photos = msg.photo

    # голос → текст (у фото подпись НЕ берём как text — она уйдёт задачей к картинке)
    text = (msg.text or ("" if (photos or msg.document) else msg.caption) or "").strip()
    if (msg.voice or msg.audio) and not text:
        if not VOICE_ON:
            await msg.reply_text("🎙 Голос выключен (нет ASR-провайдера).")
            return
        await context.bot.send_chat_action(chat_id, ChatAction.TYPING, message_thread_id=tid)
        text = await transcribe_voice(context, (msg.voice or msg.audio).file_id)
        if not text:
            await msg.reply_text(
                "🎙 Не смог распознать голос — попробуй ещё раз или напиши текстом.")
            return
        if cfg_get("notify_voice", "0") == "1":
            await msg.reply_text(f"🎙 «{text}»")

    # в личке / General без топика — подсказываем
    if update.effective_chat.type == "private" or not tid:
        if text or photos:
            await msg.reply_text(
                "Это работает в топиках группы. Нажми «➕ Новая сессия» или /new.",
                reply_markup=main_kb())
        return

    # ФОТО (одно или альбомом) + подпись-задача
    if photos:
        try:
            ph = photos[-1]
            f = await context.bot.get_file(ph.file_id)
            path = IMG_DIR / f"{ph.file_unique_id}.jpg"
            await f.download_to_drive(str(path))
        except Exception as e:
            await msg.reply_text(f"Не смог скачать фото: {e}")
            return
        cap = (msg.caption or "").strip()
        mg = msg.media_group_id
        if mg:                                    # альбом — копим и обрабатываем разом
            alb = ALBUM.setdefault(mg, {"paths": [], "cap": "", "chat": chat_id, "tid": tid})
            alb["paths"].append(str(path))
            if cap:
                alb["cap"] = cap
            if not alb.get("scheduled"):
                alb["scheduled"] = True
                asyncio.create_task(_flush_album(context.bot, mg))
            return
        await dispatch(context.bot, chat_id, tid, _image_prompt([str(path)], cap),
                       name_hint=cap or "фото")
        return

    # ДОКУМЕНТ (pdf / код / текст / любой файл до 20 МБ) + подпись-задача
    doc = msg.document
    if doc:
        try:
            fn = re.sub(r"[^\w.\-]+", "_", doc.file_name or doc.file_unique_id)
            f = await context.bot.get_file(doc.file_id)
            path = IMG_DIR / fn
            await f.download_to_drive(str(path))
        except Exception as e:
            await msg.reply_text(f"Не смог скачать файл (боту доступно до 20 МБ): {e}")
            return
        cap = (msg.caption or "").strip()
        await dispatch(
            context.bot, chat_id, tid,
            f"Пользователь прислал файл через Telegram — открой его через Read и учти "
            f"в работе:\n{path}\n\nЗадача: {cap or 'Разбери файл по смыслу.'}",
            name_hint=cap or (doc.file_name or "файл"))
        return

    if not text:
        return
    await dispatch(context.bot, chat_id, tid, text)


def _read_external(path, pos):
    """Новые user/assistant текстовые сообщения с байт-позиции pos (только до
    последнего полного перевода строки — чтобы не поймать недописанную строку)."""
    with path.open("rb") as f:
        f.seek(pos)
        data = f.read()
    cut = data.rfind(b"\n")
    if cut < 0:
        return [], pos
    consumed = data[:cut + 1]
    newpos = pos + len(consumed)
    out = []
    for line in consumed.decode("utf-8", "ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        t = o.get("type")
        if t not in ("user", "assistant"):
            continue
        c = (o.get("message") or {}).get("content")
        blocks = [c] if isinstance(c, str) else (
            [b.get("text") for b in c
             if isinstance(b, dict) and b.get("type") == "text" and b.get("text")]
            if isinstance(c, list) else [])
        parts = []
        for bt in blocks:
            bt = _strip_system(bt)
            if bt:
                parts.append(bt)
        txt = "\n".join(parts).strip()   # СОХРАНЯЕМ переносы/структуру
        if not txt:
            continue
        out.append((t, txt))
    return out, newpos


def _strip_system(text):
    """Убирает системные вставки (reminder/ide-теги), оставляя реальный текст."""
    text = re.sub(r"<system-reminder>.*?</system-reminder>", "", text, flags=re.S)
    text = re.sub(r"<ide_[a-z_]+>.*?</ide_[a-z_]+>", "", text, flags=re.S)
    text = re.sub(r"<[a-z_]+-reminder>.*?</[a-z_]+-reminder>", "", text, flags=re.S)
    text = text.strip()
    if not text or text.startswith("<") or text.startswith("Caveat:"):
        return ""
    return text


async def automirror_scan(app):
    """Авто-зеркало ВСЕХ сессий: находит сессии с новой активностью (после
    включения), у которых ещё нет вкладки, создаёт вкладку + зеркало + сводку."""
    if cfg_get("automirror", "0") != "1":
        return
    chat_id = int(cfg_get("automirror_chat", "0") or 0)
    since = float(cfg_get("automirror_since", "0") or 0)
    if not chat_id:
        return
    bound = {
        r["session_id"] for r in db.execute(
            "SELECT session_id FROM topics WHERE session_id IS NOT NULL AND chat_id=?",
            (chat_id,),
        ).fetchall()
    }
    created = 0
    for p in SESS_DIR.glob("*.jsonl"):
        if created >= 4:      # не больше 4 новых вкладок за цикл — против флуда
            break
        try:
            if p.stat().st_mtime < since:
                continue
        except Exception:
            continue
        sid = p.stem
        if sid in bound:
            continue
        name = await smart_title(sid)
        try:
            topic = await app.bot.create_forum_topic(chat_id, name=name or "сессия")
        except Exception as e:
            log.warning("automirror create: %s", e)
            continue
        tid = topic.message_thread_id
        topic_upsert(chat_id, tid, session_id=sid, name=name, mirror=1)
        try:
            MIRROR_POS[tkey(chat_id, tid)] = p.stat().st_size
        except Exception:
            pass
        await post_recap(app, chat_id, tid, sid)
        bound.add(sid)
        created += 1
        await asyncio.sleep(0.8)


async def mirror_loop(app):
    """Фоново: досылает в топик новые сообщения сессии, сделанные вне бота
    (в VS Code / терминале). Токенов не тратит — только читает файл."""
    while True:
        try:
            await asyncio.sleep(MIRROR_POLL)
            await automirror_scan(app)
            rows = db.execute(
                "SELECT chat_id,thread_id,session_id FROM topics "
                "WHERE mirror=1 AND session_id IS NOT NULL"
            ).fetchall()
            for r in rows:
                key = tkey(r["chat_id"], r["thread_id"])
                if key in RUNNING:      # бот сам стримит в топик — индикатор не нужен
                    continue
                p = SESS_DIR / f"{r['session_id']}.jsonl"
                if not p.exists():
                    continue
                chat_id, thread_id = r["chat_id"], r["thread_id"]
                size = p.stat().st_size
                pos = MIRROR_POS.get(key)
                if pos is None or pos > size:   # первый раз/усечение — с этого места
                    MIRROR_POS[key] = size
                    LAST_SIZE[key] = size
                    continue
                grew = size > LAST_SIZE.get(key, size)
                LAST_SIZE[key] = size

                if size > pos:
                    msgs, newpos = _read_external(p, pos)
                    MIRROR_POS[key] = newpos
                    if msgs:
                        wid = WORK_MSG.get(key)          # ответ идёт выше — убираем индикатор
                        if wid:
                            _wm_clear(key)
                            try:
                                await app.bot.delete_message(chat_id, wid)
                            except Exception:
                                pass
                        for role, txt in msgs[-6:]:
                            who = (ce("🖥") + " <b>ПК</b>") if role == "user" else ce("🤖")
                            for i, ch in enumerate(Streamer._split_md(txt, 3500)):
                                body = ((who + ": ") if i == 0 else "") + md_to_tg_html(ch)
                                try:
                                    await app.bot.send_message(
                                        chat_id, body, message_thread_id=thread_id,
                                        parse_mode="HTML")
                                except Exception:
                                    try:
                                        await app.bot.send_message(
                                            chat_id, re.sub(r"<[^>]+>", "", body),
                                            message_thread_id=thread_id)
                                    except Exception as e:
                                        log.debug("mirror send: %s", e)
                                await asyncio.sleep(0.4)

                # индикатор «думает» внизу вкладки — как в боте (анимир-эмодзи),
                # держится грейс-период после активности, чтобы не мигал
                if cfg_get("mirror_activity", "1") == "1":
                    if grew:
                        LAST_ACTIVE[key] = time.time()
                    active = (time.time() - LAST_ACTIVE.get(key, 0)) < 8
                    if active:
                        txt, pm = indicator_text()
                        if WORK_MSG.get(key):
                            try:
                                await app.bot.edit_message_text(
                                    txt, chat_id=chat_id, message_id=WORK_MSG[key],
                                    parse_mode=pm)
                            except Exception:
                                pass
                        else:
                            try:
                                mm = await app.bot.send_message(
                                    chat_id, txt, message_thread_id=thread_id, parse_mode=pm)
                                _wm_set(key, mm.message_id)
                            except Exception:
                                pass
                    elif WORK_MSG.get(key):
                        wid = WORK_MSG.get(key)
                        _wm_clear(key)
                        try:
                            await app.bot.delete_message(chat_id, wid)
                        except Exception:
                            pass
        except Exception as e:
            log.warning("mirror_loop: %s", e)


async def autoreload_loop():
    """Сам перезапускается, когда bot.py изменён — правки применяются без рук.
    Ждёт, пока нет активных задач, чтобы не оборвать работу."""
    import py_compile
    path = os.path.abspath(__file__)
    try:
        base = prev = os.path.getmtime(path)
    except Exception:
        return
    while True:
        await asyncio.sleep(3)
        try:
            m = os.path.getmtime(path)
        except Exception:
            continue
        # изменён + стабилен (не редактируется прямо сейчас) + бот свободен + компилится
        if m > base and m == prev and not RUNNING and not QUEUES:
            try:
                py_compile.compile(path, doraise=True)
            except Exception:
                prev = m
                continue
            log.info("bot.py изменён — авто-перезапуск")
            os.execv(sys.executable, [sys.executable, path])
        prev = m


async def _cleanup_indicators(app):
    """Удаляет осиротевшие индикаторы прошлого запуска: зеркальные (wm_) и
    зависшие плашки Стримера «думает…/🔧 Bash…» (sm_) — чтобы после рестарта
    не оставалось призраков без ответа."""
    rows = db.execute(
        "SELECT key,value FROM config WHERE key LIKE 'wm_%' OR key LIKE 'sm_%'"
    ).fetchall()
    for r in rows:
        try:
            tk = r["key"][3:]                 # <wm|sm>_<chat>:<thread>
            cid = int(tk.split(":")[0])
            await app.bot.delete_message(cid, int(r["value"]))
        except Exception:
            pass
    try:
        db.execute("DELETE FROM config WHERE key LIKE 'wm_%' OR key LIKE 'sm_%'")
        db.commit()
    except Exception:
        pass


BOT_COMMANDS = [
    ("new",       "🆕 Новая чистая сессия в этом топике"),
    ("retry",     "🔁 Повторить последний запрос (после ошибки/500)"),
    ("ctx",       "🧠 Насколько большой контекст топика"),
    ("compact",   "🗜 Сжать диалог (убрать 500/тормоза)"),
    ("status",    "🔎 Что выполняется сейчас + контекст"),
    ("stopall",   "⏹ Прервать ВСЕ задачи во всех топиках"),
    ("queue",     "📋 Показать очередь этого топика"),
    ("sessions",  "🗂 Список сессий Claude Code"),
    ("model",     "🤖 Выбрать модель (Opus/Sonnet/…)"),
    ("settings",  "⚙️ Все настройки бота"),
    ("voice",     "🎙 Распознавание голосовых"),
    ("lang",      "🌐 Язык интерфейса / Language"),
    ("donate",    "🙏 Поддержать автора / Support"),
    ("thinking",  "💭 Стиль индикатора «думает»"),
    ("thinkicon", "✨ Иконка индикатора"),
    ("verbose",   "📃 Подробность ответов (код/прогресс/только ответ)"),
    ("mirror",    "🪞 Зеркалить работу из VS Code в топик"),
    ("automirror","🔄 Авто-зеркало для новых топиков"),
    ("rename",    "🏷 Переименовать топик"),
    ("cwd",       "📂 Рабочая папка сессии"),
    ("ultra",     "🚀 Режим ultracode вкл/выкл"),
    ("queuemode", "➕ Как обрабатывать очередь сообщений"),
    ("footer",    "📊 Подпись с расходом токенов вкл/выкл"),
    ("cost",      "💳 Расход токенов этого топика"),
    ("usage",     "📈 Лимиты подписки"),
    ("id",        "🧩 ID сессии + как продолжить на ПК"),
    ("import",    "📥 Импорт истории сессий"),
    ("dedup",     "🧹 Убрать дубли топиков"),
    ("tidy",      "🧽 Прибраться в темах"),
    ("stop",      "⛔️ Прервать текущую задачу"),
    ("restart",   "♻️ Перезапустить бота"),
    ("help",      "❓ Помощь и список команд"),
    ("whoami",    "👤 Кто я для бота"),
    ("start",     "▶️ Старт / приветствие"),
]


async def _register_commands(app):
    """Регистрирует команды в Telegram — чтобы всплывала слеш-подсказка."""
    from telegram import BotCommand
    try:
        await app.bot.set_my_commands([BotCommand(c, d) for c, d in BOT_COMMANDS])
    except Exception as e:
        log.warning("set_my_commands не удалось: %s", e)


async def _resume_pending(app):
    """Возобновляет задачи, прерванные рестартом (чтобы бот всегда доотвечал)."""
    rows = db.execute("SELECT key,value FROM config WHERE key LIKE 'pending_%'").fetchall()
    db.execute("DELETE FROM config WHERE key LIKE 'pending_%'")   # чистим до запуска — без зацикливания
    db.commit()
    for r in rows:
        try:
            tk = r["key"][len("pending_"):]        # pending_<chat>:<thread>
            cid, thr = tk.split(":")
            cid = int(cid)
            thr = int(thr) or None
            await app.bot.send_message(
                cid, "↻ Возобновляю задачу, прерванную перезапуском…", message_thread_id=thr)
            asyncio.create_task(dispatch(app.bot, cid, thr, r["value"]))
        except Exception as e:
            log.warning("resume pending %s: %s", r["key"], e)


async def _post_init(app):
    await _register_commands(app)
    await _cleanup_indicators(app)
    await _resume_pending(app)
    asyncio.create_task(mirror_loop(app))
    asyncio.create_task(autoreload_loop())


async def on_error(update, context):
    err = context.error
    if isinstance(err, (TimedOut, NetworkError, RetryAfter)):
        log.warning("transient telegram error: %s", err)
        return
    log.error("handler error", exc_info=err)
    try:
        if isinstance(update, Update) and update.effective_chat:
            await context.bot.send_message(
                update.effective_chat.id, f"⚠️ Ошибка: {err}"
            )
    except Exception:
        pass


def main():
    if not BOT_TOKEN:
        raise SystemExit(
            "Нет BOT_TOKEN. Создай /root/tg-claude-bot/.env со строкой BOT_TOKEN=...\n"
            "(токен от @BotFather)"
        )
    if cfg_get("smart_titles") is None:
        cfg_set("smart_titles", "1" if os.path.exists(VERTEX_SA) else "0")
    if cfg_get("auto_compact") is None:
        cfg_set("auto_compact", "0")   # авто-сжатие контекста ВЫКЛ по умолчанию (жгло токены на ложных срабатываниях); включается тумблером в /settings
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)  # параллельные топики + рабочий /stop
        .rate_limiter(AIORateLimiter(max_retries=5))  # держит лимит + ретраит 429 до 5 раз (ответы не роняются)
        .read_timeout(30)
        .write_timeout(30)
        .connect_timeout(20)
        .pool_timeout(20)
        .get_updates_read_timeout(45)
        .post_init(_post_init)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("whoami", cmd_whoami))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("sessions", cmd_sessions))
    app.add_handler(CommandHandler("import", cmd_import))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("ctx", cmd_ctx))
    app.add_handler(CommandHandler("compact", cmd_compact))
    app.add_handler(CommandHandler("voice", cmd_voice))
    app.add_handler(CommandHandler("lang", cmd_lang))
    app.add_handler(CommandHandler("donate", cmd_donate))
    app.add_handler(CommandHandler("thinking", cmd_thinking))
    app.add_handler(CommandHandler("thinkicon", cmd_thinkicon))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("rename", cmd_rename))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("cwd", cmd_cwd))
    app.add_handler(CommandHandler("ultra", cmd_ultra))
    app.add_handler(CommandHandler("mirror", cmd_mirror))
    app.add_handler(CommandHandler("automirror", cmd_automirror))
    app.add_handler(CommandHandler("dedup", cmd_dedup))
    app.add_handler(CommandHandler("tidy", cmd_tidy))
    app.add_handler(CommandHandler("cost", cmd_cost))
    app.add_handler(CommandHandler("usage", cmd_usage))
    app.add_handler(CommandHandler("footer", cmd_footer))
    app.add_handler(CommandHandler("queuemode", cmd_queuemode))
    app.add_handler(CommandHandler("verbose", cmd_verbose))
    app.add_handler(CommandHandler("restart", cmd_restart))
    app.add_handler(CommandHandler("stop", cmd_stop))
    app.add_handler(CommandHandler("retry", cmd_retry))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("stopall", cmd_stopall))
    app.add_handler(CommandHandler("queue", cmd_queue))
    app.add_handler(
        MessageHandler(filters.StatusUpdate.FORUM_TOPIC_EDITED, on_topic_edited)
    )
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_error_handler(on_error)
    app.add_handler(
        MessageHandler(
            (filters.TEXT | filters.VOICE | filters.AUDIO | filters.CAPTION
             | filters.PHOTO | filters.Document.ALL)
            & ~filters.COMMAND,
            on_message,
        )
    )
    log.info("TG Claude Bot запущен. Владелец: %s", cfg_get("owner_id") or "(ещё не задан)")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
