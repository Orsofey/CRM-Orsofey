import logging
from aiogram import Bot
from .config import cfg

log = logging.getLogger(__name__)
REPLY_HINT = "↩️ Ответь на это сообщение — бот перешлёт твой ответ человеку."


def who(user) -> str:
    """user — aiogram User или строка базы."""
    get = (lambda k: user[k]) if not hasattr(user, "id") else (lambda k: getattr(user, k))
    uname = get("username")
    name = get("first_name") or ""
    return (f"@{uname}" if uname else name or str(get("id"))) + (f" ({name})" if uname and name else "")


async def admins(bot: Bot, text: str, user_id: int | None = None, reply_markup=None):
    """Сообщение автору. С user_id — в конце метка id:…, по ней работают ответы прямо из Telegram."""
    if user_id:
        text = f"{text}\n\n{REPLY_HINT}\nid:{user_id}"
    for a in cfg.admins:
        try:
            await bot.send_message(a, text[:4000], reply_markup=reply_markup, disable_web_page_preview=True)
        except Exception as e:
            log.info("Не доставлено админу %s: %s", a, e)
