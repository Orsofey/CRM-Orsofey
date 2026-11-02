"""Прямая публикация в YouTube, TikTok, VK, X и Fur Affinity — без пересылки файлов в Telegram.

Каждая соцсеть подключается для публикации один раз («Разрешить публикацию» в Настройках):
  YouTube — вход через Google (OAuth, право youtube.upload). Видео и шортсы. Посты «Сообщества» API не публикует.
  TikTok  — тот же вход TikTok, но с правами Content Posting API (video.publish / video.upload). Видео и фото-посты.
  VK      — вход через VK ID (права wall, photos, video, docs). Посты, фото, GIF; видео — если VK выдал приложению право video.
  X       — вход через X (OAuth 2.0: tweet.write, media.write). Текст, фото, GIF, видео.
  Fur Affinity — API нет; загрузка как из браузера по cookie аккаунта (то же делают PostyBirb и аналоги).

Функции *_publish(db, acc, t, st, s, proxy) вызываются из publish._publish_multi для каждого аккаунта:
вернуть {"url", "ext", "note"} — готово; {"wait": True} — соцсеть ещё обрабатывает файл (проверим через 30 с).
В st хранится, что уже сделано, — повторной загрузки при следующем шаге не будет.
"""
import base64, hashlib, html as _html, json, logging, os, re, secrets
from urllib.parse import urlencode
from . import db as dbm, net
from .config import cfg

log = logging.getLogger(__name__)
H = 3600


class PostError(Exception):
    pass


class NeedManual(PostError):
    """Сама CRM этот пост сюда выложить не может (нет права или API) — переводим в «вручную», а не в ошибку."""


def _redirect(platform: str) -> str:
    return f"{cfg.public_url}/oauth/{platform}"


def plain(s: str) -> str:
    return re.sub(r"</?b>", "", s or "").replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def _pkce() -> tuple[str, str]:
    v = secrets.token_urlsafe(48)[:64]
    c = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
    return v, c


def _skey(prefix: str, state: str) -> str:
    return prefix + hashlib.sha256(state.encode()).hexdigest()[:20]


async def _save_verifier(db, prefix: str, state: str, verifier: str):
    await dbm.set_setting(db, _skey(prefix, state), verifier)


async def _pop_verifier(db, prefix: str, state: str) -> str:
    k = _skey(prefix, state)
    v = await dbm.get_setting(db, k, "")
    if v:
        await db.execute("DELETE FROM settings WHERE key=?", (k,))
        await db.commit()
    return v


def can_publish(acc: dict) -> bool:
    """Есть ли у аккаунта права, чтобы CRM публиковала сама."""
    d, k = acc.get("data") or {}, acc.get("kind")
    if k == "yt":
        return bool(d.get("g_refresh") or d.get("g_access"))
    if k == "tt":
        sc = d.get("scope") or ""
        return bool(d.get("access")) and ("video.publish" in sc or "video.upload" in sc)
    if k == "vk":
        return bool(d.get("vk_access"))
    if k == "x":
        return bool(d.get("x_access"))
    if k == "fa":
        return bool(d.get("cookie"))
    return True


def _size(f: dict) -> int:
    return int(f.get("size") or (os.path.getsize(f["path"]) if f.get("path") and os.path.exists(f["path"]) else 0))


def _chunk(path: str, start: int, n: int) -> bytes:
    with open(path, "rb") as fh:
        fh.seek(start)
        return fh.read(n)


def _mime(f: dict, ptype: str) -> str:
    n = (f.get("name") or f.get("path") or "").lower()
    if n.endswith(".gif"):
        return "image/gif"
    if n.endswith(".png"):
        return "image/png"
    if n.endswith(".webp"):
        return "image/webp"
    if n.endswith((".jpg", ".jpeg")):
        return "image/jpeg"
    if n.endswith(".mov"):
        return "video/quicktime"
    if n.endswith(".webm"):
        return "video/webm"
    return "video/mp4" if ptype in ("video", "gif") else "image/jpeg"


# ================================================================== YouTube (Google OAuth)
G_AUTH = "https://accounts.google.com/o/oauth2/v2/auth"
G_TOKEN = "https://oauth2.googleapis.com/token"
YT_API = "https://www.googleapis.com/youtube/v3/"
YT_UPLOAD = "https://www.googleapis.com/upload/youtube/v3/videos"
YT_SCOPES = "https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly"
YT_CHUNK = 8 * 1024 * 1024          # кратно 256 КБ — так требует Google


def yt_auth_url(s: dict, state: str) -> str:
    if not (s.get("yt_client_id") and s.get("yt_client_secret")):
        raise PostError("Вставь Client ID и Client secret OAuth-клиента Google (Настройки → YouTube)")
    return G_AUTH + "?" + urlencode({"client_id": s["yt_client_id"], "redirect_uri": _redirect("youtube"), "response_type": "code",
                                     "scope": YT_SCOPES, "access_type": "offline", "prompt": "select_account consent", "state": state,
                                     "include_granted_scopes": "true"})


def _g_err(r: net.Resp, what: str) -> str:
    j = r.json() if isinstance(r.json(), dict) else {}
    e = j.get("error")
    msg = (e.get("message") if isinstance(e, dict) else j.get("error_description") or e) or r.text[:150]
    reason = ""
    if isinstance(e, dict):
        reason = ((e.get("errors") or [{}])[0] or {}).get("reason", "")
    if reason in ("quotaExceeded", "uploadLimitExceeded"):
        return "YouTube: дневной лимит загрузок исчерпан — попробуй завтра"
    if reason == "youtubeSignupRequired":
        return "YouTube: у этого Google-аккаунта нет канала — создай канал на youtube.com"
    if r.status == 401 or e == "invalid_grant":
        return "YouTube: доступ устарел — нажми «Разрешить публикацию» у канала ещё раз"
    if r.status == 403 and "insufficient" in str(msg).lower():
        return "YouTube: при входе не дали право загружать видео — войди ещё раз и поставь все галочки"
    return f"YouTube ({what}): {msg}"[:250]


async def yt_exchange(db, s: dict, code: str, proxy=None) -> dict:
    from . import social
    r = await net.request("POST", G_TOKEN, proxy=proxy, data={"code": code, "client_id": s["yt_client_id"],
                                                               "client_secret": s["yt_client_secret"], "redirect_uri": _redirect("youtube"),
                                                               "grant_type": "authorization_code"})
    j = r.json() or {}
    if not r.ok or not j.get("access_token"):
        raise PostError(_g_err(r, "вход"))
    data = {"g_access": j["access_token"], "g_expires": dbm.now() + int(j.get("expires_in") or 3600)}
    if j.get("refresh_token"):
        data["g_refresh"] = j["refresh_token"]
    ch = await net.request("GET", YT_API + "channels", params={"part": "snippet", "mine": "true"},
                           headers={"Authorization": "Bearer " + data["g_access"]}, proxy=proxy)
    items = (ch.json() or {}).get("items") or [] if ch.ok else []
    if not items:
        raise PostError("YouTube: у этого Google-аккаунта нет канала. Войди тем аккаунтом, где твой канал "
                        "(если канал на «аккаунте бренда» — выбери его в списке при входе)")
    c = items[0]
    return await social.add_account(db, "yt", ref=f"https://www.youtube.com/channel/{c['id']}",
                                    name=(c.get("snippet") or {}).get("title") or "YouTube", data=data, ext=c["id"])


async def google_token(db, acc: dict, s: dict, proxy=None) -> str:
    from . import social
    d = acc["data"]
    if d.get("g_access") and int(d.get("g_expires") or 0) > dbm.now() + 120:
        return d["g_access"]
    if not d.get("g_refresh"):
        raise PostError("YouTube: нажми «Разрешить публикацию» у канала ещё раз")
    r = await net.request("POST", G_TOKEN, proxy=proxy, data={"client_id": s.get("yt_client_id", ""), "client_secret": s.get("yt_client_secret", ""),
                                                               "refresh_token": d["g_refresh"], "grant_type": "refresh_token"})
    j = r.json() or {}
    if not r.ok or not j.get("access_token"):
        raise PostError(_g_err(r, "продление входа"))
    d["g_access"], d["g_expires"] = j["access_token"], dbm.now() + int(j.get("expires_in") or 3600)
    await social.save_account(db, acc)
    return d["g_access"]


async def yt_publish(db, acc: dict, t: dict, st: dict, s: dict, proxy=None) -> dict:
    if t["ptype"] != "video":
        raise NeedManual("YouTube: посты с фото и текстом («Сообщество») API YouTube не публикует — выложи вручную")
    f = t["flist"][0]
    size = _size(f)
    tok = await google_token(db, acc, s, proxy)
    h = {"Authorization": "Bearer " + tok}
    cap = t["cap"]
    short = t.get("kind") == "short"
    title = plain(cap.get("title") or t.get("title") or "").strip() or plain(cap.get("text", "")).split("\n")[0][:90] or "Новое видео"
    title = re.sub(r"[<>]", "", title)[:100]
    if short and "#shorts" not in title.lower() and len(title) <= 92:
        title += " #Shorts"
    tags = [x for x in re.split(r"[,\s]+", (cap.get("tags") or t.get("tags") or "").replace("#", "")) if x][:30]
    if not st.get("session"):
        meta = {"snippet": {"title": title, "description": re.sub(r"[<>]", "", plain(cap.get("text", "")))[:4900], "tags": tags,
                            "categoryId": "22"},
                "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False}}
        r = await net.request("POST", YT_UPLOAD, params={"uploadType": "resumable", "part": "snippet,status"}, json=meta, proxy=proxy,
                              headers=dict(h, **{"X-Upload-Content-Length": str(size), "X-Upload-Content-Type": _mime(f, "video")}))
        if not r.ok or not r.headers.get("location"):
            raise PostError(_g_err(r, "загрузка"))
        st.update(session=r.headers["location"], sent=0)
    sent = int(st.get("sent") or 0)
    if sent:                                             # прошлый раз оборвалось — спросим, сколько дошло
        r = await net.request("PUT", st["session"], headers=dict(h, **{"Content-Range": f"bytes */{size}", "Content-Length": "0"}),
                              body=b"", proxy=proxy, redirects=False)
        if r.status in (200, 201):
            return _yt_done(st, r)
        m = re.search(r"bytes=0-(\d+)", r.headers.get("range", ""))
        sent = int(m.group(1)) + 1 if m else 0
    while sent < size:
        n = min(YT_CHUNK, size - sent)
        r = await net.request("PUT", st["session"], body=_chunk(f["path"], sent, n), proxy=proxy, timeout=900, redirects=False,
                              headers=dict(h, **{"Content-Type": _mime(f, "video"), "Content-Range": f"bytes {sent}-{sent + n - 1}/{size}"}))
        if r.status in (200, 201):
            return _yt_done(st, r)
        if r.status != 308:
            st["sent"] = sent
            if r.status in (404, 410):
                st.pop("session", None)
            raise PostError(_g_err(r, "загрузка"))
        m = re.search(r"bytes=0-(\d+)", r.headers.get("range", ""))
        sent = int(m.group(1)) + 1 if m else sent + n
        st["sent"] = sent
    raise PostError("YouTube не подтвердил загрузку — нажми «Повторить»")


def _yt_done(st: dict, r: net.Resp) -> dict:
    j = r.json() or {}
    vid = j.get("id")
    if not vid:
        raise PostError("YouTube не вернул ролик — проверь «Творческую студию»")
    note = ""
    if (j.get("status") or {}).get("privacyStatus") == "private":
        note = ("Google оставил ролик приватным: проект API не прошёл проверку YouTube. Открой ролик в Творческой студии и сделай "
                "«Открытый доступ» или подай проект на аудит")
    st["video"] = vid
    return {"url": f"https://youtu.be/{vid}", "ext": f"yt:{vid}", "note": note}


# ================================================================== TikTok (Content Posting API)
TT_API = "https://open.tiktokapis.com/v2/"
TT_TOKEN = TT_API + "oauth/token/"
TT_PUBLISH_SCOPES = "video.publish,video.upload"
TT_ONE = 64 * 1024 * 1024           # до 64 МБ — одним куском
TT_CHUNK = 10 * 1024 * 1024


def _tt_code(r: net.Resp) -> tuple[str, str]:
    j = r.json() if isinstance(r.json(), dict) else {}
    e = j.get("error") or {}
    return (e.get("code") or ("ok" if r.ok else str(r.status))), (e.get("message") or "")


def _tt_human(code: str, msg: str) -> str:
    return {
        "scope_not_authorized": "TikTok: при входе не дали право публиковать — нажми «Разрешить публикацию» и поставь галочки",
        "access_token_invalid": "TikTok: вход устарел — войди через TikTok ещё раз",
        "spam_risk_too_many_posts": "TikTok: на сегодня лимит публикаций этого аккаунта исчерпан",
        "spam_risk_user_banned_from_posting": "TikTok запретил этому аккаунту публиковать через приложения",
        "reached_active_user_cap": "TikTok: дневной лимит приложения исчерпан — попробую позже",
        "url_ownership_unverified": "TikTok: для фото нужно подтвердить домен CRM в TikTok Developer Portal (Content Posting API → "
                                    "URL properties) — файл подтверждения CRM уже раздаёт",
        "privacy_level_option_mismatch": "TikTok: этот уровень приватности аккаунту недоступен",
        "rate_limit_exceeded": "TikTok: слишком часто — попробую позже",
    }.get(code, f"TikTok: {msg or code}"[:250])


async def tt_token(db, acc: dict, s: dict, proxy=None) -> str:
    from . import social
    d = acc["data"]
    if int(d.get("expires") or 0) > dbm.now() + 600:
        return d["access"]
    if not d.get("refresh"):
        raise PostError("TikTok: войди через TikTok ещё раз")
    r = await net.request("POST", TT_TOKEN, proxy=proxy, headers={"Content-Type": "application/x-www-form-urlencoded"},
                          data={"client_key": s.get("tt_client_key", ""), "client_secret": s.get("tt_client_secret", ""),
                                "grant_type": "refresh_token", "refresh_token": d["refresh"]})
    j = r.json() or {}
    if not j.get("access_token"):
        raise PostError("TikTok: не удалось продлить вход — войди ещё раз")
    d.update(access=j["access_token"], expires=dbm.now() + int(j.get("expires_in") or 86400))
    if j.get("refresh_token"):
        d["refresh"] = j["refresh_token"]
    if j.get("scope"):
        d["scope"] = j["scope"]
    await social.save_account(db, acc)
    return d["access"]


async def tt_publish(db, acc: dict, t: dict, st: dict, s: dict, proxy=None) -> dict:
    from . import publish
    tok = await tt_token(db, acc, s, proxy)
    h = {"Authorization": "Bearer " + tok, "Content-Type": "application/json; charset=UTF-8"}
    scope = acc["data"].get("scope") or ""
    cap = t["cap"]
    text = plain(cap.get("text", ""))[:2100]
    if not st.get("publish_id"):
        if t["ptype"] not in ("video", "image"):
            raise NeedManual("TikTok принимает только видео и фото")
        privacy, direct = "SELF_ONLY", "video.publish" in scope
        if direct:
            r = await net.request("POST", TT_API + "post/publish/creator_info/query/", headers=h, json={}, proxy=proxy)
            code, msg = _tt_code(r)
            if code != "ok":
                raise PostError(_tt_human(code, msg))
            opts = ((r.json() or {}).get("data") or {}).get("privacy_level_options") or []
            privacy = "PUBLIC_TO_EVERYONE" if "PUBLIC_TO_EVERYONE" in opts else (opts[0] if opts else "SELF_ONLY")
        if t["ptype"] == "image":
            if not cfg.public_url:
                raise PostError("TikTok: для фото нужен PUBLIC_URL — TikTok сам скачивает картинки с сервера CRM")
            imgs = [publish.ig_jpeg(f) for f in t["flist"][:10]]          # TikTok берёт JPEG/WEBP
            body = {"media_type": "PHOTO", "post_mode": "DIRECT_POST" if direct else "MEDIA_UPLOAD",
                    "post_info": {"title": plain(cap.get("title") or "")[:90], "description": text[:4000], "privacy_level": privacy,
                                  "disable_comment": False, "auto_add_music": True},
                    "source_info": {"source": "PULL_FROM_URL", "photo_cover_index": 0, "photo_images": imgs}}
            if not direct:
                body["post_info"].pop("privacy_level")
            r = await net.request("POST", TT_API + "post/publish/content/init/", headers=h, json=body, proxy=proxy)
            code, msg = _tt_code(r)
            if code != "ok":
                raise PostError(_tt_human(code, msg))
            st.update(publish_id=((r.json() or {}).get("data") or {}).get("publish_id"), mode="direct" if direct else "inbox",
                      started=dbm.now())
            return {"wait": True}
        f = t["flist"][0]
        size = _size(f)
        if size <= TT_ONE:
            chunk, count = size, 1
        else:
            chunk, count = TT_CHUNK, size // TT_CHUNK          # последний кусок забирает остаток
        src = {"source": "FILE_UPLOAD", "video_size": size, "chunk_size": chunk, "total_chunk_count": count}
        mode = "direct"
        if direct:
            r = await net.request("POST", TT_API + "post/publish/video/init/", headers=h, proxy=proxy,
                                  json={"post_info": {"title": text, "privacy_level": privacy, "disable_comment": False,
                                                      "disable_duet": False, "disable_stitch": False}, "source_info": src})
            code, msg = _tt_code(r)
            if code == "unaudited_client_can_only_post_to_private_accounts" and "video.upload" in scope:
                direct = False                                    # приложение без аудита → кладём в черновики TikTok
            elif code != "ok":
                raise PostError(_tt_human(code, msg))
        if not direct:
            mode = "inbox"
            r = await net.request("POST", TT_API + "post/publish/inbox/video/init/", headers=h, json={"source_info": src}, proxy=proxy)
            code, msg = _tt_code(r)
            if code != "ok":
                raise PostError(_tt_human(code, msg))
        d = (r.json() or {}).get("data") or {}
        up = d.get("upload_url")
        if not up:
            raise PostError("TikTok не дал адрес для загрузки — попробую позже")
        for i in range(count):
            a = i * chunk
            b = size - 1 if i == count - 1 else a + chunk - 1
            rr = await net.request("PUT", up, body=_chunk(f["path"], a, b - a + 1), proxy=proxy, timeout=900,
                                   headers={"Content-Type": _mime(f, "video"), "Content-Range": f"bytes {a}-{b}/{size}"})
            if rr.status >= 300:
                raise PostError(f"TikTok не принял файл ({rr.status}) — нажми «Повторить»")
        st.update(publish_id=d.get("publish_id"), mode=mode, started=dbm.now(), privacy=privacy)
        return {"wait": True}
    r = await net.request("POST", TT_API + "post/publish/status/fetch/", headers=h, json={"publish_id": st["publish_id"]}, proxy=proxy)
    code, msg = _tt_code(r)
    if code != "ok":
        raise PostError(_tt_human(code, msg))
    d = (r.json() or {}).get("data") or {}
    status = d.get("status")
    user = (acc.get("name") or "").lstrip("@")
    if status == "PUBLISH_COMPLETE":
        ids = d.get("publicaly_available_post_id") or d.get("publicly_available_post_id") or []
        pid = str(ids[0]) if ids else ""
        note = "" if st.get("privacy", "PUBLIC_TO_EVERYONE") == "PUBLIC_TO_EVERYONE" else \
            "TikTok опубликовал как «только я»: приложение ещё не прошло аудит TikTok"
        url = f"https://www.tiktok.com/@{user}/video/{pid}" if pid and user else f"https://www.tiktok.com/@{user}" if user else ""
        return {"url": url, "ext": f"tt:{pid or st['publish_id']}", "note": note}
    if status == "SEND_TO_USER_INBOX":
        return {"url": f"https://www.tiktok.com/@{user}" if user else "", "ext": f"tt:{st['publish_id']}",
                "note": "Ролик лежит в черновиках TikTok — открой приложение TikTok (уведомление «Входящие») и нажми «Опубликовать». "
                        "Сразу в ленту TikTok разрешит после аудита приложения"}
    if status == "FAILED":
        raise PostError(f"TikTok не опубликовал: {d.get('fail_reason') or 'ошибка обработки'}")
    if dbm.now() - int(st.get("started") or dbm.now()) > 45 * 60:
        raise PostError("TikTok слишком долго обрабатывает — проверь в приложении")
    return {"wait": True}


# ================================================================== VK (VK ID → токен пользователя)
VKID_AUTH = "https://id.vk.com/authorize"
VKID_TOKEN = "https://id.vk.com/oauth2/auth"
VK_API = "https://api.vk.com/method/"
VK_V = "5.199"
VK_SCOPES = "wall photos video docs groups"


async def vk_auth_url(db, s: dict, state: str) -> str:
    if not s.get("vk_app_id"):
        raise PostError("Вставь ID приложения VK ID (Настройки → VK)")
    verifier, challenge = _pkce()
    await _save_verifier(db, "vk_pkce_", state, verifier)
    return VKID_AUTH + "?" + urlencode({"response_type": "code", "client_id": s["vk_app_id"], "redirect_uri": _redirect("vkid"),
                                        "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
                                        "scope": VK_SCOPES})


async def _vk(method: str, token: str, proxy=None, **params) -> dict:
    r = await net.request("POST", VK_API + method, data=dict(params, access_token=token, v=VK_V), proxy=proxy)
    j = r.json() or {}
    if "error" in j:
        e = j["error"] if isinstance(j["error"], dict) else {"error_msg": str(j["error"])}
        code, msg = e.get("error_code"), e.get("error_msg", "")
        if code == 5:
            raise PostError("VK: вход устарел — нажми «Разрешить публикацию» у VK ещё раз")
        if code in (15, 27, 28, 200, 203, 204) and method.startswith("video."):
            raise NeedManual("VK: приложению не выдано право «video» — VK даёт его по заявке (devsupport@corp.vk.com). "
                             "Фото и текст CRM публикует сама, а это видео выложи вручную — файл и подпись ниже")
        if code in (15, 27, 214):
            raise PostError(f"VK: нет прав публиковать в этом сообществе (нужен админ или редактор): {msg}"[:250])
        if code == 7 or "permission" in msg.lower():
            raise PostError("VK: при входе не дали права (стена, фото) — включи их в настройках приложения VK ID и войди ещё раз")
        raise PostError(f"VK: {msg or code}"[:250])
    return j.get("response")


async def vk_exchange(db, s: dict, code: str, state: str, device_id: str, proxy=None) -> dict:
    from . import social
    verifier = await _pop_verifier(db, "vk_pkce_", state)
    if not verifier:
        raise PostError("VK: ссылка входа устарела — нажми «Разрешить публикацию» ещё раз")
    data = {"grant_type": "authorization_code", "code": code, "code_verifier": verifier, "client_id": s["vk_app_id"],
            "device_id": device_id, "redirect_uri": _redirect("vkid"), "state": state}
    if s.get("vk_app_secret"):
        data["client_secret"] = s["vk_app_secret"]
    r = await net.request("POST", VKID_TOKEN, data=data, proxy=proxy)
    j = r.json() or {}
    if not j.get("access_token"):
        raise PostError(f"VK: {j.get('error_description') or j.get('error') or r.status}"[:250])
    tok = {"vk_access": j["access_token"], "vk_refresh": j.get("refresh_token", ""), "vk_device": device_id,
           "vk_expires": dbm.now() + int(j.get("expires_in") or 3600), "vk_user": str(j.get("user_id") or ""),
           "vk_scope": j.get("scope", "")}
    groups = await _vk("groups.get", tok["vk_access"], proxy, filter="admin,editor", extended=1, count=100) or {}
    gl = groups.get("items") or []
    accs = await social.accounts(db, "vk")
    linked = []
    for a in accs:                         # подходящие уже добавленные сообщества получают права публикации
        ext = str(a.get("ext") or "")
        if ext.startswith("-") and any(str(-int(g["id"])) == ext for g in gl) or (ext and ext == tok["vk_user"]):
            a["data"].update(tok)
            await social.save_account(db, a)
            linked.append(a)
    if linked:
        return linked[0]
    if gl:                                  # сообществ в CRM ещё нет — добавим первое, где человек админ
        g = gl[0]
        return await social.add_account(db, "vk", ref=f"https://vk.com/{g.get('screen_name') or 'club' + str(g['id'])}",
                                        name=g.get("name") or "VK", data=tok, ext=str(-int(g["id"])))
    return await social.add_account(db, "vk", ref=f"https://vk.com/id{tok['vk_user']}", name="VK", data=tok, ext=tok["vk_user"])


async def vk_token(db, acc: dict, s: dict, proxy=None) -> str:
    from . import social
    d = acc["data"]
    if int(d.get("vk_expires") or 0) > dbm.now() + 300:
        return d["vk_access"]
    if not d.get("vk_refresh"):
        return d["vk_access"]
    data = {"grant_type": "refresh_token", "refresh_token": d["vk_refresh"], "client_id": s.get("vk_app_id", ""),
            "device_id": d.get("vk_device", ""), "state": secrets.token_urlsafe(12)}
    if s.get("vk_app_secret"):
        data["client_secret"] = s["vk_app_secret"]
    r = await net.request("POST", VKID_TOKEN, data=data, proxy=proxy)
    j = r.json() or {}
    if not j.get("access_token"):
        raise PostError("VK: не удалось продлить вход — нажми «Разрешить публикацию» у VK ещё раз")
    d.update(vk_access=j["access_token"], vk_refresh=j.get("refresh_token", d["vk_refresh"]),
             vk_expires=dbm.now() + int(j.get("expires_in") or 3600))
    await social.save_account(db, acc)
    return d["vk_access"]


async def _vk_upload(url: str, field: str, f: dict, ptype: str, proxy=None) -> dict:
    r = await net.request("POST", url, files=[(field, f.get("name") or os.path.basename(f["path"]), f["path"], _mime(f, ptype))],
                          proxy=proxy, timeout=900)
    j = r.json()
    if not r.ok or not isinstance(j, dict) or j.get("error"):
        raise PostError(f"VK не принял файл: {(j or {}).get('error') if isinstance(j, dict) else r.status}"[:200])
    return j


async def vk_publish(db, acc: dict, t: dict, st: dict, s: dict, proxy=None) -> dict:
    tok = await vk_token(db, acc, s, proxy)
    owner = int(acc.get("ext") or acc["data"].get("vk_user") or 0)
    if not owner:
        raise PostError("VK: не знаю, куда публиковать — обнови аккаунт VK в Настройках")
    gid = -owner if owner < 0 else None
    gp = {"group_id": gid} if gid else {}
    cap = t["cap"]
    text = plain(cap.get("text", ""))
    title = plain(cap.get("title") or t.get("title") or "")[:120]
    if t["ptype"] == "video":
        if not st.get("video"):
            f = t["flist"][0]
            v = await _vk("video.save", tok, proxy, name=title or text[:120] or "Видео", description=text[:5000], wallpost=1, **gp)
            res = await _vk_upload(v["upload_url"], "video_file", f, "video", proxy)
            st["video"] = f"{v.get('owner_id', owner)}_{res.get('video_id') or v.get('video_id')}"
        return {"url": f"https://vk.com/video{st['video']}", "ext": f"vkv:{st['video']}",
                "note": "VK опубликует ролик на стене, когда обработает его (обычно несколько минут)"}
    attach = []
    if t["ptype"] in ("image", "gif"):
        for f in t["flist"][:10]:
            if t["ptype"] == "gif":
                srv = await _vk("docs.getWallUploadServer", tok, proxy, **gp)
                up = await _vk_upload(srv["upload_url"], "file", f, "gif", proxy)
                doc = await _vk("docs.save", tok, proxy, file=up["file"], title=title or "GIF")
                dd = doc.get("doc") if isinstance(doc, dict) else (doc[0] if doc else {})
                attach.append(f"doc{dd['owner_id']}_{dd['id']}")
            else:
                srv = await _vk("photos.getWallUploadServer", tok, proxy, **gp)
                up = await _vk_upload(srv["upload_url"], "photo", f, "image", proxy)
                ph = await _vk("photos.saveWallPhoto", tok, proxy, photo=up["photo"], server=up["server"], hash=up["hash"], **gp)
                attach.append(f"photo{ph[0]['owner_id']}_{ph[0]['id']}")
    params = {"owner_id": owner, "message": text[:15000]}
    if gid:
        params["from_group"] = 1
    if attach:
        params["attachments"] = ",".join(attach)
    r = await _vk("wall.post", tok, proxy, **params)
    pid = (r or {}).get("post_id")
    if not pid:
        raise PostError("VK не вернул пост")
    return {"url": f"https://vk.com/wall{owner}_{pid}", "ext": f"vk:{owner}_{pid}"}


# ================================================================== X (OAuth 2.0 с PKCE)
X_AUTH = "https://x.com/i/oauth2/authorize"
X_API = "https://api.x.com/2/"
X_SCOPES = "tweet.read tweet.write users.read media.write offline.access"
X_CHUNK = 4 * 1024 * 1024


def _x_basic(s: dict) -> dict:
    if not s.get("x_client_secret"):
        return {}
    raw = f"{s['x_client_id']}:{s['x_client_secret']}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode()}


async def x_auth_url(db, s: dict, state: str) -> str:
    if not s.get("x_client_id"):
        raise PostError("Вставь Client ID (OAuth 2.0) приложения X (Настройки → X)")
    verifier, challenge = _pkce()
    await _save_verifier(db, "x_pkce_", state, verifier)
    return X_AUTH + "?" + urlencode({"response_type": "code", "client_id": s["x_client_id"], "redirect_uri": _redirect("x"),
                                     "scope": X_SCOPES, "state": state, "code_challenge": challenge, "code_challenge_method": "S256"})


def _x_human(r: net.Resp, what: str) -> str:
    j = r.json() if isinstance(r.json(), dict) else {}
    detail = str(j.get("detail") or j.get("error_description") or j.get("title") or
                 ((j.get("errors") or [{}])[0] or {}).get("message") or r.text[:150])
    if r.status == 401:
        return "X: вход устарел — нажми «Разрешить публикацию» у X ещё раз"
    if r.status == 402 or "credit" in detail.lower():
        return "X: на счёте API нет кредитов — публикация в X платная, пополни баланс в console.x.com"
    if r.status == 429:
        return "X: лимит публикаций исчерпан — попробую позже"
    if r.status == 403 and "duplicate" in detail.lower():
        return "X: такой же пост уже есть — X не даёт публиковать дубли"
    return f"X ({what}): {detail}"[:250]


async def x_exchange(db, s: dict, code: str, state: str, proxy=None) -> dict:
    from . import social
    verifier = await _pop_verifier(db, "x_pkce_", state)
    if not verifier:
        raise PostError("X: ссылка входа устарела — нажми «Разрешить публикацию» ещё раз")
    r = await net.request("POST", X_API + "oauth2/token", headers=_x_basic(s), proxy=proxy,
                          data={"code": code, "grant_type": "authorization_code", "client_id": s["x_client_id"],
                                "redirect_uri": _redirect("x"), "code_verifier": verifier})
    j = r.json() or {}
    if not j.get("access_token"):
        raise PostError(_x_human(r, "вход"))
    data = {"x_access": j["access_token"], "x_refresh": j.get("refresh_token", ""), "x_expires": dbm.now() + int(j.get("expires_in") or 7200)}
    me = await net.request("GET", X_API + "users/me", headers={"Authorization": "Bearer " + data["x_access"]}, proxy=proxy)
    u = (me.json() or {}).get("data") or {}
    if not u.get("id"):
        raise PostError(_x_human(me, "профиль"))
    return await social.add_account(db, "x", ref="@" + u.get("username", ""), name="@" + u.get("username", ""), data=data, ext=str(u["id"]))


async def x_token(db, acc: dict, s: dict, proxy=None) -> str:
    from . import social
    d = acc["data"]
    if int(d.get("x_expires") or 0) > dbm.now() + 120:
        return d["x_access"]
    if not d.get("x_refresh"):
        raise PostError("X: нажми «Разрешить публикацию» у X ещё раз")
    r = await net.request("POST", X_API + "oauth2/token", headers=_x_basic(s), proxy=proxy,
                          data={"grant_type": "refresh_token", "refresh_token": d["x_refresh"], "client_id": s.get("x_client_id", "")})
    j = r.json() or {}
    if not j.get("access_token"):
        raise PostError(_x_human(r, "продление входа"))
    d.update(x_access=j["access_token"], x_refresh=j.get("refresh_token", d["x_refresh"]),
             x_expires=dbm.now() + int(j.get("expires_in") or 7200))
    await social.save_account(db, acc)
    return d["x_access"]


def x_fit(text: str, link: str = "", limit: int = 280) -> str:
    """X считает каждую ссылку за 23 символа."""
    def weight(s):
        return len(re.sub(r"https?://\S+", "x" * 23, s))
    text = plain(text).strip()
    if link and link not in text:
        text = (text + "\n" + link).strip()
    while weight(text) > limit:
        urls = re.findall(r"https?://\S+", text)
        tail = ("\n" + urls[-1]) if urls and text.endswith(urls[-1]) else ""
        body = text[: len(text) - len(tail)] if tail else text
        body = body[: max(0, len(body) - (weight(text) - limit) - 2)].rstrip() + "…"
        text = body + tail
    return text


async def x_publish(db, acc: dict, t: dict, st: dict, s: dict, proxy=None) -> dict:
    tok = await x_token(db, acc, s, proxy)
    h = {"Authorization": "Bearer " + tok}
    ptype = t["ptype"]
    media = st.setdefault("media", [])
    files = t["flist"][:4 if ptype == "image" else 1] if ptype != "text" else []
    for f in files:
        m = next((x for x in media if x["token"] == f["token"]), None)
        if m and m.get("ready"):
            continue
        if ptype == "image" and not m:
            r = await net.request("POST", X_API + "media/upload", headers=h, proxy=proxy, timeout=300,
                                  data={"media_category": "tweet_image"},
                                  files=[("media", f.get("name") or "image.jpg", f["path"], _mime(f, "image"))])
            mid = ((r.json() or {}).get("data") or {}).get("id")
            if not mid:
                raise PostError(_x_human(r, "фото"))
            media.append({"token": f["token"], "id": mid, "ready": True})
            continue
        if not m:
            size = _size(f)
            r = await net.request("POST", X_API + "media/upload/initialize", headers=h, proxy=proxy,
                                  json={"media_type": _mime(f, ptype), "total_bytes": size,
                                        "media_category": "tweet_gif" if ptype == "gif" else "tweet_video"})
            mid = ((r.json() or {}).get("data") or {}).get("id")
            if not mid:
                raise PostError(_x_human(r, "видео"))
            for i, a in enumerate(range(0, size, X_CHUNK)):
                rr = await net.request("POST", X_API + f"media/upload/{mid}/append", headers=h, proxy=proxy, timeout=600,
                                       data={"segment_index": i},
                                       files=[("media", "chunk", _chunk(f["path"], a, X_CHUNK), "application/octet-stream")])
                if rr.status >= 300:
                    raise PostError(_x_human(rr, "загрузка видео"))
            rr = await net.request("POST", X_API + f"media/upload/{mid}/finalize", headers=h, proxy=proxy)
            if rr.status >= 300:
                raise PostError(_x_human(rr, "обработка видео"))
            pi = ((rr.json() or {}).get("data") or {}).get("processing_info") or {}
            m = {"token": f["token"], "id": mid, "ready": not pi or pi.get("state") == "succeeded", "started": dbm.now()}
            media.append(m)
        if not m.get("ready"):
            rr = await net.request("GET", X_API + "media/upload", params={"command": "STATUS", "media_id": m["id"]}, headers=h, proxy=proxy)
            pi = ((rr.json() or {}).get("data") or {}).get("processing_info") or {}
            if pi.get("state") == "failed":
                raise PostError(f"X не обработал видео: {(pi.get('error') or {}).get('message') or 'формат не подошёл'}")
            if pi and pi.get("state") != "succeeded":
                if dbm.now() - int(m.get("started") or dbm.now()) > 30 * 60:
                    raise PostError("X слишком долго обрабатывает видео")
                return {"wait": True}
            m["ready"] = True
    cap = t["cap"]
    body = {"text": x_fit(cap.get("text", ""), cap.get("link", ""))}
    if media:
        body["media"] = {"media_ids": [m["id"] for m in media]}
    r = await net.request("POST", X_API + "tweets", headers=h, json=body, proxy=proxy)
    tid = ((r.json() or {}).get("data") or {}).get("id")
    if not tid:
        raise PostError(_x_human(r, "пост"))
    user = (acc.get("name") or acc.get("ref") or "").lstrip("@") or "i"
    note = "Если работа 18+ — включи в X «Отмечать медиа как деликатное» (Настройки → Конфиденциальность → Ваши посты)" \
        if t.get("adult") else ""
    return {"url": f"https://x.com/{user}/status/{tid}", "ext": f"x:{tid}", "note": note}


# ================================================================== Fur Affinity (по cookie, как браузер)
FA = "https://www.furaffinity.net"
FA_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
FA_RATING = {"": "0", "mature": "2", "adult": "1"}       # General / Mature / Adult — коды формы Fur Affinity


def _inputs(html: str, form_hint: str = "") -> dict:
    """Скрытые и обычные поля формы (берём форму, где action содержит form_hint)."""
    block = html
    if form_hint:
        m = re.search(r"<form[^>]*action=[\"'][^\"']*" + re.escape(form_hint) + r"[^\"']*[\"'][^>]*>(.*?)</form>", html, re.S | re.I)
        if m:
            block = m.group(1)
    out = {}
    for tag in re.findall(r"<input[^>]+>", block, re.I):
        name = re.search(r"name=[\"']([^\"']+)", tag)
        if not name:
            continue
        typ = (re.search(r"type=[\"']([^\"']+)", tag) or [None, "text"])[1].lower()
        if typ in ("file", "submit", "button", "image"):
            continue
        if typ in ("checkbox", "radio") and not re.search(r"\bchecked\b", tag, re.I):
            continue
        val = re.search(r"value=[\"']([^\"']*)", tag)
        out[name.group(1)] = _html.unescape(val.group(1)) if val else ""
    return out


def _fa_msg(html: str) -> str:
    m = re.search(r'class="[^"]*(?:redirect-message|notice-message|section-body alignleft)[^"]*"[^>]*>(.*?)</', html or "", re.S | re.I)
    txt = re.sub(r"<[^>]+>", " ", m.group(1)) if m else ""
    return _html.unescape(re.sub(r"\s+", " ", txt)).strip()[:180]


async def fa_publish(db, acc: dict, t: dict, st: dict, s: dict, proxy=None) -> dict:
    cookie = acc["data"].get("cookie") or ""
    if not cookie:
        raise PostError("Fur Affinity: вставь cookie аккаунта (⚙ у аккаунта) — без входа загрузить нельзя")
    if t["ptype"] not in ("image", "gif"):
        raise NeedManual("Fur Affinity: CRM загружает только картинки и GIF — текст выложи журналом вручную")
    h = {"User-Agent": FA_UA, "Cookie": cookie, "Referer": FA + "/submit/"}
    f = t["flist"][0]
    r = await net.request("GET", FA + "/submit/", headers=h, proxy=proxy, timeout=60)
    if r.status in (401, 403) or "logout" not in r.text.lower():
        raise PostError("Fur Affinity: cookie не подходит или устарел — скопируй a и b заново (⚙ у аккаунта)")
    form = _inputs(r.text, "/submit/upload")
    if not form.get("key"):
        raise PostError("Fur Affinity: не нашёл форму загрузки — возможно, сайт изменился. Выложи вручную, подписи в карточке поста")
    form["submission_type"] = "submission"
    r = await net.request("POST", FA + "/submit/upload/", data=form, headers=h, proxy=proxy, timeout=900,
                          files=[("submission", f.get("name") or os.path.basename(f["path"]), f["path"], _mime(f, "image"))])
    fin = _inputs(r.text, "/submit/finalize")
    if not fin.get("key"):
        raise PostError(f"Fur Affinity не принял файл: {_fa_msg(r.text) or r.status}")
    cap = t["cap"]
    tags = [x for x in re.split(r"[\s,]+", (cap.get("tags") or t.get("tags") or "").replace("#", "")) if x][:60]
    fin.update({"title": plain(cap.get("title") or t.get("title") or "Untitled")[:60], "message": plain(cap.get("text", ""))[:60000],
                "keywords": " ".join(tags), "rating": FA_RATING.get(t.get("adult") or "", "0")})
    for k, v in (("cat", "1"), ("atype", "1"), ("species", "1"), ("gender", "0")):
        fin.setdefault(k, v)
    r = await net.request("POST", FA + "/submit/finalize/", data=fin, headers=h, proxy=proxy, timeout=120)
    m = re.search(r"/view/(\d+)", getattr(r, "url", "") or "") or re.search(r"(?:og:url|canonical)[^>]+/view/(\d+)", r.text)
    if not m:
        raise PostError(f"Fur Affinity не опубликовал: {_fa_msg(r.text) or 'неизвестная ошибка'} — выложи вручную")
    return {"url": f"{FA}/view/{m.group(1)}/", "ext": f"fa:{m.group(1)}"}


PUBLISHERS = {"yt": yt_publish, "tt": tt_publish, "vk": vk_publish, "x": x_publish, "fa": fa_publish}
