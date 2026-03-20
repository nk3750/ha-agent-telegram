import asyncio
import logging

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from langchain_core.messages import HumanMessage

from ha_agent.config import TELEGRAM_BOT_TOKEN, ALLOWED_CHAT_IDS
from ha_agent.agent import build_graph
from ha_agent.tools import set_notify_callback, _current_chat_id

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Per-chat conversation history
_chat_histories: dict[int, list] = {}

graph = build_graph()


async def handle_chatid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reply with the chat ID — use this to set up ALLOWED_CHAT_IDS."""
    chat_id = update.effective_chat.id
    await update.message.reply_text(
        f"Chat ID: `{chat_id}`\n\nAdd this to your `.env`:\n`ALLOWED_CHAT_IDS={chat_id}`",
        parse_mode="Markdown",
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    text = update.message.text

    logger.info(f"Message from chat_id: {chat_id}")

    if ALLOWED_CHAT_IDS and chat_id not in ALLOWED_CHAT_IDS:
        logger.warning(f"Unauthorized access attempt from chat_id: {chat_id}")
        await update.message.reply_text("Unauthorized.")
        return

    if chat_id not in _chat_histories:
        _chat_histories[chat_id] = []

    messages = _chat_histories[chat_id]
    messages.append(HumanMessage(content=text))

    # Set chat context so background tasks know where to send notifications
    _current_chat_id.set(chat_id)

    result = await asyncio.to_thread(graph.invoke, {"messages": messages})
    _chat_histories[chat_id] = result["messages"]

    response = result["messages"][-1].content
    try:
        await update.message.reply_text(response, parse_mode="HTML")
    except Exception:
        await update.message.reply_text(response)


async def post_init(application: Application):
    """Called after the bot is initialized — sets up the notification bridge."""
    loop = asyncio.get_running_loop()

    def notify(chat_id: int, message: str):
        async def _send():
            try:
                await application.bot.send_message(chat_id=chat_id, text=message, parse_mode="HTML")
            except Exception:
                await application.bot.send_message(chat_id=chat_id, text=message)

        asyncio.run_coroutine_threadsafe(_send(), loop)

    set_notify_callback(notify)
    logger.info("Notification callback registered")


def main():
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("chatid", handle_chatid))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("Home Assistant Telegram bot started!")
    app.run_polling()


if __name__ == "__main__":
    main()
