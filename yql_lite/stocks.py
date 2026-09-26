from datetime import datetime, timezone
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

import yfinance

from .config import settings
from .store import TTLCache

_quote_cache = TTLCache(ttl=settings.stocks_ttl)
_chart_cache = TTLCache(ttl=3600)

RANGES = {
    "1d": ("1d", "1m"),
    "5d": ("5d", "5m"),
    "1m": ("1mo", "15m"),
    "3m": ("3mo", "1h"),
    "6m": ("6mo", "1h"),
    "1y": ("1y", "1d"),
    "2y": ("2y", "1d"),
    "5y": ("5y", "1wk"),
    "10y": ("10y", "1wk"),
}


def _num(value, default=0):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _pick(fast, info, fast_key, info_key, default=0):
    try:
        value = fast[fast_key]
        if value is not None:
            return _num(value, default)
    except Exception:
        pass
    return _num(info.get(info_key), default)


def _change_percent(current, previous):
    if not previous or current == previous:
        return "0%"
    sign = "+" if current > previous else "-"
    return f"{sign}{abs(current - previous) / previous * 100:.2f}%"


def _news_items(ticker) -> list[dict]:
    try:
        raw = ticker.get_news() or []
    except Exception:
        return []

    items = []
    for entry in raw:
        content = entry.get("content") if isinstance(entry, dict) else None
        if isinstance(content, dict):
            title = content.get("title") or ""
            link = (content.get("canonicalUrl") or {}).get("url") or ""
            published = content.get("pubDate") or ""
            try:
                stamp = int(
                    datetime.strptime(published, "%Y-%m-%dT%H:%M:%SZ")
                    .replace(tzinfo=timezone.utc)
                    .timestamp()
                )
            except (TypeError, ValueError):
                stamp = 0
        else:
            title = entry.get("title") or ""
            link = entry.get("link") or ""
            stamp = int(_num(entry.get("providerPublishTime"), 0))

        if title and link:
            items.append({"title": escape(title), "link": escape(link), "published": stamp})
    return items


def _gmt_offset(timezone_name: str) -> int:
    try:
        offset = datetime.now(ZoneInfo(timezone_name)).utcoffset()
        return int(offset.total_seconds() // 3600) if offset else 0
    except Exception:
        return 0


class Quote:
    def __init__(self, symbol: str):
        ticker = yfinance.Ticker(symbol)
        fast = ticker.fast_info
        try:
            info = ticker.get_info() or {}
        except Exception:
            info = {}

        self.name = escape(symbol.upper())
        long_name = info.get("longName") or info.get("shortName") or symbol
        self.name_short = escape(long_name[:12] + "..." if len(long_name) > 12 else long_name)

        self.price = _pick(fast, info, "lastPrice", "regularMarketPrice")
        self.open = _pick(fast, info, "open", "regularMarketOpen")
        self.previous_close = _pick(fast, info, "previousClose", "previousClose")
        self.high = _pick(fast, info, "dayHigh", "regularMarketDayHigh")
        self.low = _pick(fast, info, "dayLow", "regularMarketDayLow")
        self.market_cap = int(_pick(fast, info, "marketCap", "marketCap"))
        self.volume = int(_pick(fast, info, "lastVolume", "volume"))
        self.average_volume = int(_pick(fast, info, "threeMonthAverageVolume", "averageVolume"))
        self.peratio = round(_num(info.get("trailingPE")), 2)
        self.dividend_yield = round(_num(info.get("dividendYield")), 2)

        year_low = _pick(fast, info, "yearLow", "fiftyTwoWeekLow")
        year_high = _pick(fast, info, "yearHigh", "fiftyTwoWeekHigh")
        self.yearrange = f"{year_low:.2f} - {year_high:.2f}"

        self.change_percent = _change_percent(self.open, self.previous_close)
        self.change = self.change_percent
        self.real_time_change = self.change_percent

        self.gmt_offset = _gmt_offset(info.get("exchangeTimezoneName") or "America/New_York")
        self.valid = bool(self.price or self.previous_close)
        self.news = _news_items(ticker)
        self.news_count = len(self.news)


def get_quote(symbol: str) -> Quote | None:
    symbol = (symbol or "").strip()
    if not symbol:
        return None
    cached = _quote_cache.get(symbol.upper())
    if cached is not None:
        return cached
    try:
        quote = Quote(symbol)
    except Exception as error:
        print(f"quote failed for {symbol}: {error}")
        return None
    if not quote.valid:
        return None
    return _quote_cache.set(symbol.upper(), quote)


def get_chart(symbol: str, range_key: str) -> list[dict]:
    period_interval = RANGES.get((range_key or "").lower())
    if not period_interval:
        return []
    key = (symbol.upper(), range_key.lower())
    cached = _chart_cache.get(key)
    if cached is not None:
        return cached

    period, interval = period_interval
    try:
        frame = yfinance.Ticker(symbol).history(period=period, interval=interval)
    except Exception as error:
        print(f"chart failed for {symbol} {range_key}: {error}")
        return []
    if frame is None or frame.empty:
        return []

    points = [
        {
            "timestamp": int(stamp.timestamp()),
            "close": round(float(row["Close"]), 4),
            "volume": int(row["Volume"]) if row["Volume"] == row["Volume"] else 0,
        }
        for stamp, row in frame.iterrows()
        if row["Close"] == row["Close"]
    ]

    if len(points) < 3:
        print(f"chart for {symbol} {range_key} only returned {len(points)} points, not caching")
        return points

    return _chart_cache.set(key, points)
