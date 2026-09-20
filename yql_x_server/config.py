"""Runtime settings.

Values come from environment variables so the module imports cleanly under
tests. __main__.py overrides them from command line flags before the app
is imported.
"""

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


@dataclass
class Settings:
    host: str = field(default_factory=lambda: _env("YQLX_HOST", "0.0.0.0"))
    port: int = field(default_factory=lambda: int(_env("YQLX_PORT", "8000")))

    # Upstream services. Both are free and need no API key.
    weather_api: str = field(
        default_factory=lambda: _env("YQLX_WEATHER_API", "https://api.open-meteo.com/v1/forecast")
    )
    geocode_api: str = field(
        default_factory=lambda: _env(
            "YQLX_GEOCODE_API", "https://geocoding-api.open-meteo.com/v1/search"
        )
    )
    reverse_geocode_api: str = field(
        default_factory=lambda: _env(
            "YQLX_REVERSE_GEOCODE_API",
            "https://api.bigdatacloud.net/data/reverse-geocode-client",
        )
    )

    # Links shown inside the Weather and Stocks apps.
    advert_link: str = field(default_factory=lambda: _env("YQLX_ADVERT_LINK", "https://open-meteo.com"))

    # Where the woeid -> place-name table lives.
    db_path: Path = field(default_factory=lambda: Path(_env("YQLX_DB_PATH", "places.db")))

    http_timeout: float = field(default_factory=lambda: float(_env("YQLX_HTTP_TIMEOUT", "10")))
    weather_ttl: int = field(default_factory=lambda: int(_env("YQLX_WEATHER_TTL", "900")))
    stocks_ttl: int = field(default_factory=lambda: int(_env("YQLX_STOCKS_TTL", "900")))
    log_queries: bool = field(default_factory=lambda: _env("YQLX_LOG_QUERIES", "1") == "1")

    def update(self, **kwargs) -> None:
        for key, value in kwargs.items():
            if value is not None:
                setattr(self, key, value)


settings = Settings()
