"""JSON-API панели CRM. Без привязки к веб-фреймворку: web.py только передаёт сюда запросы."""
import re
from . import db as dbm, crm, runtime, social, publish, analytics
from .config import cfg

SETTING_KEYS = {"rem_start", "rem_test", "rem_download", "rem_feedback", "rem_final", "report_on",
                "channel_id", "channel_title", "channel_url", "post_channel", "post_platforms", "post_channel_adult"}


class Ctx:
    """send(user_id, text) — написать человеку через бота; report() — отправить сводку автору."""
    def __init__(self, send=None, report=None, http=None, is_main=True, ws="main", notify=None, bot=None):
        self.send, self.report, self.http, self.is_main, self.ws = send, report, http, is_main, ws
        self.bot = bot                # бот (только в CRM автора) — отправить набор себе на проверку
        self.notify = notify          # notify(chat_id, text, button_text, url) — сообщение с кнопкой через бота


def _int(v, default=0):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


async def handle(db, ctx: Ctx, method: str, path: str, q: dict, body: dict) -> tuple[int, dict]:
    if method == "GET" and path == "/api/meta":
        ws_name = ""
        if not ctx.is_main:
            from . import workspaces
            w = await workspaces.get(int(ctx.ws[1:]))
            ws_name = (w["name"] if w else "") or ""
        return 200, {
            "ws_main": ctx.is_main, "ws": ctx.ws, "ws_name": ws_name, "connected": await dbm.connected_platforms(db),
            "bot_username": runtime.state.get("bot_username", ""), "public_url": cfg.public_url,
            "track_download": crm.track_download(), "drop_hours": cfg.drop_hours,
            "platforms": dbm.PLATFORMS, "stages": [{"key": k, "name": n} for k, n, _ in crm.stages()],
            "lead_statuses": [{"key": k, "name": n} for k, n in dbm.LEAD_STATUSES],
            "products": [{"key": k, "name": n} for k, n in dbm.PRODUCTS.items()],
            "prefixes": [{"prefix": p, "platform": v[0], "kind": v[1]} for p, v in dbm.PREFIX.items() if p not in ("s", "insta")],
        }
    if path.startswith("/api/pack"):
        if not ctx.is_main:
            return 403, {"error": "Подарки по ссылке раздаёт бот автора"}
        from . import packs
        bu = runtime.state.get("bot_username", "")
        try:
            if method == "GET" and path == "/api/packs":
                return 200, {"items": await packs.list_all(db, bu), "defaults": packs.DEFAULTS, "kinds": packs.KINDS,
                             "bot_username": bu}
            if method == "POST" and path == "/api/pack":
                p = await packs.save(db, body or {})
                return 200, {"ok": True, "pack": packs.public(p, bu)}
            code = (body or {}).get("code", "")
            if method == "POST" and path == "/api/pack/file":
                src = publish.media_path(body.get("token", ""))
                if not src or src.endswith(".part"):
                    return 400, {"error": "Файл не загрузился — выбери его ещё раз"}
                p = await packs.add_file(db, code, src, body.get("name") or "file")
                return 200, {"ok": True, "pack": packs.public(p, bu)}
            if method == "POST" and path == "/api/pack/file/delete":
                p = await packs.remove_file(db, code, _int(body.get("index"), -1))
                return 200, {"ok": True, "pack": packs.public(p, bu)}
            if method == "POST" and path == "/api/pack/file/rename":
                p = await packs.rename_file(db, code, _int(body.get("index"), -1), body.get("name") or "")
                return 200, {"ok": True, "pack": packs.public(p, bu)}
            if method == "POST" and path == "/api/pack/file/move":
                p = await packs.move_file(db, code, _int(body.get("index"), -1), -1 if _int(body.get("delta")) < 0 else 1)
                return 200, {"ok": True, "pack": packs.public(p, bu)}
            if method == "POST" and path == "/api/pack/delete":
                await packs.delete(db, code)
                return 200, {"ok": True}
            if method == "POST" and path == "/api/pack/test":
                p = await packs.get(db, code)
                if not p or not p["files"]:
                    return 400, {"error": "Сначала добавь файлы"}
                if not ctx.bot or not cfg.admins:
                    return 400, {"error": "Нет ADMIN_IDS — некому отправить"}
                from . import keyboards as kb
                for a in cfg.admins:
                    await ctx.bot.send_message(a, "👀 Так набор видят люди:\n\n" + p["intro"], reply_markup=kb.pack_kb(p))
                    await packs.send(ctx.bot, db, a, p)
                return 200, {"ok": True}
        except ValueError as e:
            return 400, {"error": str(e)}
        return 404, {"error": "not found"}
    if method == "GET" and path == "/api/funnel":
        return 200, await crm.funnel(db, q.get("period", "30"), q.get("platform") or None)
    if method == "GET" and path == "/api/content":
        return 200, {"items": await crm.content_list(db)}
    if method == "POST" and path == "/api/content":
        code = dbm.clean_code(body.get("code"))
        if not code:
            return 400, {"error": "Метка: латиница, цифры, _ и -"}
        plat, kind = dbm.parse_code(code)
        kw = re.sub(r"\s+", "", str(body.get("keyword") or "")).upper()[:40]
        await db.execute(
            "INSERT INTO content(code, platform, kind, title, url, keyword, views, views_at, created_at) VALUES(?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(code) DO UPDATE SET platform=excluded.platform, kind=excluded.kind, title=excluded.title, "
            "url=excluded.url, keyword=excluded.keyword, views=excluded.views, "
            "views_at=CASE WHEN excluded.views != content.views THEN excluded.views_at ELSE content.views_at END",
            (code, body.get("platform") or plat, body.get("kind") or kind, str(body.get("title") or "")[:200],
             str(body.get("url") or "")[:500], kw, max(0, _int(body.get("views"))), dbm.now(), dbm.now()))
        await db.commit()
        return 200, {"ok": True, "code": code}
    if method == "POST" and path == "/api/content/delete":
        await db.execute("DELETE FROM content WHERE code=?", (body.get("code"),))
        await db.commit()
        return 200, {"ok": True}
    if method == "GET" and path == "/api/users":
        return 200, await crm.users_list(db, q.get("status", ""), q.get("stage", ""), q.get("platform", ""),
                                         q.get("q", ""), q.get("source", ""), q.get("period", "all"),
                                         _int(q.get("limit"), 50), _int(q.get("offset"), 0))
    if method == "GET" and path == "/api/user":
        u = await crm.user_card(db, _int(q.get("id")))
        return (200, u) if u else (404, {"error": "Не найден"})
    if method == "POST" and path == "/api/user/lead":
        uid, status = _int(body.get("id")), body.get("status")
        if status in ("", None, "none"):
            await db.execute("UPDATE users SET lead_status=NULL, lead_at=NULL WHERE id=?", (uid,))
            await dbm.log_event(db, uid, "lead", {"status": None, "by": "panel"})
        elif status in dbm.LEAD_NAME:
            await dbm.set_lead(db, uid, status, by="panel")
        else:
            return 400, {"error": "Неизвестный статус"}
        return 200, {"ok": True}
    if method == "POST" and path == "/api/user/note":
        text = str(body.get("text") or "").strip()
        if not text:
            return 400, {"error": "Пустая заметка"}
        await dbm.add_note(db, _int(body.get("id")), text[:4000])
        return 200, {"ok": True}
    if method == "POST" and path == "/api/user/note/delete":
        await db.execute("DELETE FROM notes WHERE id=?", (_int(body.get("note_id")),))
        await db.commit()
        return 200, {"ok": True}
    if method == "POST" and path == "/api/user/sale":
        try:
            amount = float(str(body.get("amount", "0")).replace(" ", "").replace(",", "."))
        except ValueError:
            return 400, {"error": "Сумма — числом"}
        if amount <= 0:
            return 400, {"error": "Сумма должна быть больше нуля"}
        await dbm.add_sale(db, _int(body.get("id")), body.get("product", "automation"), amount,
                           body.get("currency") or "RUB", str(body.get("note") or "")[:500])
        return 200, {"ok": True}
    if method == "POST" and path == "/api/user/sale/delete":
        await db.execute("DELETE FROM sales WHERE id=?", (_int(body.get("sale_id")),))
        await db.commit()
        return 200, {"ok": True}
    if method == "POST" and path == "/api/user/message":
        uid, text = _int(body.get("id")), str(body.get("text") or "").strip()
        if not text:
            return 400, {"error": "Пустое сообщение"}
        if not ctx.send:
            return 503, {"error": "Бот недоступен"}
        try:
            await ctx.send(uid, text[:4000])
        except Exception as e:
            await dbm.set_blocked(db, uid, True)
            return 400, {"error": f"Не доставлено: {e}"}
        await dbm.log_event(db, uid, "admin_msg", {"text": text[:2000], "via": "panel"})
        u = await dbm.get_user(db, uid)
        if u and u["lead_status"] in (None, "request"):
            await dbm.set_lead(db, uid, "dialog", by="panel")
        return 200, {"ok": True}
    if method == "GET" and path == "/api/deals":
        return 200, await crm.deals(db)
    if method == "GET" and path == "/api/settings":
        s = await dbm.get_settings(db)
        return 200, {
            "settings": {k: s.get(k, "") for k in SETTING_KEYS},
            "channel": {"chat": str(runtime.channel_chat() or ""), "url": runtime.channel_url(),
                        "title": runtime.state.get("channel_title", "")},
            "reminders": await crm.reminder_stats(db, s),
            "report_hour": cfg.report_hour, "tz": cfg.tz, "drop_hours": cfg.drop_hours,
            "public_url": cfg.public_url, "social": await social.status(db),
        }
    if method == "POST" and path == "/api/settings":
        for k, v in (body or {}).items():
            if k in SETTING_KEYS:
                if k.startswith("rem_") or k == "report_on":
                    v = "1" if v in (True, 1, "1", "true", "on") else "0"
                await dbm.set_setting(db, k, str(v).strip())
        await runtime.refresh(db)
        return 200, {"ok": True}
    if method == "GET" and path == "/api/export":
        from . import export
        what = "funnel" if q.get("what") == "funnel" else "channels"
        fmt = "csv" if q.get("fmt") == "csv" else "xlsx"
        data, name, ctype = await export.build(db, what, fmt, str(q.get("period") or "30"), str(q.get("platform") or "all"))
        return 200, {"__file__": data, "name": name, "ctype": ctype}
    if method == "GET" and path == "/api/social":
        return 200, await social.status(db)
    if method == "POST" and path == "/api/social":
        # ключи приложений (один раз на соцсеть) — пустые не затирают сохранённые
        for k in ("tt_client_key", "ig_app_id", "social_proxy", "x_max_posts", "pt_client_id", "pn_client_id", "dc_client_id",
                  "ss_client_id", "ss_domain", "ss_scopes", "faexport_url", "post_channel_adult", "yt_client_id", "vk_app_id",
                  "x_client_id"):
            if k in body:
                await dbm.set_setting(db, k, str(body[k] or "").strip())
        for k in ("pn_standard", "tt_publish", "tt_stats", "manual_tg"):
            if k in body:
                await dbm.set_setting(db, k, "1" if body[k] in (True, 1, "1", "true") else "0")
        for k in ("yt_api_key", "vk_token", "tt_client_secret", "ig_app_secret", "x_bearer", "pt_client_secret", "pn_client_secret",
                  "dc_client_secret", "ss_client_secret", "yt_client_secret", "vk_app_secret", "x_client_secret"):
            if str(body.get(k) or "").strip():
                await dbm.set_setting(db, k, str(body[k]).strip())
        if body.get("drop_key") in ("vk_token", "x_bearer", "yt_api_key"):
            await dbm.set_setting(db, body["drop_key"], "")
        res, acc = {}, None
        # аккаунты: добавить, скрыть/показать, удалить, обновить
        add = body.get("add")
        if add in ("yt", "vk", "x", "tg"):
            ref = str(body.get("ref") or "").strip()
            if not ref:
                return 400, {"error": "Вставь ссылку на канал"}
            acc = await social.add_account(db, add, ref=ref, name=ref)
        elif add in ("fa", "e6", "dc", "bs"):
            from . import networks, atproto
            s0 = await dbm.get_settings(db)
            px = social.proxy_url(s0)
            try:
                if add == "fa":
                    name = networks.parse_fa_user(str(body.get("ref") or ""))
                    if not name:
                        return 400, {"error": "Вставь ссылку на профиль Fur Affinity"}
                    acc = await social.add_account(db, "fa", ref=name, name=name,
                                                   data={"cookie": str(body.get("cookie") or "").strip()} if body.get("cookie") else {})
                elif add == "e6":
                    acc = await networks.e6_connect(db, str(body.get("ref") or ""), str(body.get("api_key") or ""), px)
                elif add == "dc":
                    acc = await networks.dc_add_url(db, str(body.get("ref") or ""), px)
                else:
                    acc = await atproto.password_login(db, str(body.get("ref") or ""), str(body.get("app_password") or ""), px)
            except (networks.NetError, atproto.BskyError) as e:
                return 400, {"error": str(e)}
        elif add == "pt" and str(body.get("pt_access") or "").strip():      # Patreon — токены создателя вручную
            acc = await social.add_account(db, "pt", name="Patreon", data={"access": str(body["pt_access"]).strip(),
                                                                          "refresh": str(body.get("pt_refresh") or "").strip()})
        if body.get("acc_set"):                 # настройки аккаунта: доска Pinterest, канал 18+, приглашение, публиковать сюда…
            a = await social.get_account(db, body["acc_set"])
            if not a:
                return 404, {"error": "Аккаунт не найден"}
            d = a["data"]
            for k in ("board_id", "invite", "artist"):
                if k in body:
                    d[k] = str(body[k] or "").strip()[:200]
            for k in ("adult", "publish"):
                if k in body:
                    d[k] = body[k] in (True, 1, "1", "true", "on")
            if str(body.get("cookie") or "").strip():
                d["cookie"] = str(body["cookie"]).strip()[:500]
            if body.get("cookie_off"):
                d.pop("cookie", None)
            if str(body.get("name") or "").strip():
                a["name"], d["custom_name"] = str(body["name"]).strip()[:80], True
            await social.save_account(db, a)
        if "hide" in body:
            await social.set_hidden(db, body["hide"] if body["hide"] != "all" else None, bool(body.get("hidden")))
        if body.get("delete"):
            await social.delete_account(db, body["delete"])
        sync = acc["id"] if acc else body.get("sync")
        if sync:
            res = await social.sync_all(db, ctx.http, force=True, only=sync)
        if acc and add == "tg" and not await dbm.get_setting(db, "post_channel", ""):
            a2 = await social.get_account(db, acc["id"])      # первый канал — он же канал для публикаций
            if a2 and a2["data"].get("chat") and (a2["status"] or {}).get("ok"):
                await dbm.set_setting(db, "post_channel", a2["data"]["chat"])
        return 200, dict(await social.status(db), result=res, added=acc["id"] if acc else None)
    if method == "POST" and path == "/api/social/send_login":
        # отправить владельцу этой CRM в Telegram кнопку входа в соцсеть (TikTok/Instagram/Patreon)
        owner = await dbm.get_setting(db, "ws_owner", "")
        if not owner.isdigit():
            return 400, {"error": "Это твоя CRM — просто нажми «Войти»"}
        platform = body.get("platform", "")
        try:
            url = await social.oauth_url(db, platform, ctx.ws)
        except social.SyncError as e:
            return 400, {"error": str(e)}
        name = {"tiktok": "TikTok", "instagram": "Instagram", "patreon": "Patreon"}.get(platform, platform)
        if not ctx.notify:
            return 503, {"error": "Бот недоступен"}
        try:
            await ctx.notify(int(owner), f"🔗 Подключи {name} к своей CRM: нажми кнопку, войди в {name} и разреши доступ. "
                                         f"Ссылка действует 30 минут.", f"Подключить {name}", url)
        except Exception as e:
            return 400, {"error": f"Не удалось отправить: {e}"}
        return 200, {"ok": True}
    if method == "POST" and path == "/api/social/oauth":
        try:
            return 200, {"url": await social.oauth_url(db, body.get("platform", ""), ctx.ws, str(body.get("handle") or ""))}
        except social.SyncError as e:
            return 400, {"error": str(e)}
    # ---------------- Публикация
    if method == "GET" and path == "/api/posts":
        s = await dbm.get_settings(db)
        auto = await publish.auto_available(db)
        ch = await publish.channel_for_posts(db)
        return 200, {"items": await publish.list_posts(db), "auto": auto, "post_channel": str(ch or ""),
                     "support": publish.SUPPORT, "notes": {f"{p}:{t}": n for (p, t), n in publish.NOTES.items()},
                     "ptypes": publish.PTYPES, "order": publish.ORDER, "adult": publish.ADULT, "adult_block": publish.ADULT_BLOCK,
                     "adult_levels": publish.ADULT_LEVELS, "multi": list(publish.MULTI),
                     "names": {k: v["name"] for k, v in publish.PLATFORMS.items()},
                     "default_platforms": [p for p in (s.get("post_platforms") or "yt,tt,ig,vk,tg").split(",") if p],
                     "public_url": cfg.public_url, "bot_username": runtime.state.get("bot_username", "")}
    if method == "GET" and path == "/api/post":
        p = await publish.get_post(db, _int(q.get("id")))
        return (200, p) if p else (404, {"error": "Не найдено"})
    if method == "POST" and path == "/api/post/preview":
        cur = await db.execute("SELECT COALESCE(MAX(id),0)+1 FROM posts")
        nid = (await cur.fetchone())[0]
        ptype = body.get("ptype") if body.get("ptype") in publish.PTYPES else "video"
        short = body["kind"] == "short" if body.get("kind") in ("short", "long") else \
            publish.is_short(body.get("duration"), body.get("width"), body.get("height"))
        plats = [p for p in publish.ORDER if p in (body.get("platforms") or []) and p in publish.support(ptype)]
        codes = publish.make_codes(plats, ptype, short, nid)
        kw = str(body.get("keyword") or "").upper()
        return 200, {"captions": publish.build_captions(str(body.get("title") or ""), str(body.get("text") or ""), kw, codes,
                                                        ctx.is_main, ptype, str(body.get("tags") or "")),
                     "codes": codes, "short": short}
    if method == "POST" and path == "/api/post":
        try:
            pid = await publish.create_post(db, body)
        except ValueError as e:
            return 400, {"error": str(e)}
        await dbm.set_setting(db, "post_platforms", ",".join(p for p in publish.ORDER if p in (body.get("platforms") or [])))
        return 200, {"id": pid}
    if method == "POST" and path == "/api/post/target":
        tid, action = _int(body.get("id")), body.get("action")
        if action == "done":
            await publish.mark_done(db, tid, str(body.get("url") or ""))
        elif action == "retry":
            await publish.retry(db, tid)
        elif action == "auto":
            why = await publish.make_auto(db, tid)
            if why:
                return 400, {"error": why}
        elif action == "caption" and isinstance(body.get("caption"), dict):
            await publish.update_caption(db, tid, {k: str(v) for k, v in body["caption"].items() if k in ("title", "text")})
        else:
            return 400, {"error": "Неизвестное действие"}
        return 200, {"ok": True}
    if method == "POST" and path == "/api/post/delete":
        await publish.delete_post(db, _int(body.get("id")))
        return 200, {"ok": True}
    if method == "POST" and path == "/api/post/check":
        # «Проверить, вышел ли» — внеплановая синхронизация соцсетей и поиск ролика
        res = await social.sync_all(db, ctx.http, force=True)
        await publish.detect_published(db)
        return 200, {"ok": True, "result": res}
    # ---------------- Аналитика
    if method == "GET" and path == "/api/analytics":
        if q.get("days") == "all":
            return 200, await analytics.analytics(db, 365, q.get("platform") or None, all_time=True)
        return 200, await analytics.analytics(db, max(1, min(365, _int(q.get("days"), 30))), q.get("platform") or None)
    if method == "GET" and path == "/api/videos":
        return 200, {"items": await analytics.video_list(db, q.get("platform") or None)}
    if method == "GET" and path == "/api/video":
        v = await analytics.video_detail(db, q.get("code", ""))
        return (200, v) if v else (404, {"error": "Не найдено"})
    # ---------------- Доступ друзей (только в CRM автора)
    if path.startswith("/api/workspaces"):
        if not ctx.is_main:
            return 403, {"error": "Только для автора"}
        from . import workspaces
        if path == "/api/workspaces/enter":
            # автор заходит в CRM клиента, чтобы настроить её за него
            w = await workspaces.get(_int(body.get("tg_id")))
            if not w or w["status"] != "own":
                return 400, {"error": "У этого человека нет своей CRM"}
            from .web import make_token
            await workspaces.get_db(f"u{w['tg_id']}")
            return 200, {"token": make_token(12 * 3600, f"u{w['tg_id']}"), "name": w["name"]}
        if method == "POST":
            st = body.get("status")
            if st not in ("own", "view", "denied"):
                return 400, {"error": "Неизвестный статус"}
            await workspaces.decide(_int(body.get("tg_id")), st)
        return 200, {"items": [dict(r, status_name=workspaces.STATUS_NAME.get(r["status"], r["status"])) for r in await workspaces.all_rows()]}
    if method == "GET" and path == "/api/report":
        return 200, {"text": await crm.daily_report(db)}
    if method == "POST" and path == "/api/report":
        if not ctx.report:
            return 503, {"error": "Бот недоступен"}
        await ctx.report()
        return 200, {"ok": True}
    return 404, {"error": "Нет такого метода"}
