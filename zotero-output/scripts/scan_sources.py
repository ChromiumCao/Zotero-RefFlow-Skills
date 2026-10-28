"""Read original citation relationships before any conversion or import."""
from __future__ import annotations
import json
import re
import shutil
import subprocess
import zipfile
from pathlib import Path
from common import OutputError, digest, doi, normalized

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
NS = {"w": W}
def q(name): return "{" + W + "}" + name

def xml_module():
    try:
        from lxml import etree
        return etree
    except ImportError as exc:
        raise OutputError("DOCX XML operations require lxml; use an available document runtime") from exc

def xml_parse(data):
    et = xml_module()
    return et.fromstring(data, et.XMLParser(resolve_entities=False, no_network=True))

def paragraph_text(p):
    # Field instructions are not visible text. Tabs/breaks count as one character.
    result = []
    for el in p.iter():
        if el.tag == q("t"): result.append(el.text or "")
        elif el.tag == q("tab"): result.append("\t")
        elif el.tag in (q("br"), q("cr")): result.append("\n")
    return "".join(result)

def superscript_spans(p):
    spans, offset = [], 0
    for r in p.iter(q("r")):
        text = paragraph_text(r)
        if text and r.find("w:rPr/w:vertAlign[@w:val='superscript']", NS) is not None:
            if spans and spans[-1]["end"] == offset:
                spans[-1]["text"] += text; spans[-1]["end"] += len(text)
            else: spans.append({"start":offset,"end":offset+len(text),"text":text})
        offset += len(text)
    return spans

def package(path):
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        if len(names) != len(set(names)) or any(n.startswith("/") or ".." in n.split("/") for n in names):
            raise OutputError("Unsafe/duplicate DOCX package parts")
        if z.testzip(): raise OutputError("Corrupt DOCX ZIP")
        return {n: z.read(n) for n in names}

def fields(p):
    """Return direct paragraph fields, including split instrText runs."""
    result, stack, offset = [], [], 0
    for i, child in enumerate(p):
        chars = list(child.iter(q("fldChar")))
        for c in chars:
            kind = c.get(q("fldCharType"))
            if kind == "begin": stack.append({"child_start": i, "start": offset, "code": "", "nested": bool(stack)})
            elif kind == "separate" and stack: stack[-1]["separate"] = True
            elif kind == "end":
                if not stack: raise OutputError("Unbalanced field end")
                f = stack.pop()
                f.update(child_end=i + 1, end=offset + len(paragraph_text(child)))
                f["text"] = "".join(paragraph_text(c) for c in list(p)[f["child_start"]:f["child_end"]])
                if not f.get("separate"): f["invalid"] = "missing field separator"
                result.append(f)
        for text in child.iter(q("instrText")):
            if stack: stack[-1]["code"] += text.text or ""
        if child.tag == q("fldSimple"):
            result.append({"child_start": i, "child_end": i+1, "start": offset,
                           "end": offset+len(paragraph_text(child)), "code": child.get(q("instr"), ""),
                           "text": paragraph_text(child), "simple": True})
        offset += len(paragraph_text(child))
    if stack: raise OutputError("Cross-paragraph or unclosed field; repair source before conversion")
    return sorted(result, key=lambda f: f["child_start"])

def story_fields(paragraphs):
    """Fields may span paragraphs, especially a normal Zotero bibliography/TOC."""
    result, stack = {}, []
    for pi,p in enumerate(paragraphs):
        offset=0
        for ci,child in enumerate(p):
            for c in child.iter(q("fldChar")):
                kind=c.get(q("fldCharType"))
                if kind=="begin": stack.append({"paragraph_start":pi,"child_start":ci,"start":offset,"code":"","nested":bool(stack)})
                elif kind=="separate" and stack: stack[-1]["separate"]=True
                elif kind=="end":
                    if not stack: raise OutputError("Unbalanced story field end")
                    f=stack.pop(); f.update(child_end=ci+1,end=offset+len(paragraph_text(child)))
                    if not f.get("separate"): f["invalid"]="missing field separator"
                    if f["paragraph_start"]!=pi:
                        if "CSL_CITATION" in f["code"] or "EN.CITE" in f["code"]:
                            raise OutputError("Citation spans paragraphs; repair citation before conversion")
                        if "ZOTERO_BIBL" in f["code"] or "EN.REFLIST" in f["code"]:
                            f["paragraph_end"]=pi+1; f["text"]=""
                            result.setdefault(f["paragraph_start"],[]).append(f)
                    else:
                        f["text"]="".join(paragraph_text(c) for c in list(p)[f["child_start"]:f["child_end"]])
                        result.setdefault(pi,[]).append(f)
            for text in child.iter(q("instrText")):
                if stack: stack[-1]["code"]+=text.text or ""
            if child.tag==q("fldSimple"):
                result.setdefault(pi,[]).append({"child_start":ci,"child_end":ci+1,"start":offset,
                    "end":offset+len(paragraph_text(child)),"text":paragraph_text(child),"code":child.get(q("instr"),""),"simple":True})
            offset+=len(paragraph_text(child))
    if stack: raise OutputError("Unclosed document field")
    return result

def csl_ref(data, raw=None):
    authors = data.get("author", [])
    date = data.get("issued", {}).get("date-parts", [[]])
    return {"raw_text": raw or data.get("title"), "title": data.get("title"),
            "doi": doi(data.get("DOI")), "pmid": data.get("PMID"),
            "first_author": authors[0].get("family") if authors else None,
            "year": str(date[0][0]) if date and date[0] else None,
            "csl": data}

def endnote_refs(code):
    start, end = code.find("<EndNote"), code.rfind("</EndNote>")
    if start < 0 or end < 0: raise OutputError("Unsupported EndNote field without embedded EndNote XML")
    root = xml_parse(code[start:end+len("</EndNote>")].encode("utf-8"))
    refs = []
    for cite in root.iter("Cite"):
        record = cite.find("record")
        if record is None: raise OutputError("EndNote Cite has no embedded record")
        def text(name): return "".join(record.find(name).itertext()).strip() if record.find(name) is not None else None
        title = text("titles/title")
        if not title: raise OutputError("EndNote record has no title")
        authors = record.findall("contributors/authors/author")
        refs.append({"raw_text": " ".join(record.itertext()), "title": title,
                     "doi": doi(text("electronic-resource-num")), "year": text("dates/year"),
                     "first_author": "".join(authors[0].itertext()).split(",")[0] if authors else None,
                     "foreign_record_id": cite.findtext("RecNum"),
                     "options": {"locator": cite.findtext("Pages"), "prefix": cite.findtext("Prefix"),
                                 "suffix": cite.findtext("Suffix")}})
    if not refs: raise OutputError("EndNote field contains no readable citations")
    return refs

def numeric_labels(text):
    value = text.strip("[](){} ").replace("–", "-").replace("—", "-").replace("，", ",")
    labels = []
    for token in re.split(r"[,;、\s]+", value):
        if not token: continue
        if "-" in token:
            a, b = token.split("-", 1)
            if not (a.isdigit() and b.isdigit() and 0 < int(a) <= int(b) and int(b)-int(a) <= 1000):
                raise OutputError("Invalid citation range: " + text)
            labels.extend(str(i) for i in range(int(a), int(b)+1))
        elif token.isdigit(): labels.append(str(int(token)))
        else: raise OutputError("Invalid numeric marker: " + text)
    return labels

HEADING = re.compile(r"^\s*(?:#{1,6}\s*)?(?:references?|bibliography|参考文献|参考资料)\s*[:：]?\s*$", re.I)
ENTRY = re.compile(r"^\s*(?:\[(\d+)\]|(\d+)[.)、]|\[\^([^\]]+)\]:)\s*(.*)$")
NUMBER = re.compile(r"\[(\d+(?:\s*[-–—,，;、]\s*\d+)*)\]")
AUTHOR_DATE = re.compile(r"\(([^()\n]{2,160}\b(?:19|20)\d{2}[a-z]?[^()\n]{0,50})\)")
OPTIONS = ("locator", "label", "prefix", "suffix", "suppress-author", "author-only", "position")

def read_paragraphs(path):
    suffix = path.suffix.casefold()
    if suffix == ".docx":
        parts = package(path)
        rows, issues = [], []
        root = xml_parse(parts["word/document.xml"])
        if root.xpath("//w:txbxContent|//w:ins|//w:del|//w:altChunk", namespaces=NS):
            issues.append("Textboxes, tracked changes or altChunk require source repair before automated build")
        if any(n.startswith("word/header") or n.startswith("word/footer") for n in parts):
            # Headers remain intact in single-source patching; rich composer needs review.
            for name in [n for n in parts if re.fullmatch(r"word/(?:header|footer)\d+\.xml", n)]:
                if b"CSL_CITATION" in parts[name] or b"EN.CITE" in parts[name]:
                    issues.append("Citations in headers/footers are unsupported; relocate explicitly")
        for name in ("word/document.xml", "word/footnotes.xml", "word/endnotes.xml"):
            if name not in parts: continue
            story = xml_parse(parts[name])
            rel_name=str(Path(name).parent).replace("\\","/")+"/_rels/"+Path(name).name+".rels"
            rels={r.get("Id"):r.get("Target") for r in xml_parse(parts[rel_name]) if r.get("TargetMode")=="External"} if rel_name in parts else {}
            all_paragraphs=list(story.iter(q("p")))
            all_fields=story_fields(all_paragraphs)
            for i, p in enumerate(all_paragraphs):
                note = next((a for a in p.iterancestors() if a.tag in (q("footnote"), q("endnote"))), None)
                note_id = int(note.get(q("id"), "0")) if note is not None else 0
                if note is not None and note_id <= 0: continue
                rows.append({"part": name, "paragraph": i, "text": paragraph_text(p),
                             "note_index": note_id, "fields": all_fields.get(i,[]), "superscripts": superscript_spans(p),
                             "hyperlinks":[rels[h.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id")]
                                           for h in p.iter(q("hyperlink")) if h.get("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id") in rels]})
        return rows, issues
    if suffix in (".md", ".txt"):
        text = path.read_text(encoding="utf-8-sig")
    elif suffix == ".pdf":
        try:
            import pymupdf
            with pymupdf.open(path) as pdf:
                text = "\n".join(page.get_text(sort=True) for page in pdf)
        except ImportError:
            command = shutil.which("pdftotext")
            if not command: raise OutputError("PDF extraction requires PyMuPDF or pdftotext; OCR needed for scans")
            text = subprocess.run([command, "-enc", "UTF-8", str(path), "-"], check=True, capture_output=True).stdout.decode("utf-8")
        if len(text.strip()) < 40: raise OutputError("PDF has insufficient text; obtain and review OCR first")
    else: raise OutputError("Unsupported source format: " + suffix)
    # Line positions remain stable and source-specific; Markdown markup is retained.
    return [{"part": "text", "paragraph": i, "text": line, "note_index": 0, "fields": [], "superscripts": []}
            for i, line in enumerate(text.splitlines())], []

def scan_one(path, reference_list=False):
    path = Path(path).resolve()
    doc_id = path.as_uri()
    paragraphs, issues = read_paragraphs(path)
    refs, citations, ranges = [], [], []
    main = [p for p in paragraphs if p["part"] in ("text", "word/document.xml")]
    headings = [p["paragraph"] for p in main if HEADING.fullmatch(p["text"])]
    if len(headings) > 1: issues.append("Multiple bibliography headings; review individual ranges")
    first = headings[0] if headings else None
    bibliography = main if reference_list and first is None else [p for p in main if first is not None and p["paragraph"] > first]
    if reference_list and first is None and main:
        ranges.append({"part":main[0]["part"],"start":0,"end":main[-1]["paragraph"]+1,"reviewed":False})
    if first is not None:
        ranges.append({"part": main[0]["part"], "start": first, "end": main[-1]["paragraph"]+1,
                       "reviewed": False})
    for p in paragraphs:
        for f in p["fields"]:
            if "ZOTERO_BIBL" in f["code"] or "EN.REFLIST" in f["code"]:
                ranges.append({"part":p["part"],"start":p["paragraph"],"end":f.get("paragraph_end",p["paragraph"]+1),"reviewed":False})
    bibliography=[p for p in main if any(r["part"]==p["part"] and r["start"]<=p["paragraph"]<r["end"] for r in ranges)
                  and not HEADING.fullmatch(p["text"])]
    labels = {}
    for p in bibliography:
        if not p["text"].strip() or any("ZOTERO_BIBL" in f["code"] or "EN.REFLIST" in f["code"] for f in p["fields"]): continue
        m = ENTRY.match(p["text"])
        label = next((g for g in m.groups()[:3] if g), None) if m else None
        raw = m[4] if m else p["text"]
        # Continuations only for a bibliography which has explicit numbered entries.
        if not m and refs and labels:
            refs[-1]["raw_text"] += "\n" + raw
            refs[-1]["doi"] = refs[-1]["doi"] or doi(raw) or next((doi(u) for u in p.get("hyperlinks",[]) if doi(u)),None)
            continue
        rid = f"ref-{len(refs)+1:06d}"
        year = re.search(r"\b((?:19|20)\d{2}[a-z]?)\b", raw)
        ref = {"reference_id": rid, "raw_text": raw, "label": label, "doi": doi(raw) or next((doi(u) for u in p.get("hyperlinks",[]) if doi(u)),None),
               "year": year[1][:4] if year else None, "year_label": year[1] if year else None,
               "first_author": re.split(r"[,\s.]", raw.strip())[0] or None,
               "source_location": {"part": p["part"], "paragraph": p["paragraph"]}}
        refs.append(ref)
        if label:
            if label in labels: issues.append("Duplicate bibliography label " + label)
            else: labels[label] = rid
    native_by_signature = {}
    def add_native(data, raw):
        sig = json.dumps({k: data.get(k) for k in ("doi", "pmid", "title", "zotero_item", "foreign_uri")}, sort_keys=True)
        if sig in native_by_signature: return native_by_signature[sig]
        rid = f"ref-{len(refs)+1:06d}"
        data = dict(data, reference_id=rid, raw_text=data.get("raw_text") or raw)
        refs.append(data); native_by_signature[sig] = rid
        return rid
    for p in paragraphs:
        if any(r["part"]==p["part"] and r["start"]<=p["paragraph"]<r["end"] for r in ranges): continue
        protected = []
        for f in p["fields"]:
            code = f["code"].strip()
            if "ZOTERO_BIBL" in code or "EN.REFLIST" in code:
                ranges.append({"part": p["part"], "start": p["paragraph"], "end": f.get("paragraph_end",p["paragraph"]+1), "reviewed": False})
                continue
            if not any(tag in code for tag in ("CSL_CITATION", "EN.CITE")): continue
            protected.append((f["start"], f["end"]))
            entry = {"citation_id": f"cite-{len(citations)+1:06d}", "part": p["part"], "paragraph": p["paragraph"],
                     "start": f["start"], "end": f["end"], "text": f["text"], "field": f,
                     "note_index": p["note_index"], "references": [], "reviewed": False}
            try:
                if f.get("nested") or f.get("invalid"): raise OutputError("Nested/malformed citation field")
                if "CSL_CITATION" in code:
                    data = json.loads(code.split("CSL_CITATION", 1)[1].strip())
                    entry["kind"] = "zotero" if "ZOTERO_ITEM" in code else "mendeley"
                    entry["properties"] = {k: v for k, v in data.get("properties", {}).items() if k in ("unsorted",)}
                    for item in data["citationItems"]:
                        ref = csl_ref(item.get("itemData", {}), f["text"])
                        uris = item.get("uris", [])
                        ref["foreign_uri"] = uris[0] if uris else None
                        # Foreign identity is provenance, not an import constraint.
                        rid = add_native(ref, f["text"])
                        entry["references"].append({"reference_id": rid, "options": {k: item[k] for k in OPTIONS if k in item}})
                    if not entry["references"]: raise OutputError("Empty citationItems")
                else:
                    entry["kind"] = "endnote"
                    for ref in endnote_refs(code):
                        opts = {k: v for k, v in ref.pop("options").items() if v}
                        entry["references"].append({"reference_id": add_native(ref, f["text"]), "options": opts})
            except (ValueError, KeyError, TypeError) as exc:
                entry["issue"] = str(exc)
                issues.append("Unsupported citation " + entry["citation_id"] + ": " + str(exc))
            citations.append(entry)
        for m in NUMBER.finditer(p["text"]):
            if any(a <= m.start() < b for a,b in protected): continue
            ns = numeric_labels(m.group())
            missing = [v for v in ns if v not in labels]
            citations.append({"citation_id": f"cite-{len(citations)+1:06d}", "kind": "numeric", "part": p["part"],
                              "paragraph": p["paragraph"], "start": m.start(), "end": m.end(), "text": m.group(),
                              "note_index": p["note_index"], "reviewed": False,
                              "references": [{"reference_id": labels[n], "options": {}} for n in ns if n in labels],
                              "issue": "Missing bibliography labels: " + ",".join(missing) if missing else None})
        for m in AUTHOR_DATE.finditer(p["text"]):
            if any(a <= m.start() < b for a,b in protected): continue
            found, ambiguous = [], False
            for cluster in m[1].split(";"):
                year = re.search(r"\b((?:19|20)\d{2}[a-z]?)\b", cluster)
                surname = re.split(r"[,\s]", cluster.strip())[0]
                matches = [r for r in refs if normalized(r.get("first_author")) == normalized(surname)
                           and r.get("year_label", r.get("year")) == year[1]] if year else []
                if len(matches) == 1: found.append({"reference_id": matches[0]["reference_id"], "options": {}})
                else: ambiguous = True
            citations.append({"citation_id": f"cite-{len(citations)+1:06d}", "kind": "author_date", "part": p["part"],
                              "paragraph": p["paragraph"], "start": m.start(), "end": m.end(), "text": m.group(),
                              "note_index": p["note_index"], "reviewed": False, "references": found,
                              "issue": "Author-date ambiguity; resolve against this document's bibliography" if ambiguous else None})
        supers = list(p["superscripts"])
        supers += [{"start":m.start(),"end":m.end(),"text":m.group().translate(str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹","0123456789"))}
                   for m in re.finditer(r"[⁰¹²³⁴⁵⁶⁷⁸⁹]+",p["text"])]
        for span in supers:
            if any(a <= span["start"] < b for a,b in protected): continue
            if any(c["part"]==p["part"] and c["paragraph"]==p["paragraph"] and max(c["start"],span["start"]) < min(c["end"],span["end"]) for c in citations): continue
            try: nums=numeric_labels(span["text"])
            except OutputError:
                issues.append(f"Review nonnumeric superscript at {p['part']} paragraph {p['paragraph']}"); continue
            missing=[n for n in nums if n not in labels]
            citations.append({"citation_id":f"cite-{len(citations)+1:06d}","kind":"superscript","part":p["part"],
                              "paragraph":p["paragraph"],"start":span["start"],"end":span["end"],
                              "text":p["text"][span["start"]:span["end"]],"note_index":p["note_index"],"reviewed":False,
                              "references":[{"reference_id":labels[n],"options":{}} for n in nums if n in labels],
                              "issue":"Missing superscript labels: "+",".join(missing) if missing else None,
                              "review_note":"Confirm citation, not an exponent, unit or author affiliation"})
    # Overlapping ranges from a heading and an existing bibliography field collapse.
    unique = []
    for r in ranges:
        if not any(v["part"] == r["part"] and v["start"] <= r["start"] and v["end"] >= r["end"] for v in unique): unique.append(r)
    return {"document_id": doc_id, "path": str(path), "sha256": digest(path), "format": path.suffix[1:].lower(),
            "paragraphs": paragraphs, "references": refs, "citations": citations,
            "bibliography_ranges": unique, "issues": issues, "issues_reviewed": False}

def scan(paths, reference_list=False):
    if not paths: raise OutputError("Supply at least one source")
    resolved = [Path(p).resolve() for p in paths]
    if len(resolved) != len(set(resolved)): raise OutputError("Repeated source path")
    docs = [scan_one(p,reference_list=reference_list) for p in resolved]
    return {"schema_version": "zotero-output.scan/1", "documents": docs}

def references_input(scan_result):
    documents = []
    for doc in scan_result["documents"]:
        refs = []
        for ref in doc["references"]:
            r = {k: v for k,v in ref.items() if k in ("reference_id", "raw_text", "label", "doi", "pmid", "title", "first_author", "year")}
            r["source_location"] = {"bibliography": ref.get("source_location"), "foreign_uri": ref.get("foreign_uri"),
                                    "citations": [c["citation_id"] for c in doc["citations"]
                                                  if any(t["reference_id"] == ref["reference_id"] for t in c["references"])]}
            refs.append(r)
        documents.append({"document_id": doc["document_id"], "references": refs})
    return {"schema_version": "zotero-input.references/1", "documents": documents}
