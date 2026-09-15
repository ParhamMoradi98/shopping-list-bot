#!/usr/bin/env python3
"""
PanicCart — a shared Telegram bot for household shopping lists.

Items live in a *household*. Anyone you invite sees and edits the same cart.
Each item is tagged with a store (or a whole store group) and an emergency
score from 0–10, so you can ask “where should I go?” and get the panic-10s
with the grocery stores attached.

SETUP
  python3 -m venv .venv && source .venv/bin/activate
  pip install -r requirements.txt
  cp .env.example .env   # set TELEGRAM_BOT_TOKEN
  python shopping_bot.py

QUICK ADD
  milk | costco | 8
  sabzi | paeez | 10 | 2 bunches
  yogurt | persian | 9

  Fields: item | store | emergency | qty
  Only the name is required — the rest is buttons.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import logging
import os
import random
import re
import secrets
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Iterable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s | %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("paniccart")


def load_dotenv() -> None:
    path = Path(__file__).resolve().parent / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_dotenv()


def _default_db_path() -> str:
    if os.environ.get("SHOP_DB"):
        return os.environ["SHOP_DB"]
    if os.path.isdir("/data"):
        return "/data/shopping.db"
    return "shopping.db"


DB_PATH = _default_db_path()
DEFAULT_TZ = os.environ.get("SHOP_TZ", "America/Toronto")
DEFAULT_DIGEST_TIME = "08:30"

HTML = ParseMode.HTML
RULE = "━━━━━━━━━━━━━━━"
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
PAGE_SIZE = 8

# --------------------------------------------------------------------------
# catalogue — seeded per household, then fully editable
# --------------------------------------------------------------------------

DEFAULT_STORES = [
    {
        "name": "Costco",
        "emoji": "📦",
        "slug": "costco",
        "aliases": "costco,costco wholesale",
    },
    {
        "name": "PC stores",
        "emoji": "🍁",
        "slug": "pc",
        "aliases": "pc,pc store,pc stores,superstore,no frills,independent,valu-mart,valu mart",
    },
    {
        "name": "Shoppers",
        "emoji": "💊",
        "slug": "shoppers",
        "aliases": "shoppers,shoppers drug mart,sdm,pharmaprix",
    },
    {
        "name": "Persian food markets",
        "emoji": "🫒",
        "slug": "persian",
        "is_group": 1,
        "aliases": "persian,persian market,persian food,iranian,iranian market",
        "children": [
            {"name": "Paeez", "emoji": "🌿", "slug": "paeez", "aliases": "paeez,paeez market"},
            {"name": "Heeva", "emoji": "🛒", "slug": "heeva", "aliases": "heeva,heeva market"},
            {"name": "Arzon", "emoji": "💰", "slug": "arzon", "aliases": "arzon"},
            {"name": "Khorak", "emoji": "🍖", "slug": "khorak", "aliases": "khorak"},
            {"name": "Irooni market", "emoji": "🇮🇷", "slug": "irooni",
             "aliases": "irooni,irooni market,ironi,ironi market"},
            {"name": "Koorosh", "emoji": "🏪", "slug": "koorosh",
             "aliases": "koorosh,korosh,koroush,kourosh"},
        ],
    },
]

EMERGENCY = {
    0: ("🧊", "someday"),
    1: ("🌱", "low"),
    2: ("🌱", "low"),
    3: ("📌", "when convenient"),
    4: ("📌", "should grab"),
    5: ("📋", "this week"),
    6: ("📋", "this week"),
    7: ("⏰", "soon"),
    8: ("⏰", "soon"),
    9: ("🔥", "today-ish"),
    10: ("🚨", "PANIC"),
}

EMPTY_LINES = [
    "Cart is empty. That's either organisation or denial.",
    "Nothing on the list. Go touch grass. Or cheese.",
    "Quiet cart. Add milk before the universe notices.",
]

PANIC_FLAVOUR = [
    "These cannot wait:",
    "The fridge is making threats:",
    "Treat as a rescue mission:",
    "If you leave the house, grab these:",
]

ROLL_FLAVOUR = [
    "Grab this first:",
    "The cart has spoken:",
    "Highest drama item:",
    "Start here or suffer:",
]

GROCERY_EMOJI: list[tuple[tuple[str, ...], str]] = [
    (("milk", "yogurt", "yoghurt", "mast", "cheese", "butter", "cream", "doogh", "kefir"), "🥛"),
    (("egg",), "🥚"),
    (("bread", "sangak", "barbari", "lavash", "taftoon", "nan", "bagel", "tortilla"), "🍞"),
    (("rice", "berenj", "tahdig", "pasta", "noodle"), "🍚"),
    (("chicken", "morgh", "beef", "lamb", "gosht", "meat", "steak", "sausage", "kebab"), "🥩"),
    (("fish", "salmon", "tuna", "shrimp"), "🐟"),
    (("sabzi", "herb", "parsley", "cilantro", "dill", "mint", "basil"), "🌿"),
    (("lettuce", "salad", "spinach", "kale", "cabbage"), "🥬"),
    (("tomato", "cucumber", "pepper", "onion", "garlic", "potato", "carrot"), "🥕"),
    (("apple", "banana", "orange", "grape", "berry", "lemon", "lime", "fruit"), "🍎"),
    (("oil", "olive", "vinegar", "sauce", "spice", "zaatar", "sumac", "advieh"), "🫒"),
    (("bean", "lentil", "chickpea", "hummus", "tahini"), "🫘"),
    (("coffee", "tea", "chai"), "☕"),
    (("water", "soda", "juice", "cola"), "🥤"),
    (("chocolate", "cookie", "chip", "candy", "ice cream", "dessert"), "🍫"),
    (("soap", "shampoo", "toothpaste", "detergent", "tissue", "toilet"), "🧴"),
    (("vitamin", "medicine", "pill", "pharmacy"), "💊"),
    (("frozen", "ice"), "🧊"),
    (("nut", "almond", "pistachio", "walnut", "seed"), "🥜"),
]

REPLY_HOME = "🏠 Home"
REPLY_ADD = "➕ Add"
REPLY_AT = "🛒 At a store"
REPLY_GO = "🚨 Where to go"
REPLY_LIST = "📋 The list"
REPLY_STORES = "🏪 Stores"

MAIN_REPLY = ReplyKeyboardMarkup(
    [
        [REPLY_ADD, REPLY_AT],
        [REPLY_GO, REPLY_LIST],
        [REPLY_STORES, REPLY_HOME],
    ],
    resize_keyboard=True,
)


# --------------------------------------------------------------------------
# tiny utils
# --------------------------------------------------------------------------

def btn(label: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(label, callback_data=data)


def esc(text) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def chunk(seq: list, n: int) -> list[list]:
    return [seq[i:i + n] for i in range(0, len(seq), n)]


def grocery_emoji(name: str) -> str:
    blob = name.lower()
    for keywords, emoji in GROCERY_EMOJI:
        if any(k in blob for k in keywords):
            return emoji
    return "🛒"


def em_mark(level: int) -> str:
    return EMERGENCY.get(int(level), ("•", "noted"))[0]


def em_label(level: int) -> str:
    mark, word = EMERGENCY.get(int(level), ("•", "noted"))
    return f"{mark} {int(level)} {word}"


def parse_qty(raw: str) -> str:
    return re.sub(r"\s+", " ", raw.strip())[:40]


# --------------------------------------------------------------------------
# storage
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS households (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    join_code   TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS members (
    chat_id       INTEGER PRIMARY KEY,
    household_id  INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    label         TEXT NOT NULL DEFAULT '',
    joined_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    chat_id      INTEGER PRIMARY KEY,
    digest_time  TEXT NOT NULL,
    tz           TEXT NOT NULL,
    paused       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS stores (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    household_id  INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    parent_id     INTEGER NOT NULL DEFAULT 0,
    name          TEXT NOT NULL,
    emoji         TEXT NOT NULL DEFAULT '🏪',
    slug          TEXT NOT NULL,
    aliases       TEXT NOT NULL DEFAULT '',
    is_group      INTEGER NOT NULL DEFAULT 0,
    archived      INTEGER NOT NULL DEFAULT 0,
    sort_order    INTEGER NOT NULL DEFAULT 0,
    notes         TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    household_id    INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    name            TEXT NOT NULL,
    qty             TEXT NOT NULL DEFAULT '',
    notes           TEXT NOT NULL DEFAULT '',
    emergency       INTEGER NOT NULL DEFAULT 5,
    status          TEXT NOT NULL DEFAULT 'needed',
    staple          INTEGER NOT NULL DEFAULT 0,
    claimed_by      INTEGER,
    claimed_by_name TEXT NOT NULL DEFAULT '',
    added_by        INTEGER,
    added_by_name   TEXT NOT NULL DEFAULT '',
    created_at      TEXT NOT NULL,
    bought_at       TEXT,
    bought_by_name  TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS item_stores (
    item_id    INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    store_id   INTEGER NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    spread     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (item_id, store_id)
);

CREATE TABLE IF NOT EXISTS trips (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    household_id INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    store_id     INTEGER NOT NULL REFERENCES stores(id) ON DELETE CASCADE,
    started_by   INTEGER,
    started_name TEXT NOT NULL DEFAULT '',
    started_at   TEXT NOT NULL,
    ended_at     TEXT
);

CREATE TABLE IF NOT EXISTS purchases (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    household_id   INTEGER NOT NULL REFERENCES households(id) ON DELETE CASCADE,
    item_name      TEXT NOT NULL,
    store_name     TEXT NOT NULL,
    emergency      INTEGER NOT NULL DEFAULT 0,
    bought_at      TEXT NOT NULL,
    bought_by_name TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_items_house ON items (household_id, status, emergency);
CREATE INDEX IF NOT EXISTS idx_stores_house ON stores (household_id, parent_id, archived);
CREATE INDEX IF NOT EXISTS idx_item_stores ON item_stores (store_id);
"""


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def new_code(conn: sqlite3.Connection) -> str:
    while True:
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
        if not conn.execute(
            "SELECT 1 FROM households WHERE join_code = ?", (code,)
        ).fetchone():
            return code


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "store"


def seed_stores(conn: sqlite3.Connection, hid: int) -> None:
    existing = conn.execute(
        "SELECT COUNT(*) c FROM stores WHERE household_id = ?", (hid,)
    ).fetchone()["c"]
    if existing:
        return
    now = now_iso()
    order = 0
    for spec in DEFAULT_STORES:
        order += 1
        cur = conn.execute(
            "INSERT INTO stores (household_id, parent_id, name, emoji, slug, aliases, "
            "is_group, sort_order, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                hid, 0, spec["name"], spec["emoji"], spec["slug"],
                spec.get("aliases", ""), spec.get("is_group", 0), order, now,
            ),
        )
        parent_id = cur.lastrowid
        for i, child in enumerate(spec.get("children") or [], start=1):
            conn.execute(
                "INSERT INTO stores (household_id, parent_id, name, emoji, slug, aliases, "
                "is_group, sort_order, created_at) VALUES (?,?,?,?,?,?,0,?,?)",
                (
                    hid, parent_id, child["name"], child["emoji"], child["slug"],
                    child.get("aliases", ""), i, now,
                ),
            )


def init_db() -> None:
    with closing(db()) as conn:
        conn.executescript(SCHEMA)
        conn.commit()


def household_for(chat_id: int, label: str = "") -> int:
    now = now_iso()
    with closing(db()) as conn:
        row = conn.execute(
            "SELECT household_id FROM members WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        if row:
            if label:
                conn.execute(
                    "UPDATE members SET label = ? WHERE chat_id = ? AND label = ''",
                    (label, chat_id),
                )
                conn.commit()
            return row["household_id"]
        cur = conn.execute(
            "INSERT INTO households (name, join_code, created_at) VALUES (?,?,?)",
            ("Our cart", new_code(conn), now),
        )
        hid = cur.lastrowid
        conn.execute(
            "INSERT INTO members (chat_id, household_id, label, joined_at) VALUES (?,?,?,?)",
            (chat_id, hid, label, now),
        )
        seed_stores(conn, hid)
        conn.commit()
        return hid


def household_row(hid: int) -> Optional[sqlite3.Row]:
    with closing(db()) as conn:
        return conn.execute("SELECT * FROM households WHERE id = ?", (hid,)).fetchone()


def members_of(hid: int) -> list[sqlite3.Row]:
    with closing(db()) as conn:
        return conn.execute(
            "SELECT * FROM members WHERE household_id = ? ORDER BY joined_at", (hid,)
        ).fetchall()


def household_by_code(code: str) -> Optional[sqlite3.Row]:
    with closing(db()) as conn:
        return conn.execute(
            "SELECT * FROM households WHERE join_code = ? COLLATE NOCASE", (code.strip(),)
        ).fetchone()


def needed_count(hid: int) -> int:
    with closing(db()) as conn:
        return conn.execute(
            "SELECT COUNT(*) c FROM items WHERE household_id = ? AND status = 'needed'",
            (hid,),
        ).fetchone()["c"]


def panic_count(hid: int, floor: int = 10) -> int:
    with closing(db()) as conn:
        return conn.execute(
            "SELECT COUNT(*) c FROM items WHERE household_id = ? AND status = 'needed' "
            "AND emergency >= ?",
            (hid, floor),
        ).fetchone()["c"]


def move_member(chat_id: int, hid: int, label: str = "") -> None:
    now = now_iso()
    with closing(db()) as conn:
        conn.execute(
            "INSERT INTO members (chat_id, household_id, label, joined_at) VALUES (?,?,?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET household_id = excluded.household_id, "
            "label = CASE WHEN excluded.label = '' THEN members.label ELSE excluded.label END, "
            "joined_at = excluded.joined_at",
            (chat_id, hid, label, now),
        )
        conn.commit()


def merge_households(src: int, dst: int) -> None:
    if src == dst:
        return
    with closing(db()) as conn:
        id_map: dict[int, int] = {0: 0}
        stores = conn.execute(
            "SELECT * FROM stores WHERE household_id = ? ORDER BY parent_id, sort_order",
            (src,),
        ).fetchall()
        for store in stores:
            parent_dst = id_map.get(store["parent_id"], 0)
            twin = conn.execute(
                "SELECT id FROM stores WHERE household_id = ? AND parent_id = ? "
                "AND name = ? COLLATE NOCASE",
                (dst, parent_dst, store["name"]),
            ).fetchone()
            if twin:
                id_map[store["id"]] = twin["id"]
            else:
                cur = conn.execute(
                    "INSERT INTO stores (household_id, parent_id, name, emoji, slug, aliases, "
                    "is_group, archived, sort_order, notes, created_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        dst, parent_dst, store["name"], store["emoji"], store["slug"],
                        store["aliases"], store["is_group"], store["archived"],
                        store["sort_order"], store["notes"], store["created_at"],
                    ),
                )
                id_map[store["id"]] = cur.lastrowid
        items = conn.execute("SELECT * FROM items WHERE household_id = ?", (src,)).fetchall()
        item_map: dict[int, int] = {}
        for item in items:
            cur = conn.execute(
                "INSERT INTO items (household_id, name, qty, notes, emergency, status, staple, "
                "claimed_by, claimed_by_name, added_by, added_by_name, created_at, bought_at, "
                "bought_by_name) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    dst, item["name"], item["qty"], item["notes"], item["emergency"],
                    item["status"], item["staple"], item["claimed_by"], item["claimed_by_name"],
                    item["added_by"], item["added_by_name"], item["created_at"],
                    item["bought_at"], item["bought_by_name"],
                ),
            )
            item_map[item["id"]] = cur.lastrowid
        for link in conn.execute(
            "SELECT * FROM item_stores WHERE item_id IN "
            "(SELECT id FROM items WHERE household_id = ?)",
            (src,),
        ).fetchall():
            new_item = item_map.get(link["item_id"])
            new_store = id_map.get(link["store_id"])
            if new_item and new_store:
                conn.execute(
                    "INSERT OR IGNORE INTO item_stores (item_id, store_id, spread) VALUES (?,?,?)",
                    (new_item, new_store, link["spread"]),
                )
        conn.execute("UPDATE purchases SET household_id = ? WHERE household_id = ?", (dst, src))
        conn.execute("DELETE FROM households WHERE id = ?", (src,))
        conn.commit()


def get_settings(chat_id: int) -> sqlite3.Row:
    with closing(db()) as conn:
        row = conn.execute("SELECT * FROM settings WHERE chat_id = ?", (chat_id,)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO settings (chat_id, digest_time, tz, paused) VALUES (?,?,?,0)",
                (chat_id, DEFAULT_DIGEST_TIME, DEFAULT_TZ),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM settings WHERE chat_id = ?", (chat_id,)
            ).fetchone()
        return row


def update_settings(chat_id: int, **fields) -> None:
    get_settings(chat_id)
    cols = ", ".join(f"{k} = ?" for k in fields)
    with closing(db()) as conn:
        conn.execute(
            f"UPDATE settings SET {cols} WHERE chat_id = ?", (*fields.values(), chat_id)
        )
        conn.commit()


def chat_tz(chat_id: int) -> ZoneInfo:
    try:
        return ZoneInfo(get_settings(chat_id)["tz"])
    except ZoneInfoNotFoundError:
        return ZoneInfo(DEFAULT_TZ)


def today_for(chat_id: int) -> dt.date:
    return dt.datetime.now(chat_tz(chat_id)).date()


# --------------------------------------------------------------------------
# stores
# --------------------------------------------------------------------------

def store_by_id(hid: int, sid: int) -> Optional[sqlite3.Row]:
    with closing(db()) as conn:
        return conn.execute(
            "SELECT * FROM stores WHERE id = ? AND household_id = ?", (sid, hid)
        ).fetchone()


def top_stores(hid: int, include_archived: bool = False) -> list[sqlite3.Row]:
    sql = "SELECT * FROM stores WHERE household_id = ? AND parent_id = 0"
    args: list = [hid]
    if not include_archived:
        sql += " AND archived = 0"
    sql += " ORDER BY sort_order, name"
    with closing(db()) as conn:
        return conn.execute(sql, args).fetchall()


def child_stores(hid: int, parent_id: int, include_archived: bool = False) -> list[sqlite3.Row]:
    sql = "SELECT * FROM stores WHERE household_id = ? AND parent_id = ?"
    args: list = [hid, parent_id]
    if not include_archived:
        sql += " AND archived = 0"
    sql += " ORDER BY sort_order, name"
    with closing(db()) as conn:
        return conn.execute(sql, args).fetchall()


def leaf_stores(hid: int) -> list[sqlite3.Row]:
    """Stores you can actually walk into (not empty groups)."""
    with closing(db()) as conn:
        return conn.execute(
            "SELECT s.* FROM stores s WHERE s.household_id = ? AND s.archived = 0 AND ("
            "s.is_group = 0 OR EXISTS ("
            "SELECT 1 FROM stores c WHERE c.parent_id = s.id AND c.archived = 0"
            ")) ORDER BY s.parent_id, s.sort_order, s.name",
            (hid,),
        ).fetchall()


def shoppable_stores(hid: int) -> list[sqlite3.Row]:
    """Leaf stores only — for 'I'm at a store'."""
    with closing(db()) as conn:
        return conn.execute(
            "SELECT * FROM stores WHERE household_id = ? AND archived = 0 AND is_group = 0 "
            "ORDER BY parent_id, sort_order, name",
            (hid,),
        ).fetchall()


def store_label(row) -> str:
    return f"{row['emoji']} {row['name']}"


def match_store(hid: int, raw: str) -> Optional[sqlite3.Row]:
    needle = raw.strip().lower()
    if not needle:
        return None
    with closing(db()) as conn:
        rows = conn.execute(
            "SELECT * FROM stores WHERE household_id = ? AND archived = 0", (hid,)
        ).fetchall()
    for row in rows:
        names = [row["name"].lower(), row["slug"].lower()]
        names.extend(a.strip() for a in row["aliases"].split(",") if a.strip())
        if needle in names or any(needle == a or needle in a.split() for a in names):
            return row
        if needle in row["name"].lower():
            return row
    return None


def add_store(hid: int, name: str, parent_id: int = 0, is_group: int = 0,
              emoji: str = "🏪") -> sqlite3.Row:
    name = name.strip()[:48]
    with closing(db()) as conn:
        twin = conn.execute(
            "SELECT * FROM stores WHERE household_id = ? AND parent_id = ? "
            "AND name = ? COLLATE NOCASE",
            (hid, parent_id, name),
        ).fetchone()
        if twin:
            if twin["archived"]:
                conn.execute("UPDATE stores SET archived = 0 WHERE id = ?", (twin["id"],))
                conn.commit()
                twin = conn.execute("SELECT * FROM stores WHERE id = ?", (twin["id"],)).fetchone()
            return twin
        max_sort = conn.execute(
            "SELECT COALESCE(MAX(sort_order), 0) m FROM stores "
            "WHERE household_id = ? AND parent_id = ?",
            (hid, parent_id),
        ).fetchone()["m"]
        slug = slugify(name)
        taken = conn.execute(
            "SELECT 1 FROM stores WHERE household_id = ? AND slug = ?", (hid, slug)
        ).fetchone()
        if taken:
            slug = f"{slug}-{secrets.token_hex(2)}"
        cur = conn.execute(
            "INSERT INTO stores (household_id, parent_id, name, emoji, slug, aliases, "
            "is_group, sort_order, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (hid, parent_id, name, emoji, slug, name.lower(), is_group, max_sort + 1, now_iso()),
        )
        if parent_id:
            conn.execute("UPDATE stores SET is_group = 1 WHERE id = ?", (parent_id,))
        sid = cur.lastrowid
        conn.commit()
        return conn.execute("SELECT * FROM stores WHERE id = ?", (sid,)).fetchone()


def rename_store(hid: int, sid: int, name: str) -> None:
    name = name.strip()[:48]
    with closing(db()) as conn:
        conn.execute(
            "UPDATE stores SET name = ? WHERE id = ? AND household_id = ?",
            (name, sid, hid),
        )
        conn.commit()


def set_archived(hid: int, sid: int, archived: int) -> None:
    with closing(db()) as conn:
        conn.execute(
            "UPDATE stores SET archived = ? WHERE id = ? AND household_id = ?",
            (archived, sid, hid),
        )
        conn.commit()


# --------------------------------------------------------------------------
# items
# --------------------------------------------------------------------------

def get_item(hid: int, iid: int) -> Optional[sqlite3.Row]:
    with closing(db()) as conn:
        return conn.execute(
            "SELECT * FROM items WHERE id = ? AND household_id = ?", (iid, hid)
        ).fetchone()


def item_store_rows(iid: int) -> list[sqlite3.Row]:
    with closing(db()) as conn:
        return conn.execute(
            "SELECT s.*, ist.spread FROM item_stores ist "
            "JOIN stores s ON s.id = ist.store_id "
            "WHERE ist.item_id = ? ORDER BY s.parent_id, s.sort_order",
            (iid,),
        ).fetchall()


def item_store_names(iid: int, hid: int) -> str:
    links = item_store_rows(iid)
    if not links:
        return "unassigned"
    parts = []
    for row in links:
        if row["spread"] and row["is_group"]:
            kids = child_stores(hid, row["id"])
            if kids:
                parts.append(f"{row['name']} (all)")
                continue
        parts.append(row["name"])
    return " · ".join(parts)


def needed_items(hid: int) -> list[sqlite3.Row]:
    with closing(db()) as conn:
        return conn.execute(
            "SELECT * FROM items WHERE household_id = ? AND status = 'needed' "
            "ORDER BY emergency DESC, created_at",
            (hid,),
        ).fetchall()


def items_for_store(hid: int, sid: int) -> list[sqlite3.Row]:
    """Items assigned to this store, or spread from its parent group."""
    store = store_by_id(hid, sid)
    if store is None:
        return []
    with closing(db()) as conn:
        return conn.execute(
            """
            SELECT DISTINCT i.* FROM items i
            JOIN item_stores ist ON ist.item_id = i.id
            WHERE i.household_id = ? AND i.status = 'needed' AND (
                ist.store_id = ?
                OR (ist.spread = 1 AND ist.store_id = ?)
            )
            ORDER BY i.emergency DESC, i.name COLLATE NOCASE
            """,
            (hid, sid, store["parent_id"]),
        ).fetchall()


def items_at_or_above(hid: int, floor: int) -> list[sqlite3.Row]:
    with closing(db()) as conn:
        return conn.execute(
            "SELECT * FROM items WHERE household_id = ? AND status = 'needed' "
            "AND emergency >= ? ORDER BY emergency DESC, name COLLATE NOCASE",
            (hid, floor),
        ).fetchall()


def find_items(hid: int, needle: str) -> list[sqlite3.Row]:
    like = f"%{needle.strip()}%"
    with closing(db()) as conn:
        return conn.execute(
            "SELECT * FROM items WHERE household_id = ? AND status = 'needed' "
            "AND name LIKE ? COLLATE NOCASE ORDER BY emergency DESC",
            (hid, like),
        ).fetchall()


def existing_needed(hid: int, name: str) -> Optional[sqlite3.Row]:
    with closing(db()) as conn:
        return conn.execute(
            "SELECT * FROM items WHERE household_id = ? AND status = 'needed' "
            "AND name = ? COLLATE NOCASE",
            (hid, name.strip()),
        ).fetchone()


def attach_stores(conn: sqlite3.Connection, item_id: int, store_id: int, spread: int,
                  replace: bool = False) -> None:
    if replace:
        conn.execute("DELETE FROM item_stores WHERE item_id = ?", (item_id,))
    conn.execute(
        "INSERT INTO item_stores (item_id, store_id, spread) VALUES (?,?,?) "
        "ON CONFLICT(item_id, store_id) DO UPDATE SET spread = excluded.spread",
        (item_id, store_id, spread),
    )


def insert_item(hid: int, name: str, store_id: int, spread: int, emergency: int,
                qty: str = "", notes: str = "", by_id: Optional[int] = None,
                by_name: str = "", staple: int = 0) -> sqlite3.Row:
    name = name.strip()[:80]
    twin = existing_needed(hid, name)
    with closing(db()) as conn:
        if twin:
            conn.execute(
                "UPDATE items SET emergency = MAX(emergency, ?), "
                "qty = CASE WHEN ? = '' THEN qty ELSE ? END "
                "WHERE id = ?",
                (emergency, qty, qty, twin["id"]),
            )
            attach_stores(conn, twin["id"], store_id, spread)
            conn.commit()
            return conn.execute("SELECT * FROM items WHERE id = ?", (twin["id"],)).fetchone()
        cur = conn.execute(
            "INSERT INTO items (household_id, name, qty, notes, emergency, status, staple, "
            "added_by, added_by_name, created_at) VALUES (?,?,?,?,?,'needed',?,?,?,?)",
            (hid, name, qty, notes, emergency, staple, by_id, by_name, now_iso()),
        )
        iid = cur.lastrowid
        attach_stores(conn, iid, store_id, spread)
        conn.commit()
        return conn.execute("SELECT * FROM items WHERE id = ?", (iid,)).fetchone()


def set_item_fields(hid: int, iid: int, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    with closing(db()) as conn:
        conn.execute(
            f"UPDATE items SET {cols} WHERE id = ? AND household_id = ?",
            (*fields.values(), iid, hid),
        )
        conn.commit()


def mark_bought(hid: int, iid: int, store_name: str, who_name: str) -> Optional[sqlite3.Row]:
    item = get_item(hid, iid)
    if item is None:
        return None
    with closing(db()) as conn:
        conn.execute(
            "UPDATE items SET status = 'bought', bought_at = ?, bought_by_name = ?, "
            "claimed_by = NULL, claimed_by_name = '' WHERE id = ? AND household_id = ?",
            (now_iso(), who_name, iid, hid),
        )
        conn.execute(
            "INSERT INTO purchases (household_id, item_name, store_name, emergency, "
            "bought_at, bought_by_name) VALUES (?,?,?,?,?,?)",
            (hid, item["name"], store_name, item["emergency"], now_iso(), who_name),
        )
        conn.commit()
    return get_item(hid, iid)


def restore_item(hid: int, iid: int) -> None:
    set_item_fields(hid, iid, status="needed", bought_at=None, bought_by_name="")


def drop_item(hid: int, iid: int) -> None:
    set_item_fields(hid, iid, status="dropped")


# --------------------------------------------------------------------------
# scoring / routing
# --------------------------------------------------------------------------

def store_scores(hid: int, floor: int) -> list[tuple[sqlite3.Row, list[sqlite3.Row], int]]:
    """Shoppable stores with matching items, ranked by panic then total emergency."""
    ranked = []
    for store in shoppable_stores(hid):
        items = [i for i in items_for_store(hid, store["id"]) if i["emergency"] >= floor]
        if not items:
            continue
        panic = sum(1 for i in items if i["emergency"] >= 10)
        total = sum(i["emergency"] for i in items)
        ranked.append((store, items, panic * 100 + total))
    ranked.sort(key=lambda t: t[2], reverse=True)
    return ranked


def best_route(hid: int, floor: int) -> list[sqlite3.Row]:
    """Greedy cover: fewest stores to clear every item at this emergency."""
    remaining = {i["id"]: i for i in items_at_or_above(hid, floor)}
    route = []
    used = set()
    while remaining:
        best = None
        best_cover: list[sqlite3.Row] = []
        for store in shoppable_stores(hid):
            if store["id"] in used:
                continue
            cover = [i for i in items_for_store(hid, store["id"]) if i["id"] in remaining]
            if not cover:
                continue
            score = (len(cover), sum(i["emergency"] for i in cover))
            if best is None or score > best:
                best = score
                best_cover = cover
                best_store = store
        if not best_cover:
            break
        route.append(best_store)
        used.add(best_store["id"])
        for item in best_cover:
            remaining.pop(item["id"], None)
    return route


# --------------------------------------------------------------------------
# parsing
# --------------------------------------------------------------------------

class ParseError(ValueError):
    pass


def parse_emergency(raw: str) -> Optional[int]:
    raw = raw.strip()
    if re.fullmatch(r"10|[0-9]", raw):
        return int(raw)
    return None


def parse_add_line(hid: int, line: str) -> dict:
    parts = [p.strip() for p in line.split("|")]
    if not parts or not parts[0]:
        raise ParseError("Need an item name.")
    data: dict = {"name": parts[0][:80], "qty": "", "notes": "", "store": None, "emergency": None}
    if len(parts) == 1:
        return data
    # name | store | emergency | qty   OR  name | qty
    rest = parts[1:]
    store = match_store(hid, rest[0]) if rest else None
    if store:
        data["store"] = store
        rest = rest[1:]
    if rest:
        em = parse_emergency(rest[0])
        if em is not None:
            data["emergency"] = em
            rest = rest[1:]
    if rest:
        data["qty"] = parse_qty(" | ".join(rest))
    return data


# --------------------------------------------------------------------------
# draft helpers (in-progress add)
# --------------------------------------------------------------------------

def draft_of(context: ContextTypes.DEFAULT_TYPE) -> dict:
    return context.user_data.setdefault("draft", {})


def clear_draft(context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.pop("draft", None)
    context.user_data.pop("await", None)
    context.user_data.pop("await_id", None)


def set_await(context: ContextTypes.DEFAULT_TYPE, kind: str, ident: int = 0) -> None:
    context.user_data["await"] = kind
    context.user_data["await_id"] = ident


# --------------------------------------------------------------------------
# views
# --------------------------------------------------------------------------

def kb_home() -> list[list[InlineKeyboardButton]]:
    return [
        [btn("➕ Add item", "addhint"), btn("🛒 At a store", "at")],
        [btn("🚨 Where to go", "go:10"), btn("📋 Full list", "list")],
        [btn("🏪 Edit stores", "stores"), btn("🎲 Grab one", "roll")],
        [btn("📊 Stats", "stats"), btn("👥 Household", "house")],
        [btn("⚙️ Settings", "settings")],
    ]


def view_home(chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
    hid = household_for(chat_id)
    cfg = get_settings(chat_id)
    n = needed_count(hid)
    p10 = panic_count(hid, 10)
    p8 = panic_count(hid, 8)
    people = len(members_of(hid))
    bell = "🔔" if not cfg["paused"] else "🔕"
    shared = ""
    if people > 1:
        shared = f"\n👥 shared with {people - 1} other" + ("s" if people > 2 else "")
    panic_line = "🚨 no panic-10s. breathe."
    if p10:
        panic_line = f"🚨 <b>{p10}</b> panic-10 item" + ("s" if p10 != 1 else "")
    elif p8:
        panic_line = f"⏰ {p8} item" + ("s" if p8 != 1 else "") + " at 8+"
    text = (
        f"🛒 <b>PanicCart</b>\n{RULE}\n"
        f"<b>{n}</b> needed · {panic_line}\n"
        f"{bell} digest at {cfg['digest_time']}{shared}\n\n"
        f"<i>Tip: paste</i> <code>milk | costco | 8</code>\n"
        f"<i>or just send the name and tap a store.</i>"
    )
    return text, InlineKeyboardMarkup(kb_home())


def item_line(hid: int, row, show_store: bool = True) -> str:
    qty = f" × {esc(row['qty'])}" if row["qty"] else ""
    claim = f" · 🙋 {esc(row['claimed_by_name'])}" if row["claimed_by_name"] else ""
    staple = " 🔁" if row["staple"] else ""
    store = f"\n      <i>{esc(item_store_names(row['id'], hid))}</i>" if show_store else ""
    return (
        f"{em_mark(row['emergency'])}{grocery_emoji(row['name'])} "
        f"{esc(row['name'])}{qty}{staple}{claim}{store}"
    )


def view_list(chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
    hid = household_for(chat_id)
    items = needed_items(hid)
    head = f"📋 <b>The list</b>\n{RULE}"
    if not items:
        return (
            f"{head}\n\n<i>{esc(random.choice(EMPTY_LINES))}</i>",
            InlineKeyboardMarkup([[btn("➕ Add item", "addhint"), btn("🏠 Home", "home")]]),
        )
    by_em: dict[int, list] = {}
    for row in items:
        by_em.setdefault(row["emergency"], []).append(row)
    blocks = []
    for level in sorted(by_em, reverse=True):
        lines = [item_line(hid, r) for r in by_em[level]]
        title = em_label(level)
        blocks.append(f"<b>{title}</b>\n" + "\n".join(lines))
    body = "\n\n".join(blocks)
    if len(body) > 3500:
        body = body[:3490] + "…"
    text = f"{head}\n{body}\n\n<i>{len(items)} needed</i>"
    kb = InlineKeyboardMarkup([
        [btn("➕ Add", "addhint"), btn("🚨 Panic 10s", "go:10")],
        [btn("🛒 At a store", "at"), btn("🏠 Home", "home")],
    ])
    return text, kb


def view_add_stores(chat_id: int, title: str = "") -> tuple[str, InlineKeyboardMarkup]:
    hid = household_for(chat_id)
    draft = title
    name = f" for <b>{esc(draft)}</b>" if draft else ""
    text = (
        f"➕ <b>Where should we get it?</b>{name}\n{RULE}\n"
        "Pick a store. Persian markets open a second list."
    )
    rows = []
    for i, store in enumerate(top_stores(hid), start=1):
        label = f"{i} · {store_label(store)}"
        if store["is_group"] or child_stores(hid, store["id"]):
            rows.append([btn(label, f"ag:{store['id']}")])
        else:
            rows.append([btn(label, f"as:{store['id']}")])
    rows.append([btn("✏️ Edit candidates", "stores")])
    rows.append([btn("✖ Cancel", "addcancel")])
    return text, InlineKeyboardMarkup(rows)


def view_add_group(chat_id: int, parent_id: int, item_name: str = "") -> tuple[str, InlineKeyboardMarkup]:
    hid = household_for(chat_id)
    parent = store_by_id(hid, parent_id)
    if parent is None:
        return view_add_stores(chat_id, item_name)
    kids = child_stores(hid, parent_id)
    name = f"\nItem: <b>{esc(item_name)}</b>" if item_name else ""
    text = (
        f"{parent['emoji']} <b>{esc(parent['name'])}</b>{name}\n{RULE}\n"
        "0 · all stores — or pick one."
    )
    rows = [[btn(f"0 · All {parent['name']}", f"aa:{parent_id}")]]
    for i, kid in enumerate(kids, start=1):
        rows.append([btn(f"{i} · {store_label(kid)}", f"as:{kid['id']}")])
    rows.append([btn("◀ Back", "addstores"), btn("✏️ Edit these", f"stedit:{parent_id}")])
    return text, InlineKeyboardMarkup(rows)


def view_add_emergency(item_name: str, store_phrase: str) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        f"🚨 <b>How urgent?</b>\n{RULE}\n"
        f"<b>{esc(item_name)}</b>\n<i>{esc(store_phrase)}</i>\n\n"
        "0 = whenever · 10 = leave the house now"
    )
    rows = []
    for start in (0, 5):
        rows.append([btn(f"{em_mark(i)}{i}", f"ae:{i}") for i in range(start, start + 5)])
    rows.append([btn("🚨 10 · PANIC", "ae:10")])
    rows.append([btn("◀ Stores", "addstores"), btn("✖ Cancel", "addcancel")])
    return text, InlineKeyboardMarkup(rows)


def view_item(chat_id: int, iid: int) -> tuple[str, InlineKeyboardMarkup]:
    hid = household_for(chat_id)
    row = get_item(hid, iid)
    if row is None or row["status"] != "needed":
        return "That item is gone from the cart.", InlineKeyboardMarkup([[btn("🏠 Home", "home")]])
    qty = row["qty"] or "—"
    notes = row["notes"] or "—"
    claim = row["claimed_by_name"] or "nobody yet"
    staple = "yes — restocks when bought" if row["staple"] else "no"
    text = (
        f"{grocery_emoji(row['name'])} <b>{esc(row['name'])}</b>\n{RULE}\n"
        f"{em_label(row['emergency'])}\n"
        f"🏪 {esc(item_store_names(iid, hid))}\n"
        f"🔢 qty: {esc(qty)}\n"
        f"🔁 staple: {staple}\n"
        f"🙋 claimed: {esc(claim)}\n"
        f"📝 {esc(notes)}\n"
        f"<i>added by {esc(row['added_by_name'] or 'someone')}</i>"
    )
    kb = [
        [btn("✅ Got it", f"ck:{iid}:0"), btn("🙋 I'll grab it", f"claim:{iid}")],
        [btn("🚨 Urgency", f"emenu:{iid}"), btn("🔁 Staple", f"staple:{iid}")],
        [btn("🔢 Qty", f"qty:{iid}"), btn("🏪 Move store", f"move:{iid}")],
        [btn("🗑 Drop", f"askdel:{iid}"), btn("◀ List", "list")],
    ]
    return text, InlineKeyboardMarkup(kb)


def view_emergency_edit(iid: int, name: str, current: int) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        f"🚨 Urgency for <b>{esc(name)}</b>\n{RULE}\n"
        f"Currently {em_label(current)}"
    )
    rows = []
    for start in (0, 5):
        rows.append([
            btn(("· " if i == current else "") + f"{em_mark(i)}{i}", f"ie:{iid}:{i}")
            for i in range(start, start + 5)
        ])
    rows.append([btn("🚨 10 · PANIC", f"ie:{iid}:10")])
    rows.append([btn("◀ Back", f"it:{iid}")])
    return text, InlineKeyboardMarkup(rows)


def view_go(chat_id: int, floor: int) -> tuple[str, InlineKeyboardMarkup]:
    hid = household_for(chat_id)
    ranked = store_scores(hid, floor)
    items = items_at_or_above(hid, floor)
    head = (
        f"🚨 <b>Where to go</b>\n{RULE}\n"
        f"Showing items at {em_label(floor)}"
        + (" and up" if floor < 10 else " only")
        + ".\n"
    )
    if not items:
        softer = 8 if floor > 8 else 5 if floor > 5 else 0
        extra = f"\nNothing at this cutoff. Try {em_label(softer)}?"
        kb = [
            [btn("10s", "go:10"), btn("8+", "go:8"), btn("5+", "go:5"), btn("All", "go:0")],
            [btn("🏠 Home", "home")],
        ]
        return head + extra, InlineKeyboardMarkup(kb)

    blocks = [f"<i>{random.choice(PANIC_FLAVOUR)}</i>"]
    for store, store_items, _score in ranked:
        lines = [
            f"  {em_mark(i['emergency'])} {esc(i['name'])}"
            + (f" × {esc(i['qty'])}" if i["qty"] else "")
            for i in store_items
        ]
        blocks.append(f"<b>{store_label(store)}</b> · {len(store_items)}\n" + "\n".join(lines))

    route = best_route(hid, floor)
    if route:
        path = " → ".join(store_label(s) for s in route)
        blocks.append(f"🗺 <b>Shortest run</b>\n{esc(path)}")

    body = "\n\n".join(blocks)
    if len(body) > 3200:
        body = body[:3190] + "…"
    kb = [
        [btn("10s", "go:10"), btn("8+", "go:8"), btn("5+", "go:5"), btn("All", "go:0")],
        [btn("🛒 Start a trip", "at"), btn("🎲 Grab one", f"roll:{floor}")],
        [btn("🏠 Home", "home")],
    ]
    return head + "\n" + body, InlineKeyboardMarkup(kb)


def view_at_pick(chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
    hid = household_for(chat_id)
    stores = shoppable_stores(hid)
    text = (
        f"🛒 <b>Which store are you at?</b>\n{RULE}\n"
        "I'll show everything assigned there, panic first."
    )
    rows = []
    grouped: dict[int, list] = {}
    tops = {s["id"]: s for s in top_stores(hid)}
    for store in stores:
        grouped.setdefault(store["parent_id"], []).append(store)
    # top-level leaves first, then groups
    for store in stores:
        if store["parent_id"] == 0:
            n = len(items_for_store(hid, store["id"]))
            label = f"{store_label(store)}" + (f" · {n}" if n else "")
            rows.append([btn(label, f"at:{store['id']}:0")])
    for parent_id, kids in grouped.items():
        if parent_id == 0:
            continue
        parent = tops.get(parent_id) or store_by_id(hid, parent_id)
        if parent:
            rows.append([btn(f"— {store_label(parent)} —", "noop")])
        for kid in kids:
            n = len(items_for_store(hid, kid["id"]))
            label = f"{store_label(kid)}" + (f" · {n}" if n else "")
            rows.append([btn(label, f"at:{kid['id']}:0")])
    rows.append([btn("🏠 Home", "home")])
    return text, InlineKeyboardMarkup(rows)


def view_at_store(chat_id: int, sid: int, page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    hid = household_for(chat_id)
    store = store_by_id(hid, sid)
    if store is None:
        return view_at_pick(chat_id)
    items = items_for_store(hid, sid)
    panic = sum(1 for i in items if i["emergency"] >= 10)
    head = (
        f"{store['emoji']} <b>At {esc(store['name'])}</b>\n{RULE}\n"
        f"{len(items)} needed · {panic} panic-10"
    )
    if not items:
        return (
            head + "\n\n<i>Nothing assigned here. Either you're done, or the list is lying.</i>",
            InlineKeyboardMarkup([
                [btn("◀ Other stores", "at"), btn("➕ Add here", f"addat:{sid}")],
                [btn("🏠 Home", "home")],
            ]),
        )
    pages = max(1, (len(items) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    slice_ = items[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    lines = []
    for row in slice_:
        qty = f" × {esc(row['qty'])}" if row["qty"] else ""
        claim = f" · 🙋{esc(row['claimed_by_name'])}" if row["claimed_by_name"] else ""
        lines.append(
            f"{em_mark(row['emergency'])} {esc(row['name'])}{qty} "
            f"<i>{row['emergency']}</i>{claim}"
        )
    text = head + "\n\n" + "\n".join(lines)
    if pages > 1:
        text += f"\n\n<i>page {page + 1}/{pages}</i>"
    rows = []
    for row in slice_:
        rows.append([
            btn(f"✅ {row['name'][:28]}", f"ck:{row['id']}:{sid}"),
            btn("·", f"it:{row['id']}"),
        ])
    nav = []
    if page > 0:
        nav.append(btn("◀", f"at:{sid}:{page - 1}"))
    if page < pages - 1:
        nav.append(btn("▶", f"at:{sid}:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([btn("✅ Done shopping", f"tripdone:{sid}"), btn("➕ Add here", f"addat:{sid}")])
    rows.append([btn("◀ Stores", "at"), btn("🏠 Home", "home")])
    return text, InlineKeyboardMarkup(rows)


def view_stores(chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
    hid = household_for(chat_id)
    text = (
        f"🏪 <b>Store candidates</b>\n{RULE}\n"
        "These are the choices when you add an item. Tap one to rename, "
        "archive, or add a child store."
    )
    rows = []
    for i, store in enumerate(top_stores(hid, include_archived=True), start=1):
        mark = " (hidden)" if store["archived"] else ""
        extra = " ▾" if store["is_group"] or child_stores(hid, store["id"], True) else ""
        rows.append([btn(f"{i} · {store_label(store)}{extra}{mark}", f"stedit:{store['id']}")])
    rows.append([btn("➕ Add a store", "stadd:0"), btn("➕ Add a group", "stgroup")])
    rows.append([btn("🏠 Home", "home")])
    return text, InlineKeyboardMarkup(rows)


def view_store_edit(chat_id: int, sid: int) -> tuple[str, InlineKeyboardMarkup]:
    hid = household_for(chat_id)
    store = store_by_id(hid, sid)
    if store is None:
        return view_stores(chat_id)
    kids = child_stores(hid, sid, include_archived=True)
    n = len(items_for_store(hid, sid)) if not store["is_group"] else 0
    if store["is_group"] or kids:
        n = sum(len(items_for_store(hid, k["id"])) for k in kids if not k["archived"])
    state = "hidden from picker" if store["archived"] else "active"
    text = (
        f"{store['emoji']} <b>{esc(store['name'])}</b>\n{RULE}\n"
        f"{'Group' if store['is_group'] or kids else 'Store'} · {state}\n"
        f"{n} live item" + ("s" if n != 1 else "") + "\n"
    )
    if store["notes"]:
        text += f"\n{esc(store['notes'])}\n"
    rows = []
    for i, kid in enumerate(kids, start=1):
        mark = " (hidden)" if kid["archived"] else ""
        rows.append([btn(f"{i} · {store_label(kid)}{mark}", f"stedit:{kid['id']}")])
    rows.append([btn("✏️ Rename", f"stren:{sid}")])
    if store["archived"]:
        rows.append([btn("♻️ Restore", f"starch:{sid}:0")])
    else:
        rows.append([btn("🙈 Hide from picker", f"starch:{sid}:1")])
    rows.append([btn("➕ Add store under this", f"stadd:{sid}")])
    back = "stedit:" + str(store["parent_id"]) if store["parent_id"] else "stores"
    rows.append([btn("◀ Back", back)])
    return text, InlineKeyboardMarkup(rows)


def view_roll(chat_id: int, floor: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    hid = household_for(chat_id)
    pool = items_at_or_above(hid, floor) or needed_items(hid)
    if not pool:
        return (
            "🎲 Nothing to grab. The cart is at peace.",
            InlineKeyboardMarkup([[btn("🏠 Home", "home")]]),
        )
    weights = []
    for row in pool:
        weights.extend([row] * (1 + row["emergency"] + (3 if row["emergency"] >= 10 else 0)))
    pick = random.choice(weights)
    text = (
        f"🎲 <i>{random.choice(ROLL_FLAVOUR)}</i>\n{RULE}\n"
        f"{grocery_emoji(pick['name'])} <b>{esc(pick['name'])}</b>\n"
        f"{em_label(pick['emergency'])}\n"
        f"🏪 {esc(item_store_names(pick['id'], hid))}"
        + (f"\n🔢 {esc(pick['qty'])}" if pick["qty"] else "")
    )
    kb = InlineKeyboardMarkup([
        [btn("✅ Got it", f"ck:{pick['id']}:0"), btn("🎲 Again", f"roll:{floor}")],
        [btn("Open item", f"it:{pick['id']}"), btn("🏠 Home", "home")],
    ])
    return text, kb


def view_stats(chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
    hid = household_for(chat_id)
    with closing(db()) as conn:
        bought = conn.execute(
            "SELECT COUNT(*) c FROM purchases WHERE household_id = ?", (hid,)
        ).fetchone()["c"]
        recent = conn.execute(
            "SELECT * FROM purchases WHERE household_id = ? ORDER BY bought_at DESC LIMIT 8",
            (hid,),
        ).fetchall()
        top_stores_bought = conn.execute(
            "SELECT store_name, COUNT(*) c FROM purchases WHERE household_id = ? "
            "GROUP BY store_name ORDER BY c DESC LIMIT 5",
            (hid,),
        ).fetchall()
        panics = conn.execute(
            "SELECT COUNT(*) c FROM purchases WHERE household_id = ? AND emergency >= 10",
            (hid,),
        ).fetchone()["c"]
    n = needed_count(hid)
    p10 = panic_count(hid, 10)
    lines = [
        f"📊 <b>Cart stats</b>\n{RULE}",
        f"{n} still needed · {p10} panic-10s",
        f"{bought} grabbed all-time · {panics} of those were panics",
    ]
    if top_stores_bought:
        lines.append("\n<b>Where you actually go</b>")
        lines.extend(f"• {esc(r['store_name'])} · {r['c']}" for r in top_stores_bought)
    if recent:
        lines.append("\n<b>Recently grabbed</b>")
        lines.extend(
            f"• {esc(r['item_name'])} <i>({esc(r['store_name'])})</i>"
            + (f" · {esc(r['bought_by_name'])}" if r["bought_by_name"] else "")
            for r in recent
        )
    kb = InlineKeyboardMarkup([
        [btn("📤 Export CSV", "export"), btn("🔁 Restock staples", "restock")],
        [btn("🏠 Home", "home")],
    ])
    return "\n".join(lines), kb


def view_household(chat_id: int, bot_username: str = "") -> tuple[str, InlineKeyboardMarkup]:
    hid = household_for(chat_id)
    house = household_row(hid)
    people = members_of(hid)
    roster = "\n".join(
        f"• {esc(m['label'] or 'someone')}" + (" <i>(you)</i>" if m["chat_id"] == chat_id else "")
        for m in people
    )
    link = f"https://t.me/{bot_username}?start=join-{house['join_code']}" if bot_username else ""
    text = (
        f"👥 <b>{esc(house['name'])}</b>\n{RULE}\n"
        f"{roster}\n\n"
        f"<b>Invite code</b>\n<code>{house['join_code']}</code>\n"
        + (f"\n<a href=\"{link}\">Tap here to share the join link</a>\n" if link else "")
        + "\n<i>They open the bot and send</i> "
        f"<code>/join {house['join_code']}</code>\n"
        f"<i>Everyone shares the cart. Digest times stay personal.</i>"
    )
    kb = InlineKeyboardMarkup([
        [btn("✏️ Rename household", "renamehint")],
        [btn("🚪 Leave household", "askleave")],
        [btn("🏠 Home", "home")],
    ])
    return text, kb


TIME_PRESETS = ["07:00", "08:00", "08:30", "09:00", "10:00", "18:00"]


def view_settings(chat_id: int) -> tuple[str, InlineKeyboardMarkup]:
    cfg = get_settings(chat_id)
    state = "paused" if cfg["paused"] else f"on, {cfg['digest_time']} daily"
    text = (
        f"⚙️ <b>Your settings</b>\n{RULE}\n"
        f"🔔 Digest: {esc(state)}\n"
        f"🌍 Timezone: {esc(cfg['tz'])}\n\n"
        f"<i>Morning ping is panic-8+ items, grouped by store.</i>\n\n"
        f"<code>/settime 07:45</code>\n"
        f"<code>/tz America/Toronto</code>"
    )
    rows = [
        [btn(("✅ " if cfg["digest_time"] == t else "") + t, f"time:{t}") for t in TIME_PRESETS[:3]],
        [btn(("✅ " if cfg["digest_time"] == t else "") + t, f"time:{t}") for t in TIME_PRESETS[3:]],
        [btn("🔕 Pause digest" if not cfg["paused"] else "🔔 Resume digest", "togglepause")],
        [btn("📤 Export CSV", "export"), btn("🏠 Home", "home")],
    ]
    return text, InlineKeyboardMarkup(rows)


HELP = f"""🛒 <b>PanicCart</b>
{RULE}
<b>Adding</b>
Send a name, then pick a store and an urgency 0–10.
Or paste a line:
<code>milk | costco | 8</code>
<code>sabzi | paeez | 10 | 2 bunches</code>
<code>yogurt | persian | 9</code>
<code>persian</code> opens Paeez, Heeva, Arzon, Khorak, Irooni, Koorosh — or all of them.

<b>Going out</b>
/go — which store to hit (starts at panic-10)
/at — I'm at a store, show the list, tap ✅
/roll — grab the most dramatic item

<b>Stores</b>
/stores — edit candidates (add, rename, hide, nest)

<b>Sharing</b>
/invite · /join CODE · /household

<b>Other</b>
/list /find milk /stats /export
/settime 08:30 · /tz America/Toronto
"""


def view_help() -> tuple[str, InlineKeyboardMarkup]:
    return HELP, InlineKeyboardMarkup([[btn("🏠 Home", "home")]])


# --------------------------------------------------------------------------
# reply helpers
# --------------------------------------------------------------------------

async def show(update: Update, text: str, kb: Optional[InlineKeyboardMarkup]) -> None:
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                text, parse_mode=HTML, reply_markup=kb, disable_web_page_preview=True)
            return
        except BadRequest as exc:
            if "not modified" in str(exc).lower():
                return
            log.warning("edit failed, sending fresh: %s", exc)
            await update.callback_query.message.reply_text(
                text, parse_mode=HTML, reply_markup=kb)
            return
    await update.effective_message.reply_text(
        text, parse_mode=HTML, reply_markup=kb,
    )


async def show_home_msg(update: Update, chat_id: int) -> None:
    text, kb = view_home(chat_id)
    await update.effective_message.reply_text(
        text, parse_mode=HTML, reply_markup=MAIN_REPLY)
    await update.effective_message.reply_text(
        "What do you want to do?", parse_mode=HTML, reply_markup=kb)


def who(update: Update) -> str:
    user = update.effective_user
    if user is None:
        return ""
    return user.first_name or user.username or ""


async def notify_others(context: ContextTypes.DEFAULT_TYPE, hid: int,
                        exclude: int, text: str) -> None:
    for member in members_of(hid):
        if member["chat_id"] == exclude:
            continue
        try:
            await context.bot.send_message(member["chat_id"], text, parse_mode=HTML)
        except Exception as exc:
            log.warning("could not notify %s: %s", member["chat_id"], exc)


def store_phrase_from_draft(hid: int, draft: dict) -> str:
    sid = draft.get("store_id")
    if not sid:
        return "no store yet"
    store = store_by_id(hid, sid)
    if store is None:
        return "unknown store"
    if draft.get("spread"):
        return f"all {store['name']}"
    return store_label(store)


def finish_item(chat_id: int, hid: int, draft: dict, by_name: str) -> sqlite3.Row:
    return insert_item(
        hid,
        draft["name"],
        int(draft["store_id"]),
        int(draft.get("spread") or 0),
        int(draft.get("emergency") if draft.get("emergency") is not None else 5),
        qty=draft.get("qty") or "",
        notes=draft.get("notes") or "",
        by_id=chat_id,
        by_name=by_name,
        staple=int(draft.get("staple") or 0),
    )


async def finish_move(update: Update, context: ContextTypes.DEFAULT_TYPE,
                      hid: int, iid: int) -> None:
    draft = draft_of(context)
    sid = int(draft.get("store_id") or 0)
    spread = int(draft.get("spread") or 0)
    if sid:
        with closing(db()) as conn:
            attach_stores(conn, iid, sid, spread, replace=True)
            conn.commit()
    clear_draft(context)
    await show(update, *view_item(update.effective_chat.id, iid))


# --------------------------------------------------------------------------
# digest
# --------------------------------------------------------------------------

def job_name(chat_id: int) -> str:
    return f"digest:{chat_id}"


def schedule_digest(app: Application, chat_id: int) -> None:
    if app.job_queue is None:
        return
    for job in app.job_queue.get_jobs_by_name(job_name(chat_id)):
        job.schedule_removal()
    cfg = get_settings(chat_id)
    if cfg["paused"]:
        return
    hh, mm = (int(x) for x in cfg["digest_time"].split(":"))
    try:
        tz = ZoneInfo(cfg["tz"])
    except ZoneInfoNotFoundError:
        tz = ZoneInfo(DEFAULT_TZ)
    app.job_queue.run_daily(
        send_digest, time=dt.time(hour=hh, minute=mm, tzinfo=tz),
        name=job_name(chat_id), chat_id=chat_id,
    )


async def send_digest(context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = context.job.chat_id
    text, kb = view_go(chat_id, 8)
    await context.bot.send_message(chat_id, "🌅 <b>Morning cart</b> — 8+ urgency",
                                   parse_mode=HTML)
    await context.bot.send_message(chat_id, text, parse_mode=HTML, reply_markup=kb)


async def do_export(hid: int, send) -> bool:
    items = needed_items(hid)
    if not items:
        return False
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["item", "qty", "emergency", "stores", "staple", "claimed", "notes"])
    for row in items:
        writer.writerow([
            row["name"], row["qty"], row["emergency"],
            item_store_names(row["id"], hid),
            "yes" if row["staple"] else "no",
            row["claimed_by_name"], row["notes"],
        ])
    data = io.BytesIO(buf.getvalue().encode("utf-8"))
    data.seek(0)
    await send(document=InputFile(data, filename="paniccart.csv"))
    return True


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    payload = context.args[0] if context.args else ""
    if payload.startswith("join-"):
        return await do_join(update, context, payload[5:])
    household_for(chat_id, who(update))
    get_settings(chat_id)
    schedule_digest(context.application, chat_id)
    await show_home_msg(update, chat_id)


async def do_join(update: Update, context: ContextTypes.DEFAULT_TYPE, code: str) -> None:
    chat_id = update.effective_chat.id
    label = who(update)
    target = household_by_code(code)
    if target is None:
        await update.effective_message.reply_text("That invite code doesn't match any cart.")
        return
    old = household_for(chat_id, label)
    if old == target["id"]:
        await update.effective_message.reply_text("You're already on that cart.")
        return
    incoming = needed_count(old)
    move_member(chat_id, target["id"], label)
    if incoming:
        kb = InlineKeyboardMarkup([
            [btn(f"Bring {incoming} item(s) over", f"mrg:{old}")],
            [btn("Leave them behind", "home")],
        ])
        await update.effective_message.reply_text(
            f"Joined <b>{esc(target['name'])}</b>. Bring your old list along?",
            parse_mode=HTML, reply_markup=kb)
    else:
        await notify_others(context, target["id"], chat_id,
                            f"👋 <b>{esc(label or 'Someone')}</b> joined the cart.")
        await show_home_msg(update, chat_id)


async def cmd_join(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.effective_message.reply_text("Usage: /join ABC123")
        return
    await do_join(update, context, context.args[0])


async def cmd_invite(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show(update, *view_household(update.effective_chat.id, context.bot.username))


async def cmd_leave(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show(
        update,
        "🚪 Leave this household? You'll get a fresh empty cart. "
        "The shared one stays for everyone else.",
        InlineKeyboardMarkup([[btn("Yes, leave", "leave"), btn("Cancel", "house")]]),
    )


async def cmd_sethouse(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    hid = household_for(chat_id, who(update))
    name = " ".join(context.args).strip()
    if not name:
        set_await(context, "house_name")
        await update.effective_message.reply_text("Send the new household name.")
        return
    with closing(db()) as conn:
        conn.execute("UPDATE households SET name = ? WHERE id = ?", (name[:48], hid))
        conn.commit()
    await show(update, *view_household(chat_id, context.bot.username))


async def cmd_home(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clear_draft(context)
    await show_home_msg(update, update.effective_chat.id)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show(update, *view_help())


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    household_for(update.effective_chat.id, who(update))
    await show(update, *view_list(update.effective_chat.id))


async def cmd_stores(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    household_for(update.effective_chat.id, who(update))
    await show(update, *view_stores(update.effective_chat.id))


async def cmd_go(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    household_for(update.effective_chat.id, who(update))
    floor = 10
    if context.args:
        em = parse_emergency(context.args[0])
        if em is not None:
            floor = em
    await show(update, *view_go(update.effective_chat.id, floor))


async def cmd_at(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    hid = household_for(chat_id, who(update))
    if context.args:
        store = match_store(hid, " ".join(context.args))
        if store:
            sid = store["id"]
            if store["is_group"]:
                kids = child_stores(hid, sid)
                if kids:
                    rows = [[btn(f"{store_label(k)} · {len(items_for_store(hid, k['id']))}",
                                 f"at:{k['id']}:0")] for k in kids]
                    rows.append([btn("◀ All stores", "at")])
                    await show(
                        update,
                        f"{store['emoji']} <b>Which {esc(store['name'])}?</b>",
                        InlineKeyboardMarkup(rows),
                    )
                    return
            await show(update, *view_at_store(chat_id, sid, 0))
            return
    await show(update, *view_at_pick(chat_id))


async def cmd_roll(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    household_for(update.effective_chat.id, who(update))
    await show(update, *view_roll(update.effective_chat.id, 0))


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    household_for(update.effective_chat.id, who(update))
    await show(update, *view_stats(update.effective_chat.id))


async def cmd_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    get_settings(update.effective_chat.id)
    await show(update, *view_settings(update.effective_chat.id))


async def cmd_find(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    hid = household_for(chat_id, who(update))
    needle = " ".join(context.args).strip()
    if not needle:
        set_await(context, "find")
        await update.effective_message.reply_text("What should I search for?")
        return
    await show_find(update, hid, needle)


async def show_find(update: Update, hid: int, needle: str) -> None:
    rows = find_items(hid, needle)
    if not rows:
        await show(update, f"No needed items matching <b>{esc(needle)}</b>.",
                   InlineKeyboardMarkup([[btn("🏠 Home", "home")]]))
        return
    lines = [item_line(hid, r) for r in rows[:20]]
    kb_rows = [[btn(f"{r['name'][:32]}", f"it:{r['id']}")] for r in rows[:8]]
    kb_rows.append([btn("🏠 Home", "home")])
    await show(update, f"🔎 <b>{esc(needle)}</b>\n{RULE}\n" + "\n".join(lines),
               InlineKeyboardMarkup(kb_rows))


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    hid = household_for(chat_id, who(update))
    if context.args:
        line = " ".join(context.args)
        await add_from_text(update, context, hid, line)
        return
    clear_draft(context)
    set_await(context, "item_name")
    await update.effective_message.reply_text(
        "➕ What do we need?\n<i>Just the name — store and urgency are next.</i>",
        parse_mode=HTML,
    )


async def cmd_settime(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not context.args or not re.fullmatch(r"\d{1,2}:\d{2}", context.args[0]):
        await update.effective_message.reply_text("Usage: /settime 08:30")
        return
    hh, mm = context.args[0].split(":")
    stamp = f"{int(hh):02d}:{int(mm):02d}"
    update_settings(chat_id, digest_time=stamp, paused=0)
    schedule_digest(context.application, chat_id)
    await show(update, *view_settings(chat_id))


async def cmd_tz(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    if not context.args:
        await update.effective_message.reply_text("Usage: /tz America/Toronto")
        return
    name = context.args[0]
    try:
        ZoneInfo(name)
    except ZoneInfoNotFoundError:
        await update.effective_message.reply_text("Unknown timezone.")
        return
    update_settings(chat_id, tz=name)
    schedule_digest(context.application, chat_id)
    await show(update, *view_settings(chat_id))


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    update_settings(update.effective_chat.id, paused=1)
    schedule_digest(context.application, update.effective_chat.id)
    await show(update, *view_settings(update.effective_chat.id))


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    update_settings(update.effective_chat.id, paused=0)
    schedule_digest(context.application, update.effective_chat.id)
    await show(update, *view_settings(update.effective_chat.id))


async def cmd_export(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    hid = household_for(update.effective_chat.id, who(update))
    if not await do_export(hid, update.effective_message.reply_document):
        await update.effective_message.reply_text("Nothing to export yet.")


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    clear_draft(context)
    await update.effective_message.reply_text("Cancelled.")
    await show_home_msg(update, update.effective_chat.id)


# --------------------------------------------------------------------------
# add-item pipeline
# --------------------------------------------------------------------------

async def begin_add(update: Update, context: ContextTypes.DEFAULT_TYPE,
                    hid: int, name: str, qty: str = "") -> None:
    draft = draft_of(context)
    keep_store = draft.get("store_id")
    keep_spread = draft.get("spread")
    draft.clear()
    draft["name"] = name.strip()[:80]
    draft["qty"] = qty
    context.user_data["await"] = None
    if keep_store:
        draft["store_id"] = keep_store
        draft["spread"] = keep_spread or 0
        await show(update, *view_add_emergency(
            draft["name"], store_phrase_from_draft(hid, draft)))
        return
    await show(update, *view_add_stores(update.effective_chat.id, draft["name"]))


async def add_from_text(update: Update, context: ContextTypes.DEFAULT_TYPE,
                        hid: int, line: str) -> None:
    try:
        data = parse_add_line(hid, line)
    except ParseError as exc:
        await update.effective_message.reply_text(str(exc))
        return
    draft = draft_of(context)
    draft.clear()
    draft["name"] = data["name"]
    draft["qty"] = data.get("qty") or ""
    store = data.get("store")
    if store is not None:
        kids = child_stores(hid, store["id"])
        if kids and not data.get("emergency"):
            draft["parent_id"] = store["id"]
            await show(update, *view_add_group(
                update.effective_chat.id, store["id"], draft["name"]))
            return
        if kids and data.get("emergency") is not None:
            draft["store_id"] = store["id"]
            draft["spread"] = 1
        else:
            draft["store_id"] = store["id"]
            draft["spread"] = 0
        if data.get("emergency") is not None:
            draft["emergency"] = data["emergency"]
            item = finish_item(update.effective_chat.id, hid, draft, who(update))
            clear_draft(context)
            await announce_added(update, context, hid, item)
            return
        await show(update, *view_add_emergency(
            draft["name"], store_phrase_from_draft(hid, draft)))
        return
    await show(update, *view_add_stores(update.effective_chat.id, draft["name"]))


async def announce_added(update: Update, context: ContextTypes.DEFAULT_TYPE,
                         hid: int, item) -> None:
    phrase = item_store_names(item["id"], hid)
    text = (
        f"✅ <b>{esc(item['name'])}</b> is on the list\n"
        f"{em_label(item['emergency'])} · {esc(phrase)}"
        + (f" · × {esc(item['qty'])}" if item["qty"] else "")
    )
    kb = InlineKeyboardMarkup([
        [btn("🔁 Make staple", f"staple:{item['id']}"), btn("🔢 Qty", f"qty:{item['id']}")],
        [btn("➕ Add another", "addhint"), btn("📋 List", "list")],
        [btn("🏠 Home", "home")],
    ])
    await show(update, text, kb)
    await notify_others(
        context, hid, update.effective_chat.id,
        f"🛒 <b>{esc(who(update) or 'Someone')}</b> added <b>{esc(item['name'])}</b> "
        f"({em_label(item['emergency'])} · {esc(phrase)})",
    )


# --------------------------------------------------------------------------
# free text
# --------------------------------------------------------------------------

REPLY_MAP = {
    REPLY_HOME: "home",
    REPLY_ADD: "add",
    REPLY_AT: "at",
    REPLY_GO: "go",
    REPLY_LIST: "list",
    REPLY_STORES: "stores",
}


async def on_free_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    text = (update.effective_message.text or "").strip()
    if not text:
        return
    hid = household_for(chat_id, who(update))
    awaiting = context.user_data.get("await")

    if text in REPLY_MAP and awaiting not in {"item_name", "qty", "store_name",
                                              "store_rename", "house_name", "notes"}:
        clear_draft(context)
        key = REPLY_MAP[text]
        if key == "home":
            return await show_home_msg(update, chat_id)
        if key == "add":
            return await cmd_add(update, context)
        if key == "at":
            return await show(update, *view_at_pick(chat_id))
        if key == "go":
            return await show(update, *view_go(chat_id, 10))
        if key == "list":
            return await show(update, *view_list(chat_id))
        if key == "stores":
            return await show(update, *view_stores(chat_id))

    if awaiting == "item_name":
        context.user_data["await"] = None
        if "|" in text:
            return await add_from_text(update, context, hid, text)
        return await begin_add(update, context, hid, text)

    if awaiting == "qty":
        iid = int(context.user_data.get("await_id") or 0)
        set_item_fields(hid, iid, qty=parse_qty(text))
        context.user_data["await"] = None
        return await show(update, *view_item(chat_id, iid))

    if awaiting == "notes":
        iid = int(context.user_data.get("await_id") or 0)
        set_item_fields(hid, iid, notes=text[:200])
        context.user_data["await"] = None
        return await show(update, *view_item(chat_id, iid))

    if awaiting == "store_name":
        parent_id = int(context.user_data.get("await_id") or 0)
        is_group = 1 if context.user_data.get("await_group") else 0
        context.user_data.pop("await_group", None)
        context.user_data["await"] = None
        store = add_store(hid, text, parent_id=parent_id, is_group=is_group,
                          emoji="🏬" if is_group else "🏪")
        if draft_of(context).get("name"):
            if is_group or child_stores(hid, store["id"]):
                return await show(update, *view_add_group(chat_id, store["id"],
                                                          draft_of(context)["name"]))
            draft_of(context)["store_id"] = store["id"]
            draft_of(context)["spread"] = 0
            return await show(update, *view_add_emergency(
                draft_of(context)["name"], store_phrase_from_draft(hid, draft_of(context))))
        return await show(update, *view_store_edit(chat_id, store["id"]))

    if awaiting == "store_rename":
        sid = int(context.user_data.get("await_id") or 0)
        context.user_data["await"] = None
        rename_store(hid, sid, text)
        return await show(update, *view_store_edit(chat_id, sid))

    if awaiting == "house_name":
        context.user_data["await"] = None
        with closing(db()) as conn:
            conn.execute("UPDATE households SET name = ? WHERE id = ?",
                         (text[:48], hid))
            conn.commit()
        return await show(update, *view_household(chat_id, context.bot.username))

    if awaiting == "find":
        context.user_data["await"] = None
        return await show_find(update, hid, text)

    if "|" in text:
        return await add_from_text(update, context, hid, text)

    return await begin_add(update, context, hid, text)


# --------------------------------------------------------------------------
# callbacks
# --------------------------------------------------------------------------

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    chat_id = update.effective_chat.id
    hid = household_for(chat_id, who(update))
    data = query.data or ""
    await query.answer()

    if data == "noop":
        return
    if data == "home":
        clear_draft(context)
        return await show(update, *view_home(chat_id))
    if data == "list":
        return await show(update, *view_list(chat_id))
    if data == "stores":
        return await show(update, *view_stores(chat_id))
    if data == "at":
        return await show(update, *view_at_pick(chat_id))
    if data == "stats":
        return await show(update, *view_stats(chat_id))
    if data == "settings":
        return await show(update, *view_settings(chat_id))
    if data == "house":
        return await show(update, *view_household(chat_id, context.bot.username))
    if data == "roll":
        return await show(update, *view_roll(chat_id, 0))
    if data.startswith("roll:"):
        return await show(update, *view_roll(chat_id, int(data.split(":")[1])))
    if data.startswith("go:"):
        return await show(update, *view_go(chat_id, int(data.split(":")[1])))

    if data == "addhint":
        clear_draft(context)
        set_await(context, "item_name")
        return await show(
            update,
            "➕ <b>What do we need?</b>\n{0}\n"
            "Send the name, or a full line:\n"
            "<code>tahini | khorak | 9</code>".format(RULE),
            InlineKeyboardMarkup([[btn("✖ Cancel", "addcancel")]]),
        )
    if data == "addcancel":
        clear_draft(context)
        return await show(update, *view_home(chat_id))
    if data == "addstores":
        draft = draft_of(context)
        return await show(update, *view_add_stores(chat_id, draft.get("name", "")))

    if data.startswith("ag:"):
        parent_id = int(data[3:])
        draft = draft_of(context)
        return await show(update, *view_add_group(
            chat_id, parent_id, draft.get("name", "")))
    if data.startswith("aa:"):
        parent_id = int(data[3:])
        draft = draft_of(context)
        if not draft.get("name"):
            return await show(update, "Send the item name first.",
                              InlineKeyboardMarkup([[btn("➕ Add", "addhint")]]))
        draft["store_id"] = parent_id
        draft["spread"] = 1
        if draft.get("moving"):
            return await finish_move(update, context, hid, int(draft["moving"]))
        return await show(update, *view_add_emergency(
            draft["name"], store_phrase_from_draft(hid, draft)))
    if data.startswith("as:"):
        sid = int(data[3:])
        draft = draft_of(context)
        if not draft.get("name"):
            return await show(update, "Send the item name first.",
                              InlineKeyboardMarkup([[btn("➕ Add", "addhint")]]))
        draft["store_id"] = sid
        draft["spread"] = 0
        if draft.get("moving"):
            return await finish_move(update, context, hid, int(draft["moving"]))
        return await show(update, *view_add_emergency(
            draft["name"], store_phrase_from_draft(hid, draft)))
    if data.startswith("ae:"):
        draft = draft_of(context)
        if not draft.get("name") or not draft.get("store_id"):
            return await show(update, *view_add_stores(chat_id))
        draft["emergency"] = int(data[3:])
        item = finish_item(chat_id, hid, draft, who(update))
        clear_draft(context)
        return await announce_added(update, context, hid, item)

    if data.startswith("addat:"):
        sid = int(data[6:])
        store = store_by_id(hid, sid)
        draft = draft_of(context)
        draft.clear()
        if store:
            draft["store_id"] = sid
            draft["spread"] = 0
        set_await(context, "item_name")
        where = store_label(store) if store else "the store"
        return await show(
            update,
            f"➕ Adding to <b>{esc(where)}</b>\nSend the item name.",
            InlineKeyboardMarkup([[btn("✖ Cancel", "addcancel")]]),
        )

    if data.startswith("at:"):
        _, sid, page = data.split(":")
        return await show(update, *view_at_store(chat_id, int(sid), int(page)))

    if data.startswith("it:"):
        return await show(update, *view_item(chat_id, int(data[3:])))

    if data.startswith("ck:"):
        parts = data.split(":")
        iid = int(parts[1])
        sid = int(parts[2]) if len(parts) > 2 else 0
        item = get_item(hid, iid)
        store = store_by_id(hid, sid) if sid else None
        store_name = store["name"] if store else item_store_names(iid, hid)
        if item:
            mark_bought(hid, iid, store_name, who(update))
            await notify_others(
                context, hid, chat_id,
                f"✅ <b>{esc(who(update) or 'Someone')}</b> grabbed "
                f"<b>{esc(item['name'])}</b> at {esc(store_name)}",
            )
        if sid:
            return await show(update, *view_at_store(chat_id, sid, 0))
        return await show(update, *view_list(chat_id))

    if data.startswith("claim:"):
        iid = int(data[6:])
        set_item_fields(hid, iid, claimed_by=chat_id, claimed_by_name=who(update))
        item = get_item(hid, iid)
        if item:
            await notify_others(
                context, hid, chat_id,
                f"🙋 <b>{esc(who(update) or 'Someone')}</b> will grab "
                f"<b>{esc(item['name'])}</b>",
            )
        return await show(update, *view_item(chat_id, iid))

    if data.startswith("staple:"):
        iid = int(data[7:])
        row = get_item(hid, iid)
        if row:
            set_item_fields(hid, iid, staple=0 if row["staple"] else 1)
        return await show(update, *view_item(chat_id, iid))

    if data.startswith("qty:"):
        iid = int(data[4:])
        set_await(context, "qty", iid)
        return await show(
            update, "🔢 Send quantity (e.g. <code>2 bags</code> or <code>x3</code>).",
            InlineKeyboardMarkup([[btn("◀ Back", f"it:{iid}")]]))

    if data.startswith("emenu:"):
        iid = int(data[6:])
        row = get_item(hid, iid)
        if row is None:
            return await show(update, *view_list(chat_id))
        return await show(update, *view_emergency_edit(iid, row["name"], row["emergency"]))

    if data.startswith("ie:"):
        _, iid_s, n_s = data.split(":")
        iid, n = int(iid_s), int(n_s)
        set_item_fields(hid, iid, emergency=n)
        return await show(update, *view_item(chat_id, iid))

    if data.startswith("move:"):
        iid = int(data[5:])
        row = get_item(hid, iid)
        if row is None:
            return await show(update, *view_list(chat_id))
        draft = draft_of(context)
        draft.clear()
        draft["name"] = row["name"]
        draft["qty"] = row["qty"]
        draft["emergency"] = row["emergency"]
        draft["moving"] = iid
        return await show(update, *view_add_stores(chat_id, row["name"]))

    if data.startswith("askdel:"):
        iid = int(data[7:])
        row = get_item(hid, iid)
        if row is None:
            return await show(update, *view_list(chat_id))
        return await show(
            update,
            f"🗑 Drop <b>{esc(row['name'])}</b> from the cart?",
            InlineKeyboardMarkup([[btn("Yes, drop", f"del:{iid}"), btn("Cancel", f"it:{iid}")]]),
        )
    if data.startswith("del:"):
        iid = int(data[4:])
        drop_item(hid, iid)
        return await show(update, *view_list(chat_id))

    if data.startswith("tripdone:"):
        sid = int(data[9:])
        store = store_by_id(hid, sid)
        leftover = items_for_store(hid, sid) if store else []
        name = store["name"] if store else "the store"
        if leftover:
            lines = "\n".join(
                f"{em_mark(i['emergency'])} {esc(i['name'])}" for i in leftover[:20]
            )
            text = (
                f"🧾 Left at <b>{esc(name)}</b>\n{RULE}\n{lines}\n\n"
                f"<i>{len(leftover)} still needed — next time, or another store.</i>"
            )
        else:
            text = (
                f"🎉 <b>{esc(name)} is clear.</b>\n{RULE}\n"
                "Nothing left on this list. Go home a hero."
            )
        return await show(update, text, InlineKeyboardMarkup([
            [btn("🚨 Where next?", "go:10"), btn("🏠 Home", "home")],
        ]))

    if data.startswith("stedit:"):
        return await show(update, *view_store_edit(chat_id, int(data[7:])))
    if data.startswith("stadd:"):
        parent_id = int(data[6:])
        set_await(context, "store_name", parent_id)
        context.user_data["await_group"] = 0
        hint = "Send the new store name."
        if parent_id:
            parent = store_by_id(hid, parent_id)
            if parent:
                hint = f"Send the name to add under <b>{esc(parent['name'])}</b>."
        return await show(update, hint, InlineKeyboardMarkup([[btn("✖ Cancel", "stores")]]))
    if data == "stgroup":
        set_await(context, "store_name", 0)
        context.user_data["await_group"] = 1
        return await show(
            update, "Send a group name (e.g. <code>Farmer's markets</code>). You can nest stores under it.",
            InlineKeyboardMarkup([[btn("✖ Cancel", "stores")]]))
    if data.startswith("stren:"):
        sid = int(data[6:])
        set_await(context, "store_rename", sid)
        return await show(update, "Send the new name.",
                          InlineKeyboardMarkup([[btn("✖ Cancel", f"stedit:{sid}")]]))
    if data.startswith("starch:"):
        _, sid_s, flag = data.split(":")
        set_archived(hid, int(sid_s), int(flag))
        return await show(update, *view_store_edit(chat_id, int(sid_s)))

    if data == "renamehint":
        set_await(context, "house_name")
        return await show(update, "Send the new household name.",
                          InlineKeyboardMarkup([[btn("Cancel", "house")]]))
    if data == "askleave":
        return await show(
            update,
            "🚪 <b>Leave this household?</b>\nYou'll start a fresh cart. "
            "The shared one stays for everyone else.",
            InlineKeyboardMarkup([[btn("Yes, leave", "leave"), btn("Cancel", "house")]]))
    if data == "leave":
        label = who(update)
        with closing(db()) as conn:
            conn.execute("DELETE FROM members WHERE chat_id = ?", (chat_id,))
            conn.commit()
        await notify_others(context, hid, chat_id,
                            f"👋 <b>{esc(label or 'Someone')}</b> left the cart.")
        household_for(chat_id, label)
        return await show(update, *view_home(chat_id))

    if data.startswith("mrg:"):
        src = int(data[4:])
        if src != hid:
            merge_households(src, hid)
        await notify_others(
            context, hid, chat_id,
            f"📦 <b>{esc(who(update) or 'Someone')}</b> brought their list into the shared cart.",
        )
        return await show(update, *view_home(chat_id))

    if data.startswith("time:"):
        update_settings(chat_id, digest_time=data[5:], paused=0)
        schedule_digest(context.application, chat_id)
        return await show(update, *view_settings(chat_id))
    if data == "togglepause":
        cfg = get_settings(chat_id)
        update_settings(chat_id, paused=0 if cfg["paused"] else 1)
        schedule_digest(context.application, chat_id)
        return await show(update, *view_settings(chat_id))

    if data == "export":
        if not await do_export(hid, query.message.reply_document):
            await query.message.reply_text("Nothing to export yet.")
        return

    if data == "restock":
        with closing(db()) as conn:
            n = conn.execute(
                "UPDATE items SET status = 'needed', bought_at = NULL WHERE household_id = ? "
                "AND staple = 1 AND status = 'bought'",
                (hid,),
            ).rowcount
            conn.commit()
        await query.message.reply_text(
            f"🔁 Restocked {n} staple" + ("s" if n != 1 else "") + "."
        )
        return await show(update, *view_list(chat_id))


# --------------------------------------------------------------------------
# wiring
# --------------------------------------------------------------------------

async def post_init(app: Application) -> None:
    await app.bot.set_my_commands([
        BotCommand("home", "Dashboard"),
        BotCommand("add", "Add an item"),
        BotCommand("list", "Full list by urgency"),
        BotCommand("go", "Where to go (panic items)"),
        BotCommand("at", "I'm at a store"),
        BotCommand("stores", "Edit store candidates"),
        BotCommand("roll", "Grab the most urgent thing"),
        BotCommand("find", "Search the list"),
        BotCommand("stats", "What's been grabbed"),
        BotCommand("invite", "Share the cart"),
        BotCommand("join", "Join a shared cart"),
        BotCommand("household", "Who's on this cart"),
        BotCommand("settings", "Digest time and timezone"),
        BotCommand("help", "How this works"),
    ])
    with closing(db()) as conn:
        chat_ids = [r["chat_id"] for r in conn.execute("SELECT chat_id FROM members")]
    for chat_id in chat_ids:
        get_settings(chat_id)
        schedule_digest(app, chat_id)
    log.info("restored %d digest job(s)", len(chat_ids))


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.exception("handler error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text("Something broke on my end.")


def main() -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN first (get one from @BotFather).")

    init_db()
    app = ApplicationBuilder().token(token).post_init(post_init).build()

    for name, fn in [
        ("start", cmd_start), ("home", cmd_home), ("help", cmd_help),
        ("add", cmd_add), ("list", cmd_list), ("stores", cmd_stores),
        ("go", cmd_go), ("where", cmd_go), ("panic", cmd_go),
        ("at", cmd_at), ("shop", cmd_at),
        ("roll", cmd_roll), ("find", cmd_find), ("stats", cmd_stats),
        ("settings", cmd_settings),
        ("invite", cmd_invite), ("join", cmd_join), ("household", cmd_invite),
        ("leave", cmd_leave), ("sethouse", cmd_sethouse),
        ("settime", cmd_settime), ("tz", cmd_tz),
        ("pause", cmd_pause), ("resume", cmd_resume), ("export", cmd_export),
        ("cancel", cmd_cancel),
    ]:
        app.add_handler(CommandHandler(name, fn))

    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_free_text))
    app.add_error_handler(on_error)

    log.info("PanicCart running — db at %s", DB_PATH)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
