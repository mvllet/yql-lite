"""Stocks.app data, pulled from Yahoo through yfinance.

Everything here is defensive. yfinance reshapes its payloads often, and the
news feed in particular has had two different layouts. Missing fields become
zeros rather than exceptions, because a quote with a blank market cap still
renders in Stocks.app while a 500 shows nothing at all.
"""

from datetime import datetime, timezone
from xml.sax.saxutils import escape
from zoneinfo import ZoneInfo

import yfinance

from .config import settings
from .store import TTLCache

_quote_cache = TTLCache(ttl=settings.stocks_ttl)
_chart_cache = TTLCache(ttl=3600)

RANGES = {
    "1d": ("1d", "15m"),
    "5d": ("5d", "15m"),
    "1m": ("1mo", "1d"),
    "3m": ("3mo", "1d"),
    "6m": ("6mo", "1wk"),
    "1y": ("1y", "1wk"),
    "2y": ("2y", "1wk"),
    "5y": ("5y", "1wk"),
    "10y": ("10y", "1mo"),
}


def _number(value, default=0):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _fast(fast_info, key, default=0):
    try:
        return _number(fast_info[key], default)
    except Exception:
        return default


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
            stamp = int(_number(entry.get("providerPublishTime"), 0))

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
    """One row in the Stocks list."""

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

        self.price = _fast(fast, "lastPrice") or _number(info.get("regularMarketPrice"))
        self.open = _fast(fast, "open") or _number(info.get("regularMarketOpen"))
        self.previous_close = _fast(fast, "previousClose") or _number(info.get("previousClose"))
        self.high = _fast(fast, "dayHigh") or _number(info.get("regularMarketDayHigh"))
        self.low = _fast(fast, "dayLow") or _number(info.get("regularMarketDayLow"))
        self.market_cap = int(_fast(fast, "marketCap") or _number(info.get("marketCap")))
        self.volume = int(_fast(fast, "lastVolume") or _number(info.get("volume")))
        self.average_volume = int(
            _fast(fast, "threeMonthAverageVolume") or _number(info.get("averageVolume"))
        )
        self.peratio = round(_number(info.get("trailingPE")), 2)
        self.dividend_yield = round(_number(info.get("dividendYield")), 2)

        year_low = _fast(fast, "yearLow") or _number(info.get("fiftyTwoWeekLow"))
        year_high = _fast(fast, "yearHigh") or _number(info.get("fiftyTwoWeekHigh"))
        self.yearrange = f"{year_low:.2f} - {year_high:.2f}"

        # The original compared the open against the previous close and then
        # wrote the percentage string into both the change and changepercent
        # fields. Change is an absolute currency amount.
        delta = self.price - self.previous_close if self.previous_close else 0.0
        self.change = f"{delta:+.2f}"
        self.real_time_change = self.change
        if self.previous_close:
            self.change_percent = f"{delta / self.previous_close * 100:+.2f}%"
        else:
            self.change_percent = "0.00%"

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
    except Exception as error:  # noqa: BLE001
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
    except Exception as error:  # noqa: BLE001
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
        if row["Close"] == row["Close"]  # drops NaN rows
    ]
    return _chart_cache.set(key, points)
