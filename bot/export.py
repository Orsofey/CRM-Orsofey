"""Выгрузка статистики: воронка и все каналы — в Excel (несколько листов) или CSV (те же таблицы подряд, «;» для русского Excel)."""
import csv, datetime as dt, io, json
from . import db as dbm, crm, analytics, social

PERIOD_NAME = {"today": "Сегодня", "7": "7 дней", "30": "30 дней", "90": "90 дней", "all": "Всё время"}


def _t(ts) -> str:
    if not ts:
        return ""
    try:
        return dt.datetime.fromtimestamp(int(ts), crm.tz()).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return ""


def _now() -> str:
    return dt.datetime.now(crm.tz()).strftime("%Y-%m-%d %H:%M")


# ------------------------------------------------------------------ Воронка
async def funnel_tables(db, period: str = "30", platform: str = "all") -> list:
    f = await crm.funnel(db, period, platform)
    plat = "Все платформы" if platform in ("", "all", None) else platform
    summary = [["Выгружено", _now()], ["Период", PERIOD_NAME.get(period, period)], ["Платформа", plat],
               ["Пришли в бота", f["total"]], ["Оплатили", f["paid"]], ["Конверсия в оплату, %", f["conv"]],
               ["Охват контента (просмотры, всё время)", f["views"]],
               ["Перешли в бота с просмотров, %", f["views_to_bot"] if f["views_to_bot"] is not None else ""],
               ["Выручка, звёзды", f["stars"]], ["Выручка по сделкам, ₽", round(f["rub"] or 0)],
               ["«Застрял» — больше часов без шага", f["drop_hours"]]]
    stages = [[i + 1, s["name"], s["count"], s["pct_start"], s["pct_prev"] if i else "", s["stuck"], s["active"], s["left"],
               s["next_step"]] for i, s in enumerate(f["stages"])]
    plats = [[r["platform"], r["views"], r["users"], r["subscribed"], r["gift"], r["test"], r.get("download", 0), r["paid"], r["conv"]]
             for r in f["by_platform"]]
    # люди за период: путь по этапам, статус, деньги
    w, args = crm._where(crm.period_start(period), platform)
    drop_s = int(crm.cfg.drop_hours * crm.H)
    cur = await db.execute(
        f"SELECT u.*, {crm.STAGE_EXPR} AS stage, {crm.LAST_EXPR} AS last_progress, "
        f"(SELECT COALESCE(SUM(amount),0) FROM payments p WHERE p.user_id=u.id AND p.currency='XTR') AS stars, "
        f"(SELECT COALESCE(SUM(amount),0) FROM sales s WHERE s.user_id=u.id AND s.currency='RUB') AS rub "
        f"FROM users_v u WHERE {w} ORDER BY u.created_at DESC", args)
    people = []
    for r in await cur.fetchall():
        u = crm._user_view(dict(r), drop_s)
        people.append([u["id"], u.get("first_name") or "", u.get("username") or "", u.get("source") or "",
                       u.get("platform") or "", _t(u.get("created_at")), _t(u.get("subscribed_at")), _t(u.get("gift_at")),
                       _t(u.get("tested_at")), _t(u.get("download_at")), _t(u.get("offer_at")), _t(u.get("paid_at")),
                       _t(u.get("paid_until")), u["stage_name"], u["status_name"], u["next_step"], u.get("stars") or 0,
                       round(u.get("rub") or 0), u["lead_name"], _t(u.get("last_seen"))])
    leads = [[l["name"], l["count"]] for l in f["leads"]]
    return [
        ("Сводка", ["Показатель", "Значение"], summary),
        ("Этапы", ["№", "Этап", "Людей", "% от входа", "% от прошлого шага", "Застряли", "В процессе", "Ушли", "Следующий шаг"], stages),
        ("По платформам", ["Платформа", "Охват (просмотры)", "Пришли в бота", "Подписались", "Подарок", "Тест", "Скачивание", "Оплатили",
                           "Конверсия, %"], plats),
        ("Люди", ["ID", "Имя", "Username", "Метка", "Платформа", "Пришёл", "Подписался", "Подарок", "Тест", "Скачивание",
                  "Открыл подписку", "Оплатил", "Оплачено до", "Этап", "Статус", "Чего не сделал", "Звёзды", "Сделки, ₽",
                  "Личная работа", "Последняя активность"], people),
        ("Личная работа", ["Статус", "Людей"], leads),
    ]


# ------------------------------------------------------------------ Все каналы
async def channels_tables(db, days: str = "30", platform: str = "all") -> list:
    all_time = days == "all"
    a = await analytics.analytics(db, 30 if all_time else max(1, min(365, int(days or 30))), platform, all_time)
    k = a["kpi"]
    plat = "Все соцсети" if platform in ("", "all", None) else platform
    summary = [["Выгружено", _now()], ["Период", "Всё время" if all_time else f"{a['days']} дней"], ["Соцсети", plat],
               ["Просмотры за всё время" if all_time else "Новые просмотры за период", k["views"]],
               ["Всего просмотров у роликов", k["total_views"]], ["Подписчики", k["followers"] or ""],
               ["Подписчики: изменение за период", k["followers_change"] or 0], ["Обычный ролик набирает (медиана)", k["typical"]],
               ["Вовлечённость, %", k["er"] if k["er"] is not None else ""], ["Публикаций за период", k["posts"]],
               ["Пришли в бота", k["people"]], ["Оплатили", k["paid"]],
               ["Просмотров на 1 человека", k["per_person"] or ""]]
    if a.get("patreon"):
        p = a["patreon"]
        summary += [["Patreon: платных подписчиков", p.get("paid", 0)], ["Patreon: доход в месяц", round(p.get("mrr") or 0)]]
    nets = [[r["platform"], r["followers"] if r["followers"] is not None else "", r["followers_change"] or 0,
             "" if r["no_views"] else r["views"], "" if r["no_views"] else r["total_views"], r["typical"] or "",
             r["er"] if r["er"] is not None else "", r["posts"], r["likes"], r["people"], r["per_person"] or "", r["paid"]]
            for r in a["platforms"] if not platform or platform == "all" or r["platform"] == platform]
    accs = []
    for acc in await social.accounts(db):
        if acc["hidden"]:
            continue
        st = acc["status"] or {}
        name = social.KIND_NAME.get(acc["kind"], acc["kind"])
        if platform and platform != "all" and dbm.ACC_PLATFORM.get(acc["kind"]) not in (platform, None):
            continue
        accs.append([name, acc["name"], acc["ref"] or "", st.get("subscribers", st.get("followers", "")) or "",
                     st.get("videos", "") if st.get("ok") else "", "да" if st.get("ok") else ("ошибка: " + str(st.get("error") or "") if st else "ещё не загружено"),
                     _t(st.get("at"))])
    acc_name = {a2["id"]: a2["name"] for a2 in await social.accounts(db)}
    vids = []
    for c in await crm.content_list(db):
        if platform and platform != "all" and c.get("platform") != platform:
            continue
        vids.append([c.get("platform") or "", acc_name.get(c.get("account"), ""), c.get("kind") or "", c.get("title") or "",
                     c.get("url") or "", _t(c.get("published_at") or c.get("created_at")), c.get("views") or 0, c.get("likes") or 0,
                     c.get("comments") or 0, c.get("code") or "", c.get("people") or 0, c.get("subscribed") or 0, c.get("gift") or 0,
                     c.get("paid") or 0, c.get("conv") or 0, c.get("stars") or 0, round(c.get("rub") or 0)])
    days_rows = []
    series = a["daily"]["series"]
    for i, d in enumerate(a["daily"]["days"]):
        vals = [s["values"][i] for s in series]
        days_rows.append([d] + vals + [sum(vals)])
    pw, pargs = ("AND platform = ?", [platform]) if platform and platform != "all" else ("", [])
    cur = await db.execute(f"SELECT platform, account, at, followers FROM channel_stats_v WHERE followers IS NOT NULL {pw} "
                           f"ORDER BY at", pargs)
    hist = [[_t(r["at"]), r["platform"], acc_name.get(r["account"], ""), r["followers"]] for r in await cur.fetchall()]
    return [
        ("Сводка", ["Показатель", "Значение"], summary),
        ("Соцсети", ["Соцсеть", "Подписчики", "Подписчики: изменение", "Просмотры за период", "Всего просмотров", "Обычный ролик",
                     "Вовлечённость, %", "Публикаций", "Лайки", "Пришли в бота", "Просмотров на 1 чел.", "Оплатили"], nets),
        ("Аккаунты", ["Соцсеть", "Аккаунт", "Ссылка", "Подписчики", "Публикаций", "Загрузка", "Обновлено"], accs),
        ("Публикации", ["Соцсеть", "Аккаунт", "Тип", "Название", "Ссылка", "Опубликовано", "Просмотры", "Лайки", "Комментарии",
                        "Метка", "Пришли в бота", "Подписались", "Подарок", "Оплатили", "Конверсия, %", "Звёзды", "Сделки, ₽"], vids),
        ("Просмотры по дням", ["Дата"] + [s["platform"] for s in series] + ["Всего"], days_rows),
        ("Подписчики по дням", ["Когда", "Соцсеть", "Аккаунт", "Подписчики"], hist),
    ]


# ------------------------------------------------------------------ Форматы
def to_csv(tables: list) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    for i, (title, head, rows) in enumerate(tables):
        if i:
            w.writerow([])
        w.writerow([f"== {title} =="])
        w.writerow(head)
        w.writerows([[("'" + v if isinstance(v, str) and v[:1] in "=+-@" else v) for v in r] for r in rows])
    return ("﻿" + buf.getvalue()).encode("utf-8")       # BOM — чтобы Excel открыл кириллицу правильно


def to_xlsx(tables: list) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter
    wb = Workbook()
    wb.remove(wb.active)
    head_font, head_fill = Font(bold=True, color="FFFFFF"), PatternFill("solid", fgColor="2E7D74")
    for title, head, rows in tables:
        ws = wb.create_sheet(title[:31])
        ws.append(head)
        for c in ws[1]:
            c.font, c.fill = head_font, head_fill
            c.alignment = Alignment(vertical="center", wrap_text=True)
        for r in rows:
            ws.append(list(r))
            for c in ws[ws.max_row]:
                if isinstance(c.value, str) and c.value.startswith("="):
                    c.data_type = "s"           # текст, а не формула
        ws.freeze_panes = "A2"
        if rows:
            ws.auto_filter.ref = ws.dimensions
        for i, h in enumerate(head, 1):
            width = max([len(str(h))] + [len(str(r[i - 1])) for r in rows[:300] if i - 1 < len(r)])
            ws.column_dimensions[get_column_letter(i)].width = min(60, max(10, width + 2))
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


async def build(db, what: str, fmt: str, period: str = "30", platform: str = "all") -> tuple[bytes, str, str]:
    if what == "funnel":
        tables, base = await funnel_tables(db, period, platform), "voronka"
    else:
        tables, base = await channels_tables(db, period, platform), "statistika-kanalov"
    stamp = dt.datetime.now(crm.tz()).strftime("%Y-%m-%d")
    if fmt == "csv":
        return to_csv(tables), f"{base}-{stamp}.csv", "text/csv; charset=utf-8"
    return to_xlsx(tables), f"{base}-{stamp}.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
