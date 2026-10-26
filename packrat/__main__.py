import argparse
import logging
import sys

from . import __version__, config, db


def main() -> None:
    ap = argparse.ArgumentParser(prog="packrat", description="Packrat: a library for your game assets")
    ap.add_argument("--version", action="version", version=f"Packrat {__version__}")
    ap.add_argument("--config", help="path to config.toml")
    ap.add_argument("--background", action="store_true", help="start without opening the browser (start at login)")
    sub = ap.add_subparsers(dest="cmd", metavar="command",
                            description="With no command, Packrat starts as a desktop app: it opens in your "
                                        "browser and adds a tray icon to reopen or quit it.")
    sub.add_parser("scan", help="index the library folders")
    sub.add_parser("serve", help="run as a server only (no browser, no tray icon), e.g. on a NAS")
    it = sub.add_parser("itch-sync", help="import itch.io purchases and match them to the library")
    it.add_argument("--force", action="store_true", help="re-fetch file lists and page info for everything")
    th = sub.add_parser("thumbs", help="pre-generate thumbnails")
    th.add_argument("--all", action="store_true", help="every image, not just pack covers")
    mc = sub.add_parser("mcp", help="run the MCP server for AI agents (Packrat must be running)")
    mc.add_argument("--url", help="Packrat's address (default: http://localhost:<port from config>)")
    mc.add_argument("--token", help="API token, for a Packrat on another computer (shown in its Settings)")
    sub.add_parser("set-password", help="set the password for signing in from other computers (network mode)")
    args = ap.parse_args()

    if args.cmd == "mcp":
        # stdout carries the MCP protocol, so nothing else may print there.
        import os
        os.environ["PACKRAT_URL"] = (args.url or os.environ.get("PACKRAT_URL")
                                     or f"http://localhost:{config.load(args.config).port}")
        if args.token:
            os.environ["PACKRAT_TOKEN"] = args.token
        from .mcp_server import main as mcp_main
        mcp_main()
        return

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg = config.load(args.config)
    db.init(cfg)

    if args.cmd is None:
        from .launcher import run
        run(cfg, open_browser=not args.background)

    elif args.cmd == "set-password":
        import getpass
        from .auth import Auth
        password = getpass.getpass("New password: ")
        if password != getpass.getpass("Again: "):
            raise SystemExit("The passwords don't match.")
        try:
            Auth(required=False).set_password(password)
        except ValueError as e:
            raise SystemExit(str(e)) from None
        print("Password set. Other browsers will need to sign in again.")

    elif args.cmd == "scan":
        from .scanner import scan

        def progress(i, n, name):
            sys.stdout.write(f"\r[{i}/{n}] {name[:70]:<70}")
            sys.stdout.flush()

        result = scan(cfg, progress)
        print(f"\nDone: {result}")

    elif args.cmd == "itch-sync":
        from .itch import sync

        def progress(i, n, msg):
            sys.stdout.write(f"\r[{i}/{n}] {msg:<40}")
            sys.stdout.flush()

        print(f"\nDone: {sync(cfg, progress, force=args.force)}")

    elif args.cmd == "serve":
        import uvicorn
        from .app import create_app
        from .guard import is_loopback
        print(f"Packrat on http://localhost:{cfg.port}")
        if not is_loopback(cfg.host):
            print(f"Listening on {cfg.host}: other computers on your network sign in with a password.")
        uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, log_level="warning")

    elif args.cmd == "thumbs":
        from .kinds import THUMBABLE
        from .thumbs import cover_rows, make_thumbs
        conn = db.connect(cfg)
        if args.all:
            rows = conn.execute("SELECT * FROM files WHERE ext IN (%s)" % ",".join("?" * len(THUMBABLE)),
                                sorted(THUMBABLE)).fetchall()
        else:
            rows = cover_rows(conn)

        def progress(i, n, _label):
            if i % 50 == 0 or i == n:
                sys.stdout.write(f"\r{i}/{n}")
                sys.stdout.flush()

        print(f"\n{make_thumbs(cfg, rows, progress)} thumbnails made")


if __name__ == "__main__":
    main()
