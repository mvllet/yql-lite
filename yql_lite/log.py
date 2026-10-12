import logging
import time
from pathlib import Path

FORMAT = "%(asctime)s %(levelname).1s %(message)s"
DATEFMT = "%Y-%m-%dT%H:%M:%SZ"

_configured_path: Path | None = None


def _file_handler(path: Path) -> logging.FileHandler:
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(path, encoding="utf-8")
    formatter = logging.Formatter(FORMAT, datefmt=DATEFMT)
    formatter.converter = time.gmtime
    handler.setFormatter(formatter)
    return handler


def setup_logging(path: Path) -> logging.Logger:
    global _configured_path
    logger = logging.getLogger("yql-lite")
    logger.setLevel(logging.INFO)
    resolved = Path(path).expanduser()
    if _configured_path == resolved and logger.handlers:
        return logger
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    logger.addHandler(_file_handler(resolved))
    logger.propagate = False
    _configured_path = resolved
    return logger


def get_logger() -> logging.Logger:
    logger = logging.getLogger("yql-lite")
    if not logger.handlers:
        from .config import settings

        return setup_logging(settings.log_file)
    return logger
