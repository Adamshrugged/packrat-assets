"""Keeps other websites out. Packrat has no login, so it must only answer requests that really come from
this computer's browser tabs (or, if the user opts in, their own network):

- Host check: a request must be addressed to localhost, a loopback IP, an IP address when Packrat listens
  on the network, or a name listed in [server] allowed_hosts. This blocks DNS rebinding, where a web page
  points its own domain at 127.0.0.1 to read Packrat's responses.
- Origin check: a request that changes something (anything but GET/HEAD/OPTIONS) must not come from
  another site's page. This blocks a page silently posting a form to Packrat."""
import ipaddress
from urllib.parse import urlsplit

LOOPBACK_NAMES = {"localhost"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def is_loopback(host: str) -> bool:
    if host in LOOPBACK_NAMES or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def hostname(netloc: str) -> str:
    """'localhost:8765' -> 'localhost', '[::1]:8765' -> '::1'."""
    return (urlsplit("//" + netloc).hostname or "").lower()


class HostGuard:
    def __init__(self, app, bind_host: str, allowed_hosts: list[str]):
        self.app = app
        self.allowed = set(allowed_hosts)
        self.on_network = not is_loopback(bind_host)

    def host_ok(self, host: str) -> bool:
        return is_loopback(host) or host in self.allowed or (self.on_network and is_ip(host))

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1"): v.decode("latin-1") for k, v in scope["headers"]}
        host_header = headers.get("host", "")
        if not self.host_ok(hostname(host_header)):
            return await reject(send, "Packrat only answers requests addressed to localhost. "
                                      "To use another name, add it to [server] allowed_hosts in config.toml.")
        origin = headers.get("origin")
        if scope["method"] not in SAFE_METHODS and origin is not None:
            if origin == "null" or urlsplit(origin).netloc.lower() != host_header.lower():
                return await reject(send, "Request from another website blocked.")
        await self.app(scope, receive, send)


async def reject(send, message: str) -> None:
    body = message.encode()
    await send({"type": "http.response.start", "status": 403,
                "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                            (b"content-length", str(len(body)).encode())]})
    await send({"type": "http.response.body", "body": body})
