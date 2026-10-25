"""HTTP plumbing shared by the store clients: pacing, retries and keeping secrets out of error messages."""
import json
import threading
import time
import urllib.error
import urllib.request

from . import secrets

UA = "packrat/0.1 (+https://github.com/Adamshrugged/packrat-assets)"
RETRY_CODES = (429, 500, 502, 503, 504)


def retry_after(e: urllib.error.HTTPError) -> float | None:
    """Seconds the server asked us to wait (Retry-After), capped at two minutes."""
    try:
        return min(120.0, max(0.0, float(e.headers.get("Retry-After", ""))))
    except (TypeError, ValueError):
        return None


class Client:
    """Base for a store's API client. Subclasses set `error` and `gap`, and override `headers`, and
    optionally `check` (inspect a successful response) and `http_error` (map a status to a clearer error)."""
    error: type[Exception] = Exception
    gap = 0.25  # seconds between requests

    def __init__(self, secret_values: dict):
        self._secrets = secret_values
        self._lock = threading.Lock()
        self._last = 0.0

    def headers(self, auth: bool) -> dict:
        return {"User-Agent": UA}

    def check(self, response) -> None:
        pass

    def http_error(self, e: urllib.error.HTTPError) -> Exception | None:
        return None

    def throttle(self) -> None:
        with self._lock:
            wait = self._last + self.gap - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()

    def get(self, url: str, auth: bool = True, raw: bool = False):
        headers = self.headers(auth)
        for attempt in range(4):
            self.throttle()
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as r:
                    body = r.read()
                    self.check(r)
                return body if raw else json.loads(body)
            except urllib.error.HTTPError as e:
                custom = self.http_error(e)
                if custom is not None:
                    raise custom from None
                if e.code in RETRY_CODES and attempt < 3:
                    time.sleep(retry_after(e) or 2 ** attempt * 3)
                    continue
                raise self.error(secrets.redact(f"HTTP {e.code} from {url.split('?')[0]}", self._secrets)) from None
            except (urllib.error.URLError, TimeoutError) as e:
                if attempt < 3:
                    time.sleep(2 ** attempt * 3)
                    continue
                raise self.error(secrets.redact(f"network error: {e}", self._secrets)) from None
