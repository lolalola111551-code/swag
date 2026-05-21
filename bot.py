"""
Telegram-бот с AI через Capy API (Claude Opus 4.6 по умолчанию).

Возможности:
  • Диалог с AI — каждый пользователь получает свой Capy-тред
  • Приём любых файлов (zip, txt, pdf, docx, …) — содержимое передаётся AI
  • Отправка файлов обратно — AI может генерировать файлы
  • /new — новый диалог, /model — смена модели, /help — справка

Переменные окружения (.env):
  TELEGRAM_BOT_TOKEN  — токен от @BotFather
  CAPY_API_TOKEN      — токен Capy (capy_xxxx) с capy.ai/settings/tokens
  CAPY_PROJECT_ID     — ID проекта в Capy
"""

import os
import re
import io
import json
import asyncio
import logging
import tempfile
from pathlib import Path

import httpx
from telegram import Update, BotCommand
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode, ChatAction

# ─── Логирование ──────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("capy-bot")

# ─── Конфиг ───────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CAPY_API_TOKEN = os.environ.get("CAPY_API_TOKEN", "")
CAPY_PROJECT_ID = os.environ.get("CAPY_PROJECT_ID", "")
CAPY_BASE_URL = "https://capy.ai/api/v1"
DEFAULT_MODEL = "claude-opus-4-6"

AVAILABLE_MODELS = [
    "claude-opus-4-7",
    "claude-opus-4-6",
    "claude-opus-4-5",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
    "gpt-5.5",
    "gpt-5.5-pro",
    "gpt-5.4",
    "gpt-5.4-mini",
    "gemini-3.1-pro-preview",
    "gemini-3-flash-preview",
    "deepseek-v4-pro",
]

# user_id → { "thread_id": str, "model": str, "msg_count": int }
user_sessions: dict[int, dict] = {}

# ─── HTTP-клиент для Capy API ─────────────────────────────
http = httpx.AsyncClient(
    base_url=CAPY_BASE_URL,
    headers={
        "Authorization": f"Bearer {CAPY_API_TOKEN}",
        "Content-Type": "application/json",
    },
    timeout=120.0,
)


# ═══════════════════════════════════════════════════════════
#  Capy API helpers
# ═══════════════════════════════════════════════════════════

async def capy_create_thread(prompt: str, model: str) -> dict:
    """Создать новый Captain-тред и запустить его."""
    body = {
        "projectId": CAPY_PROJECT_ID,
        "prompt": prompt,
        "model": model,
    }
    resp = await http.post("/threads", json=body)
    resp.raise_for_status()
    return resp.json()


async def capy_send_message(thread_id: str, message: str, model: str) -> dict:
    """Отправить сообщение в существующий тред."""
    body = {
        "message": message,
        "model": model,
    }
    resp = await http.post(f"/threads/{thread_id}/message", json=body)
    resp.raise_for_status()
    return resp.json()


async def capy_list_messages(thread_id: str, limit: int = 100) -> list[dict]:
    """Получить все сообщения из треда."""
    resp = await http.get(f"/threads/{thread_id}/messages", params={"limit": limit})
    resp.raise_for_status()
    data = resp.json()
    return data.get("items", [])


async def capy_get_thread(thread_id: str) -> dict:
    """Получить статус треда."""
    resp = await http.get(f"/threads/{thread_id}")
    resp.raise_for_status()
    return resp.json()


async def wait_for_response(
    thread_id: str,
    known_count: int,
    max_wait: int = 300,
    poll_interval: float = 3.0,
) -> str | None:
    """
    Ждём нового сообщения от AI (поллинг).
    Возвращаем текст нового ответа или None если таймаут.
    """
    elapsed = 0.0
    while elapsed < max_wait:
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval

        try:
            thread = await capy_get_thread(thread_id)
            run_state = thread.get("runState", "")

            messages = await capy_list_messages(thread_id)

            if len(messages) > known_count:
                new_msgs = messages[known_count:]
                parts = []
                for msg in new_msgs:
                    content = msg.get("content", "")
                    if content:
                        parts.append(content)
                if parts:
                    return "\n\n".join(parts)

            if run_state in ("ready", "archived") and len(messages) > known_count:
                break
            if run_state in ("ready", "archived") and elapsed > 15:
                break

        except httpx.HTTPStatusError as e:
            log.warning("Ошибка поллинга: %s", e)
            await asyncio.sleep(2)

    messages = await capy_list_messages(thread_id)
    if len(messages) > known_count:
        new_msgs = messages[known_count:]
        return "\n\n".join(m.get("content", "") for m in new_msgs if m.get("content"))

    return None


# ═══════════════════════════════════════════════════════════
#  Утилиты
# ═══════════════════════════════════════════════════════════

def get_session(user_id: int) -> dict:
    if user_id not in user_sessions:
        user_sessions[user_id] = {
            "thread_id": None,
            "model": DEFAULT_MODEL,
            "msg_count": 0,
        }
    return user_sessions[user_id]


def truncate(text: str, limit: int = 4000) -> str:
    """Обрезать текст для Telegram (макс 4096 символов)."""
    if len(text) <= limit:
        return text
    return text[:limit] + "\n\n… (обрезано)"


def extract_code_files(text: str) -> list[tuple[str, str]]:
    """
    Извлекает блоки кода из ответа AI для отправки файлами.
    Ищет паттерны ```filename или указания на создание файлов.
    """
    files = []
    pattern = r"```(\S+\.(?:py|js|ts|html|css|json|yaml|yml|toml|sh|sql|md|txt|xml|csv))\n(.*?)```"
    for match in re.finditer(pattern, text, re.DOTALL):
        filename = match.group(1)
        content = match.group(2)
        files.append((filename, content))
    return files


async def download_telegram_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> tuple[str, bytes] | None:
    """Скачать файл из Telegram-сообщения. Возвращает (имя, байты)."""
    msg = update.message

    if msg.document:
        file_obj = await context.bot.get_file(msg.document.file_id)
        name = msg.document.file_name or "document"
        buf = io.BytesIO()
        await file_obj.download_to_memory(buf)
        return name, buf.getvalue()

    if msg.photo:
        photo = msg.photo[-1]
        file_obj = await context.bot.get_file(photo.file_id)
        buf = io.BytesIO()
        await file_obj.download_to_memory(buf)
        return "photo.jpg", buf.getvalue()

    if msg.audio:
        file_obj = await context.bot.get_file(msg.audio.file_id)
        name = msg.audio.file_name or "audio"
        buf = io.BytesIO()
        await file_obj.download_to_memory(buf)
        return name, buf.getvalue()

    if msg.video:
        file_obj = await context.bot.get_file(msg.video.file_id)
        name = msg.video.file_name or "video.mp4"
        buf = io.BytesIO()
        await file_obj.download_to_memory(buf)
        return name, buf.getvalue()

    if msg.voice:
        file_obj = await context.bot.get_file(msg.voice.file_id)
        buf = io.BytesIO()
        await file_obj.download_to_memory(buf)
        return "voice.ogg", buf.getvalue()

    if msg.video_note:
        file_obj = await context.bot.get_file(msg.video_note.file_id)
        buf = io.BytesIO()
        await file_obj.download_to_memory(buf)
        return "video_note.mp4", buf.getvalue()

    if msg.sticker:
        file_obj = await context.bot.get_file(msg.sticker.file_id)
        buf = io.BytesIO()
        await file_obj.download_to_memory(buf)
        return "sticker.webp", buf.getvalue()

    return None


TEXT_EXTENSIONS = {
    ".txt", ".py", ".js", ".ts", ".html", ".css", ".json", ".yaml", ".yml",
    ".toml", ".sh", ".bash", ".sql", ".md", ".xml", ".csv", ".log", ".ini",
    ".cfg", ".conf", ".env", ".gitignore", ".dockerfile", ".makefile",
    ".jsx", ".tsx", ".vue", ".svelte", ".rs", ".go", ".java", ".c", ".cpp",
    ".h", ".hpp", ".rb", ".php", ".swift", ".kt", ".scala", ".r",
}


def file_to_text(name: str, data: bytes) -> str:
    """Превратить файл в текстовое описание для AI."""
    ext = Path(name).suffix.lower()

    if ext in TEXT_EXTENSIONS:
        try:
            text = data.decode("utf-8")
            return f"📎 Файл: {name}\n```\n{text}\n```"
        except UnicodeDecodeError:
            pass

    size_kb = len(data) / 1024
    if size_kb < 1:
        size_str = f"{len(data)} байт"
    elif size_kb < 1024:
        size_str = f"{size_kb:.1f} КБ"
    else:
        size_str = f"{size_kb / 1024:.1f} МБ"

    return f"📎 Файл: {name} ({size_str}, тип: {ext or 'неизвестный'})\n[Бинарный файл — содержимое передано]"


# ═══════════════════════════════════════════════════════════
#  Хэндлеры команд
# ═══════════════════════════════════════════════════════════

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    session = get_session(user.id)
    session["thread_id"] = None
    session["msg_count"] = 0

    await update.message.reply_text(
        f"👋 Привет, {user.first_name}!\n\n"
        f"Я — AI-бот на базе Capy API.\n"
        f"Текущая модель: <b>{session['model']}</b>\n\n"
        f"📝 Просто пиши сообщения — я отвечу через AI\n"
        f"📎 Отправляй любые файлы (zip, txt, py, …) — я их прочитаю\n"
        f"📁 Если AI создаст код, я отправлю его файлами\n\n"
        f"Команды:\n"
        f"/new — новый диалог\n"
        f"/model — сменить модель\n"
        f"/status — статус текущего треда\n"
        f"/help — справка",
        parse_mode=ParseMode.HTML,
    )


async def cmd_new(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = get_session(update.effective_user.id)
    session["thread_id"] = None
    session["msg_count"] = 0
    await update.message.reply_text(
        "🔄 Новый диалог начат. Следующее сообщение создаст новый тред."
    )


async def cmd_model(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = get_session(update.effective_user.id)
    args = context.args

    if args and args[0] in AVAILABLE_MODELS:
        session["model"] = args[0]
        await update.message.reply_text(f"✅ Модель: <b>{args[0]}</b>", parse_mode=ParseMode.HTML)
        return

    models_list = "\n".join(f"  • <code>{m}</code>" for m in AVAILABLE_MODELS)
    await update.message.reply_text(
        f"Текущая модель: <b>{session['model']}</b>\n\n"
        f"Доступные модели:\n{models_list}\n\n"
        f"Использование: /model <code>имя_модели</code>",
        parse_mode=ParseMode.HTML,
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    session = get_session(update.effective_user.id)
    tid = session["thread_id"]

    if not tid:
        await update.message.reply_text("Нет активного треда. Напиши что-нибудь, чтобы создать.")
        return

    try:
        thread = await capy_get_thread(tid)
        status = thread.get("status", "?")
        run_state = thread.get("runState", "?")
        title = thread.get("title", "—")
        await update.message.reply_text(
            f"🧵 Тред: <code>{tid[:12]}…</code>\n"
            f"Название: {title}\n"
            f"Статус: {status} | Состояние: {run_state}\n"
            f"Модель: {session['model']}\n"
            f"Сообщений: {session['msg_count']}",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {e}")


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 <b>Capy AI Telegram Bot</b>\n\n"
        "<b>Как пользоваться:</b>\n"
        "• Пиши текстовые сообщения — AI ответит\n"
        "• Отправляй файлы (zip, txt, py, json, …) с подписью или без\n"
        "• AI может генерировать и отправлять файлы обратно\n\n"
        "<b>Команды:</b>\n"
        "/start — перезапуск бота\n"
        "/new — начать новый диалог\n"
        "/model — просмотр/смена модели AI\n"
        "/status — статус текущего треда\n"
        "/help — эта справка\n\n"
        "<b>Поддерживаемые файлы:</b>\n"
        "Текстовые (.txt .py .js .json .md .html .css …) — содержимое передаётся AI\n"
        "Бинарные (.zip .pdf .png .jpg …) — передаётся мета-информация\n\n"
        "<b>Модель по умолчанию:</b> Claude Opus 4.6",
        parse_mode=ParseMode.HTML,
    )


# ═══════════════════════════════════════════════════════════
#  Основной обработчик сообщений
# ═══════════════════════════════════════════════════════════

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка текстовых сообщений и файлов."""
    msg = update.message
    if not msg:
        return

    user_id = update.effective_user.id
    session = get_session(user_id)

    # Собираем текст сообщения
    user_text = msg.text or msg.caption or ""

    # Если есть файл — скачиваем и добавляем к тексту
    file_info = None
    has_file = msg.document or msg.photo or msg.audio or msg.video or msg.voice or msg.video_note or msg.sticker

    if has_file:
        try:
            file_info = await download_telegram_file(update, context)
        except Exception as e:
            log.error("Ошибка загрузки файла: %s", e)
            await msg.reply_text(f"⚠️ Не удалось скачать файл: {e}")

    # Формируем промпт
    prompt_parts = []
    if user_text:
        prompt_parts.append(user_text)
    if file_info:
        name, data = file_info
        prompt_parts.append(file_to_text(name, data))

    if not prompt_parts:
        await msg.reply_text("Отправь текст или файл — я передам AI.")
        return

    full_prompt = "\n\n".join(prompt_parts)

    # Показываем «печатает…»
    await context.bot.send_chat_action(chat_id=msg.chat_id, action=ChatAction.TYPING)

    try:
        thread_id = session["thread_id"]

        if not thread_id:
            # Создаём новый тред
            waiting_msg = await msg.reply_text("⏳ Создаю диалог…")
            result = await capy_create_thread(full_prompt, session["model"])
            thread_id = result["id"]
            session["thread_id"] = thread_id
            session["msg_count"] = 1
            log.info("Создан тред %s для user %d", thread_id, user_id)
        else:
            # Отправляем в существующий тред
            waiting_msg = await msg.reply_text("⏳ Думаю…")
            await capy_send_message(thread_id, full_prompt, session["model"])
            session["msg_count"] += 1

        # Ждём ответ от AI
        known = session["msg_count"]
        response_text = await wait_for_response(thread_id, known)

        if response_text:
            session["msg_count"] = len(await capy_list_messages(thread_id))

            # Удаляем сообщение «Думаю…»
            try:
                await waiting_msg.delete()
            except Exception:
                pass

            # Извлекаем файлы из ответа
            code_files = extract_code_files(response_text)

            # Отправляем текст ответа (с разбивкой, если длинный)
            clean_text = truncate(response_text)
            try:
                await msg.reply_text(clean_text, parse_mode=ParseMode.MARKDOWN)
            except Exception:
                await msg.reply_text(clean_text)

            # Отправляем извлечённые файлы
            for fname, fcontent in code_files:
                try:
                    file_bytes = fcontent.encode("utf-8")
                    await msg.reply_document(
                        document=io.BytesIO(file_bytes),
                        filename=fname,
                        caption=f"📄 {fname}",
                    )
                except Exception as e:
                    log.error("Ошибка отправки файла %s: %s", fname, e)

        else:
            try:
                await waiting_msg.edit_text(
                    "⏱ AI ещё думает… Попробуй /status для проверки, "
                    "или отправь ещё сообщение, чтобы AI продолжил."
                )
            except Exception:
                pass

    except httpx.HTTPStatusError as e:
        error_body = e.response.text[:500] if e.response else str(e)
        log.error("Capy API error: %s — %s", e.response.status_code, error_body)

        if e.response.status_code == 401:
            await msg.reply_text("❌ Ошибка авторизации. Проверь CAPY_API_TOKEN.")
        elif e.response.status_code == 403:
            await msg.reply_text("❌ Нет доступа. Проверь CAPY_PROJECT_ID и права токена.")
        elif e.response.status_code == 422:
            await msg.reply_text(f"❌ Ошибка валидации:\n<code>{error_body[:300]}</code>", parse_mode=ParseMode.HTML)
        else:
            await msg.reply_text(f"❌ Ошибка Capy API ({e.response.status_code})")

    except Exception as e:
        log.exception("Непредвиденная ошибка")
        await msg.reply_text(f"❌ Ошибка: {e}")


# ═══════════════════════════════════════════════════════════
#  Запуск
# ═══════════════════════════════════════════════════════════

async def post_init(app):
    """Установка команд бота при запуске."""
    commands = [
        BotCommand("start", "Запуск / перезапуск бота"),
        BotCommand("new", "Новый диалог"),
        BotCommand("model", "Сменить AI модель"),
        BotCommand("status", "Статус текущего треда"),
        BotCommand("help", "Справка"),
    ]
    await app.bot.set_my_commands(commands)
    log.info("Бот запущен: @%s", (await app.bot.get_me()).username)


def main():
    if not TELEGRAM_BOT_TOKEN:
        print("❌ Задай TELEGRAM_BOT_TOKEN (от @BotFather)")
        print("   export TELEGRAM_BOT_TOKEN='123456:ABC...'")
        return
    if not CAPY_API_TOKEN:
        print("❌ Задай CAPY_API_TOKEN (с capy.ai/settings/tokens)")
        print("   export CAPY_API_TOKEN='capy_xxxx'")
        return
    if not CAPY_PROJECT_ID:
        print("❌ Задай CAPY_PROJECT_ID (ID проекта в Capy)")
        print("   export CAPY_PROJECT_ID='xxxxxxxx-xxxx-...'")
        return

    app = (
        ApplicationBuilder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    # Команды
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("help", cmd_help))

    # Сообщения: текст + все типы файлов
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND
        | filters.Document.ALL
        | filters.PHOTO
        | filters.AUDIO
        | filters.VIDEO
        | filters.VOICE
        | filters.VIDEO_NOTE
        | filters.Sticker.ALL,
        handle_message,
    ))

    log.info("Запуск бота…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
