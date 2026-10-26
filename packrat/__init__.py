"""Packrat: a library for your game assets."""
from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("packrat-assets")
except PackageNotFoundError:  # running from a source folder that isn't installed
    __version__ = "dev"

ISSUES_URL = "https://github.com/Adamshrugged/packrat-assets/issues"
