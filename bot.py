import asyncio
import io
import logging
import os
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from telegram import (
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

import db
from scraper import run_scrape, ScraperError

load_dotenv("settings.env")

BOT_TOKEN = os.environ["BOT_TOKEN"]
ADMIN_USER_ID = int(os.environ["ADMIN_USER_ID"])
DEFAULT_INTERVAL = int(os.getenv("INTERVAL_MINUTES", "60"))

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger(__name__)

# ConversationHandler state
WAIT_INTERVAL = 1

MAIN_KEYBOARD = ReplyKeyboardMarkup(
    [["📸 Скриншот", "⏱ Время обновления"], ["❓ Help"]],
    resize_keyboard=True,
    is_persistent=True,
)

HELP_TEXT = (
    "📖 <b>HTX New Earn Alarm — справка</b>\n"
    "\n"
    "Бот автоматически мониторит раздел <b>HTX Earn → New</b> и отслеживает "
    "появление, исчезновение и изменение токенов с типом <b>Fixed</b>.\n"
    "\n"
    "<b>Как работает цикл проверки:</b>\n"
    "1. Открывает страницу HTX Earn через браузер (Playwright)\n"
    "2. Дважды нажимает <i>View More</i>, ожидая расширения страницы после каждого клика\n"
    "3. Делает полный скриншот страницы\n"
    "4. Извлекает из DOM таблицу токенов — только строки с типом <b>Fixed</b>\n"
    "5. Сравнивает с данными предыдущего цикла\n"
    "6. Если есть изменения — присылает алерт с указанием новых, пропавших и изменившихся строк\n"
    "7. Все данные сохраняются в базу данных SQLite\n"
    "\n"
    "<b>Кнопки управления:</b>\n"
    "📸 <b>Скриншот</b> — немедленно запустить полный цикл, получить скриншот страницы "
    "и текущую таблицу Fixed-токенов. Следующий автоматический цикл будет через заданный интервал.\n"
    "\n"
    "⏱ <b>Время обновления</b> — изменить интервал автоматической проверки. "
    "После ввода числа (минуты, 10–1440) следующий цикл будет запланирован через это время.\n"
    "\n"
    "❓ <b>Help</b> — показать эту справку.\n"
    "\n"
    "<b>Алерт об изменениях выглядит так:</b>\n"
    "<pre>"
    "🟢 НОВОЕ:      USDT  12.5%  Fixed  30d\n"
    "🔴 ПРОПАЛО:    BTC    8.0%  Fixed   7d\n"
    "🔄 ИЗМЕНИЛОСЬ: ETH  6.0%→7.5%  Fixed  14d"
    "</pre>\n"
    "\n"
    "<b>Команды:</b>\n"
    "/start — статус бота и клавиатура\n"
    "/help — эта справка\n"
    "/cancel — отменить ввод интервала"
)

# Global scheduler task reference and scrape lock
_scheduler_task: asyncio.Task | None = None
_scrape_lock = asyncio.Lock()


# ──────────────────────────────────────────────────────────────────────────────
# Admin guard
# ──────────────────────────────────────────────────────────────────────────────

def is_admin(update: Update) -> bool:
    return update.effective_user.id == ADMIN_USER_ID


async def admin_only(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    if not is_admin(update):
        await update.message.reply_text("⛔ Доступ запрещён.")
        return False
    return True


# ──────────────────────────────────────────────────────────────────────────────
# Core cycle logic
# ──────────────────────────────────────────────────────────────────────────────

async def run_cycle(app: Application, send_screenshot: bool = False):
    """Run full scrape cycle. Sends alerts on changes (always). Screenshot only if requested."""
    if _scrape_lock.locked():
        logger.warning("Scrape already in progress, skipping")
        if send_screenshot:
            await app.bot.send_message(ADMIN_USER_ID, "⏳ Парсинг уже выполняется, подождите.")
        return
    async with _scrape_lock:
        await _run_cycle_inner(app, send_screenshot)


async def _run_cycle_inner(app: Application, send_screenshot: bool = False):
    logger.info("Starting scrape cycle (send_screenshot=%s)", send_screenshot)
    try:
        screenshot_bytes, rows = await run_scrape()
    except ScraperError as e:
        logger.error("ScraperError: %s", e)
        await app.bot.send_message(
            ADMIN_USER_ID,
            f"❌ <b>Ошибка парсинга HTX Earn:</b>\n{e}",
            parse_mode=ParseMode.HTML,
        )
        return
    except Exception as e:
        logger.exception("Unexpected scraper error")
        await app.bot.send_message(
            ADMIN_USER_ID,
            f"❌ <b>Неожиданная ошибка:</b>\n{e}",
            parse_mode=ParseMode.HTML,
        )
        return

    old_rows = await db.get_last_cycle_rows()
    await db.save_cycle(rows)

    # Send screenshot if explicitly requested
    if send_screenshot:
        caption = db.format_table(rows)
        if len(screenshot_bytes) > 9 * 1024 * 1024:
            await app.bot.send_document(
                ADMIN_USER_ID,
                document=io.BytesIO(screenshot_bytes),
                filename="htx_earn.png",
                caption=caption,
                parse_mode=ParseMode.HTML,
            )
        else:
            await app.bot.send_photo(
                ADMIN_USER_ID,
                photo=io.BytesIO(screenshot_bytes),
                caption=caption,
                parse_mode=ParseMode.HTML,
            )

    # Always diff and alert on changes
    if old_rows:
        added, removed, changed = db.compute_diff(old_rows, rows)
        if added or removed or changed:
            diff_text = db.format_diff(added, removed, changed)
            await app.bot.send_message(
                ADMIN_USER_ID,
                diff_text,
                parse_mode=ParseMode.HTML,
            )
    else:
        logger.info("No previous data — skipping diff on first cycle")

    logger.info("Cycle done. Rows saved: %d", len(rows))


# ──────────────────────────────────────────────────────────────────────────────
# Scheduler
# ──────────────────────────────────────────────────────────────────────────────

async def scheduler_loop(app: Application):
    """Runs scheduled cycles based on next_run_at from DB."""
    while True:
        try:
            next_run_str = await db.get_setting("next_run_at")
            if next_run_str:
                next_run = datetime.fromisoformat(next_run_str)
                if next_run.tzinfo is None:
                    next_run = next_run.replace(tzinfo=timezone.utc)
                delay = (next_run - datetime.now(timezone.utc)).total_seconds()
            else:
                delay = 0

            if delay > 0:
                logger.info("Next scheduled run in %.0f seconds", delay)
                await asyncio.sleep(delay)
            else:
                logger.info("Running scheduled cycle now")

            interval = int(await db.get_setting("interval_minutes", str(DEFAULT_INTERVAL)))
            await run_cycle(app)
            await db.schedule_next_run(interval)

        except asyncio.CancelledError:
            logger.info("Scheduler cancelled")
            break
        except Exception as e:
            logger.exception("Scheduler error: %s", e)
            await asyncio.sleep(60)


def restart_scheduler(app: Application):
    global _scheduler_task
    if _scheduler_task and not _scheduler_task.done():
        _scheduler_task.cancel()
    _scheduler_task = asyncio.create_task(scheduler_loop(app))
    logger.info("Scheduler (re)started")


# ──────────────────────────────────────────────────────────────────────────────
# Handlers
# ──────────────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update, context):
        return
    interval = int(await db.get_setting("interval_minutes", str(DEFAULT_INTERVAL)))
    next_run_str = await db.get_setting("next_run_at")
    next_info = ""
    if next_run_str:
        next_run = datetime.fromisoformat(next_run_str)
        if next_run.tzinfo is None:
            next_run = next_run.replace(tzinfo=timezone.utc)
        secs = int((next_run - datetime.now(timezone.utc)).total_seconds())
        if secs > 0:
            m, s = divmod(secs, 60)
            next_info = f"\n⏰ Следующий запуск через: {m}м {s}с"

    await update.message.reply_text(
        f"✅ <b>HTX New Earn Alarm</b>\n\n"
        f"Интервал: <b>{interval} мин</b>{next_info}\n\n"
        f"Кнопки управления:",
        reply_markup=MAIN_KEYBOARD,
        parse_mode=ParseMode.HTML,
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update, context):
        return
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)


async def btn_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update, context):
        return
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)


async def btn_screenshot(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update, context):
        return
    msg = await update.message.reply_text("⏳ Запускаю парсинг, подождите...")
    interval = int(await db.get_setting("interval_minutes", str(DEFAULT_INTERVAL)))

    # Reset timer before running
    await db.schedule_next_run(interval)
    restart_scheduler(context.application)

    await run_cycle(context.application, send_screenshot=True)
    await msg.delete()


async def btn_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await admin_only(update, context):
        return ConversationHandler.END
    interval = await db.get_setting("interval_minutes", str(DEFAULT_INTERVAL))
    await update.message.reply_text(
        f"⏱ Текущий интервал: <b>{interval} мин</b>\n\n"
        f"Введите новый интервал в минутах (10–1440):",
        parse_mode=ParseMode.HTML,
    )
    return WAIT_INTERVAL


async def receive_interval(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return ConversationHandler.END
    text = update.message.text.strip()
    try:
        minutes = int(text)
        if not (10 <= minutes <= 1440):
            raise ValueError
    except ValueError:
        await update.message.reply_text(
            "❌ Неверное значение. Введите число от 10 до 1440 (или /cancel):"
        )
        return WAIT_INTERVAL

    await db.set_setting("interval_minutes", str(minutes))
    await db.schedule_next_run(minutes)
    restart_scheduler(context.application)

    await update.message.reply_text(
        f"✅ Интервал установлен: <b>{minutes} мин</b>\n"
        f"Следующий запуск через {minutes} мин.",
        reply_markup=MAIN_KEYBOARD,
        parse_mode=ParseMode.HTML,
    )
    return ConversationHandler.END


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Отменено.", reply_markup=MAIN_KEYBOARD)
    return ConversationHandler.END


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

async def post_init(app: Application):
    await db.init_db()
    interval = int(await db.get_setting("interval_minutes", str(DEFAULT_INTERVAL)))
    await db.set_setting("interval_minutes", str(interval))

    # Always schedule first run after full interval (never run immediately on start)
    await db.schedule_next_run(interval)
    restart_scheduler(app)

    # Send startup notification
    next_run = datetime.now(timezone.utc) + timedelta(minutes=interval)
    next_run_local = next_run.strftime("%H:%M UTC")
    await app.bot.send_message(
        ADMIN_USER_ID,
        f"🟢 <b>HTX New Earn Alarm запущен</b>\n\n"
        f"Интервал проверки: <b>{interval} мин</b>\n"
        f"Первый цикл: <b>{next_run_local}</b>",
        parse_mode=ParseMode.HTML,
    )
    logger.info("Bot initialized. Interval=%d min. First run at %s", interval, next_run_local)


def main():
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    interval_conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Text(["⏱ Время обновления"]), btn_interval)
        ],
        states={
            WAIT_INTERVAL: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, receive_interval)
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_cancel)],
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(MessageHandler(filters.Text(["📸 Скриншот"]), btn_screenshot))
    app.add_handler(MessageHandler(filters.Text(["❓ Help"]), btn_help))
    app.add_handler(interval_conv)

    logger.info("Starting bot polling...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
