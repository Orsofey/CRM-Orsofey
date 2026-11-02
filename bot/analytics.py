"""Аналитика каналов простыми словами: прирост просмотров по дням, сравнение соцсетей, обычный ролик (медиана),
вовлечённость, переходы в бота, подписчики, хиты, лучшие дни — и выводы, что с этим делать."""
import datetime as dt
from statistics import median
from . import db as dbm
from .crm import tz

PLATS = ["YouTube", "TikTok", "Instagram", "VK", "Telegram", "X", "Bluesky", "Pinterest", "Discord", "Fur Affinity", "e621",
         "SubscribeStar"]
NO_VIEWS = {"Bluesky", "Discord", "e621", "SubscribeStar"}      # эти соцсети не отдают просмотры — только лайки/избранное
WEEKDAYS = ["понедельник", "вторник", "среду", "четверг", "пятницу", "субботу", "воскресенье"]
H = 3600


def _day(ts: int) -> str:
    return dt.datetime.fromtimestamp(ts, tz()).date().isoformat()


async def _videos(db, platform: str | None = None) -> list[dict]:
    """Реальные публикации (подгружены из соцсетей) + переходы в бота и оплаты по каждой."""
    w, a = "c.synced IS NOT NULL", []
    if platform and platform != "all":
        w += " AND c.platform=?"
        a.append(platform)
    cur = await db.execute(
        f"SELECT c.code, c.platform, c.kind, c.title, c.url, c.keyword, c.views, c.likes, c.comments, c.published_at, "
        f"c.created_at, c.synced, c.ext_id, COUNT(u.id) AS people, SUM(u.paid_at IS NOT NULL) AS paid "
        f"FROM content_v c LEFT JOIN users u ON u.source=c.code WHERE {w} GROUP BY c.code", a)
    out = []
    for r in await cur.fetchall():
        v = dict(r)
        v["views"], v["likes"], v["comments"] = v["views"] or 0, v["likes"] or 0, v["comments"] or 0
        v["people"], v["paid"] = v["people"] or 0, v["paid"] or 0
        v["published_at"] = v["published_at"] or v["created_at"]
        v["short"] = v["kind"] in ("Шортс", "Reels", "Клип")
        ext = (v.get("ext_id") or "").split(":", 1)[-1].split("_")[-1]
        v["no_link"] = bool(ext and v["code"].endswith(ext))      # ролик не связан с ботом (нет метки/слова)
        out.append(v)
    return out


async def _snapshots(db, codes: list[str], since: int) -> dict[str, list]:
    if not codes:
        return {}
    out: dict[str, list] = {}
    for i in range(0, len(codes), 400):
        part = codes[i:i + 400]
        q = ",".join("?" * len(part))
        cur = await db.execute(f"SELECT code, at, views, likes, comments FROM content_stats WHERE code IN ({q}) AND at >= ? "
                               f"ORDER BY code, at", part + [since])
        for code, at, v, l, c in await cur.fetchall():
            out.setdefault(code, []).append((at, v or 0, l or 0, c or 0))
    return out


def _gains(video: dict, snaps: list, start: int) -> dict[str, int]:
    """Прирост просмотров ролика по дням. Первый снимок считаем приростом, только если ролик новый
    (иначе при первом подключении канала все старые просмотры «упали» бы в один день)."""
    days: dict[str, int] = {}
    prev = None
    for at, v, _, _ in snaps:
        if prev is None:
            if video["published_at"] and at - video["published_at"] < 3 * 86400 and at >= start:
                days[_day(video["published_at"] if video["published_at"] >= start else at)] = v
        elif at >= start and v > prev:
            days[_day(at)] = days.get(_day(at), 0) + (v - prev)
        prev = v
    return days


def _med(xs):
    xs = [x for x in xs if x is not None]
    return int(median(xs)) if xs else 0


def _er(vs):
    """Вовлечённость, %. None — соцсеть не отдаёт лайки и комментарии (например, Telegram)."""
    views = sum(v["views"] for v in vs)
    act = sum(v["likes"] + v["comments"] for v in vs)
    if not act and vs and all(v["platform"] == "Telegram" for v in vs):
        return None
    if not views:
        return None
    return round(act * 100 / views, 1) if views else 0.0


def _fmt(n) -> str:
    n = int(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f} млн".replace(".0 ", " ")
    if n >= 10_000:
        return f"{n / 1000:.0f} тыс."
    return f"{n:,}".replace(",", " ")


async def analytics(db, days: int = 30, platform: str | None = None, all_time: bool = False) -> dict:
    now = dbm.now()
    if all_time:              # «всё время»: с первой подгруженной публикации
        cur = await db.execute("SELECT MIN(COALESCE(published_at, created_at)) FROM content_v WHERE synced IS NOT NULL")
        first = (await cur.fetchone())[0] or now
        days = max(1, min(3650, (now - first) // 86400 + 1))
    start = now - days * 86400
    prev_start = start - days * 86400
    vids_all = await _videos(db)
    vids = [v for v in vids_all if not platform or platform == "all" or v["platform"] == platform]
    snaps = await _snapshots(db, [v["code"] for v in vids_all], prev_start - 86400)
    # ---- прирост по дням и платформам
    day_list, d0, d1 = [], dt.date.fromisoformat(_day(start)), dt.date.fromisoformat(_day(now))
    while d0 <= d1:
        day_list.append(d0.isoformat())
        d0 += dt.timedelta(days=1)
    daily = {p: {d: 0 for d in day_list} for p in PLATS}
    gained, gained_prev = {}, {}
    for v in vids_all:
        g = _gains(v, snaps.get(v["code"], []), prev_start)
        cur_g = sum(x for d, x in g.items() if d >= _day(start))
        prev_g = sum(x for d, x in g.items() if d < _day(start))
        v["gained"] = cur_g
        gained[v["platform"]] = gained.get(v["platform"], 0) + cur_g
        gained_prev[v["platform"]] = gained_prev.get(v["platform"], 0) + prev_g
        for d, x in g.items():
            if d in daily.get(v["platform"], {}):
                daily[v["platform"]][d] += x
    total_gained = sum(x for p, x in gained.items() if not platform or platform == "all" or p == platform)
    total_prev = sum(x for p, x in gained_prev.items() if not platform or platform == "all" or p == platform)
    history_days = 0
    cur = await db.execute("SELECT MIN(at) FROM content_stats")
    first = (await cur.fetchone())[0]
    if first:
        history_days = max(0, (now - first) // 86400)
    # ---- публикации периода
    fresh_all = [v for v in vids_all if v["published_at"] >= start]
    mature_all = [v for v in vids_all if v["published_at"] < now - 2 * 86400 and v["published_at"] >= now - max(days, 30) * 86400]
    _sel = {id(v) for v in vids}
    fresh = [v for v in fresh_all if id(v) in _sel]
    mature = [v for v in mature_all if id(v) in _sel]
    # ---- люди из бота за период — по соцсети, откуда пришли (как в «Воронке»: метки роликов, ссылки в профиле, слова)
    cur = await db.execute("SELECT platform, COUNT(*), SUM(paid_at IS NOT NULL) FROM users_v WHERE created_at >= ? GROUP BY platform", [start])
    people, paid = {}, {}
    for p, x, y in await cur.fetchall():
        people[p], paid[p] = x or 0, y or 0
    # подключённые (видимые) аккаунты по соцсетям
    cur = await db.execute("SELECT kind, COUNT(*) FROM accounts WHERE hidden=0 GROUP BY kind")
    kind_plat = dict(dbm.ACC_PLATFORM)
    connected = {kind_plat.get(k, k): n for k, n in await cur.fetchall()}
    # ---- подписчики: по каждому видимому аккаунту — последний снимок и снимок на начало периода, потом сумма
    followers = {}
    for p in PLATS:
        now_sum, change, seen = 0, 0, False
        for (acc,) in await (await db.execute("SELECT DISTINCT account FROM channel_stats_v WHERE platform=?", [p])).fetchall():
            last = await (await db.execute("SELECT followers FROM channel_stats_v WHERE platform=? AND account IS ? ORDER BY at DESC LIMIT 1",
                                           [p, acc])).fetchone()
            if not last:
                continue
            before = await (await db.execute("SELECT followers FROM channel_stats_v WHERE platform=? AND account IS ? AND at <= ? "
                                             "ORDER BY at DESC LIMIT 1", [p, acc, start])).fetchone()
            if not before:
                before = await (await db.execute("SELECT followers FROM channel_stats_v WHERE platform=? AND account IS ? ORDER BY at LIMIT 1",
                                                 [p, acc])).fetchone()
            seen = True
            now_sum += last[0]
            change += last[0] - before[0]
        if seen:
            followers[p] = {"now": now_sum, "change": change}
    # ---- сравнение соцсетей: все соцсети, даже без данных (видно, что не подключено)
    rows = []
    for p in PLATS:
        pv = [v for v in vids_all if v["platform"] == p]
        pm = [v for v in mature_all if v["platform"] == p]
        ppl = people.get(p, 0)
        rows.append({
            "platform": p, "connected": connected.get(p, 0),
            "followers": followers.get(p, {}).get("now"), "followers_change": followers.get(p, {}).get("change"),
            "posts": len([v for v in fresh_all if v["platform"] == p]), "views": gained.get(p, 0),
            "views_prev": gained_prev.get(p, 0), "total_views": sum(v["views"] for v in pv),
            "typical": _med([v["views"] for v in pm]), "er": _er(pm or pv),
            "people": ppl, "paid": paid.get(p, 0),
            "per_person": int(gained.get(p, 0) / ppl) if ppl and gained.get(p) else None,
            "shorts_typical": _med([v["views"] for v in pm if v["short"]]),
            "long_typical": _med([v["views"] for v in pm if not v["short"]]),
            "has_data": bool(pv or p in followers or ppl), "no_views": p in NO_VIEWS,
            "likes": sum(v["likes"] for v in pv),
        })
    rows = [r for r in rows if r["connected"]]          # только подключённые соцсети
    sel = [r for r in rows if not platform or platform == "all" or r["platform"] == platform]
    # ---- топы
    for v in vids:
        plat_med = next((r["typical"] for r in rows if r["platform"] == v["platform"]), 0)
        # сравниваем с обычным роликом только «дозревшие» ролики (старше 2 дней), иначе свежий всегда «хуже»
        v["x_typical"] = round(v["views"] / plat_med, 1) if plat_med and v["published_at"] < now - 2 * 86400 else None
    top_now = sorted([v for v in vids if v.get("gained")], key=lambda v: v["gained"], reverse=True)[:5]
    top_fresh = sorted(fresh, key=lambda v: v["views"], reverse=True)[:5]
    top_bot = sorted([v for v in vids if v["people"]], key=lambda v: v["people"], reverse=True)[:5]
    keep = ("code", "platform", "kind", "title", "url", "views", "likes", "comments", "published_at", "people", "paid", "gained",
            "x_typical", "no_link")
    slim = lambda xs: [{k: v.get(k) for k in keep} for v in xs]
    total_people = sum(r["people"] for r in sel)
    res = {
        "days": days, "all_time": all_time, "platform": platform or "all", "history_days": history_days,
        "kpi": {
            "views": total_gained, "views_prev": total_prev,
            # сравниваем с прошлым периодом, только если история просмотров его покрывает
            "views_change": round((total_gained - total_prev) * 100 / total_prev) if total_prev and history_days >= 2 * days - 1 else None,
            "posts": len(fresh), "typical": _med([v["views"] for v in mature]), "er": _er(mature or vids),
            "people": total_people, "paid": sum(r["paid"] for r in sel),
            "per_person": int(total_gained / total_people) if total_people and total_gained else None,
            "followers": sum(f["now"] for p, f in followers.items() if not platform or platform == "all" or p == platform),
            "followers_change": sum(f["change"] for p, f in followers.items() if not platform or platform == "all" or p == platform),
            "total_views": sum(v["views"] for v in vids),
        },
        "daily": {"days": day_list, "series": [{"platform": p, "values": [daily[p][d] for d in day_list]}
                                               for p in PLATS if any(daily[p].values())
                                               and (not platform or platform == "all" or p == platform)]},
        "platforms": rows, "top_now": slim(top_now), "top_fresh": slim(top_fresh), "top_bot": slim(top_bot),
    }
    if all_time:   # за всё время честнее показывать все просмотры роликов, а не только прирост с момента подключения CRM
        k = res["kpi"]
        k["views_tracked"], k["views"], k["views_change"] = k["views"], k["total_views"], None
        k["per_person"] = int(k["views"] / k["people"]) if k["people"] and k["views"] else None
        for r in rows:
            r["views"] = r["total_views"]
            r["per_person"] = int(r["views"] / r["people"]) if r["people"] and r["views"] else None
    res["patreon"] = await patreon_summary(db, start)
    res["has_bot"] = not await dbm.get_setting(db, "ws_owner", "")
    res["insights"] = insights(res, vids, mature)
    if platform and platform != "all":       # выбрана одна соцсеть — выводы только про неё
        others = [p for p in PLATS if p != platform]
        res["insights"] = [i for i in res["insights"] if platform in i["text"] or not any(p in i["text"] for p in others)]
    if not res["has_bot"]:      # в CRM друга нет бота — выводы про переходы в бота не нужны
        res["insights"] = [i for i in res["insights"] if not any(w in i["text"] for w in ("в бота", "боту", "ботом", "в боте"))]
    if res["patreon"]:
        p = res["patreon"]
        if p["change"]:
            was = f" (было {_fmt(p['mrr_before'])})" if p.get("mrr_before") is not None and p["mrr_before"] != p["mrr"] else ""
            res["insights"].insert(0, {"type": "good" if p["change"] > 0 else "warn",
                                       "text": f"Patreon: платных подписчиков {'стало больше' if p['change'] > 0 else 'стало меньше'} на "
                                               f"{abs(p['change'])} за период, сейчас {p['paid']}, доход в месяц — {_fmt(p['mrr'])}{was}."})
    return res


async def patreon_summary(db, start: int) -> dict | None:
    """Patreon: платные подписчики, доход в месяц и их изменение за период (по всем видимым аккаунтам вместе)."""
    import json as _j
    accs = [a for (a,) in await (await db.execute("SELECT DISTINCT account FROM channel_stats_v WHERE platform='Patreon'")).fetchall()]
    if not accs:
        return None
    out = {"paid": 0, "change": 0, "free": 0, "mrr": 0, "mrr_before": 0, "new_30": 0, "left_30": 0, "declined": 0,
           "tiers": [], "title": "", "url": "", "history": []}
    titles, hist = [], {}
    for acc in accs:
        last = await (await db.execute("SELECT followers, extra, at FROM channel_stats_v WHERE platform='Patreon' AND account IS ? "
                                       "ORDER BY at DESC LIMIT 1", [acc])).fetchone()
        if not last:
            continue
        before = await (await db.execute("SELECT followers, extra FROM channel_stats_v WHERE platform='Patreon' AND account IS ? "
                                         "AND at <= ? ORDER BY at DESC LIMIT 1", [acc, start])).fetchone()
        if not before:
            before = await (await db.execute("SELECT followers, extra FROM channel_stats_v WHERE platform='Patreon' AND account IS ? "
                                             "ORDER BY at LIMIT 1", [acc])).fetchone()
        ex, exb = _j.loads(last[1] or "{}"), _j.loads(before[1] or "{}")
        out["paid"] += last[0]
        out["change"] += last[0] - before[0]
        for k in ("free", "mrr", "new_30", "left_30", "declined"):
            out[k] += ex.get(k, 0) or 0
        out["mrr_before"] += exb.get("mrr", ex.get("mrr", 0)) or 0
        out["tiers"] += ex.get("tiers", [])
        if ex.get("title"):
            titles.append(ex["title"])
        out["url"] = out["url"] or ex.get("url", "")
        # история: на каждый снимок — сумма последних значений всех аккаунтов
        for f, a in await (await db.execute("SELECT followers, at FROM channel_stats_v WHERE platform='Patreon' AND account IS ? "
                                             "AND at >= ? ORDER BY at", [acc, start])).fetchall():
            hist.setdefault(a, {})[acc] = f
    out["title"] = " + ".join(titles)
    out["mrr"] = round(out["mrr"], 2)
    out["mrr_before"] = round(out["mrr_before"], 2)
    lastv = {}
    for a in sorted(hist):
        lastv.update(hist[a])
        out["history"].append({"at": a, "paid": sum(lastv.values())})
    return out


def insights(res: dict, vids: list, mature: list) -> list[dict]:
    """Выводы простыми словами: что работает, что нет и что сделать."""
    out = []
    rows = [r for r in res["platforms"] if r["typical"] or r["views"] or r["people"]]
    k = res["kpi"]
    if res["history_days"] < 2:
        out.append({"type": "info", "text": "История просмотров только начала копиться: прирост по дням станет точным через пару дней после подключения соцсетей. Сейчас точны итоговые просмотры каждого ролика."})
    if k["views_change"] is not None and abs(k["views_change"]) >= 10:
        up = k["views_change"] > 0
        out.append({"type": "good" if up else "warn",
                    "text": f"Просмотров за период {'больше' if up else 'меньше'}, чем за прошлый такой же период, на {abs(k['views_change'])}%."
                            + ("" if up else " Проверь частоту публикаций: роликов вышло " + str(k["posts"]) + ".")})
    reach = [r for r in rows if r["typical"]]
    if len(reach) >= 2:
        best = max(reach, key=lambda r: r["typical"])
        worst = min(reach, key=lambda r: r["typical"])
        if worst["typical"] and best["typical"] >= 1.5 * worst["typical"]:
            out.append({"type": "info", "text": f"Больше всего охвата даёт {best['platform']}: обычный ролик там набирает {_fmt(best['typical'])} "
                                               f"просмотров — в {best['typical'] / worst['typical']:.1f} раза больше, чем в {worst['platform']}."})
    conv = [r for r in rows if r["people"] >= 3 and r["views"]]
    if len(conv) >= 2:
        best = min(conv, key=lambda r: r["views"] / r["people"])
        out.append({"type": "good", "text": f"Лучше всего в бота приводит {best['platform']}: один человек на каждые "
                                           f"{_fmt(best['views'] / best['people'])} просмотров. Ставь туда призыв «напиши боту» чаще."})
        weak = [r for r in conv if r is not best and r["views"] / r["people"] > 3 * best["views"] / best["people"]]
        for r in weak[:1]:
            out.append({"type": "warn", "text": f"{r['platform']} смотрят, но в бота почти не идут ({_fmt(r['views'] / r['people'])} просмотров на 1 человека). "
                                               f"Проверь, есть ли в роликах там понятный призыв и кодовое слово."})
    no_people = [r for r in rows if r["views"] > 3000 and r["people"] == 0]
    for r in no_people[:1]:
        out.append({"type": "warn", "text": f"{r['platform']}: {_fmt(r['views'])} просмотров за период, но ни одного человека в боте. "
                                           f"Добавь ссылку с меткой в описание или кодовое слово в подпись."})
    sh = [r for r in rows if r["shorts_typical"] and r["long_typical"]]
    for r in sh[:1]:
        ratio = r["shorts_typical"] / r["long_typical"]
        if ratio >= 2 or ratio <= 0.5:
            out.append({"type": "info", "text": f"В {r['platform']} {'шортсы' if ratio > 1 else 'длинные видео'} набирают в "
                                               f"{max(ratio, 1 / ratio):.1f} раза больше: обычный шортс — {_fmt(r['shorts_typical'])}, "
                                               f"обычное длинное — {_fmt(r['long_typical'])}."})
    meds = {}
    for v in mature:
        meds.setdefault(v["platform"], []).append(v["views"])
    hits = [v for v in vids if v["published_at"] >= dbm.now() - res["days"] * 86400 and meds.get(v["platform"])
            and len(meds[v["platform"]]) >= 4 and v["views"] >= 3 * _med(meds[v["platform"]]) and v["views"] > 500]
    for v in sorted(hits, key=lambda v: v["views"], reverse=True)[:2]:
        out.append({"type": "good", "text": f"Хит: «{(v['title'] or v['code'])[:60]}» ({v['platform']}) — {_fmt(v['views'])} просмотров, "
                                           f"в {v['views'] / _med(meds[v['platform']]):.0f} раз больше обычного. Сними продолжение или перевыложи в другие соцсети."})
    # лучший день недели — по медиане просмотров роликов за 90 дней
    recent = [v for v in mature if v["published_at"] >= dbm.now() - 90 * 86400]
    if len(recent) >= 10:
        by_wd: dict[int, list] = {}
        for v in recent:
            by_wd.setdefault(dt.datetime.fromtimestamp(v["published_at"], tz()).weekday(), []).append(
                v["views"] / max(1, _med(meds.get(v["platform"], [1]))))
        good = {d: median(x) for d, x in by_wd.items() if len(x) >= 2}
        if len(good) >= 3:
            d = max(good, key=good.get)
            if good[d] >= 1.3:
                out.append({"type": "info", "text": f"Ролики, вышедшие в {WEEKDAYS[d]}, набирают в среднем в {good[d]:.1f} раза больше обычного. "
                                                   f"Ставь важные публикации на этот день."})
    no_link = [v for v in vids if v["no_link"] and v["published_at"] >= dbm.now() - 90 * 86400]
    if len(no_link) >= 3:
        out.append({"type": "warn", "text": f"{len(no_link)} роликов за 3 месяца не связаны с ботом: в описании нет ссылки с меткой, "
                                           f"а в подписи — кодового слова. Люди с них не считаются. Публикуй через экран «Публикация» — "
                                           f"он ставит метки и слова сам."})
    if k["er"]:
        out.append({"type": "info", "text": f"Вовлечённость {k['er']}%: столько зрителей ставят лайк или пишут комментарий. "
                                           + ("Это хороший уровень." if k["er"] >= 4 else "Нормально 2–6%; подними вопросом к зрителям в конце ролика.")})
    return out


async def video_detail(db, code: str) -> dict | None:
    cur = await db.execute("SELECT * FROM content WHERE code=?", [code])
    r = await cur.fetchone()
    if not r:
        return None
    v = dict(r)
    cur = await db.execute("SELECT at, views, likes, comments FROM content_stats WHERE code=? ORDER BY at", [code])
    v["history"] = [{"at": a, "views": b, "likes": c, "comments": d} for a, b, c, d in await cur.fetchall()]
    cur = await db.execute("SELECT COUNT(*), SUM(paid_at IS NOT NULL), SUM(gift_at IS NOT NULL), SUM(tested_at IS NOT NULL) "
                           "FROM users WHERE source=?", [code])
    a, b, c, d = await cur.fetchone()
    v.update(people=a or 0, paid=b or 0, gift=c or 0, test=d or 0)
    same = [x for x in await _videos(db, v["platform"]) if x["code"] != code and x["published_at"] < dbm.now() - 2 * 86400]
    typ = _med([x["views"] for x in same])
    v["typical"] = typ
    v["x_typical"] = round((v["views"] or 0) / typ, 1) if typ else None
    v["fresh"] = bool((v.get("published_at") or 0) >= dbm.now() - 2 * 86400)
    v["er"] = round(((v["likes"] or 0) + (v["comments"] or 0)) * 100 / v["views"], 1) if v.get("views") else 0
    v["per_person"] = int(v["views"] / v["people"]) if v["people"] and v.get("views") else None
    pub = v.get("published_at") or v.get("created_at")
    v["age_days"] = round((dbm.now() - pub) / 86400, 1) if pub else None
    # просмотры через сутки после выхода (если есть снимок рядом)
    if pub and v["history"]:
        near = [h for h in v["history"] if abs(h["at"] - (pub + 86400)) <= 8 * H]
        v["day1"] = near[0]["views"] if near else None
    return v


async def video_list(db, platform: str | None = None) -> list:
    vids = await _videos(db, platform)
    snaps = await _snapshots(db, [v["code"] for v in vids], dbm.now() - 14 * 86400)
    meds = {}
    for v in vids:
        if v["published_at"] < dbm.now() - 2 * 86400:
            meds.setdefault(v["platform"], []).append(v["views"])
    for v in vids:
        pts = [s[1] for s in snaps.get(v["code"], [])][-12:]
        v["spark"] = pts if len(pts) >= 2 else []
        v["gained_7d"] = (pts[-1] - pts[0]) if len(pts) >= 2 else None
        m = _med(meds.get(v["platform"], []))
        v["x_typical"] = round(v["views"] / m, 1) if m and v["published_at"] < dbm.now() - 2 * 86400 else None
        v["fresh"] = v["published_at"] >= dbm.now() - 2 * 86400
        v["er"] = round((v["likes"] + v["comments"]) * 100 / v["views"], 1) if v["views"] else 0
    return sorted(vids, key=lambda v: v["published_at"] or 0, reverse=True)


async def compact(db):
    """История: подробно за 14 дней, дальше — один снимок в день на ролик (база не разрастается)."""
    edge = dbm.now() - 14 * 86400
    await db.execute("DELETE FROM content_stats WHERE at < ? AND id NOT IN (SELECT MAX(id) FROM content_stats WHERE at < ? "
                     "GROUP BY code, date(at, 'unixepoch'))", (edge, edge))
    await db.commit()
