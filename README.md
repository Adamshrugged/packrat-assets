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

## Run from source

Needs Python 3.11 or newer.

```
pip install .
packrat serve        # web UI at http://localhost:8765
packrat scan         # re-index the library folders
```

Settings live in `~/.config/packrat/config.toml` on Linux and `%APPDATA%\packrat\config.toml` on Windows. The database and thumbnails live in `~/.local/share/packrat` or `%LOCALAPPDATA%\packrat`.

To let Claude Code use your library (search, previews, licenses, copying assets into a project), run once:

```
claude mcp add packrat --scope user -- packrat mcp
```

Packrat only listens on your own computer (`127.0.0.1`) and ignores requests from other websites. It has no login, so don't expose it to your network unless you trust everyone on it.

## License

The Packrat code is released under the [MIT License](LICENSE). This covers Packrat itself, not the assets it manages.
