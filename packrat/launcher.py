"""Regular-app mode: start Packrat, open it in the browser, and keep a tray icon to reopen or quit it.
Running it again while it's open just opens the browser."""
import json
import logging
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from logging.handlers import RotatingFileHandler

from .desktop import open_url

log = logging.getLogger("packrat")

# The favicon's four squares
COLORS = ["#5b8def", "#e8a33d", "#40b07a", "#d9576b"]


def page(cfg) -> str:
    return f"http://localhost:{cfg.port}/"


def already_running(cfg) -> str:
    """'packrat' if Packrat answers on its port, 'other' if another program does, '' if the port is free."""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{cfg.port}/api/ping", timeout=2) as r:
            return "packrat" if json.load(r).get("app") == "packrat" else "other"
    except urllib.error.HTTPError:
        return "other"
    except (urllib.error.URLError, OSError, ValueError):
        return ""


def icon_image(size: int = 64):
    """The app icon: the favicon's four squares. Also used to make the installers' icons."""
    from PIL import Image, ImageDraw
    im = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(im)
    u = size / 64
    for i, color in enumerate(COLORS):
        x, y = ((i % 2) * 34 + 2) * u, ((i // 2) * 34 + 2) * u
        draw.rounded_rectangle([x, y, x + 26 * u, y + 26 * u], radius=5 * u, fill=color)
    return im


def log_to_file(cfg) -> str:
    """The app has no console, so it logs to a file next to its database."""
    path = os.path.join(cfg.data_dir, "packrat.log")
    handler = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)  # not every page request
    return path


def make_tray(cfg, stopping: threading.Event):
    """A tray icon with Open and Quit: over D-Bus on Linux, pystray on Windows and macOS."""
    def open_page():
        open_url(page(cfg))
    if sys.platform.startswith("linux"):
        from . import tray_linux
        if not tray_linux.available():
            raise RuntimeError("this desktop has no tray (StatusNotifierItem)")
        return tray_linux.Tray("Packrat", icon_image(), [("Open Packrat", open_page), ("Quit", stopping.set)])
    import pystray
    return pystray.Icon("packrat", icon_image(), "Packrat", menu=pystray.Menu(
        pystray.MenuItem("Open Packrat", open_page, default=True), pystray.MenuItem("Quit", stopping.set)))


def run(cfg, open_browser: bool = True) -> None:
    import uvicorn

    from .app import create_app

    state = already_running(cfg)
    if state == "packrat":
        if open_browser:
            open_url(page(cfg))
        return
    if state == "other":
        raise SystemExit(f"Port {cfg.port} is used by another program. Set a different [server] port in {cfg.source}.")

    log_path = log_to_file(cfg)
    stopping = threading.Event()
    app = create_app(cfg, quit=stopping.set)
    server = uvicorn.Server(uvicorn.Config(app, host=cfg.host, port=cfg.port, log_config=None))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    while not server.started and thread.is_alive():
        time.sleep(0.05)
    if not thread.is_alive():
        raise SystemExit(f"Packrat couldn't start; see {log_path}")
    log.info("running at %s", page(cfg))
    if open_browser:
        open_url(page(cfg))

    try:
        icon = make_tray(cfg, stopping)
        threading.Thread(target=lambda: (stopping.wait(), icon.stop()), daemon=True).start()
        icon.run()  # returns when stopped (Quit in the tray or in Settings)
    except Exception as e:  # no tray on this desktop: Settings → Quit (or Ctrl+C) still stops it
        log.warning("no tray icon (%s); quit from Settings in Packrat", e)
        print(f"Packrat is running at {page(cfg)}. Quit it from Settings, or press Ctrl+C.")
        try:
            while not stopping.wait(0.5):  # a timeout keeps Ctrl+C working on Windows
                pass
        except KeyboardInterrupt:
            pass
    server.should_exit = True
    thread.join(timeout=10)
