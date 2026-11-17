"""Фоновые задачи: напоминания застрявшим (раз в 5 минут), ежедневная сводка автору, автоподгрузка соцсетей (раз в 3 ч)."""
import asyncio, datetime as dt, logging, re
from aiogram import Bot
from . import texts, keyboards as kb, db as dbm, crm, notify, social, publish, analytics, workspaces, packs
from .config import cfg

log = logging.getLogger(__name__)
H = 3600
# Тем, с кем ты уже лично общаешься, автонапоминания не шлём
NOT_IN_DIALOG = "(lead_status IS NULL OR lead_status IN ('request','lost'))"
FRESH_DAYS = 14   # не будим тех, кто пришёл больше двух недель назад (например, сразу после обновления бота)


def reminder_steps(now: int, settings: dict) -> list:
    drop = int(cfg.drop_hours * H)
    dl = crm.track_download()
    steps = [
        # (настройка, флаг в users, тип, условие SQL, текст, клавиатура(uid))
        ("rem_start", "r_start", "start",
         f"gift_at IS NULL AND created_at < {now - drop}", texts.REMIND_START, lambda uid: kb.gift_kb()),
        ("rem_test", "f_test", "test",
         f"gift_at IS NOT NULL AND tested_at IS NULL AND gift_at < {now - int(cfg.followup_test_h * H)}",
         texts.FOLLOWUP_TEST, lambda uid: kb.test_kb()),
    ]
    if dl:
        steps.append(("rem_download", "r_download", "download",
                      f"tested_at IS NOT NULL AND download_at IS NULL AND paid_at IS NULL AND tested_at < {now - drop}",
                      texts.REMIND_DOWNLOAD, kb.app_kb))
    tried = "download_at IS NOT NULL" if dl else "1=1"   # спрашиваем мнение только у тех, кто реально скачал
    steps += [
        ("rem_feedback", "f_feedback", "feedback",
         f"tested_at IS NOT NULL AND {tried} AND paid_at IS NULL AND tested_at < {now - int(cfg.followup_feedback_h * H)}",
         texts.FOLLOWUP_FEEDBACK, kb.app_kb),
        ("rem_final", "f_final", "final",
         f"tested_at IS NOT NULL AND paid_at IS NULL AND tested_at < {now - int(cfg.followup_final_h * H)}"
         + (" AND f_feedback=1" if settings.get("rem_feedback", "1") == "1" else ""),
         texts.FOLLOWUP_FINAL, kb.app_kb),
    ]
    return [s for s in steps if settings.get(s[0], "1") == "1"]


async def tick(bot: Bot, db):
    now = dbm.now()
    settings = await dbm.get_settings(db)
    done = set()   # не больше одного напоминания человеку за проход
    for _, flag, kind, cond, text, keyboard in reminder_steps(now, settings):
        cur = await db.execute(f"SELECT id FROM users WHERE {flag}=0 AND blocked=0 AND {NOT_IN_DIALOG} "
                               f"AND created_at > {now - FRESH_DAYS * 86400} AND {cond} "
                               f"AND NOT EXISTS(SELECT 1 FROM events e WHERE e.user_id=users.id AND e.type='reminder' "
                               f"AND e.created_at > {now - 20 * H}) LIMIT 200")   # не чаще раза в сутки
        for (uid,) in await cur.fetchall():
            if uid in done:
                continue
            done.add(uid)
            msg, markup = text, keyboard(uid)
            if kind == "start":            # пришёл по ссылке набора — напоминаем про набор, а не про приложение
                code = await packs.pending_for(db, uid)
                p = await packs.find_active(db, code) if code else None
                if p:
                    msg, markup = texts.REMIND_PACK, kb.pack_kb(p)
            try:
                await bot.send_message(uid, msg, reply_markup=markup)
                await dbm.log_event(db, uid, "reminder", {"kind": kind})
            except Exception as e:
                log.info("Не доставлено %s: %s", uid, e)
                if notify.user_gone(e):
                    await dbm.set_blocked(db, uid, True)
            await dbm.mark(db, uid, flag, 1)
            await asyncio.sleep(0.05)


async def maybe_report(bot: Bot, db):
    """Раз в день в REPORT_HOUR по местному времени — сводка за вчера."""
    if await dbm.get_setting(db, "report_on", "1") != "1" or not cfg.admins:
        return
    local = dt.datetime.now(crm.tz())
    today = local.date().isoformat()
    if local.hour < cfg.report_hour or await dbm.get_setting(db, "last_report", "") == today:
        return
    await dbm.set_setting(db, "last_report", today)
    await notify.admins(bot, await crm.daily_report(db))


async def run_publish(bot: Bot, db):
    """Публикации проверяем чаще — раз в 30 секунд (расписание, обработка видео в Instagram)."""
    while True:
        for ws in await workspaces.active():
            try:
                await publish.tick(await workspaces.get_db(ws), bot)
            except Exception as e:
                log.exception("Ошибка публикации (%s): %s", ws, e)
        await asyncio.sleep(30)


async def maybe_compact(db):
    today = dt.date.today().isoformat()
    if await dbm.get_setting(db, "compact_day", "") != today:
        await dbm.set_setting(db, "compact_day", today)
        await analytics.compact(db)


async def run(bot: Bot, db):
    while True:
        for job in (lambda: tick(bot, db), lambda: maybe_report(bot, db)):
            try:
                await job()
            except Exception as e:
                log.exception("Ошибка фоновой задачи: %s", e)
        for ws in await workspaces.active():          # соцсети и история — в каждой CRM (автора и друзей)
            for job in (social.sync_all, maybe_compact):
                try:
                    await job(await workspaces.get_db(ws))
                except Exception as e:
                    log.exception("Ошибка фоновой задачи (%s): %s", ws, e)
        await asyncio.sleep(300)
