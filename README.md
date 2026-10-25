# Packrat

Search your game asset hoard.

Packrat is a desktop library for the game assets you've bought: sprites, tilesets, UI, sound effects, music and fonts. It keeps your purchases downloaded and in one place, and lets you find any file in seconds.

> **Status: pre-release.** Packrat is not usable yet. Watch this repo for the first release (v0.1).

## What it does

- **Browse your library.** Search by name and tag, preview thumbnails without unzipping packs, and see each pack's license.
- **Sync your purchases.** Connect your itch.io account and Packrat downloads what you've bought into a folder you choose.
- **Connect AI agents.** A built-in [MCP](https://modelcontextprotocol.io) server lets agents such as Claude search your library and copy assets into a project.

## What it doesn't do

Packrat only manages files **you** bought or downloaded. It never uploads, shares or redistributes assets. Each asset stays under its creator's license.

## Platforms and stores

| | v0.1 | Later |
| --- | --- | --- |
| Platforms | Linux, Windows | macOS |
| Stores | itch.io | Humble Bundle (experimental) |

## Install

From the first release, download from the [Releases](https://github.com/Adamshrugged/packrat-assets/releases) page:

- **Windows:** `Packrat-Setup-<version>.exe`. It installs for your user only, with no admin rights needed. Windows may warn that the app is unrecognized, since it isn't code-signed: choose *More info → Run anyway*.
- **Linux:** `Packrat-x86_64.AppImage`. Make it executable (`chmod +x Packrat-x86_64.AppImage`) and run it.

Packrat opens in your browser and adds a tray icon to reopen or quit it.

## Run from source

Needs Python 3.11 or newer.

```
pip install .
packrat              # desktop app: opens in your browser, with a tray icon to reopen or quit it
packrat serve        # server only (no browser, no tray), e.g. always on: http://localhost:8765
packrat scan         # re-index the library folders
```

Settings live in `~/.config/packrat/config.toml` on Linux and `%APPDATA%\packrat\config.toml` on Windows. The database and thumbnails live in `~/.local/share/packrat` or `%LOCALAPPDATA%\packrat`.

To let Claude Code use your library (search, previews, licenses, copying assets into a project), run once:

```
claude mcp add packrat --scope user -- packrat mcp
```

Packrat only listens on your own computer (`127.0.0.1`) and ignores requests from other websites. To use it from other computers (for example, running on a NAS), set `host = "0.0.0.0"` under `[server]` in `config.toml`. Other computers then sign in with a password. To choose it, open Packrat in a browser and enter the one-time setup code printed in Packrat's log, or run `packrat set-password`. (Behind a reverse proxy on the same machine, every visitor looks local and skips signing in, so don't set it up that way.)

## Run on a NAS or home server (Docker)

To keep Packrat running all the time and use it from any computer on your network, copy [compose.yml](compose.yml), change the folder paths in it, then:

```
docker compose up -d
docker compose logs packrat     # the setup code for choosing a password
```

Open `http://<server>:8765` in a browser, enter the code, choose a password, and add `/library` in Settings. Your library is mounted read-only; Packrat never changes it.

Until the first release the image isn't published yet, so build it yourself first: `docker build -t ghcr.io/adamshrugged/packrat-assets:latest .` in a copy of this repository.

## Tests

```
uv run pytest          # offline tests
uv run pytest -m live  # also checks that itch.io's pages still parse (needs internet)
```

## License

The Packrat code is released under the [MIT License](LICENSE). This covers Packrat itself, not the assets it manages.
