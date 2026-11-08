"""Atomic local results and locked, fail-closed write-attempt journals."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import tempfile
import uuid


def preflight_output(path, *, sources=(), replace=False):
    target = Path(path).expanduser().resolve()
    if target in {Path(p).expanduser().resolve() for p in sources if p}:
        raise ValueError("Output must not overwrite a source file")
    if target.exists() and not replace:
        raise FileExistsError("Existing output will not be overwritten: " + str(target))
    if not target.parent.is_dir():
        raise FileNotFoundError("Output directory does not exist: " + str(target.parent))
    descriptor, name = tempfile.mkstemp(prefix=".zotero-input-probe-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(b"{}")
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        Path(name).unlink(missing_ok=True)
    return target


def atomic_json(path, value, *, replace=True):
    target = Path(path)
    descriptor, name = tempfile.mkstemp(prefix="." + target.name + "-", dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if replace:
            os.replace(temporary, target)
        else:
            # Linking publishes a complete file and fails if another writer won.
            os.link(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_text(path, content, *, replace=False):
    target = Path(path)
    descriptor, name = tempfile.mkstemp(prefix="." + target.name + "-", dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        if replace:
            os.replace(temporary, target)
        elif os.name == "nt":
            # Windows rename refuses an existing destination; hard links may be restricted.
            os.rename(temporary, target)
        else:
            os.link(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def exclusive_lock(path):
    lock = Path(str(path) + ".lock")
    try:
        with lock.open("x", encoding="utf-8") as handle:
            json.dump({"pid": os.getpid()}, handle)
    except FileExistsError as exc:
        raise RuntimeError("Another job or interrupted job owns the journal lock; verify its PID before recovery: " + str(lock)) from exc
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


def default_import_journal(base_url, library, collection):
    scope = json.dumps([base_url, library.type, library.id, collection], ensure_ascii=True)
    directory = Path(os.environ.get("ZOTERO_INPUT_STATE_DIR", str(Path(tempfile.gettempdir()) / "zotero-input-state")))
    directory.mkdir(parents=True, exist_ok=True)
    return directory / (hashlib.sha256(scope.encode()).hexdigest() + ".imports.json")


class ImportJournal:
    def __init__(self, path, scope):
        self.path, self.scope = Path(path), scope
        self.run_id = uuid.uuid4().hex
        preflight_output(self.path, replace=True)
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ValueError("Unreadable import journal; no writes allowed") from exc
            if (not isinstance(self.data, dict) or self.data.get("schema_version") != "zotero-input.import-journal/1"
                    or self.data.get("scope") != scope or not isinstance(self.data.get("attempts"), dict)):
                raise ValueError("Import journal identity/schema is invalid; no writes allowed")
            allowed = {"add_started", "add_unverified", "add_denied", "import_started", "import_unverified", "import_denied", "verified"}
            for record in self.data["attempts"].values():
                if not isinstance(record, dict) or record.get("state") not in allowed:
                    raise ValueError("Import journal contains invalid attempt state; no writes allowed")
        else:
            self.data = {"schema_version": "zotero-input.import-journal/1", "scope": scope, "attempts": {}}
            self.save()

    def records(self, tokens):
        return [self.data["attempts"][t] for t in sorted(tokens) if t in self.data["attempts"]]

    def update(self, tokens, state, **details):
        record = {"state": state, "run_id": self.run_id, **details}
        for token in tokens:
            self.data["attempts"][token] = record
        self.save()

    def save(self):
        atomic_json(self.path, self.data)
