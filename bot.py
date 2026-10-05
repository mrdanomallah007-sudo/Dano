"""
IVA + CR API — Unified OTP Telegram Bot
========================================
Pool flow (botv3):
  /start → Service → Country → Number Assigned → OTP via CR API ✅

Direct watch flow (cr_otp_bot):
  /addnumber → /watchotp → OTP auto-delivered ✅

Bulk import:
  /bulkimport → send .txt/.csv → 10K+ numbers ✅

Admin:
  /import /addnumbers /delete /poolstatus /massregister /settoken

CR API:  http://51.77.216.195/crapi/mait/viewstats
  - Primary OTP source for ALL flows
  - Replaces ivasms polling entirely
  - Token set via /settoken or .env CR_API_TOKEN
"""

from dotenv import load_dotenv
load_dotenv()

import io
import re
import logging
import os
import sys
import sqlite3
import asyncio
import socket as _socket
import time as _time
from datetime import datetime, timezone, timedelta

import httpx
from aiohttp import web
from telegram import Update, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, CopyTextButton
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────
BOT_TOKEN      = os.getenv("TG_BOT_TOKEN",    "YOUR_BOT_TOKEN_HERE")
API_BASE       = os.getenv("API_BASE_URL",    "http://127.0.0.1:8000")   # legacy ivasms (optional)
NOTIFY_HOST    = os.getenv("NOTIFY_HOST",     "0.0.0.0")
NOTIFY_PORT    = int(os.getenv("NOTIFY_PORT", "8080"))
NOTIFY_SECRET  = os.getenv("NOTIFY_SECRET",  "")
PROXY_URL: str | None = os.getenv("PROXY", "").strip() or None

# CR API
CR_API_URL     = "http://51.77.216.195/crapi/mait/viewstats"
CR_API_TOKEN   = os.getenv("CR_API_TOKEN", "")       # set here or via /settoken
CR_POLL_SEC    = int(os.getenv("CR_POLL_SEC", "6"))  # OTP poll interval — 6s

# Admin
_admin_env = os.getenv("ADMIN_IDS", "")
ADMIN_IDS: set[int] = {int(x.strip()) for x in _admin_env.split(",") if x.strip().isdigit()}

# OTP forward channel
_ch_env = os.getenv("OTP_CHANNEL_ID", "").strip()
OTP_CHANNEL_ID: int | None = int(_ch_env) if _ch_env.lstrip("-").isdigit() else None

# OTP forward group (link for button)
OTP_GROUP_LINK: str = os.getenv("OTP_GROUP_LINK", "").strip()

# Required group join
_rg_env = os.getenv("REQUIRED_GROUP_ID", "").strip()
REQUIRED_GROUP_ID: int | None   = int(_rg_env) if _rg_env.lstrip("-").isdigit() else None
REQUIRED_GROUP_LINK: str        = os.getenv("REQUIRED_GROUP_LINK", "").strip()

# ─────────────────────────────────────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────────────────────────────────────
logging.basicConfig(format="%(asctime)s [%(levelname)s] %(name)s: %(message)s", level=logging.INFO)
logger = logging.getLogger("unified_bot")
for _noisy in ("httpx","httpcore","telegram","telegram.ext","apscheduler","aiohttp","asyncio","charset_normalizer"):
    logging.getLogger(_noisy).setLevel(logging.ERROR)

_app: Application | None = None
_pending_import: dict[int, dict] = {}   # admin import wizard state
_pending_bulk:   dict[int, bool] = {}   # bulk number input state
_pending_2fa_input: dict[int, bool] = {}  # waiting for secret key input
_pending_getotp:    dict[int, bool] = {}  # waiting for number input (/getotp)
_last_sent_otp:  dict[str, str]  = {}   # number → key (dedup)

# ─────────────────────────────────────────────────────────────────────────────
# Services & Countries
# ─────────────────────────────────────────────────────────────────────────────
SERVICES = [
    {"id": "fb_reg", "label": "FB Create New", "emoji": "📘"},
    {"id": "fb_pc",  "label": "PC Clone",       "emoji": "🖥️"},
    {"id": "wa",     "label": "WhatsApp",        "emoji": "💬"},
    {"id": "gmail",  "label": "Gmail",           "emoji": "📧"},
    {"id": "other",  "label": "Other",           "emoji": "🔑"},
]
SERVICE_MAP = {s["id"]: s for s in SERVICES}

ALL_COUNTRIES = [
    {"id":"AF","label":"Afghanistan","flag":"🇦🇫"},{"id":"AL","label":"Albania","flag":"🇦🇱"},
    {"id":"DZ","label":"Algeria","flag":"🇩🇿"},{"id":"AR","label":"Argentina","flag":"🇦🇷"},
    {"id":"AM","label":"Armenia","flag":"🇦🇲"},{"id":"AU","label":"Australia","flag":"🇦🇺"},
    {"id":"AT","label":"Austria","flag":"🇦🇹"},{"id":"AZ","label":"Azerbaijan","flag":"🇦🇿"},
    {"id":"BH","label":"Bahrain","flag":"🇧🇭"},{"id":"BD","label":"Bangladesh","flag":"🇧🇩"},
    {"id":"BY","label":"Belarus","flag":"🇧🇾"},{"id":"BE","label":"Belgium","flag":"🇧🇪"},
    {"id":"BR","label":"Brazil","flag":"🇧🇷"},{"id":"BG","label":"Bulgaria","flag":"🇧🇬"},
    {"id":"CA","label":"Canada","flag":"🇨🇦"},{"id":"CL","label":"Chile","flag":"🇨🇱"},
    {"id":"CN","label":"China","flag":"🇨🇳"},{"id":"CO","label":"Colombia","flag":"🇨🇴"},
    {"id":"HR","label":"Croatia","flag":"🇭🇷"},{"id":"CY","label":"Cyprus","flag":"🇨🇾"},
    {"id":"CZ","label":"Czech Republic","flag":"🇨🇿"},{"id":"DK","label":"Denmark","flag":"🇩🇰"},
    {"id":"EG","label":"Egypt","flag":"🇪🇬"},{"id":"EE","label":"Estonia","flag":"🇪🇪"},
    {"id":"ET","label":"Ethiopia","flag":"🇪🇹"},{"id":"FI","label":"Finland","flag":"🇫🇮"},
    {"id":"FR","label":"France","flag":"🇫🇷"},{"id":"GE","label":"Georgia","flag":"🇬🇪"},
    {"id":"DE","label":"Germany","flag":"🇩🇪"},{"id":"GH","label":"Ghana","flag":"🇬🇭"},
    {"id":"GR","label":"Greece","flag":"🇬🇷"},{"id":"GT","label":"Guatemala","flag":"🇬🇹"},
    {"id":"HK","label":"Hong Kong","flag":"🇭🇰"},{"id":"HU","label":"Hungary","flag":"🇭🇺"},
    {"id":"IN","label":"India","flag":"🇮🇳"},{"id":"ID","label":"Indonesia","flag":"🇮🇩"},
    {"id":"IQ","label":"Iraq","flag":"🇮🇶"},{"id":"IE","label":"Ireland","flag":"🇮🇪"},
    {"id":"IL","label":"Israel","flag":"🇮🇱"},{"id":"IT","label":"Italy","flag":"🇮🇹"},
    {"id":"JP","label":"Japan","flag":"🇯🇵"},{"id":"JO","label":"Jordan","flag":"🇯🇴"},
    {"id":"KZ","label":"Kazakhstan","flag":"🇰🇿"},{"id":"KE","label":"Kenya","flag":"🇰🇪"},
    {"id":"KW","label":"Kuwait","flag":"🇰🇼"},{"id":"LB","label":"Lebanon","flag":"🇱🇧"},
    {"id":"LY","label":"Libya","flag":"🇱🇾"},{"id":"MY","label":"Malaysia","flag":"🇲🇾"},
    {"id":"MX","label":"Mexico","flag":"🇲🇽"},{"id":"MA","label":"Morocco","flag":"🇲🇦"},
    {"id":"MM","label":"Myanmar","flag":"🇲🇲"},{"id":"NP","label":"Nepal","flag":"🇳🇵"},
    {"id":"NL","label":"Netherlands","flag":"🇳🇱"},{"id":"NZ","label":"New Zealand","flag":"🇳🇿"},
    {"id":"NG","label":"Nigeria","flag":"🇳🇬"},{"id":"NO","label":"Norway","flag":"🇳🇴"},
    {"id":"OM","label":"Oman","flag":"🇴🇲"},{"id":"PK","label":"Pakistan","flag":"🇵🇰"},
    {"id":"PS","label":"Palestine","flag":"🇵🇸"},{"id":"PE","label":"Peru","flag":"🇵🇪"},
    {"id":"PH","label":"Philippines","flag":"🇵🇭"},{"id":"PL","label":"Poland","flag":"🇵🇱"},
    {"id":"PT","label":"Portugal","flag":"🇵🇹"},{"id":"QA","label":"Qatar","flag":"🇶🇦"},
    {"id":"RO","label":"Romania","flag":"🇷🇴"},{"id":"RU","label":"Russia","flag":"🇷🇺"},
    {"id":"SA","label":"Saudi Arabia","flag":"🇸🇦"},{"id":"SN","label":"Senegal","flag":"🇸🇳"},
    {"id":"RS","label":"Serbia","flag":"🇷🇸"},{"id":"SG","label":"Singapore","flag":"🇸🇬"},
    {"id":"ZA","label":"South Africa","flag":"🇿🇦"},{"id":"KR","label":"South Korea","flag":"🇰🇷"},
    {"id":"ES","label":"Spain","flag":"🇪🇸"},{"id":"LK","label":"Sri Lanka","flag":"🇱🇰"},
    {"id":"SD","label":"Sudan","flag":"🇸🇩"},{"id":"SE","label":"Sweden","flag":"🇸🇪"},
    {"id":"CH","label":"Switzerland","flag":"🇨🇭"},{"id":"SY","label":"Syria","flag":"🇸🇾"},
    {"id":"TW","label":"Taiwan","flag":"🇹🇼"},{"id":"TZ","label":"Tanzania","flag":"🇹🇿"},
    {"id":"TH","label":"Thailand","flag":"🇹🇭"},{"id":"TN","label":"Tunisia","flag":"🇹🇳"},
    {"id":"TR","label":"Turkey","flag":"🇹🇷"},{"id":"TM","label":"Turkmenistan","flag":"🇹🇲"},
    {"id":"AE","label":"UAE","flag":"🇦🇪"},{"id":"UG","label":"Uganda","flag":"🇺🇬"},
    {"id":"UA","label":"Ukraine","flag":"🇺🇦"},{"id":"UK","label":"United Kingdom","flag":"🇬🇧"},
    {"id":"US","label":"United States","flag":"🇺🇸"},{"id":"UZ","label":"Uzbekistan","flag":"🇺🇿"},
    {"id":"VE","label":"Venezuela","flag":"🇻🇪"},{"id":"VN","label":"Vietnam","flag":"🇻🇳"},
    {"id":"YE","label":"Yemen","flag":"🇾🇪"},{"id":"ZM","label":"Zambia","flag":"🇿🇲"},
    {"id":"ZW","label":"Zimbabwe","flag":"🇿🇼"},
]
COUNTRY_MAP = {c["id"]: c for c in ALL_COUNTRIES}
_enabled_env = os.getenv("ENABLED_COUNTRIES", "")
_enabled_set = {x.strip().upper() for x in _enabled_env.split(",") if x.strip()}
COUNTRIES    = [c for c in ALL_COUNTRIES if c["id"] in _enabled_set] if _enabled_set else ALL_COUNTRIES

# ─────────────────────────────────────────────────────────────────────────────
# Proxy helpers
# ─────────────────────────────────────────────────────────────────────────────
def _parse_proxy(raw: str) -> str | None:
    if not raw: return None
    raw = raw.strip()
    if raw.startswith(("http://","https://","socks")): return raw
    parts = raw.split(":")
    if len(parts) == 4:
        h,p,u,pw = parts
        return f"http://{u}:{pw}@{h}:{p}"
    if len(parts) == 2: return f"http://{raw}"
    return raw

def _httpx_client(**kw) -> httpx.AsyncClient:
    if PROXY_URL:
        kw.setdefault("proxy", _parse_proxy(PROXY_URL))
    return httpx.AsyncClient(**kw)

def _internet_available(timeout: float = 3.0) -> bool:
    for host, port in [("8.8.8.8", 53), ("1.1.1.1", 53)]:
        try:
            s = _socket.create_connection((host, port), timeout=timeout)
            s.close(); return True
        except OSError: continue
    return False

def _is_network_error(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(h in msg for h in ("connect","network","unreachable","timed out","timeout",
                                   "remotedisconnected","eof","getaddrinfo","nodename"))

async def _wait_for_internet(label: str = "") -> bool:
    start = _time.time()
    while _time.time() - start < 300:
        if await asyncio.to_thread(_internet_available):
            return True
        await asyncio.sleep(10)
    return False

# ─────────────────────────────────────────────────────────────────────────────
# Database — single unified DB
# ─────────────────────────────────────────────────────────────────────────────
DB_PATH = os.getenv("BOT_DB_PATH", "unified_bot.sqlite3")

def _db():
    conn = sqlite3.connect(DB_PATH, timeout=30, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL;")
    # Migration: drop UNIQUE constraint on number if exists (allow duplicates)
    try:
        conn.execute("DROP INDEX IF EXISTS sqlite_autoindex_number_pool_1")
    except Exception: pass
    try:
        # Rebuild table without UNIQUE if needed (SQLite limitation workaround)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS number_pool_new (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                number       TEXT    NOT NULL,
                pool_service TEXT    NOT NULL,
                pool_country TEXT    NOT NULL,
                status       TEXT    NOT NULL DEFAULT 'available',
                assigned_to  INTEGER,
                assigned_at  TEXT,
                otp          TEXT,
                otp_at       TEXT,
                imported_at  TEXT NOT NULL
            )
        """)
        # Check if old table has UNIQUE constraint
        tbl_info = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='number_pool'").fetchone()
        if tbl_info and 'UNIQUE' in (tbl_info[0] or ''):
            # Migrate data
            conn.execute("INSERT OR IGNORE INTO number_pool_new SELECT * FROM number_pool")
            conn.execute("DROP TABLE number_pool")
            conn.execute("ALTER TABLE number_pool_new RENAME TO number_pool")
        else:
            conn.execute("DROP TABLE IF EXISTS number_pool_new")
    except Exception: pass
    # Pool table (botv3 style)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS number_pool (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            number       TEXT    NOT NULL,
            pool_service TEXT    NOT NULL,
            pool_country TEXT    NOT NULL,
            status       TEXT    NOT NULL DEFAULT 'available',
            assigned_to  INTEGER,
            assigned_at  TEXT,
            otp          TEXT,
            otp_at       TEXT,
            imported_at  TEXT NOT NULL
        )
    """)
    # User history
    conn.execute("""
        CREATE TABLE IF NOT EXISTS user_history (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_user_id    INTEGER NOT NULL,
            tg_username   TEXT,
            number        TEXT    NOT NULL,
            service_id    TEXT    NOT NULL,
            service_label TEXT    NOT NULL,
            country_id    TEXT    NOT NULL,
            country_label TEXT    NOT NULL,
            country_flag  TEXT    NOT NULL DEFAULT '',
            otp           TEXT,
            assigned_at   TEXT    NOT NULL,
            otp_at        TEXT
        )
    """)
    # watched_numbers table removed
    # OTP history (CR API)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS otp_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            number      TEXT    NOT NULL,
            otp         TEXT    NOT NULL,
            message     TEXT    DEFAULT '',
            cli         TEXT    DEFAULT '',
            received_at TEXT    NOT NULL
        )
    """)
    # Settings
    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)
    # 2FA keys
    conn.execute("""
        CREATE TABLE IF NOT EXISTS twofa_keys (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            tg_user_id INTEGER NOT NULL,
            label      TEXT    NOT NULL DEFAULT '',
            secret_key TEXT    NOT NULL UNIQUE,
            added_at   TEXT    NOT NULL
        )
    """)
    return conn

# ── 2FA helpers — TEMPORARY (in-memory, not permanently saved) ───────────────
# Key sirf session mein rehti hai — bot restart pe clear hogi
# User har baar /2fa se fresh key enter karega — no DB storage
_temp_2fa: dict[int, list[dict]] = {}   # {user_id: [{id, secret, label, added_at}]}
_temp_2fa_counter: dict = {}            # auto-increment ID per user

def twofa_add(tg_user_id: int, secret_key: str, label: str = "") -> bool:
    """
    2FA key TEMPORARILY save karo (memory only).
    DB mein store nahi hogi — bot restart pe clear.
    Returns True if new.
    """
    import re as _re
    clean = _re.sub(r"[^A-Z2-7=]", "", secret_key.upper().replace(" ", ""))
    if not clean or len(clean) < 8:
        return False
    # Check if already in temp list
    existing = _temp_2fa.get(tg_user_id, [])
    if any(k["secret"] == clean for k in existing):
        return False   # already exists
    _temp_2fa_counter[tg_user_id] = _temp_2fa_counter.get(tg_user_id, 0) + 1
    new_id = _temp_2fa_counter[tg_user_id]
    entry = {
        "id":       new_id,
        "label":    label,
        "secret":   clean,
        "added_at": datetime.now(timezone.utc).isoformat(),
    }
    _temp_2fa.setdefault(tg_user_id, []).insert(0, entry)   # newest first
    return True

def twofa_list(tg_user_id: int) -> list[dict]:
    """Current session ki 2FA keys return karo."""
    return list(_temp_2fa.get(tg_user_id, []))

def twofa_delete(tg_user_id: int, key_id: int) -> bool:
    """Key delete karo (current session se)."""
    keys = _temp_2fa.get(tg_user_id, [])
    before = len(keys)
    _temp_2fa[tg_user_id] = [k for k in keys if k["id"] != key_id]
    return len(_temp_2fa[tg_user_id]) < before

def twofa_clear_all(tg_user_id: int) -> int:
    """Saari keys clear karo (session mein)."""
    n = len(_temp_2fa.get(tg_user_id, []))
    _temp_2fa.pop(tg_user_id, None)
    return n

def twofa_get_code(secret_key: str) -> str:
    """Generate current 6-digit TOTP code from secret key."""
    import hmac, hashlib, struct, time as _t
    import base64 as _b64
    # Pad key to valid base32
    key = secret_key.upper().replace(" ", "")
    pad = (8 - len(key) % 8) % 8
    key += "=" * pad
    try:
        key_bytes = _b64.b32decode(key)
    except Exception:
        raise ValueError("Invalid 2FA key format")
    # TOTP counter (30-second window)
    counter = int(_t.time()) // 30
    msg = struct.pack(">Q", counter)
    h = hmac.new(key_bytes, msg, hashlib.sha1).digest()
    offset = h[-1] & 0x0F
    code = struct.unpack(">I", h[offset:offset+4])[0] & 0x7FFFFFFF
    return str(code % 1000000).zfill(6)

def twofa_get_remaining() -> int:
    """Seconds remaining in current 30-second TOTP window."""
    import time as _t
    return 30 - (int(_t.time()) % 30)

# ── Settings ─────────────────────────────────────────────────────────────────
def db_get_setting(key: str, default: str = "") -> str:
    with _db() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r[0] if r else default

def db_set_setting(key: str, value: str):
    with _db() as c:
        c.execute("INSERT OR REPLACE INTO settings (key,value) VALUES (?,?)", (key, value))

# ── Pool helpers (botv3) ──────────────────────────────────────────────────────
def pool_import(numbers: list[str], service_id: str, country_id: str,
                allow_dupes: bool = True) -> tuple[int, int]:
    """
    Insert numbers into pool.
    allow_dupes=True  → sab insert (same number multiple times) — DEFAULT
    allow_dupes=False → existing numbers skip karo (unique only)
    Returns (inserted, skipped)
    """
    inserted = skipped = 0
    now = datetime.now(timezone.utc).isoformat()
    with _db() as c:
        for raw in numbers:
            num = re.sub(r"[^\d]", "", raw.strip())
            if not num or len(num) < 7: continue
            if not allow_dupes:
                # Check if already exists in this service+country pool (available)
                exists = c.execute(
                    "SELECT 1 FROM number_pool WHERE number=? AND pool_service=? AND pool_country=? AND status='available'",
                    (num, service_id, country_id)
                ).fetchone()
                if exists:
                    skipped += 1
                    continue
            c.execute(
                "INSERT INTO number_pool (number,pool_service,pool_country,status,imported_at) VALUES (?,?,?,'available',?)",
                (num, service_id, country_id, now)
            )
            inserted += 1
    return inserted, skipped

def pool_count(service_id: str | None = None, country_id: str | None = None) -> int:
    sql = "SELECT COUNT(*) FROM number_pool WHERE status='available'"
    p = []
    if service_id: sql += " AND pool_service=?"; p.append(service_id)
    if country_id: sql += " AND pool_country=?";  p.append(country_id)
    with _db() as c:
        r = c.execute(sql, p).fetchone()
    return r[0] if r else 0

def pool_countries_for_service(service_id: str) -> list[tuple[str, int]]:
    with _db() as c:
        rows = c.execute(
            "SELECT pool_country,COUNT(*) FROM number_pool WHERE status='available' AND pool_service=? GROUP BY pool_country ORDER BY COUNT(*) DESC",
            (service_id,)
        ).fetchall()
    return [(r[0], r[1]) for r in rows]

def pool_counts_all() -> dict:
    result = {s["id"]: {} for s in SERVICES}
    with _db() as c:
        rows = c.execute("SELECT pool_service,pool_country,status,COUNT(*) FROM number_pool GROUP BY pool_service,pool_country,status").fetchall()
    for svc, cty, status, cnt in rows:
        if svc in result:
            result[svc].setdefault(cty, {"available":0,"assigned":0,"otp_received":0})
            result[svc][cty][status] = cnt
    return result

def pool_assign(tg_user_id: int, tg_username: str | None, service: dict, country: dict) -> str | None:
    now = datetime.now(timezone.utc).isoformat()
    with _db() as c:
        row = c.execute(
            "SELECT id,number FROM number_pool WHERE status='available' AND pool_service=? AND pool_country=? ORDER BY id ASC LIMIT 1",
            (service["id"], country["id"])
        ).fetchone()
        if not row: return None
        pid, number = row
        c.execute("UPDATE number_pool SET status='assigned',assigned_to=?,assigned_at=? WHERE id=?",
                  (tg_user_id, now, pid))
        c.execute("""INSERT INTO user_history
            (tg_user_id,tg_username,number,service_id,service_label,country_id,country_label,country_flag,assigned_at)
            VALUES (?,?,?,?,?,?,?,?,?)""",
            (tg_user_id, tg_username or "", number,
             service["id"], service["label"], country["id"], country["label"], country["flag"], now))
    return number

def pool_assign_dual(tg_user_id: int, tg_username: str | None,
                     service: dict, country: dict) -> tuple[str | None, str | None]:
    """2 numbers ek saath assign karo. Returns (num1, num2) — num2 can be None if only 1 available."""
    now = datetime.now(timezone.utc).isoformat()
    numbers = []
    with _db() as c:
        rows = c.execute(
            "SELECT id,number FROM number_pool WHERE status='available' AND pool_service=? AND pool_country=? ORDER BY id ASC LIMIT 2",
            (service["id"], country["id"])
        ).fetchall()
        for pid, number in rows:
            c.execute("UPDATE number_pool SET status='assigned',assigned_to=?,assigned_at=? WHERE id=?",
                      (tg_user_id, now, pid))
            c.execute("""INSERT INTO user_history
                (tg_user_id,tg_username,number,service_id,service_label,country_id,country_label,country_flag,assigned_at)
                VALUES (?,?,?,?,?,?,?,?,?)""",
                (tg_user_id, tg_username or "", number,
                 service["id"], service["label"], country["id"], country["label"], country["flag"], now))
            numbers.append(number)
    n1 = numbers[0] if len(numbers) > 0 else None
    n2 = numbers[1] if len(numbers) > 1 else None
    return n1, n2


def pool_save_otp(number: str, otp: str) -> tuple[int | None, str, str]:
    now = datetime.now(timezone.utc).isoformat()
    with _db() as c:
        row = c.execute("SELECT assigned_to FROM number_pool WHERE number=?", (number,)).fetchone()
        if not row or not row[0]: return None, "—", "—"
        tg_uid = row[0]
        c.execute("UPDATE number_pool SET status='otp_received',otp=?,otp_at=? WHERE number=?", (otp, now, number))
        h = c.execute("SELECT service_label,country_flag,country_label FROM user_history WHERE number=? AND tg_user_id=? ORDER BY id DESC LIMIT 1",
                      (number, tg_uid)).fetchone()
        c.execute("UPDATE user_history SET otp=?,otp_at=? WHERE number=? AND tg_user_id=?", (otp, now, number, tg_uid))
    if h: return tg_uid, h[0], f"{h[1]} {h[2]}"
    return tg_uid, "—", "—"

def pool_delete_number(number: str) -> bool:
    with _db() as c:
        cur = c.execute("DELETE FROM number_pool WHERE number=?", (re.sub(r"[^\d]","",number),))
    return cur.rowcount > 0

def pool_delete_by_service(service_id: str) -> int:
    with _db() as c:
        return c.execute("DELETE FROM number_pool WHERE pool_service=?", (service_id,)).rowcount

def pool_delete_by_service_country(service_id: str, country_id: str) -> int:
    """Delete all numbers for specific service+country combo."""
    with _db() as c:
        return c.execute("DELETE FROM number_pool WHERE pool_service=? AND pool_country=?",
                         (service_id, country_id)).rowcount

def pool_delete_by_country(country_id: str) -> int:
    with _db() as c:
        return c.execute("DELETE FROM number_pool WHERE pool_country=?", (country_id,)).rowcount

def pool_delete_all() -> int:
    with _db() as c:
        return c.execute("DELETE FROM number_pool").rowcount

def user_history_list(tg_user_id: int) -> list[dict]:
    with _db() as c:
        rows = c.execute(
            "SELECT number,service_label,country_flag,country_label,otp,assigned_at,otp_at FROM user_history WHERE tg_user_id=? ORDER BY id DESC LIMIT 15",
            (tg_user_id,)
        ).fetchall()
    return [{"number":r[0],"service":r[1],"flag":r[2],"country":r[3],"otp":r[4],"assigned_at":r[5],"otp_at":r[6]} for r in rows]

# Watch helpers removed

def otp_save(number: str, otp: str, message: str, cli: str, received_at: str) -> bool:
    with _db() as c:
        if c.execute("SELECT 1 FROM otp_history WHERE number=? AND otp=? AND received_at=?",
                     (number, otp, received_at)).fetchone():
            return False
        c.execute("INSERT INTO otp_history (number,otp,message,cli,received_at) VALUES (?,?,?,?,?)",
                  (number, otp, message, cli, received_at))
    return True

def otp_latest(number: str) -> dict | None:
    with _db() as c:
        r = c.execute("SELECT otp,message,cli,received_at FROM otp_history WHERE number=? ORDER BY id DESC LIMIT 1",
                      (number,)).fetchone()
    return {"otp":r[0],"message":r[1],"cli":r[2],"received_at":r[3]} if r else None

# ─────────────────────────────────────────────────────────────────────────────
# CR API — OTP fetching (PRIMARY source)
# ─────────────────────────────────────────────────────────────────────────────
def _cr_token() -> str:
    return db_get_setting("cr_token") or CR_API_TOKEN

def _normalize(num: str) -> str:
    return re.sub(r"[^\d]", "", num)

async def cr_fetch(number: str, minutes: int = 30) -> list[dict]:
    """
    Fetch OTPs from CR API for a number. Returns list newest-first.
    NOTE: dt1/dt2 nahi bhejte — CR server ka timezone alag ho sakta hai (UTC mismatch bug).
    Sirf records=50 se latest OTPs lo aur client-side filter karo.
    """
    token = _cr_token()
    if not token:
        raise ValueError("CR API token set nahi hua — /settoken se set karo")
    params = {
        "token":     token,
        "filternum": _normalize(number),
        "records":   50,          # Latest 50 records — no time filter (timezone-safe)
    }
    async with _httpx_client(timeout=15) as client:
        resp = await client.get(CR_API_URL, params=params)
    if resp.status_code == 403:
        raise ValueError(
            "CR API: 403 Host not whitelisted!\n"
            "👉 Apne server ka IP CR API provider ko bhejo whitelist ke liye.\n"
            f"   Server IP: check karo 'curl ifconfig.me'"
        )
    resp.raise_for_status()
    # Safe JSON parse — empty ya HTML response crash nahi karega
    raw_text = resp.text.strip()
    if not raw_text:
        return []   # Empty response = koi OTP nahi
    try:
        data = resp.json()
    except Exception:
        # HTML error page ya invalid JSON — quietly skip
        if len(raw_text) < 300:
            raise ValueError(f"CR API bad response: {raw_text[:120]}")
        return []
    if not isinstance(data, dict):
        return []
    if data.get("status") != "success":
        msg = data.get("msg", "Unknown error")
        if "No Records" in msg or "no record" in msg.lower():
            return []   # Normal — koi OTP nahi aaya
        raise ValueError(f"CR API: {msg}")
    results = []
    for item in (data.get("data") or []):
        raw_msg = item.get("message", "")
        m = re.search(r"\b(\d{4,8})\b", raw_msg)
        results.append({
            "dt":      item.get("dt", ""),
            "num":     item.get("num", ""),
            "cli":     item.get("cli", ""),
            "message": raw_msg,
            "otp":     m.group(1) if m else "",
            "payout":  item.get("payout", ""),
        })
    return results

async def cr_fetch_latest(number: str, minutes: int = 30) -> dict | None:
    results = await cr_fetch(number, minutes)
    return results[0] if results else None

# ─────────────────────────────────────────────────────────────────────────────
# Number parser for bulk import
# ─────────────────────────────────────────────────────────────────────────────
def parse_numbers_from_text(raw: str, deduplicate: bool = False) -> list[str]:
    """
    Numbers extract karo from text.
    deduplicate=False (default) → sab numbers as-is — DB layer decide karega
    deduplicate=True            → within-file duplicates remove karo
    """
    seen, out = set(), []
    for line in raw.splitlines():
        for part in re.split(r"[,;|\t]+", line):
            part = part.strip().strip('"\'')
            digits = re.sub(r"[^\d]", "", part)
            if not (7 <= len(digits) <= 15):
                continue
            if deduplicate:
                if digits not in seen:
                    seen.add(digits); out.append(digits)
            else:
                out.append(digits)   # Sab — duplicates bhi
    return out

# ─────────────────────────────────────────────────────────────────────────────
# Formatters
# ─────────────────────────────────────────────────────────────────────────────
def _pool_summary() -> str:
    counts = pool_counts_all()
    lines = []
    for s in SERVICES:
        total = sum(v.get("available", 0) for v in counts[s["id"]].values())
        dot = "🟢" if total > 10 else ("🟡" if total > 0 else "🔴")
        lines.append(f"{s['emoji']} {s['label']}: {dot} *{total}*")
    return "\n".join(lines)

def fmt_assigned(number: str, service: dict, country: dict) -> str:
    return f"{country['flag']} *{service['label']}*"

def fmt_assigned_dual(n1: str, n2: str | None, service: dict, country: dict,
                       status: str = "⏳ _OTP wait..._") -> str:
    """Minimal message — sirf service + status."""
    return f"{country['flag']} *{service['label']}* | {status}"

def fmt_otp(number: str, service_label: str, country_str: str, otp: str, message: str = "", cli: str = "", payout: str = "") -> str:
    """OTP message — matches design: Flag Service 🟢 / Phone / OTP / Reward"""
    flag = country_str.split()[0] if country_str else "🌍"
    # Clean number — ensure + prefix, no double ++
    disp_num = number if number.startswith("+") else f"+{number}"
    reward_line = f"\n💰 Reward : +{payout} TK" if payout else ""
    return (
        f"{flag} *{service_label}* 🟢\n\n"
        f"📱 Phone  :  `{disp_num}        `\n"
        f"🔑 OTP    :  `{otp}              `"
        f"{reward_line}"
    )

def fmt_otp_watch(number: str, item: dict, service_label: str = "Watch", flag: str = "🌍") -> str:
    """Watch number OTP — same clean format as pool OTP."""
    disp = number if number.startswith("+") else f"+{number}"
    otp  = item.get("otp", "—")
    pay  = item.get("payout", "")
    reward_line = f"\n💰 Reward : +{pay} TK" if pay else ""
    return (
        f"{flag} *{service_label}* 🟢\n\n"
        f"📱 Phone  :  `{disp}        `\n"
        f"🔑 OTP    :  `{otp}              `"
        f"{reward_line}"
    )

def fmt_no_otp(number: str) -> str:
    return (
        f"⏳ *No OTP Found*\n"
        f"━━━━━━━━━━━━━━━━━━\n"
        f"📱 Number : `{number}`\n"
        f"_Last 30 min mein koi OTP nahi aaya._"
    )

def _mask(number: str) -> str:
    d = re.sub(r"[^\d]", "", number)
    if len(d) < 6: return number
    s = (len(d) - 5) // 2
    return number[: len(number)-len(d)] + d[:s] + "xxxxx" + d[s+5:]

# ─────────────────────────────────────────────────────────────────────────────
# Keyboards
# ─────────────────────────────────────────────────────────────────────────────
# ── Persistent bottom keyboard — sirf 2 buttons ──────────────────────────────
def main_reply_kb() -> ReplyKeyboardMarkup:
    """
    Message box pe click karne se yeh keyboard popup hoga.
    Sirf 2 buttons: Get Number | Get 2FA
    """
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton("📱 Get Number"), KeyboardButton("🔐 Get 2FA")],
        ],
        resize_keyboard=True,
    )

def kb_service_picker() -> InlineKeyboardMarkup:
    rows = []
    for s in SERVICES:
        cnt = pool_count(s["id"])
        if cnt == 0: continue  # empty service hide karo
        dot = "🟢" if cnt > 10 else "🟡"
        rows.append([InlineKeyboardButton(f"{s['emoji']} {s['label']}  {dot} {cnt}", callback_data=f"pick_svc:{s['id']}")])
    if not rows:
        rows.append([InlineKeyboardButton("🔴 All pools empty", callback_data="noop")])
    return InlineKeyboardMarkup(rows)

def kb_country_picker(service_id: str) -> InlineKeyboardMarkup:
    available = pool_countries_for_service(service_id)
    rows, row = [], []
    for cid, cnt in available:
        c = COUNTRY_MAP.get(cid) or {"id":cid,"label":cid,"flag":"🏳️"}
        dot = "🟢" if cnt > 10 else "🟡"
        row.append(InlineKeyboardButton(f"{c['flag']} {c['label']}  {dot}{cnt}", callback_data=f"pick_cty:{service_id}:{cid}"))
        if len(row) == 2: rows.append(row); row = []
    if row: rows.append(row)
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="getnumber")])
    return InlineKeyboardMarkup(rows)

def kb_after_assign(service_id: str, country_id: str) -> InlineKeyboardMarkup:
    rows = []
    if OTP_GROUP_LINK:
        rows.append([InlineKeyboardButton("👁 OTP GROUP", url=OTP_GROUP_LINK)])
    rows.append([
        InlineKeyboardButton("🔄 Next Number", callback_data=f"pick_cty:{service_id}:{country_id}"),
        InlineKeyboardButton("🌍 Country", callback_data=f"pick_svc:{service_id}"),
    ])
    return InlineKeyboardMarkup(rows)

def kb_after_assign_dual(n1: str, n2: str | None, service_id: str, country_id: str) -> InlineKeyboardMarkup:
    """
    CopyTextButton — image jaisa ⧉ icon wala button.
    Ek tap mein number directly clipboard mein copy hota hai.
    """
    rows = []
    # Number buttons with native ⧉ copy icon (exactly like image)
    rows.append([InlineKeyboardButton(
        text=f"📱  {n1}",
        copy_text=CopyTextButton(text=n1)
    )])
    if n2:
        rows.append([InlineKeyboardButton(
            text=f"📱  {n2}",
            copy_text=CopyTextButton(text=n2)
        )])
    if OTP_GROUP_LINK:
        rows.append([InlineKeyboardButton("🔔 OTP GROUP", url=OTP_GROUP_LINK)])
    rows.append([
        InlineKeyboardButton("🔄 Change Number", callback_data=f"pick_cty:{service_id}:{country_id}"),
        InlineKeyboardButton("🔃 Refresh OTP", callback_data=f"dual_refresh:{n1}:{n2 or ''}:{service_id}:{country_id}"),
    ])
    return InlineKeyboardMarkup(rows)

def kb_after_otp(number: str = "", otp: str = "", message: str = "", service_id: str = "", country_id: str = "") -> InlineKeyboardMarkup:
    # Sirf OTP message show hoga — koi button nahi
    return InlineKeyboardMarkup([])

# kb_watch_menu and kb_number_list removed

def kb_import_service() -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(f"{s['emoji']} {s['label']}", callback_data=f"imp_svc:{s['id']}")] for s in SERVICES]
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data="imp_cancel")])
    return InlineKeyboardMarkup(rows)

def kb_import_country(service_id: str) -> InlineKeyboardMarkup:
    rows, row = [], []
    for c in COUNTRIES:
        row.append(InlineKeyboardButton(f"{c['flag']} {c['label']}", callback_data=f"imp_cty:{service_id}:{c['id']}"))
        if len(row) == 2: rows.append(row); row = []
    if row: rows.append(row)
    rows += [[InlineKeyboardButton("⬅️ Back", callback_data="imp_back_svc")],
             [InlineKeyboardButton("❌ Cancel", callback_data="imp_cancel")]]
    return InlineKeyboardMarkup(rows)

def kb_bulk_country(service_id: str) -> InlineKeyboardMarkup:
    """Country picker for bulk import — uses bulk_cty: prefix."""
    rows, row = [], []
    for c in COUNTRIES:
        row.append(InlineKeyboardButton(f"{c['flag']} {c['label']}", callback_data=f"bulk_cty:{service_id}:{c['id']}"))
        if len(row) == 2: rows.append(row); row = []
    if row: rows.append(row)
    rows += [[InlineKeyboardButton("⬅️ Back", callback_data="bulk_back_svc")],
             [InlineKeyboardButton("❌ Cancel", callback_data="bulk_cancel")]]
    return InlineKeyboardMarkup(rows)

def kb_delete_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔢 Delete by Number",   callback_data="del_by_num")],
        [InlineKeyboardButton("📌 Delete by Service",  callback_data="del_by_svc")],
        [InlineKeyboardButton("🌍 Delete by Country",  callback_data="del_by_cty")],
        [InlineKeyboardButton("💣 Delete ALL Numbers", callback_data="del_all")],
        [InlineKeyboardButton("❌ Close",               callback_data="del_close")],
    ])

# ─────────────────────────────────────────────────────────────────────────────
# Group membership
# ─────────────────────────────────────────────────────────────────────────────
async def _check_member(user, bot) -> bool:
    """Wrapper — _check_membership ko call karta hai."""
    return await _check_membership(bot, user.id)

async def _check_membership(bot, uid: int) -> bool:
    if not REQUIRED_GROUP_ID: return True
    try:
        m = await bot.get_chat_member(chat_id=REQUIRED_GROUP_ID, user_id=uid)
        return m.status in ("member","administrator","creator")
    except Exception:
        return True

async def _send_join_required(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    link = REQUIRED_GROUP_LINK or (f"https://t.me/c/{str(REQUIRED_GROUP_ID).replace('-100','')}" if REQUIRED_GROUP_ID else "#")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Join Our Group", url=link)],
        [InlineKeyboardButton("🔄 I Joined — Check Again", callback_data="check_join")],
    ])
    text = "⛔ *Access Restricted*\n\nJoin our group to use this bot.\n1️⃣ Click *Join Our Group*\n2️⃣ Then tap *I Joined*"
    try:
        if update.callback_query: await update.callback_query.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)
        else: await update.message.reply_text(text, parse_mode="Markdown", reply_markup=kb)
    except Exception: pass

def _is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

# ─────────────────────────────────────────────────────────────────────────────
# CR API — OTP polling after number assign (replaces ivasms poll)
# ─────────────────────────────────────────────────────────────────────────────
async def _cr_poll_after_assign(number: str, tg_user_id: int, service: dict, country: dict,
                                 timeout_sec: int = 60) -> None:
    """
    Pool OTP poll:
      - Pehle existing OTPs snapshot (checked_otps pre-fill) — old OTP pe return band
      - 5s initial sleep
      - phir har 3s pe check, max 20 checks
      - Sirf NAYE OTPs deliver karo
    """
    global _app
    checked_otps: set[str] = set()
    MAX_CHECKS = 20

    # ── Step 1: Snapshot — existing OTPs pre-fill karo ──────────────────────
    # Taake purane OTPs pe poll galti se return na kare
    try:
        snapshot = await cr_fetch(number)
        for item in snapshot:
            otp = item.get("otp", "")
            key = f"{otp}:{item.get('dt','')}"
            if otp:
                checked_otps.add(key)
        logger.info(f"📸 Snapshot: {len(checked_otps)} old OTPs marked | {number}")
    except Exception:
        pass  # Snapshot fail — koi baat nahi, naye OTPs aayenge to deliver honge

    # ── Step 2: 5s initial wait ──────────────────────────────────────────────
    logger.info(f"🔄 CR poll started | {number} | initial=5s | poll=3s | max={MAX_CHECKS}")
    await asyncio.sleep(5)

    # ── Step 3: Poll loop ────────────────────────────────────────────────────
    for attempt in range(1, MAX_CHECKS + 1):
        try:
            results = await cr_fetch(number)
            logger.debug(f"🔍 Check #{attempt} | {number} | {len(results)} records")
            for item in results:
                otp = item.get("otp", "")
                key = f"{otp}:{item.get('dt','')}"
                if not otp or key in checked_otps:
                    continue  # Purana ya duplicate — skip
                checked_otps.add(key)

                # Naya OTP mila!
                tg_uid, service_label, country_str = pool_save_otp(number, otp)
                otp_save(number, otp, item["message"], item["cli"], item["dt"])

                if _app:
                    try:
                        await _app.bot.send_message(
                            chat_id=tg_user_id,
                            text=fmt_otp(
                                number, service["label"],
                                f"{country['flag']} {country['label']}",
                                otp, item["message"], item["cli"],
                                payout=item.get("payout", "")
                            ),
                            parse_mode="Markdown",
                            reply_markup=kb_after_otp(
                                number=number, otp=otp,
                                message=item.get("message",""),
                                service_id=service["id"],
                                country_id=country["id"]
                            )
                        )
                        logger.info(f"📨 Pool OTP → user {tg_user_id} | {number} → {otp} (check #{attempt})")
                    except Exception as e:
                        logger.error(f"Send error: {e}")

                await _forward_to_channel(number, service["label"], f"{country['flag']} {country['label']}", otp, item["message"])
                return  # Naya OTP deliver hua — stop

        except asyncio.CancelledError: return
        except Exception as e:
            import json as _json
            err_str = str(e)
            if not err_str or "No Records" in err_str or "no record" in err_str.lower(): pass
            elif isinstance(e, _json.JSONDecodeError) or "Expecting value" in err_str: pass
            elif _is_network_error(e): await _wait_for_internet(label=f"CR poll {number}")
            elif "403" in err_str or "whitelist" in err_str.lower():
                logger.error(f"❌ CR API 403 — whitelist mein nahi!"); return
            else: logger.debug(f"CR poll {number}: {err_str[:60]}")

        if attempt < MAX_CHECKS:
            await asyncio.sleep(3)

    logger.warning(f"⏰ CR poll done ({MAX_CHECKS} checks) | {number} — no OTP received")

# ─────────────────────────────────────────────────────────────────────────────
# Forward OTP to channel
# ─────────────────────────────────────────────────────────────────────────────
async def _forward_to_channel(number: str, service_label: str, country_str: str, otp: str, message: str = "") -> None:
    if not _app or not OTP_CHANNEL_ID: return
    try:
        await _app.bot.send_message(
            chat_id=OTP_CHANNEL_ID,
            text=fmt_otp(_mask(number), service_label, country_str, otp, message),
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.warning(f"Channel forward failed: {e}")

# Background watcher REMOVED — sirf pool CR poll use karo (_cr_poll_after_assign)
# Watch numbers manually /getotp se check karo — auto-watch spam nahi karega

# ─────────────────────────────────────────────────────────────────────────────
# Webhook — push OTP from external source
# ─────────────────────────────────────────────────────────────────────────────
async def handle_notify_otp(request: web.Request) -> web.Response:
    if NOTIFY_SECRET and request.headers.get("X-Secret", "") != NOTIFY_SECRET:
        return web.json_response({"error": "unauthorized"}, status=401)
    try: body = await request.json()
    except Exception: return web.json_response({"error": "invalid JSON"}, status=400)
    number  = str(body.get("number","")).strip()
    otp     = str(body.get("otp","")).strip()
    message = str(body.get("message","")).strip()
    if not number or not otp:
        return web.json_response({"error": "number and otp required"}, status=400)
    tg_uid, service_label, country_str = pool_save_otp(number, otp)
    if not tg_uid:
        return web.json_response({"error": "number not found"}, status=404)
    if _app:
        try:
            await _app.bot.send_message(
                chat_id=tg_uid,
                text=fmt_otp(number, service_label, country_str, otp, message),
                parse_mode="Markdown", reply_markup=kb_after_otp()
            )
            await _forward_to_channel(number, service_label, country_str, otp, message)
        except Exception as e:
            return web.json_response({"error": str(e)}, status=500)
    return web.json_response({"status": "delivered"})

async def start_notify_server() -> None:
    app_web = web.Application()
    app_web.router.add_post("/notify-otp", handle_notify_otp)
    runner = web.AppRunner(app_web)
    await runner.setup()
    await web.TCPSite(runner, NOTIFY_HOST, NOTIFY_PORT).start()
    logger.info(f"🌐 OTP notify → {NOTIFY_HOST}:{NOTIFY_PORT}/notify-otp")

# ─────────────────────────────────────────────────────────────────────────────
# COMMAND HANDLERS
# ─────────────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not await _check_member(update.effective_user, update.get_bot()): return
    # 1) Reply keyboard (message box mein permanent buttons)
    await update.message.reply_text(
        f"👋 *Welcome, {user.first_name}!*",
        parse_mode="Markdown",
        reply_markup=main_reply_kb()
    )
    # 2) Service picker inline — seedha getnumber flow
    total = pool_count()
    txt   = "📲 *Service choose karo:*" if total else "🔴 *All pools empty.* Admin se contact karo."
    await update.message.reply_text(
        txt,
        parse_mode="Markdown",
        reply_markup=kb_service_picker() if total else None
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    admin_sec = (
        "\n\n*Admin Commands:*\n"
        "`/import` — pool import wizard\n"
        "`/addnumbers fb_reg US 111,222` — add inline\n"
        "`/bulkimport` — bulk .txt/.csv\n"
        "`/delete` — delete menu\n"
        "`/poolstatus` — full stats\n"
        "`/settoken <token>` — CR API token\n"
        "`/status` — bot status\n"
    ) if _is_admin(update.effective_user.id) else ""
    await update.message.reply_text(
        f"📖 *Bot Help*\n\n📊 *Pool:*\n{_pool_summary()}\n\n"
        f"*Pool Flow:*\n1️⃣ Service → 2️⃣ Country → 3️⃣ OTP ⚡\n\n"
        f"*Direct Commands:*\n"
        f"`/mystatus` — assigned number history"
        f"{admin_sec}",
        parse_mode="Markdown", reply_markup=kb_service_picker()
    )


async def cmd_getnumber(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    total = pool_count()
    txt = f"📲 *Choose a service:*\n\n{_pool_summary()}" if total else "🔴 *All pools empty.* Contact admin."
    kb  = kb_service_picker() if total else None
    await update.message.reply_text(txt, parse_mode="Markdown", reply_markup=kb)


async def cmd_mystatus(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    hist = user_history_list(update.effective_user.id)
    if not hist:
        await update.message.reply_text(f"ℹ️ No history.\n\n📊 Pool:\n{_pool_summary()}", parse_mode="Markdown")
        return
    lines = [f"📊 *Your Numbers*\n\n*Pool:*\n{_pool_summary()}\n"]
    for i, h in enumerate(hist[:10], 1):
        icon = "✅" if h["otp"] else "⏳"
        lines.append(f"{i}. `{h['number']}` {icon}\n   📌 {h['service']}  {h['flag']} {h['country']}\n   🔑 OTP: `{h['otp'] or 'waiting...'}`\n   🕐 {h['assigned_at'][:19]}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")



async def cmd_getotp(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not await _check_member(user, update.get_bot()): return

    # Agar number argument diya: /getotp 79234451009
    if ctx.args:
        number = f"+{_normalize(ctx.args[0])}"
        msg = await update.message.reply_text(
            f"⏳ *OTP fetch ho raha hai...*\n📱 `{number}`",
            parse_mode="Markdown"
        )
        await _do_fetch_otp(msg, number)
        return

    # Koi argument nahi — number input maango
    _pending_getotp[user.id] = True
    await update.message.reply_text(
        "📱 *Number enter karo:*\n\n"
        "Jis number ka OTP chahiye woh type karo\n"
        "_(with or without + prefix)_\n\n"
        "Example: `79234451009` ya `+79234451009`",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel", callback_data="getotp_cancel")]
        ])
    )


async def _do_fetch_otp(msg, number: str):
    """CR API se OTP fetch karo aur message update karo."""
    try:
        result = await cr_fetch_latest(number)
        if result:
            otp_save(number, result["otp"], result["message"], result["cli"], result["dt"])
            await msg.edit_text(
                fmt_otp_watch(number, result),
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔄 Refresh OTP", callback_data=f"getotp_refresh:{number}"),
                ]])
            )
        else:
            await msg.edit_text(
                fmt_no_otp(number),
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔄 Retry", callback_data=f"getotp_refresh:{number}"),
                ]])
            )
    except ValueError as e:
        await msg.edit_text(str(e), parse_mode="Markdown")
    except Exception as e:
        await msg.edit_text(f"❌ Error: `{e}`", parse_mode="Markdown")


async def _cr_poll_getotp(tg_user_id: int, number: str,
                           timeout_sec: int = 60) -> None:
    """
    Refresh OTP poll — same snapshot approach as pool poll.
    Old/new koi bhi naya OTP aaye, deliver karo.
    """
    global _app
    MAX_CHECKS = 20
    checked_otps: set[str] = set()

    # Step 1: Snapshot existing OTPs
    try:
        snapshot = await cr_fetch(number)
        for item in snapshot:
            otp = item.get("otp", "")
            key = f"{otp}:{item.get('dt', '')}"
            if otp:
                checked_otps.add(key)
        logger.info(f"📸 getotp snapshot: {len(checked_otps)} existing | {number}")
    except Exception:
        pass

    # Step 2: 5s wait
    logger.info(f"🔄 getotp poll | {number} | {tg_user_id}")
    await asyncio.sleep(5)

    # Step 3: Poll
    for attempt in range(1, MAX_CHECKS + 1):
        try:
            results = await cr_fetch(number)
            for item in results:
                otp = item.get("otp", "")
                key = f"{otp}:{item.get('dt', '')}"
                if not otp or key in checked_otps:
                    continue
                checked_otps.add(key)
                otp_save(number, otp, item["message"], item["cli"], item["dt"])
                if _app:
                    try:
                        await _app.bot.send_message(
                            chat_id=tg_user_id,
                            text=fmt_otp_watch(number, item),
                            parse_mode="Markdown",
                            reply_markup=InlineKeyboardMarkup([[
                                InlineKeyboardButton("🔄 Refresh OTP", callback_data=f"getotp_refresh:{number}"),
                            ]])
                        )
                        logger.info(f"📨 getotp OTP → {tg_user_id} | {number} → {otp} (check #{attempt})")
                    except Exception as e:
                        logger.error(f"getotp send error: {e}")
                return
        except asyncio.CancelledError:
            return
        except Exception as e:
            err = str(e)
            if not err or "No Records" in err or "no record" in err.lower(): pass
            elif _is_network_error(e): await _wait_for_internet()
            else: logger.debug(f"getotp poll {number}: {err[:60]}")

        if attempt < MAX_CHECKS:
            await asyncio.sleep(3)

    if _app:
        try:
            await _app.bot.send_message(
                chat_id=tg_user_id,
                text=f"⏰ *OTP nahi mila* (20 checks)\n\n📱 `{number}`\n\n_Dobara try karo._",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔄 Retry", callback_data=f"getotp_refresh:{number}"),
                ]])
            )
        except Exception:
            pass


async def cmd_2fa(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    /2fa SECRETKEY [label]  — key add karo (temporary, session only)
    /2fa               — list show karo
    /2faclear          — saari keys clear karo
    Key sirf is session mein rehgi — bot restart pe reset.
    """
    user = update.effective_user
    args = ctx.args or []

    if not args:
        keys = twofa_list(user.id)
        if not keys:
            await update.message.reply_text(
                "🔐 *2FA Keys (Temporary)*\n\n"
                "Koi key nahi.\n\n"
                "Key add karo:\n`/2fa YOURSECRETKEY`\n"
                "ya label ke saath:\n`/2fa YOURSECRETKEY MyAccount`\n\n"
                "⚠️ _Keys bot restart pe clear ho jaati hain_",
                parse_mode="Markdown"
            )
            return
        remaining = twofa_get_remaining()
        rows = []
        for k in keys:
            try:
                code_val = twofa_get_code(k["secret"])
                label = k["label"] or k["secret"][:12] + "..."
                rows.append([InlineKeyboardButton(
                    f"🔑 {label}  →  {code_val}",
                    callback_data=f"2fa_show:{k['id']}"
                )])
            except Exception:
                rows.append([InlineKeyboardButton(
                    f"⚠️ Invalid: {k['label']}", callback_data=f"2fa_del:{k['id']}"
                )])
        rows.append([
            InlineKeyboardButton("🔄 Refresh", callback_data="2fa_list"),
            InlineKeyboardButton("🗑 Clear All", callback_data="2fa_clearall"),
        ])
        await update.message.reply_text(
            f"🔐 *2FA Keys* ({len(keys)}) | ⏱ `{remaining}s` baki\n"
            f"⚠️ _Temporary — bot restart pe clear_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(rows)
        )
        return

    # /2fa SECRETKEY [label]
    secret = args[0].strip()
    label  = " ".join(args[1:]).strip() if len(args) > 1 else ""

    # Validate key format first
    import re as _re
    clean = _re.sub(r"[^A-Z2-7=]", "", secret.upper().replace(" ", ""))
    if len(clean) < 8:
        await update.message.reply_text(
            "❌ *Invalid 2FA key!*\n\nBase32 format hona chahiye.\n"
            "Example: `JBSWY3DPEHPK3PXP`\n\n"
            "Authenticator app mein `Setup key` copy karo.",
            parse_mode="Markdown"
        )
        return

    # Test code generation
    try:
        test_code = twofa_get_code(clean)
    except ValueError as e:
        await update.message.reply_text(f"❌ Key error: {e}")
        return

    added = twofa_add(user.id, secret, label)
    remaining = twofa_get_remaining()

    if added:
        await update.message.reply_text(
            f"✅ *2FA Key Added (Temporary)*\n━━━━━━━━━━━━━━━━━━\n"
            f"🏷 Label   : `{label or 'Unnamed'}`\n"
            f"🔑 Code    : `{test_code}`\n"
            f"⏱ Expires : `{remaining}s`\n\n"
            f"⚠️ _Key sirf is session mein — bot restart pe clear_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Refresh Code", callback_data=f"2fa_refresh:{clean}"),
                InlineKeyboardButton("📋 All Keys", callback_data="2fa_list"),
            ]])
        )
    else:
        await update.message.reply_text(
            f"ℹ️ Key already added.\n🔑 Current code: `{test_code}`\n⏱ `{remaining}s`",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Refresh", callback_data=f"2fa_refresh:{clean}"),
            ]])
        )


async def cmd_2faclear(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """All 2FA keys clear karo."""
    n = twofa_clear_all(update.effective_user.id)
    await update.message.reply_text(
        f"🗑 *{n} 2FA key(s) cleared.*\n\nNaye key add karne ke liye: `/2fa SECRETKEY`",
        parse_mode="Markdown"
    )


async def cmd_settoken(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only.")
        return
    if not ctx.args:
        t = _cr_token()
        masked = (t[:8]+"..."+t[-4:]) if len(t) > 12 else ("✅ Set" if t else "❌ Not set")
        await update.message.reply_text(f"🔑 *CR API Token*\nCurrent: `{masked}`\n\n`/settoken <token>`", parse_mode="Markdown")
        return
    new_token = ctx.args[0].strip()
    db_set_setting("cr_token", new_token)
    # Test the token
    try:
        await cr_fetch("0000000000", minutes=1)
        test_result = "✅ Token working!"
    except ValueError as e:
        test_result = f"⚠️ {e}"
    except Exception:
        test_result = "✅ Token saved (test inconclusive)"
    await update.message.reply_text(
        f"✅ *Token Updated!*\n`{new_token[:8]}...{new_token[-4:]}`\n\n{test_result}",
        parse_mode="Markdown"
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    t = _cr_token()
    masked = (t[:8]+"..."+t[-4:]) if len(t) > 12 else ("✅ Set" if t else "❌ NOT SET — /settoken")
    try:
        await cr_fetch("0000000000", minutes=1)
        api_st = "🟢 CR API Connected"
    except ValueError as e:
        api_st = f"🔴 {e}"
    except Exception as e:
        api_st = f"🟡 {e}"
    await update.message.reply_text(
        f"📊 *Bot Status*\n━━━━━━━━━━━━━━━━━━\n"
        f"🔑 CR Token  : `{masked}`\n"
        f"🌐 CR API    : {api_st}\n"
        f"⏱ Poll      : every {CR_POLL_SEC}s (fast)\n"
        f"📱 Pool      : {pool_count()} available\n"
        f"👁 OTP Group : {OTP_GROUP_LINK or 'Not set'}\n"
        f"🌐 Note: CR API 403? → Server IP whitelist mein add karo\n\n"
        f"📊 Pool:\n{_pool_summary()}",
        parse_mode="Markdown"
    )


# ── Admin pool commands ───────────────────────────────────────────────────────

async def cmd_import(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only.")
        return
    await update.message.reply_text("📥 *Import Numbers*\n\nStep 1 — Pick service:",
                                    parse_mode="Markdown", reply_markup=kb_import_service())


async def cmd_bulkservice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """User ke liye: txt file se bulk numbers + service select karo."""
    user = update.effective_user
    if not _is_admin(user.id):
        await update.message.reply_text("⛔ Admin only.")
        return
    await update.message.reply_text(
        "📂 *Bulk Import by Service*\n━━━━━━━━━━━━━━━━━━\n\n"
        "Step 1 — Service choose karo:",
        parse_mode="Markdown",
        reply_markup=kb_import_service()
    )


async def cmd_addnumbers(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only.")
        return
    if not ctx.args or len(ctx.args) < 3:
        await update.message.reply_text(
            "Usage: `/addnumbers <service> <country> num1,num2`\n"
            f"Services: {', '.join(s['id'] for s in SERVICES)}", parse_mode="Markdown")
        return
    svc_id, cty_id = ctx.args[0].lower(), ctx.args[1].upper()
    if svc_id not in SERVICE_MAP:
        await update.message.reply_text(f"❌ Unknown service `{svc_id}`", parse_mode="Markdown"); return
    if cty_id not in COUNTRY_MAP:
        await update.message.reply_text(f"❌ Unknown country `{cty_id}`", parse_mode="Markdown"); return
    raw = " ".join(ctx.args[2:])
    nums = [n.strip() for n in re.split(r"[,;\s]+", raw) if n.strip()]
    inserted, dupes = pool_import(nums, svc_id, cty_id)
    svc, cty = SERVICE_MAP[svc_id], COUNTRY_MAP[cty_id]
    await update.message.reply_text(
        f"✅ *Added!*\n{svc['emoji']} {svc['label']} › {cty['flag']} {cty['label']}\n"
        f"➕ {inserted}  ⏭ {dupes} dupes\n🟢 Pool: {pool_count(svc_id, cty_id)}",
        parse_mode="Markdown"
    )


async def cmd_delete(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only.")
        return
    await update.message.reply_text(
        f"🗑 *Delete Menu*\n\n{_pool_summary()}\n\nChoose:",
        parse_mode="Markdown", reply_markup=kb_delete_menu()
    )


async def cmd_poolstatus(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only.")
        return
    counts = pool_counts_all()
    lines = ["📊 *Pool Status*\n━━━━━━━━━━━━━━━━━━"]
    grand_a = grand_t = 0
    for s in SERVICES:
        sdata = counts[s["id"]]
        sa = sum(v.get("available",0) for v in sdata.values())
        st = sum(sum(v.values()) for v in sdata.values())
        dot = "🟢" if sa > 10 else ("🟡" if sa > 0 else "🔴")
        lines.append(f"\n{s['emoji']} *{s['label']}* {dot} {sa}/{st}")
        for cid, stats in sdata.items():
            cty = COUNTRY_MAP.get(cid, {"flag":"🏳️","label":cid})
            lines.append(f"  {cty['flag']} {cty['label']}: 🟢{stats.get('available',0)} 📤{stats.get('assigned',0)} ✅{stats.get('otp_received',0)}")
        grand_a += sa; grand_t += st
    lines.append(f"\n━━━━━━━━━━━━━━━━━━\n🔢 Grand: *{grand_a}* avail / *{grand_t}* total")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_delnumber(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only.")
        return
    if not ctx.args:
        await update.message.reply_text("Usage: `/delnumber 12025551234`", parse_mode="Markdown")
        return
    deleted = pool_delete_number(ctx.args[0])
    txt = f"🗑 Deleted: `{ctx.args[0]}`\n\n{_pool_summary()}" if deleted else f"❌ Not found: `{ctx.args[0]}`"
    await update.message.reply_text(txt, parse_mode="Markdown")


# ─────────────────────────────────────────────────────────────────────────────
# Button handler — all callbacks
# ─────────────────────────────────────────────────────────────────────────────
async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try: await query.answer()
    except Exception: pass
    data = query.data
    uid  = query.from_user.id
    mid  = query.message.message_id
    cid  = query.message.chat_id

    async def edit(text, kb=None):
        try:
            await ctx.bot.edit_message_text(chat_id=cid, message_id=mid,
                                            text=text, parse_mode="Markdown", reply_markup=kb)
        except Exception: pass

    # ── Join check ────────────────────────────────────────────────────────────
    if data == "check_join":
        if not await _check_membership(ctx.bot, uid):
            try: await query.answer("⛔ Not joined yet!", show_alert=True)
            except Exception: pass
            await _send_join_required(update, ctx)
        else:
            await edit(f"📲 *Choose a service:*\n\n{_pool_summary()}", kb_service_picker())
        return

    _pool_actions = data in ("getnumber",) or data.startswith(("pick_svc:","pick_cty:"))
    if not _is_admin(uid) and _pool_actions:
        if not await _check_membership(ctx.bot, uid):
            try: await query.answer("⛔ Join our group first!", show_alert=True)
            except Exception: pass
            await _send_join_required(update, ctx); return

    # ── Pool flow ─────────────────────────────────────────────────────────────
    if data == "getnumber":
        total = pool_count()
        txt = f"📲 *Choose a service:*\n\n{_pool_summary()}" if total else "🔴 All pools empty."
        await edit(txt, kb_service_picker() if total else None)

    elif data.startswith("pick_svc:"):
        svc_id = data.split(":",1)[1]
        if svc_id not in SERVICE_MAP: return
        svc = SERVICE_MAP[svc_id]
        cnt = pool_count(svc_id)
        if cnt == 0:
            await edit(f"🔴 *{svc['emoji']} {svc['label']} pool empty.*\n\nChoose another:\n{_pool_summary()}", kb_service_picker())
        else:
            await edit(f"{svc['emoji']} *{svc['label']}*\n\n🌍 *Choose a country:*", kb_country_picker(svc_id))

    elif data.startswith("pick_cty:"):
        _, svc_id, cty_id = data.split(":")
        svc = SERVICE_MAP.get(svc_id); cty = COUNTRY_MAP.get(cty_id)
        if not svc or not cty: return
        user = query.from_user
        if pool_count(svc_id, cty_id) == 0:
            await edit(f"🔴 *{cty['flag']} {cty['label']} empty for {svc['label']}.*\n\nChoose another:", kb_country_picker(svc_id))
            return
        await edit(f"⏳ Assigning *{svc['emoji']} {svc['label']}* › *{cty['flag']} {cty['label']}*...", None)
        # ── 2 numbers assign karo ──────────────────────────────────────────────
        n1, n2 = pool_assign_dual(user.id, user.username, svc, cty)
        if not n1:
            await edit("❌ Could not assign. Try again.", kb_country_picker(svc_id)); return
        await edit(
            fmt_assigned_dual(n1, n2, svc, cty),
            kb_after_assign_dual(n1, n2, svc_id, cty_id)
        )
        # CR poll for both numbers
        asyncio.create_task(
            _cr_poll_after_assign(n1, user.id, svc, cty, timeout_sec=60),
            name=f"cr_poll_{n1}"
        )
        if n2:
            asyncio.create_task(
                _cr_poll_after_assign(n2, user.id, svc, cty, timeout_sec=60),
                name=f"cr_poll_{n2}"
            )
        logger.info(f"🔄 CR poll started | pool | {n1} + {n2 or '(only 1 available)'}")

    elif data.startswith("dual_refresh:"):
        parts = data.split(":")
        n1, n2_raw, svc_id, cty_id = parts[1], parts[2], parts[3], parts[4]
        n2 = n2_raw if n2_raw else None
        svc = SERVICE_MAP.get(svc_id); cty = COUNTRY_MAP.get(cty_id)
        if not svc or not cty: return

        await edit(
            fmt_assigned_dual(n1, n2, svc, cty, "🔄 _OTP dhundh raha hai... (60s)_"),
            kb_after_assign_dual(n1, n2, svc_id, cty_id)
        )

        asyncio.create_task(
            _cr_poll_after_assign(n1, uid, svc, cty, timeout_sec=60),
            name=f"cr_refresh_{n1}"
        )
        if n2:
            asyncio.create_task(
                _cr_poll_after_assign(n2, uid, svc, cty, timeout_sec=60),
                name=f"cr_refresh_{n2}"
            )

    # ── Watch number flow ─────────────────────────────────────────────────────



    elif data == "getotp_cancel":
        _pending_getotp.pop(uid, None)
        await edit("❌ Cancelled.")

    elif data.startswith("getotp_refresh:"):
        number = data[len("getotp_refresh:"):]
        # Instant fetch + silent background poll
        try:
            result = await cr_fetch_latest(number, minutes=30)
            if result and result.get("otp"):
                otp_save(number, result["otp"], result["message"], result["cli"], result["dt"])
                await edit(fmt_otp_watch(number, result), InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔄 Refresh OTP", callback_data=f"getotp_refresh:{number}"),
                ]]))
                return
        except Exception:
            pass
        # Nahi mila — silent background poll
        asyncio.create_task(
            _cr_poll_getotp(uid, number),
            name=f"getotp_poll_{number}"
        )

    elif data.startswith("getotp:"):
        # One-time instant fetch (last 30 min)
        number = data[len("getotp:"):]
        await edit(f"⏳ *OTP fetch ho raha hai...*\n📱 `{number}`")
        try:
            result = await cr_fetch_latest(number)
            if result:
                otp_save(number, result["otp"], result["message"], result["cli"], result["dt"])
                await edit(fmt_otp_watch(number, result), InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔄 Refresh OTP", callback_data=f"getotp_refresh:{number}"),
                ]]))
            else:
                await edit(fmt_no_otp(number), InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔄 Refresh OTP", callback_data=f"getotp_refresh:{number}"),
                ]]))
        except ValueError as e: await edit(str(e))
        except Exception as e:  await edit(f"❌ Error: `{e}`")






    # ── Admin import flow ─────────────────────────────────────────────────────
    elif data.startswith("imp_svc:"):
        if not _is_admin(uid): return
        svc_id = data.split(":",1)[1]
        svc = SERVICE_MAP.get(svc_id)
        if not svc: return
        # Carry numbers + fname from file-first flow
        prev = _pending_import.get(uid, {})
        _pending_import[uid] = {
            "service_id": svc_id,
            "numbers":    prev.get("numbers", []),
            "fname":      prev.get("fname", ""),
        }
        cnt = len(_pending_import[uid]["numbers"])
        hint = f"  \n📁 `{_pending_import[uid]['fname']}` ({cnt:,} numbers ready)" if cnt else ""
        await edit(
            f"📥 *Import → {svc['emoji']} {svc['label']}*{hint}\n\nStep 2 — Country choose karo:",
            kb_import_country(svc_id)
        )

    elif data == "imp_back_svc":
        if not _is_admin(uid): return
        _pending_import.pop(uid, None)
        await edit("📥 *Import Numbers*\n\nStep 1 — Pick service:", kb_import_service())

    elif data.startswith("imp_cty:"):
        if not _is_admin(uid): return
        _, svc_id, cty_id = data.split(":")
        svc = SERVICE_MAP.get(svc_id); cty = COUNTRY_MAP.get(cty_id)
        if not svc or not cty: return
        prev = _pending_import.get(uid, {})
        numbers = prev.get("numbers", [])
        fname   = prev.get("fname", "file")
        _pending_import[uid] = {"service_id": svc_id, "country_id": cty_id,
                                "numbers": numbers, "fname": fname}
        if numbers:
            # Numbers ready — dupe control puchho
            await edit(
                f"📥 *{svc['emoji']} {svc['label']} › {cty['flag']} {cty['label']}*\n"
                f"📁 `{fname}` — *{len(numbers):,} numbers*\n\n"
                f"🔁 Duplicate numbers ka kya karna hai?",
                InlineKeyboardMarkup([[
                    InlineKeyboardButton("🚫 Skip Dupes", callback_data=f"imp_do:{svc_id}:{cty_id}:skip"),
                    InlineKeyboardButton("✅ Allow Dupes", callback_data=f"imp_do:{svc_id}:{cty_id}:allow"),
                ]])
            )
        else:
            # No file yet — ask for file
            await edit(
                f"📥 *Import → {svc['emoji']} {svc['label']} › {cty['flag']} {cty['label']}*\n\n"
                f"📁 Ab `.txt` ya `.csv` file bhejo — har line mein ek number."
            )

    elif data.startswith("imp_do:"):
        if not _is_admin(uid): return
        # imp_do:svc_id:cty_id:skip|allow
        parts = data.split(":")
        svc_id, cty_id, mode = parts[1], parts[2], parts[3]
        svc = SERVICE_MAP.get(svc_id); cty = COUNTRY_MAP.get(cty_id)
        if not svc or not cty: return
        prev    = _pending_import.pop(uid, {})
        numbers = prev.get("numbers", [])
        fname   = prev.get("fname", "file")
        if not numbers:
            await edit("❌ Numbers nahi mile — dobara file bhejo."); return
        allow_dupes = (mode == "allow")
        inserted, skipped = pool_import(numbers, svc_id, cty_id, allow_dupes=allow_dupes)
        dupe_line = f"⏭ Dupes skipped : *{skipped:,}*" if not allow_dupes else f"🔁 Dupes added   : *{skipped:,}*"
        await edit(
            f"✅ *Import Complete!*\n━━━━━━━━━━━━━━━━━━\n"
            f"📁 `{fname}`\n"
            f"{svc['emoji']} *{svc['label']}* › {cty['flag']} {cty['label']}\n\n"
            f"📊 Total   : *{len(numbers):,}*\n"
            f"➕ Added   : *{inserted:,}*\n"
            f"{dupe_line}\n\n"
            f"🟢 Pool now: *{pool_count(svc_id, cty_id):,}*"
        )
        logger.info(f"📂 Import done svc={svc_id} cty={cty_id} inserted={inserted} skipped={skipped} mode={mode}")

    elif data == "imp_cancel":
        if not _is_admin(uid): return
        _pending_import.pop(uid, None)
        await edit("❌ Import cancelled.")


    # ── 2FA callbacks ──────────────────────────────────────────────────────────
    elif data == "2fa_list":
        keys = twofa_list(uid)
        if not keys:
            await edit("🔐 *2FA Keys*\n\nKoi key nahi.\n\n`/2fa YOURSECRETKEY label`")
            return
        rows = []
        remaining = twofa_get_remaining()
        for k in keys:
            try:
                code_val = twofa_get_code(k["secret"])
                label = k["label"] or k["secret"][:12] + "..."
                rows.append([InlineKeyboardButton(f"🔑 {label}  →  {code_val}", callback_data=f"2fa_show:{k['id']}")])
            except Exception:
                rows.append([InlineKeyboardButton(f"⚠️ {k['label'] or 'Invalid'}", callback_data=f"2fa_del:{k['id']}")])
        rows.append([InlineKeyboardButton("🔄 Refresh", callback_data="2fa_list")])
        await edit(
            f"🔐 *Your 2FA Keys* ({len(keys)})\n⏱ Expires in: `{remaining}s`",
            InlineKeyboardMarkup(rows)
        )

    elif data.startswith("2fa_show:"):
        key_id = int(data.split(":", 1)[1]) if data.split(":", 1)[1].isdigit() else 0
        keys = twofa_list(uid)
        k = next((x for x in keys if x["id"] == key_id), None)
        if not k:
            await edit("❌ Key nahi mili."); return
        try:
            code_val = twofa_get_code(k["secret"])
            remaining = twofa_get_remaining()
        except ValueError as e:
            await edit(f"❌ {e}"); return
        label = k["label"] or "Unnamed"
        await edit(
            f"🔐 *2FA Code*\n━━━━━━━━━━━━━━━━━━\n"
            f"🏷 Label    : `{label}`\n"
            f"🔑 Code     : `{code_val}`\n"
            f"⏱ Expires  : `{remaining}s`\n"
            f"🔑 Secret   : `{k['secret'][:8]}...`\n\n"
            f"_30s mein new code aayega_",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh", callback_data=f"2fa_refresh:{k['secret']}")],
                [InlineKeyboardButton("🗑 Delete", callback_data=f"2fa_del:{key_id}"),
                 InlineKeyboardButton("⬅️ All Keys", callback_data="2fa_list")],
            ])
        )

    elif data.startswith("2fa_refresh:"):
        secret = data[len("2fa_refresh:"):]
        try:
            code_val = twofa_get_code(secret)
            remaining = twofa_get_remaining()
        except ValueError as e:
            await edit(f"❌ {e}"); return
        keys = twofa_list(uid)
        k = next((x for x in keys if x["secret"] == secret), None)
        label = k["label"] if k else "Unknown"
        await edit(
            f"🔐 *2FA Code (Refreshed)*\n━━━━━━━━━━━━━━━━━━\n"
            f"🏷 Label    : `{label}`\n"
            f"🔑 Code     : `{code_val}`\n"
            f"⏱ Expires  : `{remaining}s`",
            InlineKeyboardMarkup([[
                InlineKeyboardButton("🔄 Refresh Again", callback_data=f"2fa_refresh:{secret}"),
                InlineKeyboardButton("⬅️ All Keys", callback_data="2fa_list"),
            ]])
        )

    elif data == "2fa_new_key":
        # Clear old + ask for new key input
        twofa_clear_all(uid)
        _pending_2fa_input[uid] = True
        await edit(
            "🔐 *New Key Enter Karo*\n━━━━━━━━━━━━━━━━━━\n\n"
            "🔑 Secret Key type karo (chat mein):\n\n"
            "_Example: `JBSWY3DPEHPK3PXP`_"
        )

    elif data == "2fa_clearall":
        n = twofa_clear_all(uid)
        _pending_2fa_input[uid] = True
        await edit(
            f"🗑 *{n} key(s) cleared.*\n\n"
            "🔑 Naya Secret Key type karo:"
        )

    elif data.startswith("2fa_del:"):
        key_id = int(data.split(":", 1)[1]) if data.split(":", 1)[1].isdigit() else 0
        deleted = twofa_delete(uid, key_id) if key_id else False
        keys = twofa_list(uid)
        msg = "🗑 Key deleted (session se)." if deleted else "❌ Not found."
        rows = []
        remaining = twofa_get_remaining()
        for k in keys:
            try:
                code_val = twofa_get_code(k["secret"])
                label = k["label"] or k["secret"][:12] + "..."
                rows.append([InlineKeyboardButton(f"🔑 {label}  →  {code_val}", callback_data=f"2fa_show:{k['id']}")])
            except Exception:
                pass
        rows.append([
            InlineKeyboardButton("➕ Add (/2fa)", callback_data="2fa_list"),
            InlineKeyboardButton("🗑 Clear All", callback_data="2fa_clearall"),
        ])
        await edit(
            f"{msg}\n\n🔐 *Session Keys* ({len(keys)})\n⏱ `{remaining}s`\n"
            f"⚠️ _Temporary — bot restart pe clear_",
            InlineKeyboardMarkup(rows) if rows else None
        )

    # ── Bulk import by service callbacks ──────────────────────────────────────
    elif data.startswith("imp_dup:"):
        if not _is_admin(uid): return
        _, action, target_uid = data.split(":", 2)
        target_uid = int(target_uid)
        state = _pending_import.get(target_uid, {})
        numbers = state.get("numbers", [])
        fname   = state.get("fname", "file")
        svc_id  = state.get("service_id", "")
        cty_id  = state.get("country_id", "")
        if not numbers or not svc_id or not cty_id:
            await edit("❌ Import session expire ho gayi. Dobara try karo."); return
        _pending_import.pop(target_uid, None)
        svc = SERVICE_MAP.get(svc_id, {"emoji":"📌","label":svc_id})
        cty = COUNTRY_MAP.get(cty_id, {"flag":"🌍","label":cty_id})
        # Show progress
        await edit(f"⏳ Inserting {len(numbers):,} numbers...\n({'Skip' if action=='skip' else 'Replace'} dupes)")
        if action == "replace":
            # Delete existing then insert fresh
            pool_delete_by_service_country(svc_id, cty_id)
        inserted, dupes = pool_import(numbers, svc_id, cty_id)
        dup_note = f"⏭ Skipped" if action == "skip" else f"🔄 Replaced"
        await edit(
            f"✅ *Import Complete!*\n━━━━━━━━━━━━━━━━━━\n"
            f"📁 `{fname}`\n"
            f"{svc['emoji']} {svc['label']} › {cty['flag']} {cty['label']}\n"
            f"📊 Total : *{len(numbers):,}*\n"
            f"➕ Added : *{inserted:,}*\n"
            f"{dup_note}: *{dupes:,}*\n\n"
            f"🟢 Pool  : *{pool_count(svc_id, cty_id):,}*\n\n{_pool_summary()}"
        )

    elif data.startswith("dup_choice:"):
        if not _is_admin(uid): return
        _, action, target_uid_str = data.split(":", 2)
        target_uid = int(target_uid_str)
        state = _pending_import.get(target_uid, {})
        numbers  = state.get("dup_numbers", [])
        svc_id   = state.get("service_id", "")
        cty_id   = state.get("country_id", "")
        fname    = state.get("dup_fname", "file")
        if not numbers or not svc_id or not cty_id:
            await edit("❌ Session expire ho gayi. Dobara import karo."); return
        _pending_import.pop(target_uid, None)
        svc = SERVICE_MAP.get(svc_id, {"emoji":"📌","label":svc_id})
        cty = COUNTRY_MAP.get(cty_id, {"flag":"🌍","label":cty_id})
        allow = (action == "keep")
        await edit(f"⏳ Inserting *{len(numbers):,}* numbers...\n({'All including dupes' if allow else 'Unique only'})")
        inserted, skipped = pool_import(numbers, svc_id, cty_id, allow_dupes=allow)
        dup_line = (
            f"🔁 Dupes kept  : *{len(numbers)-inserted+inserted-inserted:,}*" if allow
            else f"🗑 Removed     : *{skipped:,}*"
        )
        # Simpler calc
        if allow:
            dup_line = f"✅ All inserted (incl. dupes)"
        else:
            dup_line = f"🗑 Dupes removed : *{skipped:,}*"
        await edit(
            f"✅ *Import Complete!*\n━━━━━━━━━━━━━━━━━━\n"
            f"📁 `{fname}`\n"
            f"{svc['emoji']} {svc['label']} › {cty['flag']} {cty['label']}\n\n"
            f"📊 Total  : *{len(numbers):,}*\n"
            f"➕ Added  : *{inserted:,}*\n"
            f"{dup_line}\n\n"
            f"🟢 Pool   : *{pool_count(svc_id, cty_id):,}*\n\n{_pool_summary()}"
        )

    elif data == "bulk_cancel":
        _pending_import.pop(uid, None)
        _pending_bulk.pop(uid, None)
        await edit("❌ Import cancelled.")

    elif data.startswith("bulk_svc:"):
        if not _is_admin(uid):
            await edit("⛔ Admin only."); return
        svc_id = data.split(":", 1)[1]
        svc = SERVICE_MAP.get(svc_id)
        if not svc:
            await edit("❌ Unknown service."); return
        _pending_import[uid] = {"service_id": svc_id}
        await edit(
            f"📂 *Bulk Import → {svc['emoji']} {svc['label']}*\n\nStep 2 — Country select karo:",
            kb_bulk_country(svc_id)
        )

    elif data.startswith("bulk_cty:"):
        if not _is_admin(uid):
            await edit("⛔ Admin only."); return
        _, svc_id, cty_id = data.split(":", 2)
        svc = SERVICE_MAP.get(svc_id); cty = COUNTRY_MAP.get(cty_id)
        if not svc or not cty:
            await edit("❌ Unknown service/country."); return
        _pending_import[uid] = {"service_id": svc_id, "country_id": cty_id}
        await edit(
            f"📂 *Bulk Import*\n"
            f"{svc['emoji']} {svc['label']} › {cty['flag']} {cty['label']}\n\n"
            f"Step 3 — Ab `.txt` ya `.csv` file send karo!\n\n"
            f"✅ *Supported:*\n"
            f"• Ek line mein ek number\n"
            f"• CSV (kisi bhi column mein number)\n"
            f"• `+` ya bina `+` ke\n"
            f"• 10,000+ numbers in one file!"
        )

    # ── Admin delete flow ─────────────────────────────────────────────────────
    # ── Copy Number (from dual assign buttons) ───────────────────────────────
    elif data.startswith("copy:"):
        num_val = data[len("copy:"):]
        disp = f"+{num_val}" if not num_val.startswith("+") else num_val
        try:
            await query.answer(f"📋 Number: {disp}\n\nLong press to copy ☝️", show_alert=True)
        except Exception:
            pass

    # ── Copy OTP / SMS ────────────────────────────────────────────────────────
    elif data.startswith("copyotp:"):
        otp_val = data[len("copyotp:"):]
        try:
            await query.answer(f"✅ OTP Copied: {otp_val}", show_alert=True)
        except Exception:
            pass

    elif data.startswith("copysms:"):
        num = data[len("copysms:"):]
        latest = otp_latest(num)
        if latest:
            try:
                await query.answer(f"📋 {latest['message']}", show_alert=True)
            except Exception:
                pass
        else:
            try:
                await query.answer("❌ SMS nahi mila.", show_alert=True)
            except Exception:
                pass

    elif data == "del_menu":
        if not _is_admin(uid): return
        await edit(f"🗑 *Delete Menu*\n\n{_pool_summary()}", kb_delete_menu())

    elif data == "del_close":
        if not _is_admin(uid): return
        await edit("✅ Closed.")

    elif data == "del_by_num":
        if not _is_admin(uid): return
        await edit("🔢 Reply: `/delnumber 12025551234`",
                   InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="del_menu")]]))

    elif data == "del_by_svc":
        if not _is_admin(uid): return
        rows = [[InlineKeyboardButton(f"{s['emoji']} {s['label']} ({pool_count(s['id'])})",
                                      callback_data=f"del_svc:{s['id']}")] for s in SERVICES]
        rows.append([InlineKeyboardButton("⬅️ Back", callback_data="del_menu")])
        await edit("📌 *Delete by Service:*", InlineKeyboardMarkup(rows))

    elif data.startswith("del_svc:"):
        if not _is_admin(uid): return
        svc_id = data.split(":",1)[1]
        svc = SERVICE_MAP.get(svc_id, {"emoji":"📌","label":svc_id})
        total = pool_count(svc_id)
        await edit(f"⚠️ Delete ALL *{total}* numbers from {svc['emoji']} *{svc['label']}*?",
                   InlineKeyboardMarkup([[InlineKeyboardButton("✅ Yes", callback_data=f"del_confirm:SVC:{svc_id}")],
                                         [InlineKeyboardButton("⬅️ Back", callback_data="del_menu")]]))

    elif data == "del_by_cty":
        if not _is_admin(uid): return
        rows, row = [], []
        for c in COUNTRIES:
            t = pool_count(country_id=c["id"])
            row.append(InlineKeyboardButton(f"{c['flag']} {c['label']} ({t})", callback_data=f"del_cty:{c['id']}"))
            if len(row) == 2: rows.append(row); row = []
        if row: rows.append(row)
        rows.append([InlineKeyboardButton("⬅️ Back", callback_data="del_menu")])
        await edit("🌍 *Delete by Country:*", InlineKeyboardMarkup(rows))

    elif data.startswith("del_cty:"):
        if not _is_admin(uid): return
        cty_id = data.split(":",1)[1]
        cty = COUNTRY_MAP.get(cty_id, {"flag":"🌍","label":cty_id})
        total = pool_count(country_id=cty_id)
        await edit(f"⚠️ Delete ALL *{total}* numbers from {cty['flag']} *{cty['label']}*?",
                   InlineKeyboardMarkup([[InlineKeyboardButton("✅ Yes", callback_data=f"del_confirm:CTY:{cty_id}")],
                                         [InlineKeyboardButton("⬅️ Back", callback_data="del_menu")]]))

    elif data == "del_all":
        if not _is_admin(uid): return
        total = pool_count()
        await edit(f"💣 Delete ALL *{total}* numbers? Cannot undo.",
                   InlineKeyboardMarkup([[InlineKeyboardButton("✅ DELETE ALL", callback_data="del_confirm:ALL")],
                                         [InlineKeyboardButton("⬅️ Back", callback_data="del_menu")]]))

    elif data.startswith("del_confirm:"):
        if not _is_admin(uid): return
        action = data[len("del_confirm:"):]
        if action == "ALL":
            d = pool_delete_all()
            await edit(f"🗑 Deleted all *{d}* numbers.\n\n{_pool_summary()}")
        elif action.startswith("SVC:"):
            svc_id = action[4:]; d = pool_delete_by_service(svc_id)
            svc = SERVICE_MAP.get(svc_id, {"emoji":"📌","label":svc_id})
            await edit(f"🗑 Deleted *{d}* from {svc['emoji']} {svc['label']}.\n\n{_pool_summary()}")
        elif action.startswith("CTY:"):
            cty_id = action[4:]; d = pool_delete_by_country(cty_id)
            cty = COUNTRY_MAP.get(cty_id, {"flag":"🌍","label":cty_id})
            await edit(f"🗑 Deleted *{d}* from {cty['flag']} {cty['label']}.\n\n{_pool_summary()}")


# ─────────────────────────────────────────────────────────────────────────────
# Document handler — bulk import .txt / .csv
# ─────────────────────────────────────────────────────────────────────────────
async def handle_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """TXT/CSV file bheja → service/country choose karo → bulk import."""
    user = update.effective_user
    if not _is_admin(user.id):
        await update.message.reply_text("⛔ Sirf admin bulk import kar sakta hai.")
        return

    doc = update.message.document
    if not doc or not doc.file_name.lower().endswith((".txt", ".csv")):
        await update.message.reply_text("❌ Sirf .txt ya .csv file bhejo.")
        return

    prog = await update.message.reply_text("📥 File read kar raha hai...")

    try:
        tg_file = await doc.get_file()
        raw = (await tg_file.download_as_bytearray()).decode("utf-8", errors="ignore")
    except Exception as e:
        await prog.edit_text(f"❌ File download failed: {e}")
        return

    # Parse numbers — one per line, strip whitespace
    numbers = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"): continue
        # Support: just number OR number|extra OR number,extra
        num = line.split("|")[0].split(",")[0].strip()
        clean = _normalize(num)
        if 7 <= len(clean) <= 15:
            numbers.append(clean)

    if not numbers:
        await prog.edit_text("❌ Koi valid number nahi mila file mein.")
        return

    # Save numbers in pending state, show service picker
    _pending_import[user.id] = {
        "numbers":   numbers,
        "fname":     doc.file_name,
        "step":      "pick_service",
    }

    await prog.edit_text(
        f"✅ *{len(numbers):,} numbers* file mein mile!\n\n"
        f"📌 Step 1 — *Service choose karo:*",
        parse_mode="Markdown",
        reply_markup=kb_import_service()
    )


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text.strip()

    # ── Reply keyboard buttons ────────────────────────────────────────────────
    if "Get Number" in text or text == "📱 Get Number":
        if not await _check_member(update.effective_user, update.get_bot()): return
        total = pool_count()
        await update.message.reply_text(
            "📲 *Service choose karo:*" if total else "🔴 *Pool empty.* Admin se contact karo.",
            parse_mode="Markdown",
            reply_markup=kb_service_picker() if total else None
        )
        return

    if text in ("🔐 Get 2FA", "Get 2FA", "🔐 Get 2FA Code"):
        twofa_clear_all(user.id)
        _pending_2fa_input[user.id] = True
        await update.message.reply_text(
            "🔐 *2FA Code Generator*\n━━━━━━━━━━━━━━━━━━\n\n"
            "🔑 Secret Key type karo:\n\n"
            "_Example:_ `JBSWY3DPEHPK3PXP`",
            parse_mode="Markdown",
            reply_markup=main_reply_kb()
        )
        return

    # ── /getotp number input ─────────────────────────────────────────────────
    if _pending_getotp.pop(user.id, False):
        digits = _normalize(text)
        if len(digits) < 7 or len(digits) > 15:
            await update.message.reply_text(
                "❌ *Invalid number!*\n\nDobara type karo (sirf digits):\nExample: `79234451009`",
                parse_mode="Markdown"
            )
            _pending_getotp[user.id] = True
            return
        number = f"+{digits}"
        msg = await update.message.reply_text(
            f"⏳ *OTP fetch ho raha hai...*\n📱 `{number}`",
            parse_mode="Markdown"
        )
        await _do_fetch_otp(msg, number)
        return

    # ── 2FA direct input ─────────────────────────────────────────────────────
    if _pending_2fa_input.pop(user.id, False):
        import re as _re2
        clean_key = _re2.sub(r"[^A-Z2-7=]", "", text.strip().upper())
        if len(clean_key) < 8:
            await update.message.reply_text(
                "❌ *Invalid Key!* Base32 format chahiye.\n"
                "Example: `JBSWY3DPEHPK3PXP`\n\nDobara type karo:",
                parse_mode="Markdown"
            )
            _pending_2fa_input[user.id] = True
            return
        try:
            code = twofa_get_code(clean_key)
            rem  = twofa_get_remaining()
            twofa_add(user.id, clean_key, "")
            await update.message.reply_text(
                f"✅ *2FA Code*\n━━━━━━━━━━━━━━━━━━\n\n"
                f"🔑 Code    : `{code}`\n"
                f"⏱ Expires : `{rem}s`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔄 Refresh Code", callback_data=f"2fa_refresh:{clean_key}")],
                    [InlineKeyboardButton("🔐 New Key", callback_data="2fa_new_key")],
                ])
            )
        except Exception as e:
            await update.message.reply_text(f"❌ Key error: `{e}`\n\nDobara try karo:", parse_mode="Markdown")
            _pending_2fa_input[user.id] = True
        return

    # ── Pending bulk number input ─────────────────────────────────────────────
    if user.id not in _pending_bulk: return
    _pending_bulk.pop(user.id, None)
    clean = _normalize(text)
    if len(clean) < 7:
        await update.message.reply_text("❌ Invalid number format.", parse_mode="Markdown")
        return


# ─────────────────────────────────────────────────────────────────────────────
# Error handler
# ─────────────────────────────────────────────────────────────────────────────
async def _error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    from telegram.error import BadRequest, NetworkError, TimedOut
    err = ctx.error
    if isinstance(err, BadRequest) and "query is too old" in str(err).lower(): return
    if isinstance(err, (NetworkError, TimedOut)): return
    logger.warning(f"⚠️ Error: {err}")


# ─────────────────────────────────────────────────────────────────────────────
# Startup
# ─────────────────────────────────────────────────────────────────────────────
async def post_init(app: Application):
    global _app
    _app = app
    # Auto-save hardcoded token if not already set
    _hardcoded = "Q1NVQUhBUzRzhJhTRlV0QnmYUoZ7clNSdFBndWhmdGNpZYWAYWNmZg"
    if not db_get_setting("cr_token"):
        db_set_setting("cr_token", _hardcoded)
        logger.info("✅ CR API token auto-saved")
    await start_notify_server()
    await app.bot.set_my_commands([
        BotCommand("start",       "Welcome & overview"),
        BotCommand("getnumber",   "Pool se number lo → OTP auto"),
        BotCommand("getotp",      "OTP fetch (CR API)"),
        BotCommand("2fa",         "2FA TOTP code nikalo"),
        BotCommand("mystatus",    "Assigned number history"),
        # 2FA via "Get 2FA" button — no command needed
        BotCommand("status",      "Bot + CR API status"),
        BotCommand("settoken",    "CR API token set karo"),
        BotCommand("import",      "Admin: pool import wizard"),
        BotCommand("bulkservice", "Admin: txt file bulk import by service"),
        BotCommand("addnumbers",  "Admin: inline add"),
        BotCommand("delnumber",   "Admin: delete one number"),
        BotCommand("delete",      "Admin: delete menu"),
        BotCommand("poolstatus",  "Admin: full pool stats"),
    ])
    logger.info(f"✅ Unified Bot ready | Pool: {pool_count()} | CR token: {'✅' if _cr_token() else '❌ NOT SET'}")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────
def main():
    global BOT_TOKEN, PROXY_URL

    print("=" * 55)
    print("  🤖  Unified OTP Bot (CR API + Pool + Watch)")
    print("=" * 55)

    db = _db()

    # ── Load all saved config from DB ────────────────────────────────────────
    saved_token       = db_get_setting("bot_token")
    saved_cr_token    = db_get_setting("cr_token")
    saved_admin       = db_get_setting("admin_ids")
    saved_channel     = db_get_setting("otp_channel_id")
    saved_otp_group   = db_get_setting("otp_group_link")
    saved_req_group   = db_get_setting("required_group_id")
    saved_req_link    = db_get_setting("required_group_link")
    saved_proxy       = db_get_setting("proxy_url")

    global BOT_TOKEN, PROXY_URL, OTP_CHANNEL_ID, OTP_GROUP_LINK
    global REQUIRED_GROUP_ID, REQUIRED_GROUP_LINK

    # ── Proxy ─────────────────────────────────────────────────────────────────
    if saved_proxy:
        PROXY_URL = saved_proxy
        print(f"✅ Proxy (saved): {PROXY_URL}")
    else:
        print("\n🌐 Proxy (optional — Enter to skip):")
        raw_proxy = input("Proxy [host:port:user:pass]: ").strip()
        if raw_proxy:
            PROXY_URL = raw_proxy
            db_set_setting("proxy_url", raw_proxy)
            print(f"✅ Proxy set: {_parse_proxy(PROXY_URL)}")
        else:
            PROXY_URL = None
            print("Direct connection.")

    # ── Bot Token ─────────────────────────────────────────────────────────────
    _eff_token = saved_token or (BOT_TOKEN if BOT_TOKEN != "YOUR_BOT_TOKEN_HERE" else "")
    if not _eff_token:
        _eff_token = input("\n🔑 TG_BOT_TOKEN (1 baar enter karo — save ho jaayega): ").strip()
        if not _eff_token:
            raise ValueError("TG_BOT_TOKEN required!")
    db_set_setting("bot_token", _eff_token)
    BOT_TOKEN = _eff_token
    print(f"✅ Bot Token: ...{BOT_TOKEN[-6:]}")

    # ── CR API Token ──────────────────────────────────────────────────────────
    _eff_cr = saved_cr_token or CR_API_TOKEN
    if not _eff_cr:
        _eff_cr = input("🔑 CR API Token: ").strip()
    if _eff_cr:
        db_set_setting("cr_token", _eff_cr)
        print(f"✅ CR Token: ...{_eff_cr[-6:]}")

    # ── Admin IDs ─────────────────────────────────────────────────────────────
    if saved_admin:
        for x in saved_admin.split(","):
            if x.strip().isdigit(): ADMIN_IDS.add(int(x.strip()))
    if not ADMIN_IDS:
        raw = input("👤 Telegram Admin ID: ").strip()
        if raw.lstrip("-").isdigit():
            ADMIN_IDS.add(int(raw))
            db_set_setting("admin_ids", raw)
    print(f"✅ Admins: {ADMIN_IDS}")

    # ── OTP Channel ───────────────────────────────────────────────────────────
    if saved_channel and saved_channel.lstrip("-").isdigit():
        OTP_CHANNEL_ID = int(saved_channel)
    elif not OTP_CHANNEL_ID:
        raw_ch = input("\n📢 OTP Channel ID (optional, Enter to skip): ").strip()
        if raw_ch.lstrip("-").isdigit():
            OTP_CHANNEL_ID = int(raw_ch)
            db_set_setting("otp_channel_id", raw_ch)
    if OTP_CHANNEL_ID: print(f"✅ OTP Channel: {OTP_CHANNEL_ID}")

    # ── OTP Group Link ────────────────────────────────────────────────────────
    if saved_otp_group:
        OTP_GROUP_LINK = saved_otp_group
    elif not OTP_GROUP_LINK:
        raw_og = input("\n👁 OTP Group Link (Enter to skip): ").strip()
        if raw_og:
            OTP_GROUP_LINK = raw_og
            db_set_setting("otp_group_link", raw_og)
    if OTP_GROUP_LINK: print(f"✅ OTP Group: {OTP_GROUP_LINK}")

    # ── Required Group ────────────────────────────────────────────────────────
    if saved_req_group and saved_req_group.lstrip("-").isdigit():
        REQUIRED_GROUP_ID   = int(saved_req_group)
        REQUIRED_GROUP_LINK = saved_req_link
    elif not REQUIRED_GROUP_ID:
        raw_rg = input("\n🔐 Required Group ID (optional, Enter to skip): ").strip()
        if raw_rg.lstrip("-").isdigit():
            REQUIRED_GROUP_ID   = int(raw_rg)
            db_set_setting("required_group_id", raw_rg)
            raw_rl = input("   Group invite link: ").strip()
            REQUIRED_GROUP_LINK = raw_rl
            db_set_setting("required_group_link", raw_rl)
    if REQUIRED_GROUP_ID: print(f"✅ Required Group: {REQUIRED_GROUP_ID}")

    print("\n✅ Config saved! Agla baar sirf start karo — kuch enter nahi karna.\n")
    db.close()

    if PROXY_URL:
        _px = _parse_proxy(PROXY_URL)
        app = Application.builder().token(BOT_TOKEN)\
            .request(HTTPXRequest(proxy=_px))\
            .get_updates_request(HTTPXRequest(proxy=_px))\
            .post_init(post_init).build()
    else:
        app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    for cmd, handler in [
        ("start", cmd_start), ("help", cmd_help), ("getnumber", cmd_getnumber),
        ("getotp", cmd_getotp), ("mystatus", cmd_mystatus),
        ("2fa", cmd_2fa), ("2faclear", cmd_2faclear),
        ("settoken", cmd_settoken), ("status", cmd_status),
        ("import", cmd_import), ("bulkservice", cmd_bulkservice), ("addnumbers", cmd_addnumbers),
        ("delnumber", cmd_delnumber), ("delete", cmd_delete), ("poolstatus", cmd_poolstatus),
    ]:
        app.add_handler(CommandHandler(cmd, handler))

    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_error_handler(_error_handler)

    # Auto-reconnect loop
    while True:
        try:
            app.run_polling(drop_pending_updates=True)
            break
        except KeyboardInterrupt:
            logger.info("🛑 Stopped."); break
        except Exception as e:
            if _is_network_error(e):
                logger.warning(f"🔌 Connection lost: {e}")
                start = _time.time()
                while _time.time() - start < 300:
                    _time.sleep(10)
                    try:
                        s = _socket.create_connection(("8.8.8.8", 53), timeout=3)
                        s.close(); logger.info("✅ Internet back — restarting..."); break
                    except OSError: pass
                else:
                    logger.error("❌ No internet for 5min. Exiting."); break
                _time.sleep(3)
            else:
                logger.error(f"❌ Fatal: {e}"); break


if __name__ == "__main__":
    main()
