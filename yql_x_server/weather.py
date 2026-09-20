"""Weather from Open-Meteo, shaped into the fields the templates want.

The old YzuWeather provider proxied Open-Meteo through a private host. This
calls Open-Meteo directly. Three fixes over the original:

- Forecast days are indexed by position, not by day-of-week number. The old
  code used the weekday (1 to 7) as an array index, so the six day forecast
  only lined up when tomorrow happened to be a Wednesday.
- Visibility is read from the hourly block. Open-Meteo has no visibility in
  the current block, so the old code always fell back to a constant.
- Moon phase is computed from the synodic month instead of pulling in ephem,
  and illumination is a real cosine rather than the lunation percentage.
"""

import math
from datetime import datetime, timedelta, timezone

import httpx

from .config import settings
from .store import TTLCache

_weather_cache = TTLCache(ttl=settings.weather_ttl)

CURRENT_FIELDS = (
    "temperature_2m,apparent_temperature,relative_humidity_2m,dew_point_2m,"
    "pressure_msl,wind_speed_10m,wind_direction_10m,weather_code,is_day"
)
HOURLY_FIELDS = "temperature_2m,precipitation_probability,weather_code,is_day,visibility"
DAILY_FIELDS = (
    "temperature_2m_max,temperature_2m_min,weather_code,sunrise,sunset,"
    "precipitation_probability_max"
)

# WMO weather code to the Yahoo condition code the Weather app draws icons
# from. Pairs are (day icon, night icon).
WMO_TO_YAHOO: dict[int, tuple[int, int]] = {
    0: (32, 31),    # clear sky
    1: (34, 33),    # mainly clear
    2: (30, 29),    # partly cloudy
    3: (26, 26),    # overcast
    45: (20, 20),   # fog
    48: (20, 20),   # depositing rime fog
    51: (9, 9),     # light drizzle
    53: (9, 9),     # drizzle
    55: (9, 9),     # dense drizzle
    56: (8, 8),     # light freezing drizzle
    57: (8, 8),     # dense freezing drizzle
    61: (11, 11),   # slight rain
    63: (11, 11),   # moderate rain
    65: (40, 40),   # heavy rain
    66: (10, 10),   # light freezing rain
    67: (10, 10),   # heavy freezing rain
    71: (13, 13),   # slight snowfall
    73: (16, 16),   # moderate snowfall
    75: (41, 41),   # heavy snowfall
    77: (13, 13),   # snow grains
    80: (39, 11),   # slight rain showers
    81: (11, 11),   # moderate rain showers
    82: (40, 40),   # violent rain showers
    85: (46, 46),   # slight snow showers
    86: (42, 42),   # heavy snow showers
    95: (4, 4),     # thunderstorm
    96: (35, 35),   # thunderstorm with slight hail
    99: (17, 17),   # thunderstorm with heavy hail
}


def weather_icon(code, is_day) -> int:
    try:
        pair = WMO_TO_YAHOO[int(code)]
    except (TypeError, ValueError, KeyError):
        return 26  # cloudy, the least alarming unknown
    return pair[0] if is_day else pair[1]


SYNODIC_MONTH = 29.530588853
KNOWN_NEW_MOON = datetime(2000, 1, 6, 18, 14, tzinfo=timezone.utc)


def moon(now: datetime) -> tuple[int, int]:
    """Returns (percent of the disc lit, phase marker).

    The phase marker matches what the old server sent: 0 around a new moon,
    1 while waxing, 5 while waning.
    """
    days = (now - KNOWN_NEW_MOON).total_seconds() / 86400.0
    lunation = (days % SYNODIC_MONTH) / SYNODIC_MONTH
    illumination = int(round((1 - math.cos(2 * math.pi * lunation)) / 2 * 100))
    if illumination <= 1:
        return illumination, 0
    return illumination, 1 if lunation < 0.5 else 5


def gmt_label(utc_offset_seconds: int) -> str:
    hours = int(utc_offset_seconds) // 3600
    return f"GMT{hours}" if hours < 0 else f"GMT+{hours}"


def _local(timestamp, tz: timezone) -> datetime:
    return datetime.fromtimestamp(int(timestamp), tz)


async def fetch(client: httpx.AsyncClient, lat: float, lon: float) -> dict:
    key = (round(float(lat), 2), round(float(lon), 2))
    cached = _weather_cache.get(key)
    if cached is not None:
        return cached

    response = await client.get(
        settings.weather_api,
        params={
            "latitude": lat,
            "longitude": lon,
            "current": CURRENT_FIELDS,
            "hourly": HOURLY_FIELDS,
            "daily": DAILY_FIELDS,
            "timezone": "auto",
            "timeformat": "unixtime",
            "forecast_days": 7,
            "wind_speed_unit": "kmh",
        },
    )
    response.raise_for_status()
    return _weather_cache.set(key, normalise(response.json()))


def normalise(data: dict) -> dict:
    current = data["current"]
    hourly = data["hourly"]
    daily = data["daily"]
    tz = timezone(timedelta(seconds=int(data.get("utc_offset_seconds", 0))))

    now = _local(current["time"], tz)
    sunrise = _local(daily["sunrise"][0], tz)
    sunset = _local(daily["sunset"][0], tz)
    is_day = bool(current.get("is_day", 1))

    # First hourly slot strictly after the current reading.
    hour_index = 0
    for index, stamp in enumerate(hourly["time"]):
        if int(stamp) > int(current["time"]):
            hour_index = index
            break

    hours = []
    for index in range(hour_index, min(hour_index + 12, len(hourly["time"]))):
        slot = _local(hourly["time"][index], tz)
        hours.append(
            {
                "condition_code": weather_icon(
                    hourly["weather_code"][index], hourly["is_day"][index]
                ),
                "poP": hourly["precipitation_probability"][index] or 0,
                "temp": hourly["temperature_2m"][index],
                "time_24h": slot.strftime("%H:%M"),
            }
        )

    # daily[0] is today. The app shows the next six days.
    days = []
    for index in range(1, min(7, len(daily["time"]))):
        slot = _local(daily["time"][index], tz)
        high = daily["temperature_2m_max"][index]
        low = daily["temperature_2m_min"][index]
        days.append(
            {
                "ordinal": slot.weekday() + 1,  # Monday is 1, Sunday is 7
                "condition_code": weather_icon(daily["weather_code"][index], True),
                "condition_text": "",
                "high": high,
                "high_rounded": round(high),
                "low": low,
                "low_rounded": round(low),
                "poP": daily["precipitation_probability_max"][index] or 0,
            }
        )

    visibility_m = hourly["visibility"][max(hour_index - 1, 0)] if hourly.get("visibility") else None
    feels_like = current.get("apparent_temperature")
    if feels_like is None:
        feels_like = current["temperature_2m"]
    moon_visible, moon_phase = moon(now.astimezone(timezone.utc))

    return {
        "barometer": int(current["pressure_msl"]),
        "condition_code": weather_icon(current["weather_code"], is_day),
        "condition_text": "",
        "current_time_12h": now.strftime("%I:%M %p").lstrip("0"),
        "current_time_24h": now.strftime("%H:%M"),
        "days": days,
        "dew_point": current["dew_point_2m"],
        "feels_like": feels_like,
        "hours": hours,
        "moonfacevisible": moon_visible,
        "moonphase": moon_phase,
        "p_humidity": current["relative_humidity_2m"],
        "sunrise_12h": sunrise.strftime("%I:%M %p").lstrip("0"),
        "sunrise_24h": sunrise.strftime("%H:%M"),
        "sunset_12h": sunset.strftime("%I:%M %p").lstrip("0"),
        "sunset_24h": sunset.strftime("%H:%M"),
        "temp": current["temperature_2m"],
        "temp_rounded": round(current["temperature_2m"]),
        "timezone": gmt_label(data.get("utc_offset_seconds", 0)),
        "visibility": round((visibility_m or 10000) / 1000, 1),
        "wind_chill": feels_like,
        "wind_deg": current.get("wind_direction_10m", 0),
        "wind_speed": current.get("wind_speed_10m", 0),
    }
