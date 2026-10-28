"""Validated caller metadata and platform-exported RIS; standard library only."""
from copy import deepcopy
from pathlib import Path
import re
import urllib.parse
from html.parser import HTMLParser

import zotero as z


def checked_metadata(ref):
    from ingest import Library, Uncertain, match_reference
    entry = deepcopy(ref["metadata"])
    provenance = deepcopy(ref.get("metadata_provenance"))
    if not isinstance(entry, dict) or not isinstance(provenance, dict):
        raise Uncertain("Supplied metadata needs a record and metadata_provenance")
    kind = provenance.get("kind")
    if kind in {"publisher", "database"}:
        url = urllib.parse.urlparse(str(provenance.get("url") or ""))
        valid = url.scheme in {"http", "https"} and bool(url.hostname)
    elif kind == "source_export":
        valid = bool(provenance.get("path")) and provenance.get("format") == "RIS"
    elif kind == "user_verified":
        valid = bool(str(provenance.get("description") or "").strip())
    else:
        valid = False
    if not valid:
        raise Uncertain("Metadata needs a publisher/database URL, RIS export path, or explicit user verification")
    title = entry.get("title")
    creators = entry.get("creators")
    if not isinstance(title, str) or not title.strip() or not isinstance(creators, list) or not creators:
        raise Uncertain("Supplied metadata needs title and creators")
    for author in creators:
        if not isinstance(author, dict) or not (author.get("lastName") or author.get("family") or author.get("name")):
            raise Uncertain("Invalid author in supplied metadata")
        author["lastName"] = author.get("lastName") or author.get("family") or author.get("name")
        author["firstName"] = author.get("firstName") or author.get("given") or ""
    aliases = entry.get("title_aliases", [])
    if not isinstance(aliases, list) or any(not isinstance(t, str) for t in aliases):
        raise Uncertain("title_aliases must be a list of source-confirmed titles")
    entry["doi"] = z.normalize_doi(entry.get("doi") or entry.get("DOI"))
    entry["pmid"] = z.pmid_from_item(entry)
    abstract = entry.get("abstract") or ""
    if not isinstance(abstract, str):
        raise Uncertain("Supplied abstract must be text or null")
    abstract = z.metadata_text(abstract)
    identifier = re.fullmatch(r"PMID\s*:?\s*(\d{4,10})", abstract, re.I)
    if identifier:
        if entry["pmid"] and entry["pmid"] != identifier.group(1):
            raise Uncertain("Legacy abstract PMID conflicts with supplied identifier")
        entry["pmid"] = identifier.group(1)
    entry["abstract"] = "" if identifier or re.fullmatch(r"\d+", abstract) else abstract
    entry["year"] = z.year_from_date(str(entry.get("year") or entry.get("date") or ""))
    entry["_metadata_provenance"] = provenance
    if entry.get("ris_type", "JOUR") not in {"JOUR", "BOOK", "THES", "CONF", "CHAP", "RPRT", "GEN"}:
        raise Uncertain("Unsupported RIS document type")
    if any(ref.get(k) for k in ("doi", "DOI", "pmid", "PMID", "title")):
        candidate = {"key": "metadata", "library": Library().json(), "data": entry}
        matched, evidence = match_reference(ref, [candidate], Library())
        if not matched:
            raise Uncertain("Supplied metadata conflicts with or cannot verify source reference: " + str(evidence))
    if not entry.get("abstract"):
        entry.setdefault("_warnings", []).append("Abstract missing in supplied metadata; not fabricated")
    return entry


def references_from_ris(path: Path, *, encoding=None):
    text, codec = z.read_source_text(path, encoding)
    records, fields, raw_lines, last_tag = [], {}, [], None
    for line in text.splitlines():
        match = re.match(r"^([A-Z0-9]{2})\s{2}-\s?(.*)$", line)
        if not match:
            if line.strip():
                if last_tag is None or not fields:
                    raise ValueError("Invalid RIS: text outside record")
                fields[last_tag][-1] += " " + line.strip()
                raw_lines.append(line)
            continue
        tag, value = match.groups()
        if tag == "TY":
            if fields:
                raise ValueError("Invalid RIS: missing ER")
            fields, raw_lines = {}, []
        elif not fields:
            raise ValueError("Invalid RIS: missing TY")
        fields.setdefault(tag, []).append(value)
        raw_lines.append(line)
        last_tag = tag
        if tag != "ER":
            continue
        def first(*tags):
            return next((fields[t][0] for t in tags if fields.get(t)), "")
        creators = []
        for name in fields.get("AU", fields.get("A1", [])):
            parts = name.split(",", 1)
            creators.append({"lastName": parts[0].strip(), "firstName": parts[1].strip() if len(parts) > 1 else ""})
        abstract = first("AB", "N2")
        pmid = z.extract_pmid(" ".join(fields.get("M2", []) + fields.get("AN", []) + fields.get("UR", [])))
        if re.fullmatch(r"\s*PMID:\s*\d+\s*", abstract, re.I):
            pmid = pmid or z.extract_pmid(abstract)
            abstract = ""
        entry = {"ris_type": first("TY"), "title": first("TI", "T1"), "doi": z.normalize_doi(first("DO")),
                 "pmid": pmid, "creators": creators, "journal": first("JO", "JF", "T2"),
                 "year": z.year_from_date(first("PY", "Y1", "DA")), "volume": first("VL"),
                 "issue": first("IS"), "pages": first("SP") + ("-" + first("EP") if first("EP") else ""),
                 "abstract": z.metadata_text(abstract), "url": first("UR")}
        records.append({"raw_text": "\n".join(raw_lines), "label": first("ID") or None,
                        "doi": entry["doi"], "pmid": entry["pmid"], "title": entry["title"],
                        "creators": creators, "year": entry["year"], "source_encoding": codec,
                        "metadata": entry, "metadata_provenance": {
                            "kind": "source_export", "format": "RIS", "path": str(path.resolve())}})
        fields, raw_lines, last_tag = {}, [], None
    if fields:
        raise ValueError("Invalid RIS: incomplete record")
    return records


class MetadataCatalog:
    """Caller-provided verified records supplement references without losing source IDs."""
    def __init__(self, path: Path, fallback, *, encoding=None):
        import json
        from ingest import Library
        self.fallback = fallback
        if path.suffix.lower() == ".ris":
            records = references_from_ris(path, encoding=encoding)
        elif path.suffix.lower() == ".json":
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
            records = payload["records"]
        else:
            raise ValueError("Metadata catalog must be RIS or JSON")
        self.items = [{"key": str(n), "library": Library().json(), "data": checked_metadata(record)}
                      for n, record in enumerate(records)]

    def __call__(self, ref):
        from ingest import Library, Uncertain, match_reference
        if ref.get("metadata"):
            return checked_metadata(ref)
        found, evidence = match_reference(ref, self.items, Library())
        if evidence["ambiguous"]:
            raise Uncertain("Metadata catalog has conflicting or multiple candidates: " + str(evidence))
        if found:
            return deepcopy(found["data"])
        return self.fallback(ref)


def metadata_from_html(text, url, *, kind="publisher"):
    """Parse explicit citation metadata from a page the caller actually retrieved."""
    class CitationTags(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.fields = {}

        def handle_starttag(self, tag, attrs):
            if tag.lower() != "meta":
                return
            values = dict(attrs)
            name = (values.get("name") or values.get("property") or "").casefold()
            content = values.get("content")
            if name.startswith("citation_") and content:
                self.fields.setdefault(name, []).append(content.strip())

    parser = CitationTags()
    parser.feed(text)
    fields = parser.fields
    def first(name):
        return next(iter(fields.get("citation_" + name, [])), "")
    creators = []
    for name in fields.get("citation_author", []):
        parts = name.split(",", 1)
        creators.append({"lastName": parts[0].strip(), "firstName": parts[1].strip() if len(parts) > 1 else ""})
    entry = {"title": first("title"), "creators": creators, "doi": z.normalize_doi(first("doi")),
             "pmid": z.normalize_pmid(first("pmid")), "journal": first("journal_title"),
             "year": z.year_from_date(first("publication_date") or first("date")),
             "volume": first("volume"), "issue": first("issue"),
             "pages": first("firstpage") + ("-" + first("lastpage") if first("lastpage") else ""),
             "abstract": z.metadata_text(first("abstract")), "url": url}
    ref = {"metadata": entry, "metadata_provenance": {"kind": kind, "url": url}}
    checked_metadata(ref)
    return ref
