"""Автоподгрузка контента из соцсетей: один раз указал канал — дальше все ролики и статистика приходят сами.

YouTube: все видео и шортсы канала (до 500 последних) с просмотрами, лайками и комментариями.
VK: посты и видео со стены сообщества/страницы (до 300 последних) с просмотрами.

Метка ролика берётся из ссылки на бота в его описании (t.me/бот?start=yts_v12_3) — так просмотры
и люди из бота сходятся в одной карточке. Если ссылки нет, ролик всё равно попадает в «Контент» (метка
yt_<id>/yts_<id>/vk_<id>) — его просмотры считаются в охват платформы.
"""
import json, logging, re
from . import db as dbm
from .config import cfg

log = logging.getLogger(__name__)
H = 3600
SYNC_EVERY = 3 * H
START_RE = re.compile(r"t\.me/[A-Za-z0-9_]{3,}\?start=([A-Za-z0-9_\-]{1,64})", re.I)
DUR_RE = re.compile(r"P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")
SHORT_MAX_S = 180   # шортсы на YouTube — до 3 минут


class SyncError(Exception):
    pass


def duration_s(iso: str) -> int:
    m = DUR_RE.fullmatch(iso or "")
    if not m:
        return 0
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


def codes_in(text: str) -> list[str]:
    out = []
    for c in START_RE.findall(text or ""):
        if c not in out:
            out.append(c)
    return out


def parse_yt_channel(s: str) -> tuple[str, str]:
    """Ссылка/handle/id канала → ('id', 'UC…') или ('handle', '@name')."""
    s = (s or "").strip()
    m = re.search(r"youtube\.com/channel/(UC[\w-]{20,})", s)
    if m:
        return "id", m.group(1)
    if re.fullmatch(r"UC[\w-]{20,}", s):
        return "id", s
    m = re.search(r"youtube\.com/(@[\w.\-]+)", s)
    if m:
        return "handle", m.group(1)
    m = re.search(r"youtube\.com/(?:c|user)/([\w.\-]+)", s)
    if m:
        return "handle", "@" + m.group(1)
    s = s.split("?")[0].strip("/ ")
    return "handle", s if s.startswith("@") else "@" + s


def parse_vk_owner(s: str) -> str:
    """Ссылка на сообщество/страницу VK → короткое имя или id (club123 / public123 / id123 / orsofey)."""
    s = (s or "").strip()
    m = re.search(r"vk\.(?:com|ru)/([\w.]+)", s)
    if m:
        s = m.group(1)
    return s.split("?")[0].strip("/ @")


async def keyword_map(db) -> list[tuple[str, str, str]]:
    """Кодовые слова из «Контента»: (СЛОВО, метка, платформа)."""
    cur = await db.execute("SELECT code, keyword FROM content WHERE keyword IS NOT NULL AND keyword != ''")
    return [(kw.strip().upper(), code, dbm.parse_code(code)[0]) for code, kw in await cur.fetchall() if kw.strip()]


def assign_codes(items: list[dict], default_code, keywords: list | None = None) -> None:
    """Каждому ролику — метку из его описания. Метка, которая стоит сразу в нескольких роликах
    (например, общая ссылка tt_bio в шапке описания), не годится — такой ролик получает свою метку.
    Нет ссылки — ищем в описании кодовое слово этой же платформы («напиши боту АГЕНТ12»)."""
    seen: dict[str, int] = {}
    for it in items:
        for c in it["codes"]:
            seen[c] = seen.get(c, 0) + 1
    kw_used: dict[str, int] = {}
    for it in items:
        own = [c for c in it["codes"] if seen[c] == 1 and not c.lower().endswith("_bio")]
        it["code"] = own[0] if own else None
        if not it["code"] and keywords:
            words = set(re.findall(r"[\wА-ЯЁ]+", (it.get("text") or "").upper()))
            for kw, code, plat in keywords:
                if plat == it["platform"] and kw in words:
                    it["code"], it["by_kw"] = code, True
                    kw_used[code] = kw_used.get(code, 0) + 1
                    break
    for it in items:
        if it.get("by_kw") and kw_used.get(it["code"], 0) > 1:
            it["code"] = None          # одно слово в нескольких роликах — не угадываем
        if not it["code"]:
            it["code"], it["default"] = default_code(it), True


async def upsert_items(db, items: list[dict], source: str, account: int | None = None) -> int:
    """Записать ролики в «Контент». Если ролик раньше жил под другой меткой — переносим на новую."""
    n = 0
    for it in items:
        code = dbm.clean_code(it["code"])
        if not code:
            continue
        cur = await db.execute("SELECT code FROM content WHERE ext_id=?", (it["ext_id"],))
        olds = [r[0] for r in await cur.fetchall()]
        if it.get("default") and any(re.search(r"_p\d+$", o) for o in olds):
            code = next(o for o in olds if re.search(r"_p\d+$", o))   # ролик из «Публикации» — метка остаётся его
        for old in olds:
            if old != code:
                await db.execute("DELETE FROM content WHERE code=? AND synced=?", (old, source))
        await db.execute(
            "INSERT INTO content(code, platform, kind, title, url, keyword, views, views_at, published_at, created_at, "
            "ext_id, likes, comments, synced, account) VALUES(?,?,?,?,?,'',?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(code) DO UPDATE SET platform=excluded.platform, kind=excluded.kind, "
            "title=CASE WHEN content.title IS NULL OR content.title='' OR content.synced IS NOT NULL THEN excluded.title ELSE content.title END, "
            "url=excluded.url, views=excluded.views, views_at=excluded.views_at, published_at=excluded.published_at, "
            "ext_id=excluded.ext_id, likes=excluded.likes, comments=excluded.comments, synced=excluded.synced, account=excluded.account",
            (code, it["platform"], it["kind"], it["title"][:200], it["url"], int(it["views"] or 0), dbm.now(),
             it.get("published_at"), dbm.now(), it["ext_id"], int(it.get("likes") or 0), int(it.get("comments") or 0), source,
             account))
        n += 1
        # снимок статистики для графиков роста (пишем, только если цифры изменились)
        cur = await db.execute("SELECT views, likes, comments FROM content_stats WHERE code=? ORDER BY id DESC LIMIT 1", (code,))
        last = await cur.fetchone()
        now_vals = (int(it["views"] or 0), int(it.get("likes") or 0), int(it.get("comments") or 0))
        if last is None or tuple(last) != now_vals:
            await db.execute("INSERT INTO content_stats(code, at, views, likes, comments) VALUES(?,?,?,?,?)",
                             (code, dbm.now()) + now_vals)
    await db.commit()
    return n


async def record_channel(db, platform: str, followers: int | None, views: int | None = None, extra: dict | None = None,
                         account: int | None = None):
    """Снимок подписчиков канала (раз в синхронизацию) — для графика роста аудитории."""
    if followers is None:
        return
    await db.execute("INSERT INTO channel_stats(platform, at, followers, views, extra, account) VALUES(?,?,?,?,?,?)",
                     (platform, dbm.now(), int(followers), int(views or 0), json.dumps(extra, ensure_ascii=False) if extra else None,
                      account))
    await db.commit()


# ------------------------------------------------------------------ Аккаунты: на одну соцсеть — сколько угодно
KINDS = ("yt", "vk", "tt", "ig", "tg", "x", "bs", "pn", "dc", "fa", "e6", "pt", "ss")
KIND_NAME = {"yt": "YouTube", "vk": "VK", "tt": "TikTok", "ig": "Instagram", "tg": "Telegram", "x": "X", "pt": "Patreon",
             "bs": "Bluesky", "pn": "Pinterest", "dc": "Discord", "fa": "Fur Affinity", "e6": "e621", "ss": "SubscribeStar"}
OAUTH_KIND = {"tiktok": "tt", "instagram": "ig", "patreon": "pt", "pinterest": "pn", "discord": "dc", "subscribestar": "ss",
              "bluesky": "bs", "youtube": "yt", "vkid": "vk", "x": "x"}


def _acc(r) -> dict:
    a = dict(r)
    try:
        a["data"] = json.loads(a.get("data") or "{}")
    except ValueError:
        a["data"] = {}
    try:
        a["status"] = json.loads(a.get("status") or "null")
    except ValueError:
        a["status"] = None
    return a


async def accounts(db, kind: str | None = None) -> list[dict]:
    if kind:
        cur = await db.execute("SELECT * FROM accounts WHERE kind=? ORDER BY id", (kind,))
    else:
        cur = await db.execute("SELECT * FROM accounts ORDER BY id")
    return [_acc(r) for r in await cur.fetchall()]


async def get_account(db, aid) -> dict | None:
    try:
        aid = int(aid)
    except (TypeError, ValueError):
        return None
    cur = await db.execute("SELECT * FROM accounts WHERE id=?", (aid,))
    r = await cur.fetchone()
    return _acc(r) if r else None


async def add_account(db, kind: str, ref: str = "", name: str = "", data: dict | None = None, ext: str = "") -> dict:
    """Новый аккаунт. Тот же самый (по id в соцсети или по ссылке) не дублируем — обновляем."""
    for a in await accounts(db, kind):
        if (ext and a["ext"] == ext) or (ref and not ext and (a["ref"] or "").strip().lower() == ref.strip().lower()):
            a["data"].update(data or {})
            if name:
                a["name"] = name
            if ref:
                a["ref"] = ref
            await save_account(db, a)
            return a
    await db.execute("INSERT INTO accounts(kind, ext, name, ref, data, hidden, created_at) VALUES(?,?,?,?,?,0,?)",
                     (kind, ext, name or KIND_NAME[kind], ref, json.dumps(data or {}), dbm.now()))
    cur = await db.execute("SELECT last_insert_rowid()")
    aid = (await cur.fetchone())[0]
    await db.commit()
    return await get_account(db, aid)


async def save_account(db, a: dict):
    await db.execute("UPDATE accounts SET ext=?, name=?, ref=?, data=? WHERE id=?",
                     (a.get("ext") or "", a.get("name") or "", a.get("ref") or "", json.dumps(a.get("data") or {}), a["id"]))
    await db.commit()


async def _dedupe(db, a: dict):
    """Тот же канал добавили второй раз (другой ссылкой) — сливаем в уже существующий аккаунт, новый убираем."""
    if not a.get("ext") or not a.get("id"):
        return
    cur = await db.execute("SELECT id FROM accounts WHERE kind=? AND ext=? AND id<? ORDER BY id LIMIT 1", (a["kind"], a["ext"], a["id"]))
    r = await cur.fetchone()
    if not r:
        return
    old = r[0]
    await db.execute("UPDATE content SET account=? WHERE account=?", (old, a["id"]))
    await db.execute("UPDATE channel_stats SET account=? WHERE account=?", (old, a["id"]))
    await db.execute("UPDATE accounts SET ref=?, name=?, data=?, status=(SELECT status FROM accounts WHERE id=?) WHERE id=?",
                     (a.get("ref") or "", a.get("name") or "", json.dumps(a.get("data") or {}), a["id"], old))
    await db.execute("DELETE FROM accounts WHERE id=?", (a["id"],))
    a["id"] = old
    await db.commit()


async def set_hidden(db, aid: int | None, hidden: bool):
    """aid=None — все аккаунты сразу."""
    if aid is None:
        await db.execute("UPDATE accounts SET hidden=?", (1 if hidden else 0,))
    else:
        await db.execute("UPDATE accounts SET hidden=? WHERE id=?", (1 if hidden else 0, int(aid)))
    await db.commit()


async def delete_account(db, aid: int):
    """Убрать аккаунт и его ролики со статистикой. Ролики, с которых пришли люди, остаются как метки."""
    aid = int(aid)
    cur = await db.execute("SELECT code FROM content WHERE account=? AND EXISTS(SELECT 1 FROM users u WHERE u.source=content.code)", (aid,))
    keep = [r[0] for r in await cur.fetchall()]
    for code in keep:
        await db.execute("UPDATE content SET account=NULL, synced=NULL WHERE code=?", (code,))
    await db.execute("DELETE FROM content_stats WHERE code IN (SELECT code FROM content WHERE account=?)", (aid,))
    await db.execute("DELETE FROM content WHERE account=?", (aid,))
    await db.execute("DELETE FROM channel_stats WHERE account=?", (aid,))
    await db.execute("DELETE FROM accounts WHERE id=?", (aid,))
    await db.commit()


async def publish_accounts(db, kind: str) -> list[dict]:
    """Куда публиковать: все аккаунты соцсети, у которых не выключено «Публиковать сюда»."""
    return [a for a in await accounts(db, kind) if a["data"].get("publish", True) is not False]


async def main_account(db, kind: str) -> dict | None:
    """Аккаунт для автопубликации: первый видимый подключённый (или первый вообще)."""
    accs = [a for a in await accounts(db, kind) if (a["data"].get("token") or a["data"].get("access"))]
    vis = [a for a in accs if not a["hidden"]]
    return (vis or accs or [None])[0]


def _iso_ts(s: str | None) -> int | None:
    import datetime as dt
    try:
        return int(dt.datetime.fromisoformat((s or "").replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


# ------------------------------------------------------------------ YouTube
YT = "https://www.googleapis.com/youtube/v3/"


async def youtube_sync(db, fetch, key: str, acc, max_videos: int = 500) -> dict:
    """fetch(url, params) -> dict (JSON). acc — аккаунт (или просто ссылка на канал). Возвращает сводку для «Настроек»."""
    if isinstance(acc, str):
        acc = {"id": None, "ref": acc, "data": {}}
    channel = acc["ref"]
    kind, val = parse_yt_channel(channel)
    params = {"part": "snippet,contentDetails,statistics"}
    if key:
        params["key"] = key
    params["id" if kind == "id" else "forHandle"] = val
    data = await fetch(YT + "channels", params)
    _yt_err(data)
    if not isinstance(data, dict):
        raise SyncError("YouTube не ответил — попробую позже")
    if not data.get("items"):
        raise SyncError(f"Канал «{channel}» не найден — проверь ссылку")
    ch = data["items"][0]
    uploads = ch["contentDetails"]["relatedPlaylists"]["uploads"]
    ids, token = [], None
    while len(ids) < max_videos:
        p = {"part": "contentDetails", "playlistId": uploads, "maxResults": 50}
        if key:
            p["key"] = key
        if token:
            p["pageToken"] = token
        page = await fetch(YT + "playlistItems", p)
        _yt_err(page)
        ids += [i["contentDetails"]["videoId"] for i in page.get("items", [])]
        token = page.get("nextPageToken")
        if not token:
            break
    items = []
    for i in range(0, len(ids), 50):
        vs = await fetch(YT + "videos", dict({"part": "snippet,statistics,contentDetails", "id": ",".join(ids[i:i + 50])},
                                             **({"key": key} if key else {})))
        _yt_err(vs)
        for v in vs.get("items", []):
            sn, st = v.get("snippet", {}), v.get("statistics", {})
            short = 0 < duration_s(v.get("contentDetails", {}).get("duration", "")) <= SHORT_MAX_S
            items.append({
                "ext_id": "yt:" + v["id"], "vid": v["id"], "short": short,
                "platform": "YouTube", "kind": "Шортс" if short else "Видео",
                "title": sn.get("title", ""), "codes": codes_in(sn.get("description", "")),
                "text": sn.get("title", "") + " " + sn.get("description", ""),
                "url": f"https://youtube.com/shorts/{v['id']}" if short else f"https://youtu.be/{v['id']}",
                "views": int(st.get("viewCount", 0)), "likes": int(st.get("likeCount", 0)),
                "comments": int(st.get("commentCount", 0)), "published_at": _iso_ts(sn.get("publishedAt")),
            })
    assign_codes(items, lambda it: ("yts_" if it["short"] else "yt_") + it["vid"], await keyword_map(db))
    n = await upsert_items(db, items, "youtube", acc["id"])
    st = ch.get("statistics", {})
    await record_channel(db, "YouTube", int(st.get("subscriberCount", 0) or 0), int(st.get("viewCount", 0) or 0), account=acc["id"])
    acc["ext"], acc["name"] = ch.get("id", ""), ch.get("snippet", {}).get("title", "") or acc.get("name")
    return {"ok": True, "title": ch.get("snippet", {}).get("title", ""), "subscribers": int(st.get("subscriberCount", 0) or 0),
            "channel_views": int(st.get("viewCount", 0) or 0), "videos": n,
            "linked": sum(1 for it in items if not it["code"].endswith(it["vid"])),
            "shorts": sum(1 for it in items if it["short"])}


def _yt_err(data: dict):
    if data is None:
        raise SyncError("YouTube не ответил — попробую позже")
    if isinstance(data, dict) and data.get("error"):
        e = data["error"]
        msg = e.get("message", "") if isinstance(e, dict) else str(e)
        reason = ""
        try:
            reason = e["errors"][0]["reason"]
        except Exception:
            pass
        if reason in ("keyInvalid", "badRequest") or "API key not valid" in msg:
            raise SyncError("Ключ YouTube API не подходит — проверь, что скопирован целиком")
        if "has not been used" in msg or reason == "accessNotConfigured":
            raise SyncError("В Google Cloud не включён «YouTube Data API v3» для этого ключа")
        if reason == "quotaExceeded":
            raise SyncError("Исчерпан дневной лимит YouTube API — попробую позже")
        raise SyncError("YouTube: " + re.sub(r"<[^>]+>", "", msg)[:200])


# ------------------------------------------------------------------ VK
VK = "https://api.vk.com/method/"
VK_V = "5.199"


async def vk_sync(db, fetch, token: str, acc, max_posts: int = 300) -> dict:
    if isinstance(acc, str):
        acc = {"id": None, "ref": acc, "data": {}}
    owner = acc["ref"]
    name = parse_vk_owner(owner)
    base = {"access_token": token, "v": VK_V}
    m = re.fullmatch(r"(club|public|id)(\d+)", name)
    if m:
        owner_id = -int(m.group(2)) if m.group(1) != "id" else int(m.group(2))
        title = name
    else:
        r = await fetch(VK + "utils.resolveScreenName", dict(base, screen_name=name))
        _vk_err(r)
        obj = r.get("response") or {}
        if not obj:
            raise SyncError(f"VK: «{owner}» не найдено — проверь ссылку")
        owner_id = -int(obj["object_id"]) if obj.get("type") in ("group", "page", "event") else int(obj["object_id"])
        title = name
    items, offset = [], 0
    while offset < max_posts:
        r = await fetch(VK + "wall.get", dict(base, owner_id=owner_id, count=100, offset=offset))
        _vk_err(r)
        resp = r.get("response") or {}
        posts = resp.get("items", [])
        for p in posts:
            if p.get("is_pinned") and offset:
                continue
            vids = [a["video"] for a in p.get("attachments", []) if a.get("type") == "video" and a.get("video")]
            text = p.get("text", "") + " " + " ".join(v.get("description", "") for v in vids)
            views = (p.get("views") or {}).get("count", 0)
            if vids:
                v = vids[0]
                clip = v.get("type") == "short_video" or "clip" in str(v.get("type", ""))
                views = max(views, int(v.get("views", 0) or 0))
                items.append({"ext_id": f"vk:{owner_id}_{p['id']}", "pid": p["id"], "platform": "VK",
                              "kind": "Клип" if clip else "Видео", "clip": clip,
                              "title": v.get("title") or p.get("text", "")[:80], "codes": codes_in(text), "text": text,
                              "url": f"https://vk.com/wall{owner_id}_{p['id']}", "views": views,
                              "likes": (p.get("likes") or {}).get("count", 0), "comments": (p.get("comments") or {}).get("count", 0),
                              "published_at": p.get("date")})
            else:
                items.append({"ext_id": f"vk:{owner_id}_{p['id']}", "pid": p["id"], "platform": "VK", "kind": "Пост", "clip": False,
                              "title": (p.get("text") or "Пост без текста").split("\n")[0][:80], "codes": codes_in(text), "text": text,
                              "url": f"https://vk.com/wall{owner_id}_{p['id']}", "views": views,
                              "likes": (p.get("likes") or {}).get("count", 0), "comments": (p.get("comments") or {}).get("count", 0),
                              "published_at": p.get("date")})
        offset += 100
        if len(posts) < 100 or offset >= resp.get("count", 0):
            break
    assign_codes(items, lambda it: (("vkc_" if it["clip"] else "vk_") if it["kind"] != "Пост" else "vkp_") + str(it["pid"]),
                 await keyword_map(db))
    n = await upsert_items(db, items, "vk", acc["id"])
    if owner_id < 0:
        try:
            g = await fetch(VK + "groups.getById", dict(base, group_id=-owner_id, fields="members_count"))
            resp = g.get("response")
            grp = (resp.get("groups") or [None])[0] if isinstance(resp, dict) else (resp or [None])[0]
            if grp:
                title = grp.get("name") or title
                await record_channel(db, "VK", grp.get("members_count"), account=acc["id"])
        except Exception:
            pass
    acc["ext"], acc["name"] = str(owner_id), title
    return {"ok": True, "title": title, "videos": n,
            "linked": sum(1 for it in items if not it["code"].endswith("_" + str(it["pid"])))}


def _vk_err(r: dict):
    if not isinstance(r, dict):
        raise SyncError("VK не ответил — попробую позже")
    if r.get("error"):
        e = r["error"]
        code, msg = e.get("error_code"), e.get("error_msg", "")
        if code == 5:
            raise SyncError("VK: токен не подходит — создай сервисный ключ заново")
        if code == 1051 or "profile type" in msg:
            raise SyncError("VK: ключ от приложения VK ID — VK не пускает такие ключи к стене сообщества. "
                            "Создай на dev.vk.com «Мини-приложение» и вставь его сервисный ключ (см. «Как получить ключ»)")
        if code in (15, 30):
            raise SyncError("VK: стена закрыта — сделай сообщество открытым или используй его короткую ссылку")
        raise SyncError(f"VK: {msg}"[:200])


# ------------------------------------------------------------------ TikTok (Display API, вход через TikTok)
TT_AUTH = "https://www.tiktok.com/v2/auth/authorize/"
TT_TOKEN = "https://open.tiktokapis.com/v2/oauth/token/"
TT_API = "https://open.tiktokapis.com/v2/"
TT_SCOPES = "user.info.basic,video.list"
TT_FIELDS = "id,title,video_description,create_time,share_url,duration,view_count,like_count,comment_count,share_count"


def _tt_err(r):
    if not isinstance(r, dict):
        raise SyncError("TikTok не ответил — попробую позже")
    if r.get("error") and isinstance(r["error"], str):          # ошибки OAuth
        raise SyncError(f"TikTok: {r.get('error_description') or r['error']}"[:200])
    e = r.get("error")
    if isinstance(e, dict) and e.get("code") not in (None, "ok"):
        if e.get("code") in ("access_token_invalid", "access_token_expired"):
            raise SyncError("TikTok: вход устарел — нажми «Войти через TikTok» ещё раз")
        if e.get("code") == "scope_not_authorized":
            raise SyncError("TikTok: при входе не дали доступ к видео — войди ещё раз и разреши")
        raise SyncError(f"TikTok: {e.get('message') or e.get('code')}"[:200])


async def tiktok_exchange(db, http, code: str, redirect_uri: str) -> dict:
    """Код входа → токен → аккаунт (тот же TikTok второй раз не дублируется, а обновляется)."""
    s = await dbm.get_settings(db)
    r = await http("POST", TT_TOKEN, data={"client_key": s["tt_client_key"], "client_secret": s["tt_client_secret"],
                                            "code": code, "grant_type": "authorization_code", "redirect_uri": redirect_uri},
                   headers={"Content-Type": "application/x-www-form-urlencoded"})
    _tt_err(r)
    data = _tt_tokens(r)
    data["scope"] = str(r.get("scope") or "")
    open_id = str(r.get("open_id") or "")
    try:
        me = await http("GET", TT_API + "user/info/", params={"fields": "open_id,display_name"},
                        headers={"Authorization": "Bearer " + r["access_token"]})
        u = (me.get("data") or {}).get("user") or {}
        name, open_id = u.get("display_name", ""), str(u.get("open_id") or open_id)
    except Exception:
        name = ""
    return await add_account(db, "tt", name=name or "TikTok", data=data, ext=open_id)


def _tt_tokens(r: dict) -> dict:
    if not r.get("access_token"):
        raise SyncError("TikTok не выдал доступ — попробуй войти ещё раз")
    d = {"access": r["access_token"], "expires": dbm.now() + int(r.get("expires_in") or 86400)}
    if r.get("refresh_token"):
        d["refresh"] = r["refresh_token"]
    return d


async def tiktok_sync(db, http, acc: dict, max_videos: int = 300) -> dict:
    s = await dbm.get_settings(db)
    d = acc["data"]
    if int(d.get("expires") or 0) < dbm.now() + 2 * H:          # токен живёт сутки — продлеваем сами
        if not d.get("refresh"):
            raise SyncError("TikTok: нужно войти заново")
        r = await http("POST", TT_TOKEN, data={"client_key": s["tt_client_key"], "client_secret": s["tt_client_secret"],
                                                "grant_type": "refresh_token", "refresh_token": d["refresh"]},
                       headers={"Content-Type": "application/x-www-form-urlencoded"})
        _tt_err(r)
        d.update(_tt_tokens(r))
        await save_account(db, acc)
    items, cursor = [], None
    while len(items) < max_videos:
        body = {"max_count": 20}
        if cursor:
            body["cursor"] = cursor
        r = await http("POST", TT_API + "video/list/", params={"fields": TT_FIELDS}, json=body,
                       headers={"Authorization": "Bearer " + d["access"]})
        _tt_err(r)
        page = r.get("data") or {}
        for v in page.get("videos", []):
            text = (v.get("title") or "") + " " + (v.get("video_description") or "")
            items.append({"ext_id": "tt:" + str(v["id"]), "vid": str(v["id"]), "platform": "TikTok", "kind": "Шортс",
                          "title": (v.get("title") or v.get("video_description") or "Видео TikTok").split("\n")[0][:120],
                          "codes": codes_in(text), "text": text, "url": v.get("share_url") or "",
                          "views": int(v.get("view_count") or 0), "likes": int(v.get("like_count") or 0),
                          "comments": int(v.get("comment_count") or 0), "published_at": v.get("create_time")})
        if not page.get("has_more") or not page.get("cursor"):
            break
        cursor = page["cursor"]
    assign_codes(items, lambda it: "tt_" + it["vid"], await keyword_map(db))
    n = await upsert_items(db, items, "tiktok", acc["id"])
    out = {"ok": True, "title": acc.get("name") or "TikTok", "videos": n,
           "linked": sum(1 for it in items if not it["code"].endswith(it["vid"]))}
    # подписчики — только с правом user.info.stats (его надо добавить в приложении TikTok и войти заново)
    if "user.info.stats" in (d.get("scope") or ""):
        try:
            me = await http("GET", TT_API + "user/info/", params={"fields": "open_id,follower_count,following_count,likes_count,video_count"},
                            headers={"Authorization": "Bearer " + d["access"]})
            u = (me.get("data") or {}).get("user") or {}
            if u.get("follower_count") is not None:
                out["followers"] = int(u["follower_count"])
                await record_channel(db, "TikTok", out["followers"], account=acc["id"],
                                     extra={"likes": u.get("likes_count"), "following": u.get("following_count")})
        except Exception as e:
            log.info("TikTok: подписчики не загрузились: %s", e)
    else:
        out["note"] = "Подписчиков TikTok покажу, когда в приложении будет скоуп user.info.stats — см. «Подписчики TikTok» ниже"
    return out


# ------------------------------------------------------------------ Instagram (вход через Instagram, проф. аккаунт)
IG_AUTH = "https://www.instagram.com/oauth/authorize"
IG_TOKEN = "https://api.instagram.com/oauth/access_token"
IG_GRAPH = "https://graph.instagram.com/"
IG_SCOPES = "instagram_business_basic,instagram_business_manage_insights,instagram_business_content_publish"
IG_INSIGHTS_LAST = 60     # статистику просмотров обновляем для 60 последних публикаций (лимиты Instagram)


def ig_v() -> str:
    return cfg.ig_api_version


def _ig_err(r):
    if not isinstance(r, dict):
        raise SyncError("Instagram не ответил — возможно, он недоступен с хостинга (нужен прокси)")
    e = r.get("error")
    if e:
        if isinstance(e, dict):
            if e.get("code") == 190:
                raise SyncError("Instagram: вход устарел — нажми «Войти через Instagram» ещё раз")
            raise SyncError(f"Instagram: {e.get('message', '')}"[:200])
        raise SyncError(f"Instagram: {r.get('error_message') or r.get('error_description') or e}"[:200])
    if r.get("error_type"):
        raise SyncError(f"Instagram: {r.get('error_message', r['error_type'])}"[:200])


async def instagram_exchange(db, http, code: str, redirect_uri: str) -> dict:
    s = await dbm.get_settings(db)
    r = await http("POST", IG_TOKEN, data={"client_id": s["ig_app_id"], "client_secret": s["ig_app_secret"],
                                            "grant_type": "authorization_code", "redirect_uri": redirect_uri,
                                            "code": code.split("#")[0]})
    _ig_err(r)
    if isinstance(r.get("data"), list) and r["data"]:
        r = r["data"][0]
    user_id = str(r.get("user_id") or "")
    short = r.get("access_token")
    if not short:
        raise SyncError("Instagram не выдал доступ — попробуй войти ещё раз")
    long = await http("GET", IG_GRAPH + "access_token", params={"grant_type": "ig_exchange_token",
                                                               "client_secret": s["ig_app_secret"], "access_token": short})
    _ig_err(long)
    data = _ig_tokens(long)
    me = await http("GET", IG_GRAPH + ig_v() + "/me", params={"fields": "user_id,username", "access_token": long["access_token"]})
    ok = isinstance(me, dict) and not me.get("error")
    name = (me or {}).get("username", "") if ok else ""
    user_id = str((me or {}).get("user_id") or user_id) if ok else user_id
    data["user_id"] = user_id
    return await add_account(db, "ig", name=name or "Instagram", data=data, ext=user_id)


def _ig_tokens(r: dict) -> dict:
    if not r.get("access_token"):
        raise SyncError("Instagram не выдал долгий доступ — попробуй войти ещё раз")
    return {"token": r["access_token"], "expires": dbm.now() + int(r.get("expires_in") or 60 * 86400), "token_at": dbm.now()}


async def instagram_sync(db, http, acc: dict, max_media: int = 300) -> dict:
    d = acc["data"]
    tok = d["token"]
    # токен живёт 60 дней — продлеваем за 10 дней до конца (Instagram разрешает продлевать токен старше суток)
    if int(d.get("expires") or 0) < dbm.now() + 10 * 86400 and dbm.now() - int(d.get("token_at") or 0) > 86400:
        r = await http("GET", IG_GRAPH + "refresh_access_token", params={"grant_type": "ig_refresh_token", "access_token": tok})
        _ig_err(r)
        d.update(_ig_tokens(r))
        await save_account(db, acc)
        tok = d["token"]
    url = IG_GRAPH + ig_v() + "/me/media"
    params = {"fields": "id,caption,media_type,media_product_type,permalink,timestamp,like_count,comments_count",
              "limit": 50, "access_token": tok}
    media = []
    while url and len(media) < max_media:
        r = await http("GET", url, params=params)
        _ig_err(r)
        media += r.get("data", [])
        url = (r.get("paging") or {}).get("next")
        params = None            # в ссылке next уже всё есть
    cur = await db.execute("SELECT ext_id, views FROM content WHERE synced='instagram' AND account IS ?", (acc["id"],))
    old_views = {k: v for k, v in await cur.fetchall()}
    items = []
    for i, m in enumerate(media):
        if m.get("media_product_type") == "STORY":
            continue
        ext = "ig:" + str(m["id"])
        views = old_views.get(ext, 0) or 0
        if i < IG_INSIGHTS_LAST:
            ins = await http("GET", IG_GRAPH + ig_v() + f"/{m['id']}/insights", params={"metric": "views", "access_token": tok})
            if isinstance(ins, dict) and not ins.get("error"):
                for row in ins.get("data", []):
                    if row.get("name") == "views":
                        vals = row.get("values") or [{}]
                        views = int((row.get("total_value") or {}).get("value") or vals[0].get("value") or views)
        cap = m.get("caption") or ""
        reel = m.get("media_product_type") == "REELS"
        items.append({"ext_id": ext, "vid": str(m["id"]), "platform": "Instagram", "kind": "Reels" if reel else "Пост",
                      "title": (cap or ("Reels" if reel else "Пост")).split("\n")[0][:120], "codes": codes_in(cap), "text": cap,
                      "url": m.get("permalink") or "", "views": views, "likes": int(m.get("like_count") or 0),
                      "comments": int(m.get("comments_count") or 0), "published_at": _iso_ts((m.get("timestamp") or "").replace("+0000", "+00:00"))})
    assign_codes(items, lambda it: "ig_" + it["vid"], await keyword_map(db))
    n = await upsert_items(db, items, "instagram", acc["id"])
    try:
        me = await http("GET", IG_GRAPH + ig_v() + "/me", params={"fields": "followers_count,username,user_id", "access_token": tok})
        if isinstance(me, dict) and "followers_count" in me:
            await record_channel(db, "Instagram", me["followers_count"], account=acc["id"])
            if me.get("username"):
                acc["name"] = me["username"]
            if me.get("user_id") and not d.get("user_id"):
                d["user_id"] = str(me["user_id"])
                acc["ext"] = acc.get("ext") or d["user_id"]
    except Exception:
        pass
    return {"ok": True, "title": acc.get("name") or "Instagram", "videos": n,
            "linked": sum(1 for it in items if not it["code"].endswith(it["vid"]))}


# ------------------------------------------------------------------ X (Twitter): Bearer Token + имя аккаунта
X_API = "https://api.x.com/2/"


def parse_x_user(s: str) -> str:
    s = (s or "").strip()
    m = re.search(r"(?:x|twitter)\.com/@?([A-Za-z0-9_]{1,15})", s)
    return (m.group(1) if m else s.lstrip("@").split("/")[0].split("?")[0])[:15]


def _x_err(r, status_hint: str = ""):
    if not isinstance(r, dict):
        raise SyncError("X не ответил — возможно, он недоступен с хостинга (нужен прокси)")
    if r.get("data") is not None:
        return
    e0 = (r.get("errors") or [{}])[0] if isinstance(r.get("errors"), list) else {}
    title = str(r.get("title") or e0.get("title") or "")
    detail = str(r.get("detail") or e0.get("detail") or e0.get("message") or "")
    status = r.get("status") or e0.get("status")
    if status == 401 or "Unauthorized" in title:
        raise SyncError("X: Bearer Token не подходит — скопируй его заново в console.x.com")
    if status == 402 or "Credits" in title or "credits" in detail.lower() or "client-not-enrolled" in str(r.get("type", "")):
        raise SyncError("X: на счёте API нет кредитов — пополни баланс в console.x.com (чтение платное)")
    if status == 429 or "Too Many" in title:
        raise SyncError("X: слишком много запросов — попробую позже")
    if "Not Found" in title or "Could not find user" in detail:
        raise SyncError("X: аккаунт не найден — проверь имя")
    raise SyncError(f"X: {title or detail or 'ошибка'} {detail if title else ''}".strip()[:200])


async def x_sync(db, http, bearer: str, acc, max_posts: int = 40) -> dict:
    """Последние посты аккаунта X с показами, лайками, ответами. Повторное чтение того же поста в те же сутки
    X не тарифицирует повторно, поэтому стоимость ≈ число постов × цена чтения в день."""
    if isinstance(acc, str):
        acc = {"id": None, "ref": acc, "data": {}}
    u = parse_x_user(acc["ref"])
    h = {"Authorization": "Bearer " + bearer}
    r = await http("GET", X_API + f"users/by/username/{u}", params={"user.fields": "public_metrics,name"}, headers=h)
    _x_err(r)
    user = r["data"]
    await record_channel(db, "X", (user.get("public_metrics") or {}).get("followers_count"), account=acc["id"])
    acc["ext"], acc["name"] = str(user.get("id") or ""), "@" + (user.get("username") or u)
    r = await http("GET", X_API + f"users/{user['id']}/tweets", headers=h, params={
        "max_results": max(5, min(100, int(max_posts or 40))), "exclude": "retweets,replies",
        "tweet.fields": "created_at,public_metrics,entities"})
    if isinstance(r, dict) and r.get("data") is None and r.get("meta", {}).get("result_count") == 0:
        r = {"data": []}
    _x_err(r)
    items = []
    for t in r.get("data", []):
        urls = " ".join(x.get("expanded_url", "") for x in (t.get("entities") or {}).get("urls", []))
        text = (t.get("text") or "") + " " + urls
        m = t.get("public_metrics") or {}
        items.append({"ext_id": "x:" + t["id"], "vid": t["id"], "platform": "X", "kind": "Пост",
                      "title": (t.get("text") or "Пост").split("\n")[0][:120], "codes": codes_in(text), "text": text,
                      "url": f"https://x.com/{u}/status/{t['id']}", "views": int(m.get("impression_count") or 0),
                      "likes": int(m.get("like_count") or 0), "comments": int(m.get("reply_count") or 0),
                      "published_at": _iso_ts(t.get("created_at"))})
    assign_codes(items, lambda it: "x_" + it["vid"], await keyword_map(db))
    n = await upsert_items(db, items, "x", acc["id"])
    return {"ok": True, "title": "@" + u, "videos": n, "followers": (user.get("public_metrics") or {}).get("followers_count"),
            "linked": sum(1 for it in items if not it["code"].endswith(it["vid"]))}


# ------------------------------------------------------------------ Telegram-канал: подписчики (Bot API) + просмотры постов
# Подписчиков точно отдаёт Bot API (getChatMemberCount) — через бота воронки, ключи не нужны.
# Просмотры постов Bot API не отдаёт; у публичного канала их показывает веб-версия t.me/s/<канал> — оттуда и берём.
TG_API = "https://api.telegram.org/bot{token}/{method}"
TG_WEB = "https://t.me/s/{u}"
TG_POST_RE = re.compile(r'data-post="([A-Za-z0-9_]{4,})/(\d+)"')
TG_MAX_POSTS = 200


def parse_tg_channel(s: str) -> str:
    """@name / t.me/name / https://t.me/s/name / -100… → '@name' или '-100…'; инвайт-ссылка → '+код'."""
    s = (s or "").strip()
    m = re.search(r"t(?:elegram)?\.me/(?:s/)?(\+[\w-]+|joinchat/[\w-]+|[A-Za-z0-9_]{4,})", s)
    if m:
        v = m.group(1)
        return "+" + v.split("/", 1)[1] if v.startswith("joinchat/") else (v if v.startswith("+") else "@" + v)
    if re.fullmatch(r"-?\d{6,}", s):
        return s if s.startswith("-") else "-100" + s
    return "@" + s.lstrip("@").split("/")[0].split("?")[0]


def _tg_num(t: str) -> int:
    t = (t or "").strip().replace(" ", "").replace(",", ".").upper()
    m = re.match(r"([\d.]+)([KM]?)", t)
    if not m:
        return 0
    n = float(m.group(1) or 0)
    return int(n * (1000 if m.group(2) == "K" else 1_000_000 if m.group(2) == "M" else 1))


def _strip_html(h: str) -> str:
    import html as _h
    h = re.sub(r"<br\s*/?>", "\n", h or "")
    return _h.unescape(re.sub(r"<[^>]+>", "", h)).strip()


def parse_tg_page(html: str) -> tuple[list[dict], int | None]:
    """Посты со страницы t.me/s/<канал>: id, текст, ссылки, просмотры, дата, тип. + подписчики со страницы."""
    posts = []
    starts = [m.start() for m in TG_POST_RE.finditer(html)]
    for i, st in enumerate(starts):
        block = html[st: starts[i + 1] if i + 1 < len(starts) else len(html)]
        m = TG_POST_RE.match(block)
        u, pid = m.group(1), int(m.group(2))
        tm = re.search(r'class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', block, re.S)
        text = _strip_html(tm.group(1)) if tm else ""
        hrefs = " ".join(re.findall(r'href="([^"]+)"', block))
        vm = re.search(r'class="tgme_widget_message_views">([^<]+)<', block)
        dm = re.search(r'<time[^>]+datetime="([^"]+)"', block)
        kind = "Видео" if "tgme_widget_message_video" in block or "message_video_player" in block else \
            "Фото" if "tgme_widget_message_photo" in block else "Пост"
        posts.append({"username": u, "id": pid, "text": text, "links": hrefs, "views": _tg_num(vm.group(1)) if vm else 0,
                      "published_at": _iso_ts(dm.group(1)) if dm else None, "kind": kind})
    sub = None
    cm = re.search(r'<span class="counter_value">([^<]+)</span>\s*<span class="counter_type">(?:subscribers|members|подписчик)', html)
    if cm:
        sub = _tg_num(cm.group(1))
    return posts, sub


async def _tg_api_default(method: str, params: dict):
    import aiohttp
    async with aiohttp.ClientSession() as sess:
        async with sess.get(TG_API.format(token=cfg.token, method=method), params=params,
                            timeout=aiohttp.ClientTimeout(total=20)) as r:
            return await r.json(content_type=None)


async def _tg_page_default(url: str, proxy: str | None = None) -> str:
    import aiohttp
    async with aiohttp.ClientSession() as sess:
        async with sess.get(url, proxy=proxy, timeout=aiohttp.ClientTimeout(total=30),
                            headers={"User-Agent": "Mozilla/5.0 (compatible; OrsofeyCRM/1.0)"}) as r:
            return await r.text()


async def bot_channels(main_db) -> dict:
    """Каналы, где бот — админ: {id: {title, username, by, at}} (в базе автора; узнаём из обновлений Telegram)."""
    try:
        return json.loads(await dbm.get_setting(main_db, "bot_channels", "") or "{}")
    except ValueError:
        return {}


async def bot_channel_remember(main_db, chat_id, title: str, username: str, by):
    ch = await bot_channels(main_db)
    ch[str(chat_id)] = {"title": title, "username": username, "by": by, "at": dbm.now()}
    await dbm.set_setting(main_db, "bot_channels", json.dumps(ch, ensure_ascii=False))


async def bot_channel_forget(main_db, chat_id):
    ch = await bot_channels(main_db)
    if ch.pop(str(chat_id), None) is not None:
        await dbm.set_setting(main_db, "bot_channels", json.dumps(ch, ensure_ascii=False))


async def visible_bot_channels(db) -> dict:
    """Какие каналы бота показывать в этой CRM: автору — все, клиенту — только те, куда бота добавил он сам."""
    from . import workspaces
    try:
        main = workspaces.main_db()
    except KeyError:
        main = db
    ch = await bot_channels(main)
    owner = await dbm.get_setting(db, "ws_owner", "")
    if owner.isdigit():
        return {k: v for k, v in ch.items() if str(v.get("by")) == owner}
    return ch


def _invite_hash(s: str) -> str:
    m = re.search(r"(?:\+|joinchat/)([\w-]+)", s or "")
    return m.group(1) if m else ""


async def _resolve_invite(db, ref: str, tg_api) -> str | None:
    """Закрытый канал по ссылке-приглашению: Bot API такую ссылку не понимает, поэтому сверяем её с основными
    ссылками каналов, где бот админ (getChat отдаёт invite_link), и с каналом проверки подписки."""
    from . import runtime
    h = _invite_hash(ref)
    cid = str(runtime.state.get("channel_id") or "")
    if cid and h and h in (runtime.state.get("channel_url") or cfg.channel_url or ""):
        return cid
    for chat_id in await visible_bot_channels(db):
        r = await tg_api("getChat", {"chat_id": chat_id})
        link = ((r or {}).get("result") or {}).get("invite_link", "") if isinstance(r, dict) else ""
        if h and _invite_hash(link) == h:
            return chat_id
    return None


async def tg_sync(db, acc: dict, tg_api=None, tg_page=None, proxy: str | None = None, max_posts: int = TG_MAX_POSTS) -> dict:
    tg_api, tg_page = tg_api or _tg_api_default, tg_page or _tg_page_default
    ref = parse_tg_channel(acc["ref"])
    if ref.startswith("+"):
        known = str(acc["data"].get("chat_id") or "")          # уже находили раньше — берём сразу
        ref = known or await _resolve_invite(db, ref, tg_api)
        if not ref:
            raise SyncError("По ссылке-приглашению канал не нашёлся. Добавь бота админом этого канала — канал появится "
                            "в списке «Каналы, где бот админ» ниже, нажми там «Добавить» (ссылка не нужна)")
    chat = await tg_api("getChat", {"chat_id": ref})
    if not isinstance(chat, dict) or not chat.get("ok"):
        desc = (chat or {}).get("description", "") if isinstance(chat, dict) else ""
        if "not found" in desc.lower():
            raise SyncError("Telegram: канал не найден. Для закрытого канала сначала добавь бота админом канала")
        raise SyncError(f"Telegram: {desc or 'не ответил — попробую позже'}"[:200])
    ch = chat["result"]
    title, username, chat_id = ch.get("title") or ref, ch.get("username") or "", ch.get("id")
    cnt = await tg_api("getChatMemberCount", {"chat_id": chat_id})
    followers = cnt.get("result") if isinstance(cnt, dict) and cnt.get("ok") else None
    items, page_subs = [], None
    if username:
        before, seen = None, set()
        for _ in range(12):
            url = TG_WEB.format(u=username) + (f"?before={before}" if before else "")
            try:
                html = await tg_page(url, proxy)
            except Exception as e:
                if items:
                    break
                raise SyncError(f"Не открылась страница t.me/s/{username} ({type(e).__name__}) — если повторяется, поможет прокси")
            posts, sub = parse_tg_page(html)
            page_subs = page_subs or sub
            new = [p for p in posts if p["id"] not in seen]
            if not new:
                break
            for p in new:
                seen.add(p["id"])
                text = p["text"] + " " + p["links"]
                items.append({"ext_id": f"tg:{username.lower()}/{p['id']}", "vid": str(p["id"]), "platform": "Telegram",
                              "kind": "Пост", "title": (p["text"] or p["kind"]).split("\n")[0][:120], "codes": codes_in(text),
                              "text": text, "url": f"https://t.me/{username}/{p['id']}", "views": p["views"], "likes": 0,
                              "comments": 0, "published_at": p["published_at"]})
            before = min(p["id"] for p in new)
            if len(items) >= max_posts or before <= 1:
                break
        u = username.lower()
        assign_codes(items, lambda it: f"tg_{u}_{it['vid']}", await keyword_map(db))
        await upsert_items(db, items, "telegram", acc["id"])
    if followers is None:
        followers = page_subs
    await record_channel(db, "Telegram", followers, account=acc["id"])
    acc["ext"], acc["name"] = str(chat_id), title
    acc["data"]["chat"] = "@" + username if username else str(chat_id)
    acc["data"]["chat_id"] = str(chat_id)
    res = {"ok": True, "title": title, "subscribers": followers, "videos": len(items), "public": bool(username),
           "linked": sum(1 for it in items if not it.get("default"))}
    if not username:
        res["note"] = "Закрытый канал: Telegram показывает просмотры постов только у публичных каналов — здесь видно подписчиков"
    return res


# ------------------------------------------------------------------ Patreon: платные подписчики и доход
PT_API = "https://www.patreon.com/api/oauth2/v2/"
PT_TOKEN = "https://www.patreon.com/api/oauth2/token"
PT_UA = {"User-Agent": "OrsofeyCRM/1.0"}
PT_AUTH = "https://www.patreon.com/oauth2/authorize"
PT_SCOPES = "identity campaigns campaigns.members"


def _pt_err(r):
    if not isinstance(r, dict):
        raise SyncError("Patreon не ответил — возможно, он недоступен с хостинга (нужен прокси)")
    if r.get("errors"):
        e = r["errors"][0]
        code = str(e.get("code_name") or e.get("status") or "")
        if code in ("Unauthorized", "401") or e.get("status") == "401":
            raise SyncError("Patreon: токен устарел или не подходит — вставь новый Creator's Access Token и Refresh Token")
        if code in ("Forbidden", "403"):
            raise SyncError("Patreon: нет доступа — при создании клиента выбери API v2 и права на кампанию и подписчиков")
        raise SyncError(f"Patreon: {e.get('detail') or e.get('title') or code}"[:200])
    if r.get("error"):
        raise SyncError(f"Patreon: {r.get('error_description') or r['error']}"[:200])


async def patreon_sync(db, http, acc: dict) -> dict:
    s = await dbm.get_settings(db)
    d = acc["data"]
    tok = d.get("access")
    # токен создателя живёт около месяца — продлеваем сами за неделю до конца
    exp = int(d.get("expires") or 0)
    if d.get("refresh") and s.get("pt_client_id") and s.get("pt_client_secret") and (not exp or exp < dbm.now() + 7 * 86400):
        r = await http("POST", PT_TOKEN, data={"grant_type": "refresh_token", "refresh_token": d["refresh"],
                                               "client_id": s["pt_client_id"], "client_secret": s["pt_client_secret"]},
                       headers=dict(PT_UA, **{"Content-Type": "application/x-www-form-urlencoded"}))
        if isinstance(r, dict) and r.get("access_token"):
            tok = r["access_token"]
            d["access"] = tok
            if r.get("refresh_token"):
                d["refresh"] = r["refresh_token"]
            d["expires"] = dbm.now() + int(r.get("expires_in") or 30 * 86400)
            await save_account(db, acc)
        elif not exp:
            pass          # продлить не вышло, но текущий токен может ещё работать — пробуем им
        else:
            _pt_err(r)
    h = dict(PT_UA, Authorization="Bearer " + tok)
    r = await http("GET", PT_API + "campaigns", headers=h,
                   params={"fields[campaign]": "creation_name,patron_count,url,vanity", "include": "tiers",
                           "fields[tier]": "title,amount_cents,patron_count"})
    _pt_err(r)
    if not r.get("data"):
        raise SyncError("Patreon: у аккаунта нет кампании (страницы автора)")
    camp = r["data"][0]
    ca = camp.get("attributes") or {}
    tiers = [{"title": (t.get("attributes") or {}).get("title", ""), "amount": ((t.get("attributes") or {}).get("amount_cents") or 0) / 100,
              "patrons": (t.get("attributes") or {}).get("patron_count") or 0}
             for t in r.get("included", []) if t.get("type") == "tier" and ((t.get("attributes") or {}).get("amount_cents") or 0) > 0]
    url = PT_API + f"campaigns/{camp['id']}/members"
    params = {"fields[member]": "patron_status,currently_entitled_amount_cents,pledge_relationship_start,last_charge_status,is_follower",
              "page[count]": 1000}
    members = []
    for _ in range(20):
        m = await http("GET", url, params=params, headers=h)
        _pt_err(m)
        members += [x.get("attributes") or {} for x in m.get("data", [])]
        url = ((m.get("links") or {}).get("next"))
        params = None
        if not url:
            break
    now = dbm.now()
    paid = [x for x in members if x.get("patron_status") == "active_patron" and (x.get("currently_entitled_amount_cents") or 0) > 0]
    mrr = sum(x.get("currently_entitled_amount_cents") or 0 for x in paid) / 100
    new_30 = sum(1 for x in paid if (_iso_ts(x.get("pledge_relationship_start")) or 0) >= now - 30 * 86400)
    left = sum(1 for x in members if x.get("patron_status") == "former_patron")
    declined = sum(1 for x in members if x.get("patron_status") == "declined_patron")
    free = max(0, int(ca.get("patron_count") or 0) - len(paid))
    # сколько ушло за 30 дней — по разнице снимков (Patreon не отдаёт дату ухода)
    cur = await db.execute("SELECT extra FROM channel_stats WHERE platform='Patreon' AND account IS ? AND at <= ? ORDER BY at DESC LIMIT 1",
                           (acc["id"], now - 30 * 86400))
    old = await cur.fetchone()
    left_30 = max(0, left - json.loads(old[0] or "{}").get("former", left)) if old else 0
    extra = {"mrr": round(mrr, 2), "free": free, "new_30": new_30, "left_30": left_30, "former": left, "declined": declined,
             "tiers": tiers, "title": ca.get("creation_name") or ca.get("vanity") or "", "url": ca.get("url", "")}
    await record_channel(db, "Patreon", len(paid), None, extra, account=acc["id"])
    acc["ext"], acc["name"] = str(camp.get("id") or ""), extra["title"] or acc.get("name") or "Patreon"
    return {"ok": True, "title": extra["title"] or "Patreon", "videos": 0, "paid": len(paid), "mrr": extra["mrr"],
            "free": free, "new_30": new_30, "declined": declined}


# ------------------------------------------------------------------ Вход (OAuth)
def redirect_uri(platform: str) -> str:
    return f"{cfg.public_url}/oauth/{platform}"


def make_state(platform: str, ws: str = "main") -> str:
    from .runtime import sign
    exp = str(dbm.now() + 1800)
    return f"{exp}.{ws}.{sign(platform + exp + ws, 24)}"


def state_ws(platform: str, state: str) -> str | None:
    """Проверить state из ссылки входа соцсети; вернуть пространство CRM, откуда начали вход."""
    import hmac
    from .runtime import sign
    try:
        exp, ws, sig = state.split(".", 2)
        ok = int(exp) > dbm.now() and hmac.compare_digest(sig, sign(platform + exp + ws, 24))
        return ws if ok else None
    except Exception:
        return None


def check_state(platform: str, state: str) -> bool:
    return state_ws(platform, state) is not None


async def oauth_url(db, platform: str, ws: str = "main", handle: str = "") -> str:
    from urllib.parse import urlencode
    from . import networks, atproto
    s = await dbm.get_settings(db)
    if not cfg.public_url:
        raise SyncError("Сначала задай PUBLIC_URL в переменных Amvera — на этот адрес соцсеть вернёт тебя после входа")
    try:
        if platform == "pinterest":
            return networks.pn_auth_url(s, make_state("pinterest", ws))
        if platform == "discord":
            return networks.dc_auth_url(s, make_state("discord", ws))
        if platform == "subscribestar":
            return networks.ss_auth_url(s, make_state("subscribestar", ws))
        if platform == "bluesky":
            return await atproto.oauth_start(db, make_state("bluesky", ws), handle, proxy_url(s))
        from . import posting
        if platform == "youtube":
            return posting.yt_auth_url(s, make_state("youtube", ws))
        if platform == "vkid":
            return await posting.vk_auth_url(db, s, make_state("vkid", ws))
        if platform == "x":
            return await posting.x_auth_url(db, s, make_state("x", ws))
    except (networks.NetError, atproto.BskyError) as e:
        raise SyncError(str(e))
    except Exception as e:
        from . import posting
        if isinstance(e, posting.PostError):
            raise SyncError(str(e))
        raise
    if platform == "tiktok":
        if not (s.get("tt_client_key") and s.get("tt_client_secret")):
            raise SyncError("Вставь Client key и Client secret приложения TikTok")
        from .posting import TT_PUBLISH_SCOPES
        scopes = TT_SCOPES + (",user.info.stats" if s.get("tt_stats") == "1" else "") + \
            ("," + TT_PUBLISH_SCOPES if s.get("tt_publish") == "1" else "")
        return TT_AUTH + "?" + urlencode({"client_key": s["tt_client_key"], "response_type": "code", "scope": scopes,
                                          "redirect_uri": redirect_uri("tiktok"), "state": make_state("tiktok", ws),
                                          "disable_auto_auth": 1})   # всегда спрашивать — можно войти другим аккаунтом
    if platform == "instagram":
        if not (s.get("ig_app_id") and s.get("ig_app_secret")):
            raise SyncError("Вставь App ID и App secret приложения Instagram")
        return IG_AUTH + "?" + urlencode({"client_id": s["ig_app_id"], "redirect_uri": redirect_uri("instagram"),
                                          "response_type": "code", "scope": IG_SCOPES, "state": make_state("instagram", ws),
                                          "force_reauth": "true"})
    if platform == "patreon":
        if not (s.get("pt_client_id") and s.get("pt_client_secret")):
            raise SyncError("Вставь Client ID и Client Secret клиента Patreon")
        return PT_AUTH + "?" + urlencode({"response_type": "code", "client_id": s["pt_client_id"],
                                          "redirect_uri": redirect_uri("patreon"), "scope": PT_SCOPES,
                                          "state": make_state("patreon", ws)})
    raise SyncError("Неизвестная соцсеть")


async def patreon_exchange(db, http, code: str, redirect: str) -> dict:
    s = await dbm.get_settings(db)
    r = await http("POST", PT_TOKEN, data={"code": code, "grant_type": "authorization_code", "client_id": s["pt_client_id"],
                                            "client_secret": s["pt_client_secret"], "redirect_uri": redirect},
                   headers=dict(PT_UA, **{"Content-Type": "application/x-www-form-urlencoded"}))
    _pt_err(r)
    if not r.get("access_token"):
        raise SyncError("Patreon не выдал доступ — попробуй войти ещё раз")
    return await add_account(db, "pt", name="Patreon", data={"access": r["access_token"], "refresh": r.get("refresh_token", ""),
                                                             "expires": dbm.now() + int(r.get("expires_in") or 30 * 86400)})


async def oauth_callback(db, http, platform: str, code: str, state: str, iss: str = "", extra: dict | None = None) -> dict:
    """Соцсеть вернула человека с кодом — меняем на токен и сразу загружаем ролики."""
    from . import networks, atproto, posting
    if not check_state(platform, state or ""):
        raise SyncError("Ссылка входа устарела — нажми кнопку входа в CRM ещё раз")
    s = await dbm.get_settings(db)
    px = proxy_url(s)
    try:
        if platform == "pinterest":
            acc = await networks.pn_exchange(db, s, code, px)
        elif platform == "discord":
            acc = await networks.dc_exchange(db, s, code, px)
        elif platform == "subscribestar":
            acc = await networks.ss_exchange(db, s, code, px)
        elif platform == "bluesky":
            acc = await atproto.oauth_finish(db, code, state, iss, px)
        elif platform == "youtube":
            acc = await posting.yt_exchange(db, s, code, px)
        elif platform == "vkid":
            acc = await posting.vk_exchange(db, s, code, state, str((extra or {}).get("device_id") or ""), px)
        elif platform == "x":
            acc = await posting.x_exchange(db, s, code, state, px)
        else:
            acc = None
    except (networks.NetError, atproto.BskyError, posting.PostError) as e:
        raise SyncError(str(e))
    if acc:
        res = await sync_account(db, http, acc)
        acc = await get_account(db, acc["id"]) or acc
        return {"name": acc.get("name", ""), "status": res, "count": len(await accounts(db, acc["kind"])),
                "can_publish": posting.can_publish(acc)}
    if platform == "tiktok":
        acc = await tiktok_exchange(db, http, code, redirect_uri("tiktok"))
    elif platform == "patreon":
        acc = await patreon_exchange(db, http, code, redirect_uri("patreon"))
    else:
        acc = await instagram_exchange(db, http, code, redirect_uri("instagram"))
    res = await sync_account(db, http, acc)
    acc = await get_account(db, acc["id"]) or acc
    n = len(await accounts(db, acc["kind"]))
    from .posting import can_publish
    return {"name": acc.get("name", ""), "status": res, "count": n, "can_publish": can_publish(acc)}


# ------------------------------------------------------------------ Запуск
def proxy_url(s: dict) -> str | None:
    return (s.get("social_proxy") or cfg.social_proxy or "").strip() or None


def http_client(proxy: str | None = None):
    """http(method, url, params=, data=, json=, headers=) -> dict. Через прокси — если задан."""
    import aiohttp

    async def http(method, url, params=None, data=None, json=None, headers=None):
        async with aiohttp.ClientSession() as sess:
            async with sess.request(method, url, params={k: str(v) for k, v in params.items()} if params else None,
                                    data=data, json=json, headers=headers, proxy=proxy,
                                    timeout=aiohttp.ClientTimeout(total=40)) as r:
                try:
                    return await r.json(content_type=None)
                except Exception:
                    raise SyncError(f"Сервер ответил {r.status}, а не данными — возможно, сайт недоступен с хостинга (поможет прокси)")
    return http


async def _run(db, acc: dict, coro):
    from . import networks, atproto
    try:
        res = await coro
        res["at"] = dbm.now()
    except (SyncError, networks.NetError, atproto.BskyError) as e:
        res = {"ok": False, "error": str(e), "at": dbm.now()}
    except Exception as e:
        from . import posting
        if not isinstance(e, posting.PostError):
            raise
        res = {"ok": False, "error": str(e), "at": dbm.now()}
    except Exception as e:
        log.exception("Синхронизация %s #%s", acc.get("kind"), acc.get("id"))
        res = {"ok": False, "error": f"Не удалось связаться ({type(e).__name__}) — проверю ещё раз через 3 часа. "
                                     f"Если повторяется — сайт недоступен с хостинга, поможет прокси", "at": dbm.now()}
    if acc.get("id"):
        await db.execute("UPDATE accounts SET status=? WHERE id=?", (json.dumps(res, ensure_ascii=False), acc["id"]))
        if res.get("ok"):
            await save_account(db, acc)          # синхронизация узнала имя и id канала
            await _dedupe(db, acc)
        await db.commit()
    return res


async def sync_account(db, http, acc: dict, s: dict | None = None) -> dict:
    s = s or await dbm.get_settings(db)

    async def fetch(url, params):
        return await http("GET", url, params=params)
    k = acc["kind"]
    if k == "yt":
        key = s.get("yt_api_key") or cfg.yt_api_key
        # вход через Google надёжнее ключа: своим токеном (или токеном другого подключённого канала — для публичных данных
        # этого достаточно) читаем статистику, даже если ключа нет или он неверный
        tok_acc = acc if acc["data"].get("g_refresh") else next(
            (a for a in await accounts(db, "yt") if a["data"].get("g_refresh")), None)
        if tok_acc:
            from . import posting
            from . import net as _net

            async def gfetch(url, params):
                tok = await posting.google_token(db, tok_acc, s, proxy_url(s))
                return (await _net.request("GET", url, params=params, headers={"Authorization": "Bearer " + tok},
                                           proxy=proxy_url(s))).json()

            async def _yt_token_first():
                try:
                    return await youtube_sync(db, gfetch, "", acc)
                except Exception as e1:
                    if not key:
                        raise
                    try:
                        return await youtube_sync(db, fetch, key, acc)      # вход не сработал — пробуем ключ
                    except Exception as e2:
                        why = str(e1).replace("для этого ключа", "в проекте, где создан вход Google (Client ID)")
                        raise SyncError(f"Через вход Google: {why}. Через ключ: {e2}")
            coro = _yt_token_first()
        else:
            coro = youtube_sync(db, fetch, key, acc) if key else _fail("Нет ключа YouTube API — вставь его один раз выше "
                                                                       "или нажми «Разрешить публикацию» (вход через Google)")
    elif k == "vk":
        if s.get("vk_token"):
            coro = vk_sync(db, fetch, s["vk_token"], acc)
        elif acc["data"].get("vk_access"):
            async def _vk_user():
                from . import posting
                return await vk_sync(db, fetch, await posting.vk_token(db, acc, s, proxy_url(s)), acc)
            coro = _vk_user()
        else:
            coro = _fail("Нет сервисного ключа VK — вставь его выше или нажми «Разрешить публикацию» (вход через VK ID)")
    elif k == "tt":
        coro = tiktok_sync(db, http, acc)
    elif k == "ig":
        coro = instagram_sync(db, http, acc)
    elif k == "tg":
        coro = tg_sync(db, acc, proxy=proxy_url(s))
    elif k in ("bs", "pn", "dc", "fa", "e6", "ss"):
        from . import networks, atproto
        px = proxy_url(s)
        coro = {"bs": lambda: atproto.sync(db, acc, px), "pn": lambda: networks.pn_sync(db, acc, s, px),
                "dc": lambda: networks.dc_sync(db, acc, px), "fa": lambda: networks.fa_sync(db, acc, s, px),
                "e6": lambda: networks.e6_sync(db, acc, px), "ss": lambda: networks.ss_sync(db, acc, s, px)}[k]()
    elif k == "x":
        if s.get("x_bearer"):
            coro = x_sync(db, http, s["x_bearer"], acc, int(s.get("x_max_posts") or 40))
        elif acc["data"].get("x_access"):
            async def _x_user():
                from . import posting
                return await x_sync(db, http, await posting.x_token(db, acc, s, proxy_url(s)), acc, int(s.get("x_max_posts") or 40))
            coro = _x_user()
        else:
            coro = _fail("Нет Bearer Token X — вставь его выше или нажми «Разрешить публикацию» (вход через X)")
    else:
        coro = patreon_sync(db, http, acc)
    return await _run(db, acc, coro)


async def _fail(msg: str):
    raise SyncError(msg)


async def sync_all(db, http=None, force: bool = False, only=None) -> dict:
    """Синхронизировать все аккаунты (или only='yt'/'vk'/… — одну соцсеть, only=<id> — один аккаунт).
    Без force — не чаще раза в 3 часа."""
    s = await dbm.get_settings(db)
    if not force and dbm.now() - int(s.get("social_sync_at") or 0) < SYNC_EVERY:
        return {}
    if not only:
        await dbm.set_setting(db, "social_sync_at", dbm.now())
    http = http or http_client(proxy_url(s))
    out = {}
    for acc in await accounts(db):
        if only and only not in (acc["kind"], acc["id"], str(acc["id"])):
            continue
        out[acc["id"]] = await sync_account(db, http, acc, s)
    return out


async def status(db) -> dict:
    s = await dbm.get_settings(db)
    accs = await accounts(db)

    def lst(kind):
        out = []
        for a in accs:
            if a["kind"] != kind:
                continue
            d = a["data"]
            from .posting import can_publish
            row = {"id": a["id"], "name": a["name"] or KIND_NAME[kind], "ref": a["ref"] or "", "hidden": bool(a["hidden"]),
                   "can_publish": can_publish(a),
                   "status": a["status"], "expires": int(d.get("expires") or 0), "publish": d.get("publish", True) is not False,
                   "connected": bool(d.get("access") or d.get("token") or d.get("webhook") or d.get("api_key") or a["ref"])}
            if kind == "pn":
                row.update(boards=d.get("boards") or [], board_id=d.get("board_id") or "")
            if kind == "dc":
                row.update(adult=bool(d.get("adult")), invite=d.get("invite") or "")
            if kind == "e6":
                row.update(artist=d.get("artist") or "")
            if kind == "fa":
                row.update(cookie_set=bool(d.get("cookie")))
            if kind == "bs":
                row.update(auth=d.get("auth") or "")
            out.append(row)
        return out
    key = s.get("yt_api_key") or cfg.yt_api_key
    return {
        "yt": {"key_set": bool(key), "key_tail": key[-4:] if key and not s.get("yt_api_key__shared") else "",
               "shared": bool(s.get("yt_api_key__shared")), "accounts": lst("yt"),
               "client_id": "" if s.get("yt_client_id__shared") else s.get("yt_client_id", ""), "secret_set": bool(s.get("yt_client_secret")),
               "app_shared": bool(s.get("yt_client_id__shared")), "redirect_uri": redirect_uri("youtube") if cfg.public_url else ""},
        "vk": {"token_set": bool(s.get("vk_token")), "shared": bool(s.get("vk_token__shared")), "accounts": lst("vk"),
               "app_id": "" if s.get("vk_app_id__shared") else s.get("vk_app_id", ""), "secret_set": bool(s.get("vk_app_secret")),
               "app_shared": bool(s.get("vk_app_id__shared")), "redirect_uri": redirect_uri("vkid") if cfg.public_url else ""},
        "tt": {"client_key": "" if s.get("tt_client_key__shared") else s.get("tt_client_key", ""), "secret_set": bool(s.get("tt_client_secret")),
               "shared": bool(s.get("tt_client_key__shared")), "accounts": lst("tt"), "publish": s.get("tt_publish") == "1",
               "stats": s.get("tt_stats") == "1",
               "redirect_uri": redirect_uri("tiktok") if cfg.public_url else ""},
        "ig": {"app_id": "" if s.get("ig_app_id__shared") else s.get("ig_app_id", ""), "secret_set": bool(s.get("ig_app_secret")),
               "shared": bool(s.get("ig_app_id__shared")), "accounts": lst("ig"),
               "redirect_uri": redirect_uri("instagram") if cfg.public_url else ""},
        "tg": {"accounts": lst("tg"), "post_channel": s.get("post_channel", ""), "post_channel_adult": s.get("post_channel_adult", ""),
               "bot_channels": [{"id": k, "title": v.get("title") or k, "username": v.get("username") or "",
                                 "added": any(a["kind"] == "tg" and str(a.get("ext")) == k for a in accs)}
                                for k, v in (await visible_bot_channels(db)).items()]},
        "bs": {"accounts": lst("bs"), "oauth_ready": cfg.public_url.startswith("https://"),
               "redirect_uri": f"{cfg.public_url}/oauth/bluesky" if cfg.public_url else ""},
        "pn": {"client_id": "" if s.get("pn_client_id__shared") else s.get("pn_client_id", ""), "secret_set": bool(s.get("pn_client_secret")),
               "shared": bool(s.get("pn_client_id__shared")), "standard": s.get("pn_standard") == "1", "accounts": lst("pn"),
               "redirect_uri": redirect_uri("pinterest") if cfg.public_url else ""},
        "dc": {"client_id": "" if s.get("dc_client_id__shared") else s.get("dc_client_id", ""), "secret_set": bool(s.get("dc_client_secret")),
               "shared": bool(s.get("dc_client_id__shared")), "accounts": lst("dc"),
               "redirect_uri": redirect_uri("discord") if cfg.public_url else ""},
        "fa": {"accounts": lst("fa"), "faexport": s.get("faexport_url", "")},
        "e6": {"accounts": lst("e6")},
        "ss": {"client_id": "" if s.get("ss_client_id__shared") else s.get("ss_client_id", ""), "secret_set": bool(s.get("ss_client_secret")),
               "shared": bool(s.get("ss_client_id__shared")), "domain": s.get("ss_domain") or "com",
               "scopes": s.get("ss_scopes") or "content_provider_profile.read", "accounts": lst("ss"),
               "redirect_uri": redirect_uri("subscribestar") if cfg.public_url else ""},
        "x": {"key_set": bool(s.get("x_bearer")), "max_posts": int(s.get("x_max_posts") or 40),
              "shared": bool(s.get("x_bearer__shared")), "accounts": lst("x"),
              "client_id": "" if s.get("x_client_id__shared") else s.get("x_client_id", ""), "secret_set": bool(s.get("x_client_secret")),
              "app_shared": bool(s.get("x_client_id__shared")), "redirect_uri": redirect_uri("x") if cfg.public_url else ""},
        "pt": {"client_id": "" if s.get("pt_client_id__shared") else s.get("pt_client_id", ""), "secret_set": bool(s.get("pt_client_secret")),
               "shared": bool(s.get("pt_client_id__shared")), "accounts": lst("pt"),
               "oauth_ready": bool(s.get("pt_client_id") and s.get("pt_client_secret") and cfg.public_url),
               "redirect_uri": redirect_uri("patreon") if cfg.public_url else ""},
        "manual_tg": s.get("manual_tg") == "1",
        "proxy": bool(proxy_url(s)), "proxy_shared": bool(s.get("social_proxy__shared")), "public_url": cfg.public_url,
        "next_at": int(s.get("social_sync_at") or 0) + SYNC_EVERY,
        "any_hidden": any(a["hidden"] for a in accs), "all_hidden": bool(accs) and all(a["hidden"] for a in accs),
        "total": len(accs),
    }
