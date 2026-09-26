import sys
import time
from datetime import datetime, timedelta, timezone
from xml.etree import ElementTree

from fastapi.testclient import TestClient

from yql_lite import query, server, stocks, weather
from yql_lite.store import decode_woeid, encode_woeid

PASS, FAIL = "  ok  ", " FAIL "
failures = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global failures
    print(f"[{PASS if condition else FAIL}] {label}{'  ' + detail if detail and not condition else ''}")
    if not condition:
        failures += 1


def sample_payload() -> dict:
    offset = 7200
    tz = timezone(timedelta(seconds=offset))
    midnight = datetime.now(tz).replace(hour=0, minute=0, second=0, microsecond=0)
    base = int(midnight.timestamp())
    now = int(datetime.now(tz).replace(minute=0, second=0, microsecond=0).timestamp())

    hours = 24 * 7
    return {
        "utc_offset_seconds": offset,
        "timezone": "Europe/Berlin",
        "current": {
            "time": now,
            "temperature_2m": 14.3,
            "apparent_temperature": 13.1,
            "relative_humidity_2m": 71,
            "dew_point_2m": 9.2,
            "pressure_msl": 1014.2,
            "wind_speed_10m": 11.4,
            "wind_direction_10m": 240,
            "weather_code": 3,
            "is_day": 1,
        },
        "hourly": {
            "time": [base + 3600 * i for i in range(hours)],
            "temperature_2m": [12.0 + (i % 10) for i in range(hours)],
            "precipitation_probability": [i % 100 for i in range(hours)],
            "weather_code": [[0, 2, 3, 61, 95][i % 5] for i in range(hours)],
            "is_day": [1 if 6 <= (i % 24) <= 20 else 0 for i in range(hours)],
            "visibility": [24000.0 for _ in range(hours)],
        },
        "daily": {
            "time": [base + 86400 * i for i in range(7)],
            "temperature_2m_max": [18.4, 19.1, 16.0, 15.5, 21.2, 22.8, 17.3],
            "temperature_2m_min": [8.1, 9.4, 7.2, 6.8, 10.1, 11.5, 9.0],
            "weather_code": [3, 61, 0, 2, 80, 95, 71],
            "sunrise": [base + 25200 + 86400 * i for i in range(7)],
            "sunset": [base + 72000 + 86400 * i for i in range(7)],
            "precipitation_probability_max": [10, 80, 0, 25, 60, 90, 40],
        },
    }


def test_woeid_codec() -> None:
    print("\nWOEID codec")
    for lat, lon in [(53.55, 9.99), (-33.87, 151.21), (0.0, 0.0), (89.99, -179.99), (-89.99, 179.99)]:
        woeid = encode_woeid(lat, lon)
        back = decode_woeid(woeid)
        check(
            f"{lat}, {lon} -> {woeid} -> {back}",
            back is not None and abs(back[0] - lat) < 0.01 and abs(back[1] - lon) < 0.01,
        )
        check(f"{woeid} fits a signed 32 bit int", 0 < woeid < 2**31)
    check("garbage id rejected", decode_woeid("not-a-number") is None)


def test_query_parser() -> None:
    print("\nQuery parser")
    cases = [
        ("select * from partner.weather.locations where query=\"hamburg\" and lang='en-US'", "search"),
        ("select * from partner.weather.forecast where lat=53.55 and lon=9.99 and lang='de-DE'", "latlon"),
        ("select * from partner.weather.forecast where woeid=2618724 and lang='en-US'", "woeid"),
        (
            "select * from yql.query.multi where queries=\"select * from partner.weather.forecast "
            "where woeid=111 and lang='en-US'; select * from partner.weather.forecast where "
            "woeid=222 and lang='en-US'\"",
            "woeid",
        ),
    ]
    for raw, expected in cases:
        parsed = query.parse(raw)
        check(f"{expected:<7} {raw[:52]}...", parsed is not None and parsed.kind == expected)
    multi = query.parse(cases[3][0])
    check("multi query keeps both woeids", multi is not None and multi.woeids == ["111", "222"])
    check("html escaped query parses", query.parse("select * from partner.weather.forecast where woeid=1 and lang=&#39;en-US&#39;") is not None)
    check("empty query rejected", query.parse("") is None)


def test_normalise() -> None:
    print("\nWeather normalisation")
    data = weather.normalise(sample_payload())
    check("six forecast days", len(data["days"]) == 6, str(len(data["days"])))
    check("twelve hourly slots", len(data["hours"]) == 12, str(len(data["hours"])))

    ordinals = [day["ordinal"] for day in data["days"]]
    today = datetime.now().weekday() + 1
    expected = [(today + offset - 1) % 7 + 1 for offset in range(1, 7)]
    check(f"day ordinals run consecutively {ordinals}", ordinals == expected, str(expected))

    check("highs above lows", all(d["high"] > d["low"] for d in data["days"]))
    check("visibility in km", 0 < data["visibility"] < 100, str(data["visibility"]))
    check("moon illumination 0-100", 0 <= data["moonfacevisible"] <= 100)
    check("moon phase marker", data["moonphase"] in (0, 1, 5))
    check("barometer looks like mb", 800 < data["barometer"] < 1100)
    check("no None leaked into fields", not any(v is None for v in data.values()))
    check("icon codes in Yahoo range", all(0 <= h["condition_code"] <= 47 for h in data["hours"]))


def test_templates() -> None:
    print("\nTemplate rendering")
    data = weather.normalise(sample_payload())
    location = {
        **data,
        "woeid": encode_woeid(53.55, 9.99),
        "city": "Hamburg",
        "state": "Hamburg",
        "country_alpha3": "DEU",
        "country_name": "Germany",
        "latitude": 53.55,
        "longitude": 9.99,
        "location_id": server.LOCATION_ID,
    }
    forecast = server.render("weather.jinja2", results=[location], count=2)
    root = ElementTree.fromstring(forecast.body)
    check("weather.jinja2 is well formed XML", root.tag == "query")
    check("forecast has six days", len(root.findall(".//day")) == 6)
    check("forecast has twelve hours", len(root.findall(".//hour")) == 12)

    search = server.render(
        "weather_search.jinja2",
        results=[
            {
                "city": "Hamburg",
                "country_name": "Germany",
                "country_alpha3": "DEU",
                "location_id": server.LOCATION_ID,
                "woeid": 1,
            }
        ],
    )
    check("weather_search.jinja2 is well formed XML", ElementTree.fromstring(search.body).tag == "query")

    class FakeQuote:
        name = "AAPL"
        name_short = "Apple Inc."
        open = 189.1
        price = 191.4
        previous_close = 188.0
        change = "+3.40"
        real_time_change = "+3.40"
        change_percent = "+1.81%"
        market_cap = 2900000000000
        high = 192.0
        low = 188.5
        volume = 41000000
        average_volume = 52000000
        peratio = 29.4
        dividend_yield = 0.005
        yearrange = "164.08 - 199.62"
        gmt_offset = -4
        news = [{"title": "Apple ships something", "link": "https://example.com/a", "published": 1700000000}]

    quotes = server.render("stocks_getquotes.jinja2", type="getquotes", timestamp=int(time.time()), quotes=[FakeQuote()])
    check("stocks_getquotes.jinja2 is well formed XML", ElementTree.fromstring(quotes.body).tag == "response")

    chart = server.render(
        "stocks_getchart.jinja2",
        type="getchart",
        timestamp=int(time.time()),
        symbol="AAPL",
        market_open=189.1,
        market_close=188.0,
        gmt_offset=-4,
        points=[{"timestamp": 1700000000, "close": 191.4, "volume": 100}],
    )
    check("stocks_getchart.jinja2 is well formed XML", ElementTree.fromstring(chart.body).tag == "response")

    news = server.render("stocks_getnews.jinja2", posts=FakeQuote.news)
    check("stocks_getnews.jinja2 is well formed XML", ElementTree.fromstring(news.body).tag == "feed")

    dgw = server.render("weather_dgw.jinja2", results=[location])
    root = ElementTree.fromstring(dgw.body)
    check("weather_dgw.jinja2 is well formed XML", root.tag == "response")
    check("dgw forecast has six days", len(root.findall(".//forecast")) == 6)

    dgw_search = server.render(
        "weather_dgw_search.jinja2",
        results=[{"woeid": 1, "city": "Hamburg", "country_name": "Germany"}],
    )
    check(
        "weather_dgw_search.jinja2 is well formed XML",
        ElementTree.fromstring(dgw_search.body).tag == "response",
    )


def test_routes() -> None:
    print("\nRouting")
    with TestClient(server.app) as client:
        check("GET / answers", client.get("/").status_code == 200)
        check("GET /dgw probe answers", client.get("/dgw").status_code == 200)
        check("bad weather query is a 400", client.get("/yql/weather?q=nonsense").status_code == 400)
        check(
            "malformed stocks XML is a 400",
            client.post("/dgw", content="<broken").status_code == 400,
        )
        check(
            "empty DGW request is a 400",
            client.post("/dgw", content='<request api="weather"></request>').status_code == 400,
        )
        check(
            "unknown api is a 400",
            client.post("/dgw", content='<request api="carpool"><query type="x"/></request>').status_code == 400,
        )

        dgw_search_body = (
            '<request api="weather"><query type="getlocationid">'
            "<list><term>Hamburg</term></list><lang>en</lang>"
            "</query></request>"
        )
        response = client.post("/dgw", content=dgw_search_body)
        check(
            f"DGW weather search reaches the handler (got {response.status_code})",
            response.status_code in (200, 404),
        )

        dgw_forecast_body = (
            '<request api="weather"><query type="getforecastbylocationid">'
            "<list><item><id>516813354</id></item></list><lang>en</lang>"
            "</query></request>"
        )
        response = client.post("/dgw", content=dgw_forecast_body)
        check(
            f"DGW weather forecast reaches the handler (got {response.status_code})",
            response.status_code in (200, 404),
        )

        check(
            "DGW weather forecast with no ids is a 404",
            client.post(
                "/dgw",
                content='<request api="weather"><query type="getforecastbylocationid"><list></list><lang>en</lang></query></request>',
            ).status_code
            == 404,
        )

        encoded_body = (
            '<?xml version="1.0"?><request api="finance"><query type="getquotes">'
            "<list><symbol>%5EGDAXI</symbol></list>"
            "</query></request>"
        )
        response = client.post("/dgw", content=encoded_body)
        check(
            f"percent-encoded index ticker reaches the handler (got {response.status_code})",
            response.status_code in (200, 404),
        )


def test_live() -> None:
    print("\nLive upstream services")
    with TestClient(server.app) as client:
        response = client.get(
            "/yql/weather?q=" + "select * from partner.weather.locations where query=\"hamburg\" and lang='en-US'"
        )
        check(f"city search returns 200 (got {response.status_code})", response.status_code == 200)
        woeid = None
        if response.status_code == 200:
            root = ElementTree.fromstring(response.content)
            locations = root.findall(".//location")
            check(f"city search found {len(locations)} places", len(locations) > 0)
            if locations:
                woeid = locations[0].get("woeid")
                print(f"         first hit: {locations[0].get('city')} woeid={woeid}")

        if woeid:
            response = client.get(
                f"/yql/weather?q=select * from partner.weather.forecast where woeid={woeid} and lang='en-US'"
            )
            check(f"forecast by woeid returns 200 (got {response.status_code})", response.status_code == 200)
            if response.status_code == 200:
                root = ElementTree.fromstring(response.content)
                check("forecast has six days", len(root.findall(".//day")) == 6)
                current = root.find(".//currently")
                if current is not None:
                    print(f"         now: {current.get('temp')}C, feels {current.get('feelsLike')}C")

        response = client.get(
            "/yql/weather?q=select * from partner.weather.forecast where lat=53.55 and lon=9.99 and lang='en-US'"
        )
        check(f"forecast by lat/lon returns 200 (got {response.status_code})", response.status_code == 200)

        body = '<?xml version="1.0"?><request api="finance"><query type="getquotes"><list><symbol>AAPL</symbol></list></query></request>'
        response = client.post("/dgw", content=body)
        check(f"stock quote returns 200 (got {response.status_code})", response.status_code == 200)
        if response.status_code == 200:
            root = ElementTree.fromstring(response.content)
            quote = root.find(".//quote")
            if quote is not None:
                print(f"         AAPL {quote.findtext('price')} {quote.findtext('changepercent')}")
            else:
                print("         no <quote> in the response, check the server log above for why")

        print("\nLive chart density")
        for range_key in stocks.RANGES:
            chart_body = (
                '<?xml version="1.0"?><request api="finance"><query type="getchart">'
                f"<list><symbol>AAPL</symbol><range>{range_key}</range></list>"
                "</query></request>"
            )
            response = client.post("/dgw", content=chart_body)
            if response.status_code != 200:
                check(f"{range_key:>3} chart returns 200 (got {response.status_code})", False)
                continue
            root = ElementTree.fromstring(response.content)
            points = root.findall(".//point")
            check(f"{range_key:>3} chart has more than 5 points ({len(points)} points)", len(points) > 5)

        print("\nLive DGW weather (iOS 2-4)")
        dgw_search_body = (
            '<request api="weather"><query type="getlocationid">'
            "<list><term>Hamburg</term></list><lang>en</lang></query></request>"
        )
        response = client.post("/dgw", content=dgw_search_body)
        check(f"dgw city search returns 200 (got {response.status_code})", response.status_code == 200)
        dgw_woeid = None
        if response.status_code == 200:
            root = ElementTree.fromstring(response.content)
            items = root.findall(".//item")
            check(f"dgw city search found {len(items)} places", len(items) > 0)
            if items:
                dgw_woeid = items[0].findtext("id")
                print(f"         first hit: {items[0].findtext('city')} id={dgw_woeid}")

        if dgw_woeid:
            dgw_forecast_body = (
                '<request api="weather"><query type="getforecastbylocationid">'
                f"<list><item><id>{dgw_woeid}</id></item></list><lang>en</lang>"
                "</query></request>"
            )
            response = client.post("/dgw", content=dgw_forecast_body)
            check(f"dgw forecast returns 200 (got {response.status_code})", response.status_code == 200)
            if response.status_code == 200:
                root = ElementTree.fromstring(response.content)
                check("dgw forecast has six days", len(root.findall(".//forecast")) == 6)


if __name__ == "__main__":
    test_woeid_codec()
    test_query_parser()
    test_normalise()
    test_templates()
    test_routes()
    if "--offline" in sys.argv:
        print("\nLive upstream services: skipped")
    else:
        test_live()
    print(f"\n{'all checks passed' if not failures else str(failures) + ' check(s) failed'}")
    sys.exit(1 if failures else 0)
