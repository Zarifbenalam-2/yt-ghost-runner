"""logging_setup.py — one log format, levels, rotation, correlation ids.

Replaces the print()/ad-hoc-file situation. Call setup() once at process
start; then `from hub.logging_setup import get_logger; log = get_logger(__name__)`.

Every record carries a short correlation id (`run_id`) so a watch run, its
factory cycle and its API calls can be grepped as one story.
"""
import logging
import logging.handlers
import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LOG_DIR = Path(os.environ.get("GHOST_LOG_DIR") or (ROOT / "data" / "logs"))

_configured = False
_run_id = None

FMT = "%(asctime)s %(levelname)-7s [%(run_id)s] %(name)s: %(message)s"


class _RunIdFilter(logging.Filter):
    def filter(self, record):
        if not hasattr(record, "run_id"):
            record.run_id = current_run_id()
        return True


def current_run_id():
    """The correlation id for this process (stable for the whole process)."""
    global _run_id
    if _run_id is None:
        _run_id = os.environ.get("GHOST_RUN_ID") or uuid.uuid4().hex[:8]
    return _run_id


def setup(level=None, log_name="ghost", to_stdout=True):
    """Configure root logging once. Safe to call repeatedly."""
    global _configured
    if _configured:
        return
    _configured = True

    level = level or os.environ.get("GHOST_LOG_LEVEL", "INFO")
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    fmt = logging.Formatter(FMT, datefmt="%Y-%m-%dT%H:%M:%S")
    run_filter = _RunIdFilter()

    fh = logging.handlers.RotatingFileHandler(
        LOG_DIR / f"{log_name}.log", maxBytes=10 * 1024 * 1024, backupCount=5,
        encoding="utf-8")
    fh.setFormatter(fmt)
    fh.addFilter(run_filter)
    root.addHandler(fh)

    if to_stdout:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        sh.addFilter(run_filter)
        root.addHandler(sh)

    # quiet the noisy stdlib loggers
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)


def get_logger(name):
    """Get a logger; auto-configures logging on first use."""
    if not _configured:
        setup()
    return logging.getLogger(name)