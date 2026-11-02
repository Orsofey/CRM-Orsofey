"""HTTP для новых соцсетей: нужны заголовки ответа (DPoP-nonce Bluesky), multipart (e621, Discord, Pinterest) и сырой текст
(страницы). Транспорт подменяемый — в тестах вместо сети подставляется функция."""
import json as _json
import logging

log = logging.getLogger(__name__)


class Resp:
    def __init__(self, status: int, headers: dict | None = None, body: bytes | str | None = b"", url: str = ""):
        self.status = status
        self.url = url              # итоговый адрес после переадресаций
        self.headers = {k.lower(): v for k, v in (headers or {}).items()}
        self.body = body.encode() if isinstance(body, str) else (body or b"")

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", "replace")

    def json(self):
        try:
            return _json.loads(self.body or b"null")
        except ValueError:
            return None

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


TRANSPORT = None   # тесты: async def fake(method, url, **kw) -> Resp


async def request(method: str, url: str, *, params=None, data=None, json=None, headers=None, body: bytes | None = None,
                  files: list | None = None, proxy: str | None = None, timeout: int = 60, redirects: bool = True) -> Resp:
    """files: [(поле, имя_файла, путь_или_байты, mime)] — отправка multipart вместе с data."""
    if TRANSPORT:
        return await TRANSPORT(method, url, params=params, data=data, json=json, headers=headers or {}, body=body, files=files)
    import aiohttp
    kw = {"params": {k: str(v) for k, v in params.items()} if params else None, "headers": headers or {}, "proxy": proxy,
          "timeout": aiohttp.ClientTimeout(total=timeout),
          "allow_redirects": redirects}
    if files:
        form = aiohttp.FormData()
        for k, v in (data or {}).items():
            form.add_field(k, str(v))
        handles = []
        for field, name, src, mime in files:
            if isinstance(src, (bytes, bytearray)):
                form.add_field(field, bytes(src), filename=name, content_type=mime)
            else:
                fh = open(src, "rb")
                handles.append(fh)
                form.add_field(field, fh, filename=name, content_type=mime)
        kw["data"] = form
    elif body is not None:
        kw["data"] = body
    elif json is not None:
        kw["json"] = json
    elif data is not None:
        kw["data"] = data
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.request(method, url, **kw) as r:
                return Resp(r.status, dict(r.headers), await r.read(), str(r.url))
    finally:
        for fh in locals().get("handles", []) or []:
            try:
                fh.close()
            except Exception:
                pass
