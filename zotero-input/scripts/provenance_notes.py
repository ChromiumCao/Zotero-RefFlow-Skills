"""Opt-in immutable citation provenance child notes, verified by read-back."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime
import html
from html.parser import HTMLParser
import json
from pathlib import Path
import shutil
import subprocess
import time

import citation_handoff as ch
import ingest as ing
import zotero as z
import local_state

JOB_VERSION = "zotero-input.provenance-notes/1"


def extract_pdf_pages(path, pages, *, approved=False, cost_warning_acknowledged=False):
    if approved is not True or cost_warning_acknowledged is not True:
        raise PermissionError("Consent and token warning required before optional full-text page extraction")
    pages = list(dict.fromkeys(pages))
    if not 1 <= len(pages) <= 5 or any(type(p) is not int or p < 1 for p in pages):
        raise ValueError("Select one to five positive PDF file page indexes per batch")
    import pdf_reader
    path = Path(path).resolve()
    units = []
    for page, text in pdf_reader.read_pages(path, pages):
        units.append({"source_id": f"pdf-page-{page}", "kind": "full_text", "path": str(path), "text": text,
                      "pdf_page_index": page, "printed_page_label": None, "section": None, "locator_verified": True})
    return units


def prepare_notes(bundle, mapping, *, approved=False, cost_warning_acknowledged=False):
    if approved is not True or cost_warning_acknowledged is not True:
        raise PermissionError("Separate notes consent and token-cost warning acknowledgment required")
    ch.check_bundle(bundle)
    if mapping.get("schema_version") != ing.SCHEMA_VERSION or mapping.get("citation_bundle_id") != bundle["bundle_id"]:
        raise ValueError("Mapping does not belong to this reviewed citation bundle")
    rows = {}
    for row in mapping["mappings"]:
        key = (row["source"]["document_id"], row["source"]["reference_id"])
        if key in rows:
            raise ValueError("Duplicate source mapping")
        rows[key] = row
    groups, unresolved, libraries = {}, [], set()
    for doc in bundle["documents"]:
        for cite in doc["citations"]:
            for support in cite["supports"]:
                ref_id = support["reference_id"]
                row = rows.get((doc["document_id"], ref_id))
                if not row or row.get("outcome") not in ing.SUCCESS or not row.get("membership_verified") or not row.get("item"):
                    unresolved.append({"document_id": doc["document_id"], "reference_id": ref_id, "reason": "No verified adopted item"})
                    continue
                item = row["item"]
                if item["library"] != row["target_collection"]["library"]:
                    raise ValueError("Item/collection library mismatch")
                libraries.add((item["library"]["type"], item["library"]["id"]))
                key = (doc["document_id"], doc["text_hash"], *sorted(item["library"].items()), item["item_key"])
                group = groups.setdefault(key, {"item": deepcopy(item), "target_collection": deepcopy(row["target_collection"]),
                                              "document_id": doc["document_id"], "document_title": doc["title"],
                                              "document_hash": doc["text_hash"], "recorded_at": bundle["prepared_at"],
                                              "parent_metadata": deepcopy(bundle["references"][ref_id]["metadata"]), "usages": []})
                usage = {"citation_id": cite["citation_id"], "sentence": cite["sentence"], "start": cite["start"], "end": cite["end"],
                         "marker": cite["marker"], "quote": support["quote"], "grade": support["grade"],
                         "assessment": support["assessment"], "source": {k: deepcopy(v) for k, v in support["source"].items() if k != "text"}}
                if usage not in group["usages"]:
                    group["usages"].append(usage)
    if len(libraries) > 1:
        raise ValueError("A note job must target a single selected library")
    notes = []
    for group in groups.values():
        stable = {k: v for k, v in group.items() if k != "recorded_at"}
        group["note_id"] = ch.digest(stable)
        group["marker"] = "zotero-input-provenance/v1:" + group["note_id"]
        paragraphs = ["Citation provenance snapshot", group["marker"], "Document: " + str(group["document_title"] or "Name not supplied"),
                      "Document ID: " + group["document_id"], "Document SHA-256: " + group["document_hash"],
                      "Recorded: " + group["recorded_at"], "Snapshot only; later manuscript edits are not tracked."]
        for usage in group["usages"]:
            source = usage["source"]
            locator = ["Section: " + str(source.get("section") or "Not available"),
                       "PDF file page index: " + str(source.get("pdf_page_index") or "Not available"),
                       "Printed page label: " + str(source.get("printed_page_label") or "Not available")]
            paragraphs += ["Citation: " + str(usage["citation_id"]) + " " + usage["marker"],
                           f'Original sentence ({usage["start"]}:{usage["end"]}): ' + usage["sentence"],
                           "Source quotation: " + usage["quote"], "Evidence kind: " + source["kind"],
                           "Source: " + str(source.get("url") or source.get("path")),
                           "Source content SHA-256: " + source["content_hash"], *locator,
                           "Support: " + usage["grade"] + "; " + usage["assessment"]]
        group["html"] = "\n".join("<p>" + html.escape(p, quote=True) + "</p>" for p in paragraphs)
        group["expected_text"] = ch.normalized_text(" ".join(paragraphs))
        group["semantic_text"] = semantic_note_text(group["expected_text"], group)
        notes.append(group)
    job = {"schema_version": JOB_VERSION, "citation_bundle_id": bundle["bundle_id"],
           "consent": {"notes": True, "token_cost_warning_acknowledged": True},
           "notes": notes, "unresolved": unresolved, "complete": bool(notes) and not unresolved}
    job["integrity"] = ch.digest(job)
    return job


def check_job(job):
    if job.get("schema_version") != JOB_VERSION or job.get("integrity") != ch.digest({k: v for k, v in job.items() if k != "integrity"}):
        raise ValueError("Note job changed; regenerate after reviewing the mapping")
    consent = job.get("consent", {})
    if consent.get("notes") is not True or consent.get("token_cost_warning_acknowledged") is not True:
        raise PermissionError("Notes consent/token warning missing")


def note_text(value):
    blocks = {"p", "div", "br", "li", "blockquote", "ul", "ol", "table", "tr", "td", "th",
              "h1", "h2", "h3", "h4", "h5", "h6", "pre", "hr", "section", "article"}
    class NoteText(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.parts = []
        def handle_data(self, data):
            self.parts.append(data)
        def handle_starttag(self, tag, attrs):
            if tag in blocks:
                self.parts.append(" ")
        def handle_endtag(self, tag):
            if tag in blocks:
                self.parts.append(" ")
    parser = NoteText()
    parser.feed(value)
    return ch.normalized_text("".join(parser.parts))


def semantic_note_text(text, note):
    """Ignore only the well-formed timestamp in the generated snapshot header."""
    prefix = ch.normalized_text(" ".join([
        "Citation provenance snapshot", note["marker"],
        "Document: " + str(note["document_title"] or "Name not supplied"),
        "Document ID: " + note["document_id"], "Document SHA-256: " + note["document_hash"]]))
    separator = " Recorded: "
    if not text.startswith(prefix + separator):
        return text
    remainder = text[len(prefix + separator):]
    timestamp, space, tail = remainder.partition(" ")
    try:
        parsed = datetime.fromisoformat(timestamp)
        if parsed.tzinfo is None:
            return text
    except ValueError:
        return text
    if not space or not tail.startswith("Snapshot only; later manuscript edits are not tracked."):
        return text
    return prefix + " " + tail


def matching_note(note, rows):
    matches = []
    library = ing.Library(note["item"]["library"]["type"], note["item"]["library"]["id"])
    for row in rows:
        data = row.get("data", {})
        if data.get("deleted") or data.get("itemType") != "note" or not ing.scoped(row, library):
            continue
        if data.get("parentItem") != note["item"]["item_key"]:
            continue
        content = data.get("note") or ""
        if note["marker"] not in content:
            continue
        if semantic_note_text(note_text(content), note) != semantic_note_text(note["expected_text"], note):
            raise ing.Uncertain("Note marker exists but content differs; do not overwrite")
        matches.append(row)
    if len(matches) > 1:
        raise ing.Uncertain("Multiple provenance notes; do not create another")
    return matches[0] if matches else None


def verify_parent(note, backend):
    parent = backend.parent(note)
    wanted_library = ing.Library(note["item"]["library"]["type"], note["item"]["library"]["id"])
    if parent.get("key") != note["item"]["item_key"] or not ing.scoped(parent, wanted_library):
        raise ing.Uncertain("Parent library/item identity changed")
    data = parent.get("data", {})
    if data.get("deleted") or data.get("itemType") in {"note", "attachment", "annotation"}:
        raise ing.Uncertain("A regular reference parent is required")
    if note["target_collection"]["key"] not in data.get("collections", []):
        raise ing.Uncertain("Parent is no longer in the requested collection")
    candidate = {**parent, "library": wanted_library.json()}
    if not ing.match_reference(note["parent_metadata"], [candidate], wanted_library)[0]:
        raise ing.Uncertain("Mapped parent metadata does not agree with the reviewed reference")


def summarize(rows, job):
    return {"schema_version": "zotero-input.note-results/1", "citation_bundle_id": job["citation_bundle_id"],
            "complete": bool(rows) and not job["unresolved"] and all(r["membership_verified"] for r in rows),
            "counts": dict(Counter(r["outcome"] for r in rows)), "notes": rows, "unresolved": deepcopy(job["unresolved"])}


def verify_notes(job, backend):
    check_job(job)
    rows = []
    for note in job["notes"]:
        row = {"note_id": note["note_id"], "item": note["item"], "note_key": None,
               "outcome": "pending", "membership_verified": False}
        try:
            verify_parent(note, backend)
            found = matching_note(note, backend.children(note))
            if found:
                row.update(note_key=found["key"], outcome="verified", membership_verified=True)
        except Exception as exc:
            row["reason"] = str(exc)
            if isinstance(exc, ing.PermissionDenied):
                row["outcome"] = "failed"
        rows.append(row)
    return summarize(rows, job)


def write_notes(job, backend, *, checks=3, delay=0.3, journal_path=None):
    if journal_path:
        local_state.preflight_output(journal_path, replace=True)
        with local_state.exclusive_lock(journal_path):
            return _write_notes(job, backend, checks=checks, delay=delay, journal_path=journal_path)
    return _write_notes(job, backend, checks=checks, delay=delay)


def _write_notes(job, backend, *, checks=3, delay=0.3, journal_path=None):
    check_job(job)
    if not 1 <= checks <= 5:
        raise ValueError("Bounded verification checks required")
    journal = json.loads(Path(journal_path).read_text(encoding="utf-8")) if journal_path and Path(journal_path).exists() else {}
    if not isinstance(journal, dict) or any(not isinstance(k, str) or v not in {"started", "uncertain", "verified"}
                                           for k, v in journal.items()):
        raise ValueError("Invalid note journal; no writes allowed")
    def checkpoint():
        if journal_path:
            local_state.atomic_json(journal_path, journal)
    rows = []
    for note in job["notes"]:
        row = {"note_id": note["note_id"], "item": note["item"], "note_key": None,
               "outcome": "pending", "membership_verified": False}
        try:
            verify_parent(note, backend)
            found = matching_note(note, backend.children(note))
            if found:
                row.update(outcome="existing", note_key=found["key"], membership_verified=True)
                journal[note["note_id"]] = "verified"
                checkpoint()
            elif journal.get(note["note_id"]) in {"started", "uncertain", "verified"}:
                raise ing.Uncertain("Earlier attempt not visible; read-only verification required, no automatic rewrite")
            elif not backend.can_create():
                row.update(outcome="pending", reason="No supported note writer; use approved native Zotero script and verify")
            else:
                journal[note["note_id"]] = "started"
                checkpoint()
                response_error = None
                try:
                    backend.create(note)
                except Exception as exc:
                    response_error = exc
                for attempt in range(checks):
                    verify_parent(note, backend)
                    found = matching_note(note, backend.children(note))
                    if found:
                        break
                    if attempt + 1 < checks:
                        time.sleep(delay)
                if found:
                    row.update(outcome="created", note_key=found["key"], membership_verified=True)
                    journal[note["note_id"]] = "verified"
                    if response_error:
                        row["warning"] = "Response abnormal; actual child note content verified: " + str(response_error)
                else:
                    journal[note["note_id"]] = "uncertain"
                    if isinstance(response_error, ing.PermissionDenied):
                        raise response_error
                    raise ing.Uncertain("Note write not verified; do not retry or switch writers: " + str(response_error))
                checkpoint()
        except Exception as exc:
            row.update(outcome="failed" if isinstance(exc, ing.PermissionDenied) else "pending", reason=str(exc))
            if journal.get(note["note_id"]) == "started":
                journal[note["note_id"]] = "uncertain"
                checkpoint()
        rows.append(row)
    # Final read-back revokes provisional success if the parent/content changed.
    final = verify_notes(job, backend)
    for row, checked in zip(rows, final["notes"]):
        if row["membership_verified"] and not checked["membership_verified"]:
            row.update(outcome="pending", membership_verified=False, reason="Final note read-back did not verify content/parent")
    return summarize(rows, job)


class NoWriter:
    def supports(self, *args):
        return False


class NoteBackend:
    def __init__(self, job, *, plugin_url="http://127.0.0.1:23120/mcp", discover_writer=True):
        check_job(job)
        if not job["notes"]:
            raise ValueError("No verified reference parents for notes")
        identities = {(n["item"]["library"]["type"], n["item"]["library"]["id"]) for n in job["notes"]}
        if len(identities) != 1:
            raise ValueError("Mixed library note job")
        self.local = ing.LocalBackend(ing.Library(*next(iter(identities))), ing.Plugin(plugin_url) if discover_writer else NoWriter())

    def parent(self, note):
        return self.local.get_item(note["item"]["item_key"])

    def children(self, note):
        return self.local.all("/items/" + z.urllib.parse.quote(note["item"]["item_key"], safe="") + "/children")

    def can_create(self):
        fields = {"libraryID", "action", "parentKey", "content"}
        if not self.local.plugin.supports("write_note", fields):
            return False
        schema = self.local.plugin.tools["write_note"]["inputSchema"]
        actions = schema["properties"]["action"].get("enum")
        return actions is None or "create" in actions

    def create(self, note):
        if not self.can_create():
            raise ing.Unavailable("Verified write_note schema is unavailable")
        return self.local.plugin.call("write_note", {"libraryID": self.local.internal_library_id(), "action": "create",
                                                   "parentKey": note["item"]["item_key"], "content": note["html"]})


def native_script(job):
    check_job(job)
    template = Path(__file__).with_name("zotero_notes.js").read_text(encoding="utf-8")
    return template.replace("__JOB_JSON__", json.dumps(job, ensure_ascii=True))


def write_job(job, directory, *, bundle=None, mapping=None):
    check_job(job)
    import provenance_markdown
    if (bundle is None) != (mapping is None):
        raise ValueError("Supply both reviewed bundle and mapping for complete Markdown evidence")
    if bundle is not None and bundle.get("bundle_id") != job["citation_bundle_id"]:
        raise ValueError("Markdown evidence must belong to the note job")
    if bundle is not None:
        expected = prepare_notes(bundle, mapping, approved=True, cost_warning_acknowledged=True)
        if expected["integrity"] != job["integrity"]:
            raise ValueError("Markdown mapping and note job must adopt the same references and evidence")
    markdown = provenance_markdown.from_bundle(bundle, mapping) if bundle is not None else provenance_markdown.from_job(job)
    directory = Path(directory)
    paths = [directory / "notes-job.json", directory / "zotero-add-provenance-notes.js", directory / "notes-preview.html",
             directory / "citation-provenance.md"]
    if any(p.exists() for p in paths):
        raise FileExistsError("Use a fresh notes output directory")
    directory.mkdir(parents=True, exist_ok=True)
    paths[0].write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    paths[1].write_text(native_script(job), encoding="utf-8")
    body = "\n<hr>\n".join(n["html"] for n in job["notes"])
    paths[2].write_text('<!doctype html><html><head><meta charset="utf-8"><title>Citation provenance</title></head><body>' + body + '</body></html>', encoding="utf-8")
    local_state.atomic_text(paths[3], markdown, replace=False)
    return paths
