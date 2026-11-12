"""Logging with redaction of secrets, image data and signed URL query strings."""
from __future__ import annotations

import logging
import re
from logging.handlers import RotatingFileHandler
from pathlib import Path

_PATTERNS = [
    (re.compile(r"msy_[A-Za-z0-9_\-]+"), "msy_***"),
    (re.compile(r"(Bearer\s+)\S+", re.IGNORECASE), r"\1***"),
    (re.compile(r"data:image/[a-z]+;base64,[A-Za-z0-9+/=]+"), "data:image/...;base64,<redacted>"),
    (re.compile(r"(https?://[^\s?\"']+)\?[^\s\"']+"), r"\1?<signed>"),
]
_extra_secrets: set[str] = set()


def register_secret(value: str | None) -> None:
    """Make sure this exact value never appears in logs (e.g. the user's API key)."""
    if value and len(value) >= 6:
        _extra_secrets.add(value)


def redact(text: str) -> str:
    for secret in _extra_secrets:
        text = text.replace(secret, "***")
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001
            return True
        record.msg = redact(msg)
        record.args = ()
        if record.exc_info and record.exc_info[1] is not None:
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def setup_logging(log_dir: Path, level: int = logging.INFO) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "meshy_studio.log"
    root = logging.getLogger()
    root.setLevel(level)
    for h in list(root.handlers):
        root.removeHandler(h)
    fh = RotatingFileHandler(log_file, maxBytes=2_000_000, backupCount=5, encoding="utf-8")
    fh.setFormatter(logging.Formatter(FORMAT))
    fh.addFilter(RedactingFilter())
    root.addHandler(fh)
    sh = logging.StreamHandler()
    sh.setFormatter(logging.Formatter(FORMAT))
    sh.addFilter(RedactingFilter())
    root.addHandler(sh)
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return log_file
