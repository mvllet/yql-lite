import html
import re
from dataclasses import dataclass, field

LANG_RE = re.compile(r"lang=['\"]([^'\"]+)['\"]")
TERM_RE = re.compile(r'query\s*=\s*["\']([^"\']+)["\']')
LAT_RE = re.compile(r"lat\s*=\s*(-?\d+(?:\.\d+)?)")
LON_RE = re.compile(r"lon\s*=\s*(-?\d+(?:\.\d+)?)")
WOEID_RE = re.compile(r"woeid\s*(?:=|in)\s*\(?\s*['\"]?(\d+)")
ANY_NUMBER_RE = re.compile(r"\b\d{4,}\b")


@dataclass
class Query:
    kind: str  # "search", "latlon" or "woeid"
    lang: str = "en-US"
    term: str = ""
    lat: float | None = None
    lon: float | None = None
    woeids: list[str] = field(default_factory=list)


def parse(raw: str) -> Query | None:
    if not raw:
        return None
    text = html.unescape(raw)
    lang_match = LANG_RE.search(text)
    lang = lang_match.group(1) if lang_match else "en-US"

    if "weather.locations" in text and "yql.query.multi" not in text:
        term_match = TERM_RE.search(text)
        if term_match:
            return Query(kind="search", lang=lang, term=term_match.group(1))

    lat_match, lon_match = LAT_RE.search(text), LON_RE.search(text)
    if lat_match and lon_match:
        return Query(
            kind="latlon",
            lang=lang,
            lat=float(lat_match.group(1)),
            lon=float(lon_match.group(1)),
        )

    if "woeid" in text:
        woeids: list[str] = []
        for match in WOEID_RE.finditer(text):
            if match.group(1) not in woeids:
                woeids.append(match.group(1))
        if not woeids:
            for number in ANY_NUMBER_RE.findall(text):
                if number not in woeids:
                    woeids.append(number)
        if woeids:
            return Query(kind="woeid", lang=lang, woeids=woeids)

    return None
