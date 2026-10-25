#!/usr/bin/env bash
# Wraps the PyInstaller bundle (dist/Packrat) into one file that runs on most Linux distributions:
# dist/Packrat-x86_64.AppImage. Run after building the bundle (see packaging/README.md).
# Downloads appimagetool (https://github.com/AppImage/appimagetool) into build/ the first time.
set -euo pipefail
cd "$(dirname "$0")/../.."

appdir=build/Packrat.AppDir
rm -rf "$appdir"
mkdir -p "$appdir/usr"
cp -a dist/Packrat "$appdir/usr/packrat"
cp build/icons/packrat.png "$appdir/packrat.png"
cat > "$appdir/packrat.desktop" <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=Packrat
Comment=Your game asset library
Exec=packrat
Icon=packrat
Categories=Graphics;Development;
Terminal=false
DESKTOP
cat > "$appdir/AppRun" <<'APPRUN'
#!/bin/sh
exec "$(dirname "$(readlink -f "$0")")/usr/packrat/packrat" "$@"
APPRUN
chmod +x "$appdir/AppRun"

tool=build/appimagetool
if [ ! -x "$tool" ]; then
  curl -fsSL -o "$tool" https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage
  chmod +x "$tool"
fi
# Extract-and-run: works without FUSE (e.g. on CI machines).
APPIMAGE_EXTRACT_AND_RUN=1 ARCH=x86_64 "$tool" --no-appstream "$appdir" dist/Packrat-x86_64.AppImage
