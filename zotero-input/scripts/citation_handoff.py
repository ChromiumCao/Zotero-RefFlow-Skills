"""Evidence-reviewed citation insertion with independently consented Zotero actions."""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import sys
import unicodedata
from urllib.parse import quote as urlquote

import ingest as ing
import reference_metadata as rm
import zotero as z
import local_state

REVIEW_VERSION = "zotero-input.citation-review/1"
BUNDLE_VERSION = "zotero-input.citation-bundle/1"


def digest(value):
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def normalized_text(text):
    return " ".join(unicodedata.normalize("NFKC", text).split())


def load_search():
    name = "zotero_input_bundled_search"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).parent / "_vendor" / "nature_citation.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def reference_text(entry):
    names = [", ".join(filter(None, [c.get("lastName"), c.get("firstName")])) for c in entry["creators"]]
    values = ["; ".join(names), entry["title"], entry.get("journal"), entry.get("year"),
              entry.get("volume"), entry.get("issue"), entry.get("pages")]
    result = ". ".join(str(v) for v in values if v)
    if entry.get("doi"):
        result += ". https://doi.org/" + entry["doi"]
    elif entry.get("pmid"):
        result += ". PMID: " + entry["pmid"]
    elif entry.get("url"):
        result += ". " + entry["url"]
    return result


def review_template(text, candidates, *, document_id=None, title=None):
    """Move full candidate metadata into a local draft, never mark evidence reviewed."""
    refs, identities = [], {}
    def candidate_key(record):
        return record.get("doi") or record.get("pmid") or digest([record.get("title"), record.get("year")])
    for number, candidate in enumerate(candidates.get("references", []), 1):
        ref_id = f"R{number:05}"
        creators = []
        for name in candidate.get("authors", []):
            family, separator, given = name.partition(",")
            creators.append({"lastName": family.strip(), "firstName": given.strip() if separator else ""})
        if candidate.get("source_catalog") == "PubMed" and candidate.get("pmid"):
            url = "https://pubmed.ncbi.nlm.nih.gov/" + str(candidate["pmid"]) + "/"
        elif candidate.get("doi"):
            url = "https://api.crossref.org/works/" + urlquote(candidate["doi"], safe="")
        else:
            url = candidate.get("url")
        metadata = {k: candidate.get(k) for k in ("doi", "pmid", "title", "journal", "year", "volume", "issue", "abstract")}
        metadata.update(creators=creators, url=candidate.get("url") or url,
                        pages="-".join(str(candidate[k]) for k in ("start_page", "end_page") if candidate.get(k)))
        units = [{"source_id": "abstract", "kind": "abstract", "url": url, "text": candidate["abstract"]}] if candidate.get("abstract") else []
        record = {"reference_id": ref_id, "metadata": metadata, "metadata_provenance": {"kind": "database", "url": url},
                  "source_units": units, "candidate_warnings": deepcopy(candidate.get("author_warnings", []))}
        key = candidate_key(candidate)
        if key in identities:
            raise ValueError("Candidate identity is duplicated; deduplicate or review before generating template")
        identities[key] = ref_id
        refs.append(record)
    citations = []
    for n, segment in enumerate(candidates.get("segments", []), 1):
        sentence = segment.get("text", "")
        hits = [m.start() for m in re.finditer(re.escape(sentence), text)] if sentence else []
        supports = [{"reference_id": identities[candidate_key(record)], "source_id": "abstract", "quote": "",
                     "grade": "metadata-only candidate", "claim_compatible": False, "assessment": ""}
                    for record in segment.get("references", []) if candidate_key(record) in identities]
        citation = {"citation_id": str(segment.get("id") or f"C{n:05}"), "sentence": sentence, "supports": supports}
        if len(hits) == 1:
            citation.update(start=hits[0], end=hits[0] + len(sentence))
        citations.append(citation)
    return {"schema_version": REVIEW_VERSION, "references": refs,
            "documents": [{"document_id": document_id or "inline:" + digest(text), "title": title,
                           "text": text, "citations": citations}],
            "warning": "Unreviewed draft: verify claim support, quotations, author warnings and sentence positions before prepare."}


def validate_unit(unit):
    if unit.get("kind") not in {"abstract", "full_text"} or not isinstance(unit.get("text"), str) or not unit["text"].strip():
        raise ValueError("Evidence needs actual abstract/full-text content")
    if not unit.get("url") and not unit.get("path"):
        raise ValueError("Evidence needs a source URL or source file")
    page = unit.get("pdf_page_index")
    if page is not None and (type(page) is not int or page < 1):
        raise ValueError("PDF page index must be a positive integer")
    if unit["kind"] == "abstract" and (page is not None or unit.get("printed_page_label")):
        raise ValueError("Abstract evidence cannot establish PDF page numbers")
    if unit.get("section") or page is not None or unit.get("printed_page_label"):
        if unit.get("locator_verified") is not True:
            raise ValueError("Source locator must have been verified")
    if unit["kind"] == "abstract" and not unit.get("section"):
        unit["section"] = "Abstract"
    unit["content_hash"] = digest(unit["text"])


def sentence_span(text, citation):
    sentence = citation.get("sentence")
    if not isinstance(sentence, str) or not sentence:
        raise ValueError("Exact original sentence required")
    start, end = citation.get("start"), citation.get("end")
    if start is None and end is None:
        hits = [m.start() for m in re.finditer(re.escape(sentence), text)]
        if len(hits) != 1:
            raise ValueError("Repeated/missing sentence needs explicit start/end offsets")
        start, end = hits[0], hits[0] + len(sentence)
    if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text) or text[start:end] != sentence:
        raise ValueError("Citation offsets do not match the original sentence")
    return start, end


def prepare(payload):
    if payload.get("schema_version", REVIEW_VERSION) != REVIEW_VERSION:
        raise ValueError("Unsupported citation review version")
    records, canonical, by_key = {}, {}, {}
    for raw in payload.get("references", []):
        ref_id = raw.get("reference_id")
        if not isinstance(ref_id, str) or not ref_id or ref_id in records:
            raise ValueError("Each reviewed reference needs a unique reference_id")
        record = deepcopy(raw)
        entry = rm.checked_metadata(record)
        record["metadata"] = entry
        units = {}
        for unit in record.get("source_units", []):
            unit_id = unit.get("source_id")
            if not unit_id or unit_id in units:
                raise ValueError("Source unit IDs must be unique within a reference")
            validate_unit(unit)
            units[unit_id] = unit
        record["source_units"] = units
        ids = ing.identifiers(entry)
        key = next((k + ":" + ids[k] for k in ("doi", "pmid") if ids[k]), None)
        key = key or "title:" + digest([entry["title"], entry["creators"], entry.get("year")])
        for earlier in records.values():
            previous = ing.identifiers(earlier["metadata"])
            if any(ids[k] and ids[k] == previous[k] for k in ("doi", "pmid")):
                candidate = {"key": "earlier", "library": ing.Library().json(), "data": earlier["metadata"]}
                matched, detail = ing.match_reference(entry, [candidate], ing.Library())
                if not matched:
                    raise ValueError("Conflicting reference identities: " + str(detail))
                key = canonical[earlier["reference_id"]]
        canonical[ref_id] = key
        records[ref_id] = record
        by_key.setdefault(key, ref_id)

    documents, mappings, unresolved, numbers = [], [], [], {}
    seen_documents = set()
    for raw_document in payload.get("documents", []):
        text = raw_document.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Document text is required")
        doc_id = raw_document.get("document_id") or "inline:" + digest(text)
        if not isinstance(doc_id, str) or doc_id in seen_documents:
            raise ValueError("Document IDs must be unique")
        seen_documents.add(doc_id)
        doc = {"document_id": doc_id, "title": raw_document.get("title"), "text": text,
               "text_hash": digest(text), "citations": []}
        if raw_document.get("source_path") or raw_document.get("path"):
            doc["source_path"] = raw_document.get("source_path") or raw_document["path"]
        spans, seen_citations, insertions = [], set(), {}
        positioned = [(sentence_span(text, c), c) for c in raw_document.get("citations", [])]
        for (start, end), citation in sorted(positioned, key=lambda value: value[0]):
            cid, sentence = citation.get("citation_id"), citation.get("sentence")
            if not cid or cid in seen_citations or not isinstance(sentence, str) or not sentence:
                raise ValueError("Citation needs a unique ID and exact original sentence")
            seen_citations.add(cid)
            if any(start < b and end > a for a, b in spans):
                raise ValueError("Overlapping citation spans; combine the support records")
            spans.append((start, end))
            accepted = []
            for support in citation.get("supports", []):
                ref_id = support.get("reference_id")
                if ref_id not in records:
                    raise ValueError("Unknown reference_id")
                if support.get("grade") not in {"strong", "partial", "background"} or support.get("claim_compatible") is not True:
                    unresolved.append({"document_id": doc_id, "citation_id": cid, "reference_id": ref_id,
                                       "reason": "Support not reviewed or not compatible with this claim"})
                    continue
                if not isinstance(support.get("assessment"), str) or not support["assessment"].strip():
                    raise ValueError("Support needs a reviewer assessment")
                unit = records[ref_id]["source_units"].get(support.get("source_id"))
                quote = support.get("quote")
                if not unit or not isinstance(quote, str) or len(normalized_text(quote)) < 4 or normalized_text(quote) not in normalized_text(unit["text"]):
                    raise ValueError("Support quotation not found in its actual source unit")
                if unit["kind"] == "abstract" and records[ref_id]["metadata"].get("abstract"):
                    if normalized_text(quote) not in normalized_text(records[ref_id]["metadata"]["abstract"]):
                        raise ValueError("Abstract quotation conflicts with reference metadata")
                key = canonical[ref_id]
                numbers.setdefault(key, len(numbers) + 1)
                accepted.append({**deepcopy(support), "number": numbers[key],
                                 "source": {k: deepcopy(v) for k, v in unit.items() if k != "text"}})
            if not accepted:
                unresolved.append({"document_id": doc_id, "citation_id": cid, "reason": "No verified supporting source"})
                continue
            marker = "[" + ",".join(str(n) for n in sorted({s["number"] for s in accepted})) + "]"
            insertions[end] = marker
            row = {"document_id": doc_id, "citation_id": cid, "sentence": sentence,
                   "start": start, "end": end, "insert_at": end, "marker": marker, "supports": accepted}
            mappings.append(row)
            doc["citations"].append(row)
        rendered = text
        for at, marker in sorted(insertions.items(), reverse=True):
            rendered = rendered[:at] + marker + rendered[at:]
        doc["cited_text"] = rendered
        documents.append(doc)
        if not doc["citations"]:
            unresolved.append({"document_id": doc_id, "reason": "No reviewed citations for this document"})
    if not documents:
        raise ValueError("At least one source document is required")
    bibliography = [{"number": number, "reference_id": by_key[key],
                     "text": reference_text(records[by_key[key]]["metadata"])} for key, number in numbers.items()]
    bundle = {"schema_version": BUNDLE_VERSION, "bundle_id": digest(payload),
              "prepared_at": datetime.now(timezone.utc).isoformat(), "documents": documents,
              "references": records, "bibliography": bibliography, "citation_mappings": mappings,
              "unresolved": unresolved, "complete": bool(bibliography) and not unresolved,
              "next_action": "ask_zotero_import", "consent": {"import": False, "notes": False}}
    bundle["integrity"] = bundle_integrity(bundle)
    return bundle


def bundle_integrity(bundle):
    return digest({k: v for k, v in bundle.items() if k != "integrity"})


def check_bundle(bundle):
    if bundle.get("schema_version") != BUNDLE_VERSION or bundle.get("integrity") != bundle_integrity(bundle):
        raise ValueError("Bundle changed; regenerate it from the reviewed source")


def source_references(bundle):
    check_bundle(bundle)
    docs = []
    for doc in bundle["documents"]:
        occurrences = {}
        for citation in doc["citations"]:
            for support in citation["supports"]:
                occurrences.setdefault(support["reference_id"], []).append(citation)
        refs = []
        for ref_id, cites in occurrences.items():
            record = bundle["references"][ref_id]
            refs.append({"reference_id": ref_id, "raw_text": record.get("raw_text") or reference_text(record["metadata"]),
                         "metadata": deepcopy(record["metadata"]), "metadata_provenance": deepcopy(record["metadata_provenance"]),
                         "source_location": {"document_title": doc["title"], "document_hash": doc["text_hash"],
                                             "citations": [{k: c[k] for k in ("citation_id", "sentence", "start", "end", "marker")} for c in cites]}})
        if refs:
            docs.append({"document_id": doc["document_id"], "references": refs})
    return ing.normalize_sources(docs)


def import_bundle(bundle, backend, collection, *, approved=False, checks=3, delay=0.3, journal_path=None):
    if approved is not True:
        raise PermissionError("Explicit consent to Zotero import is required")
    refs = source_references(bundle)
    if not refs:
        raise ValueError("No reviewed references to import")
    result = ing.reconcile(refs, backend, collection, checks=checks, delay=delay, journal_path=journal_path)
    result["citation_bundle_id"] = bundle["bundle_id"]
    return result


def map_existing(bundle, prior, backend):
    """Refresh evidence/location mappings against already adopted items; never write."""
    refs = source_references(bundle)
    if prior.get("schema_version") != ing.SCHEMA_VERSION:
        raise ValueError("A versioned prior Zotero mapping is required")
    prior_target = prior["target_collection"]
    collection = prior_target if isinstance(prior_target, str) else prior_target["key"]
    if not isinstance(collection, str) or not collection:
        raise ValueError("Prior target collection is missing")
    backend.collection(collection)
    target = backend.target_items(collection)
    previous = {}
    for row in prior["mappings"]:
        key = (row["source"]["document_id"], row["source"]["reference_id"])
        if key in previous:
            raise ValueError("Duplicate source mapping")
        previous[key] = row
    rows = []
    for ref in refs:
        old = previous.get((ref["document_id"], ref["reference_id"]))
        row = {"source": {k: deepcopy(ref.get(k)) for k in ("document_id", "reference_id", "raw_text", "label", "source_location", "source_encoding")},
               "identifiers": {}, "item": None, "membership_verified": False, "outcome": "pending",
               "target_collection": {"library": backend.library.json(), "key": collection}, "match": {}, "anomalies": [],
               "fallback_reason": None, "known_duplicates": []}
        entry = rm.checked_metadata(ref)
        if old:
            row["fallback_reason"] = deepcopy(old.get("fallback_reason"))
            row["known_duplicates"] = deepcopy(old.get("known_duplicates", []))
            row["adoption_history"] = deepcopy(old.get("adoption_history") or [{
                k: deepcopy(old.get(k)) for k in ("outcome", "item", "fallback_reason", "known_duplicates")}])
        row["identifiers"] = {k: ing.identifiers(entry)[k] for k in ("doi", "pmid", "title")}
        row["metadata_provenance"] = deepcopy(entry["_metadata_provenance"])
        if old and old.get("membership_verified") and old.get("outcome") in ing.SUCCESS and old.get("item"):
            identity = old["item"]
            wanted = {**entry, "zotero_item": identity}
            found, detail = ing.match_reference(wanted, target, backend.library)
            row["match"] = detail
            if found and collection in found.get("data", {}).get("collections", []):
                row.update(item=ing.identity(found, backend.library), membership_verified=True, outcome="target_existing",
                           known_duplicates=deepcopy(old.get("known_duplicates", [])))
        if not row["membership_verified"]:
            row["anomalies"].append("Previously adopted item was not verified; no import or alternate item selection")
        rows.append(row)
    try:
        final = backend.target_items(collection)
        for row, ref in zip(rows, refs):
            if row["membership_verified"]:
                wanted = {**rm.checked_metadata(ref), "zotero_item": row["item"]}
                adopted, detail = ing.match_reference(wanted, final, backend.library)
                if not adopted or collection not in adopted.get("data", {}).get("collections", []):
                    row.update(outcome="pending", membership_verified=False)
                    row["anomalies"].append("Final target snapshot did not confirm the adopted item")
    except Exception as exc:
        for row in rows:
            row.update(outcome="pending", membership_verified=False)
            row["anomalies"].append("Final target read failed: " + str(exc))
    return {"schema_version": ing.SCHEMA_VERSION, "citation_bundle_id": bundle["bundle_id"], "library": backend.library.json(),
            "target_collection": prior["target_collection"], "mappings": rows, "counts": dict(ing.Counter(r["outcome"] for r in rows)),
            "complete": bool(rows) and all(r["membership_verified"] for r in rows)}


def write_bundle(bundle, directory):
    check_bundle(bundle)
    directory = Path(directory)
    paths = [directory / "citation-bundle.json", directory / "references.md", directory / "references.ris"]
    paths += [directory / f"document-{n:03}.cited.md" for n in range(1, len(bundle["documents"]) + 1)]
    if any(p.exists() for p in paths):
        raise FileExistsError("Use a fresh output directory; existing results will not be overwritten")
    directory.mkdir(parents=True, exist_ok=True)
    paths[0].write_text(json.dumps(bundle, ensure_ascii=False, indent=2), encoding="utf-8")
    bibliography = "\n".join(f'[{r["number"]}] {r["text"]}' for r in bundle["bibliography"])
    paths[1].write_text(bibliography + "\n", encoding="utf-8")
    ris = "".join(z.clean_ris_for_import(z._pubmed_to_ris(record["metadata"]) if record["metadata"].get("pmid") else z._doi_to_full_ris(record["metadata"]))
                  for record in (bundle["references"][r["reference_id"]] for r in bundle["bibliography"]))
    paths[2].write_text(ris, encoding="utf-8")
    for path, doc in zip(paths[3:], bundle["documents"]):
        used = {s["number"] for c in doc["citations"] for s in c["supports"]}
        local_bibliography = "\n".join(f'[{r["number"]}] {r["text"]}' for r in bundle["bibliography"] if r["number"] in used)
        path.write_text(doc["cited_text"] + "\n\n## References\n\n" + local_bibliography + "\n", encoding="utf-8")
    return paths


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, value):
    path = local_state.preflight_output(path)
    local_state.atomic_json(path, value, replace=False)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "search":
        return load_search().main(argv[1:])
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    prep = subs.add_parser("prepare")
    prep.add_argument("--review", required=True)
    prep.add_argument("--outdir", required=True)
    draft = subs.add_parser("review-template")
    draft.add_argument("--text-file", required=True)
    draft.add_argument("--candidates", required=True)
    draft.add_argument("--out", required=True)
    draft.add_argument("--document-id")
    draft.add_argument("--title")
    evidence = subs.add_parser("evidence-pages")
    evidence.add_argument("--pdf", required=True)
    evidence.add_argument("--page", type=int, action="append", required=True)
    evidence.add_argument("--approve-notes", action="store_true")
    evidence.add_argument("--acknowledge-token-cost", action="store_true")
    evidence.add_argument("--out", required=True)
    caps = subs.add_parser("notes-capabilities")
    caps.add_argument("--plugin-url", default="http://127.0.0.1:23120/mcp")
    imp = subs.add_parser("import")
    imp.add_argument("--bundle", required=True)
    imp.add_argument("--collection")
    imp.add_argument("--approve-import", action="store_true")
    imp.add_argument("--mapping-out", required=True)
    imp.add_argument("--library-type", choices=["user", "group"], default="user")
    imp.add_argument("--library-id", type=int, default=0)
    imp.add_argument("--plugin-url", default="http://127.0.0.1:23120/mcp")
    imp.add_argument("--journal", help="Optional persistent recovery journal; default is a local per-target cache")
    refresh = subs.add_parser("map-existing")
    refresh.add_argument("--bundle", required=True)
    refresh.add_argument("--prior-mapping", required=True)
    refresh.add_argument("--out", required=True)
    provenance = subs.add_parser("provenance-export")
    provenance.add_argument("--bundle", required=True)
    provenance.add_argument("--mapping")
    provenance.add_argument("--out", required=True)
    notes = subs.add_parser("notes-plan")
    notes.add_argument("--bundle", required=True)
    notes.add_argument("--mapping", required=True)
    notes.add_argument("--approve-notes", action="store_true")
    notes.add_argument("--acknowledge-token-cost", action="store_true")
    notes.add_argument("--outdir", required=True)
    for name in ("notes-write", "notes-verify"):
        cmd = subs.add_parser(name)
        cmd.add_argument("--job", required=True)
        cmd.add_argument("--result-out", required=True)
        cmd.add_argument("--journal")
        cmd.add_argument("--plugin-url", default="http://127.0.0.1:23120/mcp")
    args = parser.parse_args(argv)
    if args.command == "provenance-export":
        import provenance_markdown
        local_state.preflight_output(args.out, sources=[args.bundle, args.mapping])
        bundle = read_json(args.bundle)
        mapping = read_json(args.mapping) if args.mapping else None
        provenance_markdown.write(bundle, args.out, mapping)
        print(json.dumps({"written": str(Path(args.out).resolve()), "zotero_written": False,
                          "network_used": False}, ensure_ascii=False))
        return 0
    if args.command == "notes-capabilities":
        plugin = ing.Plugin(args.plugin_url)
        supported = False
        if not plugin.denied and plugin.supports("write_note", {"libraryID", "action", "parentKey", "content"}):
            actions = plugin.tools["write_note"]["inputSchema"]["properties"]["action"].get("enum")
            supported = actions is None or "create" in actions
        print(json.dumps({"writer_advertised": supported, "permission_denied": plugin.denied,
                          "reason": plugin.reason, "native_route": "Zotero Tools > Developer > Run JavaScript",
                          "live_write_tested": False}))
        return 0
    if args.command == "review-template":
        text, _ = z.read_source_text(Path(args.text_file))
        draft = review_template(text, read_json(args.candidates), document_id=args.document_id, title=args.title)
        write_json(args.out, draft)
        print(json.dumps({"review_file": args.out, "references": len(draft["references"]), "reviewed": False}))
        return 0
    if args.command == "evidence-pages":
        if not args.approve_notes or not args.acknowledge_token_cost:
            print(json.dumps({"next_action": "ask_notes_consent_with_token_warning", "read_pdf": False}))
            return 3
        if Path(args.out).exists():
            raise FileExistsError("Evidence output already exists")
        import provenance_notes as notes_module
        units = notes_module.extract_pdf_pages(Path(args.pdf), args.page, approved=True, cost_warning_acknowledged=True)
        write_json(args.out, {"source_units": units})
        print(json.dumps({"evidence_file": args.out, "pages": len(units), "printed_page_labels": "not inferred"}))
        return 0
    if args.command == "prepare":
        bundle = prepare(read_json(args.review))
        paths = write_bundle(bundle, args.outdir)
        print(json.dumps({"complete": bundle["complete"], "next_action": "ask_zotero_import", "outputs": [str(p) for p in paths]}, ensure_ascii=False))
        return 0 if bundle["complete"] else 2
    if args.command == "map-existing":
        if Path(args.out).exists():
            raise FileExistsError("Mapping output already exists")
        bundle, prior = read_json(args.bundle), read_json(args.prior_mapping)
        check_bundle(bundle)
        import provenance_notes as notes_module
        library = prior["library"]
        backend = ing.LocalBackend(ing.Library(library["type"], library["id"]), notes_module.NoWriter())
        result = map_existing(bundle, prior, backend)
        write_json(args.out, result)
        print(json.dumps({"complete": result["complete"], "counts": result["counts"], "writes": False}))
        return 0 if result["complete"] else 2
    if args.command == "import":
        if not args.approve_import:
            print(json.dumps({"next_action": "ask_zotero_import", "written": False}))
            return 3
        if not args.collection:
            raise ValueError("Select the target library/collection after import consent")
        local_state.preflight_output(args.mapping_out, sources=[args.bundle, args.journal])
        bundle = read_json(args.bundle)
        check_bundle(bundle)
        backend = ing.LocalBackend(ing.Library(args.library_type, args.library_id), ing.Plugin(args.plugin_url))
        journal = args.journal or local_state.default_import_journal(z.DEFAULT_BASE_URL, backend.library, args.collection)
        if Path(journal).resolve() in {Path(args.bundle).resolve(), Path(args.mapping_out).resolve()}:
            raise ValueError("Recovery journal must not overwrite source or mapping")
        result = import_bundle(bundle, backend, args.collection, approved=True, journal_path=journal)
        write_json(args.mapping_out, result)
    else:
        import provenance_notes as notes_module
        if args.command == "notes-plan":
            if not args.approve_notes or not args.acknowledge_token_cost:
                print(json.dumps({"next_action": "ask_notes_consent_with_token_warning", "written": False}))
                return 3
            bundle, mapping = read_json(args.bundle), read_json(args.mapping)
            result = notes_module.prepare_notes(bundle, mapping, approved=True, cost_warning_acknowledged=True)
            notes_module.write_job(result, Path(args.outdir), bundle=bundle, mapping=mapping)
        else:
            job = read_json(args.job)
            notes_module.check_job(job)
            local_state.preflight_output(args.result_out, sources=[args.job, args.journal])
            backend = notes_module.NoteBackend(job, plugin_url=args.plugin_url, discover_writer=args.command == "notes-write")
            if args.command == "notes-write":
                result = notes_module.write_notes(job, backend, journal_path=args.journal or str(Path(args.job).with_suffix(".journal.json")))
            else:
                result = notes_module.verify_notes(job, backend)
            write_json(args.result_out, result)
    print(json.dumps({"complete": result["complete"], "counts": result.get("counts"), "unresolved": result.get("unresolved", [])}, ensure_ascii=False))
    return 0 if result["complete"] else 2


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, PermissionError, FileExistsError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
