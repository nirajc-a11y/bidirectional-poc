import json
import logging
import re
from datetime import datetime, timezone

_PHONE_RE = re.compile(r"(\+\d{1,3})\d+(\d{4})")
_PHONE_MASK = r"\1****\2"


def redact_pii(text: str) -> str:
    """Redact phone numbers from log text. E.g. +12345678901 -> +1****8901"""
    return _PHONE_RE.sub(_PHONE_MASK, text)


class _JsonFormatter(logging.Formatter):
    def __init__(self, call_id: str = "", claim_number: str = ""):
        super().__init__()
        self._call_id = call_id
        self._claim_number = claim_number

    def format(self, record: logging.LogRecord) -> str:
        msg = redact_pii(record.getMessage())
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": msg,
        }
        if self._call_id:
            entry["call_id"] = self._call_id
        if self._claim_number:
            entry["claim_number"] = self._claim_number
        if record.exc_info:
            entry["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(entry)


class _TextFormatter(logging.Formatter):
    def __init__(self, call_id: str = ""):
        fmt = "%(asctime)s [%(name)s] %(levelname)s: %(message)s"
        if call_id:
            fmt = f"%(asctime)s [{call_id}] [%(name)s] %(levelname)s: %(message)s"
        super().__init__(fmt, datefmt="%Y-%m-%d %H:%M:%S")

    def format(self, record: logging.LogRecord) -> str:
        record.msg = redact_pii(str(record.msg))
        return super().format(record)


def configure_logger(name: str, log_format: str = "text", call_id: str = "", claim_number: str = "") -> logging.Logger:
    """Return a configured logger. Call once per module/call."""
    log = logging.getLogger(name)
    log.setLevel(logging.INFO)
    log.propagate = False
    if log.handlers:
        log.handlers.clear()
    handler = logging.StreamHandler()
    if log_format == "json":
        handler.setFormatter(_JsonFormatter(call_id=call_id, claim_number=claim_number))
    else:
        handler.setFormatter(_TextFormatter(call_id=call_id))
    log.addHandler(handler)
    return log


def get_audit_logger() -> logging.Logger:
    """Returns a dedicated audit logger writing to audit.log."""
    audit = logging.getLogger("audit")
    if audit.handlers:
        return audit
    audit.setLevel(logging.INFO)
    audit.propagate = False
    fh = logging.FileHandler("audit.log", encoding="utf-8")
    fh.setFormatter(logging.Formatter("%(asctime)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%SZ"))
    audit.addHandler(fh)
    return audit
