from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton as B
from .config import cfg
from .runtime import channel_url, download_link


def gift_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[B(text="🎁 Забрать подарок", callback_data="gift")]])


def sub_check_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [B(text="📲 Подписаться на канал", url=channel_url())],
        [B(text="✅ Я подписался", callback_data="gift")],
    ])


def pack_kb(pack: dict):
    return InlineKeyboardMarkup(inline_keyboard=[[B(text=pack["button"] or "📥 Забрать", callback_data="pk:" + pack["code"])]])


def pack_sub_kb(pack: dict):
    return InlineKeyboardMarkup(inline_keyboard=[
        [B(text="📲 Подписаться на канал", url=channel_url())],
        [B(text="✅ Я подписался", callback_data="pk:" + pack["code"])],
    ])


def test_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[B(text="📲 Протестировать приложение", callback_data="test")]])


def app_kb(user_id: int | None = None):
    link = download_link(user_id) if user_id else cfg.app_url
    rows = [[B(text="⬇️ Скачать тестовую версию", url=link)]] if link else []
    rows.append([B(text="⭐ Полная версия по подписке", callback_data="sub")])
    rows.append([B(text="🤖 Автоматизация под бизнес", callback_data="auto")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def pay_kb(invoice_link: str):
    rows = [[B(text=f"⭐ Оформить за {cfg.price_stars} звёзд / мес", url=invoice_link)]]
    if cfg.external_pay_url:
        rows.append([B(text="💳 Оплатить картой", url=cfg.external_pay_url)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def auto_kb():
    return InlineKeyboardMarkup(inline_keyboard=[[B(text="✍️ Оставить заявку", callback_data="auto_req")]])


def menu_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [B(text="🎁 Подарок", callback_data="gift")],
        [B(text="📲 Тестовая версия", callback_data="test")],
        [B(text="⭐ Подписка", callback_data="sub")],
        [B(text="🤖 Автоматизация под бизнес", callback_data="auto")],
        [B(text="📣 Канал Orsofey", url=channel_url())],
    ])
