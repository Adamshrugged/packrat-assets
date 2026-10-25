# Building the installers

CI builds these for every version tag (`.github/workflows/release.yml`); this is how to build them by hand.

Both start from a PyInstaller bundle of Packrat with Python included:

```
uv run --with pyinstaller python packaging/make_icons.py
uv run --with pyinstaller pyinstaller packaging/packrat.spec --noconfirm
```

- **Windows:** then `iscc packaging\windows\packrat.iss` (Inno Setup 6) makes `dist\Packrat-Setup-<version>.exe`.
- **Linux:** then `packaging/linux/build_appimage.sh` makes `dist/Packrat-x86_64.AppImage`.
