"""Настройки, которые меняются на лету (из панели или когда бота добавили в канал)."""
import hashlib, hmac
from .config import cfg

state = {"channel_id": "", "channel_title": "", "channel_url": "", "bot_username": ""}


async def refresh(db):
    from . import db as dbm
    s = await dbm.get_settings(db)
    state["channel_id"] = s.get("channel_id") or cfg.channel
    state["channel_title"] = s.get("channel_title") or ""
    state["channel_url"] = s.get("channel_url") or cfg.channel_url


def channel_chat():
    """id канала для проверки подписки: число (-100…) или @username. Пусто — подписку не проверяем."""
    c = (state["channel_id"] or cfg.channel or "").strip()
    if not c or "t.me/" in c or c.startswith("+"):
        return None   # инвайт-ссылку проверить нельзя — нужен id канала
    if c.lstrip("-").isdigit():
        return int(c)
    return c if c.startswith("@") else "@" + c


def channel_url() -> str:
    url = state["channel_url"] or cfg.channel_url
    if url:
        return url
    c = channel_chat()
    if isinstance(c, str):
        return f"https://t.me/{c.lstrip('@')}"
    return "https://t.me/orsofeystudio"


def secret() -> bytes:
    return hashlib.sha256(("orsofey-crm:" + cfg.token).encode()).digest()


def sign(text: str, n: int = 16) -> str:
    return hmac.new(secret(), text.encode(), hashlib.sha256).hexdigest()[:n]


def download_link(user_id: int) -> str:
    """Ссылка на скачивание через бота — так CRM видит, кто реально перешёл к скачиванию."""
    if cfg.public_url and cfg.app_url:
        return f"{cfg.public_url}/go/{user_id}/{sign(str(user_id), 10)}"
    return cfg.app_url
