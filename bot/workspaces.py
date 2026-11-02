"""Пространства CRM в одном боте.

main — CRM автора: бот, воронка, люди, сделки, соцсети, аналитика, публикация.
u<telegram id> — своя CRM друга: отдельная база, свои соцсети, аналитика и публикация (без воронки бота).
Друг пишет боту /panel → автору приходит запрос с кнопками → после одобрения бот сам присылает другу вход.
Ключи приложений автора (YouTube, VK, TikTok, Instagram, прокси) друг использует, если не задал свои.
"""
import os, re
from . import db as dbm
from .config import cfg

MAIN = "main"
_dbs: dict = {}
SCHEMA = """CREATE TABLE IF NOT EXISTS workspaces(
  tg_id INTEGER PRIMARY KEY, name TEXT, username TEXT, status TEXT, created_at INTEGER, decided_at INTEGER
)"""
STATUS_NAME = {"pending": "ждёт решения", "own": "своя CRM", "view": "доступ к твоей CRM", "denied": "отказано"}


def ws_dir() -> str:
    d = os.path.join(os.path.dirname(cfg.db_path) or "data", "workspaces")
    os.makedirs(d, exist_ok=True)
    return d


def valid(ws: str) -> bool:
    return ws == MAIN or bool(re.fullmatch(r"u\d{3,15}", ws or ""))


async def init(main_db):
    _dbs[MAIN] = main_db
    await main_db.execute(SCHEMA)
    await main_db.commit()
    dbm.SHARED_FROM = main_db          # ключи приложений автора — запасные для пространств друзей


def main_db():
    return _dbs[MAIN]


async def get_db(ws: str):
    if ws == MAIN or not valid(ws):
        return _dbs[MAIN]
    if ws not in _dbs:
        db = await dbm.connect(os.path.join(ws_dir(), f"{ws}.db"))
        await dbm.set_setting(db, "ws_owner", ws[1:])
        _dbs[ws] = db
    return _dbs[ws]


async def get(tg_id: int):
    cur = await main_db().execute("SELECT * FROM workspaces WHERE tg_id=?", (tg_id,))
    return await cur.fetchone()


async def access_for(tg_id: int) -> str | None:
    """Куда пускать человека по /panel: main, своя CRM или никуда."""
    if tg_id in cfg.admins or tg_id in cfg.panel_ids:
        return MAIN
    w = await get(tg_id)
    if w and w["status"] == "view":
        return MAIN
    if w and w["status"] == "own":
        return f"u{tg_id}"
    return None


async def request(user) -> str:
    """Записать запрос на доступ. Возвращает текущий статус."""
    w = await get(user.id)
    name = " ".join(x for x in (user.first_name, getattr(user, "last_name", None)) if x) or str(user.id)
    if w:
        await main_db().execute("UPDATE workspaces SET name=?, username=? WHERE tg_id=?", (name, user.username, user.id))
        await main_db().commit()
        return w["status"]
    await main_db().execute("INSERT INTO workspaces(tg_id, name, username, status, created_at) VALUES(?,?,?,?,?)",
                            (user.id, name, user.username, "pending", dbm.now()))
    await main_db().commit()
    return "new"


async def decide(tg_id: int, status: str):
    assert status in ("own", "view", "denied", "pending")
    await main_db().execute("UPDATE workspaces SET status=?, decided_at=? WHERE tg_id=?", (status, dbm.now(), tg_id))
    await main_db().commit()
    if status == "own":
        await get_db(f"u{tg_id}")


async def all_rows() -> list:
    cur = await main_db().execute("SELECT * FROM workspaces ORDER BY created_at DESC")
    return [dict(r) for r in await cur.fetchall()]


async def active() -> list[str]:
    """Все пространства для фоновых задач: CRM автора + своя CRM каждого одобренного друга."""
    cur = await main_db().execute("SELECT tg_id FROM workspaces WHERE status='own'")
    return [MAIN] + [f"u{r[0]}" for r in await cur.fetchall()]


async def is_main(db) -> bool:
    return not (await dbm.get_setting(db, "ws_owner", ""))
