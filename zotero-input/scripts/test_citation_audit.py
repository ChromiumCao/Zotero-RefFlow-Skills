"""Synthetic citation audit checks; no online retrieval or Zotero writes."""
from copy import deepcopy
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import citation_audit as ca
import citation_handoff as ch
import ingest as ing
import zotero as z

SENTENCE = "The outcome improved [3]."
SOURCE = "The outcome improved in the tested population."


def sample():
    return {"schema_version": ca.REVIEW_VERSION,
            "references": [{"reference_id": "D1-R3", "document_id": "D1", "label": "[3]",
                            "raw_text": "Synthetic bibliography [3]", "original": {"doi": "10.1000/audit",
                            "pmid": "12345678", "title": "Synthetic controlled study", "first_author": "Smith", "year": "2024"},
                            "metadata": {"doi": "10.1000/audit", "pmid": "12345678", "title": "Synthetic controlled study",
                                         "creators": [{"lastName": "Smith", "firstName": "Anna"}], "year": "2024", "abstract": SOURCE},
                            "metadata_provenance": {"kind": "publisher", "url": "https://example.org/paper"},
                            "identity_reviewed": True, "identity_assessment": "Synthetic record comparison.",
                            "source_units": [{"source_id": "P3", "kind": "full_text", "path": "synthetic.pdf", "text": SOURCE,
                                              "identity_verified": True, "identity_assessment": "Synthetic header identity.",
                                              "locator_verified": True, "pdf_page_index": 3, "printed_page_label": "S12",
                                              "section": "Results", "text_block_index_on_page": 2, "paragraph_label": "Results paragraph 2",
                                              "paragraph_verified": True, "paragraph_verification": "Synthetic visual review."}]}],
            "documents": [{"document_id": "D1", "title": "Synthetic manuscript", "text": SENTENCE,
                           "citations": [{"citation_id": "C1", "sentence": SENTENCE, "label": "[3]",
                                          "source_location": {"opaque": [1, "p2"]},
                                          "checks": [{"reference_id": "D1-R3", "reviewed": True, "verdict": "supported",
                                                      "assessment": "Synthetic semantic review; studied scope agrees.",
                                                      "search_scope": "Results, physical page 3", "reviewed_source_ids": ["P3"],
                                                      "evidence": [{"source_id": "P3", "quote": SOURCE}]}]}]}]}


def build(data=None):
    return ca.build_report(data or sample(), approved=True, cost_warning_acknowledged=True)


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for target in ((z.urllib.request, "urlopen"), (ing, "Plugin"), (ing, "LocalBackend")):
            guard = patch.object(*target, side_effect=AssertionError("Audit must not search/write/discover Zotero"))
            guard.start()
            self.addCleanup(guard.stop)

    def test_complete_preserves_exact_positions_and_distinct_page_labels(self):
        data = sample()
        before = deepcopy(data)
        report = build(data)
        self.assertTrue(report["complete"])
        row = report["rows"][0]
        self.assertEqual(row["sentence"], SENTENCE)
        self.assertEqual((row["start"], row["end"]), (0, len(SENTENCE)))
        self.assertEqual(row["source"]["pdf_page_index"], 3)
        self.assertEqual(row["source"]["printed_page_label"], "S12")
        self.assertEqual(row["source_location"], {"opaque": [1, "p2"]})
        self.assertFalse(report["zotero_written"])
        self.assertEqual(data, before)

    def test_missing_consent_blocks_all_commands_before_source_reads(self):
        for args in (["template", "--input", "missing.json", "--out", str(self.root / "a.json")],
                     ["evidence-pages", "--pdf", "missing.pdf", "--page", "1", "--out", str(self.root / "b.json")],
                     ["report", "--review", "missing.json", "--outdir", str(self.root / "out")]):
            for flag in ([], ["--approve-audit"], ["--acknowledge-token-cost"]):
                with self.subTest(args=args, flag=flag), patch.object(Path, "read_text", side_effect=AssertionError("Source read")), \
                     patch.object(ca.pdf_reader, "read_pages", side_effect=AssertionError("PDF read")):
                    self.assertEqual(ca.main(args + flag), 3)
        self.assertEqual(list(self.root.iterdir()), [])
        with self.assertRaises(PermissionError):
            ca.build_report(sample())
        with self.assertRaises(PermissionError):
            ca.extract_pages("missing.pdf", [1], approved=True)

    def test_template_never_carries_old_approval_and_does_not_merge_citations(self):
        data = sample()
        citation = data["documents"][0]["citations"][0]
        citation["reference_ids"] = ["D1-R3", "unknown"]
        draft = ca.template(data, approved=True, cost_warning_acknowledged=True)
        self.assertFalse(draft["references"][0]["identity_reviewed"])
        checks = draft["documents"][0]["citations"][0]["checks"]
        self.assertEqual([c["reference_id"] for c in checks], ["D1-R3", "unknown"])
        self.assertTrue(all(not c["reviewed"] and not c["evidence"] for c in checks))

    def test_doi_success_does_not_hide_pmid_title_author_or_year_conflict(self):
        for key, value in (("pmid", "87654321"), ("title", "Another article title"), ("title", "ABC"),
                           ("first_author", "Jones"), ("year", "2023"), ("doi", "10.1000/different")):
            with self.subTest(key=key):
                data = sample()
                data["references"][0]["original"][key] = value
                row = build(data)["rows"][0]
                self.assertEqual(row["identity"]["status"], "conflict")
                self.assertEqual(row["verdict"], "unverifiable")
                self.assertFalse(row["locator_complete"])

    def test_missing_metadata_missing_identifier_and_ambiguous_candidates(self):
        for change in ("metadata", "pmid", "ambiguous", "candidates", "review"):
            data = sample()
            ref = data["references"][0]
            if change == "metadata":
                ref.pop("metadata")
            elif change == "pmid":
                ref["metadata"].pop("pmid")
            elif change == "ambiguous":
                ref["ambiguous"] = True
            elif change == "candidates":
                ref["candidate_identities"] = [{"doi": "a"}, {"doi": "b"}]
            else:
                ref["identity_reviewed"] = False
            report = build(data)
            self.assertFalse(report["identity_complete"])
            self.assertEqual(report["rows"][0]["verdict"], "unverifiable")

    def test_conflicting_aliases_raw_identifiers_and_malformed_pmid_are_pending(self):
        for change in ("alias", "raw", "scientific", "doi"):
            data = sample()
            ref = data["references"][0]
            if change == "alias":
                ref["original"]["DOI"] = "10.1000/another"
            elif change == "raw":
                ref["raw_text"] = "Original entry PMID: 87654321"
            elif change == "scientific":
                ref["original"]["pmid"] = "1.2345678E7"
            else:
                ref["original"]["doi"] = "not-a-doi"
            self.assertFalse(build(data)["identity_complete"])

    def test_chinese_reference_and_source_confirmed_aliases(self):
        data = sample()
        ref = data["references"][0]
        ref["original"] = {"title": "中文文献研究", "first_author": "张三", "year": "2024"}
        ref["metadata"].update(title="English confirmed title", title_aliases=["中文文献研究"],
                               creators=[{"lastName": "张三"}])
        self.assertTrue(build(data)["identity_complete"])
        ref["metadata"].pop("title_aliases")
        self.assertFalse(build(data)["identity_complete"])

    def test_shared_doi_different_pmids_across_documents_cannot_merge(self):
        data = sample()
        ref = deepcopy(data["references"][0])
        ref["reference_id"] = "D2-R3"
        ref["original"]["pmid"] = ref["metadata"]["pmid"] = "87654321"
        data["references"].append(ref)
        report = build(data)
        self.assertTrue(all(row["identity"]["status"] == "conflict" for row in report["rows"]))

    def test_positive_partial_negative_contradiction_and_unknown_all_remain(self):
        for verdict in sorted(ca.VERDICTS):
            data = sample()
            check = data["documents"][0]["citations"][0]["checks"][0]
            check["verdict"] = verdict
            if verdict in {"not_supported", "unverifiable"}:
                check["evidence"] = []
            report = build(data)
            self.assertEqual(report["rows"][0]["verdict"], verdict)
            self.assertEqual(report["counts"][verdict], 1)
            if verdict == "not_supported":
                self.assertTrue(report["semantic_complete"])
                self.assertFalse(report["locator_complete"])

    def test_negative_requires_actual_reading_and_limited_scope(self):
        data = sample()
        check = data["documents"][0]["citations"][0]["checks"][0]
        check.update(verdict="not_supported", evidence=[], reviewed_source_ids=[])
        self.assertEqual(build(data)["rows"][0]["verdict"], "unverifiable")
        check["reviewed_source_ids"] = ["P3"]
        check["search_scope"] = ""
        self.assertEqual(build(data)["rows"][0]["verdict"], "unverifiable")

    def test_keyword_hits_and_unreviewed_assessment_do_not_pass(self):
        for key, value in (("reviewed", False), ("assessment", ""), ("evidence", [])):
            data = sample()
            data["documents"][0]["citations"][0]["checks"][0][key] = value
            self.assertFalse(build(data)["semantic_complete"])

    def test_fabricated_or_wrong_source_quote_downgrades_without_disappearing(self):
        for key, value in (("quote", "The mortality risk decreased by 99%."), ("source_id", "other-paper")):
            data = sample()
            data["documents"][0]["citations"][0]["checks"][0]["evidence"][0][key] = value
            row = build(data)["rows"][0]
            self.assertEqual(row["verdict"], "unverifiable")
            self.assertEqual(row["requested_verdict"], "supported")
            self.assertTrue(row["issues"])

    def test_unattributed_source_and_wrong_reference_unit_block_evidence(self):
        for updates in ({"identity_verified": False}, {"identity_assessment": ""}, {"reference_id": "D9-R3"}):
            data = sample()
            data["references"][0]["source_units"][0].update(updates)
            self.assertFalse(build(data)["semantic_complete"])

    def test_abstract_support_is_not_fulltext_page_or_paragraph_completion(self):
        data = sample()
        unit = data["references"][0]["source_units"][0]
        for key in ("pdf_page_index", "printed_page_label", "paragraph_label", "paragraph_verified", "paragraph_verification", "text_block_index_on_page"):
            unit.pop(key)
        unit.update(kind="abstract", section="Abstract", url="https://example.org/paper")
        report = build(data)
        self.assertTrue(report["semantic_complete"])
        self.assertFalse(report["locator_complete"])
        unit["pdf_page_index"] = 3
        self.assertFalse(build(data)["semantic_complete"])

    def test_abstract_metadata_disagreement_is_not_evidence(self):
        data = sample()
        data["references"][0]["source_units"] = [{"source_id": "P3", "kind": "abstract",
                                                  "text": "The outcome was not measured.", "url": "https://example.org/paper",
                                                  "identity_verified": True, "identity_assessment": "Reviewed source"}]
        data["documents"][0]["citations"][0]["checks"][0]["evidence"][0]["quote"] = "The outcome was not measured."
        self.assertFalse(build(data)["semantic_complete"])

    def test_text_block_is_not_automatically_a_natural_paragraph(self):
        data = sample()
        unit = data["references"][0]["source_units"][0]
        for key in ("paragraph_label", "paragraph_verified", "paragraph_verification"):
            unit.pop(key)
        report = build(data)
        self.assertTrue(report["semantic_complete"])
        self.assertFalse(report["locator_complete"])
        unit["paragraph_label"] = "2"
        self.assertFalse(build(data)["semantic_complete"])

    def test_pdf_extraction_is_bounded_hashes_snapshot_and_keeps_blocks(self):
        pdf = self.root / "source.pdf"
        pdf.write_bytes(b"%PDF-synthetic")
        with patch.object(ca.pdf_reader, "read_pages", return_value=[(3, "First block.\n\nSecond block.")]) as reader:
            units = ca.extract_pages(pdf, [3, 3], approved=True, cost_warning_acknowledged=True)
            reader.assert_called_once_with(pdf.resolve(), [3])
        self.assertEqual(len(units), 2)
        self.assertEqual([u["text_block_index_on_page"] for u in units], [1, 2])
        self.assertTrue(all(not u["paragraph_verified"] and not u["identity_verified"] for u in units))
        self.assertTrue(all(u["content_hash"] == ch.digest(u["text"]) for u in units))
        for pages in ([], list(range(1, 7)), [0], [True]):
            with self.assertRaises(ValueError):
                ca.extract_pages(pdf, pages, approved=True, cost_warning_acknowledged=True)

    def test_changed_source_cache_cannot_become_verified_evidence(self):
        data = sample()
        data["references"][0]["source_units"][0]["content_hash"] = ch.digest("Old text")
        self.assertFalse(build(data)["semantic_complete"])

    def test_pdf_change_during_extraction_does_not_publish_cache(self):
        pdf = self.root / "source.pdf"
        pdf.write_bytes(b"%PDF-before")
        def changing_file(*args):
            pdf.write_bytes(b"%PDF-after")
            return [(1, "Source text")]
        with patch.object(ca.pdf_reader, "read_pages", side_effect=changing_file), self.assertRaisesRegex(ValueError, "changed"):
            ca.extract_pages(pdf, [1], approved=True, cost_warning_acknowledged=True)

    def test_multi_document_same_numbers_and_multiple_quotes_preserve_every_row(self):
        data = sample()
        document = deepcopy(data["documents"][0])
        document["document_id"] = "D2"
        data["documents"].append(document)
        check = document["citations"][0]["checks"][0]
        check["evidence"].append({"source_id": "P3", "quote": "tested population"})
        report = build(data)
        self.assertEqual(len(report["rows"]), 3)
        self.assertEqual([r["document_id"] for r in report["rows"]], ["D1", "D2", "D2"])
        self.assertEqual([r["citation_id"] for r in report["rows"]], ["C1", "C1", "C1"])
        self.assertTrue(report["complete"])

    def test_repeated_sentence_requires_exact_offsets(self):
        data = sample()
        doc = data["documents"][0]
        doc["text"] = SENTENCE + " " + SENTENCE
        with self.assertRaisesRegex(ValueError, "offsets"):
            build(data)
        doc["citations"][0].update(start=0, end=len(SENTENCE))
        other = deepcopy(doc["citations"][0])
        other.update(citation_id="C2", start=len(SENTENCE) + 1, end=len(doc["text"]))
        doc["citations"].append(other)
        self.assertEqual(len(build(data)["rows"]), 2)

    def test_unlinked_and_unused_bibliography_records_are_not_dropped(self):
        data = sample()
        data["documents"][0]["citations"][0]["checks"][0]["reference_id"] = "missing"
        report = build(data)
        self.assertEqual(len(report["rows"]), 2)
        self.assertEqual([r["verdict"] for r in report["rows"]], ["unverifiable", "not_applicable"])
        self.assertFalse(report["complete"])
        data["documents"][0]["citations"][0]["checks"] = []
        self.assertEqual(len(build(data)["rows"]), 2)

    def test_schema_duplicate_ids_and_unknown_verdict_rejected(self):
        for mutation in ("schema", "reference", "source", "document", "citation", "check", "verdict"):
            data = sample()
            if mutation == "schema":
                data["schema_version"] = "v2"
            elif mutation == "reference":
                data["references"].append(deepcopy(data["references"][0]))
            elif mutation == "source":
                data["references"][0]["source_units"].append(deepcopy(data["references"][0]["source_units"][0]))
            elif mutation == "document":
                data["documents"].append(deepcopy(data["documents"][0]))
            elif mutation == "citation":
                data["documents"][0]["citations"].append(deepcopy(data["documents"][0]["citations"][0]))
            elif mutation == "check":
                data["documents"][0]["citations"][0]["checks"] *= 2
            else:
                data["documents"][0]["citations"][0]["checks"][0]["verdict"] = "certainly_correct"
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                build(data)

    def test_report_cli_writes_three_offline_outputs_and_never_overwrites(self):
        source = self.root / "review.json"
        source.write_text(json.dumps(sample()), encoding="utf-8")
        directory = self.root / "out"
        args = ["report", "--review", str(source), "--outdir", str(directory),
                "--approve-audit", "--acknowledge-token-cost"]
        with patch.object(ca.pdf_reader, "read_pages", side_effect=AssertionError("Exporter must reuse cache")):
            self.assertEqual(ca.main(args), 0)
        self.assertEqual(len(list(directory.iterdir())), 3)
        json_file = directory / "citation-audit.json"
        result = json.loads(json_file.read_text(encoding="utf-8"))
        self.assertEqual(result["schema_version"], ca.REPORT_VERSION)
        self.assertIn(ca.markdown_cell(SENTENCE), (directory / "citation-audit.md").read_text(encoding="utf-8"))
        self.assertTrue((directory / "citation-audit.csv").read_bytes().startswith(b"\xef\xbb\xbf"))
        before = {p.name: p.read_bytes() for p in directory.iterdir()}
        with self.assertRaises(FileExistsError):
            ca.main(args)
        self.assertEqual(before, {p.name: p.read_bytes() for p in directory.iterdir()})
        self.assertEqual(json.loads(source.read_text(encoding="utf-8")), sample())

    def test_incomplete_report_still_generates_table(self):
        data = sample()
        data["references"][0]["identity_reviewed"] = False
        source = self.root / "review.json"
        source.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(ca.main(["report", "--review", str(source), "--outdir", str(self.root / "out"),
                                  "--approve-audit", "--acknowledge-token-cost"]), 2)
        self.assertIn("无法核实", (self.root / "out/citation-audit.md").read_text(encoding="utf-8"))

    def test_safe_csv_and_markdown_keep_exact_values_in_json(self):
        report = build()
        text = ' =HYPERLINK("evil") | <script>alert(1)</script> [x](javascript:alert(1))\n`text`'
        report["rows"][0]["sentence"] = text
        paths = ca.export_report(report, self.root / "out")
        raw = json.loads(paths[0].read_text(encoding="utf-8"))
        self.assertEqual(raw["rows"][0]["sentence"], text)
        markdown = paths[1].read_text(encoding="utf-8")
        self.assertNotIn("<script>", markdown)
        self.assertIn("&#124;", markdown)
        self.assertIn(r"\[x\]", markdown)
        csv_rows = list(csv.reader(io.StringIO(paths[2].read_text(encoding="utf-8-sig"))))
        self.assertEqual(csv_rows[1][3], "'" + text)
        for value in ("+cmd", "-cmd", "@cmd", "\t=cmd"):
            self.assertEqual(ca.csv_cell(value), "'" + value)

    def test_source_conflict_preflight_blocks_all_publication(self):
        report = build()
        directory = self.root / "out"
        report["documents"][0]["source_path"] = str(directory / "citation-audit.md")
        with self.assertRaises(FileExistsError):
            ca.export_report(report, directory)
        self.assertFalse(directory.exists())


if __name__ == "__main__":
    unittest.main()
