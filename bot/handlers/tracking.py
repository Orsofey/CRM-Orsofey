"""Служебные обновления: человек заблокировал/разблокировал бота; бота сделали админом канала."""
from aiogram import Router, Bot
from aiogram.enums import ChatMemberStatus as S
from aiogram.types import ChatMemberUpdated
from .. import db as dbm, notify, runtime
from ..config import cfg

router = Router()


@router.my_chat_member()
async def my_member(ev: ChatMemberUpdated, bot: Bot, db):
    new, old = ev.new_chat_member.status, ev.old_chat_member.status
    if ev.chat.type == "private":
        if new == S.KICKED:
            await dbm.set_blocked(db, ev.from_user.id, True)
        elif new == S.MEMBER and old == S.KICKED:
            await dbm.set_blocked(db, ev.from_user.id, False)
        return
    if ev.chat.type != "channel":
        return
    from .. import social, workspaces
    try:
        main = workspaces.main_db()
    except KeyError:
        main = db
    by = ev.from_user.id if ev.from_user else None
    if new in (S.LEFT, S.KICKED):
        await social.bot_channel_forget(main, ev.chat.id)
        return
    if new == S.ADMINISTRATOR and old != S.ADMINISTRATOR:
        await social.bot_channel_remember(main, ev.chat.id, ev.chat.title or "", getattr(ev.chat, "username", None) or "", by)
        is_admin = by in cfg.admins
        # канал проверки подписки ставим сам, только если его ещё нет и бота добавил автор (раньше любой новый канал
        # перезаписывал его — теперь у бота бывает несколько каналов: публикации, 18+, каналы клиентов)
        if is_admin and not (await dbm.get_setting(main, "channel_id", "")):
            await dbm.set_setting(main, "channel_id", str(ev.chat.id))
            await dbm.set_setting(main, "channel_title", ev.chat.title or "")
            await runtime.refresh(main)
            await notify.admins(bot, f"✅ Бот стал админом канала «{ev.chat.title}» (id {ev.chat.id}).\n"
                                     f"Подписка теперь проверяется на этом канале. Поменять можно в CRM → Настройки.")
            return
        text = (f"✅ Бот стал админом канала «{ev.chat.title}».\n"
                f"Чтобы видеть его статистику и публиковать туда, открой CRM → Настройки → Соцсети → Telegram — "
                f"канал будет в списке «Каналы, где бот админ», нажми «Добавить». "
                f"Подойдёт и закрытый канал — ссылка-приглашение не нужна.")
        try:
            await bot.send_message(by, text) if by and not is_admin else await notify.admins(bot, text)
        except Exception:
            pass
