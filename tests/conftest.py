import pytest


def toml_str(path) -> str:
    """A path as a TOML literal string: single quotes take Windows backslashes as they are."""
    return f"'{path}'"


@pytest.fixture(autouse=True)
def _private_download_folders(tmp_path, monkeypatch):
    """Keep tests away from the real ~/Packrat download folders."""
    monkeypatch.setenv("PACKRAT_DOWNLOADS", str(tmp_path / "default-itch"))
    monkeypatch.setenv("PACKRAT_HUMBLE_DOWNLOADS", str(tmp_path / "default-humble"))
