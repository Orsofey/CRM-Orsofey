"""Кросспостинг: один пост → все соцсети. Типы: видео, фото (до 10), GIF, текст.

Авто: Telegram-канал (бот публикует сам), Instagram (Reels, фото и карусели — через подключённый Instagram,
файлы Meta забирает по ссылке). Полуавто: YouTube, TikTok, VK, X, Patreon — CRM готовит файлы и подписи,
присылает всё в Telegram, а опубликованное сама находит при синхронизации (по метке, кодовому слову или названию).
"""
import json, logging, os, re, secrets, time
from . import db as dbm, runtime, social
from .config import cfg

log = logging.getLogger(__name__)
TG_MAX = 50 * 1024 * 1024          # бот может отправить видео/GIF до 50 МБ
TG_PHOTO_MAX = 10 * 1024 * 1024    # и фото до 10 МБ
IG_TIMEOUT = 40 * 60               # сколько ждать, пока Instagram обработает видео

PLATFORMS = {
    "yt": {"name": "YouTube", "short": "yts", "long": "yt"},
    "tt": {"name": "TikTok", "short": "tt", "long": "tt"},
    "ig": {"name": "Instagram", "short": "ig", "long": "ig"},
    "vk": {"name": "VK", "short": "vkc", "long": "vk"},
    "tg": {"name": "Telegram", "short": "tg", "long": "tg"},
    "x": {"name": "X", "short": "x", "long": "x"},
    "pt": {"name": "Patreon", "short": "pt", "long": "pt"},
    "bs": {"name": "Bluesky", "short": "bs", "long": "bs"},
    "pn": {"name": "Pinterest", "short": "pn", "long": "pn"},
    "dc": {"name": "Discord", "short": "dc", "long": "dc"},
    "fa": {"name": "Fur Affinity", "short": "fa", "long": "fa"},
    "e6": {"name": "e621", "short": "e621", "long": "e621"},
    "ss": {"name": "SubscribeStar", "short": "ss", "long": "ss"},
}
ORDER = ["yt", "tt", "ig", "vk", "tg", "x", "bs", "pn", "dc", "fa", "e6", "pt", "ss"]
MULTI = ("bs", "pn", "dc", "e6", "yt", "tt", "vk", "x", "fa")   # публикуем сами во все аккаунты с «Публиковать сюда» и правами
PTYPES = {"video": "Видео", "image": "Фото", "gif": "GIF", "text": "Текст"}
# Что умеет каждая соцсеть для каждого типа поста: auto — CRM публикует сама (если подключено), manual — выкладываешь ты
# Patreon и SubscribeStar не дают API для публикации постов, YouTube — для постов «Сообщества»: там остаётся «вручную»
# (всё готовое — в карточке поста в CRM: скачать файл, скопировать подпись).
_NEW = {"bs": "auto", "pn": "auto", "dc": "auto", "fa": "manual", "e6": "auto", "ss": "manual"}
SUPPORT = {
    "video": {"yt": "auto", "tt": "auto", "ig": "auto", "vk": "auto", "tg": "auto", "x": "auto", "pt": "manual", **_NEW},
    "image": {"yt": "manual", "tt": "auto", "ig": "auto", "vk": "auto", "tg": "auto", "x": "auto", "pt": "manual", **_NEW, "fa": "auto"},
    "gif": {"vk": "auto", "tg": "auto", "x": "auto", "pt": "manual", **_NEW, "fa": "auto"},
    "text": {"yt": "manual", "vk": "auto", "tg": "auto", "x": "auto", "pt": "manual", "bs": "auto", "dc": "auto",
             "fa": "manual", "ss": "manual"},
}
NOTES = {("yt", "image"): "пост в «Сообществе»", ("yt", "text"): "пост в «Сообществе»", ("tt", "image"): "фото-пост",
         ("ig", "image"): "фото или карусель", ("ig", "video"): "Reels", ("fa", "text"): "журнал", ("pn", "image"): "пин",
         ("pn", "video"): "видео-пин", ("pn", "gif"): "пин", ("bs", "image"): "до 4 фото", ("e6", "image"): "каждое фото — отдельный пост",
         ("bs", "video"): "до 3 минут"}
# 18+: где можно и как помечать. None — нельзя совсем (бан за откровенный контент), строка — как CRM помечает / что сделать тебе
ADULT = {
    "yt": None, "tt": None, "ig": None, "vk": None, "pn": None,
    "tg": "только в отдельный канал для 18+ (Настройки → Публикация в Telegram), медиа под спойлером",
    "x": "перед публикацией включи «Отметить медиа как чувствительное»",
    "bs": "CRM ставит метку контента 18+ (Bluesky скроет пост от тех, кто не включил 18+)",
    "dc": "только в каналы Discord, отмеченные как 18+ (⚙ у аккаунта), файлы — под спойлером",
    "fa": "выбери рейтинг Mature или Adult при загрузке",
    "e6": "CRM ставит рейтинг: questionable (без явных сцен) или explicit",
    "pt": "отметь пост как 18+ — страница на Patreon должна быть помечена 18+",
    "ss": "только если страница на subscribestar.adult",
}
ADULT_BLOCK = {"yt": "YouTube удаляет откровенный контент и выдаёт страйки", "tt": "TikTok банит за откровенный контент",
               "ig": "Instagram банит за откровенный контент", "vk": "VK блокирует 18+ без отметки сообщества",
               "pn": "Pinterest запрещает откровенный контент"}
ADULT_LEVELS = {"": "Без 18+", "mature": "18+ без явных сцен", "adult": "18+ откровенно"}
EXT = {"video": (".mp4", ".mov", ".m4v", ".webm"), "image": (".jpg", ".jpeg", ".png", ".webp"), "gif": (".gif", ".mp4")}
STATUS_NAME = {"scheduled": "Запланировано", "queued": "В очереди", "processing": "Соцсеть обрабатывает файл",
               "ready": "Ждёт тебя — выложи вручную", "done": "Опубликовано", "error": "Ошибка"}


def media_dir() -> str:
    d = "/data/media" if os.path.isdir("/data") else os.path.join(os.path.dirname(cfg.db_path) or "data", "media")
    os.makedirs(d, exist_ok=True)
    return d


def media_path(token: str) -> str | None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{16,64}", token or ""):
        return None
    for f in os.listdir(media_dir()):
        if f.split(".")[0] == token:
            return os.path.join(media_dir(), f)
    return None


def file_url(f: dict) -> str:
    return f"{cfg.public_url}/media/{f['token']}/{f.get('name') or 'file'}"


def media_url(post) -> str:
    return f"{cfg.public_url}/media/{post['token']}/{post['file_name'] or 'video.mp4'}"


def new_upload(name: str) -> tuple[str, str]:
    ext = (os.path.splitext(name or "")[1].lower() or ".mp4")[:6]
    if ext not in (".mp4", ".mov", ".m4v", ".webm", ".jpg", ".jpeg", ".png", ".webp", ".gif"):
        ext = ".mp4"
    token = secrets.token_urlsafe(18)
    path = os.path.join(media_dir(), token + ext + ".part")
    open(path, "wb").close()
    return token, path


def is_short(duration: float | None, w: int | None, h: int | None) -> bool:
    if duration and duration > 180:
        return False
    if w and h:
        return h >= w            # вертикальное — шортс
    return bool(duration and duration <= 180)


def bot_link(code: str) -> str:
    u = runtime.state.get("bot_username") or "ИМЯ_БОТА"
    return f"https://t.me/{u}?start={code}"


def support(ptype: str) -> dict:
    return SUPPORT.get(ptype, SUPPORT["video"])


def make_codes(plats: list, ptype: str, short: bool, pid) -> dict:
    return {p: f"{PLATFORMS[p]['short' if (short and ptype == 'video') else 'long']}_p{pid}" for p in plats}


def build_captions(title: str, text: str, keyword: str, codes: dict, with_bot: bool = True, ptype: str = "video",
                   tags: str = "") -> dict:
    """Подписи под каждую соцсеть: где ссылки кликаются — ссылка с меткой, где нет — кодовое слово.
    with_bot=False — CRM друга (своего бота нет): подписи без ссылок и призывов в бота."""
    text = (text or "").strip()
    head = (title or text.split("\n")[0])
    out = {}
    if not with_bot:
        for p in codes:
            if p == "tg":
                out[p] = {"text": (f"<b>{_h(title)}</b>\n\n" if title else "") + _h(text)}
            elif p == "pt":
                out[p] = {"title": head[:200], "text": text}
            else:
                out[p] = {"text": text or head}
            if p == "vk" or (p == "yt" and ptype == "video"):
                out[p]["title"] = head[:100 if p == "yt" else 128]
            _extra(out, p, title, text, head, "", tags)
        return out
    kw = (keyword or "").strip().upper()
    gift_kw = (f"🎁 Напиши боту слово «{kw}» — пришлю подарок. Бот — по ссылке в профиле." if kw
               else "🎁 Подарок — в боте, ссылка в профиле.")
    for p, code in codes.items():
        link = bot_link(code)
        if p == "yt":
            out[p] = ({"title": head[:100], "text": f"🎁 Забери подарок: {link}\n\n{text}".strip()} if ptype == "video"
                      else {"text": f"{text}\n\n🎁 Подарок: {link}".strip()})
        elif p in ("tt", "ig"):
            out[p] = {"text": f"{text}\n\n{gift_kw}".strip()[:2200]}
        elif p == "vk":
            out[p] = {"title": head[:128], "text": f"{text}\n\n🎁 Подарок: {link}".strip()}
        elif p == "tg":
            out[p] = {"text": (f"<b>{_h(title)}</b>\n\n" if title else "") + _h(text)}
        elif p == "x":
            out[p] = {"text": f"{text or head}\n\n🎁 {link}".strip()}
        elif p == "pt":
            out[p] = {"title": head[:200], "text": f"{text}\n\n🎁 Подарок: {link}".strip()}
        else:
            _extra(out, p, title, text, head, link, tags)
    return out


def _extra(out: dict, p: str, title: str, text: str, head: str, link: str, tags: str):
    """Подписи для Bluesky, Pinterest, Discord, Fur Affinity, e621, SubscribeStar."""
    gift = f"\n\n🎁 {link}" if link else ""
    if p == "bs":
        out[p] = {"text": f"{text or head}{gift}".strip()}
    elif p == "pn":
        out[p] = {"title": head[:100], "text": (text or head)[:500]}
        if link:
            out[p]["link"] = link
    elif p == "dc":
        out[p] = {"text": ((f"**{title}**\n\n" if title else "") + f"{text}{gift}").strip()[:2000]}
    elif p == "fa":
        out[p] = {"title": head[:120], "text": f"{text}{gift}".strip(), "tags": tags}
    elif p == "e6":
        out[p] = {"text": text or head, "tags": tags}       # e621 запрещает рекламу — без ссылки на бота
    elif p == "ss":
        out[p] = {"title": head[:200], "text": f"{text}{gift}".strip()}


def _h(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


async def channel_for_posts(db):
    c = (await dbm.get_setting(db, "post_channel", "") or "").strip()
    if c:
        return int(c) if c.lstrip("-").isdigit() else (c if c.startswith("@") else "@" + c)
    if await dbm.get_setting(db, "ws_owner", ""):
        return None            # CRM друга: канал автора не подставляем
    return runtime.channel_chat()


async def recipients(db) -> list[int]:
    """Кому присылать файлы и подписи для ручной публикации: автору или владельцу CRM друга."""
    owner = await dbm.get_setting(db, "ws_owner", "")
    return [int(owner)] if owner.isdigit() else list(cfg.admins)


async def auto_available(db) -> dict:
    out = {"tg": bool(await channel_for_posts(db)), "ig": bool(await social.main_account(db, "ig") and cfg.public_url),
           "tg_adult": bool((await dbm.get_setting(db, "post_channel_adult", "") or "").strip())}
    from . import posting
    for k in MULTI:
        accs = [a for a in await social.publish_accounts(db, k) if posting.can_publish(a)]
        out[k] = bool(accs) and (k != "pn" or bool(cfg.public_url))
        if k == "dc":
            out["dc_adult"] = any(a["data"].get("adult") for a in accs)
    return out


def adult_check(p: str, adult: str, auto: dict) -> str | None:
    """Можно ли этот пост 18+ в соцсеть p. None — можно; строка — почему нельзя."""
    if not adult:
        return None
    if ADULT.get(p) is None:
        return ADULT_BLOCK.get(p, "соцсеть не разрешает 18+")
    if p == "tg" and not auto.get("tg_adult"):
        return "нужен отдельный канал для 18+ — задай его в Настройках → «Публикация в Telegram»"
    if p == "dc" and not auto.get("dc_adult"):
        return "нет канала Discord, отмеченного как 18+"
    return None


def post_files(p: dict) -> list[dict]:
    """Файлы поста (у старых постов — одно видео в колонках file/token)."""
    try:
        files = json.loads(p.get("files") or "[]")
    except ValueError:
        files = []
    if not files and p.get("token"):
        files = [{"token": p["token"], "name": p.get("file_name"), "path": p.get("file"), "size": p.get("file_size"),
                  "width": p.get("width"), "height": p.get("height")}]
    return files


# ------------------------------------------------------------------ создание
async def create_post(db, data: dict) -> int:
    ptype = data.get("ptype") if data.get("ptype") in PTYPES else "video"
    raw = data.get("files") or ([{"token": data["token"], "name": data.get("file_name"), "width": data.get("width"),
                                  "height": data.get("height")}] if data.get("token") else [])
    files = []
    for f in raw[:10]:
        path = media_path(str(f.get("token") or ""))
        if not path or path.endswith(".part"):
            raise ValueError("Файл не загружен до конца — загрузи ещё раз")
        files.append({"token": f["token"], "name": _safe_name(f.get("name"), os.path.splitext(path)[1]), "path": path,
                      "size": os.path.getsize(path), "width": f.get("width"), "height": f.get("height")})
    if ptype == "text":
        files = []
    elif not files:
        raise ValueError({"video": "Выбери видео", "image": "Выбери фото", "gif": "Выбери GIF"}[ptype])
    elif ptype in ("video", "gif") and len(files) > 1:
        files = files[:1]
    adult = data.get("adult") if data.get("adult") in ADULT_LEVELS else ""
    auto = await auto_available(db)
    plats = [p for p in ORDER if p in (data.get("platforms") or []) and p in support(ptype) and not adult_check(p, adult, auto)]
    if not plats:
        raise ValueError("Выбери хотя бы одну соцсеть, которая поддерживает такой пост" +
                         (" (для 18+ доступны не все — смотри пометки)" if adult else ""))
    tags = re.sub(r"\s+", " ", str(data.get("tags") or "").replace(",", " ")).strip()[:1000]
    source = str(data.get("source") or "").strip()[:500]
    title, text = str(data.get("title") or "").strip()[:200], str(data.get("text") or "").strip()[:5000]
    if not (title or text):
        raise ValueError("Напиши название или текст")
    kw = re.sub(r"[^\wА-ЯЁа-яё]", "", str(data.get("keyword") or "")).upper()[:30]
    dur = data.get("duration")
    f0 = files[0] if files else {}
    short = (data["kind"] == "short" if data.get("kind") in ("short", "long") else is_short(dur, f0.get("width"), f0.get("height"))) \
        if ptype == "video" else True
    publish_at = int(data.get("publish_at") or 0) or dbm.now()
    cur = await db.execute(
        "INSERT INTO posts(title, text, keyword, kind, file, file_name, file_size, duration, width, height, token, publish_at, "
        "created_at, ptype, files, adult, tags, source) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (title, text, kw, "short" if short else "long", f0.get("path"), f0.get("name"), f0.get("size"), dur, f0.get("width"),
         f0.get("height"), f0.get("token"), publish_at, dbm.now(), ptype, json.dumps(files, ensure_ascii=False), adult, tags, source))
    cur = await db.execute("SELECT last_insert_rowid()")
    pid = (await cur.fetchone())[0]
    codes = make_codes(plats, ptype, short, pid)
    with_bot = not await dbm.get_setting(db, "ws_owner", "")
    caps = build_captions(title, text, kw, codes, with_bot, ptype, tags)
    for p, c in (data.get("captions") or {}).items():          # подписи, поправленные вручную
        if p in caps and isinstance(c, dict):
            caps[p].update({k: str(v)[:5000] for k, v in c.items() if k in ("title", "text", "tags", "link")})
    for p in plats:
        mode = "auto" if support(ptype)[p] == "auto" and auto.get(p) else "manual"
        if p == "tg" and ptype in ("video", "gif") and f0.get("size", 0) > TG_MAX:
            mode = "manual"     # больше 50 МБ бот отправить не может
        if p == "tg" and ptype == "image" and any(f["size"] > TG_PHOTO_MAX for f in files):
            mode = "manual"
        status = "scheduled" if publish_at > dbm.now() + 30 else ("queued" if mode == "auto" else "ready")
        await db.execute("INSERT INTO post_targets(post_id, platform, mode, code, caption, status, updated_at) VALUES(?,?,?,?,?,?,?)",
                         (pid, p, mode, codes[p], json.dumps(caps[p], ensure_ascii=False), status, dbm.now()))
        plat, kind = dbm.parse_code(codes[p])
        await db.execute("INSERT OR IGNORE INTO content(code, platform, kind, title, keyword, created_at) VALUES(?,?,?,?,?,?)",
                         (codes[p], plat, kind if ptype == "video" else PTYPES[ptype], title or text[:80],
                          kw if p in ("tt", "ig", "yt", "vk") else "", dbm.now()))
    await db.commit()
    return pid


def _safe_name(n, ext: str = ".mp4") -> str:
    n = re.sub(r"[^\w.\-]", "_", str(n or ("file" + ext)))[-80:]
    return n if "." in n else n + ext


# ------------------------------------------------------------------ чтение
async def list_posts(db, limit: int = 50) -> list:
    cur = await db.execute("SELECT * FROM posts ORDER BY id DESC LIMIT ?", (limit,))
    posts = [dict(r) for r in await cur.fetchall()]
    for p in posts:
        p["ptype"] = p.get("ptype") or "video"
        p["targets"] = await _targets(db, p["id"])
        p.pop("files", None)
    return posts


async def get_post(db, pid: int) -> dict | None:
    cur = await db.execute("SELECT * FROM posts WHERE id=?", (pid,))
    r = await cur.fetchone()
    if not r:
        return None
    p = dict(r)
    p["ptype"] = p.get("ptype") or "video"
    files = post_files(p)
    p["media"] = [] if p["deleted_file"] else [{"url": file_url(f), "name": f.get("name"), "size": f.get("size")} for f in files]
    p["media_url"] = p["media"][0]["url"] if p["media"] else ""
    p.pop("files", None)
    p["targets"] = await _targets(db, pid)
    p["link"] = {t["platform"]: bot_link(t["code"]) for t in p["targets"]}
    return p


async def _targets(db, pid: int) -> list:
    cur = await db.execute("SELECT t.*, c.views, c.likes, c.comments, c.url AS c_url FROM post_targets t "
                           "LEFT JOIN content c ON c.code=t.code WHERE t.post_id=? ORDER BY t.id", (pid,))
    out = []
    for r in await cur.fetchall():
        t = dict(r)
        t["caption"] = json.loads(t["caption"] or "{}")
        t["name"] = PLATFORMS[t["platform"]]["name"]
        t["status_name"] = STATUS_NAME.get(t["status"], t["status"])
        t["url"] = t["url"] or t.pop("c_url", None) or ""
        t.pop("state", None)
        cur2 = await db.execute("SELECT COUNT(*) FROM users WHERE source=?", (t["code"],))
        t["people"] = (await cur2.fetchone())[0]
        out.append(t)
    return out


async def mark_done(db, target_id: int, url: str = ""):
    await db.execute("UPDATE post_targets SET status='done', url=COALESCE(NULLIF(?, ''), url), error=NULL, updated_at=? WHERE id=?",
                     (url.strip(), dbm.now(), target_id))
    await db.commit()


async def update_caption(db, target_id: int, caption: dict):
    await db.execute("UPDATE post_targets SET caption=?, updated_at=? WHERE id=? AND status IN ('scheduled','queued','ready','error')",
                     (json.dumps(caption, ensure_ascii=False), dbm.now(), target_id))
    await db.commit()


async def make_auto(db, target_id: int) -> str | None:
    """Ручную публикацию перевести на автоматическую (например, после «Разрешить публикацию»). None — готово, иначе причина."""
    cur = await db.execute("SELECT t.platform, t.status, p.ptype, p.adult FROM post_targets t JOIN posts p ON p.id=t.post_id WHERE t.id=?",
                           (target_id,))
    r = await cur.fetchone()
    if not r:
        return "Не найдено"
    plat, status, ptype, adult = r[0], r[1], r[2] or "video", r[3] or ""
    if status == "done":
        return "Уже опубликовано"
    auto = await auto_available(db)
    if support(ptype).get(plat) != "auto":
        return f"{PLATFORMS[plat]['name']} не даёт публиковать такой пост через API — только вручную"
    if not auto.get(plat):
        return f"{PLATFORMS[plat]['name']}: сначала нажми «Разрешить публикацию» в Настройках → Соцсети"
    why = adult_check(plat, adult, auto)
    if why:
        return why
    await db.execute("UPDATE post_targets SET mode='auto', status=CASE WHEN status='scheduled' THEN 'scheduled' ELSE 'queued' END, "
                     "error=NULL, state=NULL, updated_at=? WHERE id=?", (dbm.now(), target_id))
    await db.commit()
    return None


async def retry(db, target_id: int):
    await db.execute("UPDATE post_targets SET status=CASE mode WHEN 'auto' THEN 'queued' ELSE 'ready' END, error=NULL, state=NULL, "
                     "updated_at=? WHERE id=? AND status='error'", (dbm.now(), target_id))
    await db.commit()


def _remove_files(p: dict):
    for f in post_files(p):
        try:
            if f.get("path") and os.path.exists(f["path"]):
                os.remove(f["path"])
        except OSError:
            pass


async def delete_post(db, pid: int):
    cur = await db.execute("SELECT * FROM posts WHERE id=?", (pid,))
    r = await cur.fetchone()
    if r:
        _remove_files(dict(r))
    await db.execute("DELETE FROM post_targets WHERE post_id=?", (pid,))
    await db.execute("DELETE FROM posts WHERE id=?", (pid,))
    await db.commit()


# ------------------------------------------------------------------ публикация (фоновая задача, раз в 30 с)
async def tick(db, bot, http=None):
    now = dbm.now()
    # наступило время запланированных
    await db.execute("UPDATE post_targets SET status=CASE mode WHEN 'auto' THEN 'queued' ELSE 'ready' END, updated_at=? "
                     "WHERE status='scheduled' AND post_id IN (SELECT id FROM posts WHERE publish_at <= ?)", (now, now))
    await db.commit()
    cur = await db.execute("SELECT t.*, p.title, p.text, p.file, p.file_name, p.token, p.width, p.height, p.duration, p.file_size, "
                           "p.ptype, p.files, p.adult, p.tags, p.source, p.kind FROM post_targets t JOIN posts p ON p.id=t.post_id "
                           "WHERE t.status IN ('queued','processing') AND t.mode='auto'")
    for t in [dict(r) for r in await cur.fetchall()]:
        t["ptype"] = t.get("ptype") or "video"
        t["flist"] = post_files(t)
        t["adult"] = t.get("adult") or ""
        try:
            if t["platform"] == "tg":
                await _publish_tg(db, bot, t)
            elif t["platform"] == "ig":
                s = await dbm.get_settings(db)
                await _publish_ig(db, http or social.http_client(social.proxy_url(s)), t)
            elif t["platform"] in MULTI:
                await _publish_multi(db, t)
        except Exception as e:
            log.exception("Публикация %s", t["platform"])
            await _fail(db, t["id"], _human(e))
    await notify_manual(db, bot)
    await detect_published(db)
    await cleanup_media(db)


def _human(e: Exception) -> str:
    msg = str(e) or type(e).__name__
    if "chat not found" in msg.lower():
        return "Бот не видит канал — добавь его админом канала с правом публикации"
    if "not enough rights" in msg.lower() or "need administrator" in msg.lower():
        return "У бота нет права публиковать в канале — дай ему «Публикация сообщений»"
    if "too big" in msg.lower() or "entity too large" in msg.lower():
        return "Файл слишком большой для бота — выложи вручную"
    return msg[:300]


async def _fail(db, tid: int, err: str):
    await db.execute("UPDATE post_targets SET status='error', error=?, updated_at=? WHERE id=?", (err, dbm.now(), tid))
    await db.commit()


async def _publish_multi(db, t: dict):
    """Bluesky, Pinterest, Discord, e621: во все аккаунты с «Публиковать сюда». Видео обрабатывается в несколько шагов —
    что уже сделано, хранится в state, так что повторной публикации не будет."""
    from . import atproto, networks, posting
    s = await dbm.get_settings(db)
    px = social.proxy_url(s)
    state = json.loads(t.get("state") or "{}")
    accs = [a for a in await social.publish_accounts(db, t["platform"]) if posting.can_publish(a)]
    if t["adult"] and t["platform"] == "dc":
        accs = [a for a in accs if a["data"].get("adult")]
    if not accs:
        raise RuntimeError(f"{PLATFORMS[t['platform']]['name']}: нет аккаунта с правом публикации — нажми «Разрешить публикацию» "
                           f"в Настройках → Соцсети")
    cap = json.loads(t["caption"] or "{}")
    t.update(cap=cap, cap_text=cap.get("text", ""), tags=cap.get("tags") or t.get("tags") or "")
    res, waiting, errs = state.setdefault("accs", {}), False, []
    for a in accs:
        st = res.setdefault(str(a["id"]), {})
        if st.get("url") or st.get("error"):
            continue
        try:
            if t["platform"] == "bs":
                r = await atproto.publish(db, a, t, st, px)
            elif t["platform"] == "pn":
                r = await networks.pn_publish(db, a, t, st, s, px)
            elif t["platform"] == "dc":
                r = await networks.dc_publish(db, a, t, st, px)
            elif t["platform"] in posting.PUBLISHERS:
                r = await posting.PUBLISHERS[t["platform"]](db, a, t, st, s, px)
            else:
                r = await networks.e6_publish(db, a, t, st, px)
        except posting.NeedManual as e:
            st["error"], st["manual"] = str(e), True
            continue
        except (atproto.BskyError, networks.NetError, posting.PostError) as e:
            st["error"] = str(e)
            continue
        except Exception as e:
            log.exception("Публикация %s #%s", t["platform"], a["id"])
            st["error"] = _human(e)
            continue
        if r.get("wait"):
            waiting = True
        else:
            st.update(url=r["url"], ext=r["ext"], note=r.get("note", ""), name=a["name"], acc=a["id"])
    await db.execute("UPDATE post_targets SET state=?, updated_at=? WHERE id=?", (json.dumps(state, ensure_ascii=False), dbm.now(), t["id"]))
    if waiting:
        await db.execute("UPDATE post_targets SET status='processing' WHERE id=?", (t["id"],))
        await db.commit()
        return
    ok = [x for x in res.values() if x.get("url")]
    bad = [f"{x.get('name') or ''} {x['error']}".strip() for x in res.values() if x.get("error")]
    notes = "; ".join(x["note"] for x in ok if x.get("note"))
    if not ok and res and all(x.get("manual") for x in res.values() if x.get("error")):
        # нет права или API для такого поста — не ошибка: выкладываешь сам по готовому (файл и подписи — в карточке поста)
        await db.execute("UPDATE post_targets SET mode='manual', status='ready', error=?, state=NULL, updated_at=? WHERE id=?",
                         ("; ".join(bad), dbm.now(), t["id"]))
        await db.execute("UPDATE posts SET notified=0 WHERE id=?", (t["post_id"],))
        await db.commit()
        return
    if not ok:
        await db.commit()
        raise RuntimeError("; ".join(bad) or "не вышло")
    err = ("Не везде: " + "; ".join(bad)) if bad else (notes or None)
    await db.execute("UPDATE post_targets SET status='done', url=?, ext_id=?, error=?, updated_at=? WHERE id=?",
                     (ok[0]["url"], ok[0]["ext"], err, dbm.now(), t["id"]))
    await db.execute("UPDATE content SET url=?, ext_id=COALESCE(ext_id, ?), account=COALESCE(account, ?), published_at=? WHERE code=?",
                     (ok[0]["url"], ok[0]["ext"], ok[0].get("acc"), dbm.now(), t["code"]))
    await db.commit()


async def _publish_tg(db, bot, t: dict):
    from aiogram.types import FSInputFile, InlineKeyboardMarkup, InlineKeyboardButton, InputMediaPhoto
    chat = await channel_for_posts(db)
    if t.get("adult"):                       # 18+ — только в отдельный канал для взрослых
        c = (await dbm.get_setting(db, "post_channel_adult", "") or "").strip()
        if not c:
            raise RuntimeError("Нет канала для 18+ — задай его в Настройках → «Публикация в Telegram»")
        chat = int(c) if c.lstrip("-").isdigit() else (c if c.startswith("@") else "@" + c)
    cap = json.loads(t["caption"]).get("text", "")
    friend = bool(await dbm.get_setting(db, "ws_owner", ""))
    kb = None if friend else \
        InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🎁 Забрать подарок", url=bot_link(t["code"]))]])
    files, ptype = t["flist"], t["ptype"]
    long_cap = len(cap) > 1024           # подпись к медиа — до 1024 символов; длиннее — отдельным сообщением
    media_cap = None if long_cap else cap
    if ptype == "text":
        msg = await bot.send_message(chat, cap[:4096], parse_mode="HTML", reply_markup=kb)
    elif ptype == "image" and len(files) > 1:
        group = [InputMediaPhoto(media=FSInputFile(f["path"], filename=f["name"]), has_spoiler=bool(t.get("adult")),
                                 caption=(media_cap if i == 0 else None), parse_mode="HTML") for i, f in enumerate(files[:10])]
        msgs = await bot.send_media_group(chat, group)
        msg = msgs[0]
        if long_cap or kb:     # у альбома не бывает кнопок — кнопку и длинный текст шлём следом
            await bot.send_message(chat, cap[:4096] if long_cap else "🎁 Подарок — по кнопке ниже", parse_mode="HTML", reply_markup=kb)
    else:
        f = files[0]
        inp = FSInputFile(f["path"], filename=f["name"])
        kw = dict(caption=media_cap, parse_mode="HTML", reply_markup=None if long_cap else kb)
        if t.get("adult"):
            kw["has_spoiler"] = True            # 18+: медиа размыто, пока человек сам не нажмёт
        if ptype == "image":
            msg = await bot.send_photo(chat, inp, **kw)
        elif ptype == "gif":
            msg = await bot.send_animation(chat, inp, **kw)
        else:
            msg = await bot.send_video(chat, inp, supports_streaming=True, width=t["width"] or None, height=t["height"] or None,
                                       duration=int(t["duration"]) if t["duration"] else None, **kw)
        if long_cap:
            await bot.send_message(chat, cap[:4096], parse_mode="HTML", reply_markup=kb)
    ch = msg.chat
    url = f"https://t.me/{ch.username}/{msg.message_id}" if getattr(ch, "username", None) else \
        f"https://t.me/c/{str(ch.id).replace('-100', '', 1)}/{msg.message_id}"
    await db.execute("UPDATE post_targets SET status='done', url=?, ext_id=?, updated_at=? WHERE id=?",
                     (url, f"tg:{ch.id}_{msg.message_id}", dbm.now(), t["id"]))
    await db.execute("UPDATE content SET url=?, published_at=? WHERE code=?", (url, dbm.now(), t["code"]))
    if getattr(ch, "username", None):         # синхронизация канала узнает этот пост и оставит ему метку публикации
        await db.execute("UPDATE content SET ext_id=? WHERE code=? AND ext_id IS NULL",
                         (f"tg:{ch.username.lower()}/{msg.message_id}", t["code"]))
    await db.commit()


def ig_jpeg(f: dict) -> str:
    """Instagram принимает только JPEG с пропорциями от 4:5 до 1.91:1 — при необходимости конвертируем
    и добавляем поля (картинка не обрезается). Возвращает ссылку на готовый файл."""
    path = f["path"]
    try:
        from PIL import Image
    except ImportError:
        return file_url(f)
    with Image.open(path) as im:
        im = im.convert("RGB")
        w, h = im.size
        ratio = w / h
        if ratio < 0.8:
            nw, nh = int(h * 0.8), h
        elif ratio > 1.91:
            nw, nh = w, int(w / 1.91)
        else:
            nw, nh = w, h
        if (nw, nh) != (w, h):
            bg = Image.new("RGB", (nw, nh), (255, 255, 255))
            bg.paste(im, ((nw - w) // 2, (nh - h) // 2))
            im = bg
        if path.lower().endswith((".jpg", ".jpeg")) and (nw, nh) == (w, h):
            return file_url(f)
        token = secrets.token_urlsafe(18)
        out = os.path.join(media_dir(), token + ".jpg")
        im.save(out, "JPEG", quality=92)
    return file_url({"token": token, "name": "photo.jpg"})


async def _publish_ig(db, http, t: dict):
    acc = await social.main_account(db, "ig")     # публикуем в первый видимый аккаунт Instagram
    if not acc:
        raise RuntimeError("Instagram не подключён — подключи в «Настройках»")
    tok = acc["data"]["token"]
    base = social.IG_GRAPH + social.ig_v() + "/"
    ig_id = acc["data"].get("user_id")
    if not ig_id:                                   # id аккаунта нужен для публикации — узнаём один раз
        me = await http("GET", base + "me", params={"fields": "user_id,username", "access_token": tok})
        social._ig_err(me)
        ig_id = str(me.get("user_id") or me.get("id") or "")
        if not ig_id:
            raise RuntimeError("Instagram не отдал id аккаунта — войди через Instagram заново")
        acc["data"]["user_id"] = ig_id
        await social.save_account(db, acc)
    state = json.loads(t["state"] or "{}")
    if t["status"] == "queued":
        cap = json.loads(t["caption"]).get("text", "")
        files = t["flist"]
        if t["ptype"] == "image" and len(files) > 1:          # карусель: сначала каждое фото, потом общий пост
            children = []
            for f in files[:10]:
                c = await http("POST", base + f"{ig_id}/media", params={"image_url": ig_jpeg(f), "is_carousel_item": "true",
                                                                       "access_token": tok})
                social._ig_err(c)
                children.append(c["id"])
            params = {"media_type": "CAROUSEL", "children": ",".join(children), "caption": cap, "access_token": tok}
        elif t["ptype"] == "image":
            params = {"image_url": ig_jpeg(files[0]), "caption": cap, "access_token": tok}
        else:
            params = {"media_type": "REELS", "video_url": file_url(files[0]), "caption": cap, "share_to_feed": "true",
                      "access_token": tok}
        r = await http("POST", base + f"{ig_id}/media", params=params)
        social._ig_err(r)
        await db.execute("UPDATE post_targets SET status='processing', state=?, updated_at=? WHERE id=?",
                         (json.dumps({"container": r["id"], "started": dbm.now()}), dbm.now(), t["id"]))
        await db.commit()
        return
    # processing: ждём, пока Instagram скачает и обработает файлы
    r = await http("GET", base + state["container"], params={"fields": "status_code,status", "access_token": tok})
    social._ig_err(r)
    code = r.get("status_code")
    if code == "FINISHED":
        pub = await http("POST", base + f"{ig_id}/media_publish", params={"creation_id": state["container"], "access_token": tok})
        social._ig_err(pub)
        mid = pub["id"]
        link = ""
        try:
            m = await http("GET", base + mid, params={"fields": "permalink", "access_token": tok})
            link = (m or {}).get("permalink", "")
        except Exception:
            pass
        await db.execute("UPDATE post_targets SET status='done', ext_id=?, url=?, updated_at=? WHERE id=?",
                         ("ig:" + str(mid), link, dbm.now(), t["id"]))
        await db.execute("UPDATE content SET ext_id=?, url=?, synced='instagram', published_at=? WHERE code=?",
                         ("ig:" + str(mid), link, dbm.now(), t["code"]))
        await db.commit()
    elif code in ("ERROR", "EXPIRED"):
        hint = "Reels — вертикальное видео 3 с – 15 мин" if t["ptype"] == "video" else "фото — JPEG/PNG до 8 МБ"
        raise RuntimeError(f"Instagram не принял: {r.get('status') or code}. {hint}")
    elif dbm.now() - state.get("started", dbm.now()) > IG_TIMEOUT:
        raise RuntimeError("Instagram слишком долго обрабатывает — попробуй ещё раз")


# ------------------------------------------------------------------ полуавто: всё нужное — в Telegram
async def notify_manual(db, bot):
    """Присылать в Telegram файлы и подписи для ручной публикации — только если включено в Настройках.
    По умолчанию выключено: всё нужное есть в карточке поста в CRM."""
    if await dbm.get_setting(db, "manual_tg", "0") != "1":
        return
    to = await recipients(db)
    if not to:
        return
    cur = await db.execute("SELECT DISTINCT post_id FROM post_targets WHERE status='ready' AND mode='manual' "
                           "AND post_id IN (SELECT id FROM posts WHERE notified=0)")
    for (pid,) in await cur.fetchall():
        await db.execute("UPDATE posts SET notified=1 WHERE id=?", (pid,))
        await db.commit()
        p = await get_post(db, pid)
        cur2 = await db.execute("SELECT * FROM posts WHERE id=?", (pid,))
        files = post_files(dict(await cur2.fetchone()))
        manual = [t for t in p["targets"] if t["mode"] == "manual" and t["status"] == "ready"]
        if not manual:
            continue
        names = ", ".join(t["name"] for t in manual)
        what = {"video": "видео", "image": "фото", "gif": "GIF", "text": "текст"}[p["ptype"]]
        for a in to:
            try:
                from aiogram.types import FSInputFile, InputMediaPhoto
                await bot.send_message(a, f"📤 Пора выложить: {names}\n«{p['title'] or p['text'][:60]}»"
                                          + (f"\n🔞 {ADULT_LEVELS.get(p.get('adult') or '', '')}" if p.get("adult") else "") + "\n\n"
                                          f"Ниже — {what} и готовые подписи: зажми сообщение → «Копировать». "
                                          f"После публикации CRM сама найдёт пост и отметит «Опубликовано» "
                                          f"(или нажми «Я выложил» в CRM)."
                                          + (f"\n\nВсё в CRM: {cfg.public_url}" if cfg.public_url else ""),
                                       disable_web_page_preview=True)
                ok = [f for f in files if f.get("path") and os.path.exists(f["path"])]
                if p["ptype"] == "image" and ok:
                    if all(f["size"] <= TG_PHOTO_MAX for f in ok):
                        if len(ok) > 1:
                            await bot.send_media_group(a, [InputMediaPhoto(media=FSInputFile(f["path"], filename=f["name"])) for f in ok[:10]])
                        else:
                            await bot.send_photo(a, FSInputFile(ok[0]["path"], filename=ok[0]["name"]))
                    else:
                        for f in ok:
                            await bot.send_document(a, FSInputFile(f["path"], filename=f["name"]))
                elif p["ptype"] in ("video", "gif") and ok:
                    f = ok[0]
                    if f["size"] <= TG_MAX:
                        inp = FSInputFile(f["path"], filename=f["name"])
                        await (bot.send_animation(a, inp) if p["ptype"] == "gif" else bot.send_video(a, inp, supports_streaming=True))
                    elif cfg.public_url:
                        await bot.send_message(a, f"⬇️ Файл больше 50 МБ — скачать: {file_url(f)}?dl=1")
                for t in manual:
                    cap = t["caption"]
                    if cap.get("title"):
                        await bot.send_message(a, f"{t['name']} — название:")
                        await bot.send_message(a, cap["title"], disable_web_page_preview=True)
                    label = "описание" if t["platform"] in ("yt", "vk") and p["ptype"] == "video" else "текст поста" \
                        if p["ptype"] in ("text", "image") else "подпись"
                    await bot.send_message(a, f"{t['name']} — {label}:")
                    await bot.send_message(a, _plain(cap.get("text", "")), disable_web_page_preview=True)
                    if cap.get("tags"):
                        await bot.send_message(a, f"{t['name']} — ключевые слова:")
                        await bot.send_message(a, cap["tags"])
                    if p.get("adult") and ADULT.get(t["platform"]):
                        await bot.send_message(a, f"🔞 {t['name']}: {ADULT[t['platform']]}")
            except Exception as e:
                log.info("Не отправил материалы поста %s: %s", pid, e)


def _plain(s: str) -> str:
    return re.sub(r"</?b>", "", s).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


async def detect_published(db):
    """Пост из полуавто появился при синхронизации (нашёлся по метке/слову) — отмечаем «Опубликовано»."""
    cur = await db.execute("SELECT t.id, c.url FROM post_targets t JOIN content c ON c.code=t.code "
                           "WHERE t.status IN ('ready','scheduled') AND c.ext_id IS NOT NULL AND c.synced IS NOT NULL")
    for tid, url in await cur.fetchall():
        await mark_done(db, tid, url or "")
    # запасной способ — по названию/началу текста: пост вышел после публикации, и начало совпадает с нашим
    cur = await db.execute("SELECT t.id, t.platform, p.title, p.text, p.publish_at FROM post_targets t JOIN posts p ON p.id=t.post_id "
                           "WHERE t.status='ready' AND t.mode='manual'")
    plat = {"yt": "YouTube", "tt": "TikTok", "ig": "Instagram", "vk": "VK", "x": "X", "fa": "Fur Affinity"}
    for tid, pl, title, text, pub in await cur.fetchall():
        keys = [k for k in (_norm(title)[:40], _norm((text or "").split("\n")[0])[:40]) if len(k) >= 8]
        if pl not in plat or not keys:
            continue
        cur2 = await db.execute("SELECT code, title, url FROM content WHERE platform=? AND synced IS NOT NULL AND published_at >= ? "
                                "AND code NOT IN (SELECT code FROM post_targets)", (plat[pl], (pub or 0) - 3600))
        for code, ctitle, url in await cur2.fetchall():
            ct = _norm(ctitle)
            if any(ct.startswith(k) or (len(ct) >= 8 and k.startswith(ct[:40])) for k in keys):
                await db.execute("UPDATE post_targets SET code=? WHERE id=?", (code, tid))
                await mark_done(db, tid, url or "")
                break


def _norm(s: str) -> str:
    return re.sub(r"[^\wа-яё]+", " ", (s or "").lower()).strip()


async def cleanup_media(db, keep_days: int = 3, max_days: int = 21):
    """Файлы лежат на сервере, пока нужны: 3 дня после публикации везде, максимум 3 недели."""
    now = dbm.now()
    cur = await db.execute("SELECT *, (SELECT COUNT(*) FROM post_targets t WHERE t.post_id=posts.id "
                           "AND t.status NOT IN ('done')) AS open_ FROM posts WHERE deleted_file=0")
    for r in [dict(x) for x in await cur.fetchall()]:
        old = now - (r["publish_at"] or now)
        if (r["open_"] == 0 and old > keep_days * 86400) or old > max_days * 86400:
            _remove_files(r)
            await db.execute("UPDATE posts SET deleted_file=1 WHERE id=?", (r["id"],))
    # брошенные недозагрузки и файлы, из которых так и не сделали публикацию
    cur = await db.execute("SELECT token, files FROM posts WHERE deleted_file=0")
    used = set()
    for token, files in await cur.fetchall():
        used.add(token)
        try:
            used |= {f.get("token") for f in json.loads(files or "[]")}
        except ValueError:
            pass
    for fn in os.listdir(media_dir()):
        fp = os.path.join(media_dir(), fn)
        if time.time() - os.path.getmtime(fp) > 86400 and (fn.endswith(".part") or fn.split(".")[0] not in used):
            os.remove(fp)
    await db.commit()
