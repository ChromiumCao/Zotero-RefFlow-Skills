"""Collection-first import coordinator and optional versioned source mappings."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
import json
from pathlib import Path
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid

import zotero as z
import local_state

SCHEMA_VERSION = "zotero-input.mapping/1"
SUCCESS = {"target_existing", "reused_added", "new_import", "fallback_import"}


class Unavailable(RuntimeError):
    pass


class PermissionDenied(RuntimeError):
    pass


class Uncertain(RuntimeError):
    pass


class MetadataConflict(Uncertain):
    def __init__(self, ref, primary, secondary):
        super().__init__("Source PMID conflicts with verified metadata or cannot be linked to the same article")
        self.evidence = {
            "basis": [], "ambiguous": True, "reason": "metadata_identity_unverified",
            "candidates": [],
            "metadata_records": {"source": identifiers(ref), "primary": identifiers(primary),
                                 "pubmed": identifiers(secondary)},
        }


@dataclass(frozen=True)
class Library:
    type: str = "user"
    id: int = 0

    def __post_init__(self):
        if self.type not in {"user", "group"} or self.id < 0:
            raise ValueError("Invalid library identity")
        if self.type == "group" and not self.id:
            raise ValueError("A group ID is required for group libraries")

    @property
    def path(self):
        return f"/api/{'users' if self.type == 'user' else 'groups'}/{self.id}"

    def json(self):
        return {"type": self.type, "id": self.id,
                "id_kind": ("user_id" if self.id else "local_user_alias") if self.type == "user" else "group_id"}


def scoped(item, library):
    identity = item.get("library", {})
    return identity.get("type") == library.type and identity.get("id") == library.id


def identity(item, library):
    data = item.get("data", item)
    return {"library": library.json(), "item_key": item.get("key", data.get("key")),
            "uri": item.get("uri") or data.get("uri") or None}


def author_name(value):
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, dict):
        value = value.get("lastName") or value.get("family") or value.get("name")
        return re.sub(r"[\W_]+", " ", z.field_key(str(value))).strip() if value else None
    if not value:
        return None
    value = re.split(r"[,;\u3001]", unicodedata.normalize("NFKC", str(value)))[0].strip()
    if z.contains_cjk(value) and not re.search(r"[a-z]", value, re.I):
        return re.sub(r"\s+", "", value)
    words = value.split()
    if len(words) > 1 and re.fullmatch(r"[A-Z.]{1,5}", words[-1]):
        words = words[:-1]
    return re.sub(r"[\W_]+", " ", words[-1].casefold()).strip() if words else None


def author_variants(value):
    if isinstance(value, list):
        value = value[0] if value else None
    values = {author_name(value)} - {None, ""}
    if isinstance(value, dict):
        family = value.get("lastName") or value.get("family") or ""
        given = value.get("firstName") or value.get("given") or ""
        if family and given and z.contains_cjk(family) and z.contains_cjk(given):
            values.add(z.field_key(family + given))
    return values


def reference_authors(value):
    data = value.get("data", value)
    return author_variants(data.get("first_author") or data.get("creators") or data.get("authors"))


def reference_titles(value):
    data = value.get("data", value)
    titles = {z.normalize_title(data.get("title"))} - {None}
    if data.get("_metadata_provenance"):
        titles.update(filter(None, (z.normalize_title(t) for t in data.get("title_aliases", []))))
    return titles


def identifiers(value):
    data = value.get("data", value)
    return {"doi": z.normalize_doi(data.get("doi") or data.get("DOI")),
            "pmid": z.pmid_from_item(data), "title": z.normalize_title(data.get("title")),
            "year": z.year_from_date(str(data.get("year") or data.get("date") or "")),
            "author": author_name(data.get("first_author") or data.get("creators") or data.get("authors"))}


def match_reference(ref, items, library):
    wanted = identifiers(ref)
    explicit = ref.get("zotero_item")
    candidates, conflicts, bases = [], [], []
    if explicit:
        lib = explicit.get("library", {})
        if lib.get("type") != library.type or lib.get("id") != library.id or not explicit.get("item_key"):
            return None, {"basis": [], "ambiguous": True, "reason": "explicit_identity_wrong_library_or_missing_key", "candidates": []}
    for item in items:
        if not scoped(item, library):
            continue
        actual = identifiers(item)
        basis = [k for k in ("doi", "pmid") if wanted[k] and wanted[k] == actual[k]]
        shared_titles = reference_titles(ref) & reference_titles(item)
        if shared_titles:
            basis.append("title")
        if explicit:
            if item.get("key") != explicit.get("item_key"):
                continue
            basis.insert(0, "item_identity")
        if not basis:
            continue
        wanted_authors, actual_authors = reference_authors(ref), reference_authors(item)
        contradictions = [k for k in ("doi", "pmid", "year")
                          if wanted[k] and actual[k] and wanted[k] != actual[k]]
        if wanted_authors and actual_authors and not wanted_authors & actual_authors:
            contradictions.append("author")
        corroborating = bool(wanted_authors & actual_authors) or bool(wanted["year"] and wanted["year"] == actual["year"])
        if basis == ["title"] and all(z.short_cjk_title(t) for t in shared_titles) and not corroborating and not contradictions:
            conflicts.append({"item": identity(item, library), "fields": ["short_title_needs_author_or_year"]})
            continue
        if contradictions:
            conflicts.append({"item": identity(item, library), "fields": contradictions})
        else:
            candidates.append(item)
            bases.append(basis)
    ambiguous = bool(conflicts) or len(candidates) > 1
    detail = {"basis": bases[0] if len(candidates) == 1 else [], "ambiguous": ambiguous,
              "reason": "identifier_conflict" if conflicts else "multiple_candidates" if ambiguous else None,
              "candidates": [identity(i, library) for i in candidates], "conflicts": conflicts}
    return (candidates[0] if len(candidates) == 1 and not ambiguous else None), detail


def normalize_sources(documents):
    """Never collapse source occurrences, even when their references are identical."""
    records, seen = [], set()
    for doc in documents:
        doc_id = str(doc.get("document_id") or "")
        if not doc_id:
            raise ValueError("Structured input documents need document_id")
        for index, value in enumerate(doc.get("references", []), 1):
            ref = deepcopy(value)
            ref_id = str(ref.get("reference_id") or f"ref-{index:06d}")
            if (doc_id, ref_id) in seen:
                raise ValueError(f"Duplicate source identity: {doc_id}/{ref_id}")
            seen.add((doc_id, ref_id))
            ref["document_id"], ref["reference_id"] = doc_id, ref_id
            ref.setdefault("raw_text", None)
            ref.setdefault("label", None)
            ref.setdefault("source_location", None)
            ref["doi"] = z.normalize_doi(ref.get("doi") or ref.get("DOI"))
            ref["pmid"] = z.normalize_pmid(ref.get("pmid") or ref.get("PMID"))
            records.append(ref)
    return records


def file_sources(paths, encoding=None):
    docs = []
    for filename in paths:
        path = Path(filename).expanduser().resolve()
        if path.suffix.lower() == ".ris":
            import reference_metadata
            refs = reference_metadata.references_from_ris(path, encoding=encoding)
        else:
            refs = z.extract_source_references(path, encoding=encoding)
        docs.append({"document_id": path.as_uri(), "references": refs})
    return normalize_sources(docs)


def permission_error(value):
    """Recognize policy refusals across RPC, MCP and tool-result envelopes."""
    if isinstance(value, dict):
        if value.get("code") in (401, 403, "EACCES", "EPERM", "PERMISSION_DENIED", "WRITE_DISABLED"):
            return True
        if value.get("writeOperationsEnabled") is False or value.get("write_enabled") is False:
            return True
        return any(permission_error(v) for v in value.values())
    if isinstance(value, list):
        return any(permission_error(v) for v in value)
    return bool(re.search(
        r"permission|read.only|not editable|access denied|forbidden|unauthorized|not authorized|"
        r"\bEACCES\b|\bEPERM\b|write\s+(?:operations?|access|permissions?)\s+(?:are\s+|is\s+)?"
        r"(?:currently\s+)?(?:disabled|denied|not enabled)", str(value), re.I))


def parse_rpc_body(raw, request_id):
    try:
        result = json.loads(raw)
    except json.JSONDecodeError:
        messages = []
        for block in raw.replace("\r\n", "\n").split("\n\n"):
            data = "\n".join(line[5:].lstrip() for line in block.splitlines() if line.startswith("data:"))
            if data:
                messages.append(json.loads(data))
        result = next((m for m in messages if m.get("id") == request_id), None)
    if not isinstance(result, dict) or result.get("id") != request_id:
        raise Uncertain("MCP response did not identify this request")
    if result.get("error"):
        message = str(result["error"])
        if permission_error(result["error"]):
            raise PermissionDenied(message)
        raise RuntimeError(message)
    return result.get("result", {})


class Plugin:
    """Optional installed zotero-mcp-plugin; never a required dependency."""
    def __init__(self, url="http://127.0.0.1:23120/mcp"):
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "http" or parsed.hostname not in {"localhost", "127.0.0.1", "::1"} or parsed.username or parsed.password:
            raise ValueError("The plugin URL must be local")
        self.url, self.session, self.serial = url, None, 0
        self.tools, self.reason, self.denied = {}, None, False
        try:
            self.rpc("initialize", {"protocolVersion": "2024-11-05", "capabilities": {},
                                    "clientInfo": {"name": "zotero-input", "version": "2"}})
            self.rpc("notifications/initialized", {}, notification=True)
            cursor = None
            for _ in range(20):
                result = self.rpc("tools/list", {"cursor": cursor} if cursor else {})
                self.tools.update({t["name"]: t for t in result.get("tools", [])})
                cursor = result.get("nextCursor")
                if not cursor:
                    break
        except Exception as exc:
            self.tools = {}
            self.reason = str(exc)
            self.denied = isinstance(exc, PermissionDenied)

    def rpc(self, method, params, *, notification=False):
        self.serial += 1
        body = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notification:
            body["id"] = self.serial
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=20 if method == "tools/call" else 4) as response:
                self.session = response.headers.get("Mcp-Session-Id") or self.session
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise PermissionDenied(f"MCP denied access: HTTP {exc.code}") from exc
            raise
        return {} if notification else parse_rpc_body(raw, self.serial)

    def supports(self, name, fields):
        if getattr(self, "denied", False):
            raise PermissionDenied(self.reason)
        schema = self.tools.get(name, {}).get("inputSchema", {})
        return set(fields).issubset(schema.get("properties", {})) and set(schema.get("required", [])).issubset(fields)

    def call(self, name, arguments):
        if getattr(self, "denied", False):
            raise PermissionDenied(self.reason)
        try:
            result = self.rpc("tools/call", {"name": name, "arguments": arguments})
        except PermissionDenied as exc:
            self.denied, self.reason = True, str(exc)
            raise
        if result.get("isError"):
            message = str(result.get("content"))
            if permission_error(result.get("content")):
                self.denied, self.reason = True, message
                raise PermissionDenied(message)
            raise RuntimeError(message)
        if "structuredContent" in result:
            value = result["structuredContent"]
        else:
            texts = [c["text"] for c in result.get("content", []) if c.get("type") == "text"]
            if len(texts) != 1:
                raise Uncertain("Unexpected MCP tool response")
            value = json.loads(texts[0])
        if isinstance(value, dict) and (value.get("error") or value.get("success") is False):
            message = str(value)
            if permission_error(value):
                self.denied, self.reason = True, message
                raise PermissionDenied(message)
            raise RuntimeError(message)
        return value


class LocalBackend:
    def __init__(self, library=Library(), plugin=None):
        self.library = library
        self.plugin = plugin if plugin is not None else Plugin()
        self._internal_id = None
        self._library_name = None

    def bind_library(self, row):
        # /users/0 aliases the local user; response identity may be a real user ID.
        actual = row.get("library", {})
        if self.library == Library() and actual.get("type") == "user" and isinstance(actual.get("id"), int):
            self.library = Library("user", actual["id"])

    def read(self, path):
        response = z.request(path)
        if response.status in (401, 403):
            raise PermissionDenied(f"Local API denied access to {path}")
        if not response.ok:
            raise Uncertain(f"Local read failed ({response.status}): {response.error or response.text[:200]}")
        return json.loads(response.text)

    def all(self, suffix):
        rows, start = [], 0
        while True:
            page = self.read(self.library.path + suffix + "?" + z.query({"limit": 100, "start": start}))
            if not isinstance(page, list):
                raise Uncertain("Expected a complete item list")
            rows.extend(page)
            if page:
                self.bind_library(page[0])
            if len(page) < 100:
                return rows
            start += len(page)

    def collection(self, key):
        row = self.read(self.library.path + "/collections/" + urllib.parse.quote(key, safe=""))
        self.bind_library(row)
        if not scoped(row, self.library) or row.get("key") != key:
            raise Uncertain("Collection identity/library mismatch")
        self._library_name = row.get("library", {}).get("name")
        return row

    def target_items(self, key):
        self.collection(key)
        rows = self.all(f"/collections/{urllib.parse.quote(key, safe='')}/items/top")
        if any(not scoped(i, self.library) for i in rows):
            raise Uncertain("Cross-library item in target response")
        return [i for i in rows if not i.get("data", {}).get("deleted")
                and i.get("data", {}).get("itemType") not in {"note", "attachment", "annotation"}]

    def library_items(self):
        rows = self.all("/items/top")
        if any(not scoped(i, self.library) for i in rows):
            raise Uncertain("Cross-library item in library response")
        return [i for i in rows if not i.get("data", {}).get("deleted")
                and i.get("data", {}).get("itemType") not in {"note", "attachment", "annotation"}]

    def get_item(self, key):
        row = self.read(self.library.path + "/items/" + urllib.parse.quote(key, safe=""))
        if not scoped(row, self.library) or row.get("key") != key:
            raise Uncertain("Item identity/library mismatch")
        return row

    def internal_library_id(self):
        if self._internal_id is not None:
            return self._internal_id
        if self.plugin.supports("get_libraries", {"limit", "offset"}):
            libs = []
            for offset in range(0, 10000, 100):
                try:
                    page = self.plugin.call("get_libraries", {"limit": 100, "offset": offset})
                except PermissionDenied:
                    raise
                except Exception as exc:
                    raise Unavailable("Cannot read plugin libraries: " + str(exc)) from exc
                if not isinstance(page, list):
                    raise Unavailable("Cannot identify plugin libraries")
                libs.extend(page)
                if len(page) < 100:
                    break
            if self.library.type == "user":
                matches = [v for v in libs if v.get("libraryType") == "user"]
            else:
                # Plugin libraryID and public group ID are different. Only use a
                # unique name correspondence proven on both read surfaces.
                groups = self.read("/api/users/0/groups?limit=10000")
                group = [v for v in groups if (v.get("id") or v.get("data", {}).get("id")) == self.library.id]
                if len(group) != 1:
                    raise Unavailable("Cannot identify selected group")
                name = group[0]["data"]["name"]
                if sum(v.get("data", {}).get("name") == name for v in groups) != 1:
                    raise Unavailable("Group names are not unique; no reliable libraryID mapping")
                matches = [v for v in libs if v.get("libraryType") == "group" and v.get("name") == name]
            if len(matches) == 1:
                self._internal_id = matches[0]["libraryID"]
                return self._internal_id
        raise Unavailable("No reliable internal libraryID mapping")

    def can_add(self):
        return self.plugin.supports("add_items_to_collection", {"libraryID", "collectionKey", "itemKeys"})

    def add(self, key, collection):
        if not self.can_add():
            raise Unavailable(self.plugin.reason or "add_items_to_collection unavailable")
        return self.plugin.call("add_items_to_collection", {
            "libraryID": self.internal_library_id(), "collectionKey": collection, "itemKeys": [key]})

    def can_remove(self):
        return self.plugin.supports("remove_items_from_collection", {"libraryID", "collectionKey", "itemKeys"})

    def remove(self, key, collection):
        if not self.can_remove():
            raise Unavailable("remove_items_from_collection unavailable")
        return self.plugin.call("remove_items_from_collection", {
            "libraryID": self.internal_library_id(), "collectionKey": collection, "itemKeys": [key]})

    def assert_selected(self, key):
        wanted = self.collection(key)
        response = z.connector_post("/connector/getSelectedCollection", {})
        if response.status in (401, 403):
            raise PermissionDenied("Connector denied access")
        if not response.ok:
            raise Unavailable("Cannot read Connector target")
        selected = json.loads(response.text)
        if not selected.get("editable") or not selected.get("libraryEditable"):
            raise PermissionDenied("Selected Connector destination is not editable")
        collections = self.all("/collections")
        by_key = {c["key"]: c["data"] for c in collections}
        chain, cursor, seen = [], key, set()
        while cursor:
            if cursor in seen or cursor not in by_key:
                raise Uncertain("Cannot resolve target hierarchy")
            seen.add(cursor)
            chain.insert(0, by_key[cursor]["name"])
            cursor = by_key[cursor].get("parentCollection")
        try:
            lib_id = self.internal_library_id()
        except Unavailable:
            name = wanted["library"].get("name")
            roots = [t for t in selected.get("targets", []) if t.get("level") == 0 and t.get("name") == name]
            groups = self.read("/api/users/0/groups?limit=10000")
            names = [g.get("data", {}).get("name") for g in groups]
            expected = 0 if self.library.type == "user" else 1
            if not name or len(roots) != 1 or names.count(name) != expected:
                raise Unavailable("Cannot prove selected library identity")
            lib_id = int(str(roots[0]["id"]).removeprefix("L"))
        paths, current_lib, stack = [], None, []
        for target in selected.get("targets", []):
            level = target.get("level", 0)
            if level == 0:
                current_lib, stack = target["id"], []
            else:
                stack = stack[:level - 1] + [target["name"]]
                if current_lib == f"L{lib_id}" and stack == chain:
                    paths.append(target["id"])
        if selected.get("libraryID") != lib_id or len(paths) != 1 or paths[0] != f"C{selected.get('id')}":
            raise Unavailable("Connector selection is not the exact requested library/collection")

    def import_entry(self, entry, collection):
        if getattr(self.plugin, "denied", False):
            raise PermissionDenied(self.plugin.reason or "Writer permission was denied")
        self.assert_selected(collection)
        ris = z._pubmed_to_ris(entry) if entry.get("pmid") else z._doi_to_full_ris(entry)
        response = z.connector_post("/connector/import?" + z.query({"session": "codex-" + uuid.uuid4().hex}),
                                    z.clean_ris_for_import(ris), content_type="text/plain")
        if response.status in (401, 403):
            raise PermissionDenied("Connector import permission denied")
        if not response.ok:
            raise Uncertain(f"Connector import response {response.status}: {response.error or response.text[:200]}")
        # Receipt is informational. Only a subsequent target read confirms success.
        return json.loads(response.text) if response.text else None


def lookup_metadata(ref):
    if ref.get("metadata"):
        import reference_metadata
        return reference_metadata.checked_metadata(ref)
    ref = {k: v for k, v in ref.items() if k != "zotero_item"}
    values = identifiers(ref)
    if values["doi"]:
        entries = [z._lookup_by_doi(values["doi"])]
        if not entries[0] and values["pmid"]:
            entries = [z._pubmed_fetch(values["pmid"])]
    elif values["pmid"]:
        entries = [z._pubmed_fetch(values["pmid"])]
    elif ref.get("title"):
        entries = z._search_crossref(ref["title"], first_author=ref.get("first_author"), year=ref.get("year"))
        if not entries:
            entries = []
            for pmid in z._pubmed_search(ref["title"], author=ref.get("first_author"), year=ref.get("year"))[:5]:
                time.sleep(0.35)
                entries.append(z._pubmed_fetch(pmid))
    else:
        raise Uncertain("Reference needs an identifier or a verified parsed title")
    candidates = []
    for n, entry in enumerate(e for e in entries if e):
        candidates.append({"key": str(n), "library": Library().json(), "data": entry})
    matched, detail = match_reference(ref, candidates, Library())
    if not matched:
        raise Uncertain("Metadata lookup is unresolved: " + str(detail.get("reason")))
    entry = deepcopy(matched["data"])
    if not entry.get("title") or not entry.get("creators"):
        raise Uncertain("Metadata lacks title or authors")
    if values["pmid"] and not entry.get("pmid"):
        extra = z._pubmed_fetch(values["pmid"])
        if extra:
            candidate = {"key": "pubmed", "library": Library().json(), "data": extra}
            verified, _ = match_reference(ref, [candidate], Library())
            # Matching the input PMID alone cannot link two metadata records.
            # Compare with the primary record before copying any PubMed fields.
            same_record, _ = match_reference(entry, [candidate], Library())
            if not verified or not same_record or z.normalize_pmid(extra.get("pmid")) != values["pmid"]:
                raise MetadataConflict(ref, entry, extra)
            entry["pmid"] = extra["pmid"]
            if not entry.get("abstract"):
                entry["abstract"] = extra.get("abstract", "")
        else:
            entry.setdefault("_warnings", []).append("Source PMID could not be verified; omitted from imported metadata")
    return entry


def reconcile(references, backend, collection, *, lookup=lookup_metadata, checks=3, delay=0.3, journal_path=None):
    if journal_path:
        with local_state.exclusive_lock(journal_path):
            backend.collection(collection)
            scope = {"base_url": z.DEFAULT_BASE_URL, "library": backend.library.json(), "collection": collection}
            journal = local_state.ImportJournal(journal_path, scope)
            return _reconcile(references, backend, collection, lookup=lookup, checks=checks, delay=delay, journal=journal)
    return _reconcile(references, backend, collection, lookup=lookup, checks=checks, delay=delay)


def _reconcile(references, backend, collection, *, lookup=lookup_metadata, checks=3, delay=0.3, journal=None):
    """Ensure membership with <=1 add and <=1 import per article per invocation."""
    if not 1 <= checks <= 5:
        raise ValueError("Verification checks must be between 1 and 5")
    preflight_error = None
    try:
        backend.collection(collection)
    except Exception as exc:
        preflight_error = exc
    library = backend.library
    mappings, attempted, metadata_cache, validation_refs = [], set(), {}, []
    library_rows = None
    write_denial = None

    def enrich(ref, entry):
        result = dict(ref)
        for field in ("doi", "pmid", "title", "year", "creators"):
            if not result.get(field) and entry.get(field):
                result[field] = entry[field]
        if entry.get("_metadata_provenance"):
            result["_metadata_provenance"] = entry["_metadata_provenance"]
            result["title_aliases"] = list(dict.fromkeys([entry["title"], *entry.get("title_aliases", [])]))
        return result

    def metadata(ref):
        clean = {k: v for k, v in ref.items() if k != "zotero_item"}
        signature = json.dumps(identifiers(clean), sort_keys=True)
        if signature not in metadata_cache:
            entry = lookup(clean)
            candidate = {"key": "metadata", "library": library.json(), "data": entry or {}}
            matched, detail = match_reference(clean, [candidate], library)
            if not matched or not entry.get("title") or not entry.get("creators"):
                raise Uncertain("Metadata not verified: " + str(detail))
            metadata_cache[signature] = entry
        return metadata_cache[signature]

    def write_tokens(ref):
        ids = identifiers(ref)
        tokens = {f"{k}:{ids[k]}" for k in ("doi", "pmid", "title") if ids[k]}
        if ref.get("zotero_item"):
            tokens.add("item:" + str(ref["zotero_item"].get("item_key")))
        return tokens

    def guard_prior(tokens):
        if journal and any(r["state"] != "verified" and r.get("run_id") != journal.run_id for r in journal.records(tokens)):
            raise Uncertain("Resolved identifiers have an earlier uncertain/denied attempt; read-only reconciliation required")

    def check_preserved(existing, old_members):
        current = backend.get_item(existing["key"])
        if not old_members.issubset(current["data"].get("collections", [])):
            raise Uncertain("Reuse changed pre-existing collection memberships")

    def verify(ref, expected_key=None):
        last_rows = []
        for n in range(checks):
            last_rows = backend.target_items(collection)
            found, detail = match_reference(ref, last_rows, library)
            if detail["ambiguous"]:
                raise Uncertain("Target became ambiguous: " + str(detail))
            if found:
                if collection not in found.get("data", {}).get("collections", []):
                    raise Uncertain("Target list and item collection membership disagree")
                if expected_key is None or found["key"] == expected_key:
                    return found, detail, last_rows
                raise Uncertain("Target matched a different item than the attempted reuse")
            if n + 1 < checks:
                time.sleep(delay)
        return None, detail, last_rows

    for source in references:
        ref = deepcopy(source)
        source_tokens = write_tokens(ref)
        row = {"source": {k: deepcopy(source.get(k)) for k in
                          ("document_id", "reference_id", "raw_text", "label", "source_location", "source_encoding")},
               "identifiers": {k: identifiers(ref)[k] for k in ("doi", "pmid", "title")},
               "match": {"basis": [], "ambiguous": False, "candidates": []},
               "item": None, "target_collection": {"library": library.json(), "key": collection},
               "membership_verified": False, "outcome": "pending", "fallback_reason": None,
               "known_duplicates": [], "anomalies": []}
        row["metadata_provenance"] = None
        mappings.append(row)
        try:
            if preflight_error:
                raise preflight_error
            if ref.get("extraction_errors"):
                raise Uncertain("Source extraction needs review: " + str(ref["extraction_errors"]))
            if ref.get("metadata"):
                import reference_metadata
                supplied = reference_metadata.checked_metadata(ref)
                ref = enrich(ref, supplied)
                row["metadata_provenance"] = deepcopy(supplied["_metadata_provenance"])
                row["identifiers"] = {k: identifiers(ref)[k] for k in ("doi", "pmid", "title")}
            if journal and ref.get("zotero_item"):
                original = ref["zotero_item"]
                for attempt in journal.records(source_tokens):
                    previous = attempt.get("mapping", {})
                    duplicate = any(old.get("item_key") == original.get("item_key")
                                    and all(old.get("library", {}).get(k) == original.get("library", {}).get(k) for k in ("type", "id"))
                                    for old in previous.get("known_duplicates", []))
                    if not duplicate:
                        continue
                    recovered = deepcopy(ref)
                    for key, value in previous.get("identifiers", {}).items():
                        if not recovered.get(key) and value:
                            recovered[key] = value
                    old = backend.get_item(original["item_key"])
                    if not match_reference(recovered, [old], library)[0]:
                        raise Uncertain("Previously duplicated source item no longer agrees with the reference")
                    recovered.pop("zotero_item")
                    if attempt["state"] == "verified" and previous.get("item"):
                        recovered["zotero_item"] = deepcopy(previous["item"])
                    ref = recovered
                    row["fallback_reason"] = previous.get("fallback_reason")
                    row["known_duplicates"] = deepcopy(previous.get("known_duplicates", []))
                    row["adoption_history"] = deepcopy(previous.get("adoption_history") or [{
                        k: deepcopy(previous.get(k)) for k in ("outcome", "item", "fallback_reason", "known_duplicates")}])
                    break
            target = backend.target_items(collection)
            found, detail = match_reference(ref, target, library)
            row["match"] = detail
            if detail["ambiguous"]:
                continue
            if found:
                found, detail, _ = verify(ref, found["key"])
                if not found:
                    raise Uncertain("Target membership changed while verifying")
                row.update(item=identity(found, library), membership_verified=True,
                           outcome="target_existing", match=detail)
                if journal:
                    prior = journal.records(source_tokens)
                    adopted = next((r.get("mapping") for r in prior if r.get("mapping", {}).get("item") == row["item"]), None)
                    if adopted:
                        row["fallback_reason"] = adopted.get("fallback_reason")
                        row["known_duplicates"] = deepcopy(adopted.get("known_duplicates", []))
                        row["adoption_history"] = deepcopy(adopted.get("adoption_history") or [{
                            k: deepcopy(adopted.get(k)) for k in ("outcome", "item", "fallback_reason", "known_duplicates")}])
                continue
            if journal and any(r["state"] != "verified" for r in journal.records(source_tokens)):
                found, detail, _ = verify(ref)
                if found:
                    row.update(item=identity(found, library), membership_verified=True, outcome="target_existing", match=detail)
                    continue
                raise Uncertain("Earlier write attempt is not visible; read-only reconciliation required, no automatic repeat")
            if write_denial:
                raise write_denial
            if library_rows is None:
                library_rows = backend.library_items()
            existing, detail = match_reference(ref, library_rows, library)
            row["match"] = detail
            if detail["ambiguous"]:
                continue
            if ref.get("zotero_item") and not existing:
                raise Uncertain("Explicit Zotero identity was not found in the selected library")
            # Resolve external identifiers before declaring a library miss, then
            # re-match (e.g. source PMID vs library DOI-only record).
            if not existing:
                entry = metadata(ref)
                ref = enrich(ref, entry)
                row["identifiers"] = {k: identifiers(ref)[k] for k in ("doi", "pmid", "title")}
                existing, detail = match_reference(ref, library_rows, library)
                row["match"] = detail
                if detail["ambiguous"]:
                    continue
                found, detail, _ = verify(ref)
                if found:
                    row.update(item=identity(found, library), membership_verified=True,
                               outcome="target_existing", match=detail)
                    continue
            else:
                entry = None
            tokens = write_tokens(ref)
            guard_prior(tokens)
            if tokens & attempted:
                raise Uncertain("A write for this article was already attempted; verify before retrying")
            fallback, permission_error = None, None
            if existing:
                existing = backend.get_item(existing["key"])
                current_match, current_detail = match_reference(ref, [existing], library)
                if not current_match:
                    row["match"] = current_detail
                    raise Uncertain("Existing item changed before reuse")
                old_members = set(existing["data"].get("collections", []))
                current_ids = identifiers(existing)
                ref = enrich(ref, {**existing["data"], **{k: current_ids[k] for k in ("doi", "pmid", "year")}})
                tokens |= write_tokens(ref)
                guard_prior(tokens)
                if tokens & attempted:
                    raise Uncertain("A write for this article was already attempted")
                row["identifiers"] = {k: identifiers(ref)[k] for k in ("doi", "pmid", "title")}
                try:
                    if not backend.can_add():
                        raise Unavailable("Existing-item collection write is unavailable")
                    attempted.update(tokens)
                    if journal:
                        journal.update(source_tokens | tokens, "add_started", item=identity(existing, library))
                    backend.add(existing["key"], collection)
                except PermissionDenied as exc:
                    permission_error = exc
                    fallback = str(exc)
                except Exception as exc:
                    fallback = str(exc)
                found, detail, _ = verify(ref, existing["key"])
                if found:
                    check_preserved(existing, old_members)
                    row.update(item=identity(found, library), membership_verified=True,
                               outcome="reused_added", match=detail)
                    if fallback:
                        row["anomalies"].append("Write response abnormal; actual membership verified: " + fallback)
                    continue
                if permission_error:
                    if journal:
                        journal.update(source_tokens | tokens, "add_denied", reason=str(permission_error))
                    raise permission_error
                fallback = fallback or "Reuse returned success but membership was not verified"
                row["fallback_reason"] = fallback
                row["known_duplicates"] = [identity(existing, library)]
            if entry is None:
                entry = metadata(ref)
            row["metadata_provenance"] = deepcopy(entry.get("_metadata_provenance"))
            ref = enrich(ref, entry)
            row["identifiers"] = {k: identifiers(ref)[k] for k in ("doi", "pmid", "title")}
            row["anomalies"].extend(entry.get("_warnings", []))
            if not entry.get("abstract"):
                row["anomalies"].append("Abstract unavailable from metadata source")
            # Re-read immediately before fallback/new import, including after any
            # slow metadata lookup, to catch successful writes with lost replies.
            found, detail, _ = verify(ref)
            if found:
                if existing:
                    check_preserved(existing, old_members)
                row.update(item=identity(found, library), membership_verified=True,
                           outcome="reused_added" if existing else "target_existing", match=detail,
                           fallback_reason=None, known_duplicates=[])
                continue
            # An explicit old identity is evidence for reuse, not the identity
            # of a fallback copy. Match the verified metadata after importing.
            ref.pop("zotero_item", None)
            attempted.update(tokens | write_tokens(ref))
            if journal:
                guard_prior(write_tokens(ref))
                journal.update(source_tokens | tokens | write_tokens(ref), "import_started", mapping=deepcopy(row))
            write_error = None
            try:
                backend.import_entry(entry, collection)
            except PermissionDenied as exc:
                write_error = exc
            except Exception as exc:
                write_error = exc
            found, detail, _ = verify(ref)
            if not found:
                if journal:
                    journal.update(source_tokens | tokens | write_tokens(ref),
                                   "import_denied" if isinstance(write_error, PermissionDenied) else "import_unverified",
                                   mapping=deepcopy(row), reason=str(write_error))
                if isinstance(write_error, PermissionDenied):
                    raise write_error
                raise Uncertain("Import not verified; no automatic retry. " + str(write_error or "No matching target item"))
            row.update(item=identity(found, library), membership_verified=True,
                       outcome="fallback_import" if existing else "new_import", match=detail)
            if write_error:
                row["anomalies"].append("Import response abnormal; membership verified: " + str(write_error))
            library_rows = None
        except PermissionDenied as exc:
            write_denial = exc
            row.update(outcome="failed")
            row["anomalies"].append(str(exc))
        except MetadataConflict as exc:
            row["match"] = exc.evidence
            row["anomalies"].append(str(exc))
        except Exception as exc:
            row["anomalies"].append(str(exc))
        finally:
            validation_refs.append(deepcopy(ref))
    # A final snapshot validates all source occurrences, including those that
    # reused an item before later writes in the same invocation.
    successful = [r for r in mappings if r["outcome"] in SUCCESS]
    if successful:
        try:
            final_items = {i["key"]: i for i in backend.target_items(collection) if scoped(i, library)}
            for row, ref in zip(mappings, validation_refs):
                if row["outcome"] not in SUCCESS:
                    continue
                adopted = final_items.get(row["item"]["item_key"])
                if not adopted or collection not in adopted.get("data", {}).get("collections", []):
                    row.update(outcome="pending", membership_verified=False)
                    row["anomalies"].append("Final collection snapshot did not confirm the adopted item")
                elif not match_reference({**ref, "zotero_item": row["item"]}, [adopted], library)[0]:
                    row.update(outcome="pending", membership_verified=False)
                    row["anomalies"].append("Final adopted item metadata no longer agrees with this source")
                elif not adopted.get("data", {}).get("abstractNote"):
                    row["anomalies"].append("Zotero item has no abstract; existing metadata was not overwritten")
                elif re.fullmatch(r"\s*(?:PMID\s*:?\s*)?\d+\s*", adopted["data"]["abstractNote"], re.I):
                    row["anomalies"].append("Zotero abstract contains only an identifier; needs metadata repair")
        except Exception as exc:
            for row in successful:
                row.update(outcome="pending", membership_verified=False)
                row["anomalies"].append("Final collection verification failed: " + str(exc))
    counts = dict(Counter(row["outcome"] for row in mappings))
    adoptions = {}
    for row in mappings:
        if row["membership_verified"]:
            key = row["item"]["item_key"]
            earlier = adoptions.get(key)
            if earlier:
                row["fallback_reason"] = row["fallback_reason"] or earlier["fallback_reason"]
                row["known_duplicates"] = deepcopy(row["known_duplicates"] or earlier["known_duplicates"])
                row["adoption_history"] = deepcopy(earlier["adoption_history"])
        if row["membership_verified"] and not row.get("adoption_history"):
            row["adoption_history"] = [{k: deepcopy(row.get(k)) for k in ("outcome", "item", "fallback_reason", "known_duplicates")}]
        if row["membership_verified"]:
            adoptions.setdefault(row["item"]["item_key"], row)
    if journal:
        for source, row in zip(references, mappings):
            if row["membership_verified"]:
                tokens = write_tokens(source) | {f"{k}:{v}" for k, v in row["identifiers"].items() if v}
                prior = journal.records(tokens)
                if (len(prior) != len(tokens) or any(record["state"] != "verified" or record.get("mapping", {}).get("item") != row["item"]
                                                   for record in prior)):
                    journal.update(tokens, "verified", mapping=deepcopy(row))
    complete = bool(mappings) and all(row["outcome"] in SUCCESS and row["membership_verified"] for row in mappings)
    return {"schema_version": SCHEMA_VERSION, "complete": complete,
            "library": library.json(), "target_collection": collection,
            "counts": counts, "mappings": mappings}


def create_collection(backend, name, parent=None, *, checks=3, delay=0.3):
    if not name.strip():
        raise ValueError("Collection name cannot be empty")
    if parent:
        backend.collection(parent)
    def matches():
        return [c for c in backend.all("/collections") if scoped(c, backend.library)
                and c["data"]["name"] == name.strip() and (c["data"].get("parentCollection") or None) == parent]
    current = matches()
    if len(current) > 1:
        raise Uncertain("Multiple collections have this name and parent")
    if current:
        return {"key": current[0]["key"], "verified": True, "created": False}
    fields = {"libraryID", "name", "parentCollection"}
    if not backend.plugin.supports("create_collection", fields):
        raise Unavailable("No supported collection-creation writer; create/select it in Zotero before importing")
    error = None
    try:
        arguments = {"libraryID": backend.internal_library_id(), "name": name.strip()}
        if parent:
            arguments["parentCollection"] = parent
        backend.plugin.call("create_collection", arguments)
    except PermissionDenied:
        raise
    except Exception as exc:
        error = str(exc)
    for n in range(checks):
        current = matches()
        if len(current) == 1:
            return {"key": current[0]["key"], "verified": True, "created": True, "response_error": error}
        if len(current) > 1:
            break
        if n + 1 < checks:
            time.sleep(delay)
    raise Uncertain("Collection creation not verified; do not retry blindly: " + str(error))


def run_cli(args):
    if args.command == "metadata-from-html":
        import reference_metadata
        text, _ = z.read_source_text(Path(args.file), args.encoding)
        record = reference_metadata.metadata_from_html(text, args.url, kind=args.kind)
        z.dump_json({"records": [record]})
        return
    backend = LocalBackend(Library(args.library_type, args.library_id), Plugin(args.plugin_url))
    if args.command == "write-capabilities":
        denied = backend.plugin.denied
        result = {"local_api": "read_only", "plugin_available": bool(backend.plugin.tools),
                  "add_items": not denied and backend.can_add(),
                  "create_collection": not denied and backend.plugin.supports("create_collection", {"libraryID", "name", "parentCollection"}),
                  "permission_denied": denied, "reason": backend.plugin.reason, "live_write_tested": False}
        target = z.connector_post("/connector/getSelectedCollection", {})
        result["connector_reachable"] = target.ok
        result["connector_read_status"] = target.status
        z.dump_json(result)
        return
    if not args.yes:
        raise ValueError("Use --yes for the requested Zotero write")
    if args.command == "create-collection":
        z.dump_json(create_collection(backend, args.name, args.parent))
        return
    if args.references_json:
        payload = json.loads(Path(args.references_json).read_text(encoding="utf-8-sig"))
        if payload.get("schema_version", "zotero-input.references/1") != "zotero-input.references/1":
            raise ValueError("Unsupported structured reference schema_version")
        refs = normalize_sources(payload["documents"])
    else:
        refs = file_sources(args.source, encoding=args.encoding)
    if not refs:
        raise ValueError("No references extracted; check source format or supply structured references")
    if args.mapping_out:
        output = Path(args.mapping_out).resolve()
        sources = (args.source or [args.references_json]) + ([args.metadata_catalog] if args.metadata_catalog else [])
        if output in {Path(p).resolve() for p in sources}:
            raise ValueError("Mapping output must not overwrite a source file")
        local_state.preflight_output(output, sources=sources)
    lookup = lookup_metadata
    if args.metadata_catalog:
        import reference_metadata
        lookup = reference_metadata.MetadataCatalog(Path(args.metadata_catalog), lookup_metadata, encoding=args.encoding)
    journal = args.journal or local_state.default_import_journal(z.DEFAULT_BASE_URL, backend.library, args.collection_key)
    sources = (args.source or [args.references_json]) + ([args.metadata_catalog] if args.metadata_catalog else [])
    if Path(journal).resolve() in {Path(p).resolve() for p in sources + [args.mapping_out] if p}:
        raise ValueError("Recovery journal must not overwrite source or mapping")
    result = reconcile(refs, backend, args.collection_key, lookup=lookup, journal_path=journal)
    if args.mapping_out:
        local_state.atomic_json(args.mapping_out, result, replace=False)
    z.dump_json({"complete": result["complete"], "counts": result["counts"],
                 "fallbacks": [{"source": r["source"], "reason": r["fallback_reason"]}
                               for r in result["mappings"] if r["fallback_reason"]],
                 "unresolved": [r for r in result["mappings"] if r["outcome"] not in SUCCESS],
                 "warnings": [{"document_id": r["source"]["document_id"],
                               "reference_id": r["source"]["reference_id"], "anomalies": r["anomalies"]}
                              for r in result["mappings"] if r["anomalies"] and r["outcome"] in SUCCESS],
                 "mapping_file": args.mapping_out})
    if not result["complete"]:
        raise SystemExit(2)


def add_commands(subcommands):
    page = subcommands.add_parser("metadata-from-html", help="Extract explicit citation tags from an already retrieved article page")
    page.add_argument("--file", required=True)
    page.add_argument("--url", required=True)
    page.add_argument("--kind", choices=["publisher", "database"], default="publisher")
    page.add_argument("--encoding")
    page.set_defaults(func=run_cli)
    for name in ("import-references", "write-capabilities", "create-collection"):
        cmd = subcommands.add_parser(name)
        cmd.add_argument("--library-type", choices=["user", "group"], default="user")
        cmd.add_argument("--library-id", type=int, default=0)
        cmd.add_argument("--plugin-url", default="http://127.0.0.1:23120/mcp")
        if name != "write-capabilities":
            cmd.add_argument("--yes", action="store_true")
        if name == "import-references":
            inputs = cmd.add_mutually_exclusive_group(required=True)
            inputs.add_argument("--source", action="append")
            inputs.add_argument("--references-json")
            cmd.add_argument("--collection", required=True, dest="collection_key")
            cmd.add_argument("--mapping-out", help="Optional versioned source-to-Zotero JSON output")
            cmd.add_argument("--journal", help="Optional persistent recovery journal; default is a local per-target cache")
            cmd.add_argument("--encoding", help="Optional encoding for TXT/Markdown/CSV/RIS; default UTF-8 then GB18030")
            cmd.add_argument("--metadata-catalog", help="Optional RIS/JSON metadata supplement matched to source references")
        elif name == "create-collection":
            cmd.add_argument("--name", required=True)
            cmd.add_argument("--parent")
        cmd.set_defaults(func=run_cli)
