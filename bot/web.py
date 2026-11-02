"""Веб-сервер внутри бота: панель CRM (+ приложение на телефон), API и ссылка на скачивание с учётом перехода."""
import hmac, json, logging, os, secrets, time
from aiohttp import web
from . import api, db as dbm, runtime, workspaces
from .config import cfg

log = logging.getLogger(__name__)
STATIC = os.path.join(os.path.dirname(__file__), "static")
COOKIE = "crm"
_codes: dict[str, float] = {}       # одноразовые коды входа из /panel


# ---------- Вход ----------
# Токен: «срок.пространство.подпись». Пространство main — CRM автора, u<id> — своя CRM друга.
def make_token(ttl: int, ws: str = "main") -> str:
    exp = str(int(time.time()) + ttl)
    return f"{exp}.{ws}.{runtime.sign(exp + '.' + ws, 32)}"


def check_token(tok: str | None) -> str | None:
    """Возвращает пространство, если токен верный, иначе None."""
    try:
        parts = (tok or "").split(".")
        if len(parts) == 2:                         # старые токены (до пространств) — CRM автора
            exp, sig = parts
            ok = hmac.compare_digest(sig, runtime.sign(exp, 32))
            ws = "main"
        else:
            exp, ws, sig = parts
            ok = workspaces.valid(ws) and hmac.compare_digest(sig, runtime.sign(exp + "." + ws, 32))
        return ws if ok and int(exp) > time.time() else None
    except Exception:
        return None


def issue_login_code(ws: str = "main") -> str:
    now = time.time()
    for c, (exp, _) in list(_codes.items()):
        if exp < now:
            _codes.pop(c, None)
    code = f"{secrets.randbelow(10**6):06d}"
    _codes[code] = (now + 600, ws)
    return code


def login_link(ws: str = "main") -> str:
    return f"{cfg.public_url}/login?t={make_token(3600, ws)}"


def _authed(request) -> str | None:
    ws = check_token(request.cookies.get(COOKIE))
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):                  # явный токен (приложение на телефоне) важнее куки
        ws = check_token(auth[7:]) or ws
    return ws


def _session_response(data: dict, ws: str) -> web.Response:
    tok = make_token(90 * 86400, ws)
    resp = web.json_response(dict(data, token=tok))
    resp.set_cookie(COOKIE, tok, max_age=90 * 86400, httponly=True, samesite="Lax", secure=cfg.public_url.startswith("https"))
    return resp


def build_app(db, bot) -> web.Application:
    routes = web.RouteTableDef()

    async def send(uid: int, text: str):
        await bot.send_message(uid, text)

    async def report():
        from . import crm, notify
        await notify.admins(bot, await crm.daily_report(db))

    async def notify(chat_id: int, text: str, button: str, url: str):
        from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
        await bot.send_message(chat_id, text, reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text=button, url=url)]]), disable_web_page_preview=True)

    def make_ctx(ws: str):         # новый контекст на каждый запрос — запросы разных CRM не смешиваются
        if ws == "main":
            return api.Ctx(send=send, report=report, is_main=True, ws="main", notify=notify, bot=bot)
        return api.Ctx(is_main=False, ws=ws, notify=notify)

    @routes.get("/terms")
    async def terms_page(_):
        from . import legal
        return web.Response(text=legal.terms(), content_type="text/html")

    @routes.get("/privacy")
    async def privacy_page(_):
        from . import legal
        return web.Response(text=legal.privacy(), content_type="text/html")

    @routes.get("/{name:tiktok[0-9A-Za-z_-]+\\.txt}")
    async def tiktok_verify(request):
        """Файл подтверждения домена TikTok (URL properties → URL prefix): положи скачанный tiktok….txt в bot/static/."""
        path = os.path.join(STATIC, request.match_info["name"])
        if not os.path.isfile(path):
            raise web.HTTPNotFound()
        return web.FileResponse(path, headers={"Content-Type": "text/plain"})

    @routes.get("/t/{code}")
    async def tg_redirect(request):
        """Короткая ссылка на бота через свой домен: …/t/tt_bio → t.me/бот?start=tt_bio.
        Нужна, где соцсеть не принимает ссылки t.me (например, в профиле TikTok)."""
        code = dbm.clean_code(request.match_info["code"]) or ""
        u = runtime.state.get("bot_username") or ""
        if not u:
            raise web.HTTPNotFound()
        return web.Response(status=302, headers={"Location": f"https://t.me/{u}" + (f"?start={code}" if code else ""),
                                                 "Cache-Control": "no-store"})

    @routes.get("/health")
    async def health(_):
        return web.Response(text="ok")

    @routes.get("/login")
    async def login_get(request):
        """Вход по ссылке из /panel: ставим куку и открываем панель."""
        tok = request.query.get("t")
        ws = check_token(tok)
        if not ws:
            return web.Response(status=302, headers={"Location": "/?expired=1"})
        long_tok = make_token(90 * 86400, ws)
        # токен ещё и в адресе (#t=…): панель сохранит его — на iPhone приложение с экрана «Домой» не видит куки Safari
        resp = web.Response(status=302, headers={"Location": f"/#t={long_tok}"})
        resp.set_cookie(COOKIE, long_tok, max_age=90 * 86400, httponly=True, samesite="Lax",
                        secure=cfg.public_url.startswith("https"))
        return resp

    @routes.post("/api/login")
    async def login_post(request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        code = str(body.get("code", "")).strip()
        pwd = str(body.get("password", ""))
        if code and code in _codes and _codes[code][0] > time.time():
            _, ws = _codes.pop(code)
            return _session_response({"ok": True}, ws)
        if cfg.panel_password and pwd and hmac.compare_digest(pwd, cfg.panel_password):
            return _session_response({"ok": True}, "main")
        return web.json_response({"error": "Неверный или просроченный код"}, status=401)

    @routes.post("/api/logout")
    async def logout(_):
        resp = web.json_response({"ok": True})
        resp.del_cookie(COOKIE)
        return resp

    @routes.get("/go/{uid}/{sig}")
    async def go(request):
        """Ссылка «Скачать» из бота: отмечаем переход и отправляем на страницу приложения."""
        uid, sig = request.match_info["uid"], request.match_info["sig"]
        if uid.isdigit() and hmac.compare_digest(sig, runtime.sign(uid, 10)):
            try:
                await dbm.stage(db, int(uid), "download")
            except Exception as e:
                log.warning("Не записан переход к скачиванию %s: %s", uid, e)
        return web.Response(status=302, headers={"Location": cfg.app_url or "/"})

    @routes.get("/oauth/bluesky/client-metadata.json")
    async def bs_meta(_):
        from . import atproto
        return web.json_response(atproto.client_metadata(), headers={"Cache-Control": "max-age=300"})

    @routes.get("/oauth/bluesky/jwks.json")
    async def bs_jwks(_):
        from . import atproto
        k = await atproto.client_key(db)
        return web.json_response({"keys": [atproto.public_jwk(k, True)]}, headers={"Cache-Control": "max-age=300"})

    @routes.get("/oauth/{platform:tiktok|instagram|patreon|pinterest|discord|subscribestar|bluesky|youtube|vkid|x}")
    async def oauth_cb(request):
        """Сюда TikTok/Instagram возвращают после входа: меняем код на доступ и сразу грузим ролики."""
        from . import social
        platform = request.match_info["platform"]
        name = {"tiktok": "TikTok", "instagram": "Instagram", "patreon": "Patreon", "pinterest": "Pinterest", "discord": "Discord",
                "subscribestar": "SubscribeStar", "bluesky": "Bluesky", "youtube": "YouTube", "vkid": "VK", "x": "X"}[platform]
        q = request.query
        if q.get("error") or q.get("error_reason"):
            ok, msg = False, f"Вход отменён: {q.get('error_description') or q.get('error_reason') or q.get('error')}"
        elif not q.get("code"):
            ok, msg = False, "Нет кода входа — начни заново из CRM"
        else:
            try:
                ws = social.state_ws(platform, q.get("state", "")) or "main"
                wdb = await workspaces.get_db(ws)
                s = await dbm.get_settings(wdb)
                r = await social.oauth_callback(wdb, social.http_client(social.proxy_url(s)), platform, q["code"], q.get("state", ""),
                                                q.get("iss", ""), extra={"device_id": q.get("device_id", "")})
                user, st = r["name"], r["status"] or {}
                ok = True
                msg = f"{name} подключён{(' как ' + user) if user else ''}" + \
                      (f" — аккаунтов {name} в CRM: {r['count']}" if r["count"] > 1 else "") + ". " + \
                      ((f"Платных подписчиков: {st.get('paid', 0)}." if platform == "patreon" else
                        (f"Подписчиков: {st.get('subscribers')}." if st.get('subscribers') is not None else "Подключено.")
                        if platform in ("subscribestar", "discord") else f"Загружено публикаций: {st.get('videos', 0)}.")
                       if st.get("ok") else f"Но загрузка не удалась: {st.get('error', '')}")
                if platform in ("youtube", "vkid", "x") or (platform == "tiktok" and r.get("can_publish")):
                    msg += " Публикация разрешена — теперь CRM выкладывает посты сюда сама."
            except social.SyncError as e:
                ok, msg = False, str(e)
            except Exception as e:
                log.exception("OAuth %s", platform)
                ok, msg = False, f"Не удалось связаться с {name} ({type(e).__name__}). Возможно, сайт недоступен с хостинга — задай прокси в CRM."
        import html as _h
        page = (f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
                f"<title>{name}</title><body style='font:16px -apple-system,Segoe UI,Roboto,sans-serif;background:#eaf3ff;color:#0f2a4a;"
                f"display:grid;place-items:center;min-height:90vh;margin:0;padding:16px'><div style='max-width:420px;background:#fff;"
                f"padding:24px;border-radius:18px;box-shadow:0 10px 30px rgba(31,92,170,.13)'><h2 style='margin-top:0'>"
                f"{'✅' if ok else '⚠️'} {name}</h2><p>{_h.escape(msg)}</p><a href='/' style='display:inline-block;margin-top:8px;"
                f"background:#1f7cff;color:#fff;padding:10px 16px;border-radius:12px;text-decoration:none'>Вернуться в CRM</a></div>")
        return web.Response(text=page, content_type="text/html")

    # ---------- загрузка видео кусками (надёжно с телефона и через любые лимиты прокси)
    uploads: dict[str, dict] = {}

    @routes.post("/api/upload/start")
    async def up_start(request):
        if not _authed(request):
            return web.json_response({"error": "auth"}, status=401)
        from . import publish
        b = await request.json()
        size = int(b.get("size") or 0)
        if size <= 0 or size > cfg.max_upload_mb * 1024 * 1024:
            return web.json_response({"error": f"Файл больше {cfg.max_upload_mb} МБ"}, status=400)
        token, path = publish.new_upload(b.get("name", "video.mp4"))
        uploads[token] = {"path": path, "size": size, "name": b.get("name", "video.mp4")}
        return web.json_response({"token": token})

    @routes.post("/api/upload/chunk")
    async def up_chunk(request):
        if not _authed(request):
            return web.json_response({"error": "auth"}, status=401)
        u = uploads.get(request.query.get("token", ""))
        if not u:
            return web.json_response({"error": "Загрузка не найдена — начни заново"}, status=404)
        offset = int(request.query.get("offset", "0"))
        have = os.path.getsize(u["path"])
        if offset != have:                     # кусок уже есть (повтор после обрыва) — сообщаем, сколько дошло
            return web.json_response({"got": have})
        data = await request.read()
        with open(u["path"], "ab") as f:
            f.write(data)
        return web.json_response({"got": have + len(data)})

    @routes.post("/api/upload/finish")
    async def up_finish(request):
        if not _authed(request):
            return web.json_response({"error": "auth"}, status=401)
        b = await request.json()
        token = b.get("token", "")
        u = uploads.get(token)
        if not u:
            return web.json_response({"error": "Загрузка не найдена"}, status=404)
        if os.path.getsize(u["path"]) != u["size"]:
            return web.json_response({"error": "Файл дошёл не целиком — загрузи ещё раз"}, status=400)
        os.rename(u["path"], u["path"][:-5])
        uploads.pop(token, None)
        return web.json_response({"token": token, "size": u["size"]})

    @routes.get("/media/{token}/{name}")
    async def media(request):
        """Видео по секретной ссылке: его забирает Instagram и скачиваешь ты для ручной публикации."""
        from . import publish
        path = publish.media_path(request.match_info["token"])
        if not path or path.endswith(".part"):
            raise web.HTTPNotFound()
        headers = {"Cache-Control": "private, max-age=3600"}
        if request.query.get("dl"):
            headers["Content-Disposition"] = f'attachment; filename="{request.match_info["name"]}"'
        return web.FileResponse(path, headers=headers)

    @routes.route("*", "/api/{tail:.*}")
    async def api_any(request):
        if not _authed(request):
            return web.json_response({"error": "auth"}, status=401)
        body = {}
        if request.method == "POST":
            try:
                body = await request.json()
            except Exception:
                body = {}
        ws = _authed(request)
        wdb = await workspaces.get_db(ws)
        ctx = make_ctx(ws)
        status, data = await api.handle(wdb, ctx, request.method, request.path, dict(request.query), body)
        if isinstance(data, dict) and "__file__" in data:          # выгрузка: отдаём файл
            from urllib.parse import quote
            return web.Response(body=data["__file__"], headers={
                "Content-Type": data["ctype"], "Cache-Control": "no-store",
                "Content-Disposition": f"attachment; filename=\"{data['name']}\"; filename*=UTF-8''{quote(data['name'])}"})
        return web.json_response(data, status=status, dumps=lambda o: json.dumps(o, ensure_ascii=False, default=str))

    async def static_file(name: str, ctype: str | None = None, cache: str = "no-cache"):
        path = os.path.join(STATIC, name)
        resp = web.FileResponse(path, headers={"Cache-Control": cache})
        if ctype:
            resp.content_type = ctype
        return resp

    @routes.get("/")
    async def index(_):
        return await static_file("index.html")

    @routes.get("/manifest.webmanifest")
    async def manifest(_):
        return await static_file("manifest.webmanifest", "application/manifest+json")

    @routes.get("/sw.js")
    async def sw(_):
        return await static_file("sw.js", "application/javascript")

    @routes.get("/{name:icon-[0-9a-z]+\\.png|apple-touch-icon\\.png|favicon\\.png}")
    async def icons(request):
        return await static_file(request.match_info["name"], "image/png", "public, max-age=604800")

    app = web.Application(client_max_size=16 * 1024 * 1024)
    app.add_routes(routes)
    return app


async def start(db, bot) -> web.AppRunner:
    app = build_app(db, bot)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", cfg.port).start()
    log.info("Панель CRM: порт %s, адрес %s", cfg.port, cfg.public_url or "(PUBLIC_URL не задан)")
    return runner
