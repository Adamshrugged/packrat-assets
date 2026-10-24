"""One-line installers for the MCP client, served by the catalog:

    Windows:      irm http://<catalog>/install.ps1 | iex
    Linux / Mac:  curl -fsSL http://<catalog>/install.sh | sh

They download mcp_server.py from the catalog, create a private virtual environment, install its
dependencies and register it with Claude Code for all projects. The catalog URL is baked in from the
address the script was requested from.

For testing: PACKRAT_HOME overrides the install folder; PACKRAT_SKIP_REGISTER=1 skips the
`claude mcp` registration.
"""

POWERSHELL = r"""
# Packrat MCP installer (Windows)
$ErrorActionPreference = 'Continue'
$base = '__BASE__'
$dir = if ($env:PACKRAT_HOME) { $env:PACKRAT_HOME } else { Join-Path $HOME '.packrat' }
function Fail($msg) { Write-Host "`nERROR: $msg" -ForegroundColor Red; return }

Write-Host "Installing the Packrat MCP server into $dir" -ForegroundColor Cyan
New-Item -ItemType Directory -Force $dir | Out-Null
try {
    Invoke-WebRequest "$base/download/mcp_server.py" -OutFile (Join-Path $dir 'mcp_server.py') -UseBasicParsing
} catch { Fail "Could not download from $base. Is the catalog running and reachable from this computer?"; return }

# Find Python 3.10+ (the 'py' launcher first; skips the Microsoft Store placeholder).
$py = $null
foreach ($cand in @(@('py', '-3'), @('python'), @('python3'))) {
    if (-not (Get-Command $cand[0] -ErrorAction SilentlyContinue)) { continue }
    $args0 = @($cand | Select-Object -Skip 1)
    $ok = & $cand[0] @args0 -c "import sys; print(sys.version_info >= (3, 10))" 2>$null
    if ($ok -eq 'True') { $py = $cand; break }
}
if (-not $py) { Fail "Python 3.10 or newer was not found. Install it (e.g. 'winget install Python.Python.3.12'), open a new terminal and run this again."; return }

$venvPy = Join-Path $dir 'venv\Scripts\python.exe'
if (-not (Test-Path $venvPy)) {
    Write-Host "Creating virtual environment..."
    $pyArgs = @($py | Select-Object -Skip 1)
    & $py[0] @pyArgs -m venv (Join-Path $dir 'venv')
    if (-not (Test-Path $venvPy)) { Fail "Could not create the virtual environment."; return }
}
Write-Host "Installing packages (mcp, pillow)..."
& $venvPy -m pip install --quiet --disable-pip-version-check --upgrade "mcp>=2.3" pillow
if ($LASTEXITCODE -ne 0) { Fail "pip install failed (see messages above)."; return }

$script = Join-Path $dir 'mcp_server.py'
if ($env:PACKRAT_SKIP_REGISTER -eq '1') { Write-Host "Skipping registration (test mode)."; return }
if (-not (Get-Command claude -ErrorAction SilentlyContinue)) {
    Fail "Claude Code ('claude') isn't on PATH. After installing it, run:`n  claude mcp add packrat --scope user -e PACKRAT_URL=$base -- `"$venvPy`" `"$script`""
    return
}
Write-Host "Registering with Claude Code..."
& claude mcp remove packrat -s user *> $null
& claude mcp add packrat --scope user -e "PACKRAT_URL=$base" -- $venvPy $script
& claude mcp list 2>$null | Select-String 'packrat'
Write-Host "`nDone. Restart Claude Code, then type /mcp to confirm 'packrat' is connected." -ForegroundColor Green
"""

SHELL = r"""#!/bin/sh
# Packrat MCP installer (Linux / macOS)
BASE='__BASE__'
DIR="${PACKRAT_HOME:-$HOME/.packrat}"
fail() { printf '\nERROR: %s\n' "$1" >&2; exit 1; }

echo "Installing the Packrat MCP server into $DIR"
mkdir -p "$DIR"
curl -fsSL "$BASE/download/mcp_server.py" -o "$DIR/mcp_server.py" \
  || fail "Could not download from $BASE. Is the catalog running and reachable from this computer?"

PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    PY="$c"; break
  fi
done
[ -n "$PY" ] || fail "Python 3.10 or newer was not found. Install it, then run this again."

if [ ! -x "$DIR/venv/bin/python" ]; then
  echo "Creating virtual environment..."
  "$PY" -m venv "$DIR/venv" || { rm -rf "$DIR/venv"; fail "Could not create a virtual environment. On Debian/Ubuntu: sudo apt install python3-venv"; }
fi
echo "Installing packages (mcp, pillow)..."
"$DIR/venv/bin/python" -m pip install --quiet --disable-pip-version-check --upgrade "mcp>=2.3" pillow \
  || fail "pip install failed (see messages above)."

if [ "${PACKRAT_SKIP_REGISTER:-0}" = "1" ]; then echo "Skipping registration (test mode)."; exit 0; fi
command -v claude >/dev/null 2>&1 || fail "Claude Code ('claude') isn't on PATH. After installing it, run:
  claude mcp add packrat --scope user -e PACKRAT_URL=$BASE -- $DIR/venv/bin/python $DIR/mcp_server.py"
echo "Registering with Claude Code..."
claude mcp remove packrat -s user >/dev/null 2>&1 || true
claude mcp add packrat --scope user -e "PACKRAT_URL=$BASE" -- "$DIR/venv/bin/python" "$DIR/mcp_server.py"
claude mcp list 2>/dev/null | grep packrat || true
printf '\nDone. Restart Claude Code, then type /mcp to confirm packrat is connected.\n'
"""


def render(template: str, base_url: str) -> str:
    return template.replace("__BASE__", base_url.rstrip("/").replace("'", ""))
