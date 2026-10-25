"""Sign-in for network mode. Packrat is open to browsers on its own computer; when it listens on the network
([server] host), everyone else signs in with a password (or, for the MCP server, an API token).

Before a password exists, a one-time setup code is printed in Packrat's log, so only someone who can see the
log (the person who started it, or `docker logs`) can choose the password. `packrat set-password` works too."""
import hashlib
import hmac
import json
import logging
import secrets as rand
import time
from http.cookies import SimpleCookie

from . import secrets
from .guard import is_loopback

log = logging.getLogger("packrat.auth")

COOKIE = "packrat_session"
SESSION_DAYS = 30
MIN_PASSWORD = 8
MAX_FAILURES = 5  # per address, per FAILURE_WINDOW
FAILURE_WINDOW = 300
# Reachable without signing in: the sign-in page itself and what it needs.
OPEN_PATHS = {"/login.html", "/style.css", "/api/auth/status", "/api/auth/login", "/api/auth/setup"}


def hash_password(password: str) -> str:
    salt = rand.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt:16384:8:1:{salt.hex()}:{digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _, n, r, p, salt, digest = stored.split(":")
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p), dklen=32)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got.hex(), digest)


def _save(**values) -> None:
    from .settings import save_secrets
    save_secrets({**secrets.load(), **values})


class Auth:
    def __init__(self, required: bool):
        self.required = required  # Packrat listens on the network
        self.setup_code = None
        self._failures: dict[str, list[float]] = {}
        if required and not self.password_set():
            self.setup_code = f"{rand.randbelow(10**8):08d}"
            msg = (f"No password yet. Open Packrat from another computer and enter this setup code to choose one: "
                   f"{self.setup_code[:4]}-{self.setup_code[4:]}  (or run: packrat set-password)")
            log.warning(msg)

    # ------------------------------------------------------------ password

    def password_set(self) -> bool:
        return bool(secrets.load().get("password_hash"))

    def check_password(self, password: str) -> bool:
        stored = secrets.load().get("password_hash")
        return bool(stored) and verify_password(password, stored)

    def set_password(self, password: str) -> None:
        if len(password) < MIN_PASSWORD:
            raise ValueError(f"use at least {MIN_PASSWORD} characters")
        values = secrets.load()
        _save(password_hash=hash_password(password),
              session_secret=values.get("session_secret") or rand.token_hex(32),
              api_token=values.get("api_token") or rand.token_urlsafe(32))
        self.setup_code = None

    def check_setup_code(self, code: str) -> bool:
        return bool(self.setup_code) and hmac.compare_digest(code.replace("-", "").strip(), self.setup_code)

    # ------------------------------------------------------------ sessions and tokens

    def _sign(self, expires: int) -> str:
        values = secrets.load()
        # The password hash is part of the signature, so changing the password signs everyone out.
        msg = f"{expires}:{values.get('password_hash', '')}".encode()
        return hmac.new(values.get("session_secret", "").encode(), msg, hashlib.sha256).hexdigest()

    def new_session(self) -> str:
        expires = int(time.time()) + SESSION_DAYS * 86400
        return f"{expires}.{self._sign(expires)}"

    def valid_session(self, value: str) -> bool:
        try:
            expires, sig = value.split(".")
            return int(expires) > time.time() and hmac.compare_digest(sig, self._sign(int(expires)))
        except ValueError:
            return False

    def api_token(self) -> str:
        token = secrets.load().get("api_token")
        if not token:
            token = rand.token_urlsafe(32)
            _save(api_token=token)
        return token

    def new_api_token(self) -> str:
        token = rand.token_urlsafe(32)
        _save(api_token=token)
        return token

    def valid_token(self, value: str) -> bool:
        token = secrets.load().get("api_token")
        return bool(token) and hmac.compare_digest(value, token)

    # ------------------------------------------------------------ guessing limits

    def allow_attempt(self, address: str) -> bool:
        now = time.time()
        recent = [t for t in self._failures.get(address, []) if now - t < FAILURE_WINDOW]
        self._failures[address] = recent
        return len(recent) < MAX_FAILURES

    def failed(self, address: str) -> None:
        self._failures.setdefault(address, []).append(time.time())

    def succeeded(self, address: str) -> None:
        self._failures.pop(address, None)

    # ------------------------------------------------------------ requests

    def is_local(self, scope) -> bool:
        client = scope.get("client")
        return bool(client) and is_loopback(client[0])

    def signed_in(self, scope) -> bool:
        """Allowed in: no sign-in needed, on this computer, a valid session cookie, or the API token."""
        if not self.required or self.is_local(scope):
            return True
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        bearer = headers.get("authorization", "")
        if bearer.lower().startswith("bearer ") and self.valid_token(bearer[7:].strip()):
            return True
        morsel = SimpleCookie(headers.get("cookie", "")).get(COOKIE)
        return bool(morsel) and self.valid_session(morsel.value)


class AuthGuard:
    """ASGI middleware: in network mode, requests from other computers must be signed in."""

    def __init__(self, app, auth: Auth):
        self.app, self.auth = app, auth

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"] in OPEN_PATHS or self.auth.signed_in(scope):
            return await self.app(scope, receive, send)
        if scope["path"].startswith("/api/"):
            body = json.dumps({"detail": "sign in required"}).encode()
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]})
            await send({"type": "http.response.body", "body": body})
        else:
            await send({"type": "http.response.start", "status": 303,
                        "headers": [(b"location", b"/login.html"), (b"content-length", b"0")]})
            await send({"type": "http.response.body", "body": b""})
