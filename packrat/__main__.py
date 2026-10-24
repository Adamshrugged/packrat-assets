import argparse
import logging
import sys

from . import config, db


def main() -> None:
    ap = argparse.ArgumentParser(prog="packrat", description="Packrat: a library for your game assets")
    ap.add_argument("--config", help="path to config.toml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("scan", help="index the asset roots")
    sub.add_parser("serve", help="run the web UI")
    it = sub.add_parser("itch-sync", help="import itch.io purchases and match them to the library")
    it.add_argument("--force", action="store_true", help="re-fetch file lists and page info for everything")
    th = sub.add_parser("thumbs", help="pre-generate thumbnails")
    th.add_argument("--all", action="store_true", help="every image, not just pack covers")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    cfg = config.load(args.config)
    db.init(cfg)

    if args.cmd == "scan":
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
        print(f"Packrat on http://localhost:{cfg.port}")
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
