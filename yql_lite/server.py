import logging
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
from .log import get_logger, setup_logging

setup_logging(settings.log_file)
logging.getLogger("uvicorn.access").disabled = True
logging.getLogger("uvicorn.error").setLevel(logging.CRITICAL)
logger = get_logger()


def _client(request: Request) -> str:
    return request.client.host if request.client else "?"


def _ms(start: float) -> int:
    return int((time.perf_counter() - start) * 1000)


def _short(error: Exception, limit: int = 120) -> str:
    text = str(error).replace("\n", " ").strip()
    return text[:limit] + ("..." if len(text) > limit else "") or type(error).__name__


def _query_summary(parsed: query.Query) -> str:
    if parsed.kind == "search":
        return f'search="{parsed.term}" lang={parsed.lang}'
    if parsed.kind == "latlon":
        return f"lat={parsed.lat} lon={parsed.lon} lang={parsed.lang}"
    return f"woeid={','.join(parsed.woeids)} lang={parsed.lang}"

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
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
    )
    print(f"Server running on port {settings.port}", flush=True)
    try:
        yield
    finally:
        await app.state.http.aclose()


app = FastAPI(title="yql-lite", lifespan=lifespan)
router = APIRouter(default_response_class=XMLResponse)


async def build_location(client: httpx.AsyncClient, place, lang: str) -> dict | None:
    try:
        conditions = await weather.fetch(client, place.latitude, place.longitude)
    except (httpx.HTTPError, KeyError, ValueError) as error:
        logger.warning(f"weather fetch failed {place.name} woeid={place.woeid}: {_short(error)}")
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


async def _resolve_one(client: httpx.AsyncClient, woeid: str, lang: str, out: list, index: int) -> None:
    try:
        out[index] = await geo.resolve(client, woeid, lang)
    except httpx.HTTPError as error:
        logger.warning(f"resolve failed woeid={woeid}: {_short(error)}")
        out[index] = None


async def resolve_places(client: httpx.AsyncClient, parsed: query.Query) -> list:
    if parsed.kind == "latlon":
        return [await geo.reverse(client, parsed.lat, parsed.lon, parsed.lang)]
    woeids = list(parsed.woeids[: settings.max_woeids])
    if not woeids:
        return []
    out: list = [None] * len(woeids)
    async with anyio.create_task_group() as group:
        for index, woeid in enumerate(woeids):
            group.start_soon(_resolve_one, client, woeid, parsed.lang, out, index)
    return [place for place in out if place]


async def _build_one(client: httpx.AsyncClient, place, lang: str, out: list, index: int) -> None:
    out[index] = await build_location(client, place, lang)


async def build_locations(client: httpx.AsyncClient, places: list, lang: str) -> list:
    if not places:
        return []
    out: list = [None] * len(places)
    async with anyio.create_task_group() as group:
        for index, place in enumerate(places):
            group.start_soon(_build_one, client, place, lang, out, index)
    return [location for location in out if location]


@router.get("/yql/weather")
@router.get("/v1/yql")
async def weather_yql(request: Request):
    start = time.perf_counter()
    parsed = query.parse(request.query_params.get("q", ""))
    if parsed is None:
        if settings.log_queries:
            logger.info(f"GET {request.url.path} invalid -> 400 {_ms(start)}ms {_client(request)}")
        return Response("Invalid Request", status_code=400)

    client: httpx.AsyncClient = request.app.state.http
    summary = _query_summary(parsed)

    if parsed.kind == "search":
        try:
            places = await geo.search(client, parsed.term, parsed.lang)
        except httpx.HTTPError as error:
            logger.warning(f"geo search failed term={parsed.term!r}: {_short(error)}")
            places = []
        if settings.log_queries:
            logger.info(f"GET {request.url.path} {summary} -> 200 {len(places)}results {_ms(start)}ms {_client(request)}")
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
    results = await build_locations(client, places, parsed.lang)

    if not results:
        if settings.log_queries:
            logger.info(f"GET {request.url.path} {summary} -> 404 0results {_ms(start)}ms {_client(request)}")
        return Response("No results", status_code=404)
    if settings.log_queries:
        logger.info(f"GET {request.url.path} {summary} -> 200 {len(results)}results {_ms(start)}ms {_client(request)}")
    return render("weather.jinja2", results=results, count=len(results) * 2)


@router.get("/dgw")
async def dgw_probe():
    return Response("ok")


async def dgw_weather(client: httpx.AsyncClient, root: ElementTree.Element, search: bool, *, request: Request, start: float):
    query_node = root[0]
    lang = query_node[1].text if len(query_node) > 1 else "en-US"
    req_type = root[0].attrib.get("type", "")

    if search:
        term = query_node[0][0].text if len(query_node) and len(query_node[0]) else ""
        try:
            places = await geo.search(client, term, lang)
        except httpx.HTTPError as error:
            logger.warning(f"geo search failed term={term!r}: {_short(error)}")
            places = []
        if settings.log_queries:
            logger.info(f"POST {request.url.path} weather={req_type} term={term!r} -> 200 {len(places)}results {_ms(start)}ms {_client(request)}")
        return render(
            "weather_dgw_search.jinja2",
            results=[
                {"city": escape(p.name), "country_name": escape(geo.country_name(p.country_code)), "woeid": p.woeid}
                for p in places
            ],
        )

    woeids = [node.text.split("|")[-1] for node in query_node.iter("id") if node.text]
    woeids = woeids[: settings.max_woeids]
    out: list = [None] * len(woeids)
    async with anyio.create_task_group() as group:
        for index, woeid in enumerate(woeids):
            group.start_soon(_resolve_one, client, woeid, lang, out, index)
    places = [place for place in out if place]

    results = await build_locations(client, places, lang)

    if settings.log_queries:
        status = 404 if not results else 200
        logger.info(f"POST {request.url.path} weather={req_type} woeids={','.join(woeids)} -> {status} {len(results)}results {_ms(start)}ms {_client(request)}")
    if not results:
        return Response("No results", status_code=404)
    return render("weather_dgw.jinja2", results=results)


@router.post("/dgw")
@router.post("/yql/weather/dgw")
async def dgw(request: Request):
    start = time.perf_counter()
    body = (await request.body()).decode("utf-8", "replace")
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        if settings.log_queries:
            logger.info(f"POST {request.url.path} malformed -> 400 {_ms(start)}ms {_client(request)}")
        return Response("Malformed XML", status_code=400)

    if len(root) == 0:
        if settings.log_queries:
            logger.info(f"POST {request.url.path} empty -> 400 {_ms(start)}ms {_client(request)}")
        return Response("Empty request", status_code=400)

    client: httpx.AsyncClient = request.app.state.http
    api = root.attrib.get("api")

    if api == "weather":
        req_type = root[0].attrib.get("type", "")
        if req_type == "getlocationid":
            return await dgw_weather(client, root, search=True, request=request, start=start)
        if req_type == "getforecastbylocationid":
            return await dgw_weather(client, root, search=False, request=request, start=start)
        if settings.log_queries:
            logger.info(f"POST {request.url.path} weather={req_type} unsupported -> 400 {_ms(start)}ms {_client(request)}")
        return Response("Unsupported request type", status_code=400)

    if api == "finance":
        return await dgw_finance(root, request=request, start=start)

    if settings.log_queries:
        logger.info(f"POST {request.url.path} api={api} unsupported -> 400 {_ms(start)}ms {_client(request)}")
    return Response("Unsupported api", status_code=400)


def _load_one_quote(symbol: str):
    return stocks.get_quote(symbol)


async def _fetch_one_quote(symbol: str, out: list, index: int, limiter: anyio.Semaphore) -> None:
    async with limiter:
        out[index] = await anyio.to_thread.run_sync(_load_one_quote, symbol)


async def _load_quotes_parallel(symbols: list[str]) -> list:
    if not symbols:
        return []
    workers = max(1, min(settings.quote_workers, len(symbols)))
    limiter = anyio.Semaphore(workers)
    out: list = [None] * len(symbols)
    async with anyio.create_task_group() as group:
        for index, symbol in enumerate(symbols):
            group.start_soon(_fetch_one_quote, symbol, out, index, limiter)
    return [quote for quote in out if quote]


async def dgw_finance(root: ElementTree.Element, *, request: Request, start: float):
    req_type = root[0].attrib.get("type", "")
    symbols = [node.text.strip() for node in root.iter() if node.tag in ("symbol", "phrase") and node.text]
    range_node = next((node.text for node in root.iter() if node.tag == "range"), None)

    def _log(status: int, detail: str = "") -> None:
        if settings.log_queries:
            extra = f" {detail}" if detail else ""
            logger.info(f"POST {request.url.path} finance={req_type} symbols={','.join(symbols)} range={range_node}{extra} -> {status} {_ms(start)}ms {_client(request)}")

    if not symbols:
        _log(400)
        return Response("No symbols", status_code=400)

    symbols = symbols[: settings.max_symbols]
    if req_type == "getnews":
        quotes = await _load_quotes_parallel(symbols[:1])
    elif req_type == "getchart":
        quotes = await _load_quotes_parallel(symbols[:1])
    else:
        quotes = await _load_quotes_parallel(symbols)
    if not quotes:
        _log(404)
        return Response("Unknown ticker", status_code=404)

    if req_type in ("getquotes", "getsymbol"):
        _log(200, f"{len(quotes)}quotes")
        return render("stocks_getquotes.jinja2", type="getquotes", timestamp=int(time.time()), quotes=quotes)

    if req_type == "getnews":
        _log(200, f"{quotes[0].news_count}posts")
        return render("stocks_getnews.jinja2", posts=quotes[0].news)

    if req_type == "getchart":
        points = await anyio.to_thread.run_sync(stocks.get_chart, symbols[0], range_node or "1d")
        if not points:
            _log(404)
            return Response("Invalid ticker or range", status_code=404)
        _log(200, f"{len(points)}points")
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

    _log(400, "unsupported")
    return Response("Unsupported request type", status_code=400)


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
