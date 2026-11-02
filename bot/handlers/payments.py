"""Подписка в Telegram Stars: createInvoiceLink с subscription_period = 30 дней (только XTR)."""
import datetime as dt
from aiogram import Router, F, Bot
from aiogram.types import Message, PreCheckoutQuery, LabeledPrice
from .. import texts, keyboards as kb, db as dbm, notify
from ..config import cfg, SUBSCRIPTION_PERIOD

router = Router()


def fmt(ts: int) -> str:
    return dt.datetime.fromtimestamp(ts).strftime("%d.%m.%Y")


async def send_sub_offer(bot: Bot, chat_id: int, user, db):
    await dbm.upsert_user(db, user, None)
    u = await dbm.get_user(db, user.id)
    if u and u["paid_until"] and u["paid_until"] > dbm.now():
        await bot.send_message(chat_id, texts.ALREADY_PAID.format(until=fmt(u["paid_until"])))
        return
    await dbm.stage(db, user.id, "offer")
    link = await bot.create_invoice_link(
        title=cfg.sub_title[:32],
        description="Полная версия ассистента Orsofey и все обновления. Продление раз в 30 дней.",
        payload=f"sub:{user.id}",
        currency="XTR",
        prices=[LabeledPrice(label="Подписка на 30 дней", amount=cfg.price_stars)],
        subscription_period=SUBSCRIPTION_PERIOD,
    )
    await bot.send_message(chat_id, texts.SUB_OFFER, reply_markup=kb.pay_kb(link))


@router.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    await q.answer(ok=True)


@router.message(F.successful_payment)
async def paid(message: Message, db, bot: Bot):
    p = message.successful_payment
    until = p.subscription_expiration_date or (dbm.now() + SUBSCRIPTION_PERIOD)
    await dbm.upsert_user(db, message.from_user, None)
    await dbm.add_payment(db, message.from_user.id, p.total_amount, p.currency,
                          p.telegram_payment_charge_id, p.is_recurring, until)
    await dbm.mark(db, message.from_user.id, "paid_until", until)
    await message.answer(texts.PAID.format(until=fmt(until)))
    u = await dbm.get_user(db, message.from_user.id)
    await notify.admins(bot, f"⭐ Оплата: {notify.who(message.from_user)} — {p.total_amount} {p.currency}"
                             f"{' (продление)' if p.is_recurring and not getattr(p, 'is_first_recurring', True) else ''}\n"
                             f"Источник: {u['source'] if u else '—'}")
