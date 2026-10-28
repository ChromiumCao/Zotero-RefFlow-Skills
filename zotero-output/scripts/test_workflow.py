"""Synthetic behavioral tests. No network or live Zotero writes."""
import copy
import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from common import OutputError, write_json
from scan_sources import scan, references_input, q, fields, xml_parse, paragraph_text
from citation_plan import make_plan, validate_plan
from word_output import build_docx, field_runs, verify_docx, zip_bytes, package, replace_span
from zotero_output import build_markdown
from docx_metadata import clean_docx_metadata, clean_metadata_parts, verify_metadata

def mapping_for(scanned):
    rows=[]
    for doc in scanned["documents"]:
        for ref in doc["references"]:
            number=2 if "beta" in str(ref).lower() else 1
            key="ITEM000"+str(number)
            item={"library":{"type":"user","id":0,"id_kind":"local_user_alias"},"item_key":key,
                  "uri":"http://zotero.org/users/local/TESTUSER/items/"+key}
            rows.append({"source":{"document_id":doc["document_id"],"reference_id":ref["reference_id"]},
                         "identifiers":{"doi":ref.get("doi"),"title":ref.get("title")},"item":item,
                         "outcome":"target_existing","membership_verified":True,
                         "target_collection":{"library":item["library"],"key":"COLL0001"}})
    return {"schema_version":"zotero-input.mapping/1","complete":True,"mappings":rows}

def accept(plan):
    plan["reviewed"]=True
    plan["composition_reviewed"]=True
    for doc in plan["documents"]:
        doc["issues_reviewed"]=True
        for c in doc["citations"]: c["reviewed"]=True
        for r in doc["bibliography_ranges"]: r["reviewed"]=True
    return plan

def bundle_for(plan):
    records=[]
    for target in plan["items"]:
        item=target["item"]
        csl={"type":"article-journal","title":target["identifiers"].get("title") or "Alpha Study",
             "DOI":target["identifiers"].get("doi"),"author":[{"family":"Alpha","given":"A"}],
             "issued":{"date-parts":[[2020]]}}
        records.append({"key":target["key"],"item":item,"uri":item["uri"],"csl":csl,
                        "membership_verified":True,"target_collection":target["target_collection"]})
    return {"schema_version":"zotero-output.items/1","records":records}

class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
    def tearDown(self): self.temp.cleanup()
    def text(self,name,body):
        p=self.root/name; p.write_text(body,encoding="utf-8"); return p
    def plan(self,paths,mode="manuscript"):
        s=scan(paths); return accept(make_plan(s,mapping_for(s),"vancouver",mode))
    def test_colliding_numbers_and_cross_document_duplicate(self):
        a=self.text("a.txt","A [1].\nReferences\n[1] Alpha A. Alpha Study. 2020. doi:10.1234/alpha")
        b=self.text("b.md","B [1].\n# References\n[1] Alpha A. Alpha Study. 2020. doi:10.1234/alpha")
        plan=self.plan([a,b]); self.assertEqual(len(plan["items"]),1)
        out=self.root/"merged.docx"; report=build_docx(plan,bundle_for(plan),out)
        self.assertEqual(report["citation_fields"],2)
        self.assertEqual(len(report["citation_ids"]),2)
        self.assertEqual(len(report["item_uris"]),1)
        self.assertFalse(report["word_refresh_verified"])
    def test_range_cluster_and_repeated_reference(self):
        a=self.text("a.txt","A [1-2,1].\nReferences\n[1] Alpha A. Study. 2020. 10.1234/alpha\n[2] Beta B. Study. 2021. 10.1234/beta")
        plan=self.plan([a]); self.assertEqual(len(plan["documents"][0]["citations"][0]["references"]),3)
        report=build_docx(plan,bundle_for(plan),self.root/"range.docx")
        self.assertEqual(report["citation_fields"],1); self.assertEqual(len(report["item_uris"]),2)
    def test_mapping_missing_or_unverified_rejected(self):
        a=self.text("a.txt","A [1].\nReferences\n[1] Alpha A. Study. 2020. 10.1234/alpha")
        s=scan([a]); mapping=mapping_for(s)
        mapping["mappings"][0]["membership_verified"]=False
        plan=accept(make_plan(s,mapping,"vancouver"))
        with self.assertRaises(OutputError): validate_plan(plan)
        with self.assertRaises(OutputError): make_plan(s,{**mapping,"mappings":[]},"vancouver")
    def test_stale_source_and_source_overwrite_rejected(self):
        a=self.text("a.txt","A [1].\nReferences\n[1] Alpha A. 2020. 10.1234/alpha")
        plan=self.plan([a])
        with self.assertRaises(OutputError): validate_plan(plan,a)
        a.write_text("changed",encoding="utf-8")
        with self.assertRaises(OutputError): validate_plan(plan)
    def test_unreviewed_candidate_and_ignore_reason(self):
        a=self.text("a.txt","Equation [9].\nReferences\n[1] Alpha A. 2020. 10.1234/alpha")
        plan=self.plan([a]); citation=plan["documents"][0]["citations"][0]
        with self.assertRaises(OutputError): validate_plan(plan)
        citation.update(action="ignore",reason="Equation number, not a citation")
        self.assertEqual(validate_plan(plan),0)
    def test_uri_conflict(self):
        a=self.text("a.txt","A [1].\nReferences\n[1] Alpha A. 2020. 10.1234/alpha")
        plan=self.plan([a]); bundle=bundle_for(plan)
        bundle["records"][0]["uri"]="http://zotero.org/users/123/items/WRONGKEY"
        with self.assertRaises(OutputError): build_docx(plan,bundle,self.root/"invalid.docx")
        self.assertFalse((self.root/"invalid.docx").exists())
    def test_bibliography_only_preserves_uncited_without_hidden_citations(self):
        a=self.text("a.txt","References\n[1] Alpha A. 2020. 10.1234/alpha")
        plan=self.plan([a],"bibliography")
        report=build_docx(plan,bundle_for(plan),self.root/"bibliography.docx")
        self.assertEqual(report["citation_fields"],0)
        self.assertEqual(len(report["item_uris"]),1)
    def test_markdown_output(self):
        a=self.text("a.md","# A\nText [1].\n# References\n[1] Alpha A. 2020. 10.1234/alpha")
        plan=self.plan([a]); out=self.root/"output.md"
        build_markdown(plan,bundle_for(plan),out)
        self.assertIn("[@zo_user_0_ITEM0001]",out.read_text("utf-8"))
        self.assertTrue(out.with_suffix(".references.json").exists())
    def test_rich_docx_runs_table_and_media_preserved(self):
        from docx import Document
        from PIL import Image
        image=self.root/"tiny.png"; Image.new("RGB",(10,10),"red").save(image)
        doc=Document(); p=doc.add_paragraph(); p.add_run("A ").bold=True; p.add_run("[1]").italic=True; p.add_run(" end.")
        doc.add_table(rows=1,cols=1).cell(0,0).text="Table prose"
        doc.add_picture(str(image)); doc.add_paragraph("References"); doc.add_paragraph("[1] Alpha A. 2020. 10.1234/alpha")
        source=self.root/"a.docx"; doc.save(source)
        plan=self.plan([source]); out=self.root/"rich.docx"; build_docx(plan,bundle_for(plan),out)
        result=Document(out); self.assertEqual(len(result.tables),1); self.assertEqual(len(result.inline_shapes),1)
        self.assertTrue(result.paragraphs[0].runs[0].bold)
        self.assertIn(" end.",result.paragraphs[0].text)
    def test_split_text_run_preserves_styles(self):
        from docx import Document
        p=Document().add_paragraph(); p.add_run("Alpha [").bold=True; p.add_run("1] beta").italic=True
        replace_span(p._p,6,9,field_runs("ADDIN TEST","X"))
        self.assertEqual(paragraph_text(p._p),"Alpha X beta")
        self.assertTrue(p.runs[0].bold); self.assertTrue(p.runs[-1].italic)

    def test_native_fields_relinked_and_options_preserved(self):
        from docx import Document
        doc=Document(); p=doc.add_paragraph("Claim ")
        payload={"citationID":"foreign-original","properties":{"noteIndex":0,"unsorted":True},
                 "citationItems":[{"id":10,"uris":["http://zotero.org/users/999/items/OLD00001"],
                                   "itemData":{"title":"Alpha Study","type":"article-journal","DOI":"10.1234/alpha"},
                                   "locator":"42","label":"page","prefix":"see "}]}
        for r in field_runs("ADDIN ZOTERO_ITEM CSL_CITATION "+json.dumps(payload),"[7]"): p._p.append(r)
        source=self.root/"native.docx"; doc.save(source)
        plan=self.plan([source]); out=self.root/"native-out.docx"; build_docx(plan,bundle_for(plan),out)
        root=xml_parse(package(out)["word/document.xml"])
        converted=next(f for p in root.iter(q("p")) for f in fields(p) if "CSL_CITATION" in f["code"])
        data=json.loads(converted["code"].split("CSL_CITATION",1)[1])
        self.assertNotEqual(data["citationID"],"foreign-original")
        self.assertEqual(data["citationItems"][0]["locator"],"42")
        self.assertIn("/users/local/TESTUSER/",data["citationItems"][0]["uris"][0])
    def test_endnote_embedded_record(self):
        from docx import Document
        doc=Document(); p=doc.add_paragraph("Claim ")
        xml='<EndNote><Cite><RecNum>12</RecNum><Pages>7</Pages><record><titles><title>Alpha Study</title></titles><contributors><authors><author>Alpha, A</author></authors></contributors><dates><year>2020</year></dates><electronic-resource-num>10.1234/alpha</electronic-resource-num></record></Cite></EndNote>'
        for r in field_runs("ADDIN EN.CITE "+xml,"(Alpha 2020)"): p._p.append(r)
        source=self.root/"endnote.docx"; doc.save(source)
        s=scan([source]); self.assertEqual(s["documents"][0]["citations"][0]["kind"],"endnote")
        plan=accept(make_plan(s,mapping_for(s),"vancouver"))
        self.assertEqual(build_docx(plan,bundle_for(plan),self.root/"converted.docx")["citation_fields"],1)
    def test_multiline_zotero_bibliography(self):
        from docx import Document
        doc=Document(); doc.add_paragraph("Prose")
        rows=field_runs('ADDIN ZOTERO_BIBL {"uncited":[],"omitted":[],"custom":[]} CSL_BIBLIOGRAPHY',"First entry")
        p=doc.add_paragraph(); [p._p.append(r) for r in rows[:-1]]
        p=doc.add_paragraph("Second entry"); p._p.append(rows[-1])
        source=self.root/"biblio.docx"; doc.save(source)
        s=scan([source]); ranges=s["documents"][0]["bibliography_ranges"]
        self.assertTrue(any(r["start"]==1 and r["end"]==3 for r in ranges))
        self.assertEqual(s["documents"][0]["citations"],[])
    def test_superscript_and_reference_list_without_heading(self):
        from docx import Document
        doc=Document(); p=doc.add_paragraph("Claim"); p.add_run("1").font.superscript=True
        doc.add_paragraph("References"); doc.add_paragraph("[1] Alpha A. 2020. 10.1234/alpha")
        source=self.root/"sup.docx"; doc.save(source); s=scan([source])
        self.assertEqual(s["documents"][0]["citations"][0]["kind"],"superscript")
        a=self.text("refs.txt","[1] Alpha A. 2020. 10.1234/alpha")
        s=scan([a],reference_list=True); self.assertEqual(len(s["documents"][0]["references"]),1)
    def test_author_date_and_identifier_conflict(self):
        a=self.text("a.txt","Claim (Alpha, 2020).\nReferences\nAlpha A. Alpha Study. 2020. doi:10.1234/alpha")
        s=scan([a]); self.assertEqual(s["documents"][0]["citations"][0]["kind"],"author_date")
        mapping=mapping_for(s); mapping["mappings"][0]["identifiers"]["doi"]="10.1234/wrong"
        with self.assertRaises(OutputError): make_plan(s,mapping,"vancouver")
    def test_multi_rich_docx_composition(self):
        try: import docxcompose
        except ImportError: self.skipTest("docxcompose not installed in this runtime")
        from docx import Document
        sources=[]
        for name in ("a","b"):
            doc=Document(); doc.add_paragraph(name+" [1]."); doc.add_table(rows=1,cols=1).cell(0,0).text=name+" table"
            doc.add_paragraph("References"); doc.add_paragraph("[1] Alpha A. 2020. 10.1234/alpha")
            p=self.root/(name+".docx"); doc.save(p); sources.append(p)
        plan=self.plan(sources); out=self.root/"multi.docx"
        self.assertEqual(build_docx(plan,bundle_for(plan),out)["citation_fields"],2)
        self.assertEqual(len(Document(out).tables),2)
    def test_pdf_editable_reconstruction(self):
        try: import pymupdf
        except ImportError: self.skipTest("PyMuPDF not installed")
        pdf=pymupdf.open(); page=pdf.new_page(); page.insert_text((50,50),"Claim [1].\nReferences\n[1] Alpha A. Alpha Study. 2020. 10.1234/alpha")
        source=self.root/"paper.pdf"; pdf.save(source); pdf.close()
        plan=self.plan([source]); self.assertEqual(build_docx(plan,bundle_for(plan),self.root/"pdf-output.docx")["citation_fields"],1)

    def test_verified_fields_do_not_hide_missing_prose(self):
        from docx import Document
        a=self.text("a.txt","Important claim [1].\nReferences\n[1] Alpha A. 2020. 10.1234/alpha")
        plan=self.plan([a]); bundle=bundle_for(plan); out=self.root/"prose.docx"
        build_docx(plan,bundle,out)
        doc=Document(out); doc.paragraphs[0].runs[0].text="Changed claim "; doc.save(out)
        clean_docx_metadata(out)
        with self.assertRaises(OutputError): verify_docx(out,plan,bundle)
    def test_footnote_field_and_context_preserved(self):
        from docx import Document
        from lxml import etree
        doc=Document(); p=doc.add_paragraph("A note"); r=p.add_run(); etree.SubElement(r._r,q("footnoteReference")).set(q("id"),"7")
        doc.add_paragraph("References"); doc.add_paragraph("[1] Alpha A. 2020. 10.1234/alpha")
        source=self.root/"notes.docx"; doc.save(source); parts=package(source)
        root=etree.Element(q("footnotes"),nsmap={"w":"http://schemas.openxmlformats.org/wordprocessingml/2006/main"})
        note=etree.SubElement(root,q("footnote")); note.set(q("id"),"7")
        p=etree.SubElement(note,q("p")); r=etree.SubElement(p,q("r")); etree.SubElement(r,q("t")).text="Note claim [1]."
        parts["word/footnotes.xml"]=etree.tostring(root)
        rels=xml_parse(parts["word/_rels/document.xml.rels"])
        etree.SubElement(rels,"{http://schemas.openxmlformats.org/package/2006/relationships}Relationship",Id="rFoot",Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/footnotes",Target="footnotes.xml")
        parts["word/_rels/document.xml.rels"]=etree.tostring(rels)
        ct=xml_parse(parts["[Content_Types].xml"])
        etree.SubElement(ct,"{http://schemas.openxmlformats.org/package/2006/content-types}Override",PartName="/word/footnotes.xml",ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml")
        parts["[Content_Types].xml"]=etree.tostring(ct); source.write_bytes(zip_bytes(parts).getvalue())
        plan=self.plan([source]); out=self.root/"notes-out.docx"
        build_docx(plan,bundle_for(plan),out)
        root=xml_parse(package(out)["word/footnotes.xml"])
        code=next(f["code"] for p in root.iter(q("p")) for f in fields(p) if "CSL_CITATION" in f["code"])
        payload=json.loads(code.split("CSL_CITATION",1)[1]); self.assertEqual(payload["properties"]["noteIndex"],1)
        self.assertIn("Note claim",paragraph_text(root))
    def test_structured_input_contract(self):
        import os,sys
        input_dir=Path(os.environ.get("ZOTERO_INPUT_SKILL",Path.home()/".codex"/"skills"/"zotero-input"))/"scripts"
        if not (input_dir/"ingest.py").exists(): self.skipTest("zotero-input is not installed")
        sys.path.insert(0,str(input_dir))
        try:
            from ingest import normalize_sources
            a=self.text("a.txt","Claim [1], again [1].\nReferences\n[1] Alpha A. 2020. 10.1234/alpha")
            s=scan([a]); structured=references_input(s); rows=normalize_sources(structured["documents"])
            self.assertEqual(len(rows),1)
            self.assertEqual(len(rows[0]["source_location"]["citations"]),2)
        finally: sys.path.remove(str(input_dir))

    def test_mixed_order_retains_rich_package(self):
        from docx import Document
        a=self.text("prefix.txt","Prefix [1].\nReferences\n[1] Alpha A. 2020. 10.1234/alpha")
        doc=Document(); doc.add_paragraph("Rich [1]."); doc.add_table(rows=1,cols=1).cell(0,0).text="Rich table"
        doc.add_paragraph("References"); doc.add_paragraph("[1] Alpha A. 2020. 10.1234/alpha")
        b=self.root/"rich.docx"; doc.save(b); plan=self.plan([a,b]); out=self.root/"mixed.docx"
        build_docx(plan,bundle_for(plan),out)
        result=Document(out); self.assertTrue(result.paragraphs[0].text.startswith("Prefix")); self.assertEqual(len(result.tables),1)
    def test_differing_headers_rejected_without_loss(self):
        try: import docxcompose
        except ImportError: self.skipTest("docxcompose not installed")
        from docx import Document
        sources=[]
        for name in ("a","b"):
            doc=Document(); doc.sections[0].header.paragraphs[0].text=name+" source header"
            doc.add_paragraph(name+" [1]."); doc.add_paragraph("References"); doc.add_paragraph("[1] Alpha A. 2020. 10.1234/alpha")
            p=self.root/(name+".docx"); doc.save(p); sources.append(p)
        plan=self.plan(sources); out=self.root/"unsafe.docx"
        with self.assertRaises(OutputError): build_docx(plan,bundle_for(plan),out)
        self.assertFalse(out.exists())
    def test_doi_hyperlink_target_extracted(self):
        from docx import Document
        from docx.opc.constants import RELATIONSHIP_TYPE as RT
        from lxml import etree
        doc=Document(); doc.add_paragraph("Claim [1]."); doc.add_paragraph("References")
        p=doc.add_paragraph("[1] Alpha A. Study. 2020. ")
        rid=p.part.relate_to("https://doi.org/10.1234/alpha",RT.HYPERLINK,is_external=True)
        link=etree.SubElement(p._p,q("hyperlink")); link.set("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id",rid)
        r=etree.SubElement(link,q("r")); etree.SubElement(r,q("t")).text="Publisher link"
        source=self.root/"link.docx"; doc.save(source)
        s=scan([source]); self.assertEqual(s["documents"][0]["references"][0]["doi"],"10.1234/alpha")

    def test_new_output_has_no_author_or_generator_remarks(self):
        from docx import Document
        a=self.text("privacy.txt","Claim [1].\nReferences\n[1] Alpha A. 2020. 10.1234/alpha")
        plan=self.plan([a]); out=self.root/"private.docx"
        result=build_docx(plan,bundle_for(plan),out)
        props=Document(out).core_properties
        self.assertEqual(props.author,""); self.assertEqual(props.last_modified_by,"")
        self.assertEqual(props.comments,""); self.assertTrue(result["metadata_clean"])
        cite=next(f for p in Document(out).paragraphs for f in fields(p._p) if "CSL_CITATION" in f["code"])
        item=json.loads(cite["code"].split("CSL_CITATION",1)[1])["citationItems"][0]
        self.assertEqual(item["itemData"]["author"][0]["family"],"Alpha")

    def test_inherited_authors_are_preserved_without_changing_source(self):
        from docx import Document
        doc=Document(); doc.core_properties.author="Original Writer"
        doc.core_properties.last_modified_by="Editor"
        doc.core_properties.title="Codex manuscripts"
        doc.core_properties.comments="A meaningful editorial description"
        doc.add_paragraph("Claim [1]."); doc.add_paragraph("References")
        doc.add_paragraph("[1] Alpha A. 2020. 10.1234/alpha")
        source=self.root/"authored.docx"; doc.save(source); original=source.read_bytes()
        plan=self.plan([source]); out=self.root/"anonymous.docx"
        build_docx(plan,bundle_for(plan),out)
        props=Document(out).core_properties
        self.assertEqual(props.author,"Original Writer")
        core=xml_parse(package(out)["docProps/core.xml"])
        self.assertEqual(next(e.text for e in core if e.tag.endswith("}lastModifiedBy")),"Editor")
        self.assertEqual(props.title,"Codex manuscripts")
        self.assertEqual(props.comments,"A meaningful editorial description")
        self.assertEqual(source.read_bytes(),original)

    def test_post_save_cleanup_preserves_fields_preferences_and_media(self):
        a=self.text("postsave.txt","Claim [1].\nReferences\n[1] Alpha A. 2020. 10.1234/alpha")
        plan=self.plan([a]); bundle=bundle_for(plan); out=self.root/"postsave.docx"
        build_docx(plan,bundle,out); parts=package(out)
        root=xml_parse(parts["docProps/core.xml"])
        for element in root:
            if element.tag.endswith("}creator"): element.text="Word user"
            if element.tag.endswith("}lastModifiedBy"): element.text="Word editor"
            if element.tag.endswith("}description"): element.text="Generated by Codex"
        from lxml import etree
        parts["docProps/core.xml"]=etree.tostring(root)
        out.write_bytes(zip_bytes(parts).getvalue())
        before=package(out)
        with self.assertRaisesRegex(OutputError,"metadata"): verify_docx(out,plan,bundle)
        result=clean_docx_metadata(out); after=package(out)
        self.assertTrue(result["metadata_clean"])
        self.assertEqual(set(before),set(after))
        for name in before:
            if name!="docProps/core.xml": self.assertEqual(before[name],after[name],name)
        self.assertTrue(verify_docx(out,plan,bundle)["metadata_clean"])
        self.assertEqual(clean_docx_metadata(out)["sha256"],result["sha256"])

    def test_custom_authors_and_credits_removed_but_zotero_prefs_retained(self):
        from lxml import etree
        a=self.text("custom.txt","Claim [1].\nReferences\n[1] Alpha A. 2020. 10.1234/alpha")
        plan=self.plan([a]); out=self.root/"custom.docx"; build_docx(plan,bundle_for(plan),out)
        parts=package(out); root=xml_parse(parts["docProps/custom.xml"])
        original_prefs=[etree.tostring(p) for p in root]
        cp="http://schemas.openxmlformats.org/officeDocument/2006/custom-properties"
        vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"
        for name,value in (("Author","Someone"),("Remarks","Generated with python-docx"),("Project","Keep me")):
            prop=etree.SubElement(root,"{"+cp+"}property",name=name,pid=str(len(root)+2))
            etree.SubElement(prop,"{"+vt+"}lpwstr").text=value
        parts["docProps/custom.xml"]=etree.tostring(root)
        clean_metadata_parts(parts); verify_metadata(parts)
        cleaned=xml_parse(parts["docProps/custom.xml"])
        self.assertEqual([etree.tostring(p) for p in cleaned if p.get("name","").startswith("ZOTERO_PREF")],original_prefs)
        self.assertFalse(any(p.get("name")=="Remarks" for p in cleaned))
        self.assertEqual(next(p[0].text for p in cleaned if p.get("name")=="Author"),"Someone")
        self.assertEqual(next(p[0].text for p in cleaned if p.get("name")=="Project"),"Keep me")

    def test_human_comment_authors_and_company_preserved(self):
        parts={"docProps/app.xml":b'<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"><Application>python-docx</Application><AppVersion>1.2</AppVersion><Company>Person</Company><Manager>Owner</Manager><Pages>4</Pages></Properties>',
               "word/comments.xml":b'<w:comments xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:comment w:author="Someone" w:initials="SS" w:id="2"><w:p><w:r><w:t>Preserve this review note</w:t></w:r></w:p></w:comment></w:comments>'}
        clean_metadata_parts(parts); self.assertTrue(verify_metadata(parts))
        root=xml_parse(parts["word/comments.xml"]); comment=root[0]
        self.assertEqual(comment.get(q("author")),"Someone"); self.assertEqual(comment.get(q("initials")),"SS")
        self.assertEqual(paragraph_text(comment),"Preserve this review note")
        app=xml_parse(parts["docProps/app.xml"])
        self.assertEqual(next(e.text for e in app if e.tag.endswith("}Pages")),"4")
        self.assertIsNone(next(e.text for e in app if e.tag.endswith("}Application")))
        self.assertEqual(next(e.text for e in app if e.tag.endswith("}Company")),"Person")
        self.assertEqual(next(e.text for e in app if e.tag.endswith("}Manager")),"Owner")

    def test_variant_namespace_real_editor_is_preserved(self):
        parts={"docProps/core.xml":b'<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"><x:lastModifiedBy xmlns:x="http://schemas.openxmlformats.org/officeDocument/2006/custom-properties">Editor</x:lastModifiedBy></cp:coreProperties>'}
        clean_metadata_parts(parts)
        self.assertEqual("".join(xml_parse(parts["docProps/core.xml"]).itertext()),"Editor")
        self.assertTrue(verify_metadata(parts))

if __name__=="__main__": unittest.main()
