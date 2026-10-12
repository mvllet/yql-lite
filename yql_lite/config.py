import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass
class Settings:
    host: str = field(default_factory=lambda: _env("YQL_LITE_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(os.environ.get("YQL_LITE_PORT", "8000")))

    weather_api: str = field(
        default_factory=lambda: _env("YQL_LITE_WEATHER_API", "https://api.open-meteo.com/v1/forecast")
    )
    geocode_api: str = field(
        default_factory=lambda: _env(
            "YQL_LITE_GEOCODE_API", "https://geocoding-api.open-meteo.com/v1/search"
        )
    )
    reverse_geocode_api: str = field(
        default_factory=lambda: _env(
            "YQL_LITE_REVERSE_GEOCODE_API",
            "https://api.bigdatacloud.net/data/reverse-geocode-client",
        )
    )

    advert_link: str = field(default_factory=lambda: _env("YQL_LITE_ADVERT_LINK", "https://open-meteo.com"))

    db_path: Path = field(default_factory=lambda: Path(_env("YQL_LITE_DB_PATH", "places.db")))

    http_timeout: float = field(default_factory=lambda: float(_env("YQL_LITE_HTTP_TIMEOUT", "10")))
    weather_ttl: int = field(default_factory=lambda: int(_env("YQL_LITE_WEATHER_TTL", "900")))
    stocks_ttl: int = field(default_factory=lambda: int(_env("YQL_LITE_STOCKS_TTL", "900")))
    log_queries: bool = field(default_factory=lambda: _env("YQL_LITE_LOG_QUERIES", "1") == "1")
    log_file: Path = field(default_factory=lambda: Path(_env("YQL_LITE_LOG_FILE", "server.log")))

    def update(self, **kwargs) -> None:
        for key, value in kwargs.items():
            if value is not None:
                setattr(self, key, value)


settings = Settings()
