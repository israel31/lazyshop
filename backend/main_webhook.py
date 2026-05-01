"""
FastAPI entrypoint that adds a Telegram webhook bot on top of the existing
search backend in main.py.

Use this file when you want one deployed web service to handle both:
1. the product-search API
2. Telegram webhook updates
"""

import logging
import os
from html import escape

from fastapi import HTTPException, Request
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from main import SearchRequest, app, execute_search, list_connectors

logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_WEBHOOK_URL = os.getenv("TELEGRAM_WEBHOOK_URL")
TELEGRAM_WEBHOOK_SECRET = os.getenv("TELEGRAM_WEBHOOK_SECRET")

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif")

telegram_app: Application | None = None


def _bot_enabled() -> bool:
    return bool(TELEGRAM_BOT_TOKEN)


def format_results(data: dict) -> tuple[str, InlineKeyboardMarkup]:
    identified = escape(data.get("identified_as", "Unknown product"))
    results = data.get("results", [])
    backend_error = data.get("error")

    if not results:
        lines = [
            f"<b>AI identified:</b> {identified}",
            "",
            "No products found across active stores.",
            "Try a clearer image or a different product.",
        ]
        if backend_error:
            lines.extend(["", f"<i>{escape(str(backend_error))}</i>"])
        return "\n".join(lines), InlineKeyboardMarkup([])

    lines = [
        f"<b>AI identified:</b> {identified}",
        f"Found <b>{len(results)}</b> result(s).",
        "",
    ]

    buttons: list[list[InlineKeyboardButton]] = []
    for index, item in enumerate(results[:8], start=1):
        title = escape(str(item.get("title", "No title"))[:60])
        price = escape(str(item.get("price", "N/A")))
        source = escape(str(item.get("source", "Store")))
        link = str(item.get("link", "#"))

        lines.append(f"<b>{index}. {title}</b>")
        lines.append(f"Price: {price} | Store: {source}")
        lines.append("")

        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"{index}. Buy on {item.get('source', 'Store')} - {item.get('price', 'N/A')}",
                    url=link,
                )
            ]
        )

    return "\n".join(lines).strip(), InlineKeyboardMarkup(buttons)


async def run_search(image_url: str) -> dict:
    return await execute_search(SearchRequest(image_url=image_url))


async def get_telegram_file_url(
    context: ContextTypes.DEFAULT_TYPE,
    file_id: str,
) -> str:
    file = await context.bot.get_file(file_id)
    return file.file_path


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    welcome = (
        "<b>Welcome to VisualSearch AI</b>\n\n"
        "I can find products for you across Nigerian stores like Jumia.\n\n"
        "<b>How to use:</b>\n"
        "Send me a product photo\n"
        "Or paste a direct image URL\n\n"
        "I will identify the item and search for deals."
    )
    await update.message.reply_text(welcome, parse_mode=ParseMode.HTML)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    help_text = (
        "<b>VisualSearch AI Help</b>\n\n"
        "<b>Commands:</b>\n"
        "/start - Welcome message\n"
        "/help - This help text\n"
        "/stores - List active stores\n\n"
        "<b>Searching:</b>\n"
        "Send any product photo directly in chat\n"
        "Or send a message containing an image URL\n"
        "(must end in .jpg, .jpeg, .png, .webp, or .gif)\n\n"
        "<b>Tips:</b>\n"
        "Use clear, well-lit product photos\n"
        "Avoid cluttered backgrounds\n"
        "Brand shots from official sites work well"
    )
    await update.message.reply_text(help_text, parse_mode=ParseMode.HTML)


async def stores_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    connectors = await list_connectors()
    lines = ["<b>Active Stores</b>", ""]
    for connector in connectors:
        status = "ON" if connector["is_active"] else "OFF"
        lines.append(f"{status} - {escape(connector['name'])}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = await update.message.reply_text(
        "Got your image. Analyzing with AI...",
        parse_mode=ParseMode.HTML,
    )

    photo = update.message.photo[-1]
    image_url = await get_telegram_file_url(context, photo.file_id)

    await msg.edit_text("AI identified the product. Searching stores...")

    try:
        data = await run_search(image_url)
        logger.info("Webhook bot returned %s result(s) for photo search", len(data.get("results", [])))
        text, keyboard = format_results(data)
        await msg.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    except Exception:
        logger.exception("Search error during webhook photo handling")
        await msg.edit_text("Something went wrong. Please try again.")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()

    if not (text.startswith("http") and any(text.lower().endswith(ext) for ext in IMAGE_EXTENSIONS)):
        await update.message.reply_text(
            "Send me a product photo or paste a direct image URL to search.\n"
            "Type /help for instructions."
        )
        return

    msg = await update.message.reply_text("Fetching image from URL...")
    await msg.edit_text("Identifying product with AI...")

    try:
        data = await run_search(text)
        logger.info("Webhook bot returned %s result(s) for URL search", len(data.get("results", [])))
        result_text, keyboard = format_results(data)
        await msg.edit_text(result_text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    except Exception:
        logger.exception("Search error during webhook URL handling")
        await msg.edit_text("Something went wrong. Please try again.")


def _build_telegram_application() -> Application:
    bot_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    bot_app.add_handler(CommandHandler("start", start_command))
    bot_app.add_handler(CommandHandler("help", help_command))
    bot_app.add_handler(CommandHandler("stores", stores_command))
    bot_app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    return bot_app


async def _ensure_webhook() -> dict:
    if not telegram_app:
        return {"ok": False, "description": "Telegram bot is not configured"}
    if not TELEGRAM_WEBHOOK_URL:
        return {"ok": False, "description": "Missing TELEGRAM_WEBHOOK_URL"}

    webhook_kwargs = {"url": TELEGRAM_WEBHOOK_URL}
    if TELEGRAM_WEBHOOK_SECRET:
        webhook_kwargs["secret_token"] = TELEGRAM_WEBHOOK_SECRET

    ok = await telegram_app.bot.set_webhook(**webhook_kwargs)
    info = await telegram_app.bot.get_webhook_info()
    return {
        "ok": ok,
        "url": info.url,
        "pending_update_count": info.pending_update_count,
        "last_error_message": info.last_error_message,
    }


@app.on_event("startup")
async def startup_telegram_webhook() -> None:
    global telegram_app

    if not _bot_enabled():
        logger.info("Telegram webhook bot disabled: TELEGRAM_BOT_TOKEN is not set.")
        return

    telegram_app = _build_telegram_application()
    await telegram_app.initialize()
    await telegram_app.start()
    logger.info("Telegram webhook bot initialized.")

    if TELEGRAM_WEBHOOK_URL:
        result = await _ensure_webhook()
        logger.info("Webhook registration result: %s", result)
    else:
        logger.info("TELEGRAM_WEBHOOK_URL not set yet. Skipping automatic webhook registration.")


@app.on_event("shutdown")
async def shutdown_telegram_webhook() -> None:
    global telegram_app

    if not telegram_app:
        return

    await telegram_app.stop()
    await telegram_app.shutdown()
    telegram_app = None


@app.get("/telegram/status")
async def telegram_status() -> dict:
    enabled = _bot_enabled()
    data = {
        "enabled": enabled,
        "webhook_url_env": TELEGRAM_WEBHOOK_URL,
        "has_secret": bool(TELEGRAM_WEBHOOK_SECRET),
    }

    if telegram_app:
        info = await telegram_app.bot.get_webhook_info()
        data["webhook_info"] = {
            "url": info.url,
            "pending_update_count": info.pending_update_count,
            "last_error_message": info.last_error_message,
        }

    return data


@app.post("/telegram/set-webhook")
async def set_telegram_webhook() -> dict:
    result = await _ensure_webhook()
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result["description"])
    return result


@app.post("/telegram/delete-webhook")
async def delete_telegram_webhook() -> dict:
    if not telegram_app:
        raise HTTPException(status_code=400, detail="Telegram bot is not configured")

    ok = await telegram_app.bot.delete_webhook(drop_pending_updates=False)
    info = await telegram_app.bot.get_webhook_info()
    return {
        "ok": ok,
        "url": info.url,
        "pending_update_count": info.pending_update_count,
    }


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request) -> dict:
    if not telegram_app:
        raise HTTPException(status_code=503, detail="Telegram bot is not configured")

    if TELEGRAM_WEBHOOK_SECRET:
        header_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if header_secret != TELEGRAM_WEBHOOK_SECRET:
            raise HTTPException(status_code=403, detail="Invalid Telegram webhook secret")

    payload = await request.json()
    update = Update.de_json(payload, telegram_app.bot)
    await telegram_app.process_update(update)
    return {"ok": True}

