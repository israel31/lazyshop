"""
VisualSearch AI Telegram Bot

Lets users send a product image or image URL to find matching
products across active connectors such as Jumia.
"""

import logging
import os
from html import escape

import httpx
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
BACKEND_URL = os.getenv("BACKEND_URL")
BACKEND_HOSTPORT = os.getenv("BACKEND_HOSTPORT")

if not BACKEND_URL:
    if BACKEND_HOSTPORT:
        BACKEND_URL = f"http://{BACKEND_HOSTPORT}"
    else:
        BACKEND_URL = "http://127.0.0.1:8000"

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN in .env")

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

IMAGE_EXTENSIONS = (".jpg", ".jpeg", ".png", ".webp", ".gif")


async def call_search_api(image_url: str) -> dict:
    async with httpx.AsyncClient(timeout=60.0) as client:
        response = await client.post(
            f"{BACKEND_URL}/search",
            json={"image_url": image_url},
        )
        response.raise_for_status()
        return response.json()


async def get_telegram_file_url(
    context: ContextTypes.DEFAULT_TYPE,
    file_id: str,
) -> str:
    file = await context.bot.get_file(file_id)
    return file.file_path


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


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{BACKEND_URL}/connectors")
            response.raise_for_status()
            connectors = response.json()
    except Exception:
        await update.message.reply_text("Could not reach the backend. Is it running?")
        return

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
        data = await call_search_api(image_url)
        logger.info("Backend returned %s result(s) for photo search", len(data.get("results", [])))
        text, keyboard = format_results(data)
        await msg.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    except httpx.ConnectError:
        await msg.edit_text(
            f"<b>Backend not reachable.</b>\nMake sure your FastAPI server is running at {escape(BACKEND_URL)}",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        logger.exception("Search error during photo handling")
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
        data = await call_search_api(text)
        logger.info("Backend returned %s result(s) for URL search", len(data.get("results", [])))
        result_text, keyboard = format_results(data)
        await msg.edit_text(result_text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    except httpx.ConnectError:
        await msg.edit_text(
            f"<b>Backend not reachable.</b>\nMake sure your FastAPI server is running at {escape(BACKEND_URL)}",
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        logger.exception("Search error during URL handling")
        await msg.edit_text("Something went wrong. Please try again.")


def main() -> None:
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("stores", stores_command))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Bot is running. Press Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
