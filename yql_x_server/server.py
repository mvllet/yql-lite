"""HTTP surface.

Weather uses the YQL protocol (iOS 5 to 7): a GET with a pseudo-SQL q=
parameter. Stocks uses the DGW protocol: a POST carrying an XML request body.
Both answer with XML shaped like Yahoo's old responses.
"""

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
        headers={"User-Agent": "YQL-X-Server/2.0"},
        follow_redirects=True,
    )
    yield
    await app.state.http.aclose()


app = FastAPI(title="YQL-X-Server", lifespan=lifespan)
router = APIRouter(default_response_class=XMLResponse)


async def build_location(client: httpx.AsyncClient, place, lang: str) -> dict | None:
    """One place plus its current weather, flattened for the template."""
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

    if parsed.kind == "latlon":
        places = [await geo.reverse(client, parsed.lat, parsed.lon, parsed.lang)]
    else:
        places = []
        for woeid in parsed.woeids:
            place = await geo.resolve(client, woeid, parsed.lang)
            if place:
                places.append(place)

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


@router.post("/dgw")
async def dgw(request: Request):
    """Stocks.app request handler."""
    body = (await request.body()).decode("utf-8", "replace")
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        return Response("Malformed XML", status_code=400)

    if root.attrib.get("api") != "finance" or len(root) == 0:
        return Response("Unsupported api", status_code=400)

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


@app.get("/")
async def index():
    return {
        "name": "YQL-X-Server",
        "weather": "GET /yql/weather?q=<yql>",
        "stocks": "POST /dgw",
    }


app.include_router(router)
