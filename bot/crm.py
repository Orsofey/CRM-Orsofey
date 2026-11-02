"""Аналитика CRM: воронка, контент, люди, сделки, сводка. Только чтение базы — используется панелью и ботом."""
import datetime as dt
import json
from zoneinfo import ZoneInfo
from .config import cfg
from .db import (STAGES, STAGE_NAME, NEXT_STEP, LEAD_STATUSES, LEAD_NAME, PRODUCTS, PLATFORMS, now, parse_code)

H = 3600
FUNNEL_EVENTS = ("subscribed", "gift", "test", "download", "offer", "paid", "sale", "user_msg")

STAGE_EXPR = ("CASE WHEN paid_at IS NOT NULL THEN 'paid' WHEN offer_at IS NOT NULL THEN 'offer' "
              "WHEN download_at IS NOT NULL THEN 'download' WHEN tested_at IS NOT NULL THEN 'test' "
              "WHEN gift_at IS NOT NULL THEN 'gift' WHEN subscribed_at IS NOT NULL THEN 'subscribed' ELSE 'start' END")
LAST_EXPR = ("MAX(created_at, COALESCE(subscribed_at,0), COALESCE(gift_at,0), COALESCE(tested_at,0), "
             "COALESCE(download_at,0), COALESCE(offer_at,0), COALESCE(paid_at,0))")


def tz():
    try:
        return ZoneInfo(cfg.tz)
    except Exception:
        return ZoneInfo("Europe/Moscow")


def track_download() -> bool:
    """Переход к скачиванию виден, только если у бота есть публичный адрес (ссылка идёт через него)."""
    return bool(cfg.public_url and cfg.app_url)


def stages() -> list:
    return [s for s in STAGES if s[0] != "download" or track_download()]


def period_start(period: str) -> int:
    if period == "today":
        d = dt.datetime.now(tz()).replace(hour=0, minute=0, second=0, microsecond=0)
        return int(d.timestamp())
    if period in ("7", "30", "90"):
        return now() - int(period) * 86400
    return 0


def _where(start: int, platform: str | None, alias: str = "") -> tuple[str, list]:
    a = f"{alias}." if alias else ""
    sql, args = [f"{a}created_at >= ?"], [start]
    if platform and platform != "all":
        sql.append(f"{a}platform = ?")
        args.append(platform)
    return " AND ".join(sql), args


def status_of(row: dict, drop_s: int) -> str:
    if row.get("paid_at"):
        return "paid"
    if row.get("blocked"):
        return "left"
    return "stuck" if row["last_progress"] < now() - drop_s else "active"


STATUS_NAME = {"paid": "Оплатил", "left": "Ушёл (заблокировал бота)", "stuck": "Застрял", "active": "В процессе"}


async def _one(db, sql, args=()):
    cur = await db.execute(sql, args)
    r = await cur.fetchone()
    return (r[0] if r else 0) or 0


# ---------------------------------------------------------------- Воронка
async def funnel(db, period: str = "30", platform: str | None = None) -> dict:
    drop_s = int(cfg.drop_hours * H)
    start = period_start(period)
    w, args = _where(start, platform)
    st = stages()
    cols = ", ".join(f"SUM({c} IS NOT NULL) AS {k}" for k, _, c in st)
    cur = await db.execute(f"SELECT {cols} FROM users_v WHERE {w}", args)
    counts = dict(await cur.fetchone())
    # где люди сейчас: этап × статус
    cur = await db.execute(
        f"SELECT {STAGE_EXPR} AS stage, SUM(blocked=1) AS left_, "
        f"SUM(blocked=0 AND {LAST_EXPR} < ?) AS stuck, SUM(blocked=0 AND {LAST_EXPR} >= ?) AS active "
        f"FROM users_v WHERE {w} GROUP BY stage", [now() - drop_s, now() - drop_s] + args)
    where_now = {r["stage"]: dict(r) for r in await cur.fetchall()}
    total = counts.get("start") or 0
    out_stages, prev = [], None
    for k, name, _ in st:
        c = counts.get(k) or 0
        wn = where_now.get(k, {})
        out_stages.append({
            "key": k, "name": name, "count": c,
            "pct_prev": round(c * 100 / prev, 1) if prev else (100.0 if k == "start" and c else 0),
            "pct_start": round(c * 100 / total, 1) if total else 0,
            "stuck": 0 if k == "paid" else (wn.get("stuck") or 0),
            "active": 0 if k == "paid" else (wn.get("active") or 0),
            "left": wn.get("left_") or 0,
            "next_step": NEXT_STEP.get(k, ""),
        })
        prev = c
    # охват (просмотры контента за всё время) и деньги за период
    pw, pargs = ("WHERE platform = ?", [platform]) if platform and platform != "all" else ("", [])
    views = await _one(db, f"SELECT COALESCE(SUM(views),0) FROM content_v {pw}", pargs)
    from_viewed = await _one(db, "SELECT COUNT(*) FROM users_v u JOIN content_v c ON c.code=u.source WHERE c.views > 0"
                             + (" AND u.platform = ?" if pargs else ""), pargs)
    uw, uargs = _where(0, platform, "u")
    stars = await _one(db, f"SELECT COALESCE(SUM(p.amount),0) FROM payments p JOIN users_v u ON u.id=p.user_id "
                           f"WHERE p.currency='XTR' AND p.created_at >= ? AND {uw}", [start] + uargs)
    rub = await _one(db, f"SELECT COALESCE(SUM(s.amount),0) FROM sales s JOIN users_v u ON u.id=s.user_id "
                         f"WHERE s.currency='RUB' AND s.created_at >= ? AND {uw}", [start] + uargs)
    paid = counts.get("paid") or 0
    return {
        "period": period, "platform": platform or "all", "drop_hours": cfg.drop_hours,
        "track_download": track_download(),
        "stages": out_stages, "total": total, "paid": paid,
        "conv": round(paid * 100 / total, 1) if total else 0,
        "views": views, "views_to_bot": round(from_viewed * 100 / views, 2) if views else None,
        "stars": stars, "rub": rub,
        "by_platform": await by_platform(db, start),
        "leads": await lead_counts(db, start, platform),
        "biggest_drop": _biggest_drop(out_stages),
    }


def _biggest_drop(st: list) -> dict | None:
    best = None
    for s in st:
        if s["key"] != "paid" and s["stuck"] and (best is None or s["stuck"] > best["stuck"]):
            best = s
    return {"key": best["key"], "name": best["name"], "stuck": best["stuck"], "next_step": best["next_step"]} if best else None


async def by_platform(db, start: int) -> list:
    cur = await db.execute(
        "SELECT platform, COUNT(*) AS users, SUM(subscribed_at IS NOT NULL) AS subscribed, SUM(gift_at IS NOT NULL) AS gift, "
        "SUM(tested_at IS NOT NULL) AS test, SUM(download_at IS NOT NULL) AS download, SUM(paid_at IS NOT NULL) AS paid "
        "FROM users_v WHERE created_at >= ? GROUP BY platform", [start])
    rows = {r["platform"] or "Без метки": dict(r) for r in await cur.fetchall()}
    cur = await db.execute("SELECT platform, COALESCE(SUM(views),0) FROM content_v GROUP BY platform")
    views = {p: v for p, v in await cur.fetchall()}
    from .db import connected_platforms
    conn = set(await connected_platforms(db))
    out = []
    for p in PLATFORMS:
        r = rows.get(p, {"users": 0, "subscribed": 0, "gift": 0, "test": 0, "download": 0, "paid": 0})
        if p not in conn and not (p in ("Другое", "Без метки") and r["users"]):
            continue                      # показываем только подключённые соцсети
        r = dict(r, platform=p, views=views.get(p, 0) or 0)
        r["conv"] = round(r["paid"] * 100 / r["users"], 1) if r["users"] else 0
        out.append(r)
    return out


async def lead_counts(db, start: int = 0, platform: str | None = None) -> list:
    w, args = _where(0, platform)
    cur = await db.execute(f"SELECT lead_status, COUNT(*) FROM users_v WHERE lead_status IS NOT NULL AND lead_at >= ? "
                           f"AND {w} GROUP BY lead_status", [start] + args)
    got = {k: v for k, v in await cur.fetchall()}
    return [{"key": k, "name": n, "count": got.get(k, 0)} for k, n in LEAD_STATUSES]


# ---------------------------------------------------------------- Контент
async def content_list(db) -> list:
    cur = await db.execute(
        "SELECT c.*, COUNT(u.id) AS people, SUM(u.subscribed_at IS NOT NULL) AS subscribed, SUM(u.gift_at IS NOT NULL) AS gift, "
        "SUM(u.tested_at IS NOT NULL) AS test, SUM(u.download_at IS NOT NULL) AS download, "
        "SUM(u.paid_at IS NOT NULL) AS paid, MAX(u.created_at) AS last_user "
        "FROM content_v c LEFT JOIN users u ON u.source = c.code GROUP BY c.code")
    rows = [dict(r) for r in await cur.fetchall()]
    cur = await db.execute("SELECT u.source, COALESCE(SUM(p.amount),0) FROM payments p JOIN users u ON u.id=p.user_id "
                           "WHERE p.currency='XTR' GROUP BY u.source")
    stars = {k: v for k, v in await cur.fetchall()}
    cur = await db.execute("SELECT u.source, COALESCE(SUM(s.amount),0) FROM sales s JOIN users u ON u.id=s.user_id "
                           "WHERE s.currency='RUB' GROUP BY u.source")
    rub = {k: v for k, v in await cur.fetchall()}
    for r in rows:
        for k in ("people", "subscribed", "gift", "test", "download", "paid"):
            r[k] = r[k] or 0
        r["views"] = r["views"] or 0
        r["to_bot"] = round(r["people"] * 100 / r["views"], 2) if r["views"] else None
        r["conv"] = round(r["paid"] * 100 / r["people"], 1) if r["people"] else 0
        r["stars"] = stars.get(r["code"], 0)
        r["rub"] = rub.get(r["code"], 0)
        ext = (r.get("ext_id") or "").split(":", 1)[-1].split("_")[-1]
        r["no_link"] = bool(r.get("synced") and ext and r["code"].endswith(ext))
    rows.sort(key=lambda r: (r["people"], r["views"], r["created_at"] or 0), reverse=True)
    return rows


# ---------------------------------------------------------------- Люди
def _user_view(r: dict, drop_s: int) -> dict:
    r = dict(r)
    r["stage_name"] = STAGE_NAME.get(r["stage"], r["stage"])
    r["status"] = status_of(r, drop_s)
    r["status_name"] = STATUS_NAME[r["status"]]
    r["next_step"] = "" if r["status"] in ("paid",) else NEXT_STEP.get(r["stage"], "")
    r["lead_name"] = LEAD_NAME.get(r.get("lead_status") or "", "")
    r["name"] = r.get("first_name") or (("@" + r["username"]) if r.get("username") else str(r["id"]))
    return r


async def users_list(db, status: str = "", stage: str = "", platform: str = "", q: str = "", source: str = "",
                     period: str = "all", limit: int = 50, offset: int = 0) -> dict:
    drop_s = int(cfg.drop_hours * H)
    t_drop = now() - drop_s
    where, args = ["created_at >= ?"], [period_start(period)]
    if platform and platform != "all":
        where.append("platform = ?"); args.append(platform)
    if source:
        where.append("source = ?"); args.append(source)
    if stage:
        where.append(f"({STAGE_EXPR}) = ?"); args.append(stage)
    if status == "paid":
        where.append("paid_at IS NOT NULL")
    elif status == "left":
        where.append("paid_at IS NULL AND blocked = 1")
    elif status == "stuck":
        where.append(f"paid_at IS NULL AND blocked = 0 AND {LAST_EXPR} < ?"); args.append(t_drop)
    elif status == "active":
        where.append(f"paid_at IS NULL AND blocked = 0 AND {LAST_EXPR} >= ?"); args.append(t_drop)
    elif status == "lead":
        where.append("lead_status IS NOT NULL")
    if q:
        q = q.strip().lstrip("@")
        where.append("(username LIKE ? OR first_name LIKE ? OR CAST(id AS TEXT) = ? OR source LIKE ?)")
        args += [f"%{q}%", f"%{q}%", q, f"%{q}%"]
    w = " AND ".join(where)
    total = await _one(db, f"SELECT COUNT(*) FROM users WHERE {w}", args)
    cur = await db.execute(
        f"SELECT id, username, first_name, source, platform, created_at, last_seen, blocked, paid_at, paid_until, "
        f"lead_status, {STAGE_EXPR} AS stage, {LAST_EXPR} AS last_progress FROM users WHERE {w} "
        f"ORDER BY COALESCE(last_seen, created_at) DESC LIMIT ? OFFSET ?", args + [min(limit, 200), offset])
    return {"total": total, "items": [_user_view(dict(r), drop_s) for r in await cur.fetchall()]}


async def user_card(db, user_id: int) -> dict | None:
    drop_s = int(cfg.drop_hours * H)
    cur = await db.execute(f"SELECT *, {STAGE_EXPR} AS stage, {LAST_EXPR} AS last_progress FROM users WHERE id=?", [user_id])
    r = await cur.fetchone()
    if not r:
        return None
    u = _user_view(dict(r), drop_s)
    u["platform"], u["kind"] = parse_code(u["source"])
    cur = await db.execute("SELECT title FROM content WHERE code=?", [u["source"]])
    t = await cur.fetchone()
    u["source_title"] = t[0] if t else ""
    u["stages"] = [{"key": k, "name": n, "at": u.get(c)} for k, n, c in stages()]
    cur = await db.execute("SELECT type, meta, created_at FROM events WHERE user_id=? ORDER BY id DESC LIMIT 300", [user_id])
    u["events"] = [{"type": e["type"], "meta": json.loads(e["meta"]) if e["meta"] else {}, "at": e["created_at"]}
                   for e in await cur.fetchall()]
    cur = await db.execute("SELECT id, text, created_at FROM notes WHERE user_id=? ORDER BY id DESC", [user_id])
    u["notes"] = [dict(x) for x in await cur.fetchall()]
    cur = await db.execute("SELECT amount, currency, recurring, expires_at, created_at FROM payments WHERE user_id=? ORDER BY id DESC", [user_id])
    u["payments"] = [dict(x) for x in await cur.fetchall()]
    cur = await db.execute("SELECT id, product, amount, currency, note, created_at FROM sales WHERE user_id=? ORDER BY id DESC", [user_id])
    u["sales"] = [dict(x, product_name=PRODUCTS.get(x["product"], x["product"])) for x in await cur.fetchall()]
    return u


# ---------------------------------------------------------------- Сделки (личная работа)
async def deals(db) -> dict:
    cur = await db.execute(
        "SELECT u.id, u.username, u.first_name, u.source, u.platform, u.lead_status, u.lead_at, u.last_seen, "
        "(SELECT meta FROM events e WHERE e.user_id=u.id AND e.type IN ('user_msg','admin_msg') ORDER BY e.id DESC LIMIT 1) AS last_msg, "
        "(SELECT type FROM events e WHERE e.user_id=u.id AND e.type IN ('user_msg','admin_msg') ORDER BY e.id DESC LIMIT 1) AS last_msg_type, "
        "(SELECT COALESCE(SUM(amount),0) FROM sales s WHERE s.user_id=u.id AND s.currency='RUB') AS rub "
        "FROM users u WHERE u.lead_status IS NOT NULL ORDER BY u.lead_at DESC")
    cols = {k: {"key": k, "name": n, "items": []} for k, n in LEAD_STATUSES}
    for r in await cur.fetchall():
        d = dict(r)
        d["name"] = d.get("first_name") or (("@" + d["username"]) if d.get("username") else str(d["id"]))
        d["last_msg"] = (json.loads(d["last_msg"]).get("text", "") if d["last_msg"] else "")[:160]
        d["waiting"] = d["last_msg_type"] == "user_msg"   # последним написал человек — ждёт ответа
        cols.get(d["lead_status"], cols["request"])["items"].append(d)
    won = await _one(db, "SELECT COALESCE(SUM(amount),0) FROM sales WHERE currency='RUB'")
    return {"columns": list(cols.values()), "rub_total": won}


# ---------------------------------------------------------------- Напоминания
REMINDERS = [
    ("rem_start", "start", "Зашёл, но не забрал подарок", "через {h} ч"),
    ("rem_test", "test", "Забрал подарок, но не нажал «Протестировать»", "через {h} ч"),
    ("rem_download", "download", "Нажал «Протестировать», но не перешёл к скачиванию", "через {h} ч"),
    ("rem_feedback", "feedback", "Тестирует, но не оплатил — спросить мнение", "через {fb} ч после теста"),
    ("rem_final", "final", "Последнее напоминание о подписке", "через {fin} ч после теста"),
]


async def reminder_stats(db, settings: dict) -> list:
    out = []
    progress = "','".join(("subscribed", "gift", "test", "download", "offer", "paid", "sale"))
    for key, kind, name, when in REMINDERS:
        sent = await _one(db, "SELECT COUNT(*) FROM events WHERE type='reminder' AND json_extract(meta,'$.kind')=?", [kind])
        back = await _one(db, "SELECT COUNT(*) FROM events e WHERE e.type='reminder' AND json_extract(e.meta,'$.kind')=? "
                              f"AND EXISTS(SELECT 1 FROM events x WHERE x.user_id=e.user_id AND x.created_at>=e.created_at "
                              f"AND x.id>e.id AND x.type IN ('{progress}'))", [kind])
        out.append({"key": key, "kind": kind, "name": name,
                    "when": when.format(h=int(cfg.drop_hours), fb=int(cfg.followup_feedback_h), fin=int(cfg.followup_final_h)),
                    "on": settings.get(key, "1") == "1", "sent": sent, "back": back,
                    "available": kind != "download" or track_download()})
    return out


# ---------------------------------------------------------------- Ежедневная сводка
def _fmt_money(stars: float, rub: float) -> str:
    parts = []
    if stars:
        parts.append(f"{int(stars)} ⭐")
    if rub:
        parts.append(f"{int(rub):,} ₽".replace(",", " "))
    return " + ".join(parts) or "—"


async def daily_report(db, day: dt.date | None = None) -> str:
    """Сводка за прошедшие сутки (по местному времени)."""
    z = tz()
    if day is None:
        day = (dt.datetime.now(z) - dt.timedelta(days=1)).date()
    a = int(dt.datetime.combine(day, dt.time.min, z).timestamp())
    b = a + 86400
    drop_s = int(cfg.drop_hours * H)
    new = await _one(db, "SELECT COUNT(*) FROM users_v WHERE created_at >= ? AND created_at < ?", [a, b])
    cur = await db.execute("SELECT platform, COUNT(*) c FROM users_v WHERE created_at >= ? AND created_at < ? "
                           "GROUP BY platform ORDER BY c DESC", [a, b])
    plat = " · ".join(f"{p} {c}" for p, c in await cur.fetchall())
    # сколько дошли до каждого этапа за сутки (по событиям)
    cur = await db.execute("SELECT type, COUNT(DISTINCT user_id) FROM events WHERE created_at >= ? AND created_at < ? "
                           "GROUP BY type", [a, b])
    ev = {k: v for k, v in await cur.fetchall()}
    steps = [("subscribed", "подписались"), ("gift", "лид-магнит"), ("test", "тест")]
    if track_download():
        steps.append(("download", "скачать"))
    steps += [("offer", "смотрели подписку"), ("paid", "оплатили")]
    line = " · ".join(f"{n} {ev.get(k, 0)}" for k, n in steps)
    # обвалились за сутки: 24 ч без шага истекли именно вчера
    cur = await db.execute(f"SELECT {STAGE_EXPR} AS stage, COUNT(*) FROM users_v WHERE paid_at IS NULL AND blocked = 0 "
                           f"AND {LAST_EXPR} >= ? AND {LAST_EXPR} < ? GROUP BY stage", [a - drop_s, b - drop_s])
    drops = {k: v for k, v in await cur.fetchall()}
    left = ev.get("blocked", 0)
    cur = await db.execute("SELECT u.source, COALESCE(c.title,''), COUNT(*) n FROM users_v u LEFT JOIN content c ON c.code=u.source "
                           "WHERE u.created_at >= ? AND u.created_at < ? AND u.source != 'direct' "
                           "GROUP BY u.source ORDER BY n DESC LIMIT 3", [a, b])
    top = await cur.fetchall()
    stars = await _one(db, "SELECT COALESCE(SUM(amount),0) FROM payments WHERE currency='XTR' AND created_at >= ? AND created_at < ?", [a, b])
    rub = await _one(db, "SELECT COALESCE(SUM(amount),0) FROM sales WHERE currency='RUB' AND created_at >= ? AND created_at < ?", [a, b])
    waiting = 0
    for col in (await deals(db))["columns"]:
        if col["key"] in ("request", "dialog", "call"):
            waiting += sum(1 for d in col["items"] if d["waiting"] or col["key"] == "request")
    total = await _one(db, "SELECT COUNT(*) FROM users_v")
    paid_all = await _one(db, "SELECT COUNT(*) FROM users_v WHERE paid_at IS NOT NULL")

    L = [f"📊 Orsofey CRM · {day.strftime('%d.%m')}", "",
         f"👥 Новых в боте: {new}" + (f" ({plat})" if plat else ""), f"↳ {line}"]
    if drops or left:
        L += ["", f"🕳 Обвалились за сутки ({int(cfg.drop_hours)} ч без шага):"]
        for k, _, _ in stages():
            if drops.get(k) and k != "paid":
                L.append(f"• {STAGE_NAME[k].lower()} → {NEXT_STEP[k]}: {drops[k]}")
        if left:
            L.append(f"• заблокировали бота: {left}")
    if top:
        L += ["", "🔥 Лучший контент за сутки:"]
        for code, title, n in top:
            L.append(f"• {code}" + (f" «{title}»" if title else "") + f" — {n} чел.")
    L += ["", f"💰 Оплаты: {_fmt_money(stars, rub)}"]
    if waiting:
        L.append(f"💬 Ждут твоего ответа (заявки и переписки): {waiting}")
    L += ["", f"Всего в базе: {total} · оплатили за всё время: {paid_all}"]
    if cfg.public_url:
        L.append(f"Открыть CRM: {cfg.public_url}")
    return "\n".join(L)
