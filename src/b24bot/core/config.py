"""Конфигурация сервиса. Единственное место, где читается окружение."""
from __future__ import annotations

import base64
import ipaddress
from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Инвариант И-4: хосты внешних вызовов только отсюда, никогда из входящих данных.
OAUTH_HOSTS: tuple[str, ...] = ("oauth.bitrix24.tech", "oauth.bitrix.info")
B24_DOMAIN_SUFFIXES: tuple[str, ...] = (".bitrix24.ru", ".bitrix24.com", ".bitrix24.by",
                                        ".bitrix24.kz", ".bitrix24.de")
TELEGRAM_HOST = "api.telegram.org"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    domain: str = Field(default="localhost")
    public_base_url: str = Field(default="http://localhost:8000")

    database_url: str

    master_key: str
    master_key_id: int = 1

    b24_client_id: str = ""
    b24_client_secret: str = ""

    # Передавать ли `redirect_uri` на экран согласия портала (`b24/oauth.py`).
    # По умолчанию НЕТ: у локального приложения обработчик один, документация
    # разрешает параметр не слать, а незарегистрированный адрес портал вправе
    # отвергнуть — и тогда привязка не начнётся вовсе. Включать только после
    # того, как адрес приёма кода прописан обработчиком приложения на портале.
    b24_oauth_redirect: bool = False

    # api.telegram.org по адресу из DNS с сервера недоступен (проверено 16.08.2026:
    # таймаут). Прокси — запасной путь к Telegram, первый — прямые адреса ниже.
    # Входящие вебхуки прокси НЕ требуют: Telegram сам стучится на наш домен.
    tg_proxy_url: str = ""

    # Прямые адреса api.telegram.org — первый путь к Telegram (tg/api.py, `Route`).
    # 23.09.2026 на боевом сервере: DNS отдаёт 149.154.166.110 — таймаут, а
    # 149.154.167.220 отвечает за 0.2 с и отдаёт хоть 47 КБ, тогда как прокси
    # замораживает соединение после ~16 КБ, и крупный апдейт не доходил никогда.
    # Через запятую; пусто — прямых адресов нет. В TLS всегда называется
    # api.telegram.org, так что чужой адрес не пройдёт проверку сертификата и
    # токен туда не уйдёт.
    tg_api_ips: str = "149.154.167.220"

    # Короткое имя мини-аппа из BotFather (`/newapp`). Ссылка вида
    # t.me/<бот>/<имя>?startapp=… — единственный способ открыть мини-апп из группы:
    # кнопки web_app в инлайн-клавиатуре Telegram разрешает только в личке.
    # Значение общее для всех ботов теннантов; отдельное имя у теннанта хранится
    # в tg_bots.miniapp_short_name и имеет приоритет.
    miniapp_short_name: str = ""
    # Куда собран фронтенд. В образе это /app/web/miniapp, локально — пусто.
    miniapp_dist: str = "/app/web/miniapp"

    # Второй рубеж изоляции: соединения объявляют RLS-enforce (db/pool.py).
    # По умолчанию выключено: политики в базе (миграция 0020) инертны, пока
    # соединение само не скажет `app.rls='enforce'`. Порядок включения на
    # боевом сервере — docs/80-deploy.md §9. В CI включён всегда (conftest).
    rls_enforce: bool = False

    log_level: str = "INFO"

    @field_validator("master_key")
    @classmethod
    def _check_master_key(cls, v: str) -> str:
        raw = base64.urlsafe_b64decode(v + "=" * (-len(v) % 4))
        if len(raw) != 32:
            raise ValueError("MASTER_KEY должен быть 32 байтами в base64url")
        return v

    @field_validator("tg_api_ips")
    @classmethod
    def _check_tg_api_ips(cls, v: str) -> str:
        # Только адреса: имя хоста здесь означало бы второй источник DNS, а весь
        # смысл настройки в том, что DNS с этого хоста ведёт в закрытый адрес.
        for item in v.split(","):
            if item.strip():
                ipaddress.ip_address(item.strip())
        return v

    @property
    def tg_direct_ips(self) -> tuple[str, ...]:
        return tuple(i.strip() for i in self.tg_api_ips.split(",") if i.strip())

    @property
    def master_key_bytes(self) -> bytes:
        return base64.urlsafe_b64decode(self.master_key + "=" * (-len(self.master_key) % 4))

    @property
    def tg_proxy(self) -> str | None:
        """Прокси для httpx с ОБЯЗАТЕЛЬНЫМ удалённым резолвом DNS.

        socks5:// заставляет клиента резолвить имя локально, а локальный DNS отдаёт
        заблокированный адрес — соединение уходит в таймаут. socks5h:// передаёт имя
        прокси, и он резолвит сам. Проверено: socks5h — 401 за 0.5 с, socks5 — таймаут.
        """
        url = self.tg_proxy_url.strip()
        if not url:
            return None
        if url.startswith("socks5://"):
            url = "socks5h://" + url[len("socks5://"):]
        return url

    @property
    def asyncpg_dsn(self) -> str:
        return self.database_url.replace("postgresql+asyncpg://", "postgresql://")


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


def is_trusted_portal_domain(domain: str) -> bool:
    """Домен портала считается доверенным, только если это домен Битрикс24.

    Используется для Content-Security-Policy: frame-ancestors. Значение приходит
    из входящего запроса, поэтому проверяется, а не применяется как есть.
    """
    d = (domain or "").strip().lower()
    if not d or "/" in d or " " in d:
        return False
    return any(d.endswith(s) for s in B24_DOMAIN_SUFFIXES)
