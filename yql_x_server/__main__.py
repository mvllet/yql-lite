"""Command line entry point."""

import argparse
from pathlib import Path

from .config import settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="yql-x-server")
    parser.add_argument("--host", default=None, help="bind address (default 0.0.0.0)")
    parser.add_argument("--port", type=int, default=None, help="bind port (default 8000)")
    parser.add_argument("--db-path", type=Path, default=None, help="place table location")
    parser.add_argument("--advert-link", default=None, help="URL behind the in-app links")
    parser.add_argument("--quiet", action="store_true", help="stop logging each query")
    args = parser.parse_args()

    settings.update(
        host=args.host,
        port=args.port,
        db_path=args.db_path,
        advert_link=args.advert_link,
        log_queries=False if args.quiet else None,
    )

    import uvicorn

    from .server import app

    uvicorn.run(app, host=settings.host, port=settings.port, proxy_headers=True, forwarded_allow_ips="*")


if __name__ == "__main__":
    main()
