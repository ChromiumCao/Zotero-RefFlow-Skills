"""Shared contracts; no Zotero writes and no dependency on a fixed skill path."""
from __future__ import annotations
import hashlib
import json
import re
from pathlib import Path

class OutputError(ValueError):
    pass

def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))

def write_json(path, value):
    path = Path(path).resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()

def normalized(value):
    return re.sub(r"[\W_]+", "", str(value or "").casefold())

def doi(value):
    m = re.search(r"10\.\d{4,9}/[^\s<>\"{}]+", str(value or ""), re.I)
    return m.group().rstrip(".,;)").casefold() if m else None

def identity(item):
    lib = item["library"]
    return f"{lib['type']}:{lib['id']}:{item['item_key']}"

URI = re.compile(r"^https?://(?:www\.)?zotero\.org/(users/(?:[1-9]\d*|local/[A-Za-z0-9]+)|groups/[1-9]\d*)/items/([A-Z0-9]{8})$")

def validate_uri(uri, item):
    m = URI.fullmatch(str(uri or ""))
    if not m or m[2] != item["item_key"]:
        raise OutputError("Missing or invalid authoritative item URI: " + identity(item))
    lib = item["library"]
    if lib["type"] == "group" and m[1] != f"groups/{lib['id']}":
        raise OutputError("URI/library mismatch")
    if lib["type"] == "user":
        if not m[1].startswith("users/"):
            raise OutputError("URI/library mismatch")
        if int(lib["id"]) != 0 and m[1] != f"users/{lib['id']}":
            raise OutputError("URI/public user ID mismatch")
    return uri

def validate_sources(documents, out=None):
    seen = set()
    output = Path(out).resolve() if out else None
    for doc in documents:
        path = Path(doc["path"]).resolve()
        if path in seen:
            raise OutputError("Source supplied more than once: " + str(path))
        seen.add(path)
        if output == path:
            raise OutputError("Output must be a new file, different from every source")
        if digest(path) != doc["sha256"]:
            raise OutputError("Source changed since scan: " + str(path))
