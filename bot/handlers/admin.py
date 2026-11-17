"""Команды автора: /panel — вход в CRM, /stats — воронка, /report — сводка сейчас, /broadcast — рассылка.
Ответ (reply) на уведомление бота о сообщении человека — уходит этому человеку."""
import asyncio, re
from aiogram import Router, Bot, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton as B
from .. import db as dbm, crm, notify, workspaces
from ..config import cfg
from ..web import issue_login_code, login_link

router = Router()
ID_RE = re.compile(r"id:(\d{3,})\s*$")


def is_admin(m: Message) -> bool:
    return bool(m.from_user and m.from_user.id in cfg.admins)


def _login_kb(ws: str):
    return InlineKeyboardMarkup(inline_keyboard=[[B(text="📊 Открыть CRM", url=login_link(ws))]]) if cfg.public_url else None


async def send_login(bot: Bot, chat_id: int, ws: str, intro: str = ""):
    code = issue_login_code(ws)
    text = (intro + "\n\n" if intro else "") + (
        f"🔐 Код входа в CRM: {code}\n(действует 10 минут, одноразовый)\n\n"
        "Нажми «Открыть CRM». На телефоне добавь её на экран «Домой» (меню браузера → «На экран Домой») — "
        "будет как приложение. Войти снова — команда /panel.")
    if not cfg.public_url:
        text += "\n\n⚠️ У бота не задан PUBLIC_URL — сообщи автору."
    await bot.send_message(chat_id, text, reply_markup=_login_kb(ws), disable_web_page_preview=True)


@router.message(Command("panel"))
async def panel(message: Message, bot: Bot):
    u = message.from_user
    ws = await workspaces.access_for(u.id)
    if ws:
        await send_login(bot, message.chat.id, ws)
        return
    if cfg.auto_workspaces:                       # автор разрешил всем свою CRM без подтверждения
        await workspaces.request(u)
        await workspaces.decide(u.id, "own")
        await send_login(bot, message.chat.id, f"u{u.id}", "✅ Твоя CRM готова.")
        return
    st = await workspaces.request(u)
    if st == "denied":
        await message.answer("Автор пока не дал доступ к CRM.")
        return
    await message.answer("⏳ Запрос отправлен автору бота. Как только он подтвердит — пришлю сюда ссылку для входа.")
    if st == "new":
        who = f"@{u.username}" if u.username else (u.first_name or str(u.id))
        for a in cfg.admins:
            try:
                await bot.send_message(a, f"🔑 {who} просит доступ к CRM.\n\n"
                                          f"«Своя CRM» — у него будет отдельная CRM в этом же боте: свои соцсети, аналитика и "
                                          f"публикация, твоих данных он не увидит.\n«Доступ к моей» — будет видеть твою CRM.",
                                       reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                           [B(text="✅ Своя CRM", callback_data=f"ws:own:{u.id}")],
                                           [B(text="👀 Доступ к моей", callback_data=f"ws:view:{u.id}"),
                                            B(text="✖️ Отказать", callback_data=f"ws:denied:{u.id}")]]))
            except Exception:
                pass


@router.callback_query(F.data.startswith("ws:"))
async def ws_decide(call: CallbackQuery, bot: Bot):
    if call.from_user.id not in cfg.admins:
        await call.answer("Только для автора", show_alert=True)
        return
    _, status, uid = call.data.split(":")
    uid = int(uid)
    await workspaces.decide(uid, status)
    label = {"own": "✅ Своя CRM выдана", "view": "👀 Доступ к твоей CRM выдан", "denied": "✖️ Отказано"}[status]
    try:
        await call.message.edit_text(call.message.text.split("\n")[0] + f"\n\n{label}. Изменить — CRM → Настройки → «Доступ».")
    except Exception:
        pass
    await call.answer(label)
    try:
        if status == "own":
            await send_login(bot, uid, f"u{uid}", "✅ Автор открыл тебе CRM. Это твоё личное пространство: подключи свои соцсети "
                                                  "в «Настройках» — дальше статистика будет приходить сама.")
        elif status == "view":
            await send_login(bot, uid, "main", "✅ Автор дал тебе доступ к своей CRM.")
        else:
            await bot.send_message(uid, "Автор не дал доступ к CRM.")
    except Exception:
        pass


@router.message(Command("stats"))
async def stats(message: Message, db):
    if not is_admin(message):
        return
    s = await dbm.stats(db)
    lines = [f"👥 Всего: {s['users']} · ✅ подписались: {s['subscribed']} · 🎁 подарок: {s['gift']}",
             f"📲 тест: {s['tested']} · ⬇️ скачать: {s['download']} · 💰 оплатили: {s['paid']} (подписка сейчас: {s['paid_now']})",
             f"Звёзд: {s['stars']} · ₽ по сделкам: {int(s['rub'])} · ушли: {s['blocked']}", "",
             "По платформам (пришли / подарок / тест / оплатили):"]
    for r in s["by_platform"]:
        lines.append(f"• {r['platform']}: {r['c']} / {r['g']} / {r['t']} / {r['p']}")
    if cfg.public_url:
        lines += ["", f"Подробно — в CRM: {cfg.public_url}"]
    await message.answer("\n".join(lines))


@router.message(Command("report"))
async def report(message: Message, db):
    if not is_admin(message):
        return
    await message.answer(await crm.daily_report(db), disable_web_page_preview=True)


@router.message(Command("broadcast"))
async def broadcast(message: Message, bot: Bot, db):
    """Ответь командой /broadcast на сообщение — оно уйдёт всем (копией)."""
    if not is_admin(message):
        return
    src = message.reply_to_message
    if not src:
        await message.answer("Ответь командой /broadcast на сообщение, которое нужно разослать.")
        return
    ok = fail = 0
    BATCH = 100
    semaphore = asyncio.Semaphore(20)  # не превышаем лимиты Telegram

    async def send_one(uid: int):
        nonlocal ok, fail
        async with semaphore:
            try:
                await bot.copy_message(uid, src.chat.id, src.message_id)
                ok += 1
            except Exception as e:
                fail += 1
                if notify.user_gone(e):
                    await dbm.set_blocked(db, uid, True)

    offset = 0
    while True:
        cur = await db.execute("SELECT id FROM users WHERE blocked=0 LIMIT ? OFFSET ?", (BATCH, offset))
        batch_ids = [r[0] async for r in cur]
        if not batch_ids:
            break
        await asyncio.gather(*[send_one(uid) for uid in batch_ids])
        offset += BATCH

    await message.answer(f"Готово: доставлено {ok}, не доставлено {fail}.")


def _reply_target(message: Message) -> int | None:
    r = message.reply_to_message
    if not (r and is_admin(message) and r.from_user and r.from_user.is_bot):
        return None
    m = ID_RE.search(r.text or r.caption or "")
    return int(m.group(1)) if m else None


@router.message(F.reply_to_message, lambda m: _reply_target(m) is not None)
async def reply_relay(message: Message, bot: Bot, db):
    """Автор отвечает на уведомление — бот пересылает ответ человеку и пишет переписку в CRM."""
    uid = _reply_target(message)
    try:
        await bot.copy_message(uid, message.chat.id, message.message_id)
    except Exception as e:
        await message.reply(f"Не доставлено: {e}")
        if notify.user_gone(e):
            await dbm.set_blocked(db, uid, True)
        return
    await dbm.log_event(db, uid, "admin_msg", {"text": (message.text or message.caption or "[вложение]")[:2000]})
    u = await dbm.get_user(db, uid)
    if u and u["lead_status"] in (None, "request"):
        await dbm.set_lead(db, uid, "dialog", by="reply")
    await message.reply("✅ Отправлено")
