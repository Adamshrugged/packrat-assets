"""Listing and reading archives without extracting them."""
import subprocess
import zipfile

import pytest

from packrat import archives


@pytest.fixture
def zip_pack(tmp_path):
    path = tmp_path / "pack.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("Pack/hero.png", b"\x89PNG data")
        z.writestr("Pack/__MACOSX/._hero.png", b"junk")
        z.writestr("Pack/sub/", b"")
    return str(path)


def test_zip(zip_pack):
    assert [m[0] for m in archives.list_members(zip_pack, None)] == ["Pack/hero.png"]
    assert archives.read_member(zip_pack, "Pack/hero.png", None) == b"\x89PNG data"


@pytest.mark.parametrize("member, pattern", [("a[1]*.png", "a\\[1]\\*.png"), ("plain.png", "plain.png")])
def test_bsdtar_pattern(member, pattern):
    assert archives._bsdtar_pattern(member) == pattern


@pytest.mark.skipif(not archives._find("bsdtar"), reason="needs bsdtar")
def test_falls_back_when_7zip_fails(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "tile [1].png").write_bytes(b"tile")
    target = str(tmp_path / "pack.tar")
    subprocess.run([archives._find("bsdtar"), "-cf", target, "-C", str(src), "tile [1].png"], check=True)
    # a 7-Zip that can't open the file (like Fedora's with RAR) must not stop the other tools
    archives._worked.clear()
    assert [m[0] for m in archives.list_members(target, "/nonexistent/7z")] == ["tile [1].png"]
    assert archives.read_member(target, "tile [1].png", "/nonexistent/7z") == b"tile"
