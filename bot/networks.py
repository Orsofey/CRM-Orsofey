"""Pinterest, Discord, Fur Affinity, e621, SubscribeStar: подключение, статистика и (где есть API) публикация.

Pinterest — вход одной кнопкой (OAuth), пины с картинкой/видео/каруселью, показы и сохранения.
Discord — одной кнопкой (OAuth «вебхук»: человек сам выбирает сервер и канал) или ссылкой вебхука; посты с файлами.
Fur Affinity — у сайта нет API: статистика через FAExport (открытый сервис), публикация — вручную (CRM готовит всё).
e621 — имя + API-ключ (OAuth у сайта нет); загрузка работ с тегами и рейтингом, избранное и комментарии.
SubscribeStar — одной кнопкой (OAuth); подписчики по API, посты — вручную (API публикации у сайта нет).
"""
import base64, datetime as dt, json, logging, re, secrets
from urllib.parse import urlencode
from . import db as dbm, net
from .config import cfg

log = logging.getLogger(__name__)


class NetError(Exception):
    pass


def _redirect(platform: str) -> str:
    return f"{cfg.public_url}/oauth/{platform}"


def _int(v) -> int:
    try:
        return int(float(str(v).replace(",", "").replace(" ", "")))
    except (TypeError, ValueError):
        return 0


async def _old_views(db, acc_id) -> dict:
    cur = await db.execute("SELECT ext_id, views, likes, comments FROM content WHERE account IS ?", (acc_id,))
    return {r[0]: (r[1] or 0, r[2] or 0, r[3] or 0) for r in await cur.fetchall()}


# ================================================================== Pinterest
PN_AUTH = "https://www.pinterest.com/oauth/"
PN_API = "https://api.pinterest.com/v5"
PN_SANDBOX = "https://api-sandbox.pinterest.com/v5"
PN_SCOPES = "boards:read,boards:write,pins:read,pins:write,user_accounts:read"


def pn_auth_url(s: dict, state: str) -> str:
    if not (s.get("pn_client_id") and s.get("pn_client_secret")):
        raise NetError("Вставь App ID и App secret приложения Pinterest")
    return PN_AUTH + "?" + urlencode({"client_id": s["pn_client_id"], "redirect_uri": _redirect("pinterest"), "response_type": "code",
                                      "scope": PN_SCOPES, "state": state})


def _pn_basic(s: dict) -> dict:
    tok = base64.b64encode(f"{s['pn_client_id']}:{s['pn_client_secret']}".encode()).decode()
    return {"Authorization": "Basic " + tok, "Content-Type": "application/x-www-form-urlencoded"}


def _pn_err(r: net.Resp, what: str = ""):
    j = r.json() or {}
    msg = j.get("message") or j.get("error_description") or j.get("error") or r.status
    if r.status == 401:
        raise NetError("Pinterest: вход устарел — нажми «Войти через Pinterest» ещё раз")
    raise NetError(f"Pinterest{(' (' + what + ')') if what else ''}: {msg}"[:220])


async def pn_exchange(db, s: dict, code: str, proxy=None) -> dict:
    from . import social
    r = await net.request("POST", PN_API + "/oauth/token", proxy=proxy, headers=_pn_basic(s),
                          data={"grant_type": "authorization_code", "code": code, "redirect_uri": _redirect("pinterest")})
    if not r.ok:
        _pn_err(r, "вход")
    t = r.json()
    data = {"access": t["access_token"], "refresh": t.get("refresh_token", ""), "expires": dbm.now() + int(t.get("expires_in") or 2592000)}
    u = await net.request("GET", PN_API + "/user_account", headers={"Authorization": "Bearer " + data["access"]}, proxy=proxy)
    name = (u.json() or {}).get("username", "") if u.ok else ""
    return await social.add_account(db, "pn", name=name or "Pinterest", data=data, ext=name)


async def _pn_token(db, acc: dict, s: dict, proxy=None) -> str:
    from . import social
    d = acc["data"]
    if int(d.get("expires") or 0) < dbm.now() + 86400 and d.get("refresh") and s.get("pn_client_id"):
        r = await net.request("POST", PN_API + "/oauth/token", proxy=proxy, headers=_pn_basic(s),
                              data={"grant_type": "refresh_token", "refresh_token": d["refresh"]})
        if r.ok:
            t = r.json()
            d["access"] = t["access_token"]
            d["refresh"] = t.get("refresh_token", d["refresh"])
            d["expires"] = dbm.now() + int(t.get("expires_in") or 2592000)
            await social.save_account(db, acc)
    return d["access"]


async def pn_sync(db, acc: dict, s: dict, proxy=None, max_pins: int = 200, with_stats: int = 25) -> dict:
    from . import social
    h = {"Authorization": "Bearer " + await _pn_token(db, acc, s, proxy)}
    u = await net.request("GET", PN_API + "/user_account", headers=h, proxy=proxy)
    if not u.ok:
        _pn_err(u)
    ua = u.json()
    b = await net.request("GET", PN_API + "/boards", params={"page_size": 100}, headers=h, proxy=proxy)
    if b.ok:
        acc["data"]["boards"] = [{"id": x["id"], "name": x.get("name", "")} for x in (b.json() or {}).get("items", [])]
    pins, book = [], None
    while len(pins) < max_pins:
        p = {"page_size": 100}
        if book:
            p["bookmark"] = book
        r = await net.request("GET", PN_API + "/pins", params=p, headers=h, proxy=proxy)
        if not r.ok:
            _pn_err(r)
        j = r.json() or {}
        pins += j.get("items", [])
        book = j.get("bookmark")
        if not book:
            break
    old = await _old_views(db, acc["id"])
    end = dt.date.today()
    start = end - dt.timedelta(days=89)
    items = []
    for i, pin in enumerate(pins):
        ext = "pn:" + str(pin["id"])
        views, saves, _ = old.get(ext, (0, 0, 0))
        if i < with_stats:
            a = await net.request("GET", PN_API + f"/pins/{pin['id']}/analytics", headers=h, proxy=proxy,
                                  params={"start_date": start.isoformat(), "end_date": end.isoformat(),
                                          "metric_types": "IMPRESSION,SAVE,PIN_CLICK,OUTBOUND_CLICK"})
            sm = ((a.json() or {}).get("all") or {}).get("summary_metrics") or {} if a.ok else {}
            if sm:
                views, saves = max(views, _int(sm.get("IMPRESSION"))), max(saves, _int(sm.get("SAVE")))
        text = f"{pin.get('title') or ''} {pin.get('description') or ''} {pin.get('link') or ''}"
        items.append({"ext_id": ext, "vid": str(pin["id"]), "platform": "Pinterest", "kind": "Пин",
                      "title": (pin.get("title") or pin.get("description") or "Пин").split("\n")[0][:120],
                      "codes": social.codes_in(text), "text": text, "url": f"https://www.pinterest.com/pin/{pin['id']}/",
                      "views": views, "likes": saves, "comments": 0, "published_at": social._iso_ts(pin.get("created_at"))})
    social.assign_codes(items, lambda it: "pn_" + it["vid"], await social.keyword_map(db))
    await social.upsert_items(db, items, "pinterest", acc["id"])
    await social.record_channel(db, "Pinterest", ua.get("follower_count"), ua.get("monthly_views"), account=acc["id"])
    acc["name"] = ua.get("username") or acc.get("name")
    acc["ext"] = acc.get("ext") or acc["name"]
    return {"ok": True, "title": acc["name"], "subscribers": ua.get("follower_count"), "videos": len(items),
            "linked": sum(1 for it in items if not it.get("default")), "monthly_views": ua.get("monthly_views")}


async def pn_publish(db, acc: dict, t: dict, state: dict, s: dict, proxy=None) -> dict:
    from . import social, publish
    base = PN_API if s.get("pn_standard") == "1" else PN_SANDBOX
    h = {"Authorization": "Bearer " + await _pn_token(db, acc, s, proxy)}
    board = acc["data"].get("board_id")
    if not board:
        r = await net.request("GET", base + "/boards", params={"page_size": 25}, headers=h, proxy=proxy)
        items = (r.json() or {}).get("items", []) if r.ok else []
        if items:
            board = items[0]["id"]
        else:
            r = await net.request("POST", base + "/boards", json={"name": "Orsofey"}, headers=h, proxy=proxy)
            if not r.ok:
                _pn_err(r, "доска")
            board = r.json()["id"]
        acc["data"]["board_id"] = board
        await social.save_account(db, acc)
    cap = t["cap"]
    files = t["flist"]
    if t["ptype"] == "video":
        if not state.get("media_id"):
            r = await net.request("POST", base + "/media", json={"media_type": "video"}, headers=h, proxy=proxy)
            if not r.ok:
                _pn_err(r, "видео")
            m = r.json()
            up = await net.request("POST", m["upload_url"], data=m.get("upload_parameters") or {}, proxy=proxy, timeout=600,
                                   files=[("file", files[0]["name"], files[0]["path"], "video/mp4")])
            if up.status >= 300:
                raise NetError(f"Pinterest не принял видео ({up.status})")
            state.update(media_id=m["media_id"], started=dbm.now())
            return {"wait": True}
        r = await net.request("GET", base + f"/media/{state['media_id']}", headers=h, proxy=proxy)
        st = (r.json() or {}).get("status")
        if st == "failed":
            raise NetError("Pinterest не обработал видео — нужен MP4 от 4 секунд")
        if st != "succeeded":
            if dbm.now() - state.get("started", dbm.now()) > 40 * 60:
                raise NetError("Pinterest слишком долго обрабатывает видео")
            return {"wait": True}
        media = {"source_type": "video_id", "media_id": state["media_id"], "cover_image_key_frame_time": 1}
    elif len(files) > 1:
        media = {"source_type": "multiple_image_urls", "items": [{"url": publish.file_url(f)} for f in files[:5]]}
    else:
        media = {"source_type": "image_url", "url": publish.file_url(files[0])}
    body = {"board_id": board, "title": (cap.get("title") or "")[:100], "description": (cap.get("text") or "")[:500],
            "media_source": media}
    if cap.get("link"):
        body["link"] = cap["link"]
    r = await net.request("POST", base + "/pins", json=body, headers=h, proxy=proxy)
    if not r.ok:
        _pn_err(r, "пин")
    pid = r.json()["id"]
    return {"url": f"https://www.pinterest.com/pin/{pid}/", "ext": f"pn:{pid}",
            "note": "" if base == PN_API else "пробный доступ Pinterest: пин виден только тебе, пока приложение не одобрено"}


# ================================================================== Discord
DC_AUTH = "https://discord.com/oauth2/authorize"
DC_TOKEN = "https://discord.com/api/oauth2/token"
DC_API = "https://discord.com/api/v10"
DC_FILE_MAX = 10 * 1024 * 1024       # вебхук без бустов сервера — до 10 МБ на файл
WEBHOOK_RE = re.compile(r"https://(?:ptb\.|canary\.)?discord(?:app)?\.com/api(?:/v\d+)?/webhooks/(\d+)/([\w-]+)")


def dc_auth_url(s: dict, state: str) -> str:
    if not (s.get("dc_client_id") and s.get("dc_client_secret")):
        raise NetError("Вставь Client ID и Client Secret приложения Discord")
    return DC_AUTH + "?" + urlencode({"client_id": s["dc_client_id"], "response_type": "code", "scope": "webhook.incoming",
                                      "redirect_uri": _redirect("discord"), "state": state})


async def _dc_add(db, wh: dict, url: str) -> dict:
    from . import social
    data = {"webhook": url, "channel_id": str(wh.get("channel_id") or ""), "guild_id": str(wh.get("guild_id") or ""), "adult": False}
    name = f"Discord · канал …{data['channel_id'][-4:]}" if data["channel_id"] else "Discord"
    return await social.add_account(db, "dc", name=name, data=data, ext=str(wh.get("id") or ""))


async def dc_exchange(db, s: dict, code: str, proxy=None) -> dict:
    r = await net.request("POST", DC_TOKEN, proxy=proxy, headers={"Content-Type": "application/x-www-form-urlencoded"},
                          data={"client_id": s["dc_client_id"], "client_secret": s["dc_client_secret"], "grant_type": "authorization_code",
                                "code": code, "redirect_uri": _redirect("discord")})
    j = r.json() or {}
    if not r.ok or not j.get("webhook"):
        raise NetError(f"Discord: {j.get('error_description') or j.get('error') or r.status}")
    wh = j["webhook"]
    return await _dc_add(db, wh, wh.get("url") or f"https://discord.com/api/webhooks/{wh['id']}/{wh['token']}")


async def dc_add_url(db, url: str, proxy=None) -> dict:
    m = WEBHOOK_RE.search(url or "")
    if not m:
        raise NetError("Это не ссылка вебхука. В Discord: настройки канала → Интеграции → Вебхуки → «Копировать URL»")
    url = f"https://discord.com/api/webhooks/{m.group(1)}/{m.group(2)}"
    r = await net.request("GET", url, proxy=proxy)
    if not r.ok:
        raise NetError("Discord не нашёл этот вебхук — скопируй ссылку заново")
    return await _dc_add(db, r.json() or {}, url)


async def dc_sync(db, acc: dict, proxy=None) -> dict:
    from . import social
    d = acc["data"]
    members, title = None, acc.get("name")
    inv = re.sub(r".*/", "", (d.get("invite") or "").strip().rstrip("/"))
    if inv:
        r = await net.request("GET", f"{DC_API}/invites/{inv}", params={"with_counts": "true"}, proxy=proxy)
        if r.ok:
            j = r.json() or {}
            members = j.get("approximate_member_count")
            g = (j.get("guild") or {}).get("name")
            if g and not d.get("custom_name"):
                title = g
    # реакции на наши посты (вебхук может читать только свои сообщения)
    cur = await db.execute("SELECT code, ext_id, title, url, views, published_at FROM content WHERE account=? AND ext_id LIKE 'dc:%'", (acc["id"],))
    items = []
    for code, ext, ttl, url, views, pub in await cur.fetchall():
        r = await net.request("GET", f"{d['webhook']}/messages/{ext[3:]}", proxy=proxy)
        if not r.ok:
            continue
        reacts = sum(_int(x.get("count")) for x in (r.json() or {}).get("reactions") or [])
        items.append({"ext_id": ext, "vid": ext[3:], "platform": "Discord", "kind": "Пост", "title": ttl or "Пост", "codes": [code],
                      "text": "", "url": url or "", "views": views or 0, "likes": reacts, "comments": 0, "published_at": pub, "code": code})
    if items:
        await social.upsert_items(db, items, "discord", acc["id"])
    await social.record_channel(db, "Discord", members, account=acc["id"])
    acc["name"] = title
    return {"ok": True, "title": title, "subscribers": members, "videos": len(items), "linked": len(items),
            "note": "" if inv else "Вставь ссылку-приглашение на сервер в ⚙ — будет видно число участников"}


async def dc_publish(db, acc: dict, t: dict, state: dict, proxy=None) -> dict:
    from . import publish
    d = acc["data"]
    adult = t.get("adult", "")
    if adult and not d.get("adult"):
        raise NetError("Канал Discord не отмечен как 18+ — пост 18+ туда не отправлен (в ⚙ аккаунта включи «канал 18+»)")
    text = (t["cap"].get("text") or "")[:2000]
    files, links = [], []
    for i, f in enumerate(t["flist"][:10]):
        if (f.get("size") or 0) > DC_FILE_MAX:
            links.append(publish.file_url(f))
            continue
        name = ("SPOILER_" if adult else "") + (f.get("name") or f"file{i}")
        files.append((f"files[{i}]", name, f["path"], "application/octet-stream"))
    if links:
        text = (text + "\n" + "\n".join(links))[:2000]
    payload = {"content": text, "allowed_mentions": {"parse": []}}
    r = await net.request("POST", d["webhook"], params={"wait": "true"}, proxy=proxy, timeout=300,
                          data={"payload_json": json.dumps(payload, ensure_ascii=False)}, files=files or None) if files else \
        await net.request("POST", d["webhook"], params={"wait": "true"}, json=payload, proxy=proxy)
    j = r.json() or {}
    if not r.ok:
        if r.status == 404:
            raise NetError("Discord: вебхук удалён — подключи канал заново")
        raise NetError(f"Discord не принял пост: {j.get('message') or r.status}")
    mid = j.get("id")
    return {"url": f"https://discord.com/channels/{d.get('guild_id') or j.get('guild_id', '@me')}/{j.get('channel_id') or d.get('channel_id')}/{mid}",
            "ext": f"dc:{mid}"}


# ================================================================== Fur Affinity (через FAExport)
FAX = "https://faexport.spangle.org.uk"


def parse_fa_user(s: str) -> str:
    m = re.search(r"furaffinity\.net/(?:user|gallery|scraps)/([\w.~\-]+)", s or "")
    return (m.group(1) if m else (s or "").strip().lstrip("@").split("/")[0]).lower()


FA = "https://www.furaffinity.net"
FA_UA = "Mozilla/5.0 (compatible; OrsofeyCRM/1.0; +stats for the account owner)"


class _FANotFound(Exception):
    pass


async def _fa_via_faexport(base: str, name: str, cookie: str, proxy, with_stats: int) -> dict:
    h = {"Accept": "application/json"}
    if cookie:
        h["FA_COOKIE"] = cookie
    r = await net.request("GET", f"{base}/user/{name}.json", headers=h, proxy=proxy)
    if r.status == 404:
        raise _FANotFound()
    if not r.ok:
        j = r.json() if isinstance(r.json(), dict) else {}
        raise NetError(f"FAExport ответил {r.status}" + (f": {str(j.get('error'))[:70]}…" if j.get("error") else ""))
    u = r.json() or {}
    w = u.get("watchers")
    out = {"name": u.get("full_name") or u.get("name") or name,
           "watchers": _int(w.get("count")) if isinstance(w, dict) else (_int(w) if w is not None else None), "subs": []}
    g = await net.request("GET", f"{base}/user/{name}/gallery.json", params={"full": 1, "page": 1}, headers=h, proxy=proxy)
    for i, x in enumerate(g.json() if g.ok and isinstance(g.json(), list) else []):
        sid = str(x.get("id") if isinstance(x, dict) else x)
        it = {"id": sid, "title": (x.get("title") if isinstance(x, dict) else "") or ""}
        if i < with_stats:
            d = await net.request("GET", f"{base}/submission/{sid}.json", headers=h, proxy=proxy)
            if d.ok:
                j = d.json() or {}
                raw = j.get("description") or ""
                it.update(views=_int(j.get("views")), favs=_int(j.get("favorites")), comments=_int(j.get("comments")),
                          title=j.get("title") or it["title"], pub=j.get("posted_at"),
                          desc=re.sub(r"<[^>]+>", " ", raw) + " " + " ".join(re.findall(r"""href=["']([^"']+)""", raw)))
        out["subs"].append(it)
    return out


def _fa_text(html: str) -> str:
    import html as _h
    return _h.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html or "")))


def _fa_need_login(html: str) -> bool:
    t = (html or "").lower()
    return "registered users only" in t or "log in to view" in t or "you must be logged in" in t


async def _fa_get(url: str, cookie: str, proxy) -> net.Resp:
    h = {"User-Agent": FA_UA, "Accept": "text/html"}
    if cookie:
        h["Cookie"] = cookie
    return await net.request("GET", url, headers=h, proxy=proxy, timeout=40)


async def _fa_direct(name: str, cookie: str, proxy, with_stats: int) -> dict:
    """Запасной путь — сама страница Fur Affinity (с нашего сервера, через прокси, если задан)."""
    r = await _fa_get(f"{FA}/user/{name}/", cookie, proxy)
    if r.status == 404 or "cannot be found" in r.text.lower() or "user not found" in r.text.lower():
        raise _FANotFound()
    if r.status in (403, 503) and ("cloudflare" in r.text.lower() or "challenge" in r.text.lower()):
        raise NetError("Fur Affinity закрыт проверкой Cloudflare для сервера")
    if not r.ok:
        raise NetError(f"Fur Affinity ответил {r.status}")
    if _fa_need_login(r.text):
        raise NetError("профиль виден только вошедшим — вставь cookie в ⚙")
    t = _fa_text(r.text)
    m = re.search(r"Watched by\D{0,40}?(\d[\d,]*)", t)
    nm = re.search(r"Userpage of (.+?) -- Fur Affinity", r.text)
    out = {"name": (nm.group(1).strip() if nm else name), "watchers": _int(m.group(1)) if m else None, "subs": []}
    g = await _fa_get(f"{FA}/gallery/{name}/", cookie, proxy)
    seen = []
    if g.ok:
        titles = {sid: ttl for sid, ttl in re.findall(r'<a href="/view/(\d+)/"[^>]*title="([^"]*)"', g.text)}
        for sid in re.findall(r'id="sid-(\d+)"', g.text):
            if sid not in seen:
                seen.append(sid)
        for i, sid in enumerate(seen[:72]):
            it = {"id": sid, "title": titles.get(sid, "")}
            if i < with_stats:
                d = await _fa_get(f"{FA}/view/{sid}/", cookie, proxy)
                if d.ok and not _fa_need_login(d.text):
                    h, tt = d.text, _fa_text(d.text)

                    def num(modern, classic):
                        mm = re.search(r'class="%s">\s*<span class="font-large">\s*([\d,]+)' % modern, h) or \
                             re.search(r"%s:\s*([\d,]+)" % classic, tt)
                        return _int(mm.group(1)) if mm else 0
                    dm = re.search(r'data-time="(\d{9,})"', h)
                    desc_m = re.search(r'class="submission-description[^"]*"[^>]*>(.*?)</div>', h, re.S)
                    raw = desc_m.group(1) if desc_m else ""
                    tm = re.search(r'<div class="submission-title">\s*<h2[^>]*>\s*<p>(.*?)</p>', h, re.S)
                    it.update(views=num("views", "Views"), favs=num("favorites", "Favorites"), comments=num("comments", "Comments"),
                              pub=int(dm.group(1)) if dm else None, title=(_fa_text(tm.group(1)).strip() if tm else it["title"]),
                              desc=_fa_text(raw) + " " + " ".join(re.findall(r"""href=["']([^"']+)""", raw)))
            out["subs"].append(it)
    return out


async def fa_sync(db, acc: dict, s: dict, proxy=None, with_stats: int = 25) -> dict:
    """Статистика Fur Affinity: сначала через FAExport, не вышло — прямо со страниц сайта."""
    from . import social
    base = (s.get("faexport_url") or FAX).rstrip("/")
    name = parse_fa_user(acc["ref"])
    cookie = acc["data"].get("cookie") or ""
    errs, data = [], None
    for how, fn in (("FAExport", lambda: _fa_via_faexport(base, name, cookie, proxy, with_stats)),
                    ("сайт напрямую", lambda: _fa_direct(name, cookie, proxy, with_stats))):
        try:
            data = await fn()
            break
        except _FANotFound:
            raise NetError(f"Fur Affinity: пользователь «{name}» не найден — проверь ссылку на профиль")
        except NetError as e:
            errs.append(f"{how}: {e}")
        except Exception as e:
            errs.append(f"{how}: нет связи ({type(e).__name__})")
    if data is None:
        hint = "" if cookie else " Если профиль или работы видны только вошедшим — вставь cookie в ⚙."
        raise NetError("Fur Affinity: не получилось загрузить (" + "; ".join(errs) + ")." + hint +
                       (" С сервера в России сайт может быть закрыт — помогает прокси в настройках соцсетей." if not proxy else "")
                       + " Попробую снова через 3 часа.")
    old = await _old_views(db, acc["id"])
    items = []
    for x in data["subs"]:
        ext = "fa:" + x["id"]
        ov = old.get(ext, (0, 0, 0))
        has = "views" in x
        desc = x.get("desc", "")
        items.append({"ext_id": ext, "vid": x["id"], "platform": "Fur Affinity", "kind": "Работа", "title": (x.get("title") or "Работа")[:120],
                      "codes": social.codes_in(desc), "text": f"{x.get('title', '')} {desc}", "url": f"{FA}/view/{x['id']}/",
                      "views": x["views"] if has else ov[0], "likes": x.get("favs", ov[1]) if has else ov[1],
                      "comments": x.get("comments", ov[2]) if has else ov[2],
                      "published_at": (x["pub"] if isinstance(x.get("pub"), int) else social._iso_ts(x.get("pub"))) if x.get("pub") else None})
    social.assign_codes(items, lambda it: "fa_" + it["vid"], await social.keyword_map(db))
    await social.upsert_items(db, items, "furaffinity", acc["id"])
    await social.record_channel(db, "Fur Affinity", data["watchers"], account=acc["id"])
    acc["name"], acc["ext"] = data["name"] or name, name
    res = {"ok": True, "title": acc["name"], "subscribers": data["watchers"], "videos": len(items),
           "linked": sum(1 for it in items if not it.get("default"))}
    if errs:
        res["note"] = "FAExport не ответил — данные взяты прямо с сайта"
    return res


# ================================================================== e621
E6 = "https://e621.net"


def _e6_headers(d: dict) -> dict:
    tok = base64.b64encode(f"{d['user']}:{d['api_key']}".encode()).decode()
    return {"Authorization": "Basic " + tok, "User-Agent": f"OrsofeyCRM/1.0 (by {d['user']} on e621)"}


async def e6_connect(db, user: str, api_key: str, proxy=None) -> dict:
    from . import social
    user = re.sub(r".*/users?/", "", (user or "").strip()).strip("/ ")
    if not user or not api_key:
        raise NetError("Нужны имя на e621 и API-ключ (Аккаунт → Manage API Access)")
    d = {"user": user, "api_key": api_key.strip(), "artist": user.lower()}
    r = await net.request("GET", E6 + "/posts.json", params={"tags": f"user:{user}", "limit": 1}, headers=_e6_headers(d), proxy=proxy)
    if r.status in (401, 403):
        raise NetError("e621: имя или API-ключ не подходят")
    if not r.ok:
        raise NetError(f"e621 недоступен ({r.status}) — с хостинга в России может понадобиться прокси")
    return await social.add_account(db, "e6", ref=user, name=user, data=d, ext=user.lower())


async def e6_sync(db, acc: dict, proxy=None) -> dict:
    from . import social
    d = acc["data"]
    r = await net.request("GET", E6 + "/posts.json", params={"tags": f"user:{d['user']}", "limit": 200}, headers=_e6_headers(d), proxy=proxy)
    if not r.ok:
        raise NetError("e621: имя или API-ключ не подходят" if r.status in (401, 403) else f"e621 недоступен ({r.status})")
    items = []
    for p in (r.json() or {}).get("posts", []):
        text = (p.get("description") or "") + " " + " ".join(p.get("sources") or [])
        items.append({"ext_id": f"e6:{p['id']}", "vid": str(p["id"]), "platform": "e621", "kind": "Пост",
                      "title": ((p.get("description") or "").split("\n")[0] or f"Пост #{p['id']}")[:120],
                      "codes": social.codes_in(text), "text": text, "url": f"{E6}/posts/{p['id']}", "views": 0,
                      "likes": _int(p.get("fav_count")), "comments": _int(p.get("comment_count")),
                      "published_at": social._iso_ts(p.get("created_at"))})
    social.assign_codes(items, lambda it: "e621_" + it["vid"], await social.keyword_map(db))
    await social.upsert_items(db, items, "e621", acc["id"])
    return {"ok": True, "title": d["user"], "subscribers": None, "videos": len(items), "linked": sum(1 for it in items if not it.get("default"))}


def e6_rating(adult: str) -> str:
    return {"adult": "e", "mature": "q"}.get(adult or "", "s")


async def e6_publish(db, acc: dict, t: dict, state: dict, proxy=None) -> dict:
    d = acc["data"]
    tags = [x for x in re.split(r"[\s,]+", (t.get("tags") or "").lower().replace("#", "")) if x]
    artist = (d.get("artist") or d["user"]).lower().replace(" ", "_")
    if artist not in tags:
        tags.insert(0, artist)
    if len(tags) < 5:
        raise NetError("e621: добавь в «Теги» хотя бы 4 тега (вид, персонаж, что на картинке) — без них загрузку отклонят")
    done = state.setdefault("done", [])
    mime = {"gif": "image/gif", "video": "video/mp4"}.get(t["ptype"], "image/jpeg")
    for f in t["flist"][:4 if t["ptype"] == "image" else 1]:
        if f["token"] in [x["token"] for x in done]:
            continue
        data = {"upload[tag_string]": " ".join(tags), "upload[rating]": e6_rating(t.get("adult")),
                "upload[source]": t.get("source") or "", "upload[description]": (t["cap"].get("text") or "")[:5000]}
        r = await net.request("POST", E6 + "/uploads.json", data=data, headers=_e6_headers(d), proxy=proxy, timeout=600,
                              files=[("upload[file]", f["name"], f["path"], mime)])
        j = r.json() or {}
        if r.ok and j.get("success") is not False and (j.get("post_id") or j.get("location")):
            pid = j.get("post_id") or str(j.get("location", "")).rstrip("/").rsplit("/", 1)[-1]
        elif j.get("reason") == "duplicate" and j.get("post_id"):
            pid = j["post_id"]
        else:
            raise NetError(f"e621 не принял: {j.get('message') or j.get('reason') or r.status}"[:250])
        done.append({"token": f["token"], "id": str(pid)})
    first = done[0]["id"]
    return {"url": f"{E6}/posts/{first}", "ext": f"e6:{first}"}


# ================================================================== SubscribeStar
def ss_base(s: dict) -> str:
    return "https://www.subscribestar." + ("adult" if s.get("ss_domain") == "adult" else "com")


def ss_auth_url(s: dict, state: str) -> str:
    if not (s.get("ss_client_id") and s.get("ss_client_secret")):
        raise NetError("Вставь Client ID и Client Secret интеграции SubscribeStar")
    scopes = (s.get("ss_scopes") or "content_provider_profile.read").replace(",", "+").replace(" ", "+")
    return ss_base(s) + "/oauth2/authorize?" + urlencode({"client_id": s["ss_client_id"], "redirect_uri": _redirect("subscribestar"),
                                                           "response_type": "code", "state": state}) + "&scope=" + scopes


async def ss_exchange(db, s: dict, code: str, proxy=None) -> dict:
    from . import social
    r = await net.request("POST", ss_base(s) + "/oauth2/token", proxy=proxy,
                          data={"client_id": s["ss_client_id"], "client_secret": s["ss_client_secret"], "code": code,
                                "grant_type": "authorization_code", "redirect_uri": _redirect("subscribestar")})
    j = r.json() or {}
    if not r.ok or not j.get("access_token"):
        raise NetError(f"SubscribeStar: {j.get('error_description') or j.get('error') or r.status}")
    data = {"access": j["access_token"], "refresh": j.get("refresh_token", ""), "expires": dbm.now() + int(j.get("expires_in") or 2592000),
            "domain": s.get("ss_domain") or "com"}
    return await social.add_account(db, "ss", name="SubscribeStar", data=data)


async def _gql(s: dict, tok: str, query: str, proxy=None) -> dict:
    r = await net.request("POST", ss_base(s) + "/api/graphql/v1", json={"query": query}, proxy=proxy,
                          headers={"Authorization": "Bearer " + tok})
    j = r.json() or {}
    if r.status == 401:
        raise NetError("SubscribeStar: вход устарел — войди ещё раз")
    if not r.ok and not j:
        raise NetError(f"SubscribeStar недоступен ({r.status})")
    return j


def _unwrap(tp: dict) -> dict:
    while tp and tp.get("ofType"):
        tp = tp["ofType"]
    return tp or {}


async def ss_sync(db, acc: dict, s: dict, proxy=None) -> dict:
    """Документация API SubscribeStar закрыта, поэтому CRM сама смотрит схему (introspection) и берёт числовые поля
    профиля автора: подписчики, доход и т.п."""
    from . import social
    d = acc["data"]
    if int(d.get("expires") or 0) < dbm.now() + 86400 and d.get("refresh"):
        r = await net.request("POST", ss_base(s) + "/oauth2/token", proxy=proxy,
                              data={"client_id": s.get("ss_client_id"), "client_secret": s.get("ss_client_secret"),
                                    "grant_type": "refresh_token", "refresh_token": d["refresh"]})
        if r.ok and (r.json() or {}).get("access_token"):
            j = r.json()
            d.update(access=j["access_token"], refresh=j.get("refresh_token", d["refresh"]),
                     expires=dbm.now() + int(j.get("expires_in") or 2592000))
            await social.save_account(db, acc)
    sch = await _gql(s, d["access"], "{__schema{queryType{fields{name type{name kind ofType{name kind ofType{name kind}}}}}}}", proxy)
    roots = ((((sch.get("data") or {}).get("__schema") or {}).get("queryType") or {}).get("fields")) or []
    root = next((f for f in roots if "content_provider" in f["name"]), None) or next((f for f in roots if f["name"] in ("user", "me")), None)
    if not root:
        raise NetError("SubscribeStar: API не дал доступ к профилю автора — в интеграции включи право content_provider_profile.read")
    tname = _unwrap(root["type"]).get("name")
    ti = await _gql(s, d["access"], '{__type(name:"%s"){fields{name type{name kind ofType{name kind ofType{name kind}}}}}}' % tname, proxy)
    fields = (((ti.get("data") or {}).get("__type") or {}).get("fields")) or []
    scal = [f["name"] for f in fields if _unwrap(f["type"]).get("kind") == "SCALAR"][:30]
    if not scal:
        raise NetError("SubscribeStar: в профиле автора нет доступных полей")
    q = "{%s{%s}}" % (root["name"], " ".join(scal))
    res = ((await _gql(s, d["access"], q, proxy)).get("data") or {}).get(root["name"]) or {}
    num = {k: v for k, v in res.items() if isinstance(v, (int, float)) or (isinstance(v, str) and re.fullmatch(r"-?\d+(\.\d+)?", v))}
    subs_key = next((k for k in num if re.search(r"subscri.*count|subscribers|active_subscri|patron", k)), None)
    subs = _int(num[subs_key]) if subs_key else None
    title = res.get("name") or res.get("profile_name") or res.get("nickname") or acc.get("name") or "SubscribeStar"
    extra = {k: num[k] for k in list(num)[:12]}
    await social.record_channel(db, "SubscribeStar", subs, None, extra, account=acc["id"])
    acc["name"] = str(title)
    acc["ext"] = str(res.get("id") or acc.get("ext") or "")
    out = {"ok": True, "title": acc["name"], "subscribers": subs, "videos": 0, "linked": 0, "fields": extra}
    if subs is None:
        out["note"] = "API SubscribeStar не отдал число подписчиков — проверь права интеграции"
    return out
