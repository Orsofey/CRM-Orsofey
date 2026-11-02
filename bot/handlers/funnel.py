"""Воронка: /start → подписка на канал → подарок → тест приложения → подписка. Каждый шаг пишется в CRM."""
import logging
from aiogram import Router, F, Bot
from aiogram.enums import ChatMemberStatus
from aiogram.filters import CommandStart, CommandObject, Command
from aiogram.types import Message, CallbackQuery, FSInputFile
from .. import texts, keyboards as kb, db as dbm, notify, packs
from ..config import cfg
from ..runtime import channel_chat
from .payments import send_sub_offer

router = Router()
log = logging.getLogger(__name__)


async def is_subscribed(bot: Bot, user_id: int) -> tuple[bool, bool]:
    """(можно выдавать подарок, подписка реально проверена)."""
    chat = channel_chat()
    if not chat:
        return True, False
    try:
        m = await bot.get_chat_member(chat, user_id)
        ok = m.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR) or \
            (m.status == ChatMemberStatus.RESTRICTED and getattr(m, "is_member", False))
        return ok, True
    except Exception as e:  # бот не админ канала или канал указан неверно — не блокируем человека
        log.warning("Проверка подписки не удалась: %s", e)
        return True, False


@router.message(CommandStart())
async def start(message: Message, command: CommandObject, db):
    code = dbm.clean_code(command.args)
    pack = await packs.find_active(db, code.lower()) if code else None
    if pack:                                                   # отдельная ссылка на набор (?start=skills)
        is_new = await dbm.upsert_user(db, message.from_user, pack["code"])
        await _attribute(db, message.from_user.id, pack["code"], "")
        await dbm.log_event(db, message.from_user.id, "start", {"code": pack["code"], "new": is_new})
        await dbm.log_event(db, message.from_user.id, "pack_open", {"code": pack["code"]})
        await message.answer(pack["intro"], reply_markup=kb.pack_kb(pack))
        return
    is_new = await dbm.upsert_user(db, message.from_user, code)   # метка источника: ?start=yt_v12
    await dbm.log_event(db, message.from_user.id, "start", {"code": code or "direct", "new": is_new})
    await message.answer(texts.WELCOME, reply_markup=kb.gift_kb())


@router.message(Command("menu"))
async def menu(message: Message):
    await message.answer(texts.MENU, reply_markup=kb.menu_kb())


@router.message(Command("help"))
async def help_(message: Message):
    await message.answer(texts.HELP)


async def give_gift(bot: Bot, chat_id: int, user, db, via: str = "button"):
    await dbm.upsert_user(db, user, None)
    ok, verified = await is_subscribed(bot, user.id)
    if not ok:
        await dbm.log_event(db, user.id, "sub_check", {"ok": False})
        await bot.send_message(chat_id, texts.NEED_SUB, reply_markup=kb.sub_check_kb())
        return False
    await dbm.stage(db, user.id, "subscribed", {"verified": verified})
    if cfg.lead_file:
        try:
            await bot.send_document(chat_id, FSInputFile(cfg.lead_file), caption=texts.GIFT)
        except Exception as e:
            log.error("Не отправился файл лид-магнита %s: %s", cfg.lead_file, e)
            await bot.send_message(chat_id, texts.GIFT + (f"\n\n{cfg.lead_url}" if cfg.lead_url else ""))
    else:
        await bot.send_message(chat_id, texts.GIFT + (f"\n\n{cfg.lead_url}" if cfg.lead_url else ""))
    await dbm.stage(db, user.id, "gift", {"via": via})
    await bot.send_message(chat_id, texts.OFFER_TEST, reply_markup=kb.test_kb())
    return True


async def give_pack(bot: Bot, chat_id: int, user, db, pack: dict, via: str = "button"):
    """Набор по отдельной ссылке: та же проверка подписки, потом файлы, потом — предложение попробовать приложение."""
    await dbm.upsert_user(db, user, None)
    if pack["need_sub"]:
        ok, verified = await is_subscribed(bot, user.id)
        if not ok:
            await dbm.log_event(db, user.id, "sub_check", {"ok": False, "pack": pack["code"]})
            await bot.send_message(chat_id, texts.PACK_NEED_SUB, reply_markup=kb.pack_sub_kb(pack))
            return False
        await dbm.stage(db, user.id, "subscribed", {"verified": verified})
    try:
        await packs.send(bot, db, chat_id, pack)
    except Exception as e:
        log.error("Набор %s не отправился: %s", pack["code"], e)
        await bot.send_message(chat_id, texts.PACK_FAIL)
        await notify.admins(bot, f"⚠️ Набор «{pack['title']}» не отправился {notify.who(user)}: {e}")
        return False
    await dbm.log_event(db, user.id, "pack_got", {"code": pack["code"], "via": via})
    await dbm.stage(db, user.id, "gift", {"via": "pack", "pack": pack["code"]})
    await bot.send_message(chat_id, texts.PACK_AFTER, reply_markup=kb.test_kb())
    return True


@router.callback_query(F.data.startswith("pk:"))
async def pack_cb(call: CallbackQuery, bot: Bot, db):
    pack = await packs.find_active(db, call.data[3:])
    if not pack:
        await call.answer("Этот набор больше не раздаётся 🙏", show_alert=True)
        return
    ok = await give_pack(bot, call.message.chat.id, call.from_user, db, pack)
    await call.answer(None if ok else texts.NOT_SUBSCRIBED)


@router.callback_query(F.data == "gift")
async def gift_cb(call: CallbackQuery, bot: Bot, db):
    ok = await give_gift(bot, call.message.chat.id, call.from_user, db)
    await call.answer(None if ok else texts.NOT_SUBSCRIBED)


@router.message(F.text.lower().contains("orsofey") | F.text.lower().contains("орсофей"))
async def keyword(message: Message, bot: Bot, db):
    """Кодовое слово из роликов — сразу подарок."""
    await dbm.upsert_user(db, message.from_user, None)
    me = await dbm.get_user(db, message.from_user.id)
    code = await dbm.content_by_keyword(db, message.text, me["platform"] if me and me["source"] not in (None, "direct") else None)
    if code:
        await _attribute(db, message.from_user.id, code, message.text)
    await give_gift(bot, message.chat.id, message.from_user, db, via="keyword")


@router.callback_query(F.data == "test")
async def test_cb(call: CallbackQuery, db):
    await dbm.upsert_user(db, call.from_user, None)
    await dbm.stage(db, call.from_user.id, "test")
    await call.message.answer(texts.TEST_SENT, reply_markup=kb.app_kb(call.from_user.id))
    await call.answer()


@router.callback_query(F.data == "sub")
async def sub_cb(call: CallbackQuery, bot: Bot, db):
    await send_sub_offer(bot, call.message.chat.id, call.from_user, db)
    await call.answer()


@router.message(Command("sub"))
async def sub_cmd(message: Message, bot: Bot, db):
    await send_sub_offer(bot, message.chat.id, message.from_user, db)


# ---------- Автоматизации под бизнес (личные продажи) ----------
@router.callback_query(F.data == "auto")
async def auto_cb(call: CallbackQuery, db):
    await dbm.upsert_user(db, call.from_user, None)
    await dbm.log_event(db, call.from_user.id, "auto_view")
    await call.message.answer(texts.AUTO_INFO, reply_markup=kb.auto_kb())
    await call.answer()


@router.message(Command("auto"))
async def auto_cmd(message: Message, db):
    await dbm.upsert_user(db, message.from_user, None)
    await dbm.log_event(db, message.from_user.id, "auto_view")
    await message.answer(texts.AUTO_INFO, reply_markup=kb.auto_kb())


@router.callback_query(F.data == "auto_req")
async def auto_req(call: CallbackQuery, bot: Bot, db):
    u = await dbm.get_user(db, call.from_user.id)
    if not u or u["lead_status"] in (None, "lost"):
        await dbm.set_lead(db, call.from_user.id, "request", by="user")
        await notify.admins(bot, f"🤖 Заявка на автоматизацию: {notify.who(call.from_user)}\n"
                                 f"Источник: {u['source'] if u else '—'}", call.from_user.id)
    await call.message.answer(texts.AUTO_THANKS)
    await call.answer()


async def _attribute(db, user_id: int, code: str, text: str):
    u = await dbm.get_user(db, user_id)
    if u and (u["source"] in (None, "direct") or str(u["source"]).endswith("_bio")) and u["source"] != code:
        await dbm.set_source(db, user_id, code)
    if text:
        await dbm.log_event(db, user_id, "keyword", {"code": code, "text": text[:100]})


@router.message(F.text & ~F.text.startswith("/"))
async def feedback(message: Message, bot: Bot, db):
    """Кодовое слово из шортса → подарок. Любой другой текст — сообщение автору (сохраняем и пересылаем)."""
    await dbm.upsert_user(db, message.from_user, None)
    pack = await packs.by_keyword(db, message.text)
    if pack:                                                    # кодовое слово набора («напиши боту СКИЛЛЫ»)
        await _attribute(db, message.from_user.id, pack["code"], message.text)
        await dbm.log_event(db, message.from_user.id, "pack_open", {"code": pack["code"]})
        await give_pack(bot, message.chat.id, message.from_user, db, pack, via="keyword")
        return
    me = await dbm.get_user(db, message.from_user.id)
    code = await dbm.content_by_keyword(db, message.text, me["platform"] if me and me["source"] not in (None, "direct") else None)
    if code:
        await _attribute(db, message.from_user.id, code, message.text)
        await give_gift(bot, message.chat.id, message.from_user, db, via="keyword")
        return
    await dbm.add_feedback(db, message.from_user.id, message.text)
    u = await dbm.get_user(db, message.from_user.id)
    tag = f"{u['source']}" + (f" · заявка: {dbm.LEAD_NAME.get(u['lead_status'], '')}" if u and u["lead_status"] else "")
    await notify.admins(bot, f"💬 {notify.who(message.from_user)} [{tag}]:\n{message.text}", message.from_user.id)
    if not (u and u["lead_status"] in ("dialog", "call", "won")):   # в личной переписке автоответ не нужен
        await message.answer(texts.FEEDBACK_THANKS)
