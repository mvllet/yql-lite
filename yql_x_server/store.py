import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any

from .config import settings

LON_STEPS = 36001
MAX_WOEID = 18000 * LON_STEPS + 36000


def encode_woeid(lat: float, lon: float) -> int:
    lat_units = min(max(int(round((float(lat) + 90) * 100)), 0), 18000)
    lon_units = min(max(int(round((float(lon) + 180) * 100)), 0), 36000)
    return lat_units * LON_STEPS + lon_units


def decode_woeid(woeid: int | str) -> tuple[float, float] | None:
    try:
        value = int(woeid)
    except (TypeError, ValueError):
        return None
    if value < 0 or value > MAX_WOEID:
        return None
    lat_units, lon_units = divmod(value, LON_STEPS)
    return lat_units / 100 - 90, lon_units / 100 - 180


@dataclass
class Place:
    woeid: int
    name: str
    country_code: str
    state: str
    latitude: float
    longitude: float


class TTLCache:
    def __init__(self, ttl: int):
        self.ttl = ttl
        self._data: dict[Any, tuple[float, Any]] = {}
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            entry = self._data.get(key)
            if not entry:
                return None
            stored_at, value = entry
            if time.time() - stored_at > self.ttl:
                del self._data[key]
                return None
            return value

    def set(self, key, value):
        with self._lock:
            self._data[key] = (time.time(), value)
        return value


_db_lock = threading.Lock()
_conn: sqlite3.Connection | None = None


def _db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(settings.db_path, check_same_thread=False)
        _conn.execute(
            """CREATE TABLE IF NOT EXISTS places (
                   woeid INTEGER PRIMARY KEY,
                   name TEXT NOT NULL,
                   country_code TEXT NOT NULL,
                   state TEXT NOT NULL,
                   latitude REAL NOT NULL,
                   longitude REAL NOT NULL
               )"""
        )
        _conn.commit()
    return _conn


def save_place(place: Place) -> Place:
    with _db_lock:
        _db().execute(
            "INSERT OR REPLACE INTO places VALUES (?, ?, ?, ?, ?, ?)",
            (
                place.woeid,
                place.name,
                place.country_code,
                place.state,
                place.latitude,
                place.longitude,
            ),
        )
        _db().commit()
    return place


def load_place(woeid: int | str) -> Place | None:
    try:
        key = int(woeid)
    except (TypeError, ValueError):
        return None
    with _db_lock:
        row = _db().execute("SELECT * FROM places WHERE woeid = ?", (key,)).fetchone()
    return Place(*row) if row else None
