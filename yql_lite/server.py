import re
import time
from contextlib import asynccontextmanager
from pathlib import Path
from xml.etree import ElementTree
from xml.sax.saxutils import escape

import anyio
import httpx
from fastapi import APIRouter, FastAPI, Request, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import geo, query, stocks, weather
from .config import settings

TEMPLATES = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "templates"),
    autoescape=select_autoescape(default=False),
    trim_blocks=True,
    lstrip_blocks=True,
)

LOCATION_ID = "ASXX0075"
_BETWEEN_TAGS = re.compile(r">\s+<")


class XMLResponse(Response):
    media_type = "application/xml"
    charset = "utf-8"


def render(name: str, **context) -> XMLResponse:
    context.setdefault("advert_link", settings.advert_link)
    context.setdefault("created", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    xml = TEMPLATES.get_template(name).render(**context)
    return XMLResponse(_BETWEEN_TAGS.sub("><", xml).strip())


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.http = httpx.AsyncClient(
        timeout=settings.http_timeout,
        headers={"User-Agent": "yql-lite/2.0"},
        follow_redirects=True,
    )
    yield
    await app.state.http.aclose()


app = FastAPI(title="yql-lite", lifespan=lifespan)
router = APIRouter(default_response_class=XMLResponse)


async def build_location(client: httpx.AsyncClient, place, lang: str) -> dict | None:
    try:
        conditions = await weather.fetch(client, place.latitude, place.longitude)
    except (httpx.HTTPError, KeyError, ValueError) as error:
        print(f"weather failed for {place.name} ({place.woeid}): {error}")
        return None

    return {
        **conditions,
        "woeid": place.woeid,
        "city": escape(place.name),
        "state": escape(place.state),
        "country_alpha3": geo.alpha3(place.country_code),
        "country_name": escape(geo.country_name(place.country_code)),
        "latitude": round(place.latitude, 4),
        "longitude": round(place.longitude, 4),
        "location_id": LOCATION_ID,
    }


async def resolve_places(client: httpx.AsyncClient, parsed: query.Query) -> list:
    if parsed.kind == "latlon":
        return [await geo.reverse(client, parsed.lat, parsed.lon, parsed.lang)]
    places = []
    for woeid in parsed.woeids:
        place = await geo.resolve(client, woeid, parsed.lang)
        if place:
            places.append(place)
    return places


@router.get("/yql/weather")
@router.get("/v1/yql")
async def weather_yql(request: Request):
    parsed = query.parse(request.query_params.get("q", ""))
    if parsed is None:
        return Response("Invalid Request", status_code=400)
    if settings.log_queries:
        print(f"{request.client.host if request.client else '?'} -> {parsed}")

    client: httpx.AsyncClient = request.app.state.http

    if parsed.kind == "search":
        try:
            places = await geo.search(client, parsed.term, parsed.lang)
        except httpx.HTTPError as error:
            print(f"search failed for {parsed.term}: {error}")
            places = []
        return render(
            "weather_search.jinja2",
            results=[
                {
                    "city": escape(place.name),
                    "country_name": escape(geo.country_name(place.country_code)),
                    "country_alpha3": geo.alpha3(place.country_code),
                    "location_id": LOCATION_ID,
                    "woeid": place.woeid,
                }
                for place in places
            ],
        )

    places = await resolve_places(client, parsed)
    results = []
    for place in places:
        location = await build_location(client, place, parsed.lang)
        if location:
            results.append(location)

    if not results:
        return Response("No results", status_code=404)
    return render("weather.jinja2", results=results, count=len(results) * 2)


@router.get("/dgw")
async def dgw_probe():
    return Response("ok")


async def dgw_weather(client: httpx.AsyncClient, root: ElementTree.Element, search: bool):
    query_node = root[0]
    lang = query_node[1].text if len(query_node) > 1 else "en-US"

    if search:
        term = query_node[0][0].text if len(query_node) and len(query_node[0]) else ""
        try:
            places = await geo.search(client, term, lang)
        except httpx.HTTPError as error:
            print(f"search failed for {term}: {error}")
            places = []
        return render(
            "weather_dgw_search.jinja2",
            results=[
                {"city": escape(p.name), "country_name": escape(geo.country_name(p.country_code)), "woeid": p.woeid}
                for p in places
            ],
        )

    woeids = [node.text.split("|")[-1] for node in query_node.iter("id") if node.text]
    places = []
    for woeid in woeids:
        place = await geo.resolve(client, woeid, lang)
        if place:
            places.append(place)

    results = []
    for place in places:
        location = await build_location(client, place, lang)
        if location:
            results.append(location)

    if not results:
        return Response("No results", status_code=404)
    return render("weather_dgw.jinja2", results=results)


@router.post("/dgw")
@router.post("/yql/weather/dgw")
async def dgw(request: Request):
    body = (await request.body()).decode("utf-8", "replace")
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        return Response("Malformed XML", status_code=400)

    if len(root) == 0:
        return Response("Empty request", status_code=400)

    client: httpx.AsyncClient = request.app.state.http
    api = root.attrib.get("api")

    if api == "weather":
        req_type = root[0].attrib.get("type", "")
        if settings.log_queries:
            print(f"dgw weather {req_type}")
        if req_type == "getlocationid":
            return await dgw_weather(client, root, search=True)
        if req_type == "getforecastbylocationid":
            return await dgw_weather(client, root, search=False)
        return Response("Unsupported request type", status_code=400)

    if api == "finance":
        return await dgw_finance(root)

    return Response("Unsupported api", status_code=400)


async def dgw_finance(root: ElementTree.Element):
    req_type = root[0].attrib.get("type", "")
    symbols = [node.text.strip() for node in root.iter() if node.tag in ("symbol", "phrase") and node.text]
    range_node = next((node.text for node in root.iter() if node.tag == "range"), None)
    if settings.log_queries:
        print(f"dgw finance {req_type} symbols={symbols} range={range_node}")

    if not symbols:
        return Response("No symbols", status_code=400)

    quotes = [quote for quote in await anyio.to_thread.run_sync(_load_quotes, symbols) if quote]
    if not quotes:
        return Response("Unknown ticker", status_code=404)

    if req_type in ("getquotes", "getsymbol"):
        return render("stocks_getquotes.jinja2", type="getquotes", timestamp=int(time.time()), quotes=quotes)

    if req_type == "getnews":
        return render("stocks_getnews.jinja2", posts=quotes[0].news)

    if req_type == "getchart":
        points = await anyio.to_thread.run_sync(stocks.get_chart, symbols[0], range_node or "1d")
        if not points:
            return Response("Invalid ticker or range", status_code=404)
        return render(
            "stocks_getchart.jinja2",
            type="getchart",
            timestamp=int(time.time()),
            symbol=quotes[0].name,
            market_open=quotes[0].open,
            market_close=quotes[0].previous_close,
            gmt_offset=quotes[0].gmt_offset,
            points=points,
        )

    return Response("Unsupported request type", status_code=400)


def _load_quotes(symbols: list[str]):
    return [stocks.get_quote(symbol) for symbol in symbols]


@app.get("/health")
async def health():
    return Response("OK", media_type="text/plain")


@app.get("/")
async def index():
    return {
        "name": "yql-lite",
        "weather_yql": "GET /yql/weather?q=<yql>",
        "weather_dgw": "POST /dgw (api=\"weather\")",
        "stocks": "POST /dgw (api=\"finance\")",
    }


app.include_router(router)
