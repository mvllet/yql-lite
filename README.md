# YQL-X-Server 2

Weather and Stocks backend for iOS 5, 6 and 7. Point the WeatherX / NDBX tweak
at this server and the stock Weather and Stocks apps work again.

## Quick start

Requires [uv](https://docs.astral.sh/uv/) and Python 3.13.

    uv venv --python 3.13
    source .venv/bin/activate          # Windows: .venv\Scripts\activate
    uv pip install -e .
    yql-x-server

The server listens on 0.0.0.0:8000. On the device set both the weather and the
stocks URL to `http://YOUR-IP:8000`.

Check it before touching the device:

    python selftest.py

## Flags

    yql-x-server --host 0.0.0.0 --port 8000 --db-path places.db --quiet

Every flag also reads an environment variable: `YQLX_HOST`, `YQLX_PORT`,
`YQLX_DB_PATH`, `YQLX_ADVERT_LINK`, `YQLX_WEATHER_API`, `YQLX_GEOCODE_API`,
`YQLX_REVERSE_GEOCODE_API`, `YQLX_HTTP_TIMEOUT`, `YQLX_WEATHER_TTL`,
`YQLX_STOCKS_TTL`, `YQLX_LOG_QUERIES`.

## Endpoints

| Method | Path | Used by |
|---|---|---|
| GET | `/yql/weather?q=...` | Weather.app |
| GET | `/v1/yql?q=...` | Weather.app, older tweak builds |
| POST | `/dgw` | Stocks.app |
| GET | `/` | status |

## Upstream services

No API keys anywhere.

- Weather: Open-Meteo forecast API
- City search: Open-Meteo geocoding API
- Reverse geocoding for GPS locations: BigDataCloud client endpoint
- Stocks: Yahoo Finance via yfinance

## WOEIDs

Yahoo's WOEID database is gone. This server mints its own ids by packing the
rounded coordinates into a single integer, so a woeid decodes back to a
lat/lon pair with no lookup. Display names are cached in `places.db` when a
city is searched, and recovered by reverse geocoding when the file is missing.

Ids are nine digits and fit in a signed 32 bit integer.

## Differences from the original server

- Dropped the DGW weather protocol (iOS 2 to 4). Only Stocks uses DGW here.
- Dropped Redis, Sentry, Prometheus, geopy, ephem, timezonefinder,
  starlette-context and the three provider abstraction layers.
- Forecast days are indexed by position. The original indexed daily arrays by
  day-of-week number, so the six day forecast only lined up when tomorrow was
  a Wednesday.
- Stocks `<change>` is a currency amount, not a repeated percentage string.
- Stocks XML closes the tag it opens. The original opened `<result>` and closed
  `</r>`, which NSXMLParser rejects.
- No generated-WOEID JSON file that grows forever.
