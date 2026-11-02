"""Подарки по отдельной ссылке: t.me/бот?start=skills → бот присылает набор файлов (например, скиллы из ролика).

Основной подарок (приложение «Агент») остаётся на обычной ссылке. Набор — это код ссылки, тексты, файлы и
откуда ссылка (YouTube-ролик, шортс…), чтобы CRM считала, сколько людей пришло за ним и сколько дошло до приложения.
Файлы лежат в /data/packs/<код>/, в Telegram уходят один раз — дальше бот шлёт их по file_id (мгновенно).
"""
import io, json, logging, os, re, shutil, zipfile
from . import db as dbm
from .config import cfg

log = logging.getLogger(__name__)
CODE_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,40}$")
MAX_FILES = 30
TG_DOC_LIMIT = 50 * 1024 * 1024

DEFAULTS = {
    "title": "Скиллы из ролика",
    "intro": "Привет 👋 Я бот Orsofey.\n\nЗдесь 10 моих скиллов из ролика — те, которыми я сам пользуюсь каждый день. "
             "Жми кнопку ниже — пришлю файлы 👇",
    "button": "📥 Забрать скиллы",
    "caption": "Держи 🎁 Это мои скиллы из ролика.\n\nЗагрузи их в раздел навыков (Skills) — и пользуйся. "
               "Если что-то не получается — напиши сюда, помогу.",
    "keyword": "",
    "mode": "files",
    "need_sub": 1,
    "platform": "YouTube",
    "kind": "Видео",
}
KINDS = {  # откуда ссылка → формат для «Аналитики»
    "YouTube": ["Видео", "Шортс", "Пост"], "TikTok": ["Шортс"], "Instagram": ["Reels", "Пост"],
    "VK": ["Видео", "Клип", "Пост"], "Telegram": ["Пост"], "X": ["Пост"], "Patreon": ["Пост"], "Другое": ["—"],
}


def root() -> str:
    d = "/data/packs" if os.path.isdir("/data") else os.path.join(os.path.dirname(cfg.db_path) or "data", "packs")
    os.makedirs(d, exist_ok=True)
    return d


def pack_dir(code: str) -> str:
    d = os.path.join(root(), code)
    os.makedirs(d, exist_ok=True)
    return d


def safe_name(name: str) -> str:
    name = os.path.basename((name or "").replace("\\", "/")).strip()
    name = re.sub(r"[^\w.\- ()\[\]+]", "_", name, flags=re.U)[:120].strip(" .")
    return name or "file"


def _row(r) -> dict:
    d = dict(r)
    d["files"] = json.loads(d.get("files") or "[]")
    d["cache"] = json.loads(d.get("cache") or "{}")
    return d


async def reload(db):
    """Метки ссылок наборов → платформа/формат (чтобы люди со ссылки считались, например, как YouTube · Видео)."""
    cur = await db.execute("SELECT code, platform, kind FROM packs")
    dbm.PACK_LABELS.clear()
    for code, plat, kind in await cur.fetchall():
        dbm.PACK_LABELS[code.lower()] = (plat or "Другое", kind or "—")


async def get(db, code: str | None) -> dict | None:
    if not code:
        return None
    cur = await db.execute("SELECT * FROM packs WHERE code=?", (code.lower(),))
    r = await cur.fetchone()
    return _row(r) if r else None


async def find_active(db, code: str | None) -> dict | None:
    p = await get(db, code)
    return p if p and p["active"] and p["files"] else None


async def by_keyword(db, text: str) -> dict | None:
    words = set(re.findall(r"[\wА-ЯЁ]+", (text or "").upper()))
    if not words:
        return None
    cur = await db.execute("SELECT * FROM packs WHERE active=1 AND keyword IS NOT NULL AND keyword<>''")
    for r in await cur.fetchall():
        p = _row(r)
        if p["files"] and p["keyword"].upper() in words:
            return p
    return None


async def save(db, body: dict) -> dict:
    code = (body.get("code") or "").strip().lower()
    old = (body.get("old_code") or "").strip().lower() or None
    if not CODE_RE.match(code):
        raise ValueError("Код ссылки — латиница, цифры, «_» или «-», от 2 до 40 символов (например, skills)")
    if code in ("direct",) or re.fullmatch(r"(yts|yt|s|tt|ig|insta|vkc|vkp|vk|tg|x|tw|pt)(_.*|\d.*)?", code):
        raise ValueError("Такой код похож на метку ролика — возьми слово, например skills или skills10")
    exist = await get(db, old or code)
    if old and old != code:
        if await get(db, code):
            raise ValueError("Набор с таким кодом уже есть")
        if exist:   # переименование: переносим папку и пути файлов
            src, dst = os.path.join(root(), old), os.path.join(root(), code)
            if os.path.isdir(src) and not os.path.exists(dst):
                shutil.move(src, dst)
            for f in exist["files"]:
                f["path"] = os.path.join(dst, os.path.basename(f["path"]))
            await db.execute("UPDATE packs SET code=?, files=? WHERE code=?",
                             (code, json.dumps(exist["files"], ensure_ascii=False), old))
    elif not old and exist:
        raise ValueError("Набор с таким кодом уже есть")
    cur = await get(db, code)
    vals = {k: body.get(k, (cur or DEFAULTS).get(k, DEFAULTS.get(k))) for k in DEFAULTS}
    vals["need_sub"] = 1 if vals["need_sub"] in (True, 1, "1", "true", "on") else 0
    vals["mode"] = "zip" if vals["mode"] == "zip" else "files"
    if vals["platform"] not in KINDS:
        vals["platform"] = "Другое"
    if vals["kind"] not in KINDS[vals["platform"]]:
        vals["kind"] = KINDS[vals["platform"]][0]
    vals["keyword"] = re.sub(r"[^\wА-Яа-яЁё]", "", vals["keyword"] or "", flags=re.U)[:30]
    for k in ("title", "intro", "button", "caption"):
        vals[k] = (vals[k] or "").strip() or DEFAULTS[k]
    vals["button"] = vals["button"][:60]
    vals["caption"] = vals["caption"][:1000]
    vals["intro"] = vals["intro"][:3500]
    active = 0 if body.get("active") in (False, 0, "0") else 1
    if cur:
        await db.execute("UPDATE packs SET title=?, intro=?, button=?, caption=?, keyword=?, mode=?, need_sub=?, "
                         "platform=?, kind=?, active=? WHERE code=?",
                         (vals["title"], vals["intro"], vals["button"], vals["caption"], vals["keyword"], vals["mode"],
                          vals["need_sub"], vals["platform"], vals["kind"], active, code))
    else:
        await db.execute("INSERT INTO packs(code, title, intro, button, caption, keyword, mode, need_sub, platform, kind, "
                         "files, cache, active, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,'[]','{}',?,?)",
                         (code, vals["title"], vals["intro"], vals["button"], vals["caption"], vals["keyword"],
                          vals["mode"], vals["need_sub"], vals["platform"], vals["kind"], active, dbm.now()))
    await db.commit()
    await reload(db)
    await dbm.ensure_content(db, code)
    await db.execute("UPDATE content SET platform=?, kind=?, title=CASE WHEN title IS NULL OR title='' THEN ? ELSE title END "
                     "WHERE code=? AND synced IS NULL", (vals["platform"], vals["kind"], vals["title"], code))
    await db.commit()
    return await get(db, code)


async def _store(db, p: dict):
    await db.execute("UPDATE packs SET files=?, cache=? WHERE code=?",
                     (json.dumps(p["files"], ensure_ascii=False), json.dumps(p["cache"]), p["code"]))
    await db.commit()


async def add_file(db, code: str, src_path: str, name: str) -> dict:
    p = await get(db, code)
    if not p:
        raise ValueError("Набор не найден")
    if len(p["files"]) >= MAX_FILES:
        raise ValueError(f"В наборе уже {MAX_FILES} файлов")
    size = os.path.getsize(src_path)
    if size > TG_DOC_LIMIT:
        os.remove(src_path)
        raise ValueError("Telegram-бот отправляет файлы до 50 МБ — этот больше")
    name = safe_name(name)
    taken = {f["name"].lower() for f in p["files"]}
    base, ext = os.path.splitext(name)
    i = 2
    while name.lower() in taken:          # десять файлов SKILL.md подряд — даём разные имена
        name = f"{base} ({i}){ext}"
        i += 1
    dst = os.path.join(pack_dir(p["code"]), f"{len(p['files'])}_{dbm.now()}_{name}")
    shutil.move(src_path, dst)
    p["files"].append({"name": name, "path": dst, "size": size})
    p["cache"].pop("zip", None)
    await _store(db, p)
    return p


async def remove_file(db, code: str, index: int) -> dict:
    p = await get(db, code)
    if not p or not (0 <= index < len(p["files"])):
        raise ValueError("Файл не найден")
    f = p["files"].pop(index)
    try:
        os.remove(f["path"])
    except OSError:
        pass
    p["cache"].pop("zip", None)
    await _store(db, p)
    return p


async def rename_file(db, code: str, index: int, name: str) -> dict:
    p = await get(db, code)
    if not p or not (0 <= index < len(p["files"])):
        raise ValueError("Файл не найден")
    p["files"][index]["name"] = safe_name(name)
    p["files"][index].pop("file_id", None)        # имя файла в Telegram зашито в file_id — загрузим заново
    p["cache"].pop("zip", None)
    await _store(db, p)
    return p


async def move_file(db, code: str, index: int, delta: int) -> dict:
    p = await get(db, code)
    j = index + delta
    if not p or not (0 <= index < len(p["files"])) or not (0 <= j < len(p["files"])):
        raise ValueError("Файл не найден")
    p["files"][index], p["files"][j] = p["files"][j], p["files"][index]
    p["cache"].pop("zip", None)
    await _store(db, p)
    return p


async def delete(db, code: str):
    await db.execute("DELETE FROM packs WHERE code=?", (code,))
    await db.commit()
    shutil.rmtree(os.path.join(root(), code), ignore_errors=True)
    await reload(db)


def zip_name(p: dict) -> str:
    t = safe_name(p["title"] or p["code"]).replace(" ", "_")
    return (t or p["code"]) + ".zip"


def build_zip(p: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for f in p["files"]:
            if os.path.exists(f["path"]):
                z.write(f["path"], f["name"])
    return buf.getvalue()


async def send(bot, db, chat_id: int, p: dict):
    """Отправить набор. Первый раз файлы загружаются в Telegram, дальше — по file_id."""
    from aiogram.types import FSInputFile, BufferedInputFile, InputMediaDocument
    caption = p["caption"]
    if p["mode"] == "zip":
        fid = p["cache"].get("zip")
        if fid:
            try:
                await bot.send_document(chat_id, fid, caption=caption)
                return
            except Exception as e:
                log.warning("zip по file_id не ушёл (%s) — загружаю заново", e)
        data = build_zip(p)
        if len(data) > TG_DOC_LIMIT:
            raise ValueError("Архив больше 50 МБ — переключи набор на «Файлами»")
        m = await bot.send_document(chat_id, BufferedInputFile(data, zip_name(p)), caption=caption)
        if m and getattr(m, "document", None):
            p["cache"]["zip"] = m.document.file_id
            await _store(db, p)
        return
    files = [f for f in p["files"] if f.get("file_id") or os.path.exists(f["path"])]
    if not files:
        raise ValueError("В наборе нет файлов")
    changed = False
    for start in range(0, len(files), 10):    # альбом документов — до 10 штук
        chunk = files[start:start + 10]
        last = start + 10 >= len(files)

        def media(f, cap):
            src = f.get("file_id") or FSInputFile(f["path"], filename=f["name"])
            return src, cap

        if len(chunk) == 1:
            src, _ = media(chunk[0], None)
            try:
                m = await bot.send_document(chat_id, src, caption=caption if last else None)
            except Exception:
                if not chunk[0].get("file_id"):
                    raise
                chunk[0].pop("file_id", None)
                m = await bot.send_document(chat_id, FSInputFile(chunk[0]["path"], filename=chunk[0]["name"]),
                                            caption=caption if last else None)
            msgs = [m]
        else:
            def group():
                return [InputMediaDocument(media=f.get("file_id") or FSInputFile(f["path"], filename=f["name"]),
                                           caption=caption if (last and i == len(chunk) - 1) else None)
                        for i, f in enumerate(chunk)]
            try:
                msgs = await bot.send_media_group(chat_id, group())
            except Exception:
                if not any(f.get("file_id") for f in chunk):
                    raise
                for f in chunk:            # file_id устарел (например, сменили бота) — загрузим файлы заново
                    f.pop("file_id", None)
                msgs = await bot.send_media_group(chat_id, group())
        for f, m in zip(chunk, msgs or []):
            doc = getattr(m, "document", None)
            if doc and f.get("file_id") != doc.file_id:
                f["file_id"] = doc.file_id
                changed = True
    if changed:
        by_path = {f["path"]: f.get("file_id") for f in files}
        for f in p["files"]:
            if by_path.get(f["path"]):
                f["file_id"] = by_path[f["path"]]
        await _store(db, p)


async def pending_for(db, user_id: int) -> str | None:
    """Человек открыл ссылку набора, но ещё не забрал его — код набора (для напоминания)."""
    cur = await db.execute("SELECT meta FROM events WHERE user_id=? AND type='pack_open' ORDER BY id DESC LIMIT 1", (user_id,))
    r = await cur.fetchone()
    if not r:
        return None
    try:
        code = json.loads(r[0] or "{}").get("code")
    except ValueError:
        return None
    cur = await db.execute("SELECT 1 FROM events WHERE user_id=? AND type='pack_got' AND meta LIKE ?", (user_id, f'%"{code}"%'))
    if await cur.fetchone():
        return None
    return code if await find_active(db, code) else None


async def stats(db, code: str) -> dict:
    like = f'%"code": "{code}"%'
    q = lambda t: db.execute(f"SELECT COUNT(DISTINCT user_id) FROM events WHERE type='{t}' AND meta LIKE ?", (like,))
    cur = await db.execute("SELECT COUNT(DISTINCT user_id) FROM events WHERE type IN ('pack_open','pack_got') AND meta LIKE ?", (like,))
    opened = (await cur.fetchone())[0]   # кто-то жмёт кнопку из старого сообщения, не открывая ссылку заново
    got = (await (await q("pack_got")).fetchone())[0]
    cur = await db.execute("SELECT SUM(u.tested_at IS NOT NULL), SUM(u.paid_at IS NOT NULL) FROM users u "
                           "WHERE EXISTS(SELECT 1 FROM events e WHERE e.user_id=u.id AND e.type='pack_got' AND e.meta LIKE ?)",
                           (like,))
    tested, paid = await cur.fetchone()
    cur = await db.execute("SELECT COUNT(*) FROM users WHERE source=?", (code,))
    came = (await cur.fetchone())[0]
    return {"opened": opened, "got": got, "tested": tested or 0, "paid": paid or 0, "new": came,
            "conv": min(100, round(got * 100 / opened)) if opened else 0}


def public(p: dict, bot_username: str) -> dict:
    out = {k: p[k] for k in ("code", "title", "intro", "button", "caption", "keyword", "mode", "need_sub",
                             "platform", "kind", "active", "created_at")}
    out["files"] = [{"name": f["name"], "size": f["size"], "sent": bool(f.get("file_id"))} for f in p["files"]]
    out["total"] = sum(f["size"] for f in p["files"])
    out["link"] = f"https://t.me/{bot_username}?start={p['code']}" if bot_username else ""
    return out


async def list_all(db, bot_username: str) -> list[dict]:
    cur = await db.execute("SELECT * FROM packs ORDER BY created_at DESC")
    out = []
    for r in await cur.fetchall():
        p = _row(r)
        d = public(p, bot_username)
        d["stats"] = await stats(db, p["code"])
        out.append(d)
    return out
