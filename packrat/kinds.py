import os

KINDS = {
    "image": {".png", ".gif", ".jpg", ".jpeg", ".bmp", ".webp", ".tga", ".svg"},
    "source": {".psd", ".aseprite", ".ase", ".ai", ".eps", ".kra", ".xcf", ".pdn",
               ".tmx", ".tsx", ".ldtk", ".blend", ".spine"},
    "audio": {".wav", ".ogg", ".mp3", ".flac", ".m4a", ".aif", ".aiff", ".opus"},
    "font": {".ttf", ".otf", ".woff", ".woff2", ".eot", ".fnt"},
    "archive": {".zip", ".rar", ".7z", ".unitypackage"},
    "doc": {".txt", ".pdf", ".md", ".docx", ".doc", ".rtf", ".url", ".html", ".htm", ".json"},
}
_BY_EXT = {ext: kind for kind, exts in KINDS.items() for ext in exts}

# Archives whose contents we can list and read.
LISTABLE = {".zip", ".rar", ".7z"}
# Extensions Pillow can turn into a thumbnail.
THUMBABLE = (KINDS["image"] - {".svg"}) | {".psd"}

JUNK_NAMES = {"desktop.ini", "thumbs.db", ".ds_store", "__macosx"}
JUNK_EXTS = {".sfk", ".tmp"}


def ext_of(name: str) -> str:
    return os.path.splitext(name)[1].lower()


def kind_of(name: str) -> str:
    return _BY_EXT.get(ext_of(name), "other")


def is_junk(name: str) -> bool:
    n = name.lower()
    return n in JUNK_NAMES or n.startswith("._") or ext_of(n) in JUNK_EXTS


def is_junk_member(member: str) -> bool:
    parts = member.split("/")
    return any(is_junk(p) for p in parts if p)
