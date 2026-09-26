# yql-x-server

Fork of YQL-X-Server. Changes from the original:

- Replaced apis.yzu.moe, OpenWeatherMap, Nominatim and GeoNames with Open-Meteo (weather + geocoding) and BigDataCloud (reverse geocoding). No API keys required.
- Replaced the ord()-based generated WOEID scheme and its growing JSON file with a WOEID encoded from rounded lat/lon, decodable with no lookup. Place names are cached in places.db.
- Fixed forecast day ordering: days are now indexed by position instead of by weekday number, so the 6-day forecast lines up correctly regardless of what day it is.
- Fixed the stocks chart interval per range so each range (1d, 5d, 1m, 3m, 6m, 1y, 2y, 5y, 10y) has real point density instead of falling back to coarse or empty intervals.
- Fixed stocks change/changepercent to match the original's percent-based fields.
- Removed Redis, Sentry, Prometheus, geopy, ephem, timezonefinder, starlette-context, and the multi-provider abstraction layers.
- Reduced dependency count from ~61 to ~41 resolved packages.
