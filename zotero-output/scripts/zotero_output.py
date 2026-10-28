#!/usr/bin/env python3
"""Portable, staged citation reconstruction CLI. It never writes to Zotero."""
from __future__ import annotations
import argparse
import importlib.util
import json
import shutil
import sys
from pathlib import Path
from common import OutputError, digest, read_json, write_json
from scan_sources import scan, references_input
from citation_plan import fetch_items, item_index, make_plan, make_static_plan, validate_plan
from word_output import build_docx, verify_docx, citation_text, static_bibliography
from library_mapping import resolve_library
from docx_metadata import clean_docx_metadata

def build_markdown(plan,bundle,out):
    validate_plan(plan,out)
    records=item_index(plan,bundle)
    path=Path(out).resolve()
    if path.exists(): raise OutputError("Output already exists; choose a new filename")
    for sidecar in (path.with_suffix(".references.json"),Path(str(path)+".manifest.json")):
        if sidecar.exists(): raise OutputError("Sidecar already exists: "+str(sidecar))
        if sidecar in [Path(d["path"]).resolve() for d in plan["documents"]]: raise OutputError("Sidecar would overwrite a source")
    static = plan.get("citation_mode") == "static"
    keys={key:"zo_"+key.replace(":","_") for key in records}
    lines=["---","bibliography: "+json.dumps(path.with_suffix(".references.json").name),
           "csl: https://raw.githubusercontent.com/citation-style-language/styles/master/"+plan["style"]+".csl","---",""]
    if static: lines=[]
    if plan["mode"]=="bibliography":
        if not static: lines.insert(1,'nocite: "@*"')
    else:
        for doc in plan["documents"]:
            if doc["format"]=="docx":
                if not plan.get("markdown_reconstruction_reviewed"):
                    raise OutputError("DOCX to Markdown reconstructs text; review table/media/note handling explicitly")
                if any(p["part"]!="word/document.xml" for p in doc["paragraphs"]):
                    raise OutputError("DOCX notes need a reviewed Markdown footnote conversion")
            for p in doc["paragraphs"]:
                if any(r["part"]==p["part"] and r["start"]<=p["paragraph"]<r["end"] for r in doc["bibliography_ranges"]): continue
                text=p["text"]
                for c in sorted([c for c in doc["citations"] if c["part"]==p["part"] and c["paragraph"]==p["paragraph"] and c.get("action")=="replace"],key=lambda c:c["start"],reverse=True):
                    if static:
                        text=text[:c["start"]]+citation_text(c,records)+text[c["end"]:]; continue
                    members=[]
                    for key,ref in zip(c["item_keys"],c["references"]):
                        opts=ref.get("options",{})
                        if opts.get("author-only"): raise OutputError("author-only needs a reviewed Pandoc conversion")
                        marker=("-@" if opts.get("suppress-author") else "@")+keys[key]
                        if opts.get("locator"): marker+=", "+opts.get("label","page")+" "+str(opts["locator"])
                        members.append((opts.get("prefix","")+" "+marker+" "+opts.get("suffix","")).strip())
                    text=text[:c["start"]]+"["+"; ".join(dict.fromkeys(members))+"]"+text[c["end"]:]
                lines.extend([text,""])
    lines.extend(static_bibliography(plan,records) if static else ["# References", "", "::: {#refs}", ":::", ""])
    path.parent.mkdir(parents=True,exist_ok=True)
    csl=[]
    for key,row in records.items() if not static else []:
        entry=dict(row["csl"]); entry["id"]=keys[key]; csl.append(entry)
    if not static: write_json(path.with_suffix(".references.json"),csl)
    path.write_text("\n".join(lines),encoding="utf-8")
    if static:
        result={"schema_version":"zotero-output.manifest/1","citation_mode":"static","document":str(path),
                "sha256":digest(path),"format":"md","style":"source-text","requires":"No live Zotero fields; plain numbered text"}
        write_json(str(path)+".manifest.json",result)
        return result
    result={"schema_version":"zotero-output.manifest/1","document":str(path),"sha256":digest(path),
            "format":"md","citation_keys":keys,"item_uris":{keys[k]:r["uri"] for k,r in records.items()},
            "style":plan["style"],"requires":"Pandoc/citeproc rendering; the official remote CSL style may be cached locally for offline use; Markdown does not contain Word live fields",
            "sources":[{"path":d["path"],"sha256":d["sha256"]} for d in plan["documents"]]}
    write_json(str(path)+".manifest.json",result)
    return result

def doctor():
    return {"python":sys.version.split()[0],"modules":{m:bool(importlib.util.find_spec(m)) for m in ("lxml","docx","docxcompose","pymupdf")},
            "commands":{c:shutil.which(c) for c in ("pdftotext","pandoc")},
            "word_refresh":"requires actual Microsoft Word + Zotero plugin; presence of fields is insufficient"}

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest="command",required=True)
    commands.add_parser("doctor")
    m=commands.add_parser("clean-metadata"); m.add_argument("--document",required=True)
    s=commands.add_parser("scan"); s.add_argument("--source",action="append",required=True); s.add_argument("--out",required=True); s.add_argument("--references-out"); s.add_argument("--reference-list",action="store_true")
    r=commands.add_parser("references"); r.add_argument("--scan",required=True); r.add_argument("--out",required=True)
    r=commands.add_parser("resolve"); r.add_argument("--scan",required=True); r.add_argument("--out",required=True)
    destination=r.add_mutually_exclusive_group(required=True); destination.add_argument("--no-collection",action="store_true"); destination.add_argument("--collection")
    r.add_argument("--library-type",choices=("user","group"),default="user"); r.add_argument("--library-id",type=int,default=0); r.add_argument("--base-url",default="http://127.0.0.1:23119")
    p=commands.add_parser("plan"); p.add_argument("--scan",required=True); choice=p.add_mutually_exclusive_group(required=True); choice.add_argument("--mapping"); choice.add_argument("--static",action="store_true"); p.add_argument("--style",required=True); p.add_argument("--mode",choices=("manuscript","bibliography"),default="manuscript"); p.add_argument("--out",required=True)
    i=commands.add_parser("items"); i.add_argument("--plan",required=True); i.add_argument("--out",required=True); i.add_argument("--base-url",default="http://127.0.0.1:23119")
    b=commands.add_parser("build"); b.add_argument("--plan",required=True); b.add_argument("--items"); b.add_argument("--out",required=True)
    v=commands.add_parser("verify"); v.add_argument("--document",required=True); v.add_argument("--plan",required=True); v.add_argument("--items"); v.add_argument("--out")
    args=parser.parse_args(argv)
    try:
        if args.command=="doctor": result=doctor()
        elif args.command=="clean-metadata": result=clean_docx_metadata(args.document)
        elif args.command=="scan":
            result=scan(args.source,reference_list=args.reference_list); write_json(args.out,result)
            if args.references_out: write_json(args.references_out,references_input(result))
            result={"scan":str(Path(args.out).resolve()),"documents":len(result["documents"]),
                    "references":sum(len(d["references"]) for d in result["documents"]),
                    "citation_candidates":sum(len(d["citations"]) for d in result["documents"]),
                    "issues":sum(len(d["issues"]) for d in result["documents"])}
        elif args.command=="references":
            result=references_input(read_json(args.scan)); write_json(args.out,result)
            result={"references":str(Path(args.out).resolve()),"documents":len(result["documents"])}
        elif args.command=="resolve":
            result=resolve_library(read_json(args.scan),no_collection=args.no_collection,collection=args.collection,
                                   library_type=args.library_type,library_id=args.library_id,base=args.base_url)
            write_json(args.out,result)
            print(json.dumps({"mapping":str(Path(args.out).resolve()),"complete":result["complete"],"pending":sum(r["item"] is None for r in result["mappings"]),"writes":0},ensure_ascii=False))
            return 0 if result["complete"] else 2
        elif args.command=="plan":
            result=make_static_plan(read_json(args.scan),args.mode,args.style) if args.static else make_plan(read_json(args.scan),read_json(args.mapping),args.style,args.mode)
            write_json(args.out,result)
            result={"plan":str(Path(args.out).resolve()),"canonical_items":len(result["items"]),"pending":len(result["mapping_issues"]),"reviewed":False}
        elif args.command=="items":
            result=fetch_items(read_json(args.plan),args.base_url); write_json(args.out,result)
            result={"items":str(Path(args.out).resolve()),"count":len(result["records"]),"writes":0}
        elif args.command=="build":
            plan=read_json(args.plan)
            if not args.items and plan.get("citation_mode") != "static": raise OutputError("Dynamic output requires --items")
            bundle=read_json(args.items) if args.items else {}
            if Path(args.out).suffix.lower()==".docx": result=build_docx(plan,bundle,args.out)
            elif Path(args.out).suffix.lower()==".md": result=build_markdown(plan,bundle,args.out)
            else: raise OutputError("Editable outputs are .docx or .md")
        else:
            plan=read_json(args.plan)
            if not args.items and plan.get("citation_mode") != "static": raise OutputError("Dynamic verification requires --items")
            result=verify_docx(args.document,plan,read_json(args.items) if args.items else {})
            if args.out: write_json(args.out,result)
        print(json.dumps(result,ensure_ascii=False,indent=2))
        return 0
    except (OutputError,OSError,ValueError,KeyError) as exc:
        print(json.dumps({"error":str(exc),"completed":False},ensure_ascii=False),file=sys.stderr)
        return 2

if __name__=="__main__": sys.exit(main())
