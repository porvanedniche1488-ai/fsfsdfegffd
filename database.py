# -*- coding: utf-8 -*-
"""База даних: підписки, налаштування, користувачі."""
import os
import sqlite3
import time
from contextlib import contextmanager

import config


def _ensure_db_dir():
    """На Railway файловая система ephemeral — кладём БД в постоянный volume,
    если он примонтирован (DB_PATH указывает внутрь /data или иного volume)."""
    d = os.path.dirname(config.DB_PATH)
    if d:
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass


@contextmanager
def db():
    _ensure_db_dir()
    conn = sqlite3.connect(config.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
            uid INTEGER PRIMARY KEY,
            username TEXT,
            first_name TEXT,
            joined_ts REAL
        );
        CREATE TABLE IF NOT EXISTS subs(
            uid INTEGER PRIMARY KEY,
            expires_ts REAL
        );
        CREATE TABLE IF NOT EXISTS settings(
            key TEXT PRIMARY KEY,
            value TEXT
        );
        """)
        # значення за замовчуванням
        row = c.execute("SELECT value FROM settings WHERE key='price_stars'").fetchone()
        if not row:
            c.execute("INSERT INTO settings(key,value) VALUES('price_stars',?)",
                      (str(config.DEFAULT_PRICE_STARS),))


def get_price() -> int:
    with db() as c:
        row = c.execute("SELECT value FROM settings WHERE key='price_stars'").fetchone()
        return int(row["value"]) if row else config.DEFAULT_PRICE_STARS


def set_price(stars: int):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('price_stars',?)",
                  (str(int(stars)),))


def get_sub_days() -> int:
    with db() as c:
        row = c.execute("SELECT value FROM settings WHERE key='sub_days'").fetchone()
        return int(row["value"]) if row else config.SUB_DAYS


def set_sub_days(days: int):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO settings(key,value) VALUES('sub_days',?)",
                  (str(int(days)),))


def register_user(uid: int, username: str | None, first_name: str | None):
    with db() as c:
        c.execute("INSERT OR IGNORE INTO users(uid,username,first_name,joined_ts) VALUES(?,?,?,?)",
                  (uid, username, first_name, time.time()))


def all_users() -> list[int]:
    with db() as c:
        return [r["uid"] for r in c.execute("SELECT uid FROM users")]


def add_sub(uid: int, days: int):
    now = time.time()
    with db() as c:
        row = c.execute("SELECT expires_ts FROM subs WHERE uid=?", (uid,)).fetchone()
        base = max(now, row["expires_ts"]) if row else now
        c.execute("INSERT OR REPLACE INTO subs(uid,expires_ts) VALUES(?,?)",
                  (uid, base + days * 86400))


def is_active(uid: int) -> bool:
    with db() as c:
        row = c.execute("SELECT expires_ts FROM subs WHERE uid=?", (uid,)).fetchone()
        return bool(row and row["expires_ts"] > time.time())


def active_users() -> list[int]:
    now = time.time()
    with db() as c:
        return [r["uid"] for r in c.execute("SELECT uid FROM subs WHERE expires_ts>?", (now,))]


def sub_info(uid: int):
    with db() as c:
        row = c.execute("SELECT expires_ts FROM subs WHERE uid=?", (uid,)).fetchone()
    if not row:
        return None
    left = row["expires_ts"] - time.time()
    return max(0.0, left)
