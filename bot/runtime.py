"""Настройки, которые меняются на лету (из панели или когда бота добавили в канал)."""
import hashlib, hmac, os, secrets
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


_secret: bytes | None = None


def secret() -> bytes:
    """Ключ подписи входа в панель и ссылок. Не зависит от токена бота: SECRET_KEY из окружения,
    иначе случайный ключ, один раз созданный рядом с базой (на Amvera — в постоянном /data)."""
    global _secret
    if _secret is None:
        key = os.getenv("SECRET_KEY", "").strip()
        if not key:
            path = os.path.join(os.path.dirname(cfg.db_path) or "data", "secret.key")
            try:
                with open(path) as f:
                    key = f.read().strip()
            except FileNotFoundError:
                pass
            if not key:
                key = secrets.token_hex(32)
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
                    f.write(key)
        _secret = hashlib.sha256(("orsofey-crm:" + key).encode()).digest()
    return _secret


def sign(text: str, n: int = 16) -> str:
    return hmac.new(secret(), text.encode(), hashlib.sha256).hexdigest()[:n]


def download_link(user_id: int) -> str:
    """Ссылка на скачивание через бота — так CRM видит, кто реально перешёл к скачиванию."""
    if cfg.public_url and cfg.app_url:
        return f"{cfg.public_url}/go/{user_id}/{sign(str(user_id), 10)}"
    return cfg.app_url
