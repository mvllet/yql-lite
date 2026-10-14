import httpx
from iso3166 import countries

from .config import settings
from .store import Place, TTLCache, decode_woeid, encode_woeid, load_place, save_place

_search_cache = TTLCache(ttl=86400, maxsize=500)
_reverse_cache = TTLCache(ttl=86400, maxsize=2000)

UNKNOWN_COUNTRY = "UNK"


def short_lang(lang: str | None) -> str:
    if not lang:
        return "en"
    return lang.replace("_", "-").split("-")[0].lower()[:2] or "en"


def alpha3(country_code: str) -> str:
    try:
        return countries.get(country_code).alpha3
    except KeyError:
        return UNKNOWN_COUNTRY


def country_name(country_code: str) -> str:
    try:
        return countries.get(country_code).name
    except KeyError:
        return country_code or "Unknown"


def _to_place(result: dict) -> Place:
    latitude = float(result["latitude"])
    longitude = float(result["longitude"])
    return Place(
        woeid=encode_woeid(latitude, longitude),
        name=str(result.get("name") or "Unknown")[:80],
        country_code=str(result.get("country_code") or "").upper()[:2],
        state=str(result.get("admin1") or "")[:80],
        latitude=latitude,
        longitude=longitude,
    )


async def search(client: httpx.AsyncClient, term: str, lang: str, limit: int = 10) -> list[Place]:
    term = (term or "").strip()
    if len(term) < 2:
        return []

    key = (term.lower(), short_lang(lang), limit)
    cached = _search_cache.get(key)
    if cached is not None:
        return cached

    response = await client.get(
        settings.geocode_api,
        params={"name": term, "count": limit, "language": short_lang(lang), "format": "json"},
    )
    response.raise_for_status()
    results = response.json().get("results") or []

    places = [_to_place(item) for item in results if item.get("latitude") is not None]
    for place in places:
        save_place(place)
    return _search_cache.set(key, places)


async def reverse(client: httpx.AsyncClient, lat: float, lon: float, lang: str) -> Place:
    key = (round(float(lat), 2), round(float(lon), 2), short_lang(lang))
    cached = _reverse_cache.get(key)
    if cached is not None:
        return cached

    name, country_code, state = "", "", ""
    try:
        response = await client.get(
            settings.reverse_geocode_api,
            params={"latitude": lat, "longitude": lon, "localityLanguage": short_lang(lang)},
        )
        response.raise_for_status()
        data = response.json()
        name = data.get("city") or data.get("locality") or data.get("principalSubdivision") or ""
        country_code = (data.get("countryCode") or "").upper()
        state = data.get("principalSubdivision") or ""
    except (httpx.HTTPError, ValueError):
        pass

    place = Place(
        woeid=encode_woeid(lat, lon),
        name=str(name or "Current Location")[:80],
        country_code=str(country_code or "").upper()[:2],
        state=str(state or "")[:80],
        latitude=float(lat),
        longitude=float(lon),
    )
    save_place(place)
    return _reverse_cache.set(key, place)


async def resolve(client: httpx.AsyncClient, woeid: str, lang: str) -> Place | None:
    place = load_place(woeid)
    if place:
        return place

    coordinates = decode_woeid(woeid)
    if coordinates is None:
        return None
    return await reverse(client, coordinates[0], coordinates[1], lang)
