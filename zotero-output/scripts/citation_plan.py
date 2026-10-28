"""Join source occurrences to verified destination items, without library mutation."""
from __future__ import annotations
import copy
import json
import re
import urllib.parse
import urllib.request
from common import OutputError, identity, normalized, validate_sources, validate_uri, doi

SUCCESS = {"target_existing", "reused_added", "new_import", "fallback_import"}

def compatible(a, b):
    for key in ("doi", "pmid"):
        if a.get(key) and b.get(key) and str(a[key]).casefold() != str(b[key]).casefold(): return False
    for key in ("title", "first_author", "year"):
        if a.get(key) and b.get(key) and normalized(a[key]) != normalized(b[key]): return False
    return True

def make_plan(scan, mapping, style, mode="manuscript"):
    if scan.get("schema_version") != "zotero-output.scan/1": raise OutputError("Unsupported scan schema")
    schema = mapping.get("schema_version")
    if schema not in {"zotero-input.mapping/1", "zotero-output.mapping/1"}: raise OutputError("Unsupported mapping schema")
    scope = mapping.get("scope", "collection")
    if scope not in {"collection", "library"}: raise OutputError("Invalid mapping scope")
    if scope == "library" and (schema != "zotero-output.mapping/1" or mapping.get("collection_policy") != "none"):
        raise OutputError("Library scope requires an explicit no-collection Output mapping")
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", style): raise OutputError("Specify the exact CSL style slug")
    validate_sources(scan["documents"])
    expected = {(d["document_id"], r["reference_id"]) for d in scan["documents"] for r in d["references"]}
    bindings, aliases, rows, issues = {}, {}, {}, []
    for row in mapping["mappings"]:
        source = row["source"]
        pair = (source["document_id"], source["reference_id"])
        if pair not in expected: raise OutputError("Mapping contains an unknown source occurrence")
        if pair in rows: raise OutputError("Duplicate mapping source occurrence")
        rows[pair] = row
    if set(rows) != expected: raise OutputError("Mapping does not cover every source reference")
    canonical = []
    for doc in scan["documents"]:
        for ref in doc["references"]:
            pair = (doc["document_id"], ref["reference_id"])
            row = rows[pair]
            verified = row.get("library_verified") if scope == "library" else row.get("membership_verified")
            allowed = {"library_existing"} if scope == "library" else SUCCESS
            if row.get("outcome") not in allowed or not verified or not row.get("item"):
                issues.append({"source": list(pair), "issue": "Reference identity/membership is not verified"})
                continue
            evidence = row.get("identifiers", {})
            if not compatible(ref, evidence): raise OutputError("Source/mapping metadata conflict: " + str(pair))
            item = row["item"]
            target = row.get("target_collection") or mapping.get("target_collection")
            if scope == "library" and target is not None: raise OutputError("No-collection mapping has a collection")
            if scope == "collection" and (not target or not target.get("key", target.get("collection_key"))):
                raise OutputError("Mapping lacks target collection identity")
            if target and (item["library"]["type"] != target["library"]["type"] or item["library"]["id"] != target["library"]["id"]):
                raise OutputError("Item and target collection are in different libraries")
            key = identity(item)
            ids = {k: evidence.get(k) or ref.get(k) for k in ("doi", "pmid", "title", "first_author", "year")}
            chosen = key
            for previous in canonical:
                shared = any(ids.get(k) and previous["identifiers"].get(k) == ids[k] for k in ("doi", "pmid"))
                exact = (ids.get("title") and ids.get("first_author") and ids.get("year")
                         and all(normalized(ids[k]) == normalized(previous["identifiers"].get(k)) for k in ("title", "first_author", "year")))
                if previous["key"] == key or ((shared or exact) and previous["item"]["library"] == item["library"]):
                    if not compatible(ids, previous["identifiers"]): raise OutputError("Conflicting records cannot be deduplicated")
                    chosen = previous["key"]
                    break
            if chosen == key and not any(p["key"] == key for p in canonical):
                canonical.append({"key": key, "item": item, "identifiers": ids, "target_collection": target})
            bindings["\n".join(pair)] = chosen
            aliases[key] = chosen
    documents = copy.deepcopy(scan["documents"])
    for doc in documents:
        for c in doc["citations"]:
            c["item_keys"] = [bindings.get(doc["document_id"]+"\n"+r["reference_id"]) for r in c["references"]]
            c["action"] = "replace"
    return {"schema_version": "zotero-output.plan/1", "style": style, "locale": "en-US", "mode": mode,
            "scope": scope, "collection_policy": mapping.get("collection_policy", "collection"),
            "documents": documents, "items": canonical, "source_bindings": bindings, "item_aliases": aliases,
            "mapping_issues": issues, "preserve_uncited": mode == "bibliography", "reviewed": False}

def validate_plan(plan, out=None):
    if plan.get("schema_version") != "zotero-output.plan/1": raise OutputError("Unsupported plan schema")
    if plan.get("mode") not in ("manuscript", "bibliography"): raise OutputError("Invalid plan mode")
    if plan.get("scope", "collection") not in {"collection", "library"}: raise OutputError("Invalid plan scope")
    if plan.get("citation_mode") == "static" and plan.get("style") != "source-text": raise OutputError("Static mode uses source-text formatting")
    if plan.get("scope", "collection") == "library" and plan.get("collection_policy") != "none":
        raise OutputError("Library scope requires explicit no-collection choice")
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*",str(plan.get("style",""))): raise OutputError("Invalid CSL style slug")
    validate_sources(plan["documents"], out)
    if plan.get("mapping_issues"): raise OutputError("Resolve incomplete input mappings before build")
    if not plan.get("reviewed"): raise OutputError("Review source relationships and set plan.reviewed=true")
    items = {r["key"]: r for r in plan["items"]}
    if len(items) != len(plan["items"]): raise OutputError("Duplicate canonical item")
    total = 0
    for doc in plan["documents"]:
        if doc.get("issues") and not doc.get("issues_reviewed"):
            raise OutputError("Unreviewed source scan issues: " + doc["path"])
        for r in doc["bibliography_ranges"]:
            if not r.get("reviewed"): raise OutputError("Review every bibliography removal range")
            if r["start"] < 0 or r["end"] <= r["start"]: raise OutputError("Invalid bibliography range")
        spans = {}
        for c in doc["citations"]:
            if plan["mode"] == "bibliography": continue
            if not c.get("reviewed"): raise OutputError("Unreviewed citation " + c["citation_id"])
            if c.get("action") == "ignore":
                if not c.get("reason"): raise OutputError("Ignored candidate requires a reason")
                if c.get("field"): raise OutputError("Cannot ignore a native citation field in a relinking output")
                continue
            if c.get("action") != "replace" or c.get("issue"):
                raise OutputError("Unresolved citation " + c["citation_id"])
            keys = c.get("item_keys", [])
            if not keys or any(k not in items for k in keys): raise OutputError("Citation has no complete verified binding")
            if len(keys) != len(c["references"]): raise OutputError("Cluster binding length mismatch")
            for key, ref in zip(keys,c["references"]):
                if plan["source_bindings"].get(doc["document_id"]+"\n"+ref["reference_id"])!=key:
                    raise OutputError("Citation no longer agrees with its verified source mapping")
            row = next((p for p in doc["paragraphs"] if p["part"] == c["part"] and p["paragraph"] == c["paragraph"]), None)
            if not row or row["text"][c["start"]:c["end"]] != c["text"]:
                raise OutputError("Citation anchor mismatch")
            if any(r["part"] == c["part"] and r["start"] <= c["paragraph"] < r["end"] for r in doc["bibliography_ranges"]):
                raise OutputError("Body citation overlaps bibliography removal range")
            locator = (c["part"], c["paragraph"])
            for a,b in spans.setdefault(locator, []):
                if max(a,c["start"]) < min(b,c["end"]): raise OutputError("Overlapping citation spans")
            spans[locator].append((c["start"],c["end"]))
            total += 1
    return total

def request_json(path, base="http://127.0.0.1:23119"):
    parsed = urllib.parse.urlparse(base)
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise OutputError("Only Zotero's local read-only API is supported by this helper")
    with urllib.request.urlopen(base.rstrip("/")+path, timeout=20) as response:
        return json.load(response)

def fetch_items(plan, base="http://127.0.0.1:23119"):
    if plan.get("citation_mode") == "static":
        return {"schema_version":"zotero-output.items/1", "citation_mode":"static", "records":[], "source":"source text; no library reads"}
    records = []
    for target in plan["items"]:
        item = target["item"]
        lib = item["library"]
        namespace = "users" if lib["type"] == "user" else "groups"
        prefix = f"/api/{namespace}/{lib['id']}"
        raw = request_json(prefix+"/items/"+item["item_key"], base)
        data = raw.get("data", {})
        if raw.get("key") != item["item_key"] or data.get("deleted") or data.get("itemType") in ("attachment", "note", "annotation"):
            raise OutputError("Destination is missing, deleted or not a bibliographic parent")
        actual = raw.get("library", {})
        if actual.get("type") != lib["type"] or (int(lib["id"]) != 0 and actual.get("id") != lib["id"]):
            raise OutputError("Destination library identity changed")
        scope = plan.get("scope", "collection")
        destination = target.get("target_collection")
        if scope == "library":
            if plan.get("collection_policy") != "none" or destination is not None: raise OutputError("Invalid no-collection plan")
        else:
            if not destination: raise OutputError("Missing target collection")
            collection = destination.get("key", destination.get("collection_key"))
            if collection not in data.get("collections", []): raise OutputError("Destination collection membership no longer holds")
        result = request_json(prefix+"/items/"+item["item_key"]+"?format=csljson", base)
        if not isinstance(result, list) or len(result) != 1: raise OutputError("Expected one CSL item")
        csl = result[0]
        candidates = [raw.get("uri"), data.get("uri"), csl.get("id")]
        uri = None
        for candidate in candidates:
            try: uri = validate_uri(candidate, item); break
            except OutputError: pass
        if uri is None:
            # A public library ID from this item's own API envelope is authoritative.
            public_id = actual.get("id")
            if isinstance(public_id, int) and public_id > 0:
                uri = validate_uri(f"http://zotero.org/{namespace}/{public_id}/items/{item['item_key']}",
                                   {**item, "library": {**lib, "id": public_id}})
            else: raise OutputError("Local user/0 item lacks an authoritative URI; obtain a Zotero export with URI")
        csl["id"] = uri
        records.append({"key": target["key"], "item": item, "uri": uri, "csl": csl,
                        "scope": scope, "library_verified": True,
                        "membership_verified": scope == "collection", "target_collection": destination})
    return {"schema_version": "zotero-output.items/1", "source": "Zotero local read-only API", "records": records}

def item_index(plan, bundle):
    if plan.get("citation_mode") == "static": return static_records(plan)
    if bundle.get("schema_version") != "zotero-output.items/1": raise OutputError("Unsupported items schema")
    rows = {r["key"]: r for r in bundle["records"]}
    if len(rows) != len(bundle["records"]): raise OutputError("Repeated item record")
    for target in plan["items"]:
        row = rows.get(target["key"])
        scope = plan.get("scope", "collection")
        if scope == "library":
            verified = row and row.get("scope") == "library" and row.get("library_verified") and row.get("target_collection") is None
        else:
            verified = row and row.get("membership_verified") and row.get("target_collection") == target.get("target_collection")
        if not verified: raise OutputError("Missing verified destination record")
        if identity(row["item"]) != target["key"]: raise OutputError("Item record identity mismatch")
        validate_uri(row["uri"], row["item"])
        if not row.get("csl", {}).get("title") or not row["csl"].get("type"):
            raise OutputError("CSL title/type missing")
        evidence = {"title": row["csl"].get("title"), "doi": doi(row["csl"].get("DOI")), "pmid": row["csl"].get("PMID")}
        if not compatible(target["identifiers"], evidence): raise OutputError("Destination metadata conflicts with mapped source")
    return rows


def make_static_plan(scan, mode="manuscript", style="source-text"):
    """Plain numbered output from reviewed source references; no Zotero identity."""
    if scan.get("schema_version") != "zotero-output.scan/1": raise OutputError("Unsupported scan schema")
    if style != "source-text": raise OutputError("Static mode currently supports source-text numbered formatting; named CSL styles require an available renderer")
    validate_sources(scan["documents"])
    documents = copy.deepcopy(scan["documents"]); items = []; bindings = {}
    for doc in documents:
        for ref in doc["references"]:
            ids = {k: ref.get(k) for k in ("doi", "pmid", "title", "first_author", "year")}
            selected = None
            for previous in items:
                shared = any(ids.get(k) and ids[k] == previous["identifiers"].get(k) for k in ("doi", "pmid"))
                exact = normalized(ref.get("raw_text")) and normalized(ref.get("raw_text")) == normalized(previous["raw_text"])
                if shared or exact:
                    if not compatible(ids, previous["identifiers"]): raise OutputError("Conflicting static references")
                    selected = previous; break
            if selected is None:
                raw = ref.get("raw_text") or ""
                if ref.get("csl"):
                    csl = ref["csl"]
                    authors = "; ".join(a.get("literal") or " ".join(str(a.get(k) or "") for k in ("given","family")).strip() for a in csl.get("author",[]))
                    raw = ". ".join(str(v) for v in (authors, csl.get("title"), csl.get("container-title"),
                                                     ref.get("year"), csl.get("volume"), csl.get("issue"), csl.get("page")) if v)
                    if ref.get("doi"): raw += ". doi:"+ref["doi"]
                if not raw.strip(): raise OutputError("Static bibliography entry has no readable source text")
                selected = {"key": f"static:{len(items)+1}", "identifiers": ids,
                            "raw_text": ref.get("raw_text"), "static_text": " ".join(raw.split())}
                items.append(selected)
            bindings[doc["document_id"]+"\n"+ref["reference_id"]] = selected["key"]
        for c in doc["citations"]:
            c["action"] = "replace"
            c["item_keys"] = [bindings[doc["document_id"]+"\n"+r["reference_id"]] for r in c["references"]]
    return {"schema_version": "zotero-output.plan/1", "citation_mode": "static", "style": style,
            "locale": "en-US", "mode": mode, "documents": documents, "items": items,
            "source_bindings": bindings, "item_aliases": {}, "mapping_issues": [],
            "preserve_uncited": mode == "bibliography", "reviewed": False}


def static_records(plan):
    order = list(dict.fromkeys(k for d in plan["documents"] for c in sorted(d["citations"], key=lambda c:(c["paragraph"],c["start"]))
                              if c.get("action") == "replace" for k in c["item_keys"]))
    order += [i["key"] for i in plan["items"] if i["key"] not in order]
    by_key = {i["key"]: i for i in plan["items"]}
    return {key: {**by_key[key], "number": n} for n, key in enumerate(order, 1)}
