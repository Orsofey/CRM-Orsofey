import os
from dataclasses import dataclass
from dotenv import dotenv_values

# Настройки берём из переменных окружения (Amvera → «Переменные»), а чего там нет или пусто — из файла .env.
# .env ищем в папке проекта и в постоянном хранилище /data (туда его можно положить один раз — переживёт пересборки).
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _path in (os.path.join(_ROOT, ".env"), "/data/.env", os.path.join(os.getcwd(), ".env")):
    if os.path.isfile(_path):
        for _k, _v in dotenv_values(_path).items():
            if _v and not os.environ.get(_k):
                os.environ[_k] = _v


def _f(name: str, default: str) -> float:
    try:
        return float(os.getenv(name, default) or default)
    except ValueError:
        return float(default)


@dataclass(frozen=True)
class Config:
    token: str = os.getenv("BOT_TOKEN", "")
    admins: tuple = tuple(int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x)
    # Кому ещё можно входить в CRM (командой /panel), без уведомлений и сводок. Через запятую
    # 1 — любой, кто напишет /panel, сразу получает свою CRM без подтверждения автора
    auto_workspaces: bool = os.getenv("AUTO_WORKSPACES", "").strip() in ("1", "true", "yes")
    panel_ids: tuple = tuple(int(x) for x in os.getenv("PANEL_IDS", "").replace(" ", "").split(",") if x.strip().lstrip("-").isdigit())
    # Канал для проверки подписки: @username или числовой id (-100…). Для приватного канала id бот узнает сам,
    # когда ты добавишь его админом, — см. README.
    channel: str = os.getenv("CHANNEL", "").strip()
    # Ссылка на канал для кнопки «Подписаться» (для приватного канала — инвайт-ссылка t.me/+…)
    channel_url: str = os.getenv("CHANNEL_URL", "").strip()
    lead_file: str = os.getenv("LEAD_MAGNET_FILE", "").strip()
    lead_url: str = os.getenv("LEAD_MAGNET_URL", "").strip()
    app_url: str = os.getenv("APP_URL", "").strip()
    price_stars: int = int(os.getenv("PRICE_STARS", "250") or 250)
    sub_title: str = os.getenv("SUBSCRIPTION_TITLE", "Orsofey Assistant · подписка")
    external_pay_url: str = os.getenv("EXTERNAL_PAY_URL", "").strip()
    followup_test_h: float = _f("FOLLOWUP_TEST_H", "24")
    followup_feedback_h: float = _f("FOLLOWUP_FEEDBACK_H", "48")
    followup_final_h: float = _f("FOLLOWUP_FINAL_H", "120")
    # на Amvera (и любом сервере с папкой /data) база сама ложится в постоянное хранилище
    db_path: str = os.getenv("DB_PATH") or ("/data/bot.db" if os.path.isdir("/data") else "data/bot.db")

    # ---- CRM ----
    # Через сколько часов без следующего шага человек считается «обвалившимся» (и получает напоминание)
    drop_hours: float = _f("DROP_HOURS", "24")
    # Адрес, по которому открывается панель CRM (домен проекта на Amvera), без слэша в конце
    public_url: str = os.getenv("PUBLIC_URL", "").strip().rstrip("/")
    port: int = int(os.getenv("PORT", "8080") or 8080)
    # Необязательный пароль для входа в панель (без него вход — по коду из команды /panel в боте)
    panel_password: str = os.getenv("PANEL_PASSWORD", "").strip()
    tz: str = os.getenv("TZ_NAME", "Europe/Moscow").strip() or "Europe/Moscow"
    report_hour: int = int(os.getenv("REPORT_HOUR", "9") or 9)
    # Необязательно: ключ YouTube Data API — просмотры роликов YouTube будут подтягиваться сами
    yt_api_key: str = os.getenv("YT_API_KEY", "").strip()
    # Необязательно: HTTP-прокси для соцсетей, недоступных с хостинга (например, Instagram из России): http://user:pass@host:port
    social_proxy: str = os.getenv("SOCIAL_PROXY", "").strip()
    ig_api_version: str = os.getenv("IG_API_VERSION", "v24.0").strip() or "v24.0"
    # Максимальный размер видео для загрузки в «Публикацию», МБ (видео лежит на диске Amvera до публикации)
    max_upload_mb: int = int(os.getenv("MAX_UPLOAD_MB", "4096") or 4096)


cfg = Config()
SUBSCRIPTION_PERIOD = 30 * 24 * 60 * 60  # Telegram принимает только 30 дней (2592000 с)
