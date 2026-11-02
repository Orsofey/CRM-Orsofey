import asyncio, logging
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from .config import cfg
from . import db as dbm, scheduler, runtime, web, workspaces, packs
from .handlers import funnel, payments, admin, tracking


async def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not cfg.token:
        raise SystemExit("Нет BOT_TOKEN. На Amvera: проект → «Переменные» → добавь BOT_TOKEN (секретом) и ADMIN_IDS, "
                         "затем перезапусти. Локально: скопируй .env.example в .env и заполни.")
    bot = Bot(cfg.token, default=DefaultBotProperties(parse_mode=None))
    db = await dbm.connect()
    await workspaces.init(db)                   # CRM друзей — отдельные базы рядом с основной
    await runtime.refresh(db)
    await packs.reload(db)                      # ссылки на наборы (?start=skills) → откуда ссылка
    try:
        runtime.state["bot_username"] = (await bot.get_me()).username or ""
    except Exception as e:
        logging.warning("get_me не удался: %s", e)
    dp = Dispatcher(db=db)                      # db попадает в хендлеры аргументом `db`
    # funnel последним: в нём «любой текст = сообщение автору»
    dp.include_routers(tracking.router, admin.router, payments.router, funnel.router)
    await web.start(db, bot)                    # панель CRM на порту PORT (по умолчанию 8080)
    asyncio.create_task(scheduler.run(bot, db))
    asyncio.create_task(scheduler.run_publish(bot, db))
    await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())


if __name__ == "__main__":
    asyncio.run(main())
