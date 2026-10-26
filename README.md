# Packrat

Search your game asset hoard.

Packrat is a desktop library for the game assets you've bought: sprites, tilesets, UI, sound effects, music and fonts. It keeps your purchases downloaded and in one place, and lets you find any file in seconds.

> **Status: pre-release.** It works from source today; installers come with the first release (v0.1).

## What it does

- **Browse your library.** Search by name and tag, preview images inside zips without unpacking them, and keep notes and license details per pack.
- **Sync your itch.io purchases.** Packrat downloads what you've bought into a folder you choose, skipping packs you already have.
- **Add free assets.** Search itch.io from Packrat and add free packs in one click.
- **Connect AI agents.** A built-in [MCP](https://modelcontextprotocol.io) server lets agents such as Claude search your library and copy assets into a project.

## What it doesn't do

Packrat only manages files **you** bought or downloaded. It never uploads, shares or redistributes assets, and never changes your library folders. Each asset stays under its creator's license.

## Platforms and stores

| | v0.1 | Later |
| --- | --- | --- |
| Platforms | Linux, Windows | macOS |
| Stores | itch.io | Humble Bundle (experimental) |

## Install

From the first release, download from the [Releases](https://github.com/Adamshrugged/packrat-assets/releases) page:

- **Windows:** `Packrat-Setup-<version>.exe` installs for your user, no admin rights needed. It isn't code-signed, so Windows may warn about it: choose *More info → Run anyway*.
- **Linux:** `Packrat-x86_64.AppImage`. Make it executable (`chmod +x Packrat-x86_64.AppImage`) and run it.

Packrat opens in your browser, with a tray icon to reopen or quit it. The Guide inside Packrat covers everyday use.

## Run from source

Needs Python 3.11 or newer. In a copy of this repository:

```
pip install .
packrat              # the desktop app: opens in your browser, with a tray icon
packrat serve        # server only (no browser, no tray), e.g. always on
packrat scan         # re-index the library folders
```

To let Claude Code use your library, run `claude mcp add packrat --scope user -- packrat mcp` once. (The Guide in Packrat shows the exact command for your install.)

Settings are kept in `~/.config/packrat` on Linux and `%APPDATA%\packrat` on Windows; the database and previews in `~/.local/share/packrat` or `%LOCALAPPDATA%\packrat`.

## Use it from other computers

Packrat normally only answers on its own computer. To open it to your network, set `host = "0.0.0.0"` under `[server]` in `config.toml`. Other computers then sign in with a password: on the first visit, enter the one-time setup code from Packrat's log and choose one (or run `packrat set-password`).

Don't put it behind a reverse proxy on the same machine: every visitor would look local and skip signing in.

## Run on a NAS or home server (Docker)

Copy [compose.yml](compose.yml), change the folder paths in it, then:

```
docker compose up -d
docker compose logs packrat     # the setup code for choosing a password
```

Open `http://<server>:8765`, enter the code, choose a password, and add `/library` in Settings. Your library is mounted read-only.

## Development

```
uv run pytest          # tests
uv run pytest -m live  # also checks that itch.io's pages still parse (needs internet)
```

Building the installers: see [packaging/README.md](packaging/README.md).

## License

The Packrat code is released under the [MIT License](LICENSE). This covers Packrat itself, not the assets it manages.
