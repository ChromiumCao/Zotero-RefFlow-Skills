"""Read-only citation audit artifacts; semantic decisions belong to the reviewer."""
from __future__ import annotations

import argparse
from collections import Counter
from copy import deepcopy
import csv
from datetime import datetime, timezone
import hashlib
import html
import io
import json
from pathlib import Path
import re
import sys

import citation_handoff as ch
import ingest as ing
import local_state
import pdf_reader
import reference_metadata as rm
import zotero as z

REVIEW_VERSION = "zotero-input.citation-audit-review/1"
REPORT_VERSION = "zotero-input.citation-audit-report/1"
VERDICTS = {"supported", "partially_supported", "not_supported", "contradicted", "unverifiable"}
LABELS = {"supported": "支持", "partially_supported": "部分支持",
          "not_supported": "已审阅范围内未找到支持", "contradicted": "相矛盾",
          "unverifiable": "无法核实", "not_applicable": "未发现正文引用"}


def consent(approved, cost_warning_acknowledged):
    if approved is not True or cost_warning_acknowledged is not True:
        raise PermissionError("Citation audit needs current consent after a higher-token-cost warning")


def require_id(value, seen, label):
    if not isinstance(value, str) or not value.strip() or value in seen:
        raise ValueError(label + " must be a nonempty unique string")
    seen.add(value)
    return value


def title_key(value):
    # Unlike matching, auditing also needs to flag wrong short English titles.
    return re.sub(r"[\W_]+", " ", z.field_key(value)).strip()


def template(payload, *, approved=False, cost_warning_acknowledged=False):
    consent(approved, cost_warning_acknowledged)
    draft = deepcopy(payload)
    draft["schema_version"] = REVIEW_VERSION
    for ref in draft.get("references", []):
        ref.update(identity_reviewed=False, identity_assessment="")
    for doc in draft.get("documents", []):
        for citation in doc.get("citations", []):
            refs = citation.pop("reference_ids", None)
            checks = [{"reference_id": rid} for rid in refs] if refs is not None else citation.get("checks", [])
            citation["checks"] = [{**check, "reviewed": False, "verdict": "unverifiable",
                                   "assessment": "", "search_scope": "", "evidence": [],
                                   "reviewed_source_ids": []} for check in checks]
    draft["warning"] = "Unreviewed draft. Lookup hits and matching words do not prove semantic support."
    return draft


def original_fields(ref):
    fields = ref.get("original", {})
    if not isinstance(fields, dict):
        raise ValueError("original must contain parsed bibliography fields")
    selected = {k: fields.get(k, ref.get(k)) for k in
                ("doi", "DOI", "pmid", "PMID", "title", "year", "first_author")
                if fields.get(k, ref.get(k)) is not None}
    original = z.reference_from_fields(selected, raw_text=ref.get("raw_text") or " ")
    errors = list(original.get("extraction_errors", []))
    raw = ref.get("raw_text") or ""
    for lower, upper, normalizer, extractor in (("doi", "DOI", z.normalize_doi, z.extract_doi),
                                               ("pmid", "PMID", z.normalize_pmid, z.extract_pmid)):
        values = {normalizer(selected[k]) for k in (lower, upper) if selected.get(k)} - {None}
        raw_value = extractor(raw)
        if len(values) > 1 or (values and raw_value and raw_value not in values):
            errors.append("Conflicting original identifier representations: " + lower)
        if lower == "pmid" and any(not re.fullmatch(r"\d{4,10}", str(selected[k]).strip())
                                   for k in (lower, upper) if selected.get(k)):
            errors.append("Parsed PMID must be a complete integer identifier")
        if lower == "doi" and any(not z.extract_doi(str(selected[k])) for k in (lower, upper) if selected.get(k)):
            errors.append("Parsed DOI is malformed")
    if errors:
        original["extraction_errors"] = errors
    for key in ("creators", "authors"):
        if fields.get(key):
            original[key] = deepcopy(fields[key])
    return original


def identity_review(ref):
    original = original_fields(ref)
    result = {"status": "unverified", "basis": [], "issues": [], "metadata": None,
              "original": original, "assessment": ref.get("identity_assessment")}
    try:
        # Validate retrieved metadata independently so a wrong bibliography remains a row.
        entry = rm.checked_metadata({"metadata": ref.get("metadata"),
                                     "metadata_provenance": ref.get("metadata_provenance")})
    except (ing.Uncertain, ValueError, TypeError, KeyError) as exc:
        result["issues"].append("Metadata unavailable/invalid: " + str(exc))
        return result
    result["metadata"] = entry
    if ref.get("ambiguous") is True or len(ref.get("candidate_identities", [])) > 1:
        result.update(status="ambiguous", issues=["Multiple candidates: no arbitrary association"])
        return result
    wanted, actual = ing.identifiers(original), ing.identifiers(entry)
    conflicts = [k for k in ("doi", "pmid", "year") if wanted[k] and actual[k] and wanted[k] != actual[k]]
    if original.get("title") and title_key(original["title"]) not in {
        title_key(t) for t in [entry["title"]] + entry.get("title_aliases", [])
    }:
        conflicts.append("title")
    if ing.reference_authors(original) and not ing.reference_authors(original) & ing.reference_authors(entry):
        conflicts.append("author")
    if conflicts:
        result.update(status="conflict", issues=["Bibliography conflicts with retrieved record: " + ", ".join(conflicts)])
        return result
    missing = [k for k in ("doi", "pmid", "year") if wanted[k] and not actual[k]]
    if original.get("extraction_errors") or missing:
        result["issues"] += original.get("extraction_errors", []) + ["Unverified original field: " + k for k in missing]
        return result
    candidate = {"key": "metadata-comparison-only", "library": ing.Library().json(), "data": entry}
    match, detail = ing.match_reference(original, [candidate], ing.Library())
    result["basis"] = detail["basis"]
    if not match:
        result["issues"].append("Insufficient or conflicting original identity: " + str(detail.get("reason")))
    elif ref.get("identity_reviewed") is not True or not str(ref.get("identity_assessment") or "").strip():
        result["issues"].append("Actual retrieved identity has not been reviewed")
    else:
        result["status"] = "verified"
    return result


def validate_source(unit, entry):
    unit = deepcopy(unit)
    if unit.get("content_hash") and unit["content_hash"] != ch.digest(unit.get("text")):
        raise ValueError("Source cache content changed; review the actual source again")
    ch.validate_unit(unit)
    if unit.get("identity_verified") is not True or not str(unit.get("identity_assessment") or "").strip():
        raise ValueError("Source passage must be verified as belonging to this reference")
    if unit["kind"] == "abstract" and entry and entry.get("abstract"):
        if ch.normalized_text(unit["text"]) not in ch.normalized_text(entry["abstract"]):
            raise ValueError("Abstract passage conflicts with retrieved metadata")
    if unit.get("paragraph_verified") is True and not unit.get("paragraph_label"):
        raise ValueError("Verified natural paragraph needs its actual label")
    if unit.get("paragraph_label"):
        if unit.get("paragraph_verified") is not True or not str(unit.get("paragraph_verification") or "").strip():
            raise ValueError("Natural paragraph needs visual/structured-source confirmation")
        if unit["kind"] == "abstract":
            raise ValueError("Abstract snippets cannot establish original full-text paragraphs")
    block = unit.get("text_block_index_on_page")
    if block is not None and (type(block) is not int or block < 1 or unit.get("pdf_page_index") is None):
        raise ValueError("Text block number needs a physical page and positive index")
    return unit


def extract_pages(path, pages, *, approved=False, cost_warning_acknowledged=False):
    consent(approved, cost_warning_acknowledged)
    pages = list(dict.fromkeys(pages))
    if not pages or len(pages) > 5 or any(type(p) is not int or p < 1 for p in pages):
        raise ValueError("Select one to five positive physical PDF pages per evidence batch")
    path = Path(path).resolve()
    file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
    units = []
    for page, text in pdf_reader.read_pages(path, pages):
        # Blank-line blocks are extraction units, not verified natural paragraphs.
        for index, block in enumerate(re.split(r"\n[ \t]*\n", text), 1):
            if not block.strip():
                continue
            units.append({"source_id": f"{file_hash[:16]}-p{page}-b{index}", "kind": "full_text",
                          "path": str(path), "text": block, "source_file_sha256": file_hash,
                          "extracted_from_pdf": True, "pdf_page_index": page, "locator_verified": True,
                          "printed_page_label": None, "section": None, "text_block_index_on_page": index,
                          "paragraph_label": None, "paragraph_verified": False,
                          "identity_verified": False, "identity_assessment": "",
                          "content_hash": ch.digest(block)})
    if hashlib.sha256(path.read_bytes()).hexdigest() != file_hash:
        raise ValueError("PDF changed during extraction; no reliable evidence snapshot")
    return units


def build_report(payload, *, approved=False, cost_warning_acknowledged=False):
    consent(approved, cost_warning_acknowledged)
    if payload.get("schema_version") != REVIEW_VERSION:
        raise ValueError("Unsupported citation audit review version")
    references, identities, units = {}, {}, {}
    for ref in payload.get("references", []):
        rid = require_id(ref.get("reference_id"), set(references), "Reference ID")
        references[rid] = deepcopy(ref)
        identities[rid] = identity_review(ref)
        sources = {}
        for unit in ref.get("source_units", []):
            sid = require_id(unit.get("source_id"), set(sources), "Source ID within a reference")
            sources[sid] = deepcopy(unit)
        units[rid] = sources
    # A shared identifier cannot conceal contradictory records across sources.
    for index, rid in enumerate(references):
        a = identities[rid]["metadata"]
        if not a:
            continue
        for other in list(references)[:index]:
            b = identities[other]["metadata"]
            if not b:
                continue
            ai, bi = ing.identifiers(a), ing.identifiers(b)
            if any(ai[k] and ai[k] == bi[k] for k in ("doi", "pmid")):
                contradictory = any(ai[k] and bi[k] and ai[k] != bi[k] for k in ("doi", "pmid", "year"))
                contradictory |= not bool(ing.reference_titles(a) & ing.reference_titles(b))
                contradictory |= not bool(ing.reference_authors(a) & ing.reference_authors(b))
                if contradictory:
                    for key in (rid, other):
                        identities[key]["status"] = "conflict"
                        identities[key]["issues"].append("Shared identifier conflicts across reference records")
    rows, documents, seen_docs, cited = [], [], set(), set()
    for doc in payload.get("documents", []):
        did = require_id(doc.get("document_id"), seen_docs, "Document ID")
        text = doc.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("Exact source document text is required")
        documents.append({"document_id": did, "title": doc.get("title"), "source_path": doc.get("source_path"),
                          "text": text, "text_hash": ch.digest(text)})
        seen_citations = set()
        for citation in doc.get("citations", []):
            cid = require_id(citation.get("citation_id"), seen_citations, "Citation ID within a document")
            start, end = ch.sentence_span(text, citation)
            seen_checks = set()
            for check in citation.get("checks") or [{"reference_id": None, "verdict": "unverifiable"}]:
                rid = check.get("reference_id")
                if rid in seen_checks:
                    raise ValueError("Combine multiple quotations under one citation/reference check")
                seen_checks.add(rid)
                cited.add(rid)
                ref = references.get(rid, {})
                identity = identities.get(rid, {"status": "unverified", "basis": [], "issues": ["Unresolved bibliography link"],
                                                "metadata": None, "original": None, "assessment": None})
                verdict = check.get("verdict", "unverifiable")
                if verdict not in VERDICTS:
                    raise ValueError("Unsupported semantic verdict")
                issues = list(identity["issues"])
                if check.get("reviewed") is not True or not str(check.get("assessment") or "").strip():
                    issues.append("Semantic review/assessment missing")
                if not str(check.get("search_scope") or "").strip():
                    issues.append("Reviewed scope missing")
                evidence = check.get("evidence", [])
                inspected = check.get("reviewed_source_ids", [])
                verified_units = {}
                for sid in dict.fromkeys([q.get("source_id") for q in evidence] + list(inspected)):
                    try:
                        if sid not in units.get(rid, {}):
                            raise ValueError("Evidence references an unknown source unit")
                        if units[rid][sid].get("reference_id", rid) != rid:
                            raise ValueError("Source unit belongs to a different reference")
                        verified_units[sid] = validate_source(units[rid][sid], identity["metadata"])
                    except (ValueError, TypeError, KeyError) as exc:
                        issues.append(str(exc))
                if verdict in {"supported", "partially_supported", "contradicted"} and not evidence:
                    issues.append("This verdict requires an exact original passage")
                if verdict == "not_supported" and not verified_units:
                    issues.append("No actual source content reviewed; absence is not disproof")
                for quote in evidence:
                    unit = verified_units.get(quote.get("source_id"))
                    excerpt = quote.get("quote")
                    if not unit or not isinstance(excerpt, str) or not excerpt.strip() or ch.normalized_text(excerpt) not in ch.normalized_text(unit["text"]):
                        issues.append("Quotation not found in its source unit")
                semantic_verified = verdict != "unverifiable" and identity["status"] == "verified" and not issues
                for number, quote in enumerate(evidence or [{}], 1):
                    unit = verified_units.get(quote.get("source_id")) if evidence else next(iter(verified_units.values()), None)
                    locator = bool(semantic_verified and evidence and unit and unit["kind"] == "full_text" and unit.get("locator_verified") is True
                                   and (unit.get("pdf_page_index") or unit.get("section"))
                                   and unit.get("paragraph_verified") is True and unit.get("paragraph_label"))
                    rows.append({"document_id": did, "document_title": doc.get("title"), "citation_id": cid,
                                 "sentence": citation["sentence"], "start": start, "end": end, "label": citation.get("label"),
                                 "source_location": deepcopy(citation.get("source_location")), "reference_id": rid,
                                 "raw_reference": ref.get("raw_text"), "reference_label": ref.get("label"),
                                 "identity": deepcopy(identity), "requested_verdict": verdict,
                                 "verdict": verdict if semantic_verified else "unverifiable",
                                 "semantic_verified": semantic_verified, "locator_complete": locator,
                                 "assessment": check.get("assessment"), "search_scope": check.get("search_scope"),
                                 "suggestion": check.get("suggestion"), "evidence_index": number if evidence else None,
                                 "quote": quote.get("quote"), "source": deepcopy(unit),
                                 "reviewed_source_ids": deepcopy(inspected), "issues": list(dict.fromkeys(issues))})
    if not documents:
        raise ValueError("At least one source document is required")
    for rid in (key for key in references if key not in cited):
        rows.append({"document_id": references[rid].get("document_id"), "citation_id": None, "reference_id": rid,
                     "raw_reference": references[rid].get("raw_text"), "reference_label": references[rid].get("label"),
                     "identity": identities[rid], "verdict": "not_applicable", "semantic_verified": False,
                     "locator_complete": False, "issues": identities[rid]["issues"] + ["No linked body citation; bibliography only"]})
    body = [row for row in rows if row["citation_id"] is not None]
    semantic_complete = bool(body) and all(row["semantic_verified"] for row in body)
    locator_complete = bool(body) and all(row["locator_complete"] for row in body)
    identity_complete = bool(rows) and all(row["identity"]["status"] == "verified" for row in rows)
    return {"schema_version": REPORT_VERSION, "audit_id": ch.digest(payload),
            "created_at": datetime.now(timezone.utc).isoformat(), "documents": documents,
            "references": references, "rows": rows, "counts": dict(Counter(row["verdict"] for row in rows)),
            "identity_complete": identity_complete, "semantic_complete": semantic_complete,
            "locator_complete": locator_complete, "complete": identity_complete and semantic_complete and locator_complete,
            "zotero_written": False, "manuscript_modified": False,
            "warning": "Reviewer judgments, not automatic semantic proof. Negative findings apply only to reviewed scope. "
                       "Cached locators are creation-time snapshots, not newly read PDF verification."}


def cell(value):
    if value is None or value == "":
        return "未取得"
    return str(value)


COLUMNS = [("document_id", "文档 ID"), ("citation_id", "引用 ID"), ("label", "正文标签"),
           ("sentence", "正文原句"), ("reference_id", "文献 ID"), ("raw_reference", "原参考文献"),
           ("title", "核实题名"), ("doi", "DOI"), ("pmid", "PMID"), ("identity", "身份核查"),
           ("verdict", "语义结论"), ("quote", "原文证据"), ("kind", "证据级别"), ("section", "章节"),
           ("pdf_page_index", "PDF 页序"), ("printed_page_label", "印刷页码"),
           ("paragraph_label", "已核实自然段"), ("text_block_index_on_page", "提取文本块（非自然段）"),
           ("paragraph_verification", "段落核实依据"), ("source", "原文来源"), ("search_scope", "审阅范围"),
           ("assessment", "核查说明"), ("suggestion", "修改建议"), ("issues", "异常/缺失")]


def table_row(row):
    result = deepcopy(row)
    identity = row["identity"]
    result.update({k: (identity.get("metadata") or {}).get(k) for k in ("title", "doi", "pmid")})
    result["identity"] = identity["status"]
    result["verdict"] = LABELS[row["verdict"]]
    source = row.get("source") or {}
    result.update({k: source.get(k) for k in ("kind", "section", "pdf_page_index", "printed_page_label",
                                             "paragraph_label", "text_block_index_on_page", "paragraph_verification")})
    result["source"] = source.get("url") or source.get("path")
    result["issues"] = "; ".join(row["issues"])
    return [cell(result.get(key)) for key, _ in COLUMNS]


def markdown_cell(value):
    value = html.escape(value, quote=True).replace("|", "&#124;").replace("\r", "").replace("\n", "<br>")
    return re.sub(r"([\\`*_[\]{}])", r"\\\1", value)


def csv_cell(value):
    return "'" + value if value.lstrip().startswith(("=", "+", "-", "@")) else value


def export_report(report, directory, *, sources=()):
    directory = Path(directory).resolve()
    paths = [directory / ("citation-audit." + suffix) for suffix in ("json", "md", "csv")]
    protected = list(sources) + [d.get("source_path") for d in report["documents"]]
    protected += [u.get("path") for r in report["references"].values() for u in r.get("source_units", [])]
    # Validate every destination before publishing any table; never overwrite source data.
    for path in paths:
        if path.resolve() in {Path(p).resolve() for p in protected if p} or path.exists():
            raise FileExistsError("Output exists or conflicts with a source: " + str(path))
    directory.mkdir(parents=True, exist_ok=True)
    for path in paths:
        local_state.preflight_output(path, sources=protected)
    data = [table_row(row) for row in report["rows"]]
    headers = [label for _, label in COLUMNS]
    markdown = "# 引文核查汇总\n\n" + REPORT_VERSION + "\n\n"
    markdown += "此表包含审阅者的判断，并非自动语义证明；未找到支持仅限所列审阅范围。\n"
    markdown += "页码和段落是已审阅缓存的快照。缺失、摘要证据或未核实自然段不代表全文定位完成。\n\n"
    markdown += "| " + " | ".join(headers) + " |\n| " + " | ".join(["---"] * len(headers)) + " |\n"
    markdown += "".join("| " + " | ".join(markdown_cell(v) for v in row) + " |\n" for row in data)
    csv_buffer = io.StringIO(newline="")
    writer = csv.writer(csv_buffer)
    writer.writerow(headers)
    writer.writerows([[csv_cell(v) for v in row] for row in data])
    contents = [json.dumps(report, ensure_ascii=False, indent=2) + "\n", markdown, "\ufeff" + csv_buffer.getvalue()]
    for path, content in zip(paths, contents):
        local_state.atomic_text(path, content)
    return paths


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    draft = commands.add_parser("template")
    draft.add_argument("--input", required=True)
    draft.add_argument("--out", required=True)
    pages = commands.add_parser("evidence-pages")
    pages.add_argument("--pdf", required=True)
    pages.add_argument("--page", action="append", type=int, required=True)
    pages.add_argument("--out", required=True)
    report = commands.add_parser("report")
    report.add_argument("--review", required=True)
    report.add_argument("--outdir", required=True)
    for cmd in (draft, pages, report):
        cmd.add_argument("--approve-audit", action="store_true")
        cmd.add_argument("--acknowledge-token-cost", action="store_true")
    args = parser.parse_args(argv)
    if not args.approve_audit or not args.acknowledge_token_cost:
        print(json.dumps({"next_action": "ask_audit_consent_with_token_warning", "source_read": False,
                          "written": False, "zotero_written": False}))
        return 3
    if args.command == "report":
        payload = json.loads(Path(args.review).read_text(encoding="utf-8-sig"))
        result = build_report(payload, approved=True, cost_warning_acknowledged=True)
        paths = export_report(result, args.outdir, sources=[args.review])
        print(json.dumps({"complete": result["complete"], "semantic_complete": result["semantic_complete"],
                          "locator_complete": result["locator_complete"], "counts": result["counts"],
                          "outputs": [str(p) for p in paths], "zotero_written": False}, ensure_ascii=False))
        return 0 if result["complete"] else 2
    source = args.input if args.command == "template" else args.pdf
    local_state.preflight_output(args.out, sources=[source])
    if args.command == "template":
        payload = json.loads(Path(args.input).read_text(encoding="utf-8-sig"))
        result = template(payload, approved=True, cost_warning_acknowledged=True)
    else:
        result = {"source_units": extract_pages(args.pdf, args.page, approved=True, cost_warning_acknowledged=True)}
    local_state.atomic_text(args.out, json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"output": str(Path(args.out).resolve()), "zotero_written": False}))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, RuntimeError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
