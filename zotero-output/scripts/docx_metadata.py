"""Conservatively remove automation credits; retain actual document identities."""
from __future__ import annotations
import io
import re
import uuid
import zipfile
from pathlib import Path
from common import OutputError, digest
from scan_sources import package, xml_module, xml_parse, W

APP = "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
CUSTOM = "http://schemas.openxmlformats.org/officeDocument/2006/custom-properties"
TOOLS = r"(?:python(?:[-_\s]?docx)?|codex|chatgpt|openai|claude|an\s+ai|ai)"
MODEL = re.compile(r"(?:openai\s+)?(?:codex|chatgpt|gpt[-_\s]?\d+(?:\.\d+)*(?:[-_\s](?:codex|mini|pro|sol|terra|astra|luna))?|claude(?:[-_\s](?:sonnet|opus|haiku))?(?:[-_\s]\d+(?:\.\d+)*)?|gemini(?:[-_\s]\d+(?:\.\d+)*)?(?:[-_\s](?:pro|flash))?|ai\s+assistant)", re.I)
CREDIT = re.compile(r"(?:generated|created|produced|authored|written)\s+(?:by|with|using)\s+(?:"
                    + TOOLS + "|" + MODEL.pattern + r")[.!。\s]*", re.I)


def _without_credit(text, *, tool_field=False, author_field=False):
    value = str(text or "")
    if tool_field and (re.fullmatch(TOOLS, value.strip(), re.I) or MODEL.fullmatch(value.strip())): return ""
    # The python-docx template's exact program name is known automation metadata.
    if (tool_field or author_field) and value.strip().casefold() == "python-docx": return ""
    if author_field:
        names = value.split(";")
        kept_names = [n for n in names if not (MODEL.fullmatch(n.strip()) or CREDIT.fullmatch(n.strip()))]
        if len(names) != len(kept_names): value = "; ".join(n.strip() for n in kept_names)
    lines = value.splitlines(keepends=True)
    kept = [line for line in lines if not CREDIT.fullmatch(line.strip())]
    return "".join(kept).rstrip("\r\n") if len(kept) != len(lines) else value


def clean_metadata_parts(parts):
    """Only remove unambiguous credit values/lines in document properties."""
    et = xml_module()
    changed = []
    for name in ("docProps/core.xml", "docProps/app.xml", "docProps/custom.xml"):
        if name not in parts: continue
        root = xml_parse(parts[name])
        dirty = False
        for node in list(root):
            tag = et.QName(node)
            property_name = node.get("name", "")
            if tag.namespace == CUSTOM:
                if property_name.upper().startswith("ZOTERO_"): continue
            # Keep structured/unknown values conservatively. Never scan Word
            # comments, revisions, disclosure statements or citation payloads.
            leaves = list(node) if tag.namespace == CUSTOM else [node]
            if len(leaves) != 1 or len(leaves[0]): continue
            leaf = leaves[0]
            before = leaf.text or ""
            label = re.sub(r"[\W_]+", "", property_name.casefold())
            tool_field = label in {"generator", "generatedby", "producer"} or (tag.namespace == APP and tag.localname == "Application")
            author_field = (name == "docProps/core.xml" and tag.localname in {"creator", "lastModifiedBy"}) or label in {"author","authors","creator","createdby","lastmodifiedby","lastsavedby","作者","最后修改者"}
            after = _without_credit(before, tool_field=tool_field, author_field=author_field)
            if before != after:
                if tag.namespace == CUSTOM and not after: root.remove(node)
                else: leaf.text = after or None
                dirty = True
        if dirty:
            parts[name] = et.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
            changed.append(name)
    for name in list(parts):
        if not (name.startswith("word/") and name.endswith(".xml")) or b":author=" not in parts[name]: continue
        root = xml_parse(parts[name]); dirty = False
        for node in root.iter():
            for key, value in list(node.attrib.items()):
                attr = et.QName(key)
                if attr.localname == "author" and (attr.namespace == W or str(attr.namespace).startswith("http://schemas.microsoft.com/office/word/")) and MODEL.fullmatch(value.strip()):
                    node.set(key, "")
                    for initials in list(node.attrib):
                        if et.QName(initials).localname == "initials": node.set(initials, "")
                    dirty = True
        if dirty:
            parts[name] = et.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
            changed.append(name)
    return changed


def inherit_source_identity(parts, documents):
    """Keep source-order author strings; retain a last editor only if agreed."""
    et = xml_module(); sources = []
    for doc in documents:
        if doc["format"] != "docx": continue
        source = package(doc["path"]); clean_metadata_parts(source)
        if "docProps/core.xml" in source: sources.append(xml_parse(source["docProps/core.xml"]))
    if not sources or "docProps/core.xml" not in parts: return
    root = xml_parse(parts["docProps/core.xml"])
    for field, ns in (("creator", "http://purl.org/dc/elements/1.1/"),
                      ("lastModifiedBy", "http://schemas.openxmlformats.org/package/2006/metadata/core-properties")):
        values = list(dict.fromkeys((e.text or "").strip() for s in sources for e in s
                                   if et.QName(e).localname == field and (e.text or "").strip()))
        value = "; ".join(values) if field == "creator" else values[0] if len(values) == 1 else ""
        nodes = [e for e in root if et.QName(e).localname == field]
        for e in nodes: root.remove(e)
        et.SubElement(root, "{"+ns+"}"+field).text = value or None
    parts["docProps/core.xml"] = et.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def verify_metadata(parts):
    probe = dict(parts)
    if clean_metadata_parts(probe):
        raise OutputError("Automation-credit metadata remains; clean metadata after the final save")
    return True


def clean_docx_metadata(document):
    """Explicit in-place cleanup, also used after Word's final save/close."""
    original = Path(document)
    if original.is_symlink(): raise OutputError("Metadata cleanup requires a regular DOCX file")
    path = original.resolve()
    if path.suffix.casefold() != ".docx": raise OutputError("Metadata cleanup requires DOCX")
    parts = package(path)
    changed = clean_metadata_parts(parts)
    verify_metadata(parts)
    if changed:
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, data in parts.items(): archive.writestr(name, data)
        temporary = path.with_name(path.name+"."+uuid.uuid4().hex+".metadata")
        try:
            temporary.write_bytes(stream.getvalue())
            verify_metadata(package(temporary))
            temporary.replace(path)
        finally:
            if temporary.exists(): temporary.unlink()
    return {"document": str(path), "sha256": digest(path), "metadata_clean": True,
            "changed_parts": changed}
