import json, os, re, time
import aiosqlite
from .config import cfg

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
  source TEXT, created_at INTEGER,
  gift_at INTEGER, tested_at INTEGER, paid_until INTEGER,
  f_test INTEGER DEFAULT 0, f_feedback INTEGER DEFAULT 0, f_final INTEGER DEFAULT 0,
  blocked INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS payments(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, amount INTEGER, currency TEXT,
  charge_id TEXT, recurring INTEGER, expires_at INTEGER, created_at INTEGER
);
CREATE TABLE IF NOT EXISTS feedback(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, text TEXT, created_at INTEGER
);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, type TEXT, meta TEXT, created_at INTEGER
);
CREATE INDEX IF NOT EXISTS ix_events_user ON events(user_id, created_at);
CREATE INDEX IF NOT EXISTS ix_events_type ON events(type, created_at);
CREATE TABLE IF NOT EXISTS content(
  code TEXT PRIMARY KEY, platform TEXT, kind TEXT, title TEXT, url TEXT, keyword TEXT,
  views INTEGER DEFAULT 0, views_at INTEGER, published_at INTEGER, created_at INTEGER
);
CREATE TABLE IF NOT EXISTS notes(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, text TEXT, created_at INTEGER
);
CREATE TABLE IF NOT EXISTS sales(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, product TEXT, amount REAL, currency TEXT,
  note TEXT, created_at INTEGER
);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS content_stats(
  id INTEGER PRIMARY KEY AUTOINCREMENT, code TEXT, at INTEGER, views INTEGER, likes INTEGER, comments INTEGER
);
CREATE INDEX IF NOT EXISTS ix_cstats ON content_stats(code, at);
CREATE TABLE IF NOT EXISTS channel_stats(
  id INTEGER PRIMARY KEY AUTOINCREMENT, platform TEXT, at INTEGER, followers INTEGER, views INTEGER, extra TEXT
);
CREATE TABLE IF NOT EXISTS posts(
  id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, text TEXT, keyword TEXT, kind TEXT,
  file TEXT, file_name TEXT, file_size INTEGER, duration REAL, width INTEGER, height INTEGER, token TEXT,
  publish_at INTEGER, created_at INTEGER, notified INTEGER DEFAULT 0, deleted_file INTEGER DEFAULT 0,
  ptype TEXT DEFAULT 'video', files TEXT
);
CREATE TABLE IF NOT EXISTS post_targets(
  id INTEGER PRIMARY KEY AUTOINCREMENT, post_id INTEGER, platform TEXT, mode TEXT, code TEXT, caption TEXT,
  status TEXT, ext_id TEXT, url TEXT, error TEXT, state TEXT, updated_at INTEGER
);
CREATE INDEX IF NOT EXISTS ix_targets ON post_targets(post_id);
CREATE TABLE IF NOT EXISTS accounts(
  id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, ext TEXT, name TEXT, ref TEXT, data TEXT DEFAULT '{}',
  status TEXT, hidden INTEGER DEFAULT 0, created_at INTEGER
);
CREATE TABLE IF NOT EXISTS packs(
  code TEXT PRIMARY KEY, title TEXT, intro TEXT, button TEXT, caption TEXT, keyword TEXT,
  mode TEXT DEFAULT 'files', need_sub INTEGER DEFAULT 1, platform TEXT DEFAULT 'YouTube', kind TEXT DEFAULT 'Видео',
  files TEXT DEFAULT '[]', cache TEXT DEFAULT '{}', active INTEGER DEFAULT 1, created_at INTEGER
);
"""

# Колонки, которых не было в первой версии бота, — добавляются к старой базе автоматически
USER_COLUMNS = {
    "platform": "TEXT", "source_first": "TEXT",
    "subscribed_at": "INTEGER", "download_at": "INTEGER", "offer_at": "INTEGER", "paid_at": "INTEGER",
    "last_seen": "INTEGER", "r_start": "INTEGER DEFAULT 0", "r_download": "INTEGER DEFAULT 0",
    "lead_status": "TEXT", "lead_at": "INTEGER",
}

# Колонки контента для автоподгрузки из соцсетей
CONTENT_COLUMNS = {"ext_id": "TEXT", "likes": "INTEGER DEFAULT 0", "comments": "INTEGER DEFAULT 0", "synced": "TEXT"}

# ---------- Этапы воронки (по порядку) ----------
STAGES = [
    ("start", "Зашёл в бота", "created_at"),
    ("subscribed", "Подписался на канал", "subscribed_at"),
    ("gift", "Получил лид-магнит", "gift_at"),
    ("test", "Нажал «Протестировать»", "tested_at"),
    ("download", "Перешёл к скачиванию", "download_at"),
    ("offer", "Открыл подписку", "offer_at"),
    ("paid", "Оплатил", "paid_at"),
]
STAGE_COL = {k: c for k, _, c in STAGES}
STAGE_NAME = {k: n for k, n, _ in STAGES}
# Чего человек НЕ сделал, если застрял на этапе
NEXT_STEP = {
    "start": "не подписался на канал",
    "subscribed": "не забрал лид-магнит",
    "gift": "не нажал «Протестировать»",
    "test": "не перешёл к скачиванию",
    "download": "не открыл подписку",
    "offer": "не оплатил",
}

# Личная работа (переписка → созвон → сделка)
LEAD_STATUSES = [
    ("request", "Заявка"), ("dialog", "Переписка"), ("call", "Созвон"), ("won", "Оплачено"), ("lost", "Отказ"),
]
LEAD_NAME = dict(LEAD_STATUSES)

PRODUCTS = {"agent": "Подписка на Агента", "automation": "Автоматизация под бизнес", "other": "Другое"}

DEFAULT_SETTINGS = {
    "rem_start": "1", "rem_test": "1", "rem_download": "1", "rem_feedback": "1", "rem_final": "1",
    "report_on": "1", "channel_id": "", "channel_title": "", "channel_url": "",
    "yt_channel": "", "yt_api_key": "", "vk_owner": "", "vk_token": "",
    "tt_client_key": "", "tt_client_secret": "", "ig_app_id": "", "ig_app_secret": "", "social_proxy": "",
    "post_channel": "", "post_platforms": "yt,tt,ig,vk,tg",
    "x_user": "", "x_bearer": "", "x_max_posts": "40",
    "pt_client_id": "", "pt_client_secret": "", "pt_access": "", "pt_refresh": "",
    "manual_tg": "0",           # присылать в Telegram материалы для ручной публикации (по умолчанию — нет, всё в CRM)
}

# ---------- Метки источников ----------
# yt_v12 — ролик YouTube, yts_v12_3 — шортс YouTube, tt_ / ig_ / vk_ / vkc_ — TikTok, Instagram, VK, клипы VK,
# vkp_ — пост VK, tg_ — пост в Telegram, *_bio — ссылка в профиле. Старые yt01 / s03 / tg01 / tt01 тоже понимаются.
PREFIX = {
    "yts": ("YouTube", "Шортс"), "yt": ("YouTube", "Видео"), "s": ("YouTube", "Шортс"),
    "tt": ("TikTok", "Шортс"), "ig": ("Instagram", "Reels"), "insta": ("Instagram", "Reels"),
    "vkc": ("VK", "Клип"), "vkp": ("VK", "Пост"), "vk": ("VK", "Видео"),
    "tg": ("Telegram", "Пост"),
    "x": ("X", "Пост"), "tw": ("X", "Пост"), "pt": ("Patreon", "Пост"),
    "bs": ("Bluesky", "Пост"), "pn": ("Pinterest", "Пин"), "dc": ("Discord", "Пост"), "fa": ("Fur Affinity", "Работа"),
    "ss": ("SubscribeStar", "Пост"),          # метки e621 — e621_<id> (см. parse_code)
}
PLATFORMS = ["YouTube", "TikTok", "Instagram", "VK", "Telegram", "X", "Bluesky", "Pinterest", "Discord", "Fur Affinity", "e621",
             "Patreon", "SubscribeStar", "Другое", "Без метки"]


PACK_LABELS: dict[str, tuple[str, str]] = {}   # ссылки на наборы (?start=skills) → откуда ссылка (ставит packs.reload)


def parse_code(code: str | None) -> tuple[str, str]:
    """Метка → (платформа, формат)."""
    if not code or code == "direct":
        return "Без метки", "—"
    c = code.lower()
    if c in PACK_LABELS:
        return PACK_LABELS[c]
    if c.startswith("e621"):
        return "e621", "Пост"
    m = re.match(r"[a-z]+", c)
    prefix = m.group(0) if m else ""
    plat, kind = PREFIX.get(prefix, ("Другое", "—"))
    if c.endswith("_bio") or c.endswith("-bio"):
        kind = "Ссылка в профиле"
    return plat, kind


def clean_code(code: str | None) -> str | None:
    if not code:
        return None
    code = re.sub(r"[^A-Za-z0-9_\-]", "", code)[:64]
    return code or None


def now() -> int:
    return int(time.time())


# ---------- Подключение ----------
async def connect(path: str | None = None) -> aiosqlite.Connection:
    path = path or cfg.db_path
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    db = await aiosqlite.connect(path)
    db.row_factory = aiosqlite.Row
    # WAL: панель читает, пока планировщик пишет; NORMAL — без fsync на каждый commit (в WAL это безопасно)
    for pragma in ("journal_mode=WAL", "synchronous=NORMAL", "busy_timeout=5000"):
        await db.execute(f"PRAGMA {pragma}")
    await db.executescript(SCHEMA)
    await migrate(db)
    await db.commit()
    return db


async def migrate(db):
    cur = await db.execute("PRAGMA table_info(users)")
    have = {r[1] for r in await cur.fetchall()}
    for col, typ in USER_COLUMNS.items():
        if col not in have:
            await db.execute(f"ALTER TABLE users ADD COLUMN {col} {typ}")
    cur = await db.execute("PRAGMA table_info(content)")
    have = {r[1] for r in await cur.fetchall()}
    for col, typ in CONTENT_COLUMNS.items():
        if col not in have:
            await db.execute(f"ALTER TABLE content ADD COLUMN {col} {typ}")
    await db.execute("CREATE INDEX IF NOT EXISTS ix_content_ext ON content(ext_id)")
    cur = await db.execute("PRAGMA table_info(posts)")
    have_p = {r[1] for r in await cur.fetchall()}
    for col, typ in (("ptype", "TEXT DEFAULT 'video'"), ("files", "TEXT"), ("adult", "TEXT DEFAULT ''"), ("tags", "TEXT DEFAULT ''"),
                     ("source", "TEXT DEFAULT ''")):
        if col not in have_p:
            await db.execute(f"ALTER TABLE posts ADD COLUMN {col} {typ}")
    cur = await db.execute("PRAGMA table_info(channel_stats)")
    if "extra" not in {r[1] for r in await cur.fetchall()}:
        await db.execute("ALTER TABLE channel_stats ADD COLUMN extra TEXT")
    for tbl in ("content", "channel_stats"):
        cur = await db.execute(f"PRAGMA table_info({tbl})")
        if "account" not in {r[1] for r in await cur.fetchall()}:
            await db.execute(f"ALTER TABLE {tbl} ADD COLUMN account INTEGER")
    await migrate_accounts(db)
    for ix in ("ix_users_created ON users(created_at)", "ix_users_source ON users(source)",
               "ix_users_lead ON users(lead_status, lead_at)", "ix_sales_user ON sales(user_id)",
               "ix_sales_created ON sales(created_at)", "ix_notes_user ON notes(user_id)",
               "ix_payments_user ON payments(user_id)", "ix_payments_created ON payments(created_at)",
               "ix_chstats ON channel_stats(platform, at)"):
        await db.execute(f"CREATE INDEX IF NOT EXISTS {ix}")
    # Скрытые аккаунты соцсетей: их ролики, подписчики и люди с их роликов не попадают в воронку и аналитику
    for v in ("content_v", "channel_stats_v", "users_v"):
        await db.execute(f"DROP VIEW IF EXISTS {v}")
    await db.execute(f"CREATE VIEW content_v AS SELECT * FROM content WHERE {VISIBLE_SQL}")
    await db.execute(f"CREATE VIEW channel_stats_v AS SELECT * FROM channel_stats WHERE {VISIBLE_SQL}")
    await db.execute("CREATE VIEW users_v AS SELECT * FROM users WHERE source IS NULL OR source NOT IN "
                     f"(SELECT code FROM content WHERE NOT ({VISIBLE_SQL}))")
    # старым записям — платформу по метке и «первый источник»
    cur = await db.execute("SELECT id, source FROM users WHERE platform IS NULL")
    for uid, src in await cur.fetchall():
        await db.execute("UPDATE users SET platform=?, source_first=COALESCE(source_first, source) WHERE id=?",
                         (parse_code(src)[0], uid))
    # первые оплаты из старой таблицы payments
    await db.execute("UPDATE users SET paid_at=(SELECT MIN(created_at) FROM payments p WHERE p.user_id=users.id) "
                     "WHERE paid_at IS NULL AND EXISTS(SELECT 1 FROM payments p WHERE p.user_id=users.id)")
    for k, v in DEFAULT_SETTINGS.items():
        await db.execute("INSERT OR IGNORE INTO settings(key, value) VALUES(?,?)", (k, v))


VISIBLE_SQL = "(account IS NULL OR account NOT IN (SELECT id FROM accounts WHERE hidden=1))"
ACC_SOURCE = {"yt": "youtube", "vk": "vk", "tt": "tiktok", "ig": "instagram", "x": "x", "pt": None, "tg": "telegram",
              "bs": "bluesky", "pn": "pinterest", "dc": "discord", "fa": "furaffinity", "e6": "e621", "ss": None}
ACC_PLATFORM = {"yt": "YouTube", "vk": "VK", "tt": "TikTok", "ig": "Instagram", "x": "X", "pt": "Patreon", "tg": "Telegram",
                "bs": "Bluesky", "pn": "Pinterest", "dc": "Discord", "fa": "Fur Affinity", "e6": "e621", "ss": "SubscribeStar"}


async def connected_platforms(db) -> list[str]:
    """Соцсети, у которых есть хоть один видимый аккаунт (в порядке PLATFORMS)."""
    cur = await db.execute("SELECT DISTINCT kind FROM accounts WHERE hidden=0")
    have = {ACC_PLATFORM.get(k) for (k,) in await cur.fetchall()}
    return [p for p in PLATFORMS if p in have]


async def migrate_accounts(db):
    """Раньше на соцсеть был один аккаунт в настройках — переносим его в список аккаунтов (один раз)."""
    cur = await db.execute("SELECT value FROM settings WHERE key='accounts_migrated'")
    if await cur.fetchone():
        return
    cur = await db.execute("SELECT key, value FROM settings")
    s = {k: v for k, v in await cur.fetchall()}
    now_ = now()

    def st(k):
        try:
            return json.loads(s.get(f"{k}_status") or "null") or {}
        except ValueError:
            return {}
    found = []
    if s.get("yt_channel"):
        found.append(("yt", s["yt_channel"], st("yt").get("title") or s["yt_channel"], {}))
    if s.get("vk_owner"):
        found.append(("vk", s["vk_owner"], st("vk").get("title") or s["vk_owner"], {}))
    if s.get("tt_access"):
        found.append(("tt", "", s.get("tt_user") or "TikTok", {"access": s["tt_access"], "refresh": s.get("tt_refresh", ""),
                                                              "expires": int(s.get("tt_expires") or 0)}))
    if s.get("ig_token"):
        found.append(("ig", "", s.get("ig_user") or "Instagram", {"token": s["ig_token"], "expires": int(s.get("ig_expires") or 0),
                                                                 "token_at": int(s.get("ig_token_at") or 0), "user_id": s.get("ig_user_id", "")}))
    if s.get("x_user"):
        found.append(("x", s["x_user"], "@" + s["x_user"].lstrip("@"), {}))
    if s.get("pt_access"):
        found.append(("pt", "", st("pt").get("title") or "Patreon", {"access": s["pt_access"], "refresh": s.get("pt_refresh", ""),
                                                                   "expires": int(s.get("pt_expires") or 0)}))
    for kind, ref, name, data in found:
        await db.execute("INSERT INTO accounts(kind, ext, name, ref, data, status, hidden, created_at) VALUES(?,?,?,?,?,?,0,?)",
                         (kind, "", name, ref, json.dumps(data), s.get(f"{kind}_status"), now_))
        cur = await db.execute("SELECT last_insert_rowid()")
        aid = (await cur.fetchone())[0]
        if ACC_SOURCE[kind]:
            await db.execute("UPDATE content SET account=? WHERE synced=? AND account IS NULL", (aid, ACC_SOURCE[kind]))
        await db.execute("UPDATE channel_stats SET account=? WHERE platform=? AND account IS NULL", (aid, ACC_PLATFORM[kind]))
    await db.execute("INSERT OR REPLACE INTO settings(key, value) VALUES('accounts_migrated', '1')")


# ---------- Настройки ----------
# Ключи приложений автора, которыми пользуются CRM друзей, если не задали свои
SHARED_KEYS = ("yt_api_key", "vk_token", "tt_client_key", "tt_client_secret", "ig_app_id", "ig_app_secret", "social_proxy",
               "x_bearer", "pt_client_id", "pt_client_secret", "pn_client_id", "pn_client_secret", "pn_standard",
               "dc_client_id", "dc_client_secret", "ss_client_id", "ss_client_secret", "ss_domain", "ss_scopes", "faexport_url",
               "yt_client_id", "yt_client_secret", "vk_app_id", "vk_app_secret", "x_client_id", "x_client_secret", "tt_publish", "tt_stats")
SHARED_FROM = None          # база автора (ставит workspaces.init)


async def get_settings(db) -> dict:
    cur = await db.execute("SELECT key, value FROM settings")
    out = dict(DEFAULT_SETTINGS)
    out.update({k: v for k, v in await cur.fetchall()})
    if SHARED_FROM is not None and db is not SHARED_FROM:
        cur = await SHARED_FROM.execute(f"SELECT key, value FROM settings WHERE key IN ({','.join('?' * len(SHARED_KEYS))})", SHARED_KEYS)
        for k, v in await cur.fetchall():
            if v and not out.get(k):
                out[k] = v
                out[k + "__shared"] = "1"
    return out


async def get_setting(db, key: str, default: str = "") -> str:
    cur = await db.execute("SELECT value FROM settings WHERE key=?", (key,))
    r = await cur.fetchone()
    val = r[0] if r and r[0] is not None else DEFAULT_SETTINGS.get(key, default)
    if not val and key in SHARED_KEYS and SHARED_FROM is not None and db is not SHARED_FROM:
        return await get_setting(SHARED_FROM, key, default)
    return val


async def set_setting(db, key: str, value):
    await db.execute("INSERT INTO settings(key, value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, "" if value is None else str(value)))
    await db.commit()


# ---------- Пользователи и события ----------
async def log_event(db, user_id: int, type_: str, meta: dict | None = None, commit: bool = True):
    await db.execute("INSERT INTO events(user_id, type, meta, created_at) VALUES(?,?,?,?)",
                     (user_id, type_, json.dumps(meta, ensure_ascii=False) if meta else None, now()))
    await db.execute("UPDATE users SET last_seen=? WHERE id=?", (now(), user_id))
    if commit:
        await db.commit()


async def ensure_content(db, code: str | None):
    """Неизвестная метка сама появляется во вкладке «Контент» — останется дописать название и просмотры."""
    if not code or code == "direct":
        return
    plat, kind = parse_code(code)
    await db.execute("INSERT OR IGNORE INTO content(code, platform, kind, title, created_at) VALUES(?,?,?,?,?)",
                     (code, plat, kind, "", now()))


async def upsert_user(db, u, source: str | None):
    """Создать/обновить человека. source — метка из ссылки ?start=…; первая метка не перезаписывается."""
    source = clean_code(source)
    cur = await db.execute("SELECT source FROM users WHERE id=?", (u.id,))
    row = await cur.fetchone()
    if row is None:
        src = source or "direct"
        await db.execute(
            "INSERT INTO users(id, username, first_name, source, source_first, platform, created_at, last_seen) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (u.id, u.username, u.first_name, src, src, parse_code(src)[0], now(), now()))
        await ensure_content(db, source)
        await db.commit()
        return True
    await db.execute("UPDATE users SET username=?, first_name=?, blocked=0 WHERE id=?", (u.username, u.first_name, u.id))
    # пришёл «без метки» или по ссылке из профиля, а теперь — с конкретного ролика: уточняем источник
    if source and (row[0] in (None, "direct") or str(row[0]).endswith("_bio")) and source != row[0]:
        await set_source(db, u.id, source, commit=False)
    await db.commit()
    return False


async def set_source(db, user_id: int, code: str, commit: bool = True):
    await db.execute("UPDATE users SET source=?, platform=? WHERE id=?", (code, parse_code(code)[0], user_id))
    await ensure_content(db, code)
    if commit:
        await db.commit()


async def mark(db, user_id: int, field: str, value=None):
    assert field in {"gift_at", "tested_at", "paid_until", "f_test", "f_feedback", "f_final", "blocked",
                     "subscribed_at", "download_at", "offer_at", "paid_at", "r_start", "r_download"}
    await db.execute(f"UPDATE users SET {field}=? WHERE id=?", (now() if value is None else value, user_id))
    await db.commit()


async def stage(db, user_id: int, key: str, meta: dict | None = None):
    """Человек дошёл до этапа: пишем время первого достижения + событие в историю."""
    col = STAGE_COL[key]
    await db.execute(f"UPDATE users SET {col}=COALESCE({col}, ?) WHERE id=?", (now(), user_id))
    await log_event(db, user_id, key, meta)


async def set_blocked(db, user_id: int, blocked: bool):
    cur = await db.execute("SELECT blocked FROM users WHERE id=?", (user_id,))
    r = await cur.fetchone()
    if r is None or bool(r[0]) == blocked:
        return
    await db.execute("UPDATE users SET blocked=? WHERE id=?", (1 if blocked else 0, user_id))
    await log_event(db, user_id, "blocked" if blocked else "unblocked")


async def get_user(db, user_id: int):
    cur = await db.execute("SELECT * FROM users WHERE id=?", (user_id,))
    return await cur.fetchone()


async def add_payment(db, user_id, amount, currency, charge_id, recurring, expires_at):
    await db.execute("INSERT INTO payments(user_id, amount, currency, charge_id, recurring, expires_at, created_at) VALUES(?,?,?,?,?,?,?)",
                     (user_id, amount, currency, charge_id, int(bool(recurring)), expires_at, now()))
    await db.commit()
    await stage(db, user_id, "paid", {"amount": amount, "currency": currency, "recurring": bool(recurring)})


async def add_feedback(db, user_id, text):
    await db.execute("INSERT INTO feedback(user_id, text, created_at) VALUES(?,?,?)", (user_id, text, now()))
    await log_event(db, user_id, "user_msg", {"text": text[:2000]})


async def set_lead(db, user_id: int, status: str, by: str = "admin"):
    assert status in LEAD_NAME
    await db.execute("UPDATE users SET lead_status=?, lead_at=? WHERE id=?", (status, now(), user_id))
    await log_event(db, user_id, "lead", {"status": status, "by": by})


async def add_note(db, user_id: int, text: str):
    await db.execute("INSERT INTO notes(user_id, text, created_at) VALUES(?,?,?)", (user_id, text, now()))
    await db.commit()


async def add_sale(db, user_id: int, product: str, amount: float, currency: str = "RUB", note: str = ""):
    product = product if product in PRODUCTS else "other"
    await db.execute("INSERT INTO sales(user_id, product, amount, currency, note, created_at) VALUES(?,?,?,?,?,?)",
                     (user_id, product, amount, currency, note, now()))
    await log_event(db, user_id, "sale", {"product": product, "amount": amount, "currency": currency, "note": note},
                    commit=False)
    if product == "agent":
        await db.execute("UPDATE users SET paid_at=COALESCE(paid_at, ?) WHERE id=?", (now(), user_id))
    else:
        await db.execute("UPDATE users SET lead_status='won', lead_at=? WHERE id=?", (now(), user_id))
    await db.commit()


async def content_by_keyword(db, text: str, platform: str | None = None) -> str | None:
    """Кодовое слово из ролика («напиши АГЕНТ12 в бота») → метка ролика.
    Одно слово может стоять у одного ролика в разных соцсетях — тогда берём ту соцсеть, откуда человек пришёл
    (по ссылке из профиля: tt_bio → TikTok), а если неизвестно — первую по порядку TikTok → Instagram → остальные."""
    t = (text or "").strip().upper()
    if not t or len(t) > 80:
        return None
    cur = await db.execute("SELECT code, keyword FROM content WHERE keyword IS NOT NULL AND keyword != ''")
    words = set(re.findall(r"[\wА-ЯЁ]+", t))
    found = [code for code, kw in await cur.fetchall() if kw.strip() and (t == kw.strip().upper() or kw.strip().upper() in words)]
    if not found:
        return None
    if platform:
        same = [c for c in found if parse_code(c)[0] == platform]
        if same:
            return same[0]
    order = {"TikTok": 0, "Instagram": 1, "YouTube": 2, "VK": 3, "Telegram": 4}
    return sorted(found, key=lambda c: order.get(parse_code(c)[0], 9))[0]


async def stats(db) -> dict:
    """Короткая статистика для команды /stats."""
    q = lambda sql: db.execute(sql)
    out = {}
    for key, sql in {
        "users": "SELECT COUNT(*) FROM users",
        "subscribed": "SELECT COUNT(*) FROM users WHERE subscribed_at IS NOT NULL",
        "gift": "SELECT COUNT(*) FROM users WHERE gift_at IS NOT NULL",
        "tested": "SELECT COUNT(*) FROM users WHERE tested_at IS NOT NULL",
        "download": "SELECT COUNT(*) FROM users WHERE download_at IS NOT NULL",
        "paid": "SELECT COUNT(*) FROM users WHERE paid_at IS NOT NULL",
        "paid_now": f"SELECT COUNT(*) FROM users WHERE paid_until > {now()}",
        "payments": "SELECT COUNT(*) FROM payments",
        "stars": "SELECT COALESCE(SUM(amount),0) FROM payments WHERE currency='XTR'",
        "rub": "SELECT COALESCE(SUM(amount),0) FROM sales WHERE currency='RUB'",
        "blocked": "SELECT COUNT(*) FROM users WHERE blocked=1",
    }.items():
        out[key] = (await (await q(sql)).fetchone())[0]
    cur = await db.execute("SELECT platform, COUNT(*) c, SUM(gift_at IS NOT NULL) g, SUM(tested_at IS NOT NULL) t, "
                           "SUM(paid_at IS NOT NULL) p FROM users GROUP BY platform ORDER BY c DESC")
    out["by_platform"] = [dict(r) for r in await cur.fetchall()]
    return out
