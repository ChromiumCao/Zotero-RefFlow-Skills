"""Targeted isolated regression tests; no Zotero/network writes are allowed."""
from copy import deepcopy
import io
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

import ingest as ing
import zotero as z
from test_ingest import ENTRY, MemoryBackend, item, sources


class ReconcileBoundaryTests(unittest.TestCase):
    def test_same_document_duplicate_occurrences_map_independently(self):
        refs = ing.normalize_sources([{
            "document_id": "same-doc",
            "references": [
                {"reference_id": "3a", "doi": ENTRY["doi"],
                 "source_location": {"paragraph": 3, "occurrence": 1}},
                {"reference_id": "3b", "doi": ENTRY["doi"],
                 "source_location": {"paragraph": 9, "occurrence": 2}},
            ],
        }])
        backend = MemoryBackend()
        result = ing.reconcile(refs, backend, "TARGET", lookup=lambda _: deepcopy(ENTRY),
                               checks=3, delay=0)
        self.assertTrue(result["complete"])
        self.assertEqual(len(result["mappings"]), 2)
        self.assertEqual(backend.imports, 1)
        self.assertEqual([r["source"]["reference_id"] for r in result["mappings"]], ["3a", "3b"])
        self.assertEqual([r["source"]["source_location"] for r in result["mappings"]],
                         [{"paragraph": 3, "occurrence": 1}, {"paragraph": 9, "occurrence": 2}])
        self.assertEqual([r["item"]["item_key"] for r in result["mappings"]], ["NEW1", "NEW1"])

    def test_unknown_import_status_is_not_repeated_for_same_occurrence(self):
        refs = sources({"reference_id": "a", "doi": ENTRY["doi"]},
                       {"reference_id": "b", "doi": ENTRY["doi"]})
        backend = MemoryBackend(import_mode="failed")
        result = ing.reconcile(refs, backend, "TARGET", lookup=lambda _: deepcopy(ENTRY),
                               checks=3, delay=0)
        self.assertFalse(result["complete"])
        self.assertEqual(backend.imports, 1)
        self.assertTrue(all(row["outcome"] == "pending" for row in result["mappings"]))

    def test_plugin_disconnect_is_unavailable_without_network_or_dependency(self):
        with patch.object(ing.Plugin, "rpc", side_effect=ConnectionRefusedError("offline")):
            plugin = ing.Plugin()
        self.assertFalse(plugin.tools)
        self.assertFalse(plugin.supports("add_items_to_collection",
                                         {"libraryID", "collectionKey", "itemKeys"}))

    def test_json_rpc_tool_permission_error_is_not_downgraded(self):
        plugin = ing.Plugin.__new__(ing.Plugin)
        plugin.rpc = Mock(return_value={
            "isError": True,
            "content": [{"type": "text", "text": "HTTP 403: permission denied"}],
        })
        with self.assertRaises(ing.PermissionDenied):
            plugin.call("add_items_to_collection", {})

    def test_connector_http_permission_error_is_not_fallback(self):
        backend = ing.LocalBackend(plugin=Mock())
        backend.collection = Mock(return_value={"key": "TARGET", "library": {"name": "My Library"}})
        denied = Mock(ok=False, status=403, text="forbidden", error="HTTP 403")
        with patch.object(z, "connector_post", return_value=denied):
            with self.assertRaises(ing.PermissionDenied):
                backend.assert_selected("TARGET")


class MetadataConflictTests(unittest.TestCase):
    def setUp(self):
        self.network_guard = patch.object(z.urllib.request, "urlopen", side_effect=AssertionError("Unexpected network"))
        self.network_guard.start()
        self.addCleanup(self.network_guard.stop)

    def lookup_pair(self, primary, secondary):
        ref = {"doi": primary["doi"], "pmid": "87654321"}
        with patch.object(z, "_lookup_by_doi", return_value=deepcopy(primary)), \
             patch.object(z, "_pubmed_fetch", return_value=deepcopy(secondary)):
            return ing.lookup_metadata(ref)

    def test_doi_and_pmid_from_different_records_are_not_merged(self):
        doi_entry = {
            "doi": "10.1000/doi-record", "title": "DOI record title",
            "creators": [{"firstName": "A", "lastName": "Smith"}], "year": "2024",
            "abstract": "DOI abstract",
        }
        pmid_entry = {
            "pmid": "87654321", "title": "Different PMID record",
            "creators": [{"firstName": "B", "lastName": "Jones"}], "year": "2011",
            "abstract": "PMID abstract",
        }
        ref = {"doi": doi_entry["doi"], "pmid": pmid_entry["pmid"]}
        with patch.object(z, "_lookup_by_doi", return_value=deepcopy(doi_entry)), \
             patch.object(z, "_pubmed_fetch", return_value=deepcopy(pmid_entry)):
            with self.assertRaises(ing.Uncertain):
                ing.lookup_metadata(ref)

    def test_metadata_conflict_keeps_reconciliation_pending_without_writes(self):
        primary = {**deepcopy(ENTRY), "pmid": None}
        secondary = {"pmid": "87654321", "title": "A different article entirely",
                     "creators": [{"lastName": "Jones"}], "year": "2011"}
        backend = MemoryBackend()
        with patch.object(z, "_lookup_by_doi", return_value=primary), \
             patch.object(z, "_pubmed_fetch", return_value=secondary):
            result = ing.reconcile(sources({"doi": primary["doi"], "pmid": secondary["pmid"]}),
                                   backend, "TARGET", checks=1, delay=0)
        row = result["mappings"][0]
        self.assertFalse(result["complete"])
        self.assertEqual(row["outcome"], "pending")
        self.assertTrue(row["match"]["ambiguous"])
        self.assertEqual(row["match"]["candidates"], [])
        self.assertIn("metadata_records", row["match"])
        self.assertIsNone(row["item"])
        self.assertEqual((backend.adds, backend.imports), (0, 0))

    def test_matching_records_can_merge_with_or_without_pubmed_doi(self):
        primary = {**deepcopy(ENTRY), "pmid": None, "abstract": ""}
        for doi in (primary["doi"], None):
            with self.subTest(pubmed_doi=doi):
                secondary = {**deepcopy(ENTRY), "doi": doi, "pmid": "87654321"}
                result = self.lookup_pair(primary, secondary)
                self.assertEqual(result["pmid"], secondary["pmid"])
                self.assertEqual(result["doi"], primary["doi"])
                self.assertEqual(result["abstract"], secondary["abstract"])
                self.assertIsNone(primary["pmid"])

    def test_matching_title_does_not_override_identifier_author_or_year_conflicts(self):
        primary = {**deepcopy(ENTRY), "pmid": None}
        for fields in ({"doi": "10.1000/conflict"}, {"year": "2011"},
                       {"creators": [{"lastName": "Jones"}]}):
            with self.subTest(fields=fields):
                secondary = {**deepcopy(ENTRY), "doi": None, "pmid": "87654321", **fields}
                with self.assertRaises(ing.MetadataConflict):
                    self.lookup_pair(primary, secondary)

    def test_no_shared_article_evidence_is_not_enough_to_merge(self):
        with self.assertRaises(ing.MetadataConflict):
            self.lookup_pair({**deepcopy(ENTRY), "pmid": None}, {"pmid": "87654321"})

    def test_unavailable_pubmed_does_not_attach_unverified_pmid(self):
        primary = {**deepcopy(ENTRY), "pmid": None}
        result = self.lookup_pair(primary, None)
        self.assertIsNone(result.get("pmid"))
        self.assertEqual(result["abstract"], primary["abstract"])
        self.assertTrue(result["_warnings"])

    def test_pubmed_abstract_is_not_used_as_identifier(self):
        entry = {"pmid": "12345678", "title": ENTRY["title"],
                 "creators": deepcopy(ENTRY["creators"]), "abstract": "12345678"}
        ris = z._pubmed_to_ris(entry)
        self.assertIn("AB  - 12345678", ris)
        self.assertIn("M2  - PMID: 12345678", ris)
        self.assertNotIn("N2  - PMID", ris)


class ExtractionIsolationTests(unittest.TestCase):
    def write_docx(self, path):
        xml = (b'<?xml version="1.0" encoding="UTF-8"?>'
               b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
               b'<w:body><w:p><w:r><w:t>[1] DOI: 10.1000/docx</w:t></w:r></w:p>'
               b'<w:tbl><w:tr><w:tc><w:p><w:r><w:t>[2] PMID: 12345678</w:t></w:r></w:p>'
               b'</w:tc></w:tr></w:tbl></w:body></w:document>')
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("word/document.xml", xml)

    def write_xlsx(self, path):
        shared = (b'<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                  b'<si><t>DOI</t></si><si><t>10.1000/xlsx</t></si></sst>')
        sheet = (b'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
                 b'<row><c t="s"><v>0</v></c></row>'
                 b'<row><c t="s"><v>1</v></c></row></sheetData></worksheet>')
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("xl/sharedStrings.xml", shared)
            archive.writestr("xl/worksheets/sheet1.xml", sheet)

    def test_markdown_csv_docx_xlsx_extract_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            md = root / "refs.md"
            csv_path = root / "refs.csv"
            docx = root / "refs.docx"
            xlsx = root / "refs.xlsx"
            md.write_text("References\n[1] DOI: 10.1000/md", encoding="utf-8")
            csv_path.write_text("doi,title\n10.1000/csv,CSV study\n", encoding="utf-8")
            self.write_docx(docx)
            self.write_xlsx(xlsx)
            self.assertEqual(z.extract_source_references(md)[0]["doi"], "10.1000/md")
            self.assertEqual(z.extract_source_references(csv_path)[0]["doi"], "10.1000/csv")
            self.assertEqual(z.extract_source_references(docx)[0]["doi"], "10.1000/docx")
            self.assertEqual(z.extract_source_references(docx)[1]["pmid"], "12345678")
            self.assertEqual(z.extract_source_references(xlsx)[0]["doi"], "10.1000/xlsx")

    @unittest.skipUnless(shutil.which("pdftotext") or importlib.util.find_spec("pypdf") or importlib.util.find_spec("PyPDF2"),
                         "No optional PDF text reader available")
    def test_pdf_extraction_path(self):
        objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                   b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
                   b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
        stream = b"BT /F1 12 Tf 36 750 Td (DOI: 10.1000/native-pdf) Tj ET"
        objects.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")
        data, offsets = b"%PDF-1.4\n", [0]
        for n, obj in enumerate(objects, 1):
            offsets.append(len(data))
            data += str(n).encode() + b" 0 obj\n" + obj + b"\nendobj\n"
        start = len(data)
        data += b"xref\n0 6\n0000000000 65535 f \n"
        data += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:])
        data += b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n" + str(start).encode() + b"\n%%EOF\n"
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "references.pdf"
            path.write_bytes(data)
            refs = z.extract_source_references(path)
            self.assertEqual(refs[0]["doi"], "10.1000/native-pdf")
            self.assertEqual(refs[0]["source_location"]["pdf_page_index"], 1)


class TargetAndMetadataSafetyTests(unittest.TestCase):
    def test_target_existing_does_not_call_external_lookup_or_write(self):
        backend = MemoryBackend([item(members=("TARGET",))])
        with patch.object(z.urllib.request, "urlopen", side_effect=AssertionError("network")), \
             patch.object(ing.Plugin, "rpc", side_effect=AssertionError("plugin write")):
            result = ing.reconcile(sources(), backend, "TARGET", checks=3, delay=0)
        self.assertTrue(result["complete"])
        self.assertEqual((backend.adds, backend.imports), (0, 0))


if __name__ == "__main__":
    unittest.main()
