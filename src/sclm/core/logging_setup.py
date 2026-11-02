"""Logging: rotating file log in the XDG state dir plus optional stderr output.

Loggers: use ``logging.getLogger(__name__)`` everywhere (all under ``sclm.*``).
yt-dlp output should be routed to ``logging.getLogger("sclm.ytdlp")`` via yt-dlp's
``logger`` option (download/ owns that adapter).
"""

from __future__ import annotations

import logging
import sys
import threading
from logging.handlers import RotatingFileHandler
from pathlib import Path
from types import TracebackType

LOG_FORMAT = "%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s"
LOG_MAX_BYTES = 1_000_000
LOG_BACKUP_COUNT = 3
_MARKER = "_sclm_handler"

log = logging.getLogger("sclm")


def setup_logging(log_dir: Path, *, level: int = logging.INFO, console: bool = True) -> Path:
    """Configure the ``sclm`` logger. Idempotent: replaces handlers from earlier calls.

    Returns the log file path (``<log_dir>/sclm.log``, rotated at 1 MB, 3 backups).
    """
    log_dir = Path(log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "sclm.log"

    for handler in list(log.handlers):
        if getattr(handler, _MARKER, False):
            log.removeHandler(handler)
            handler.close()

    fmt = logging.Formatter(LOG_FORMAT)
    file_handler = RotatingFileHandler(log_file, maxBytes=LOG_MAX_BYTES,
                                       backupCount=LOG_BACKUP_COUNT, encoding="utf-8")
    file_handler.setFormatter(fmt)
    setattr(file_handler, _MARKER, True)
    log.addHandler(file_handler)

    if console:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(fmt)
        setattr(stream, _MARKER, True)
        log.addHandler(stream)

    log.setLevel(level)
    log.propagate = False
    return log_file


def install_excepthook() -> None:
    """Log uncaught exceptions (main thread, Qt slots and worker threads) instead of
    letting them vanish. Call once at startup."""

    def hook(exc_type: type[BaseException], exc: BaseException,
             tb: TracebackType | None) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        log.critical("Uncaught exception", exc_info=(exc_type, exc, tb))

    def thread_hook(args: threading.ExceptHookArgs) -> None:
        if args.exc_type is SystemExit or args.exc_value is None:
            return
        log.critical("Uncaught exception in thread %s",
                     args.thread.name if args.thread else "?",
                     exc_info=(args.exc_type, args.exc_value, args.exc_traceback))

    sys.excepthook = hook
    threading.excepthook = thread_hook
