"""Read-only Output mapping adapter; no dependency on Zotero Input or writers."""
from __future__ import annotations
import re
import urllib.error
from common import OutputError, URI, normalized, doi, validate_sources
from citation_plan import compatible, fetch_items, request_json


def collection_decision(choice=None, *, question_asked=False, elapsed_seconds=0, key=None):
    """Routing only, never a writer or a simulated interactive timer."""
    if choice == "none": return "none"
    if choice is None and key: return "existing"
    if choice == "existing": return "existing" if key else "clarify"
    if choice == "new": return "new"
    if choice is not None: raise OutputError("Unknown collection choice")
    if not question_asked: return "ask"
    return "new" if elapsed_seconds >= 60 else "wait"


def _ids(raw):
    data = raw.get("data", raw)
    creators = data.get("creators", [])
    first = next((c.get("lastName") or c.get("name") for c in creators if c.get("creatorType", "author") == "author"), None)
    year = re.search(r"\b(?:19|20)\d{2}\b", str(data.get("date") or ""))
    pmid = re.search(r"(?:PMID|PubMed ID)\s*:\s*(\d+)", str(data.get("extra") or ""), re.I)
    return {"doi": doi(data.get("DOI")), "pmid": data.get("PMID") or (pmid[1] if pmid else None),
            "title": data.get("title"), "first_author": first, "year": year[0] if year else None}


def resolve_library(scan, *, no_collection=False, collection=None, library_type="user", library_id=0,
                    base="http://127.0.0.1:23119"):
    if scan.get("schema_version") != "zotero-output.scan/1": raise OutputError("Unsupported scan schema")
    validate_sources(scan["documents"])
    if no_collection == bool(collection): raise OutputError("Choose --no-collection or an exact --collection; unspecified follows the skill's 60-second policy")
    if collection and not re.fullmatch(r"[A-Z0-9]{8}", collection): raise OutputError("Use a resolved Zotero collection key")
    if library_type not in {"user", "group"} or library_id < 0 or (library_type == "group" and library_id == 0):
        raise OutputError("Specify a valid public library identity")
    lib = {"type": library_type, "id": library_id}
    if library_type == "user" and library_id == 0: lib["id_kind"] = "local_user_alias"
    scope = "library" if no_collection else "collection"
    prefix = f"/api/{'users' if library_type == 'user' else 'groups'}/{library_id}"
    target = None if no_collection else {"library": lib, "key": collection}
    if target: request_json(prefix+"/collections/"+collection, base)
    inventory = []; offset = 0
    while True:
        page = request_json(prefix+f"/items/top?limit=100&start={offset}", base)
        if not isinstance(page, list): raise OutputError("Unreadable library inventory")
        inventory.extend(page)
        if len(page) < 100: break
        offset += 100
    inventory = [r for r in inventory if not r.get("data", {}).get("deleted")
                 and r.get("data", {}).get("itemType") not in {"attachment", "note", "annotation"}]
    rows = []
    for doc in scan["documents"]:
        for ref in doc["references"]:
            row = {"source": {"document_id": doc["document_id"], "reference_id": ref["reference_id"],
                              "raw_text": ref.get("raw_text"), "source_location": ref.get("source_location")},
                   "identifiers": {k: ref.get(k) for k in ("doi", "pmid", "title", "first_author", "year")},
                   "outcome": "pending", "item": None, "target_collection": target,
                   "library_verified": False, "membership_verified": False, "match": {}}
            rows.append(row)
            native = URI.fullmatch(str(ref.get("foreign_uri", "")))
            matches = []; conflicts = []
            for candidate in inventory:
                ids = _ids(candidate)
                shared = any(ref.get(k) and str(ref[k]).casefold() == str(ids.get(k)).casefold() for k in ("doi", "pmid"))
                shared |= bool(ref.get("title") and normalized(ref["title"]) == normalized(ids.get("title")))
                native_key = bool(native and native[2] == candidate.get("key"))
                if not (shared or native_key): continue
                if not compatible(ref, ids):
                    if shared or native_key: conflicts.append(candidate["key"])
                    continue
                matches.append((candidate, ids, native_key, shared))
            # Verify a native URI against this profile's fresh authoritative URI,
            # never a numeric local ID or a same-key item from another profile.
            verified = []
            for candidate, ids, native_key, shared in matches:
                item = {"library": lib, "item_key": candidate["key"]}
                probe = {"scope": "library", "collection_policy": "none", "items": [
                    {"key": f"{library_type}:{library_id}:{candidate['key']}", "item": item, "target_collection": None}]}
                try: record = fetch_items(probe, base)["records"][0]
                except (OutputError, urllib.error.URLError) as exc:
                    row["match"]["read_error"] = str(exc); continue
                authoritative = URI.fullmatch(record["uri"])
                csl = record["csl"]
                authors = csl.get("author", []); date = csl.get("issued", {}).get("date-parts", [[]])
                fresh_ids = {"doi":doi(csl.get("DOI")),"title":csl.get("title"),"pmid":csl.get("PMID"),
                             "first_author":authors[0].get("family") if authors else None,
                             "year":str(date[0][0]) if date and date[0] else None}
                if not compatible(ref, fresh_ids) or not compatible(ids, fresh_ids):
                    conflicts.append(candidate["key"]); continue
                uri_agrees = native_key and authoritative and (native[1], native[2]) == (authoritative[1], authoritative[2])
                if shared or uri_agrees: verified.append((candidate, ids, record, bool(uri_agrees)))
            native_matches = [v for v in verified if v[3]]
            if len(native_matches) == 1:
                verified = native_matches; conflicts = []
            if conflicts or len(verified) != 1 or row["match"].get("read_error"):
                row["match"].update(reason="conflict_or_ambiguous" if conflicts or len(verified)>1 else "unresolved_identity",
                                    candidates=[v[0]["key"] for v in verified], conflicts=conflicts)
                continue
            candidate, ids, record, _ = verified[0]
            if target and collection not in candidate.get("data", {}).get("collections", []):
                row["match"]["reason"] = "not_in_collection; optional Input collaboration required"
                continue
            # A second read verifies requested membership, not just the inventory.
            if target:
                record = fetch_items({"scope": "collection", "items": [
                    {"key": record["key"], "item": record["item"], "target_collection": target}]}, base)["records"][0]
            row.update(item={**record["item"], "uri": record["uri"]}, identifiers=ids,
                       outcome="library_existing" if no_collection else "target_existing",
                       library_verified=True, membership_verified=not no_collection)
            row["match"]["basis"] = "verified_library_identity"
    return {"schema_version": "zotero-output.mapping/1", "scope": scope,
            "collection_policy": "none" if no_collection else "collection", "library": lib,
            "target_collection": target, "complete": all(r["item"] for r in rows),
            "mappings": rows, "writes": 0}
