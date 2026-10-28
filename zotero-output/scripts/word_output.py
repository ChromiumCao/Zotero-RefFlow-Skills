"""Native fields and conservative OOXML patching; Word Refresh is a separate gate."""
from __future__ import annotations
import copy
import io
import json
import posixpath
import re
import uuid
import zipfile
from pathlib import Path
from common import OutputError, digest, write_json
from scan_sources import NS, W, fields, story_fields, package, paragraph_text, q, xml_module, xml_parse
from citation_plan import item_index, validate_plan
from docx_metadata import clean_metadata_parts, verify_metadata, inherit_source_identity

def citation_text(citation, records):
    members = []
    for key, ref in zip(citation["item_keys"], citation["references"]):
        opts = ref.get("options", {})
        if opts.get("author-only"): raise OutputError("Static numbered mode cannot render author-only citations")
        text = str(records[key]["number"])
        if opts.get("locator"): text += ", "+str(opts.get("label", "page"))+" "+str(opts["locator"])
        text = (str(opts.get("prefix", ""))+" "+text+" "+str(opts.get("suffix", ""))).strip()
        if text not in members: members.append(text)
    return "["+"; ".join(members)+"]"

def citation_runs(citation, records, static=False):
    return [run(citation_text(citation, records))] if static else field_runs(citation_code(citation, records), citation["text"])

def static_bibliography(plan, records):
    cited = {k for d in plan["documents"] for c in d["citations"] if c.get("action") == "replace" for k in c["item_keys"]}
    entries = [f"[{r['number']}] {r['static_text']}" for k,r in records.items() if k in cited or plan.get("preserve_uncited")]
    return ["References"]+entries if entries else []

def run(text, properties=None):
    et = xml_module()
    r = et.Element(q("r"))
    if properties is not None: r.append(copy.deepcopy(properties))
    for i,line in enumerate(text.split("\n")):
        if i: et.SubElement(r,q("br"))
        t = et.SubElement(r, q("t")); t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve"); t.text = line
    return r

def field_runs(code, text):
    et = xml_module()
    result = []
    for kind in ("begin", "code", "separate", "result", "end"):
        r = et.Element(q("r"))
        if kind == "code":
            el = et.SubElement(r, q("instrText")); el.set("{http://www.w3.org/XML/1998/namespace}space", "preserve"); el.text = " " + code + " "
        elif kind == "result": r = run(text)
        else: et.SubElement(r, q("fldChar")).set(q("fldCharType"), kind)
        result.append(r)
    return result

def citation_code(citation, records):
    items, seen = [], set()
    for key, ref in zip(citation["item_keys"], citation["references"]):
        row = records[key]
        opts = ref.get("options", {})
        signature = (key, json.dumps(opts, sort_keys=True))
        if signature in seen: continue
        seen.add(signature)
        data = copy.deepcopy(row["csl"]); data["id"] = row["uri"]
        items.append({"id": row["uri"], "uris": [row["uri"]], "itemData": data, **opts})
    payload = {"citationID": uuid.uuid4().hex[:12], "citationItems": items,
               "properties": {"noteIndex": citation.get("note_index", 0), **citation.get("properties", {})},
               "schema": "https://github.com/citation-style-language/schema/raw/master/csl-citation.json"}
    return "ADDIN ZOTERO_ITEM CSL_CITATION " + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

def slice_child(child, start, end):
    """Slice text-only runs/wrappers while retaining styles and hyperlink properties."""
    et = xml_module()
    clone = copy.deepcopy(child)
    allowed = {q(n) for n in ("r", "rPr", "t", "tab", "br", "cr", "hyperlink", "smartTag", "sdt", "sdtPr", "sdtContent")}
    if any(el.tag not in allowed and el.tag not in {n.tag for n in clone.iter() if any(a.tag in (q("rPr"),q("sdtPr")) for a in n.iterancestors())} for el in clone.iter()):
        raise OutputError("Citation splits a drawing/bookmark/field or unsupported wrapper; choose a safe XML anchor")
    position = 0
    for el in list(clone.iter()):
        if el.tag not in (q("t"),q("tab"),q("br"),q("cr")): continue
        value = (el.text or "") if el.tag == q("t") else "\t" if el.tag == q("tab") else "\n"
        lo, hi = max(start-position, 0), min(end-position, len(value))
        if hi <= lo: el.getparent().remove(el)
        elif el.tag == q("t"):
            el.text = value[lo:hi]; el.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        position += len(value)
    return clone if paragraph_text(clone) else None

def replace_span(p, start, end, replacement):
    if start < 0 or end <= start or end > len(paragraph_text(p)): raise OutputError("Invalid text span")
    children, before, after, pos = list(p), [], [], 0
    for child in children:
        length = len(paragraph_text(child)); stop = pos+length
        if stop <= start: before.append(child)
        elif pos >= end: after.append(child)
        else:
            if start > pos:
                prefix = slice_child(child, 0, start-pos)
                if prefix is not None: before.append(prefix)
            if end < stop:
                suffix = slice_child(child, end-pos, length)
                if suffix is not None: after.append(suffix)
            if length == 0: raise OutputError("Non-text content intersects a citation span")
        pos = stop
    for child in children: p.remove(child)
    for child in before+replacement+after: p.append(child)

def patch_docx(doc, plan, records):
    parts = package(doc["path"])
    roots = {name: xml_parse(parts[name]) for name in ("word/document.xml", "word/footnotes.xml", "word/endnotes.xml") if name in parts}
    if roots["word/document.xml"].xpath("//w:txbxContent|//w:ins|//w:del|//w:altChunk", namespaces=NS):
        raise OutputError("Repair tracked changes/textboxes/altChunk before automated DOCX conversion")
    for name, root in roots.items():
        paragraphs = list(root.iter(q("p")))
        for i, p in enumerate(paragraphs):
            selected = [c for c in doc["citations"] if c["part"] == name and c["paragraph"] == i and c.get("action") == "replace"]
            for c in sorted(selected, key=lambda c: c["start"], reverse=True):
                replacement = citation_runs(c, records, plan.get("citation_mode") == "static")
                if c.get("field"):
                    f = c["field"]
                    found = [v for v in fields(p) if v["child_start"] == f["child_start"] and v["code"] == f["code"]]
                    if len(found) != 1: raise OutputError("Original field boundaries changed")
                    for child in list(p)[f["child_start"]:f["child_end"]]: p.remove(child)
                    for j, child in enumerate(replacement): p.insert(f["child_start"]+j, child)
                else: replace_span(p, c["start"], c["end"], replacement)
            if any(r["part"] == name and r["start"] <= i < r["end"] for r in doc["bibliography_ranges"]):
                if p.xpath(".//w:sectPr|.//w:drawing|.//w:footnoteReference|.//w:endnoteReference", namespaces=NS):
                    raise OutputError("Bibliography removal would discard section/media/note content; narrow the range")
                parent=p.getparent(); parent.remove(p)
                if parent.tag==q("tc") and not any(c.tag==q("p") for c in parent): parent.append(xml_module().Element(q("p")))
        parts[name] = xml_module().tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
    return parts

def zip_bytes(parts):
    result = io.BytesIO()
    with zipfile.ZipFile(result, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in parts.items(): z.writestr(name, data)
    result.seek(0)
    return result

def document_prefs(parts, style, locale):
    et = xml_module()
    cp = "http://schemas.openxmlformats.org/officeDocument/2006/custom-properties"
    vt = "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"
    pr = "http://schemas.openxmlformats.org/package/2006/relationships"
    ct = "http://schemas.openxmlformats.org/package/2006/content-types"
    data = et.Element("data", {"data-version": "3"})
    et.SubElement(data, "session", id=uuid.uuid4().hex)
    et.SubElement(data, "style", id="http://www.zotero.org/styles/"+style, locale=locale,
                  hasBibliography="1", bibliographyStyleHasBeenSet="0")
    prefs = et.SubElement(data, "prefs")
    for name, value in (("fieldType","Field"), ("noteType","0"), ("automaticJournalAbbreviations","true")):
        et.SubElement(prefs, "pref", name=name, value=value)
    text = et.tostring(data, encoding="unicode")
    custom = xml_parse(parts["docProps/custom.xml"]) if "docProps/custom.xml" in parts else et.Element("{"+cp+"}Properties", nsmap={None:cp,"vt":vt})
    for p in list(custom):
        if (p.get("name") or "").startswith("ZOTERO_PREF"): custom.remove(p)
    pid = max([int(p.get("pid", "1")) for p in custom]+[1])+1
    for i in range(0,len(text),255):
        p = et.SubElement(custom,"{"+cp+"}property", name=f"ZOTERO_PREF_{i//255+1}", pid=str(pid), fmtid="{D5CDD505-2E9C-101B-9397-08002B2CF9AE}")
        et.SubElement(p,"{"+vt+"}lpwstr").text = text[i:i+255]; pid += 1
    parts["docProps/custom.xml"] = et.tostring(custom,xml_declaration=True,encoding="UTF-8",standalone=True)
    rels = xml_parse(parts["_rels/.rels"])
    kind = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/custom-properties"
    if not any(r.get("Type") == kind for r in rels):
        used = {r.get("Id") for r in rels}; n=1
        while f"rId{n}" in used: n+=1
        et.SubElement(rels,"{"+pr+"}Relationship",Id=f"rId{n}",Type=kind,Target="docProps/custom.xml")
    parts["_rels/.rels"] = et.tostring(rels,xml_declaration=True,encoding="UTF-8",standalone=True)
    types = xml_parse(parts["[Content_Types].xml"])
    if not any(t.get("PartName") == "/docProps/custom.xml" for t in types):
        et.SubElement(types,"{"+ct+"}Override",PartName="/docProps/custom.xml",ContentType="application/vnd.openxmlformats-officedocument.custom-properties+xml")
    parts["[Content_Types].xml"] = et.tostring(types,xml_declaration=True,encoding="UTF-8",standalone=True)

def reconstructed_docx(doc, records, static=False):
    try:
        from docx import Document
    except ImportError as exc: raise OutputError("Reconstructed DOCX creation requires python-docx") from exc
    document = Document()
    for row in doc["paragraphs"]:
        if any(r["part"] == row["part"] and r["start"] <= row["paragraph"] < r["end"] for r in doc["bibliography_ranges"]): continue
        p = document.add_paragraph()
        text = row["text"]
        heading = re.match(r"^(#{1,6})\s+(.*)$",text) if doc["format"] == "md" else None
        p.add_run(text)
        for c in sorted([c for c in doc["citations"] if c["paragraph"] == row["paragraph"] and c.get("action") == "replace"],key=lambda c:c["start"],reverse=True):
            replace_span(p._p,c["start"],c["end"],citation_runs(c,records,static))
        if heading and not any(c["paragraph"] == row["paragraph"] for c in doc["citations"]):
            p.text = heading[2]; p.style = f"Heading {min(len(heading[1]),3)}"
    stream = io.BytesIO(); document.save(stream); stream.seek(0)
    return stream

def append_bibliography(parts, plan, records):
    et = xml_module()
    root = xml_parse(parts["word/document.xml"])
    body = root.find("w:body",NS)
    position = len(body)-1 if len(body) and body[-1].tag == q("sectPr") else len(body)
    if plan.get("citation_mode") == "static":
        for i, text in enumerate(static_bibliography(plan, records)):
            p = et.Element(q("p")); p.append(run(text)); body.insert(position+i, p)
        parts["word/document.xml"] = et.tostring(root,xml_declaration=True,encoding="UTF-8",standalone=True)
        return
    title = et.Element(q("p")); title.append(run("References")); body.insert(position,title)
    p = et.Element(q("p"))
    cited = {key for d in plan["documents"] for c in d["citations"] if c.get("action") == "replace" for key in c.get("item_keys",[]) }
    uncited = [[r["uri"]] for key,r in records.items() if key not in cited] if plan.get("preserve_uncited") else []
    metadata = {"uncited": uncited, "omitted": [], "custom": []}
    code = "ADDIN ZOTERO_BIBL " + json.dumps(metadata,separators=(",", ":")) + " CSL_BIBLIOGRAPHY"
    used = list(records) if plan.get("preserve_uncited") else [key for key in records if key in cited]
    provisional = "\n".join(records[key]["csl"]["title"] for key in used)
    for r in field_runs(code, provisional): p.append(r)
    body.insert(position+1,p)
    parts["word/document.xml"] = et.tostring(root,xml_declaration=True,encoding="UTF-8",standalone=True)

def normalize_note_context(parts):
    et = xml_module()
    main = xml_parse(parts["word/document.xml"])
    refs = main.xpath("//w:footnoteReference|//w:endnoteReference",namespaces=NS)
    note_map = {(el.tag,el.get(q("id"))):i+1 for i,el in enumerate(refs)}
    for name, kind in (("word/footnotes.xml","footnote"),("word/endnotes.xml","endnote")):
        if name not in parts: continue
        root = xml_parse(parts[name])
        for note in root.iter(q(kind)):
            number = note_map.get((q(kind+"Reference"),note.get(q("id"))))
            if number is None: continue
            for p in note.iter(q("p")):
                for f in sorted(fields(p),key=lambda f:f["child_start"],reverse=True):
                    if "ZOTERO_ITEM CSL_CITATION" not in f["code"]: continue
                    payload=json.loads(f["code"].split("CSL_CITATION",1)[1]); payload["properties"]["noteIndex"]=number
                    replacement=field_runs("ADDIN ZOTERO_ITEM CSL_CITATION "+json.dumps(payload,ensure_ascii=False),f["text"])
                    for child in list(p)[f["child_start"]:f["child_end"]]: p.remove(child)
                    for i,child in enumerate(replacement): p.insert(f["child_start"]+i,child)
        parts[name]=et.tostring(root,xml_declaration=True,encoding="UTF-8",standalone=True)

def build_docx(plan, bundle, out):
    validate_plan(plan,out)
    records = item_index(plan,bundle)
    path = Path(out).resolve()
    if path.exists(): raise OutputError("Output already exists; choose a new filename")
    if Path(str(path)+".manifest.json").exists(): raise OutputError("Output manifest already exists")
    if plan["mode"] == "bibliography":
        from docx import Document
        empty=io.BytesIO(); Document().save(empty); empty.seek(0); streams=[empty]
    else:
        streams=[]
        for doc in plan["documents"]:
            streams.append(zip_bytes(patch_docx(doc,plan,records)) if doc["format"] == "docx" else reconstructed_docx(doc,records,plan.get("citation_mode") == "static"))
    rich = [d for d in plan["documents"] if d["format"] == "docx"]
    if len(streams) > 1:
        from docx import Document
        if len(rich) > 1:
            try: from docxcompose.composer import Composer
            except ImportError as exc: raise OutputError("Multiple rich DOCX files require docxcompose or an explicitly reviewed alternate merge") from exc
            for doc in rich:
                pp=package(doc["path"])
                if "word/endnotes.xml" in pp or any(n.startswith(("word/comments", "word/embeddings/")) for n in pp):
                    raise OutputError("Rich merge contains endnotes/comments/embedded objects unsupported by bundled composer; use a trusted alternate merge")
            master_parts=package(plan["documents"][0]["path"]) if plan["documents"][0]["format"]=="docx" else {}
            master_headers={paragraph_text(xml_parse(v)) for n,v in master_parts.items() if re.fullmatch(r"word/(?:header|footer)\d+\.xml",n)}
            for doc in rich:
                if doc is plan["documents"][0]: continue
                for name,data in package(doc["path"]).items():
                    if re.fullmatch(r"word/(?:header|footer)\d+\.xml",name):
                        text=paragraph_text(xml_parse(data))
                        if text.strip() and text not in master_headers:
                            raise OutputError("Source-specific header/footer would be lost by composer; use a merge retaining section relationships")
            if not plan.get("composition_reviewed"):
                raise OutputError("Review multi-DOCX composition: master headers/footers and section layout; set composition_reviewed=true")
            composer=Composer(Document(streams[0]))
            for stream in streams[1:]: composer.append(Document(stream))
            merged=io.BytesIO(); composer.save(merged); merged.seek(0)
        elif rich:
            # The rich source remains the master. Other text documents carry no relationships.
            master_index=next(i for i,d in enumerate(plan["documents"]) if d["format"]=="docx")
            master=Document(streams[master_index]); prefix_position=0
            for i,stream in enumerate(streams):
                if i==master_index: continue
                other=Document(stream)
                for child in list(other.element.body):
                    if child.tag==q("sectPr"): continue
                    if i<master_index:
                        master.element.body.insert(prefix_position,copy.deepcopy(child)); prefix_position+=1
                    else: master.element.body.insert(len(master.element.body)-1,copy.deepcopy(child))
            merged=io.BytesIO(); master.save(merged); merged.seek(0)
        else:
            master=Document(streams[0])
            for stream in streams[1:]:
                for child in list(Document(stream).element.body):
                    if child.tag != q("sectPr"): master.element.body.insert(len(master.element.body)-1,copy.deepcopy(child))
            merged=io.BytesIO(); master.save(merged); merged.seek(0)
    else: merged=streams[0]
    with zipfile.ZipFile(merged) as z: parts={n:z.read(n) for n in z.namelist()}
    normalize_note_context(parts)
    append_bibliography(parts,plan,records)
    if plan.get("citation_mode") == "static":
        if "docProps/custom.xml" in parts:
            custom = xml_parse(parts["docProps/custom.xml"])
            for node in list(custom):
                if node.get("name", "").startswith("ZOTERO_PREF"): custom.remove(node)
            parts["docProps/custom.xml"] = xml_module().tostring(custom,xml_declaration=True,encoding="UTF-8",standalone=True)
    else: document_prefs(parts,plan["style"],plan.get("locale","en-US"))
    inherit_source_identity(parts,plan["documents"])
    clean_metadata_parts(parts)
    path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+"."+uuid.uuid4().hex+".building")
    try:
        temporary.write_bytes(zip_bytes(parts).getvalue())
        report=verify_docx(temporary,plan,bundle)
        temporary.replace(path)
    finally:
        if temporary.exists(): temporary.unlink()
    report.update(document=str(path),sha256=digest(path),integration="static; no live Zotero fields" if plan.get("citation_mode") == "static" else "pending Word Zotero Refresh and library-link verification")
    write_json(str(path)+".manifest.json",report)
    return report

def verify_docx(path,plan,bundle):
    expected_count=validate_plan(plan)
    records=item_index(plan,bundle)
    parts=package(path)
    metadata_clean=verify_metadata(parts)
    required_parts = ["word/document.xml","[Content_Types].xml","_rels/.rels"]
    if plan.get("citation_mode") != "static": required_parts.append("docProps/custom.xml")
    for required in required_parts:
        if required not in parts: raise OutputError("Required DOCX part missing: "+required)
    # XML and OPC relationships, including drawings, notes and numbering targets.
    for name,data in parts.items():
        if name.endswith((".xml",".rels")): xml_parse(data)
        if not name.endswith(".rels"): continue
        folder=posixpath.dirname(name).replace("/_rels","") if name != "_rels/.rels" else ""
        for rel in xml_parse(data):
            if rel.get("TargetMode") == "External": continue
            target=urllib_unquote(rel.get("Target",""))
            absolute=target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join(folder,target))
            if absolute not in parts: raise OutputError("Missing relationship target: "+absolute)
    if plan.get("citation_mode") == "static":
        bibliography = static_bibliography(plan,records)
        paragraphs = list(xml_parse(parts["word/document.xml"]).iter(q("p")))
        if bibliography and [paragraph_text(p) for p in paragraphs[-len(bibliography):]] != bibliography:
            raise OutputError("Static bibliography changed")
        for name in parts:
            if name.startswith("word/") and name.endswith(".xml"):
                for fs in story_fields(list(xml_parse(parts[name]).iter(q("p")))).values():
                    if any(any(tag in f["code"] for tag in ("ZOTERO_ITEM", "ZOTERO_BIBL", "EN.CITE")) for f in fs):
                        raise OutputError("Static output contains live/foreign citation fields")
        return {"schema_version":"zotero-output.verification/1", "citation_mode":"static",
                "structural_verified":True,"source_prose_verified":verify_prose(parts,plan),
                "metadata_clean":metadata_clean,"citation_fields":0,"bibliography_fields":0,
                "word_refresh_verified":False,"library_links_verified":False}
    ids,clusters,bibliographies,uris=set(),[],[],set()
    known={r["uri"] for r in records.values()}
    for name in ("word/document.xml","word/footnotes.xml","word/endnotes.xml"):
        if name not in parts: continue
        root=xml_parse(parts[name])
        all_fields=story_fields(list(root.iter(q("p"))))
        for paragraph_fields in all_fields.values():
            for f in paragraph_fields:
                code=f["code"].strip()
                if "EN.CITE" in code or ("CSL_CITATION" in code and "ZOTERO_ITEM" not in code): raise OutputError("Unconverted foreign citation field")
                if "ZOTERO_ITEM CSL_CITATION" in code:
                    if f.get("invalid"): raise OutputError("Malformed Zotero field")
                    data=json.loads(code.split("CSL_CITATION",1)[1])
                    cid=data.get("citationID")
                    if not cid or cid in ids: raise OutputError("Missing/duplicate citationID")
                    ids.add(cid)
                    cluster=[]
                    for item in data["citationItems"]:
                        item_uris=item.get("uris",[])
                        matching=[u for u in item_uris if u in known]
                        if len(matching)!=1: raise OutputError("Field not connected to one adopted destination item")
                        from common import validate_uri
                        bound=next(r for r in records.values() if r["uri"]==matching[0])
                        for uri in item_uris: validate_uri(uri,bound["item"])
                        if not item.get("itemData",{}).get("title"): raise OutputError("Field missing embedded bibliographic data")
                        uris.add(matching[0]); cluster.append(matching[0])
                    clusters.append(cluster)
                elif "ZOTERO_BIBL" in code:
                    data=json.loads(code.split("ZOTERO_BIBL",1)[1].split("CSL_BIBLIOGRAPHY",1)[0].strip())
                    bibliographies.append(data)
    if len(ids)!=expected_count: raise OutputError(f"Citation field count differs: {len(ids)} != {expected_count}")
    if len(bibliographies)!=1: raise OutputError("Expected exactly one dynamic bibliography field")
    expected=[]
    for doc in plan["documents"]:
        for c in sorted(doc["citations"],key=lambda c:(c["part"]!="word/document.xml" and c["part"]!="text", c["part"],c["paragraph"],c["start"])):
            if plan["mode"] == "manuscript" and c.get("action") == "replace":
                seen=set(); cluster=[]
                for key,ref in zip(c["item_keys"],c["references"]):
                    sig=(key,json.dumps(ref.get("options",{}),sort_keys=True))
                    if sig not in seen: cluster.append(records[key]["uri"]); seen.add(sig)
                expected.append(cluster)
    # Preserve cluster identity multiplicity even if Word reorders a cluster by style.
    if sorted(map(lambda c:tuple(sorted(c)),clusters))!=sorted(map(lambda c:tuple(sorted(c)),expected)):
        raise OutputError("Citation cluster identities/multiplicity changed")
    expected_uncited={r["uri"] for r in records.values()}-uris if plan.get("preserve_uncited") else set()
    actual_uncited={u for group in bibliographies[0].get("uncited",[]) for u in group}
    if actual_uncited!=expected_uncited: raise OutputError("Uncited bibliography membership changed")
    custom=xml_parse(parts["docProps/custom.xml"])
    chunks=sorted([p for p in custom if re.fullmatch(r"ZOTERO_PREF_\d+",p.get("name",""))],key=lambda p:int(p.get("name").rsplit("_",1)[1]))
    prefs=xml_parse("".join("".join(p.itertext()) for p in chunks).encode("utf-8"))
    if prefs.find("style").get("id")!="http://www.zotero.org/styles/"+plan["style"]: raise OutputError("Document style mismatch")
    prose_verified=verify_prose(parts,plan)
    return {"schema_version":"zotero-output.verification/1","structural_verified":True,
            "source_prose_verified":prose_verified,"metadata_clean":metadata_clean,
            "citation_fields":len(ids),"bibliography_fields":1,"item_uris":sorted(uris|actual_uncited),
            "citation_ids":sorted(ids),"sources":[{"path":d["path"],"sha256":d["sha256"]} for d in plan["documents"]],
            "item_aliases":plan["item_aliases"],"source_bindings":plan["source_bindings"],
            "uncited_source_items":[k for k,r in records.items() if r["uri"] not in uris],
            "word_refresh_verified":False,"library_links_verified":False}

def urllib_unquote(value):
    from urllib.parse import unquote
    return unquote(value).split("#",1)[0]

def verify_prose(parts,plan):
    """Detect missing/changed prose and moved citations, even after numbers refresh."""
    if plan["mode"]=="bibliography": return None
    def clean(text): return " ".join(text.split())
    static = plan.get("citation_mode") == "static"
    records = item_index(plan,{}) if static else None
    for part in ("word/document.xml","word/footnotes.xml","word/endnotes.xml"):
        expected=[]
        for doc in plan["documents"]:
            for row in doc["paragraphs"]:
                source_part="word/document.xml" if row["part"]=="text" else row["part"]
                if source_part!=part: continue
                if any(r["part"]==row["part"] and r["start"]<=row["paragraph"]<r["end"] for r in doc["bibliography_ranges"]): continue
                text=row["text"]
                selected=sorted([c for c in doc["citations"] if c["part"]==row["part"] and c["paragraph"]==row["paragraph"] and c.get("action")=="replace"],key=lambda c:c["start"],reverse=True)
                for c in selected: text=text[:c["start"]]+(citation_text(c,records) if static else "{ZOTERO_CITATION}")+text[c["end"]:]
                if doc["format"]=="md" and not selected: text=re.sub(r"^#{1,6}\s+","",text)
                if clean(text): expected.append(clean(text))
        actual=[]
        if part in parts:
            root=xml_parse(parts[part]); paragraphs=list(root.iter(q("p"))); all_fields=story_fields(paragraphs)
            excluded=set()
            if static and part == "word/document.xml":
                bibliography = static_bibliography(plan,records)
                if bibliography:
                    if [paragraph_text(p) for p in paragraphs[-len(bibliography):]] != bibliography:
                        raise OutputError("Static bibliography changed")
                    excluded.update(range(len(paragraphs)-len(bibliography),len(paragraphs)))
            for pi,fs in all_fields.items():
                for f in fs:
                    if "ZOTERO_BIBL" in f["code"]: excluded.update(range(pi,f.get("paragraph_end",pi+1)))
            # The writer adds precisely one plain References heading before its bibliography.
            if part=="word/document.xml" and excluded:
                first=min(excluded)
                if first>0 and paragraph_text(paragraphs[first-1])=="References": excluded.add(first-1)
            for pi,p in enumerate(paragraphs):
                if pi in excluded: continue
                note=next((a for a in p.iterancestors() if a.tag in (q("footnote"),q("endnote"))),None)
                if note is not None and int(note.get(q("id"),"0"))<=0: continue
                text=paragraph_text(p)
                for f in sorted(all_fields.get(pi,[]),key=lambda f:f["start"],reverse=True):
                    if "ZOTERO_ITEM CSL_CITATION" in f["code"]: text=text[:f["start"]]+"{ZOTERO_CITATION}"+text[f["end"]:]
                if clean(text): actual.append(clean(text))
        if actual!=expected: raise OutputError("Prose/citation positions were changed by composition or editing: "+part)
    return True
