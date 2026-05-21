"""
Telegram-бот с AI через Capy API (Claude Opus 4.6).
Один файл, без .env, без сервера.

Запуск:
  pip install python-telegram-bot httpx
  python bot.py
"""

import re
import io
import asyncio
import logging
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

# ══════════════════════════════════════════════════════════════
#  ⬇⬇⬇  ВСТАВЬ СВОИ ТОКЕНЫ СЮДА  ⬇⬇⬇
# ══════════════════════════════════════════════════════════════

TELEGRAM_BOT_TOKEN = "YOUR_TELEGRAM_BOT_TOKEN"
CAPY_API_TOKEN = "capy_YOUR_TOKEN_HERE"

# ══════════════════════════════════════════════════════════════

CAPY_PROJECT_ID = "2445f350-126f-42f3-8ded-63c11e0717dc"
CAPY_BASE = "https://capy.ai/api/v1"
MODEL = "claude-opus-4-6"

MODELS = [
    "claude-opus-4-7", "claude-opus-4-6", "claude-opus-4-5",
    "claude-sonnet-4-6", "claude-haiku-4-5",
    "gpt-5.5", "gpt-5.5-pro", "gpt-5.4", "gpt-5.4-mini",
    "gemini-3.1-pro-preview", "gemini-3-flash-preview",
    "deepseek-v4-pro",
]

logging.basicConfig(format="%(asctime)s [%(levelname)s] %(message)s", level=logging.INFO)
log = logging.getLogger("bot")

sessions: dict[int, dict] = {}

http: httpx.AsyncClient | None = None

def get_http():
    global http
    if http is None:
        http = httpx.AsyncClient(
            base_url=CAPY_BASE,
            headers={"Authorization": f"Bearer {CAPY_API_TOKEN}", "Content-Type": "application/json"},
            timeout=120.0,
        )
    return http

TEXT_EXT = {
    ".txt", ".py", ".js", ".ts", ".html", ".css", ".json", ".yaml", ".yml",
    ".toml", ".sh", ".sql", ".md", ".xml", ".csv", ".log", ".ini", ".cfg",
    ".env", ".jsx", ".tsx", ".vue", ".rs", ".go", ".java", ".c", ".cpp",
    ".h", ".rb", ".php", ".swift", ".kt", ".r",
}


# ─── Capy API ─────────────────────────────────────────────

async def capy_create(prompt, model):
    r = await get_http().post("/threads", json={"projectId": CAPY_PROJECT_ID, "prompt": prompt, "model": model})
    r.raise_for_status()
    return r.json()

async def capy_send(tid, msg, model):
    r = await get_http().post(f"/threads/{tid}/message", json={"message": msg, "model": model})
    r.raise_for_status()
    return r.json()

async def capy_messages(tid):
    r = await get_http().get(f"/threads/{tid}/messages", params={"limit": 100})
    r.raise_for_status()
    return r.json().get("items", [])

async def capy_thread(tid):
    r = await get_http().get(f"/threads/{tid}")
    r.raise_for_status()
    return r.json()

async def wait_response(tid, known, max_wait=300):
    elapsed = 0.0
    while elapsed < max_wait:
        await asyncio.sleep(3)
        elapsed += 3
        try:
            msgs = await capy_messages(tid)
            if len(msgs) > known:
                return "\n\n".join(m["content"] for m in msgs[known:] if m.get("content"))
            t = await capy_thread(tid)
            if t.get("runState") in ("ready", "archived") and elapsed > 10:
                break
        except Exception as e:
            log.warning("poll error: %s", e)
    msgs = await capy_messages(tid)
    if len(msgs) > known:
        return "\n\n".join(m["content"] for m in msgs[known:] if m.get("content"))
    return None


# ─── Утилиты ──────────────────────────────────────────────

def ses(uid):
    if uid not in sessions:
        sessions[uid] = {"tid": None, "model": MODEL, "n": 0}
    return sessions[uid]

def cut(text, lim=4000):
    return text if len(text) <= lim else text[:lim] + "\n\n… (обрезано)"

def code_files(text):
    out = []
    for m in re.finditer(r"```(\S+\.(?:py|js|ts|html|css|json|yaml|yml|sh|sql|md|txt|xml|csv))\n(.*?)```", text, re.DOTALL):
        out.append((m.group(1), m.group(2)))
    return out

async def dl_file(update, ctx):
    msg = update.message
    for attr, default in [("document", None), ("audio", None), ("video", None), ("voice", "voice.ogg"), ("video_note", "video.mp4"), ("sticker", "sticker.webp")]:
        obj = getattr(msg, attr, None)
        if obj:
            f = await ctx.bot.get_file(obj.file_id)
            buf = io.BytesIO()
            await f.download_to_memory(buf)
            name = getattr(obj, "file_name", None) or default or attr
            return name, buf.getvalue()
    if msg.photo:
        f = await ctx.bot.get_file(msg.photo[-1].file_id)
        buf = io.BytesIO()
        await f.download_to_memory(buf)
        return "photo.jpg", buf.getvalue()
    return None

def file2text(name, data):
    ext = Path(name).suffix.lower()
    if ext in TEXT_EXT:
        try:
            return f"📎 Файл: {name}\n```\n{data.decode('utf-8')}\n```"
        except UnicodeDecodeError:
            pass
    sz = len(data)
    s = f"{sz} Б" if sz < 1024 else f"{sz/1024:.1f} КБ" if sz < 1048576 else f"{sz/1048576:.1f} МБ"
    return f"📎 Файл: {name} ({s}, тип: {ext or '?'})"


# ─── Команды ──────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = ses(update.effective_user.id)
    s["tid"] = None; s["n"] = 0
    await update.message.reply_text(
        f"👋 Привет, {update.effective_user.first_name}!\n\n"
        f"Модель: <b>{s['model']}</b>\n\n"
        "📝 Пиши — AI ответит\n"
        "📎 Кидай файлы (zip, txt, py, …)\n"
        "📁 AI может отправлять файлы обратно\n\n"
        "/new — новый диалог\n"
        "/model — сменить модель\n"
        "/status — статус треда",
        parse_mode=ParseMode.HTML,
    )

async def cmd_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = ses(update.effective_user.id)
    s["tid"] = None; s["n"] = 0
    await update.message.reply_text("🔄 Новый диалог. Пиши!")

async def cmd_model(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = ses(update.effective_user.id)
    if ctx.args and ctx.args[0] in MODELS:
        s["model"] = ctx.args[0]
        await update.message.reply_text(f"✅ Модель: <b>{ctx.args[0]}</b>", parse_mode=ParseMode.HTML)
        return
    ml = "\n".join(f"  • <code>{m}</code>" for m in MODELS)
    await update.message.reply_text(
        f"Сейчас: <b>{s['model']}</b>\n\n{ml}\n\n/model <code>имя</code>",
        parse_mode=ParseMode.HTML,
    )

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    s = ses(update.effective_user.id)
    if not s["tid"]:
        await update.message.reply_text("Нет активного треда.")
        return
    try:
        t = await capy_thread(s["tid"])
        await update.message.reply_text(
            f"🧵 <code>{s['tid'][:16]}…</code>\n"
            f"Статус: {t.get('status')} | {t.get('runState')}\n"
            f"Модель: {s['model']} | Сообщений: {s['n']}",
            parse_mode=ParseMode.HTML,
        )
    except Exception as e:
        await update.message.reply_text(f"❌ {e}")


# ─── Главный обработчик ──────────────────────────────────

async def handle(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    s = ses(update.effective_user.id)
    text = msg.text or msg.caption or ""

    # Файл
    has_file = msg.document or msg.photo or msg.audio or msg.video or msg.voice or msg.video_note or msg.sticker
    fi = None
    if has_file:
        try:
            fi = await dl_file(update, ctx)
        except Exception as e:
            await msg.reply_text(f"⚠️ Не скачал файл: {e}")

    parts = []
    if text:
        parts.append(text)
    if fi:
        parts.append(file2text(fi[0], fi[1]))
    if not parts:
        await msg.reply_text("Отправь текст или файл.")
        return

    prompt = "\n\n".join(parts)
    await ctx.bot.send_chat_action(chat_id=msg.chat_id, action=ChatAction.TYPING)

    try:
        if not s["tid"]:
            w = await msg.reply_text("⏳ Создаю диалог…")
            r = await capy_create(prompt, s["model"])
            s["tid"] = r["id"]
            s["n"] = 1
        else:
            w = await msg.reply_text("⏳ Думаю…")
            await capy_send(s["tid"], prompt, s["model"])
            s["n"] += 1

        resp = await wait_response(s["tid"], s["n"])

        if resp:
            s["n"] = len(await capy_messages(s["tid"]))
            try:
                await w.delete()
            except Exception:
                pass

            try:
                await msg.reply_text(cut(resp), parse_mode=ParseMode.MARKDOWN)
            except Exception:
                await msg.reply_text(cut(resp))

            for fname, fc in code_files(resp):
                try:
                    await msg.reply_document(
                        document=io.BytesIO(fc.encode("utf-8")),
                        filename=fname,
                        caption=f"📄 {fname}",
                    )
                except Exception:
                    pass
        else:
            try:
                await w.edit_text("⏱ AI ещё думает… /status для проверки")
            except Exception:
                pass

    except httpx.HTTPStatusError as e:
        code = e.response.status_code
        body = e.response.text[:300]
        if code == 401:
            await msg.reply_text("❌ Неверный CAPY_API_TOKEN.")
        elif code == 403:
            await msg.reply_text("❌ Нет доступа. Проверь токен и project ID.")
        else:
            await msg.reply_text(f"❌ Capy API {code}:\n<code>{body}</code>", parse_mode=ParseMode.HTML)
    except Exception as e:
        log.exception("error")
        await msg.reply_text(f"❌ {e}")


# ─── Запуск ───────────────────────────────────────────────

async def init(app):
    await app.bot.set_my_commands([
        BotCommand("start", "Запуск бота"),
        BotCommand("new", "Новый диалог"),
        BotCommand("model", "Сменить модель"),
        BotCommand("status", "Статус треда"),
    ])
    log.info("Бот запущен: @%s", (await app.bot.get_me()).username)

def main():
    if "YOUR" in TELEGRAM_BOT_TOKEN:
        print("❌ Вставь TELEGRAM_BOT_TOKEN в начало bot.py")
        return
    if "YOUR" in CAPY_API_TOKEN:
        print("❌ Вставь CAPY_API_TOKEN в начало bot.py")
        return

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("new", cmd_new))
    app.add_handler(CommandHandler("model", cmd_model))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND | filters.Document.ALL | filters.PHOTO
        | filters.AUDIO | filters.VIDEO | filters.VOICE | filters.VIDEO_NOTE | filters.Sticker.ALL,
        handle,
    ))
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
