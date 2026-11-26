"""Tiny stdlib JSON-over-HTTP helper (no third-party dependencies)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from epochal_pilot import __version__


class HTTPError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def post_json(url: str, body: dict, headers: dict | None = None, timeout: float = 60) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", f"epochal-pilot/{__version__}")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            err = json.loads(e.read().decode("utf-8"))
            if isinstance(err, dict):
                inner = err.get("error")
                detail = inner.get("type") or inner.get("message", "") if isinstance(inner, dict) else str(inner)
        except Exception:
            pass
        # Never include request headers (they hold API keys) in the message.
        raise HTTPError(e.code, f"HTTP {e.code} from {url.split('?')[0]} {detail}".strip()) from None
    except urllib.error.URLError as e:
        raise HTTPError(0, f"cannot reach {url.split('?')[0]}: {e.reason}") from None
