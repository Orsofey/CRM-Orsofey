"""Bluesky (AT Protocol): вход одной кнопкой (OAuth с DPoP) или паролем приложения, статистика постов, публикация.

OAuth у Bluesky устроен строже обычного: приложение описано JSON-файлом на нашем сайте (client-metadata.json),
каждый запрос подписывается ключом сессии (DPoP), а сервер выдаёт «nonce», который надо подставлять в подпись.
Ключ приложения (для подписи client_assertion) создаётся сам и лежит в настройках автора — общий для всех CRM.
"""
import base64, hashlib, io, json, logging, re, secrets, time
from urllib.parse import quote, urlparse
from . import db as dbm, net
from .config import cfg

log = logging.getLogger(__name__)
ENTRYWAY = "https://bsky.social"
PUBLIC_API = "https://public.api.bsky.app"
VIDEO_SVC = "https://video.bsky.app"
PLC = "https://plc.directory"
SCOPE = "atproto transition:generic"
IMG_MAX = 950_000          # Bluesky принимает картинки до ~1 МБ
TEXT_MAX = 300


class BskyError(Exception):
    pass


# ------------------------------------------------------------------ подписи (ES256)
def b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def new_key() -> dict:
    from cryptography.hazmat.primitives.asymmetric import ec
    k = ec.generate_private_key(ec.SECP256R1())
    n = k.private_numbers()
    return {"kty": "EC", "crv": "P-256", "x": b64u(n.public_numbers.x.to_bytes(32, "big")),
            "y": b64u(n.public_numbers.y.to_bytes(32, "big")), "d": b64u(n.private_value.to_bytes(32, "big")),
            "kid": secrets.token_hex(8)}


def public_jwk(j: dict, with_kid: bool = False) -> dict:
    out = {"kty": "EC", "crv": "P-256", "x": j["x"], "y": j["y"]}
    if with_kid:
        out.update(kid=j["kid"], use="sig", alg="ES256")
    return out


def sign_jwt(jwk: dict, header: dict, claims: dict) -> str:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
    h = b64u(json.dumps(header, separators=(",", ":")).encode())
    p = b64u(json.dumps(claims, separators=(",", ":")).encode())
    key = ec.derive_private_key(int.from_bytes(b64d(jwk["d"]), "big"), ec.SECP256R1())
    r, s = decode_dss_signature(key.sign(f"{h}.{p}".encode(), ec.ECDSA(hashes.SHA256())))
    return f"{h}.{p}.{b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"


def dpop_proof(jwk: dict, method: str, url: str, nonce: str | None = None, access: str | None = None) -> str:
    claims = {"jti": secrets.token_urlsafe(16), "htm": method, "htu": url.split("?")[0].split("#")[0], "iat": int(time.time())}
    if nonce:
        claims["nonce"] = nonce
    if access:
        claims["ath"] = b64u(hashlib.sha256(access.encode()).digest())
    return sign_jwt(jwk, {"typ": "dpop+jwt", "alg": "ES256", "jwk": public_jwk(jwk)}, claims)


# ------------------------------------------------------------------ наше приложение (client metadata)
def client_id() -> str:
    return f"{cfg.public_url}/oauth/bluesky/client-metadata.json"


def redirect_uri() -> str:
    return f"{cfg.public_url}/oauth/bluesky"


def client_metadata() -> dict:
    return {"client_id": client_id(), "client_name": "Orsofey CRM", "client_uri": cfg.public_url,
            "application_type": "web", "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
            "redirect_uris": [redirect_uri()], "scope": SCOPE, "dpop_bound_access_tokens": True,
            "token_endpoint_auth_method": "private_key_jwt", "token_endpoint_auth_signing_alg": "ES256",
            "jwks_uri": f"{cfg.public_url}/oauth/bluesky/jwks.json",
            "tos_uri": f"{cfg.public_url}/terms", "policy_uri": f"{cfg.public_url}/privacy"}


async def client_key(main_db) -> dict:
    """Ключ приложения — один на весь сервер (в настройках автора). Создаётся при первом входе."""
    raw = await dbm.get_setting(main_db, "bs_client_jwk", "")
    if raw:
        return json.loads(raw)
    k = new_key()
    await dbm.set_setting(main_db, "bs_client_jwk", json.dumps(k))
    return k


async def _main_db():
    from . import workspaces
    return workspaces.main_db()


def _assertion(ckey: dict, aud: str) -> dict:
    claims = {"iss": client_id(), "sub": client_id(), "aud": aud, "jti": secrets.token_urlsafe(16), "iat": int(time.time()),
              "exp": int(time.time()) + 300}
    return {"client_id": client_id(), "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": sign_jwt(ckey, {"alg": "ES256", "kid": ckey["kid"]}, claims)}


# ------------------------------------------------------------------ кто это (handle → DID → сервер)
async def _get_json(url: str, params=None, proxy=None):
    r = await net.request("GET", url, params=params, proxy=proxy, headers={"Accept": "application/json"})
    if not r.ok:
        raise BskyError(f"Bluesky: {url.split('/')[2]} ответил {r.status}")
    return r.json() or {}


async def resolve_handle(handle: str, proxy=None) -> str:
    handle = handle.strip().lstrip("@")
    if handle.startswith("did:"):
        return handle
    if "." not in handle:
        handle += ".bsky.social"
    d = await _get_json(f"{PUBLIC_API}/xrpc/com.atproto.identity.resolveHandle", {"handle": handle}, proxy)
    if not d.get("did"):
        raise BskyError(f"Bluesky: аккаунт «{handle}» не найден")
    return d["did"]


async def did_doc(did: str, proxy=None) -> dict:
    if did.startswith("did:plc:"):
        return await _get_json(f"{PLC}/{did}", proxy=proxy)
    if did.startswith("did:web:"):
        return await _get_json(f"https://{did[8:]}/.well-known/did.json", proxy=proxy)
    raise BskyError("Bluesky: неизвестный тип DID")


def pds_of(doc: dict) -> str:
    for s in doc.get("service") or []:
        if s.get("id", "").endswith("#atproto_pds") or s.get("type") == "AtprotoPersonalDataServer":
            return s["serviceEndpoint"].rstrip("/")
    raise BskyError("Bluesky: у аккаунта не найден сервер данных")


def handle_of(doc: dict) -> str:
    for a in doc.get("alsoKnownAs") or []:
        if a.startswith("at://"):
            return a[5:]
    return ""


async def issuer_of_pds(pds: str, proxy=None) -> str:
    d = await _get_json(f"{pds}/.well-known/oauth-protected-resource", proxy=proxy)
    return (d.get("authorization_servers") or [""])[0].rstrip("/")


async def as_meta(issuer: str, proxy=None) -> dict:
    return await _get_json(f"{issuer}/.well-known/oauth-authorization-server", proxy=proxy)


# ------------------------------------------------------------------ запросы с DPoP (повтор с nonce)
async def _dpop_post(url: str, form: dict, key: dict, nonce: str | None, proxy=None) -> tuple[net.Resp, str | None]:
    for _ in range(2):
        r = await net.request("POST", url, data=form, proxy=proxy,
                              headers={"DPoP": dpop_proof(key, "POST", url, nonce), "Content-Type": "application/x-www-form-urlencoded"})
        new = r.headers.get("dpop-nonce")
        if r.status == 400 and (r.json() or {}).get("error") == "use_dpop_nonce" and new:
            nonce = new
            continue
        return r, new or nonce
    return r, nonce


def _oauth_err(r: net.Resp, what: str):
    j = r.json() or {}
    raise BskyError(f"Bluesky ({what}): {j.get('error_description') or j.get('error') or r.status}"[:200])


async def oauth_start(db, state: str, handle: str = "", proxy=None) -> str:
    """Начать вход: вернуть ссылку на страницу Bluesky «Разрешить доступ»."""
    if not cfg.public_url.startswith("https://"):
        raise BskyError("Для входа через Bluesky нужен PUBLIC_URL с https://")
    issuer, did = ENTRYWAY, None
    if handle.strip():
        did = await resolve_handle(handle, proxy)
        issuer = await issuer_of_pds(pds_of(await did_doc(did, proxy)), proxy) or ENTRYWAY
    meta = await as_meta(issuer, proxy)
    ckey = await client_key(await _main_db())
    dkey = new_key()
    verifier = secrets.token_urlsafe(48)
    form = {"response_type": "code", "code_challenge": b64u(hashlib.sha256(verifier.encode()).digest()),
            "code_challenge_method": "S256", "state": state, "redirect_uri": redirect_uri(), "scope": SCOPE}
    if handle.strip():
        form["login_hint"] = handle.strip().lstrip("@")
    form.update(_assertion(ckey, meta.get("issuer", issuer)))
    r, nonce = await _dpop_post(meta["pushed_authorization_request_endpoint"], form, dkey, None, proxy)
    if not r.ok:
        _oauth_err(r, "вход")
    req_uri = (r.json() or {}).get("request_uri")
    pending = {"verifier": verifier, "dpop": dkey, "nonce": nonce, "issuer": meta.get("issuer", issuer),
               "token_endpoint": meta["token_endpoint"], "did": did, "at": dbm.now()}
    await dbm.set_setting(db, "bs_pending_" + hashlib.sha256(state.encode()).hexdigest()[:20], json.dumps(pending))
    return f"{meta['authorization_endpoint']}?client_id={quote(client_id(), safe='')}&request_uri={quote(req_uri, safe='')}"


async def oauth_finish(db, code: str, state: str, iss: str = "", proxy=None) -> dict:
    from . import social
    key = "bs_pending_" + hashlib.sha256(state.encode()).hexdigest()[:20]
    raw = await dbm.get_setting(db, key, "")
    if not raw:
        raise BskyError("Вход устарел — нажми «Войти через Bluesky» ещё раз")
    p = json.loads(raw)
    await dbm.set_setting(db, key, "")
    if iss and iss.rstrip("/") != p["issuer"].rstrip("/"):
        raise BskyError("Bluesky: ответ пришёл не от того сервера — войди ещё раз")
    ckey = await client_key(await _main_db())
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri(), "code_verifier": p["verifier"]}
    form.update(_assertion(ckey, p["issuer"]))
    r, nonce = await _dpop_post(p["token_endpoint"], form, p["dpop"], p.get("nonce"), proxy)
    if not r.ok:
        _oauth_err(r, "доступ")
    t = r.json() or {}
    did = t.get("sub") or ""
    if p.get("did") and did != p["did"]:
        raise BskyError("Bluesky: вошли не тем аккаунтом, который указали")
    doc = await did_doc(did, proxy)
    pds = pds_of(doc)
    if (await issuer_of_pds(pds, proxy)).rstrip("/") != p["issuer"].rstrip("/"):
        raise BskyError("Bluesky: сервер аккаунта не подтвердил вход — попробуй ещё раз")
    handle = handle_of(doc) or did
    data = {"auth": "oauth", "did": did, "handle": handle, "pds": pds, "issuer": p["issuer"], "token_endpoint": p["token_endpoint"],
            "access": t["access_token"], "refresh": t.get("refresh_token", ""), "expires": dbm.now() + int(t.get("expires_in") or 3600),
            "dpop": p["dpop"], "nonce_as": nonce, "nonce_pds": None}
    return await social.add_account(db, "bs", name="@" + handle, data=data, ext=did)


async def password_login(db, identifier: str, app_password: str, proxy=None) -> dict:
    """Запасной вход: имя + пароль приложения (Настройки Bluesky → Конфиденциальность → Пароли приложений)."""
    from . import social
    ident = identifier.strip().lstrip("@")
    did = await resolve_handle(ident, proxy)
    pds = pds_of(await did_doc(did, proxy))
    r = await net.request("POST", f"{pds}/xrpc/com.atproto.server.createSession", json={"identifier": did, "password": app_password.strip()},
                          proxy=proxy)
    j = r.json() or {}
    if not r.ok:
        if j.get("error") == "AuthFactorTokenRequired":
            raise BskyError("Bluesky просит код из почты — используй «пароль приложения», а не основной пароль")
        raise BskyError("Bluesky: неверное имя или пароль приложения" if r.status == 401 else f"Bluesky: {j.get('message') or r.status}")
    data = {"auth": "password", "did": j.get("did", did), "handle": j.get("handle", ident), "pds": pds,
            "access": j["accessJwt"], "refresh": j["refreshJwt"], "password": app_password.strip()}
    return await social.add_account(db, "bs", name="@" + data["handle"], data=data, ext=data["did"])


# ------------------------------------------------------------------ запросы к своему серверу от имени аккаунта
async def _refresh(db, acc: dict, proxy=None):
    from . import social
    d = acc["data"]
    if d.get("auth") == "oauth":
        ckey = await client_key(await _main_db())
        form = {"grant_type": "refresh_token", "refresh_token": d["refresh"]}
        form.update(_assertion(ckey, d["issuer"]))
        r, nonce = await _dpop_post(d["token_endpoint"], form, d["dpop"], d.get("nonce_as"), proxy)
        d["nonce_as"] = nonce
        if not r.ok:
            await social.save_account(db, acc)
            raise BskyError("Bluesky: вход устарел — нажми «Войти через Bluesky» ещё раз")
        t = r.json() or {}
        d.update(access=t["access_token"], refresh=t.get("refresh_token", d["refresh"]),
                 expires=dbm.now() + int(t.get("expires_in") or 3600))
    else:
        r = await net.request("POST", f"{d['pds']}/xrpc/com.atproto.server.refreshSession",
                              headers={"Authorization": "Bearer " + d["refresh"]}, proxy=proxy)
        if r.ok:
            j = r.json() or {}
            d.update(access=j["accessJwt"], refresh=j["refreshJwt"])
        else:                                    # refresh истёк — входим заново сохранённым паролем приложения
            r = await net.request("POST", f"{d['pds']}/xrpc/com.atproto.server.createSession",
                                  json={"identifier": d["did"], "password": d.get("password", "")}, proxy=proxy)
            if not r.ok:
                raise BskyError("Bluesky: пароль приложения больше не подходит — добавь аккаунт заново")
            j = r.json() or {}
            d.update(access=j["accessJwt"], refresh=j["refreshJwt"])
    await social.save_account(db, acc)


async def call(db, acc: dict, method: str, url: str, *, params=None, json_body=None, body: bytes | None = None,
               content_type: str | None = None, proxy=None) -> net.Resp:
    """Запрос к серверу данных аккаунта (PDS) с авторизацией; сам обновляет доступ и nonce."""
    from . import social
    d = acc["data"]
    if d.get("auth") == "oauth" and int(d.get("expires") or 0) < dbm.now() + 60:
        await _refresh(db, acc, proxy)
    for attempt in range(3):
        h = {}
        if content_type:
            h["Content-Type"] = content_type
        if d.get("auth") == "oauth":
            h["Authorization"] = "DPoP " + d["access"]
            h["DPoP"] = dpop_proof(d["dpop"], method, url, d.get("nonce_pds"), d["access"])
        else:
            h["Authorization"] = "Bearer " + d["access"]
        r = await net.request(method, url, params=params, json=json_body, body=body, headers=h, proxy=proxy)
        if d.get("auth") == "oauth" and r.headers.get("dpop-nonce") and r.headers.get("dpop-nonce") != d.get("nonce_pds"):
            d["nonce_pds"] = r.headers["dpop-nonce"]
            await social.save_account(db, acc)
            if r.status == 401 and "use_dpop_nonce" in (r.headers.get("www-authenticate", "") + r.text):
                continue
        j = r.json() if r.status in (400, 401) else None
        if r.status in (400, 401) and attempt < 2 and (isinstance(j, dict) and j.get("error") in ("ExpiredToken", "InvalidToken", "invalid_token")
                                                         or "invalid_token" in r.headers.get("www-authenticate", "")):
            await _refresh(db, acc, proxy)
            continue
        return r
    return r


def xrpc(acc: dict, nsid: str) -> str:
    return f"{acc['data']['pds']}/xrpc/{nsid}"


# ------------------------------------------------------------------ статистика
async def sync(db, acc: dict, proxy=None, max_posts: int = 200) -> dict:
    from . import social
    d = acc["data"]
    did = d.get("did") or acc.get("ext")
    prof = await _get_json(f"{PUBLIC_API}/xrpc/app.bsky.actor.getProfile", {"actor": did}, proxy)
    handle = prof.get("handle") or d.get("handle") or did
    items, cursor = [], None
    while len(items) < max_posts:
        params = {"actor": did, "limit": 100, "filter": "posts_no_replies"}
        if cursor:
            params["cursor"] = cursor
        page = await _get_json(f"{PUBLIC_API}/xrpc/app.bsky.feed.getAuthorFeed", params, proxy)
        for it in page.get("feed") or []:
            post = it.get("post") or {}
            if it.get("reason") or (post.get("author") or {}).get("did") != did:
                continue                         # репосты чужих постов не считаем
            rec = post.get("record") or {}
            rkey = post.get("uri", "").rsplit("/", 1)[-1]
            uris = [f.get("uri", "") for fc in rec.get("facets") or [] for f in fc.get("features") or []]
            ext = ((rec.get("embed") or {}).get("external") or {}).get("uri", "")
            text = (rec.get("text") or "") + " " + " ".join(uris) + " " + ext
            emb = (rec.get("embed") or {}).get("$type", "")
            kind = "Видео" if "video" in emb else "Фото" if "images" in emb else "Пост"
            items.append({"ext_id": f"bs:{rkey}", "vid": rkey, "platform": "Bluesky", "kind": "Пост",
                          "title": (rec.get("text") or kind).split("\n")[0][:120], "codes": social.codes_in(text), "text": text,
                          "url": f"https://bsky.app/profile/{handle}/post/{rkey}", "views": 0,
                          "likes": int(post.get("likeCount") or 0) + int(post.get("repostCount") or 0),
                          "comments": int(post.get("replyCount") or 0), "published_at": social._iso_ts(rec.get("createdAt"))})
        cursor = page.get("cursor")
        if not cursor or not page.get("feed"):
            break
    social.assign_codes(items, lambda it: "bs_" + it["vid"], await social.keyword_map(db))
    await social.upsert_items(db, items, "bluesky", acc["id"])
    await social.record_channel(db, "Bluesky", prof.get("followersCount"), account=acc["id"])
    acc["name"], d["handle"] = "@" + handle, handle
    return {"ok": True, "title": "@" + handle, "subscribers": prof.get("followersCount"), "videos": len(items),
            "linked": sum(1 for it in items if not it.get("default"))}


# ------------------------------------------------------------------ публикация
URL_RE = re.compile(r"https?://[^\s<>\"]+")
TAG_RE = re.compile(r"(?<![\w#])#([\wА-Яа-яЁё]{1,64})")


def facets(text: str) -> list:
    out = []
    for m in URL_RE.finditer(text):
        url = m.group(0).rstrip(".,;:!?)»")
        s = len(text[:m.start()].encode())
        out.append({"index": {"byteStart": s, "byteEnd": s + len(url.encode())},
                    "features": [{"$type": "app.bsky.richtext.facet#link", "uri": url}]})
    for m in TAG_RE.finditer(text):
        s = len(text[:m.start()].encode())
        out.append({"index": {"byteStart": s, "byteEnd": s + len(m.group(0).encode())},
                    "features": [{"$type": "app.bsky.richtext.facet#tag", "tag": m.group(1)}]})
    return out


def fit_text(text: str, limit: int = TEXT_MAX) -> str:
    """Bluesky — до 300 знаков. Ссылку на бота в конце сохраняем, обрезаем текст перед ней."""
    if len(text) <= limit:
        return text
    m = list(URL_RE.finditer(text))
    tail = text[m[-1].start() - 3:] if m and len(text) - m[-1].start() < 120 else ""
    room = limit - len(tail) - 1
    return (text[:room].rstrip() + "…" + tail)[:limit]


def jpeg_under(path: str, limit: int = IMG_MAX) -> tuple[bytes, int, int]:
    from PIL import Image
    with Image.open(path) as im:
        im = im.convert("RGB")
        w, h = im.size
        scale = min(1.0, 2000 / max(w, h))
        if scale < 1:
            im = im.resize((int(w * scale), int(h * scale)))
        q = 90
        while True:
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=q)
            if buf.tell() <= limit or q <= 40:
                if buf.tell() > limit:
                    im = im.resize((im.size[0] * 3 // 4, im.size[1] * 3 // 4))
                    q = 80
                    continue
                return buf.getvalue(), im.size[0], im.size[1]
            q -= 10


def labels(adult: str):
    if adult not in ("mature", "adult"):
        return None
    return {"$type": "com.atproto.label.defs#selfLabels", "values": [{"val": "porn" if adult == "adult" else "sexual"}]}


async def publish(db, acc: dict, t: dict, state: dict, proxy=None) -> dict:
    """Один шаг публикации в один аккаунт. state — что уже сделано (для видео: ждём обработку).
    Возвращает {"url":…, "ext":…} когда готово, или {"wait": True, …} пока видео обрабатывается."""
    d = acc["data"]
    text = fit_text(t["cap_text"])
    record = {"$type": "app.bsky.feed.post", "text": text, "createdAt": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
              "langs": ["ru"]}
    f = facets(text)
    if f:
        record["facets"] = f
    lab = labels(t.get("adult", ""))
    if lab:
        record["labels"] = lab
    files = t["flist"]
    if t["ptype"] == "image" and files:
        imgs = []
        for fl in files[:4]:
            data, w, h = jpeg_under(fl["path"])
            r = await call(db, acc, "POST", xrpc(acc, "com.atproto.repo.uploadBlob"), body=data, content_type="image/jpeg", proxy=proxy)
            if not r.ok:
                raise BskyError(f"Bluesky не принял фото ({r.status})")
            imgs.append({"alt": "", "image": r.json()["blob"], "aspectRatio": {"width": w, "height": h}})
        record["embed"] = {"$type": "app.bsky.embed.images", "images": imgs}
    elif t["ptype"] in ("video", "gif") and files:
        fl = files[0]
        if not state.get("blob"):
            if not state.get("job"):
                host = urlparse(d["pds"]).hostname
                r = await call(db, acc, "GET", xrpc(acc, "com.atproto.server.getServiceAuth"), proxy=proxy,
                               params={"aud": f"did:web:{host}", "lxm": "com.atproto.repo.uploadBlob", "exp": dbm.now() + 1800})
                if not r.ok:
                    raise BskyError(f"Bluesky: нет доступа к загрузке видео ({r.status})")
                svc = r.json()["token"]
                mime = "image/gif" if fl["path"].lower().endswith(".gif") else "video/mp4"
                with open(fl["path"], "rb") as fh:
                    blob = fh.read()
                r = await net.request("POST", f"{VIDEO_SVC}/xrpc/app.bsky.video.uploadVideo", proxy=proxy, body=blob,
                                      params={"did": d["did"], "name": secrets.token_hex(8) + (".gif" if mime == "image/gif" else ".mp4")},
                                      headers={"Authorization": "Bearer " + svc, "Content-Type": mime}, timeout=600)
                j = r.json() or {}
                job = j.get("jobId") or (j.get("jobStatus") or {}).get("jobId")
                if not job:
                    raise BskyError(f"Bluesky не принял видео: {j.get('message') or j.get('error') or r.status}. "
                                    f"Видео — до 3 минут и 100 МБ")
                state.update(job=job, started=dbm.now())
                return {"wait": True}
            r = await net.request("GET", f"{VIDEO_SVC}/xrpc/app.bsky.video.getJobStatus", params={"jobId": state["job"]}, proxy=proxy)
            js = (r.json() or {}).get("jobStatus") or {}
            if js.get("state") == "JOB_STATE_FAILED":
                raise BskyError(f"Bluesky не обработал видео: {js.get('error') or js.get('message') or ''}".strip())
            if not js.get("blob"):
                if dbm.now() - state.get("started", dbm.now()) > 30 * 60:
                    raise BskyError("Bluesky слишком долго обрабатывает видео — попробуй ещё раз")
                return {"wait": True}
            state["blob"] = js["blob"]
        emb = {"$type": "app.bsky.embed.video", "video": state["blob"]}
        if fl.get("width") and fl.get("height"):
            emb["aspectRatio"] = {"width": int(fl["width"]), "height": int(fl["height"])}
        record["embed"] = emb
    r = await call(db, acc, "POST", xrpc(acc, "com.atproto.repo.createRecord"), proxy=proxy,
                   json_body={"repo": d["did"], "collection": "app.bsky.feed.post", "record": record})
    if not r.ok:
        j = r.json() or {}
        raise BskyError(f"Bluesky не опубликовал: {j.get('message') or j.get('error') or r.status}")
    rkey = r.json()["uri"].rsplit("/", 1)[-1]
    return {"url": f"https://bsky.app/profile/{d.get('handle') or d['did']}/post/{rkey}", "ext": f"bs:{rkey}"}
