"""Isolated v0.1.1 acceptance checks; mocked API is not live Zotero evidence."""
import builtins
import contextlib
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from docx import Document
import test_workflow as fixtures
from common import OutputError, read_json, write_json
from scan_sources import scan, package, xml_parse, fields, q
from citation_plan import make_plan, make_static_plan, fetch_items, validate_plan, item_index
from library_mapping import resolve_library, collection_decision
from word_output import build_docx, verify_docx, field_runs
from docx_metadata import clean_docx_metadata, clean_metadata_parts
from zotero_output import main, build_markdown


class StandaloneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root = Path(self.temp.name)
        self.raw = {"key":"ITEM0001", "library":{"type":"user","id":0}, "data":{
            "itemType":"journalArticle", "title":"Alpha Study", "DOI":"10.1234/alpha", "date":"2020",
            "creators":[{"lastName":"Alpha","creatorType":"author"}], "collections":[]}}
        self.csl = {"id":"http://zotero.org/users/local/TESTUSER/items/ITEM0001", "type":"article-journal",
                    "title":"Alpha Study", "DOI":"10.1234/alpha", "author":[{"family":"Alpha"}],
                    "issued":{"date-parts":[[2020]]}}
        self.inventory = [self.raw]; self.calls = []
    def tearDown(self): self.temp.cleanup()
    def text(self):
        path=self.root/'a.txt'
        path.write_text('Claim [1].\nReferences\n[1] Alpha A. Alpha Study. 2020. doi:10.1234/alpha',encoding='utf-8')
        return path
    def native(self, foreign=None, metadata=True):
        doc=Document(); doc.core_properties.author='Alice'
        p=doc.add_paragraph('Claim ')
        payload={"citationID":"old", "properties":{}, "citationItems":[{
            "id":9999,"uris":[foreign or self.csl['id']],"itemData":self.csl if metadata else {}}]}
        for r in field_runs('ADDIN ZOTERO_ITEM CSL_CITATION '+json.dumps(payload),'[7]'): p._p.append(r)
        path=self.root/'native.docx'; doc.save(path); return path
    def api(self, path, base=None):
        self.calls.append(path)
        if '/items/top?' in path: return copy.deepcopy(self.inventory)
        if '/collections/' in path: return {"key":"COLL0001"}
        key=path.split('/items/')[1].split('?')[0]
        row=next(r for r in self.inventory if r['key']==key)
        if '?format=csljson' in path:
            csl=copy.deepcopy(self.csl); csl['id']=csl['id'].rsplit('/',1)[0]+'/'+key
            return [csl]
        return copy.deepcopy(row)
    @contextlib.contextmanager
    def api_context(self):
        with patch('library_mapping.request_json',self.api),patch('citation_plan.request_json',self.api): yield
    def library_plan(self, source):
        scanned=scan([source]); mapping=resolve_library(scanned,no_collection=True)
        return fixtures.accept(make_plan(scanned,mapping,'vancouver'))

    def test_native_docx_pipeline_without_input_imports(self):
        source=self.native(); original_import=builtins.__import__
        def without_input(name,*args,**kwargs):
            if name in {'ingest','zotero'}: raise ImportError('Zotero Input is not installed')
            return original_import(name,*args,**kwargs)
        with patch('builtins.__import__',side_effect=without_input), self.api_context():
            plan=self.library_plan(source); bundle=fetch_items(plan)
            out=self.root/'merged.docx'; report=build_docx(plan,bundle,out)
            self.assertTrue(verify_docx(out,plan,bundle)['structural_verified'])
        self.assertEqual(report['citation_fields'],1)
        self.assertEqual(Document(out).core_properties.author,'Alice')
        self.assertFalse(any('/collections/' in c for c in self.calls))

    def test_no_collection_reuses_unfiled_item(self):
        with self.api_context():
            plan=self.library_plan(self.text()); bundle=fetch_items(plan)
        self.assertEqual(plan['scope'],'library'); self.assertIsNone(plan['items'][0]['target_collection'])
        self.assertTrue(bundle['records'][0]['library_verified'])
        self.assertFalse(bundle['records'][0]['membership_verified'])
        self.assertEqual(len(item_index(plan,bundle)),1)

    def test_title_only_readonly_matching(self):
        source=self.text(); scanned=scan([source]); ref=scanned['documents'][0]['references'][0]
        ref.update(doi=None,title='Alpha Study')
        with self.api_context(): mapping=resolve_library(scanned,no_collection=True)
        self.assertTrue(mapping['complete']); self.assertEqual(mapping['writes'],0)

    def test_input_collaboration_contract_and_membership_still_enforced(self):
        scanned=scan([self.text()]); mapping=fixtures.mapping_for(scanned)
        plan=fixtures.accept(make_plan(scanned,mapping,'vancouver'))
        with self.api_context():
            with self.assertRaises(OutputError): fetch_items(plan)
            self.raw['data']['collections']=['COLL0001']
            bundle=fetch_items(plan)
        self.assertTrue(item_index(plan,bundle))
        self.assertEqual(plan['scope'],'collection')
        downgraded={**mapping,'scope':'library','collection_policy':'none'}
        with self.assertRaises(OutputError): make_plan(scanned,downgraded,'vancouver')

    def test_explicit_none_never_uses_timeout_default(self):
        for elapsed in (0,59,60,600):
            self.assertEqual(collection_decision('none',question_asked=True,elapsed_seconds=elapsed),'none')
        with self.api_context(): resolve_library(scan([self.text()]),no_collection=True)
        self.assertFalse(any('collection' in call for call in self.calls))

    def test_unspecified_collection_keeps_ask_and_sixty_second_default(self):
        self.assertEqual(collection_decision(),'ask')
        self.assertEqual(collection_decision(key='COLL0001'),'existing')
        self.assertEqual(collection_decision(question_asked=True,elapsed_seconds=59.999),'wait')
        self.assertEqual(collection_decision(question_asked=True,elapsed_seconds=60),'new')
        self.assertEqual(collection_decision('existing',question_asked=True,elapsed_seconds=600),'clarify')
        with self.assertRaises(OutputError): resolve_library(scan([self.text()]))
        self.assertEqual(self.calls,[])

    def test_missing_reference_stays_pending_and_cannot_build(self):
        self.inventory=[]
        with self.api_context(): plan=self.library_plan(self.text())
        self.assertEqual(len(plan['mapping_issues']),1)
        with self.assertRaises(OutputError): build_docx(plan,{},self.root/'wrong.docx')
        self.assertFalse((self.root/'wrong.docx').exists())

    def test_foreign_local_uri_is_not_proof_by_same_key(self):
        source=self.native('http://zotero.org/users/local/OTHERUSER/items/ITEM0001',metadata=False)
        with self.api_context(): result=resolve_library(scan([source]),no_collection=True)
        self.assertFalse(result['complete'])

    def test_ambiguity_and_identity_conflict_are_not_first_match(self):
        other=copy.deepcopy(self.raw); other['key']='ITEM0002'; self.inventory.append(other)
        with self.api_context(): result=resolve_library(scan([self.text()]),no_collection=True)
        self.assertFalse(result['complete'])
        with self.api_context(): native=resolve_library(scan([self.native()]),no_collection=True)
        self.assertTrue(native['complete']); self.assertEqual(native['mappings'][0]['item']['item_key'],'ITEM0001')
        self.inventory=[self.raw]; self.raw['data']['creators'][0]['lastName']='Different'
        with self.api_context(): conflict=resolve_library(scan([self.text()]),no_collection=True)
        self.assertFalse(conflict['complete'])

    def test_collection_adapter_does_not_add_membership(self):
        with self.api_context():
            result=resolve_library(scan([self.text()]),collection='COLL0001')
            self.assertFalse(result['complete'])
            self.raw['data']['collections']=['COLL0001']
            result=resolve_library(scan([self.text()]),collection='COLL0001')
        self.assertTrue(result['complete']); self.assertEqual(result['writes'],0)

    def test_bare_agent_and_model_names_removed_but_people_preserved(self):
        for name in ('Codex','GPT6','GPT-6','GPT-6.1','OpenAI Codex','OpenAI GPT-6','ChatGPT','Generated by Python','Generated by AI'):
            doc=Document(); doc.core_properties.author=name; doc.core_properties.last_modified_by=name
            out=self.root/'model.docx'; doc.save(out); clean_docx_metadata(out)
            values=[e.text for e in xml_parse(package(out)['docProps/core.xml']) if e.tag.endswith(('}creator','}lastModifiedBy'))]
            self.assertTrue(all(not v for v in values),name)
        doc=Document(); doc.core_properties.author='Dr. Codex Smith'
        doc.core_properties.comments='The authors used ChatGPT for language editing.'
        out=self.root/'human.docx'; doc.save(out); clean_docx_metadata(out); original=out.read_bytes()
        self.assertEqual(Document(out).core_properties.author,'Dr. Codex Smith')
        self.assertEqual(Document(out).core_properties.comments,'The authors used ChatGPT for language editing.')
        clean_docx_metadata(out); self.assertEqual(original,out.read_bytes())

    def test_targeted_credit_line_preserves_note_and_real_editors(self):
        doc=Document(); doc.core_properties.author='Alice; GPT6'
        doc.core_properties.last_modified_by='Human editor'
        doc.core_properties.comments='Editorial note\nGenerated by Codex'
        out=self.root/'notes.docx'; doc.save(out); clean_docx_metadata(out)
        props=Document(out).core_properties
        self.assertEqual(props.author,'Alice'); self.assertEqual(props.comments,'Editorial note')
        core=xml_parse(package(out)['docProps/core.xml'])
        self.assertIn('Human editor',[e.text for e in core])

    def test_multiple_source_authors_are_combined_and_conflicting_editors_blank(self):
        paths=[]
        for name,editor in (('Alice','Editor A'),('Bob','Editor B')):
            doc=Document(); doc.core_properties.author=name; doc.core_properties.last_modified_by=editor
            doc.add_paragraph(name+' [1].'); doc.add_paragraph('References')
            doc.add_paragraph('[1] Alpha A. 2020. doi:10.1234/alpha')
            path=self.root/(name+'.docx'); doc.save(path); paths.append(path)
        scanned=scan(paths); plan=fixtures.accept(make_plan(scanned,fixtures.mapping_for(scanned),'vancouver'))
        out=self.root/'authors.docx'; build_docx(plan,fixtures.bundle_for(plan),out)
        self.assertEqual(Document(out).core_properties.author,'Alice; Bob')
        core=xml_parse(package(out)['docProps/core.xml'])
        self.assertFalse(next(e.text for e in core if e.tag.endswith('}lastModifiedBy')))

    def test_static_docx_and_markdown_without_library_or_input(self):
        scanned=scan([self.text()]); plan=fixtures.accept(make_static_plan(scanned))
        with patch('citation_plan.request_json',side_effect=AssertionError('No library reads in static mode')):
            out=self.root/'plain.docx'; report=build_docx(plan,{},out)
            self.assertEqual(report['citation_mode'],'static'); self.assertEqual(report['citation_fields'],0)
            self.assertNotIn(b'ZOTERO_ITEM',package(out)['word/document.xml'])
            self.assertTrue(verify_docx(out,plan,{})['source_prose_verified'])
            md=self.root/'plain.md'; build_markdown(plan,{},md)
            self.assertIn('Claim [1]',md.read_text(encoding='utf-8'))
            self.assertFalse(md.with_suffix('.references.json').exists())
            self.assertEqual(fetch_items(plan)['records'],[])
        with self.assertRaises(OutputError): make_static_plan(scanned,style='apa')

    def test_static_conversion_removes_native_fields_and_preserves_authors(self):
        plan=fixtures.accept(make_static_plan(scan([self.native()])))
        out=self.root/'static.docx'; build_docx(plan,{},out)
        self.assertTrue(verify_docx(out,plan,{})['structural_verified'])
        self.assertEqual(Document(out).core_properties.author,'Alice')
        self.assertNotIn(b'ZOTERO_ITEM',package(out)['word/document.xml'])

    def test_static_cli_reuses_plan_build_verify_commands(self):
        source=self.text(); write_json(self.root/'scan.json',scan([source]))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(['plan','--scan',str(self.root/'scan.json'),'--static','--style','source-text','--out',str(self.root/'plan.json')]),0)
            plan=fixtures.accept(read_json(self.root/'plan.json')); write_json(self.root/'plan.json',plan)
            self.assertEqual(main(['build','--plan',str(self.root/'plan.json'),'--out',str(self.root/'cli.docx')]),0)
            self.assertEqual(main(['verify','--plan',str(self.root/'plan.json'),'--document',str(self.root/'cli.docx')]),0)

    def test_static_bibliography_only_and_tamper_detection(self):
        plan=fixtures.accept(make_static_plan(scan([self.text()]),mode='bibliography'))
        out=self.root/'list.docx'; build_docx(plan,{},out)
        self.assertNotIn('Claim', '\n'.join(p.text for p in Document(out).paragraphs))
        doc=Document(out); doc.paragraphs[-1].text='Wrong entry'; doc.save(out)
        with self.assertRaises(OutputError): verify_docx(out,plan,{})

    def test_human_reviews_and_disclosures_untouched(self):
        body=b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:ins w:author="Dr Smith" w:id="3"><w:r><w:t>We used ChatGPT. Generated by AI is a disclosure here.</w:t></w:r></w:ins></w:body></w:document>'
        comments=b'<w:comments xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:comment w:author="Human" w:initials="HU" w:id="1"><w:p><w:r><w:t>Keep review.</w:t></w:r></w:p></w:comment></w:comments>'
        parts={'word/document.xml':body,'word/comments.xml':comments}
        self.assertEqual(clean_metadata_parts(parts),[])
        self.assertEqual(parts['word/document.xml'],body); self.assertEqual(parts['word/comments.xml'],comments)

    def test_fresh_csl_conflict_blocks_snapshot_match(self):
        self.csl['title']='Different Article'
        with self.api_context(): result=resolve_library(scan([self.text()]),no_collection=True)
        self.assertFalse(result['complete'])

    def test_membership_flag_cannot_replace_library_verification(self):
        with self.api_context():
            plan=self.library_plan(self.text()); bundle=fetch_items(plan)
        bundle['records'][0].update(library_verified=False,membership_verified=True)
        with self.assertRaises(OutputError): item_index(plan,bundle)

    def test_static_order_dedup_and_prose_verification(self):
        a=self.root/'order.txt'
        a.write_text('Beta [2], alpha [1], beta again [2].\nReferences\n[1] Alpha A. Study. 2020. doi:10.1234/alpha\n[2] Beta B. Study. 2021. doi:10.1234/beta',encoding='utf-8')
        b=self.text()
        plan=fixtures.accept(make_static_plan(scan([a,b])))
        self.assertEqual(len(plan['items']),2)
        out=self.root/'ordered.docx'; build_docx(plan,{},out)
        doc=Document(out)
        self.assertEqual(doc.paragraphs[0].text,'Beta [1], alpha [2], beta again [1].')
        doc.paragraphs[0].text='Incorrect [9].'; doc.save(out)
        with self.assertRaises(OutputError): verify_docx(out,plan,{})

    def test_static_plain_document_does_not_add_empty_reference_heading(self):
        path=self.root/'no-refs.txt'; path.write_text('Ordinary prose.',encoding='utf-8')
        plan=fixtures.accept(make_static_plan(scan([path])))
        out=self.root/'ordinary.docx'; build_docx(plan,{},out)
        self.assertEqual([p.text for p in Document(out).paragraphs],['Ordinary prose.'])

    def test_static_verifier_rejects_hidden_live_header_field(self):
        from lxml import etree
        from word_output import zip_bytes
        plan=fixtures.accept(make_static_plan(scan([self.text()])))
        out=self.root/'header.docx'; build_docx(plan,{},out)
        parts=package(out); root=etree.Element(q('hdr')); p=etree.SubElement(root,q('p'))
        for r in field_runs('ADDIN ZOTERO_ITEM CSL_CITATION {}','[1]'): p.append(r)
        parts['word/header9.xml']=etree.tostring(root); out.write_bytes(zip_bytes(parts).getvalue())
        with self.assertRaisesRegex(OutputError,'Static output contains'): verify_docx(out,plan,{})

    def test_static_metadata_retains_full_embedded_author_list(self):
        self.csl['author'].append({'family':'Beta','given':'B'})
        plan=fixtures.accept(make_static_plan(scan([self.native()])))
        out=self.root/'full-authors.docx'; build_docx(plan,{},out)
        self.assertIn('B Beta',Document(out).paragraphs[-1].text)

if __name__=='__main__': unittest.main()
