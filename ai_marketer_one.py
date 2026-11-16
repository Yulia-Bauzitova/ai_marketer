#!/usr/bin/env python3
"""AI-маркетолог 24/7 — всё приложение в одном файле.

Что внутри одного файла:
    • сайт и кабинет (открывается в браузере на ПК и на телефоне);
    • Telegram-бот: свой токен и имя указываются при первом запуске;
    • нейросеть: облако по ключу или локальная модель (Ollama) без интернета;
    • картинки: локальная Stable Diffusion, облако или карточка бренда;
    • база знаний, задачи, согласование, публикация в канал, журнал.

Запуск:
    python ai_marketer_one.py

Ничего устанавливать не нужно, кроме Python 3.9+. Pillow не обязателен:
без него картинки рисуются встроенным генератором PNG.

Все данные лежат рядом с файлом: ai_marketer.json (настройки),
ai_marketer.db (база), media/ (картинки).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import os
import pathlib
import re
import secrets
import socket
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from datetime import datetime

def _app_dir() -> pathlib.Path:
    """Папка приложения. Для собранного .exe — папка рядом с ним, не временная."""
    if getattr(sys, "frozen", False):
        return pathlib.Path(sys.executable).resolve().parent
    return pathlib.Path(__file__).resolve().parent


APP_DIR = _app_dir()
CONF_PATH = APP_DIR / "ai_marketer.json"
DB_PATH = APP_DIR / "ai_marketer.db"
MEDIA_DIR = APP_DIR / "media"
VERSION = "1.0"
STARTED = time.time()

# --------------------------------------------------------------------- настройки
DEFAULTS = {
    "bot_token": "",
    "bot_username": "",
    "admin_hash": "",
    "company": "",
    "site_title": "AI-маркетолог",
    "provider": "offline",          # offline | cloud | local
    "api_base": "https://api.vsegpt.ru/v1",
    "api_key": "",
    "text_model": "openai/gpt-4o-mini",
    "image_backend": "auto",        # auto | local | api | card
    "image_model": "",
    "local_sd_url": "http://127.0.0.1:7860",
    "local_llm_url": "http://127.0.0.1:11434/v1",
    "channel": "",
    "host": "127.0.0.1",
    "port": 8081,
    "secret": "",
    "tg_api": "https://api.telegram.org",
}

_lock = threading.Lock()


def conf() -> dict:
    """Настройки из файла рядом с приложением. Секретный ключ создаётся сам."""
    data = dict(DEFAULTS)
    try:
        saved = json.loads(CONF_PATH.read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            data.update({key: value for key, value in saved.items() if key in DEFAULTS})
    except (OSError, ValueError):
        pass
    if not data.get("secret"):
        data["secret"] = secrets.token_hex(32)
        save_conf(data)
    return data


def save_conf(data: dict) -> None:
    with _lock:
        CONF_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                             encoding="utf-8")


def patch_conf(**changes) -> dict:
    data = conf()
    data.update(changes)
    save_conf(data)
    return data


# ------------------------------------------------------------------------- база
def conn() -> sqlite3.Connection:
    base = sqlite3.connect(DB_PATH, timeout=15)
    base.row_factory = sqlite3.Row
    base.execute("PRAGMA journal_mode=WAL")
    base.executescript(
        """
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            status TEXT NOT NULL,
            result TEXT DEFAULT '',
            image TEXT DEFAULT '',
            source TEXT DEFAULT 'site',
            created_at TEXT NOT NULL,
            published_at TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS knowledge (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS journal (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            action TEXT NOT NULL,
            details TEXT DEFAULT ''
        );
        """
    )
    return base


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def log(action: str, details: str = "") -> None:
    try:
        base = conn()
        base.execute("INSERT INTO journal(ts, action, details) VALUES(?,?,?)",
                     (now(), action, (details or "")[:400]))
        base.commit()
        base.close()
    except sqlite3.Error:
        pass


def create_task(text: str, source: str = "site") -> int:
    base = conn()
    cur = base.execute("INSERT INTO tasks(text, status, created_at, source) VALUES(?,?,?,?)",
                       (" ".join(text.split())[:2000], "new", now(), source))
    base.commit()
    task_id = int(cur.lastrowid)
    base.close()
    log("task_create", f"#{task_id} {text[:80]}")
    return task_id


def update_task(task_id: int, **fields) -> None:
    if not fields:
        return
    allowed = {"status", "result", "image", "published_at"}
    pairs = [(key, value) for key, value in fields.items() if key in allowed]
    if not pairs:
        return
    base = conn()
    base.execute(f"UPDATE tasks SET {', '.join(f'{key}=?' for key, _ in pairs)} WHERE id=?",
                 [value for _, value in pairs] + [task_id])
    base.commit()
    base.close()


def claim_task(task_id: int) -> bool:
    """Атомарно забирает задачу в публикацию: один пост — одна отправка."""
    base = conn()
    cur = base.execute(
        "UPDATE tasks SET status='publishing' "
        "WHERE id=? AND status IN ('approved','scheduled','publish_error')", (task_id,))
    base.commit()
    ok = cur.rowcount == 1
    base.close()
    return ok


def get_task(task_id: int):
    base = conn()
    row = base.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    base.close()
    return dict(row) if row else None


def tasks(limit: int = 30, status: str | None = None) -> list[dict]:
    base = conn()
    if status:
        rows = base.execute("SELECT * FROM tasks WHERE status=? ORDER BY id DESC LIMIT ?",
                            (status, limit)).fetchall()
    else:
        rows = base.execute("SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    base.close()
    return [dict(row) for row in rows]


def knowledge() -> list[dict]:
    base = conn()
    rows = base.execute("SELECT * FROM knowledge ORDER BY id DESC LIMIT 50").fetchall()
    base.close()
    return [dict(row) for row in rows]


def add_knowledge(title: str, content: str) -> None:
    base = conn()
    base.execute("INSERT INTO knowledge(title, content, created_at) VALUES(?,?,?)",
                 (title.strip()[:120] or "Без названия", content.strip()[:4000], now()))
    base.commit()
    base.close()
    log("knowledge_add", title[:80])


def journal(limit: int = 40) -> list[dict]:
    base = conn()
    rows = base.execute("SELECT * FROM journal ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    base.close()
    return [dict(row) for row in rows]


def stats() -> dict:
    base = conn()
    rows = base.execute("SELECT status, COUNT(*) AS n FROM tasks GROUP BY status").fetchall()
    base.close()
    data = {row["status"]: row["n"] for row in rows}
    data["total"] = sum(data.values())
    return data


# ------------------------------------------------------------------ нейросеть
SYSTEM = (
    "Ты AI-маркетолог компании. Пиши по-русски, по делу, без воды. "
    "Структура поста: цепляющая первая строка, 2–3 коротких абзаца, "
    "2–3 уместных эмодзи, призыв к действию и 3–5 хештегов. "
    "Используй только факты из базы знаний, ничего не выдумывай."
)


def _provider_settings() -> tuple[str, str, str, str]:
    """(режим, адрес, ключ, модель) — облако или локальная модель."""
    data = conf()
    mode = (data.get("provider") or "offline").lower()
    if mode == "local":
        return mode, data.get("local_llm_url") or DEFAULTS["local_llm_url"], "", \
            data.get("text_model") or "qwen2.5:7b"
    return mode, (data.get("api_base") or DEFAULTS["api_base"]).rstrip("/"), \
        data.get("api_key") or "", data.get("text_model") or DEFAULTS["text_model"]


def ai_status() -> str:
    mode, base, key, model = _provider_settings()
    if mode == "offline":
        return "офлайн-шаблоны (нейросеть не подключена)"
    if mode == "local":
        return f"локальная модель {model} ({base}) — без интернета"
    if not key:
        return "облако выбрано, но ключ не указан — работают шаблоны"
    return f"облако: {model} ({base})"


def _template_answer(question: str, facts: list[str]) -> str:
    """Ответ без нейросети: аккуратный пост из базы знаний."""
    company = conf().get("company") or "наша компания"
    first = facts[0] if facts else f"{company} — производство и услуги под задачу клиента."
    second = facts[1] if len(facts) > 1 else "Работаем аккуратно, в срок и с гарантией."
    return (f"{question.strip().capitalize() or 'Новый материал'}\n\n"
            f"{first}\n\n{second}\n\n"
            f"Напишите нам — рассчитаем стоимость и сроки под вашу задачу. 🙌\n\n"
            f"#{company.replace(' ', '')} #предложение #сроки").strip()


def ask(question: str) -> tuple[str, str]:
    """Текст поста. Возвращает (текст, причина если ответ шаблонный)."""
    facts = [f"{item['title']}: {item['content']}" for item in knowledge()][:8]
    mode, base, key, model = _provider_settings()
    if mode == "offline" or (mode == "cloud" and not key):
        return _template_answer(question, facts), "нейросеть не подключена"

    context = "\n".join(facts) if facts else f"Компания: {conf().get('company') or 'без названия'}"
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": f"{context}\n\nЗадача: {question}"}],
        "temperature": 0.7,
        "max_tokens": 900,
    }).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    request = urllib.request.Request(base + "/chat/completions", data=payload,
                                     headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            data = json.loads(response.read().decode("utf-8", "replace"))
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
        text = str(text).strip()
        if len(text) < 30:
            raise ValueError("модель вернула пустой ответ")
        return text, ""
    except Exception as exc:  # noqa: BLE001 - шаблон лучше ошибки
        log("ai_error", f"{type(exc).__name__}: {exc}")
        return _template_answer(question, facts), f"нейросеть не ответила ({type(exc).__name__})"


# -------------------------------------------------------------------- картинки
PALETTES = (("#0F3D5C", "#F2A03D"), ("#123B2E", "#8FD694"), ("#3B1F4E", "#F0A6CA"),
            ("#0B3C49", "#FFD166"), ("#402020", "#FF8C61"))
PIL_OK = False
try:  # Pillow не обязателен
    from PIL import Image, ImageDraw, ImageFilter, ImageFont  # type: ignore

    PIL_OK = True
except Exception:  # noqa: BLE001
    Image = ImageDraw = ImageFilter = ImageFont = None  # type: ignore


def _png_plain(text: str, path: pathlib.Path, size: int = 1080,
               color: tuple[int, int, int] = (15, 61, 92)) -> pathlib.Path:
    """PNG без библиотек: градиент полосами. Работает всегда."""
    rows = bytearray()
    for y in range(size):
        k = y / max(size - 1, 1)
        rows.append(0)  # фильтр строки
        r = int(color[0] + 40 * k)
        g = int(color[1] + 60 * k)
        b = int(color[2] + 80 * k)
        rows.extend(bytes((r, g, b)) * size)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (len(data).to_bytes(4, "big") + tag + data
                + (zlib.crc32(tag + data) & 0xFFFFFFFF).to_bytes(4, "big"))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", size.to_bytes(4, "big") * 2 + bytes((8, 2, 0, 0, 0)))
           + chunk(b"IDAT", zlib.compress(bytes(rows), 6))
           + chunk(b"IEND", b""))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)
    return path


def _local_sd(prompt: str, path: pathlib.Path) -> pathlib.Path | None:
    """Локальная Stable Diffusion (WebUI/Forge/SD.Next с --api): без интернета."""
    data = conf()
    url = (data.get("local_sd_url") or DEFAULTS["local_sd_url"]).rstrip("/")
    size = 768
    payload = json.dumps({
        "prompt": prompt + ", high quality commercial photography, vibrant colors, "
                            "soft light, no text, no letters, no watermark",
        "negative_prompt": "text, letters, watermark, logo, blurry, low quality",
        "steps": 25, "cfg_scale": 7, "width": size, "height": size,
        "sampler_name": "DPM++ 2M Karras",
    }).encode("utf-8")
    request = urllib.request.Request(url + "/sdapi/v1/txt2img", data=payload, method="POST",
                                     headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            images = json.loads(response.read().decode("utf-8", "replace")).get("images") or []
    except Exception:  # noqa: BLE001 - нет сервера, нет модели
        return None
    if not images:
        return None
    raw = images[0].split(",", 1)[-1]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(base64.b64decode(raw))
    except Exception:  # noqa: BLE001
        return None
    return path if path.stat().st_size > 2000 else None


def _cloud_image(prompt: str, path: pathlib.Path) -> pathlib.Path | None:
    """Картинка по ключу: OpenRouter/Gemini через chat/completions."""
    data = conf()
    key, model = data.get("api_key"), data.get("image_model")
    base = (data.get("api_base") or "").rstrip("/")
    if not key or not model or not base:
        return None
    payload = json.dumps({"model": model,
                          "messages": [{"role": "user", "content": prompt}],
                          "modalities": ["image", "text"]}).encode("utf-8")
    request = urllib.request.Request(
        base + "/chat/completions", data=payload, method="POST",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json",
                 "HTTP-Referer": "https://ai-marketer.local", "X-Title": "AI Marketer"})
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            body = json.loads(response.read().decode("utf-8", "replace"))
        message = ((body.get("choices") or [{}])[0] or {}).get("message") or {}
        for item in message.get("images") or []:
            url = ((item or {}).get("image_url") or {}).get("url") or item.get("url")
            if not url:
                continue
            raw = url.split(",", 1)[-1] if url.startswith("data:") else None
            if raw:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(base64.b64decode(raw))
                return path if path.stat().st_size > 2000 else None
    except Exception:  # noqa: BLE001
        return None
    return None


def make_image(title: str, essence: str = "") -> tuple[pathlib.Path, str, str]:
    """Картинка к посту. Возвращает (путь, способ, причина отката)."""
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^a-zA-Z0-9а-яА-ЯёЁ]+", "-", title)[:40].strip("-") or "poster"
    stamp = hashlib.md5(f"{title}{time.time()}".encode()).hexdigest()[:8]
    target = MEDIA_DIR / f"{safe}-{stamp}.png"
    company = conf().get("company") or "Компания"
    prompt = (f"Яркая рекламная фотография для соцсетей компании «{company}». "
              f"Тема: {title}. Суть: {essence[:250]}. Без текста и логотипов.")

    backend = (conf().get("image_backend") or "auto").lower()
    if backend in ("auto", "local"):
        made = _local_sd(prompt, target)
        if made:
            return made, "local", ""
    if backend in ("auto", "api"):
        made = _cloud_image(prompt, target)
        if made:
            return made, "cloud", ""
    if backend == "local":
        reason = "локальная Stable Diffusion не отвечает — карточка бренда"
    elif backend == "api":
        reason = "облако не настроено — карточка бренда"
    else:
        reason = "нейросеть картинок не настроена — карточка бренда"

    primary, accent = PALETTES[hash(company) % len(PALETTES)]
    path = _png_plain(title, target, 1080, _rgb(primary))
    if PIL_OK:
        try:
            path = _card(company, title, essence, target, primary, accent)
            return path, "card", reason
        except Exception:  # noqa: BLE001 - остаётся простой PNG
            pass
    return path, "plain", reason


def _rgb(color: str) -> tuple[int, int, int]:
    color = (color or "#0F3D5C").lstrip("#")
    try:
        return tuple(int(color[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    except ValueError:
        return (15, 61, 92)


def _card(company: str, title: str, essence: str, path: pathlib.Path,
          primary: str, accent: str) -> pathlib.Path:
    """Карточка бренда: градиент, пятна, полоса акцента и крупный заголовок."""
    size = 1080
    base = Image.new("RGB", (size, size), _rgb(primary))
    draw = ImageDraw.Draw(base)
    for y in range(size):  # вертикальный градиент
        k = y / size
        draw.line([(0, y), (size, y)],
                  fill=tuple(int(_rgb(primary)[i] * (1 - k) + _rgb(accent)[i] * k * 0.75)
                             for i in range(3)))
    blobs = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    blob_draw = ImageDraw.Draw(blobs)
    blob_draw.ellipse((int(size * .6), int(size * -.1), int(size * 1.2), int(size * .5)),
                      fill=_rgb(accent) + (120,))
    blob_draw.ellipse((int(size * -.2), int(size * .7), int(size * .5), int(size * 1.3)),
                      fill=(255, 255, 255, 45))
    blobs = blobs.filter(ImageFilter.GaussianBlur(size // 12))
    base = Image.alpha_composite(base.convert("RGBA"), blobs).convert("RGB")
    draw = ImageDraw.Draw(base)
    draw.rectangle((0, int(size * .66), size, int(size * .675)), fill=_rgb(accent))

    font = None
    for candidate in ("arialbd.ttf", "Arial Bold.ttf", "DejaVuSans-Bold.ttf",
                      "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        try:
            font = ImageFont.truetype(candidate, 64)
            break
        except Exception:  # noqa: BLE001
            continue
    if font is None:
        font = ImageFont.load_default()

    words, lines, line = str(title).split(), [], ""
    for word in words:
        probe = f"{line} {word}".strip()
        if draw.textlength(probe, font=font) > size - 160 and line:
            lines.append(line)
            line = word
        else:
            line = probe
        if len(lines) == 4:
            break
    if line and len(lines) < 4:
        lines.append(line)

    y = int(size * .34)
    for row in lines:
        draw.text((80, y), row, font=font, fill=(255, 255, 255))
        y += int(64 * 1.25)
    stamp = f"{company} · {datetime.now().strftime('%d.%m')}"
    draw.text((80, size - 110), stamp[:48], font=ImageFont.load_default(), fill=(255, 255, 255))
    base.save(path, format="PNG")
    return path


# ----------------------------------------------------------------------- бот
def tg(method: str, timeout: int | None = None, **params) -> dict:
    """Вызов Telegram Bot API. Ошибки возвращаются словами, не исключением."""
    data = conf()
    token = data.get("bot_token") or ""
    if not token:
        return {"ok": False, "description": "токен бота не указан"}
    url = f"{data.get('tg_api') or DEFAULTS['tg_api']}/bot{token}/{method}"
    payload = json.dumps(params).encode("utf-8") if params else None
    request = urllib.request.Request(url, data=payload, method="POST" if payload else "GET",
                                     headers={"Content-Type": "application/json"})
    wait = timeout if timeout is not None else (65 if method == "getUpdates" else 15)
    try:
        with urllib.request.urlopen(request, timeout=wait) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        try:
            return json.loads(exc.read().decode("utf-8", "replace"))
        except ValueError:
            return {"ok": False, "description": f"HTTP {exc.code}"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "description": f"нет связи: {exc}"}


def bot_check() -> tuple[bool, str]:
    """Проверяет токен и заодно узнаёт имя бота — его видно в кабинете."""
    result = tg("getMe")
    if result.get("ok"):
        username = result.get("result", {}).get("username") or ""
        if username and username != conf().get("bot_username"):
            patch_conf(bot_username=username)
        return True, "@" + username if username else "токен рабочий"
    return False, str(result.get("description") or "неизвестная ошибка")


def send_message(chat_id, text: str, keyboard: dict | None = None) -> dict:
    params = {"chat_id": chat_id, "text": text[:4000],
              "disable_web_page_preview": True}
    if keyboard:
        params["reply_markup"] = keyboard
    return tg("sendMessage", **params)


def send_photo(chat_id, path: str, caption: str, keyboard: dict | None = None) -> dict:
    """Фото отправляем как файл через multipart: не нужен публичный URL."""
    data = conf()
    token = data.get("bot_token") or ""
    if not token or not path or not os.path.isfile(path):
        return send_message(chat_id, caption, keyboard)
    boundary = "----aimarketer" + secrets.token_hex(8)
    parts: list[bytes] = []
    for name, value in (("chat_id", str(chat_id)), ("caption", caption[:1024])):
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                     f"{value}\r\n".encode("utf-8"))
    if keyboard:
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"reply_markup\"\r\n"
                     f"Content-Type: application/json\r\n\r\n"
                     f"{json.dumps(keyboard)}\r\n".encode("utf-8"))
    parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"photo\"; "
                 f"filename=\"{os.path.basename(path)}\"\r\nContent-Type: image/png\r\n\r\n"
                 .encode("utf-8") + pathlib.Path(path).read_bytes() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(parts)
    url = f"{data.get('tg_api') or DEFAULTS['tg_api']}/bot{token}/sendPhoto"
    request = urllib.request.Request(
        url, data=body, method="POST",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read().decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        send_message(chat_id, caption + "\n\n(картинку отправить не удалось)")
        return {"ok": False, "description": str(exc)}


def task_keyboard(task_id: int) -> dict:
    return {"inline_keyboard": [[
        {"text": "✅ Согласовать", "callback_data": f"approve:{task_id}"},
        {"text": "❌ Отклонить", "callback_data": f"reject:{task_id}"}],
        [{"text": "🚀 Опубликовать", "callback_data": f"publish:{task_id}"}]]}


def answer_callback(callback_id: str, text: str) -> None:
    tg("answerCallbackQuery", callback_query_id=callback_id, text=text[:180])


def publish_task(task_id: int) -> str:
    """Публикация в канал. Без согласования и без атомарного захвата — нельзя."""
    data = conf()
    task = get_task(task_id)
    if not task:
        return "Задача не найдена"
    if task["status"] not in ("approved", "scheduled", "publish_error"):
        return "Сначала согласуйте материал"
    if not claim_task(task_id):
        return "Этот материал уже публикуется"
    channel = data.get("channel")
    if not channel:
        update_task(task_id, status="approved")
        return "Канал не указан в настройках"
    caption = (task.get("result") or task.get("text") or "")[:1024]
    result = (send_photo(channel, task.get("image") or "", caption)
              if task.get("image") else send_message(channel, caption))
    if result.get("ok"):
        update_task(task_id, status="published", published_at=now())
        log("publish", f"#{task_id} → {channel}")
        return "Опубликовано"
    update_task(task_id, status="publish_error")
    log("publish_error", f"#{task_id}: {result.get('description')}")
    return f"Не опубликовано: {result.get('description')}"


def handle_update(update: dict) -> None:
    """Один апдейт от Telegram: сообщение или нажатие кнопки."""
    if "callback_query" in update:
        query = update["callback_query"]
        data = (query.get("data") or "").strip()
        chat_id = (query.get("message") or {}).get("chat", {}).get("id")
        action, _, raw_id = data.partition(":")
        task_id = int(raw_id) if raw_id.isdigit() else 0
        if action == "approve":
            update_task(task_id, status="approved")
            answer_callback(query.get("id", ""), "Согласовано")
            send_message(chat_id, f"Материал #{task_id} согласован. Можно публиковать.")
        elif action == "reject":
            update_task(task_id, status="rejected")
            answer_callback(query.get("id", ""), "Отклонено")
            send_message(chat_id, f"Материал #{task_id} отклонён.")
        elif action == "publish":
            verdict = publish_task(task_id)
            answer_callback(query.get("id", ""), verdict)
            send_message(chat_id, f"#{task_id}: {verdict}")
        return

    message = update.get("message") or {}
    text = (message.get("text") or "").strip()
    chat_id = (message.get("chat") or {}).get("id")
    if not text or not chat_id:
        return
    command, _, argument = text.partition(" ")
    command = command.split("@")[0].lower()
    argument = argument.strip()

    if command in ("/start", "/help"):
        send_message(chat_id,
                     "🤖 AI-маркетолог на связи.\n\n"
                     "/post тема — текст поста\n"
                     "/pic тема — только картинка\n"
                     "/drafts — что на согласовании\n"
                     "/status — состояние системы\n\n"
                     "Любой текст без команды — задача: сделаю пост с картинкой.")
        return
    if command == "/status":
        send_message(chat_id, f"Нейросеть: {ai_status()}\n"
                              f"Бот: {bot_check()[1]}\n"
                              f"Задач: {stats()['total']}, на согласовании: "
                              f"{stats().get('review', 0)}")
        return
    if command == "/drafts":
        rows = tasks(15, status="review")
        if not rows:
            send_message(chat_id, "На согласовании пусто.")
            return
        for row in rows[:5]:
            send_message(chat_id, f"#{row['id']} {row['status']}\n{row['result'][:600]}",
                         task_keyboard(row["id"]))
        return
    if command == "/pic":
        topic = argument or "Новый материал"
        path, mode, reason = make_image(topic, topic)
        label = {"local": "локальная нейросеть", "cloud": "нейросеть по ключу",
                 "card": "карточка бренда", "plain": "простая картинка"}.get(mode, mode)
        caption = f"Картинка: {topic[:180]} ({label})"
        if reason:
            caption += f"\n\n{reason}"
        send_photo(chat_id, str(path), caption)
        return
    if command == "/post":
        question = argument or "Пост для соцсетей"
        task_id = create_task(question, "bot")
        answer, problem = ask(question)
        update_task(task_id, status="review", result=answer)
        note = f"\n\n⚠️ {problem}" if problem else ""
        send_message(chat_id, f"Пост #{task_id}:\n\n{answer}{note}", task_keyboard(task_id))
        return

    # обычный текст = задача: текст + картинка
    task_id = create_task(text, "bot")
    answer, problem = ask(text)
    path, mode, reason = make_image(answer.splitlines()[0][:70] or text, answer[:300])
    update_task(task_id, status="review", result=answer, image=str(path))
    note = f"\n\n⚠️ {problem}" if problem else ""
    send_photo(chat_id, str(path), f"#{task_id}\n\n{answer}{note}"[:1024],
               task_keyboard(task_id))


def bot_loop() -> None:
    """Long polling в отдельном потоке. Без токена просто спит."""
    offset = 0
    while True:
        if not conf().get("bot_token"):
            time.sleep(3)
            continue
        try:
            result = tg("getUpdates", offset=offset, timeout=50)
        except Exception:  # noqa: BLE001
            time.sleep(5)
            continue
        if not result.get("ok"):
            time.sleep(5)
            continue
        for update in result.get("result") or []:
            offset = max(offset, int(update.get("update_id", 0)) + 1)
            try:
                handle_update(update)
            except Exception as exc:  # noqa: BLE001 - бот не должен падать
                log("bot_error", f"{type(exc).__name__}: {exc}")


# ----------------------------------------------------------------------- PWA
def manifest() -> dict:
    """Манифест: приложение ставится значком на телефон и рабочий стол."""
    data = conf()
    name = data.get("company") or "AI-маркетолог"
    return {
        "name": f"{name} — AI-маркетолог",
        "short_name": "AI Маркетолог",
        "description": "Посты, картинки и публикации под согласованием",
        "start_url": "/app",
        "scope": "/",
        "display": "standalone",
        "orientation": "portrait-primary",
        "background_color": "#f4f7f9",
        "theme_color": "#0f6b8f",
        "lang": "ru",
        "icons": [
            {"src": "/icon-192.png", "sizes": "192x192", "type": "image/png",
             "purpose": "any maskable"},
            {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png",
             "purpose": "any maskable"},
        ],
    }


SW_JS = """
const CACHE = "ai-marketer-v1";
const SHELL = ["/", "/app", "/manifest.webmanifest", "/icon-192.png", "/icon-512.png"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)).catch(() => null));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key)))
    ).then(() => self.clients.claim())
  );
});

// Сначала сеть, при обрыве — кэш: кабинет всегда свежий, но и без сети открывается.
self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;
  event.respondWith(
    fetch(request)
      .then((response) => {
        const copy = response.clone();
        caches.open(CACHE).then((cache) => cache.put(request, copy)).catch(() => null);
        return response;
      })
      .catch(() => caches.match(request).then((hit) => hit || caches.match("/app")))
  );
});
"""

PWA_HEAD = """
<link rel="manifest" href="/manifest.webmanifest">
<meta name="theme-color" content="#0f6b8f">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="apple-mobile-web-app-title" content="AI Маркетолог">
<link rel="apple-touch-icon" href="/icon-192.png">
"""

SW_REG = """
<script>
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => navigator.serviceWorker.register("/sw.js").catch(() => null));
}
</script>
"""


def icon_png(size: int) -> bytes:
    """Иконка приложения: градиент и буквы AI. Кэшируется в media/."""
    cache = MEDIA_DIR / f"icon-{size}.png"
    if cache.is_file() and cache.stat().st_size > 500:
        return cache.read_bytes()
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    if PIL_OK:
        picture = Image.new("RGB", (size, size))
        draw = ImageDraw.Draw(picture)
        for y in range(size):  # диагональный градиент бренда
            k = y / max(size - 1, 1)
            draw.line([(0, y), (size, y)],
                      fill=(int(15 + 20 * k), int(107 - 30 * k), int(143 - 20 * k)))
        font = None
        for candidate in ("arialbd.ttf", "DejaVuSans-Bold.ttf",
                          "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
            try:
                font = ImageFont.truetype(candidate, int(size * 0.42))
                break
            except Exception:  # noqa: BLE001
                continue
        text = "AI"
        if font is not None:
            width = draw.textlength(text, font=font)
            draw.text(((size - width) / 2, size * 0.30), text, font=font, fill=(255, 255, 255))
        picture.save(cache, format="PNG")
        return cache.read_bytes()
    _png_plain("AI", cache, size, (15, 107, 143))
    return cache.read_bytes()


# ------------------------------------------------------------------- страницы
CSS = """
:root{--ink:#12212b;--muted:#63757f;--line:#dfe7ec;--acc:#0f6b8f;--ok:#1e8449;--warn:#b9770e}
*{box-sizing:border-box}
body{margin:0;font:16px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;
color:var(--ink);background:#f4f7f9}
a{color:var(--acc);text-decoration:none}
.wrap{max-width:900px;margin:0 auto;padding:16px 16px 90px}
.top{background:#fff;border-bottom:1px solid var(--line);padding:12px 16px;display:flex;
align-items:center;gap:12px;position:sticky;top:0;z-index:5}
.top b{font-size:16px}
.top .sp{flex:1}
.card{background:#fff;border:1px solid var(--line);border-radius:14px;padding:16px;margin:0 0 14px}
.card h2{margin:0 0 10px;font-size:17px}
.card h3{margin:14px 0 6px;font-size:15px}
label{display:block;font-size:13px;color:var(--muted);margin:10px 0 4px}
input,select,textarea{width:100%;padding:11px 12px;border:1px solid var(--line);border-radius:10px;
font:inherit;background:#fbfdfe}
textarea{min-height:90px;resize:vertical}
button,.btn{display:inline-block;padding:11px 16px;border:0;border-radius:10px;background:var(--acc);
color:#fff;font:inherit;font-weight:600;cursor:pointer;text-align:center}
.btn.ghost{background:#fff;color:var(--acc);border:1px solid var(--line)}
.btn.small{padding:7px 11px;font-size:13px;font-weight:500}
.row{display:flex;gap:10px;flex-wrap:wrap;align-items:center}
.kpi{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:10px}
.kpi div{background:#f7fafb;border:1px solid var(--line);border-radius:12px;padding:12px}
.kpi b{display:block;font-size:22px}
.muted{color:var(--muted);font-size:13px}
.tag{display:inline-block;padding:2px 8px;border-radius:20px;font-size:12px;background:#eef3f6}
.tag.ok{background:#e8f6ee;color:var(--ok)}
.tag.warn{background:#fdf3e3;color:var(--warn)}
.task{border:1px solid var(--line);border-radius:12px;padding:12px;margin:0 0 10px;background:#fff}
.task .txt{white-space:pre-wrap;font-size:14px}
.task img{max-width:100%;border-radius:10px;margin-top:8px}
.flash{background:#e8f6ee;color:var(--ok);border-radius:10px;padding:10px 12px;margin-bottom:12px}
.flash.err{background:#fdecea;color:#b03a2e}
.status{display:grid;gap:6px;font-size:14px}
.status span{color:var(--muted)}
nav.bottom{position:fixed;left:0;right:0;bottom:0;background:#fff;border-top:1px solid var(--line);
display:none;justify-content:space-around;padding:8px 4px;z-index:9}
nav.bottom a{font-size:12px;text-align:center;color:var(--muted);padding:4px 8px;border-radius:8px}
nav.bottom a.on{color:var(--acc);background:#eef5f8}
@media(max-width:640px){
 nav.bottom{display:flex}
 .wrap{padding-bottom:80px}
 .card{padding:14px}
 .hide-sm{display:none}
}
"""


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""))


def page(title: str, body: str, nav: str = "", flash: str = "", error: bool = False) -> str:
    data = conf()
    notice = f"<div class='flash{' err' if error else ''}'>{esc(flash)}</div>" if flash else ""
    bottom = ""
    if nav:
        items = [("/app", "Обзор", "dash"), ("/app#tasks", "Задачи", "tasks"),
                 ("/settings", "Настройки", "set")]
        bottom = ("<nav class='bottom'>" + "".join(
            f"<a class='{'on' if key == nav else ''}' href='{url}'>{label}</a>"
            for url, label, key in items) + "</nav>")
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>{esc(title)} · {esc(data.get('site_title') or 'AI-маркетолог')}</title>
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
{PWA_HEAD}
<style>{CSS}</style></head><body>
<div class="top"><b>🤖 {esc(data.get('site_title') or 'AI-маркетолог')}</b>
<span class="sp"></span>
<span class="muted hide-sm">{esc(data.get('company') or 'компания не указана')}</span>
<a class="btn ghost small" href="/logout">Выйти</a></div>
<div class="wrap">{notice}{body}</div>{bottom}{SW_REG}</body></html>"""


def page_setup(flash: str = "", error: bool = False) -> str:
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Первый запуск</title>{PWA_HEAD}<style>{CSS}</style></head><body>
<div class="wrap" style="max-width:560px">
<h1 style="font-size:22px">Первый запуск</h1>
<p class="muted">Заполните один раз. Всё сохранится рядом с файлом приложения,
перезапуск не понадобится.</p>
{f"<div class='flash err'>{esc(flash)}</div>" if flash else ""}
<form method="post" action="/setup" class="card">
  <h2>Бот</h2>
  <label>Имя бота (как в Telegram, можно с @)</label>
  <input name="bot_username" placeholder="@my_marketer_bot" value="">
  <label>Токен бота от @BotFather</label>
  <input name="bot_token" placeholder="123456789:AA..." autocomplete="off">
  <p class="muted">Имя нужно для ссылок, токен — чтобы бот отвечал. Токен можно
  вставить позже в настройках.</p>
  <h2>Компания и вход</h2>
  <label>Название компании</label>
  <input name="company" placeholder="ООО «Салаватстекло»" required>
  <label>Пароль администратора</label>
  <input name="password" type="password" minlength="6" required>
  <label>Канал для публикации (необязательно)</label>
  <input name="channel" placeholder="@my_channel или -1001234567890">
  <h2>Нейросеть</h2>
  <label>Режим</label>
  <select name="provider">
    <option value="offline">Без нейросети (шаблоны, работает всегда)</option>
    <option value="cloud">Облако по ключу (OpenRouter, VseGPT)</option>
    <option value="local">Локальная модель Ollama (без интернета)</option>
  </select>
  <label>Ключ API (для облака)</label>
  <input name="api_key" placeholder="sk-or-v1-..." autocomplete="off">
  <div class="row" style="margin-top:16px"><button type="submit">Сохранить и войти</button></div>
</form></div></body></html>"""


def page_login(flash: str = "") -> str:
    return f"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Вход</title>{PWA_HEAD}<style>{CSS}</style></head><body>
<div class="wrap" style="max-width:420px">
<h1 style="font-size:22px">Вход в кабинет</h1>
{f"<div class='flash err'>{esc(flash)}</div>" if flash else ""}
<form method="post" action="/login" class="card">
  <label>Пароль администратора</label>
  <input name="password" type="password" required autofocus>
  <div class="row" style="margin-top:14px"><button type="submit">Войти</button></div>
</form></div></body></html>"""


def lan_url() -> str:
    """Адрес для телефона: тот же Wi-Fi, порт приложения."""
    data = conf()
    port = int(data.get("port") or 8081)
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))
        address = probe.getsockname()[0]
        probe.close()
    except Exception:  # noqa: BLE001 - без сети остаётся localhost
        address = "127.0.0.1"
    return f"http://{address}:{port}"


def page_dashboard(flash: str = "") -> str:
    data = conf()
    counts = stats()
    bot_ok, bot_detail = (True, "@" + data["bot_username"]) if data.get("bot_username") \
        else (False, "имя не указано")
    rows = tasks(20)
    cards = []
    for row in rows:
        status = row["status"]
        tag = {"review": "warn", "published": "ok", "rejected": ""}.get(status, "")
        buttons = ""
        if status in ("review", "new"):
            buttons = (f"<form method='post' action='/app/approve' style='display:inline'>"
                       f"<input type='hidden' name='id' value='{row['id']}'>"
                       f"<button class='btn small'>Согласовать</button></form> "
                       f"<form method='post' action='/app/reject' style='display:inline'>"
                       f"<input type='hidden' name='id' value='{row['id']}'>"
                       f"<button class='btn ghost small'>Отклонить</button></form>")
        elif status in ("approved", "scheduled", "publish_error"):
            buttons = (f"<form method='post' action='/app/publish' style='display:inline'>"
                       f"<input type='hidden' name='id' value='{row['id']}'>"
                       f"<button class='btn small'>Опубликовать</button></form>")
        picture = (f"<img src='/media/{esc(urllib.parse.quote(pathlib.Path(row['image']).name))}'"
                   f" alt='картинка'>"
                   if row.get("image") else "")
        cards.append(
            f"<div class='task'><div class='row'><b>#{row['id']}</b>"
            f"<span class='tag {tag}'>{esc(status)}</span>"
            f"<span class='muted'>{esc(row['created_at'])}</span></div>"
            f"<div class='txt'>{esc((row['result'] or row['text'])[:700])}</div>{picture}"
            f"<div class='row' style='margin-top:10px'>{buttons}</div></div>")
    feed = "".join(cards) or "<p class='muted'>Пока нет материалов. Создайте первый ниже.</p>"

    knowledge_rows = "".join(
        f"<li><b>{esc(item['title'])}</b> — {esc(item['content'][:120])}</li>"
        for item in knowledge()[:8]) or "<li class='muted'>База знаний пуста</li>"

    return page("Кабинет", f"""
<div class="card">
  <h2>Состояние</h2>
  <div class="kpi">
    <div><span class="muted">Материалов</span><b>{counts.get('total', 0)}</b></div>
    <div><span class="muted">На согласовании</span><b>{counts.get('review', 0)}</b></div>
    <div><span class="muted">Опубликовано</span><b>{counts.get('published', 0)}</b></div>
  </div>
  <div class="status" style="margin-top:12px">
    <div><span>Нейросеть:</span> {esc(ai_status())}</div>
    <div><span>Бот:</span> {'<span class="tag ok">' + esc(bot_detail) + '</span>'
                            if bot_ok else '<span class="tag warn">токен не проверен</span>'}</div>
    <div><span>Картинки:</span> {esc(conf().get('image_backend') or 'auto')},
         Pillow {'есть' if PIL_OK else 'нет — рисуем встроенно'}</div>
    <div><span>Канал:</span> {esc(data.get('channel') or 'не указан')}</div>
    <div><span>Для телефона:</span> <b>{esc(lan_url())}</b> (тот же Wi-Fi)</div>
  </div>
</div>

<div class="card">
  <h2>Новый материал</h2>
  <form method="post" action="/app/task">
    <label>Задача обычными словами</label>
    <textarea name="text" placeholder="Пост про акцию на остекление, скидка 15% до конца месяца"
              required></textarea>
    <label>Что сделать</label>
    <select name="mode">
      <option value="both">Текст и картинка</option>
      <option value="text">Только текст</option>
      <option value="image">Только картинка</option>
    </select>
    <div class="row" style="margin-top:12px"><button type="submit">Сделать</button></div>
  </form>
</div>

<div class="card" id="tasks">
  <h2>Материалы</h2>
  {feed}
</div>

<div class="card">
  <h2>База знаний</h2>
  <ul class="muted" style="padding-left:18px">{knowledge_rows}</ul>
  <form method="post" action="/app/knowledge">
    <label>Заголовок</label><input name="title" placeholder="Прайс, гарантии, преимущества" required>
    <label>Текст</label><textarea name="content" required
      placeholder="Стеклопакеты от 3500 руб/м², срок 5 дней, гарантия 5 лет"></textarea>
    <div class="row" style="margin-top:12px"><button type="submit">Добавить</button></div>
  </form>
</div>

<div class="card">
  <h2>Журнал</h2>
  <ul class="muted" style="padding-left:18px">{
    "".join(f"<li>{esc(row['ts'])} · {esc(row['action'])} {esc(row['details'][:80])}</li>"
            for row in journal(12)) or "<li>Пока пусто</li>"}</ul>
</div>""", nav="dash", flash=flash)


def page_settings(flash: str = "", error: bool = False) -> str:
    data = conf()
    providers = {"offline": "Без нейросети (шаблоны)", "cloud": "Облако по ключу",
                 "local": "Локальная модель (Ollama)"}
    backends = {"auto": "auto — локальная, иначе облако", "local": "local — только локально",
                "api": "api — только облако", "card": "card — только карточка"}
    options_provider = "".join(
        f"<option value='{key}'{' selected' if data.get('provider') == key else ''}>{esc(label)}</option>"
        for key, label in providers.items())
    options_backend = "".join(
        f"<option value='{key}'{' selected' if data.get('image_backend') == key else ''}>{esc(label)}</option>"
        for key, label in backends.items())
    return page("Настройки", f"""
<div class="card">
  <h2>Бот</h2>
  <form method="post" action="/settings">
    <label>Имя бота</label>
    <input name="bot_username" value="{esc(data.get('bot_username'))}" placeholder="@my_bot">
    <label>Токен (оставьте пустым, чтобы не менять)</label>
    <input name="bot_token" placeholder="{esc('ключ сохранён' if data.get('bot_token') else '123456789:AA...')}"
           autocomplete="off">
    <label>Канал публикации</label>
    <input name="channel" value="{esc(data.get('channel'))}" placeholder="@channel или -100...">
    <h2>Нейросеть текстов</h2>
    <label>Режим</label><select name="provider">{options_provider}</select>
    <label>Адрес API</label>
    <input name="api_base" value="{esc(data.get('api_base'))}">
    <label>Ключ (оставьте пустым, чтобы не менять)</label>
    <input name="api_key" placeholder="{esc('ключ сохранён' if data.get('api_key') else 'sk-or-v1-...')}"
           autocomplete="off">
    <label>Модель текстов</label>
    <input name="text_model" value="{esc(data.get('text_model'))}">
    <label>Адрес локальной модели (Ollama)</label>
    <input name="local_llm_url" value="{esc(data.get('local_llm_url'))}">
    <h2>Картинки</h2>
    <label>Источник</label><select name="image_backend">{options_backend}</select>
    <label>Модель картинок в облаке</label>
    <input name="image_model" value="{esc(data.get('image_model'))}"
           placeholder="google/gemini-2.5-flash-image-preview">
    <label>Адрес локальной Stable Diffusion</label>
    <input name="local_sd_url" value="{esc(data.get('local_sd_url'))}">
    <h2>Компания и доступ</h2>
    <label>Название компании</label>
    <input name="company" value="{esc(data.get('company'))}">
    <label>Новый пароль (пусто — не менять)</label>
    <input name="password" type="password" autocomplete="new-password">
    <div class="row" style="margin-top:16px">
      <button type="submit">Сохранить</button>
      <a class="btn ghost" href="/app">Отмена</a>
    </div>
  </form>
</div>
<div class="card">
  <h2>Проверки</h2>
  <form method="post" action="/settings/check" style="display:inline">
    <button class="btn ghost">Проверить бота</button></form>
  <form method="post" action="/settings/check-ai" style="display:inline">
    <button class="btn ghost">Проверить нейросеть</button></form>
  <p class="muted" style="margin-top:10px">Адрес для телефона: <b>{esc(lan_url())}</b>.
  Чтобы телефон открывал кабинет, в настройках сети укажите host 0.0.0.0.</p>
</div>""", nav="set", flash=flash, error=error)


# --------------------------------------------------------------------- доступ
def hash_password(password: str) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                 salt.encode("utf-8"), 200_000)
    return f"pbkdf2_sha256$200000${salt}${digest.hex()}"


def check_password(password: str) -> bool:
    stored = conf().get("admin_hash") or ""
    try:
        _, rounds, salt, digest = stored.split("$")
        probe = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"),
                                    salt.encode("utf-8"), int(rounds)).hex()
        return hmac.compare_digest(probe, digest)
    except (ValueError, TypeError):
        return False


def session_cookie() -> str:
    """Подписанная кука: подделать нельзя, срок 30 дней."""
    stamp = str(int(time.time()))
    signature = hmac.new(conf()["secret"].encode(), stamp.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{stamp}.{signature}"


def session_ok(cookie: str) -> bool:
    try:
        stamp, signature = (cookie or "").split(".", 1)
        expected = hmac.new(conf()["secret"].encode(), stamp.encode(),
                            hashlib.sha256).hexdigest()[:32]
        if not hmac.compare_digest(signature, expected):
            return False
        return time.time() - int(stamp) < 30 * 86400
    except (ValueError, TypeError):
        return False


def configured() -> bool:
    data = conf()
    return bool(data.get("admin_hash") and data.get("company"))


# ---------------------------------------------------------------------- сервер
import http.server  # noqa: E402


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "AIMarketer/" + VERSION

    # --- служебное
    def _cookies(self) -> dict:
        raw = self.headers.get("Cookie") or ""
        result = {}
        for part in raw.split(";"):
            if "=" in part:
                key, _, value = part.partition("=")
                result[key.strip()] = value.strip()
        return result

    def _send(self, code: int, body: str | bytes, ctype: str = "text/html; charset=utf-8",
              headers: dict | None = None) -> None:
        raw = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(raw)

    def _redirect(self, location: str, headers: dict | None = None) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def _form(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        parsed = urllib.parse.parse_qs(raw)
        return {key: values[0] for key, values in parsed.items()}

    def _auth(self) -> bool:
        return session_ok(self._cookies().get("am", ""))

    def log_message(self, fmt: str, *args) -> None:  # тише в консоли
        pass

    # --- GET
    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        flash = (query.get("msg") or [""])[0]
        bad = (query.get("err") or [""])[0]

        if path == "/healthz":
            return self._send(200, f"ai-marketer-one ok {VERSION}", "text/plain; charset=utf-8")
        if path == "/manifest.webmanifest":
            return self._send(200, json.dumps(manifest(), ensure_ascii=False),
                              "application/manifest+json; charset=utf-8")
        if path == "/sw.js":
            return self._send(200, SW_JS, "application/javascript; charset=utf-8")
        if path in ("/icon-192.png", "/icon-512.png", "/apple-touch-icon.png"):
            size = 192 if "192" in path or "apple" in path else 512
            return self._send(200, icon_png(size), "image/png",
                              {"Cache-Control": "public, max-age=86400"})
        if path == "/offline":
            return self._send(200, page("Без сети",
                                        "<div class='card'><h2>Нет связи</h2>"
                                        "<p>Приложение откроется, когда вернётся сеть. "
                                        "Данные сохранены на этом устройстве.</p></div>"))
        if path.startswith("/media/"):
            return self._serve_media(path[len("/media/"):])
        if not configured():
            return self._send(200, page_setup(bad))
        if path == "/setup":
            return self._send(200, page_setup(bad, bool(bad)))
        if path == "/login":
            return self._send(200, page_login(bad))
        if path == "/logout":
            return self._redirect("/login", {"Set-Cookie": "am=; Path=/; Max-Age=0; HttpOnly"})
        if not self._auth():
            return self._redirect("/login")
        if path in ("/", "/app"):
            return self._send(200, page_dashboard(flash))
        if path == "/settings":
            return self._send(200, page_settings(flash, bool(bad)))
        return self._send(404, page("404", "<div class='card'>Страница не найдена</div>"))

    def _serve_media(self, name: str) -> None:
        safe = pathlib.Path(urllib.parse.unquote(name)).name
        target = (MEDIA_DIR / safe).resolve()
        try:
            target.relative_to(MEDIA_DIR.resolve())
        except ValueError:
            return self._send(404, "not found", "text/plain")
        if not target.is_file():
            return self._send(404, "not found", "text/plain")
        ctype = "image/png" if target.suffix.lower() == ".png" else "application/octet-stream"
        return self._send(200, target.read_bytes(), ctype)

    # --- POST
    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.urlparse(self.path).path.rstrip("/") or "/"
        form = self._form()

        if path == "/setup":
            return self._setup(form)
        if path == "/login":
            if check_password(form.get("password") or ""):
                log("login", "успешный вход")
                return self._redirect("/app", {"Set-Cookie": f"am={session_cookie()}; "
                                                              "Path=/; HttpOnly; SameSite=Strict; "
                                                              "Max-Age=2592000"})
            log("login_fail", "неверный пароль")
            return self._redirect("/login?err=" + urllib.parse.quote("Неверный пароль"))

        if not self._auth():
            return self._redirect("/login")

        if path == "/app/task":
            return self._make(form)
        if path == "/app/approve":
            task_id = int(form.get("id") or 0)
            update_task(task_id, status="approved")
            log("approve", f"#{task_id}")
            return self._redirect("/app?msg=" + urllib.parse.quote("Согласовано"))
        if path == "/app/reject":
            task_id = int(form.get("id") or 0)
            update_task(task_id, status="rejected")
            log("reject", f"#{task_id}")
            return self._redirect("/app?msg=" + urllib.parse.quote("Отклонено"))
        if path == "/app/publish":
            verdict = publish_task(int(form.get("id") or 0))
            return self._redirect("/app?msg=" + urllib.parse.quote(verdict))
        if path == "/app/knowledge":
            title = (form.get("title") or "").strip()
            content = (form.get("content") or "").strip()
            if len(title) < 2 or len(content) < 5:
                return self._redirect("/app?err=" + urllib.parse.quote("Нужны заголовок и текст"))
            add_knowledge(title, content)
            return self._redirect("/app?msg=" + urllib.parse.quote("Добавлено в базу знаний"))
        if path == "/settings":
            return self._settings(form)
        if path == "/settings/check":
            ok, detail = bot_check()
            message = ("Бот отвечает: " + detail) if ok else ("Бот не отвечает: " + detail)
            return self._redirect("/settings?msg=" + urllib.parse.quote(message))
        if path == "/settings/check-ai":
            text, problem = ask("Проверка связи: напиши одно короткое предложение о компании.")
            message = ("Нейросеть отвечает: " + text[:80]) if not problem else \
                ("Нейросеть недоступна: " + problem + " — работают шаблоны")
            return self._redirect("/settings?msg=" + urllib.parse.quote(message))
        return self._send(404, page("404", "<div class='card'>Страница не найдена</div>"))

    def _setup(self, form: dict) -> None:
        company = (form.get("company") or "").strip()
        password = form.get("password") or ""
        if len(company) < 2:
            return self._redirect("/setup?err=" + urllib.parse.quote("Укажите название компании"))
        if len(password) < 6:
            return self._redirect("/setup?err=" + urllib.parse.quote("Пароль минимум 6 символов"))
        token = (form.get("bot_token") or "").strip()
        username = (form.get("bot_username") or "").strip().lstrip("@")
        changes = {"company": company, "admin_hash": hash_password(password),
                   "provider": form.get("provider") or "offline",
                   "channel": (form.get("channel") or "").strip()}
        if token:
            changes["bot_token"] = token
        if username:
            changes["bot_username"] = username
        if form.get("api_key"):
            changes["api_key"] = form["api_key"].strip()
        patch_conf(**changes)
        if token:
            # проверка идёт в фоне: без интернета форма не должна «висеть»
            threading.Thread(target=bot_check, daemon=True).start()
        log("setup", company)
        return self._redirect("/app", {"Set-Cookie": f"am={session_cookie()}; Path=/; HttpOnly; "
                                                      "SameSite=Strict; Max-Age=2592000"})

    def _settings(self, form: dict) -> None:
        changes = {}
        for field in ("bot_username", "channel", "api_base", "text_model", "image_model",
                      "local_llm_url", "local_sd_url", "company", "provider", "image_backend"):
            if field in form:
                changes[field] = (form.get(field) or "").strip()
        if changes.get("bot_username"):
            changes["bot_username"] = changes["bot_username"].lstrip("@")
        if form.get("bot_token"):
            changes["bot_token"] = form["bot_token"].strip()
        if form.get("api_key"):
            changes["api_key"] = form["api_key"].strip()
        if form.get("password"):
            if len(form["password"]) < 6:
                return self._redirect("/settings?err=" + urllib.parse.quote("Пароль минимум 6 символов"))
            changes["admin_hash"] = hash_password(form["password"])
        patch_conf(**changes)
        log("settings", ", ".join(changes.keys()))
        return self._redirect("/settings?msg=" + urllib.parse.quote("Настройки сохранены"))

    def _make(self, form: dict) -> None:
        text = (form.get("text") or "").strip()
        mode = form.get("mode") or "both"
        if len(text) < 3:
            return self._redirect("/app?err=" + urllib.parse.quote("Опишите задачу словами"))
        task_id = create_task(text, "site")
        if mode == "image":
            path, kind, reason = make_image(text, text)
            update_task(task_id, status="review", result=f"Картинка к теме: {text}",
                        image=str(path))
            log("image_only", f"#{task_id} {kind}")
            message = "Картинка готова" + (f" ({reason})" if reason else "")
            return self._redirect("/app?msg=" + urllib.parse.quote(message))

        answer, problem = ask(text)
        if mode == "text":
            update_task(task_id, status="review", result=answer)
            message = "Текст готов" + (f": {problem}" if problem else "")
            return self._redirect("/app?msg=" + urllib.parse.quote(message[:160]))

        path, kind, reason = make_image(answer.splitlines()[0][:70] or text, answer[:300])
        update_task(task_id, status="review", result=answer, image=str(path))
        note = "; ".join(item for item in (problem, reason) if item)
        message = "Материал готов" + (f" ({note})" if note else "")
        return self._redirect("/app?msg=" + urllib.parse.quote(message[:160]))


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def find_port(start: int) -> int:
    """Свободный порт: 8081, если занят — следующий."""
    for port in range(start, start + 10):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return start


# ------------------------------------------------------------------------ старт
def banner(port: int, host: str) -> None:
    data = conf()
    line = "=" * 58
    print(line)
    print(f" AI-МАРКЕТОЛОГ {VERSION} — один файл, всё внутри")
    print(line)
    print(f" Сайт на этом компьютере:  http://127.0.0.1:{port}")
    print(f" С телефона (тот же Wi-Fi): {lan_url()}")
    if host != "0.0.0.0":
        print("   сейчас доступен только этот компьютер; для телефона")
        print("   запустите: python ai_marketer_one.py --host 0.0.0.0")
    print("")
    print(f" Бот:     {('@' + data['bot_username']) if data.get('bot_username') else 'не подключён'}")
    print(f" Тексты:  {ai_status()}")
    print(f" Картинки: {data.get('image_backend') or 'auto'}"
          f"{' (Pillow есть)' if PIL_OK else ' (без Pillow — простые картинки)'}")
    print(f" Канал:   {data.get('channel') or 'не указан'}")
    print("")
    print(" Остановить: Ctrl+C")
    print(line)


def main() -> int:
    args = [item for item in sys.argv[1:]]
    host = "127.0.0.1"
    port_arg = 0
    no_browser = "--no-browser" in args
    for index, arg in enumerate(args):
        if arg == "--host" and index + 1 < len(args):
            host = args[index + 1]
        if arg == "--port" and index + 1 < len(args) and args[index + 1].isdigit():
            port_arg = int(args[index + 1])

    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    data = conf()
    if host == "127.0.0.1" and data.get("host"):
        host = data["host"]
    port = port_arg or find_port(int(data.get("port") or 8081))
    patch_conf(port=port, host=host)

    threading.Thread(target=bot_loop, daemon=True).start()
    server = Server((host, port), Handler)
    banner(port, host)

    if not configured():
        print("\n Первый запуск: откройте сайт и заполните форму —")
        print(" имя бота, токен, компания и пароль администратора.\n")
    if not no_browser and host in ("127.0.0.1", "localhost"):
        try:
            import webbrowser

            threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
        except Exception:  # noqa: BLE001
            pass

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
