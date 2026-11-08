"""Synthetic text/metadata tests; no real paper claims or Zotero writes."""
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import importlib.util
import sys

import citation_handoff as ch
import ingest as ing
import zotero as z
from test_ingest import MemoryBackend

SENTENCE = "An intervention improved the measured outcome."
ABSTRACT = "The intervention improved the measured outcome in the tested population."
ENTRY = {"doi": "10.1000/cited", "pmid": "12345678", "title": "A controlled study",
         "creators": [{"lastName": "Smith", "firstName": "Anna"}], "year": "2024",
         "journal": "Test Journal", "abstract": ABSTRACT}


def payload():
    return {"schema_version": ch.REVIEW_VERSION,
            "references": [{"reference_id": "R1", "metadata": deepcopy(ENTRY),
                            "metadata_provenance": {"kind": "publisher", "url": "https://example.org/article"},
                            "source_units": [{"source_id": "A", "kind": "abstract", "url": "https://example.org/article",
                                              "text": ABSTRACT}]}],
            "documents": [{"document_id": "D1", "title": "Synthetic manuscript", "text": SENTENCE,
                           "citations": [{"citation_id": "C1", "sentence": SENTENCE,
                                          "supports": [{"reference_id": "R1", "source_id": "A", "quote": "improved the measured outcome",
                                                        "grade": "strong", "claim_compatible": True,
                                                        "assessment": "The study tested the stated outcome; synthetic test only."}]}]}]}


class CitationTests(unittest.TestCase):
    def setUp(self):
        self.guard = patch.object(z.urllib.request, "urlopen", side_effect=AssertionError("Network blocked"))
        self.guard.start()
        self.addCleanup(self.guard.stop)

    def test_uncited_text_becomes_cited_with_bibliography_and_mapping(self):
        bundle = ch.prepare(payload())
        self.assertTrue(bundle["complete"])
        self.assertEqual(bundle["documents"][0]["cited_text"], SENTENCE + "[1]")
        self.assertEqual(bundle["citation_mappings"][0]["sentence"], SENTENCE)
        self.assertEqual(bundle["bibliography"][0]["number"], 1)
        self.assertEqual(bundle["next_action"], "ask_zotero_import")
        self.assertEqual(bundle["consent"], {"import": False, "notes": False})

    def test_missing_abstract_or_fulltext_quote_cannot_be_inserted(self):
        data = payload()
        data["documents"][0]["citations"][0]["supports"][0]["quote"] = "A fabricated result"
        with self.assertRaisesRegex(ValueError, "quotation"):
            ch.prepare(data)

    def test_metadata_only_or_contradictory_support_stays_unresolved(self):
        for grade, compatible in (("metadata-only candidate", True), ("contradictory", True), ("strong", False)):
            data = payload()
            support = data["documents"][0]["citations"][0]["supports"][0]
            support.update(grade=grade, claim_compatible=compatible)
            bundle = ch.prepare(data)
            self.assertFalse(bundle["complete"])
            self.assertEqual(bundle["documents"][0]["cited_text"], SENTENCE)
            self.assertFalse(bundle["bibliography"])

    def test_assessment_required(self):
        data = payload()
        data["documents"][0]["citations"][0]["supports"][0].pop("assessment")
        with self.assertRaises(ValueError):
            ch.prepare(data)

    def test_repeated_sentences_require_explicit_positions(self):
        data = payload()
        data["documents"][0]["text"] = SENTENCE + " " + SENTENCE
        with self.assertRaisesRegex(ValueError, "explicit"):
            ch.prepare(data)
        citation = data["documents"][0]["citations"][0]
        citation.update(start=len(SENTENCE) + 1, end=len(data["documents"][0]["text"]))
        self.assertEqual(ch.prepare(data)["documents"][0]["cited_text"], SENTENCE + " " + SENTENCE + "[1]")

    def test_positions_and_overlaps_are_checked(self):
        data = payload()
        data["documents"][0]["citations"][0].update(start=1, end=len(SENTENCE))
        with self.assertRaises(ValueError):
            ch.prepare(data)
        data = payload()
        duplicate = deepcopy(data["documents"][0]["citations"][0])
        duplicate["citation_id"] = "C2"
        data["documents"][0]["citations"].append(duplicate)
        with self.assertRaisesRegex(ValueError, "Overlapping"):
            ch.prepare(data)

    def test_duplicate_document_or_reference_ids_are_rejected(self):
        for field in ("documents", "references"):
            data = payload()
            data[field].append(deepcopy(data[field][0]))
            with self.assertRaises(ValueError):
                ch.prepare(data)

    def test_multiple_documents_share_one_reference_but_keep_all_mappings(self):
        data = payload()
        other = deepcopy(data["documents"][0])
        other["document_id"] = "D2"
        data["documents"].append(other)
        bundle = ch.prepare(data)
        self.assertEqual(len(bundle["bibliography"]), 1)
        refs = ch.source_references(bundle)
        self.assertEqual([r["document_id"] for r in refs], ["D1", "D2"])
        self.assertEqual([r["reference_id"] for r in refs], ["R1", "R1"])
        backend = MemoryBackend()
        result = ch.import_bundle(bundle, backend, "TARGET", approved=True, checks=1, delay=0)
        self.assertTrue(result["complete"])
        self.assertEqual(backend.imports, 1)
        self.assertEqual(len(result["mappings"]), 2)
        self.assertEqual(result["citation_bundle_id"], bundle["bundle_id"])

    def test_shared_doi_with_conflicting_pmid_is_rejected(self):
        data = payload()
        other = deepcopy(data["references"][0])
        other["reference_id"] = "R2"
        other["metadata"]["pmid"] = "87654321"
        data["references"].append(other)
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            ch.prepare(data)

    def test_locators_and_abstract_pages_are_not_invented(self):
        for fields in ({"pdf_page_index": 0}, {"pdf_page_index": 2}, {"section": "Results"}):
            data = payload()
            data["references"][0]["source_units"][0].update(fields)
            with self.assertRaises(ValueError):
                ch.prepare(data)
        bundle = ch.prepare(payload())
        source = bundle["citation_mappings"][0]["supports"][0]["source"]
        self.assertEqual(source["section"], "Abstract")
        self.assertNotIn("pdf_page_index", source)
        self.assertNotIn("text", source)

    def test_unknown_title_is_not_required_until_notes(self):
        data = payload()
        data["documents"][0].pop("title")
        self.assertTrue(ch.prepare(data)["complete"])

    def test_bundle_mutation_is_rejected(self):
        bundle = ch.prepare(payload())
        bundle["documents"][0]["text"] = "Changed later"
        with self.assertRaises(ValueError):
            ch.source_references(bundle)

    def test_import_requires_its_own_consent(self):
        backend = Mock()
        with self.assertRaises(PermissionError):
            ch.import_bundle(ch.prepare(payload()), backend, "TARGET")
        backend.assert_not_called()
        self.assertEqual(backend.method_calls, [])

    def test_declined_import_cli_does_not_initialize_zotero(self):
        with patch.object(ing, "LocalBackend", side_effect=AssertionError("No Zotero access")), patch("sys.stdout", new_callable=io.StringIO):
            code = ch.main(["import", "--bundle", "does-not-exist.json", "--mapping-out", "unused.json"])
        self.assertEqual(code, 3)

    def test_declined_notes_cli_does_not_read_files_or_zotero(self):
        with patch.object(ch, "read_json", side_effect=AssertionError("No file access")), patch("sys.stdout", new_callable=io.StringIO):
            code = ch.main(["notes-plan", "--bundle", "missing", "--mapping", "missing", "--outdir", "unused", "--approve-notes"])
        self.assertEqual(code, 3)

    def test_output_files_and_overwrite_protection(self):
        bundle = ch.prepare(payload())
        with tempfile.TemporaryDirectory() as root:
            paths = ch.write_bundle(bundle, Path(root))
            self.assertEqual(len(paths), 4)
            self.assertIn(SENTENCE + "[1]", paths[-1].read_text(encoding="utf-8"))
            self.assertIn("DO  - 10.1000/cited", paths[2].read_text(encoding="utf-8"))
            with self.assertRaises(FileExistsError):
                ch.write_bundle(bundle, Path(root))

    def test_chinese_sentence_offsets_and_exact_quotes(self):
        data = payload()
        text = "\u8be5\u5e72\u9884\u6539\u5584\u4e86\u6d4b\u91cf\u6307\u6807\u3002"
        quote = "\u6539\u5584\u4e86\u6d4b\u91cf\u6307\u6807"
        data["documents"][0]["text"] = text
        data["documents"][0]["citations"][0]["sentence"] = text
        data["documents"][0]["citations"][0]["supports"][0]["quote"] = quote
        data["references"][0]["metadata"]["abstract"] = text
        data["references"][0]["source_units"][0]["text"] = text
        self.assertEqual(ch.prepare(data)["documents"][0]["cited_text"], text + "[1]")

    def test_search_is_bundled_and_all_scope_does_not_narrow_silently(self):
        search = ch.load_search()
        self.assertTrue(search.in_scope("A local Chinese journal", "all"))
        self.assertFalse(search.in_scope("A local Chinese journal", "cns"))
        self.assertEqual(search.parse_args([]).scope, "all")

    def test_search_safe_xml_runs_without_defusedxml_package(self):
        path = Path(ch.__file__).parent / "_vendor" / "nature_citation.py"
        spec = importlib.util.spec_from_file_location("search_without_defusedxml", path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        with patch.dict(sys.modules, {"defusedxml": None}):
            spec.loader.exec_module(module)
        self.assertEqual(module.ET.fromstring(b"<root><item>text</item></root>").tag, "root")
        for data in (b'<!DOCTYPE r [<!ENTITY x "expanded">]><r>&x;</r>', b'<r>\x00</r>', b"a" * (10 * 1024 * 1024 + 1)):
            with self.assertRaises(ValueError):
                module.ET.fromstring(data)

    def test_review_template_keeps_large_metadata_local_and_unapproved(self):
        record = {"doi": ENTRY["doi"], "pmid": ENTRY["pmid"], "title": ENTRY["title"], "year": ENTRY["year"],
                  "journal": ENTRY["journal"], "abstract": ABSTRACT, "authors": ["Smith, Anna"], "source_catalog": "Crossref"}
        candidate = {"references": [record], "segments": [{"id": "S1", "text": SENTENCE, "references": [record]}]}
        draft = ch.review_template(SENTENCE, candidate)
        self.assertEqual(draft["references"][0]["metadata"]["creators"][0]["firstName"], "Anna")
        self.assertEqual(draft["references"][0]["source_units"][0]["text"], ABSTRACT)
        self.assertFalse(draft["documents"][0]["citations"][0]["supports"][0]["claim_compatible"])
        self.assertFalse(ch.prepare(draft)["complete"])

    def test_fulltext_page_extraction_without_consent_does_not_read_file(self):
        with patch("sys.stdout", new_callable=io.StringIO), patch.object(ch, "read_json", side_effect=AssertionError("No reads")):
            code = ch.main(["evidence-pages", "--pdf", "missing.pdf", "--page", "3", "--out", "unused.json"])
        self.assertEqual(code, 3)

    def test_numbering_follows_text_order_not_review_array_order(self):
        data = payload()
        other = deepcopy(data["references"][0])
        other["reference_id"] = "R2"
        other["metadata"].update(doi="10.1000/second", pmid=None, title="A different controlled study")
        data["references"].append(other)
        second_sentence = "A second claim is reviewed."
        other_citation = deepcopy(data["documents"][0]["citations"][0])
        other_citation.update(citation_id="C2", sentence=second_sentence)
        other_citation["supports"][0]["reference_id"] = "R2"
        data["documents"][0]["text"] = SENTENCE + " " + second_sentence
        data["documents"][0]["citations"].insert(0, other_citation)
        self.assertEqual(ch.prepare(data)["documents"][0]["cited_text"], SENTENCE + "[1] " + second_sentence + "[2]")

    def test_enriched_fulltext_evidence_can_remap_without_importing_again(self):
        original = ch.prepare(payload())
        backend = MemoryBackend()
        prior = ch.import_bundle(original, backend, "TARGET", approved=True, checks=1, delay=0)
        data = payload()
        data["references"][0]["source_units"][0].update(kind="full_text", section="Results", locator_verified=True)
        enriched = ch.prepare(data)
        self.assertNotEqual(enriched["bundle_id"], original["bundle_id"])
        backend.import_entry = Mock(side_effect=AssertionError("No writes"))
        backend.add = Mock(side_effect=AssertionError("No writes"))
        updated = ch.map_existing(enriched, prior, backend)
        self.assertTrue(updated["complete"])
        self.assertEqual(updated["citation_bundle_id"], enriched["bundle_id"])
        self.assertEqual(updated["mappings"][0]["item"], prior["mappings"][0]["item"])
        backend.import_entry.assert_not_called()
        backend.add.assert_not_called()

    def test_remap_missing_or_changed_parent_does_not_choose_another_or_write(self):
        original = ch.prepare(payload())
        backend = MemoryBackend()
        prior = ch.import_bundle(original, backend, "TARGET", approved=True, checks=1, delay=0)
        backend.rows[0]["data"]["DOI"] = "10.1000/another"
        self.assertFalse(ch.map_existing(original, prior, backend)["complete"])
        self.assertEqual(backend.imports, 1)


if __name__ == "__main__":
    unittest.main()
