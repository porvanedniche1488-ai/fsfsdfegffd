# -*- coding: utf-8 -*-
"""
Telegram-бот: сигнали Binance Futures + платна підписка в Telegram Stars.

Команди користувача:
  /start  - привітання
  /help   - довідка
  /buy    - купити пропуск (оплата зірками Telegram)
  /status - статус підписки
  /signal - поточний топ-сигнал (для активних)

Команди адміна (ADMIN_ID):
  /setprice <зірки>     - ціна пропуску
  /setdays <днів>       - тривалість пропуску
  /grant <uid> [днів]   - видач/подовження доступу вручну
  /revoke <uid>         - зняти доступ
  /broadcast <текст>    - розсилка всім активним
  /stats                - статистика
  /scan                 - примусовий скан ринку + розсилка
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, LabeledPrice,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, ContextTypes, MessageHandler, filters,
    CallbackQueryHandler, PreCheckoutQueryHandler,
)

import config
import database as db

db.init_db()  # гарантуємо наявність таблиць до побудови HELP_TEXT

from analyzer import Analyzer, format_signal

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("bot")

# глобальні посилання — щоб scheduler міг надсилати повідомлення
_app: Application | None = None
_scan_lock = asyncio.Lock()   # захист від паралельних сканів (/scan під час cron)


async def new_analyzer() -> Analyzer:
    """Кожен скан отримує свіжий http-клієнт і закриває його після роботи.
    Це усуває помилки 'client closed' / завислі з'єднання після тривалої роботи."""
    return Analyzer(httpx.AsyncClient(timeout=20))

HELP_TEXT = (
    "🤖 <b>Binance Signals Bot</b>\n\n"
    "<b>Що робить:</b> аналізує ринок Binance Futures і надсилає готові угоди:\n"
    "🟢 ДОВГИЙ/КОРОТКИЙ ЛОНГ, 🔴 ДОВГИЙ/КОРОТКИЙ ШОРТ\n"
    "У кожній угоді: монета, ціна входу, стоп-лос, 2 тейк-профіти та оцінка шансу.\n"
    f"Раз на {{interval}} хв — до {{maxsig}} різних монет прямо в чат. Тільки за підпискою.\n\n"
    "<b>Команди:</b>\n"
    "/buy — купити пропуск (⭐️ Telegram Stars)\n"
    "/status — мій доступ\n"
    "/signal — ТОП угод просто зараз (потрібна підписка)\n"
    "/help — ця довідка\n\n"
    "<b>Оплата:</b> ⭐️ {price} за {days} днів. Без очікування — оплата через Telegram "
    "і доступ одразу."
)  # ціна/дні підставляються динамічно при кожній відправці


async def is_admin(uid: int) -> bool:
    return uid == config.ADMIN_ID


async def has_access(uid: int) -> bool:
    """Адмін — завжди безкоштовно; всі решта — тільки з активною підпискою."""
    return uid == config.ADMIN_ID or db.is_active(uid)


# ---------------- user commands ----------------

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    db.register_user(u.id, u.username, u.first_name)
    price = db.get_price()
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⭐️ Купити пропуск", callback_data="buy")]])
    if u.id == config.ADMIN_ID:
        await update.message.reply_text(
            f"Привіт, {u.first_name}! 👑 Ти адмін — доступ безкоштовний і назавжди.\n"
            f"Сигнали (до {config.MAX_SIGNALS_PER_SCAN} монет за скан) приходитимуть раз на "
            f"{config.SCAN_INTERVAL_MIN} хв. Адмін-команди: /help",
        )
    elif db.is_active(u.id):
        left = db.sub_info(u.id)
        await update.message.reply_text(
            f"Привіт, {u.first_name}! ✅ Твій доступ активний ще {left/86400:.1f} дн. "
            f"Сигнали приходитимуть автоматично раз на {config.SCAN_INTERVAL_MIN} хв.",
            reply_markup=kb,
        )
    else:
        await update.message.reply_text(
            f"Привіт, {u.first_name}! 👋\n\n"
            f"Я даю готові угоди по Binance Futures: лонг/шорт, стоп-лос, 2 тейки, шанс.\n"
            f"Раз на {config.SCAN_INTERVAL_MIN} хвилин — до {config.MAX_SIGNALS_PER_SCAN} найкращих угод прямо в чат.\n"
            f"⛔️ Сигнали надсилаються ТІЛЬКИ тим, хто купив пропуск.\n\n"
            f"🎫 Пропуск: ⭐️{price} на {db.get_sub_days()} днів.",
            reply_markup=kb,
        )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # текст формуємо заново — щоб ціна/дні завжди були актуальні після /setprice
    text = HELP_TEXT.format(interval=config.SCAN_INTERVAL_MIN,
                            maxsig=config.MAX_SIGNALS_PER_SCAN,
                            price=db.get_price(), days=db.get_sub_days())
    if update.effective_user.id == config.ADMIN_ID:
        text += ("\n\n👑 <b>Адмін-команди:</b>\n"
                 "/setprice 50 — ціна в зірках\n"
                 "/setdays 30 — тривалість пропуску\n"
                 "/setmax 8 — скільки монет у розсилці\n"
                 "/setminconf 65 — мінімальний шанс %\n"
                 "/grant uid [днів] · /revoke uid\n"
                 "/scan — терміновий скан + розсилка\n"
                 "/broadcast текст · /stats")
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if u.id == config.ADMIN_ID:
        await update.message.reply_text("👑 Ти адмін — безкоштовний доступ назавжди.")
    elif db.is_active(u.id):
        left = db.sub_info(u.id)
        await update.message.reply_text(f"✅ Доступ активний. Залишилось: {left/86400:.1f} дн.")
    else:
        await update.message.reply_text(
            f"❌ Пропуску немає. /buy — коштує ⭐️{db.get_price()} на {db.get_sub_days()} днів."
        )


async def cb_buy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Кнопка 'купити' — кидаємо invoice зі сплатою зірками."""
    q = update.callback_query
    await q.answer()
    price = db.get_price()
    await ctx.bot.send_invoice(
        chat_id=q.from_user.id,
        title=f"Пропуск на {db.get_sub_days()} днів",
        description="Доступ до сигналів Binance Futures: лонг/шорт зі стопами та тейками.",
        payload=f"sub_{q.from_user.id}_{int(time.time())}",
        provider_token="",           # порожньо для Telegram Stars
        currency="XTR",              # XTR = Telegram Stars
        prices=[LabeledPrice(label="Пропуск", amount=price)],
    )


async def cmd_buy(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    price = db.get_price()
    await ctx.bot.send_invoice(
        chat_id=update.effective_chat.id,
        title=f"Пропуск на {db.get_sub_days()} днів",
        description="Доступ до сигналів Binance Futures.",
        payload=f"sub_{update.effective_user.id}_{int(time.time())}",
        provider_token="",
        currency="XTR",
        prices=[LabeledPrice(label="Пропуск", amount=price)],
    )


async def on_precheckout(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)


async def on_successful_payment(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    sp = update.message.successful_payment
    uid = update.effective_user.id
    db.add_sub(uid, db.get_sub_days())
    log.info("Payment success uid=%s stars=%s", uid, sp.total_amount)
    await update.message.reply_text(
        f"🎉 Оплачено! Пропуск активовано на {db.get_sub_days()} днів.\n"
        f"Перший сигнал — найближчим часом, далі раз на {config.SCAN_INTERVAL_MIN} хв."
    )


async def cmd_signal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Ручний запит сигналів. Тільки адмін (безкоштовно) або платні підписники.
    Повертає до 5 різних монет — не одну."""
    u = update.effective_user
    if not await has_access(u.id):
        await update.message.reply_text(
            f"🔒 Сигнали доступні тільки за підпискою.\n/buy — ⭐️{db.get_price()} на {db.get_sub_days()} днів."
        )
        return
    async with _scan_lock:
        msg = await update.message.reply_text("⏳ Сканую ринок…")
        analyzer = await new_analyzer()
        try:
            sigs = await analyzer.scan_market(config.TOP_N_SYMBOLS, min_confidence=config.MIN_CONFIDENCE)
        except Exception as e:
            log.error("manual scan failed: %s", e)
            await msg.edit_text("⚠️ Binance зараз не відповідає. Спробуй через хвилину.")
            return
        finally:
            await analyzer.client.aclose()
        if not sigs:
            await msg.edit_text("Зараз сильних сетапів немає. Чекай наступного скану або /signal пізніше.")
            return
        tag = " 👑(адмін, безкоштовно)" if u.id == config.ADMIN_ID else ""
        await msg.edit_text(f"📊 ТОП-{min(len(sigs), 5)} угод просто зараз{tag}:")
        for s in sigs[:5]:
            await asyncio.sleep(0.1)
            await ctx.bot.send_message(u.id, format_signal(s))


async def cmd_signals(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Те саме, що /signal — псевдонім."""
    await cmd_signal(update, ctx)


# ---------------- admin commands ----------------

async def cmd_setprice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return
    try:
        val = int(ctx.args[0])
        assert 1 <= val <= 100000
    except Exception:
        await update.message.reply_text("Формат: /setprice 50  (ціна в зірках)")
        return
    db.set_price(val)
    await update.message.reply_text(f"💰 Нова ціна пропуску: ⭐️{val}")


async def cmd_setdays(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return
    try:
        val = int(ctx.args[0])
        assert 1 <= val <= 3650
    except Exception:
        await update.message.reply_text("Формат: /setdays 30")
        return
    db.set_sub_days(val)
    await update.message.reply_text(f"📅 Тривалість пропуску: {val} днів")


async def cmd_grant(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return
    try:
        uid = int(ctx.args[0])
        days = int(ctx.args[1]) if len(ctx.args) > 1 else db.get_sub_days()
    except Exception:
        await update.message.reply_text("Формат: /grant 123456789 [днів]")
        return
    db.add_sub(uid, days)
    await update.message.reply_text(f"✅ Видано {days} дн. користувачу {uid}")


async def cmd_revoke(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return
    try:
        uid = int(ctx.args[0])
    except Exception:
        await update.message.reply_text("Формат: /revoke 123456789")
        return
    with db.db() as c:
        c.execute("DELETE FROM subs WHERE uid=?", (uid,))
    await update.message.reply_text(f"🚫 Доступ знято для {uid}")


async def cmd_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return
    text = " ".join(ctx.args)
    if not text:
        await update.message.reply_text("Формат: /broadcast Привіт усім!")
        return
    users = db.active_users()
    sent, failed = 0, 0
    for uid in users:
        try:
            await ctx.bot.send_message(uid, text)
            sent += 1
        except Exception:
            failed += 1
    await update.message.reply_text(f"📣 Розіслано: {sent}, помилок: {failed}")


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return
    with db.db() as c:
        total = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        active = len(db.active_users())
    await update.message.reply_text(
        f"📊 Всього юзерів: {total}\n✅ З доступом: {active}\n💰 Ціна: ⭐️{db.get_price()} / {db.get_sub_days()} дн."
    )


async def cmd_scan(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await is_admin(update.effective_user.id):
        return
    await update.message.reply_text("🔍 Запускаю скан…")
    # не чекаємо — скан у фоні, інакше Telegram показує «бот не відповідає»
    asyncio.create_task(run_scan_and_broadcast(source="admin/scan"))


async def cmd_setmax(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Адмін: скільки монет розсилати за один скан."""
    if not await is_admin(update.effective_user.id):
        return
    try:
        val = int(ctx.args[0])
        assert 1 <= val <= 20
    except Exception:
        await update.message.reply_text(f"Формат: /setmax 8 (зараз {config.MAX_SIGNALS_PER_SCAN})")
        return
    config.MAX_SIGNALS_PER_SCAN = val
    await update.message.reply_text(f"⚙️ Максимум сигналів за скан: {val} монет")


async def cmd_setminconf(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Адмін: мінімальний шанс %, нижчий сигнал не надсилається."""
    if not await is_admin(update.effective_user.id):
        return
    try:
        val = int(ctx.args[0])
        assert 50 <= val <= 95
    except Exception:
        await update.message.reply_text(f"Формат: /setminconf 65 (зараз {config.MIN_CONFIDENCE}%)")
        return
    config.MIN_CONFIDENCE = val
    await update.message.reply_text(f"⚙️ Мінімальний шанс сигналу: {val}%")


# ---------------- scheduler: signals to subscribers ----------------

async def post_init(app):
    """Ініціалізація після старту: планувальник скану ринку + health-сервер."""
    global _app
    # у python-telegram-bot v21+ post_init може отримати ApplicationBuilder, а не Application
    application = getattr(app, "application", None) or app
    _app = application
    sched = AsyncIOScheduler(timezone="UTC")
    sched.add_job(scan_job, trigger="interval", minutes=config.SCAN_INTERVAL_MIN)
    sched.start()
    log.info("Scheduler started every %d min", config.SCAN_INTERVAL_MIN)
    try:
        await start_health_server()
    except Exception as e:
        log.warning("Health server failed to start: %s", e)


async def run_scan_and_broadcast(source: str = "cron"):
    """Скан ринку -> список різних монет (до MAX_SIGNALS_PER_SCAN) -> розсилка.
    Адмін (ADMIN_ID) отримує безкоштовно; всі решта — тільки платні підписники."""
    if _app is None:
        return
    async with _scan_lock:  # одночасно лише один скан (cron + /scan не збиваються)
        analyzer = await new_analyzer()
        try:
            sigs = await analyzer.scan_market(config.TOP_N_SYMBOLS,
                                              min_confidence=config.MIN_CONFIDENCE)
        except Exception as e:
            log.error("[%s] scan failed: %s", source, e)
            await analyzer.client.aclose()
            return
        finally:
            pass
        await analyzer.client.aclose()

        if not sigs:
            log.info("[%s] no signals this round", source)
            return

        sigs = sigs[:config.MAX_SIGNALS_PER_SCAN]

        # кому надсилати: платні підписники + адмін (безкоштовно)
        recipients = set(db.active_users())
        recipients.add(config.ADMIN_ID)
        log.info("[%s] broadcasting %d different coin(s) to %d user(s)",
                 source, len(sigs), len(recipients))

        for uid in recipients:
            header = f"📡 Сигнали {time.strftime('%d.%m %H:%M UTC')} • {len(sigs)} шт."
            if uid == config.ADMIN_ID:
                header += " 👑(адмін — безкоштовно)"
            try:
                await _app.bot.send_message(uid, header)
            except Exception as e:
                log.warning("[%s] send header to %s failed: %s", source, uid, e)
                continue
            for s in sigs:
                try:
                    await _app.bot.send_message(uid, format_signal(s))
                    await asyncio.sleep(0.15)  # антифлуд
                except Exception as e:
                    log.warning("[%s] send to %s failed: %s", source, uid, e)


async def scan_job():
    """Періодичне завдання (раз на SCAN_INTERVAL_MIN хв)."""
    await run_scan_and_broadcast("cron")


async def err_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    log.error("Update error: %s", ctx.error)


async def start_health_server():
    """Мікро HTTP-сервер з /health — щоб Railway бачив, що бот живий,
    і не вбивав процес (підмінна змінна PORT задається автоматично)."""
    from aiohttp import web

    async def health(request):
        return web.json_response({"status": "ok", "bot": "binance-signals"})

    app = web.Application()
    app.router.add_get("/health", health)
    app.router.add_get("/", health)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", int(os.environ.get("PORT", "8080")))
    await site.start()
    log.info("Health server on port %s", os.environ.get("PORT", "8080"))


def main():
    db.init_db()
    app = Application.builder().token(config.BOT_TOKEN).build()
    app.post_init = post_init

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("buy", cmd_buy))
    app.add_handler(CommandHandler("signal", cmd_signal))
    app.add_handler(CommandHandler("signals", cmd_signals))
    app.add_handler(CallbackQueryHandler(cb_buy, pattern="^buy$"))

    # admin
    app.add_handler(CommandHandler("setprice", cmd_setprice))
    app.add_handler(CommandHandler("setdays", cmd_setdays))
    app.add_handler(CommandHandler("setmax", cmd_setmax))
    app.add_handler(CommandHandler("setminconf", cmd_setminconf))
    app.add_handler(CommandHandler("grant", cmd_grant))
    app.add_handler(CommandHandler("revoke", cmd_revoke))
    app.add_handler(CommandHandler("broadcast", cmd_broadcast))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("scan", cmd_scan))

    # payments
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, on_successful_payment))
    from telegram.ext import PreCheckoutQueryHandler
    app.add_handler(PreCheckoutQueryHandler(on_precheckout))

    app.add_error_handler(err_handler)

    print("Bot is running… Ctrl+C to stop.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
