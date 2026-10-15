# -*- coding: utf-8 -*-
"""
Налаштування бота. Значення можна перевизначити через змінні оточення,
щоб не світити секрети в коді.
"""
import os

# Токен бота від @BotFather (BOT)
BOT_TOKEN = os.environ.get("BOT_TOKEN", "5994037577:AAFjj03rMKr31vhtB-b2zmF1Q1MZdSYRCag")

# Твій Telegram ID адміна. Дізнатися: напиши боту @userinfobot — він покаже ID.
ADMIN_ID = int(os.environ.get("ADMIN_ID", "1813590984"))

# Ціна пропуску в TELEGRAM STARS (зірках). Міняти можна командою /setprice (тільки адмін).
DEFAULT_PRICE_STARS = int(os.environ.get("PRICE_STARS", "50"))

# Тривалість підписки в днях
SUB_DAYS = int(os.environ.get("SUB_DAYS", "30"))

# Період аналізу ринку (хвилин). За замовчуванням — раз в 30 хв.
SCAN_INTERVAL_MIN = int(os.environ.get("SCAN_INTERVAL_MIN", "30"))

# Скільки монет аналізувати запуск (топ за об'ємом futures)
TOP_N_SYMBOLS = int(os.environ.get("TOP_N_SYMBOLS", "120"))

# Максимум сигналів в одній розсилці (найкращі за шансом)
MAX_SIGNALS_PER_SCAN = int(os.environ.get("MAX_SIGNALS_PER_SCAN", "8"))

# Мінімальний «шанс спрацювання» (%), нижчий сигнал не розсилається
MIN_CONFIDENCE = int(os.environ.get("MIN_CONFIDENCE", "65"))

# База SQLite (підписки, налаштування, історія сигналів)
DB_PATH = os.environ.get("DB_PATH", os.path.join(os.path.dirname(__file__), "bot.db"))
