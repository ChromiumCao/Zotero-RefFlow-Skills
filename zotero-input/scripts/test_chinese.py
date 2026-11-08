"""Chinese ingestion regressions with blocked network and isolated library state."""
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from xml.sax.saxutils import escape
import zipfile

import ingest as ing
import reference_metadata as rm
import zotero as z
from test_ingest import MemoryBackend, item, sources

TITLE = "\u4e2d\u56fd\u6210\u5e74\u4eba\u7cd6\u5c3f\u75c5\u9632\u6cbb\u7684\u591a\u4e2d\u5fc3\u7814\u7a76"
SHORT = "\u7cd6\u5c3f\u75c5\u9632\u6cbb"
AUTHOR = "\u5f20\u4e09"
JOURNAL = "\u4e2d\u534e\u533b\u5b66\u6742\u5fd7"
ABSTRACT = "\u76ee\u7684\uff1a\u8bc4\u4f30\u5e72\u9884\u6548\u679c\u3002\u7ed3\u679c\uff1a\u89c2\u5bdf\u5230\u6307\u6807\u6539\u5584\u3002"
ENTRY = {"doi": "10.1000/chinese", "title": SHORT,
         "creators": [{"firstName": "\u4e09", "lastName": "\u5f20"}],
         "year": "2024", "journal": JOURNAL, "abstract": ABSTRACT}
PROVENANCE = {"kind": "database", "url": "https://oversea.cnki.net/example-record"}


class ChineseBackend(MemoryBackend):
    def import_entry(self, entry, collection):
        self.imports += 1
        key = "NEW" + str(self.imports)
        self.rows.append({"key": key, "library": self.library.json(), "data": {
            "key": key, "itemType": "journalArticle", "collections": [collection],
            "DOI": entry.get("doi"), "title": entry["title"], "creators": deepcopy(entry["creators"]),
            "date": entry.get("year"), "abstractNote": entry.get("abstract") or "",
            "extra": "PMID: " + str(entry["pmid"]) if entry.get("pmid") else ""}})


class ChineseTests(unittest.TestCase):
    def setUp(self):
        self.guard = patch.object(z.urllib.request, "urlopen", side_effect=AssertionError("Real network blocked"))
        self.guard.start()
        self.addCleanup(self.guard.stop)

    def supplied(self, **changes):
        metadata = {**deepcopy(ENTRY), **changes}
        return {"title": metadata["title"], "first_author": AUTHOR, "year": "2024",
                "metadata": metadata, "metadata_provenance": deepcopy(PROVENANCE)}

    def run_import(self, refs, backend=None, lookup=ing.lookup_metadata):
        backend = backend or ChineseBackend()
        result = ing.reconcile(refs, backend, "TARGET", lookup=lookup, checks=1, delay=0)
        return result, backend

    def chinese_item(self, key="OLD", members=("OTHER",), **changes):
        return item(key, members, DOI=ENTRY["doi"], title=SHORT, date="2024",
                    creators=deepcopy(ENTRY["creators"]), abstractNote=ABSTRACT, extra="", **changes)

    def ris(self, **changes):
        entry = {**deepcopy(ENTRY), **changes}
        return z._doi_to_full_ris(entry)

    def test_chinese_column_aliases_and_fullwidth_doi_header(self):
        fields = {"\u9898\u540d": SHORT, "\u4f5c\u8005": AUTHOR, "\u51fa\u7248\u5e74": "2024",
                  "\u7f16\u53f7": "[3]", "\uff24\uff2f\uff29": ENTRY["doi"]}
        ref = z.reference_from_fields(fields)
        self.assertEqual((ref["title"], ref["first_author"], ref["year"], ref["label"], ref["doi"]),
                         (SHORT, AUTHOR, "2024", "[3]", ENTRY["doi"]))

    def test_short_titles_and_unparsed_rows_do_not_disappear(self):
        refs = z.references_from_lines(["\u53c2\u8003\u6587\u732e", "[3] " + SHORT, "[4] ???"])
        self.assertEqual(len(refs), 2)
        self.assertEqual(refs[0]["title"], SHORT)
        result, _ = self.run_import(ing.normalize_sources([{"document_id": "D", "references": refs}]))
        self.assertEqual(len(result["mappings"]), 2)
        self.assertFalse(result["complete"])

    def test_nfkc_title_and_doi_normalization(self):
        self.assertEqual(z.normalize_title(SHORT + "\uff21"), z.normalize_title(SHORT + "A"))
        self.assertEqual(z.normalize_doi("\uff11\uff10\uff0e\uff11\uff10\uff10\uff10/chinese\u3002"), ENTRY["doi"])
        self.assertEqual(z.extract_doi("DOI: \uff11\uff10\uff0e\uff11\uff10\uff10\uff10/chinese\u3002"), ENTRY["doi"])

    def test_english_author_initials_still_match_surnames(self):
        for value in ("Smith A", "Smith A.", "Anna Smith", "Anna Smith; Jones B"):
            with self.subTest(value=value):
                self.assertEqual(ing.author_name(value), "smith")

    def test_marked_chinese_reference_parses_title_author_year(self):
        raw = "[3] " + AUTHOR + ",\u674e\u56db. " + SHORT + "[J]. " + JOURNAL + ",2024,1(1):1-3."
        ref = z.references_from_lines([raw])[0]
        self.assertEqual((ref["title"], ref["first_author"], ref["year"]), (SHORT, AUTHOR, "2024"))
        self.assertEqual(ref["raw_text"], raw)
        self.assertEqual(ref["label"], "[3]")

    def test_unmarked_text_does_not_get_fabricated_authors(self):
        ref = z.references_from_lines([SHORT + " " + AUTHOR])[0]
        self.assertIsNone(ref["first_author"])

    def test_short_chinese_title_needs_corroboration(self):
        target = self.chinese_item(members=("TARGET",))
        found, detail = ing.match_reference({"title": SHORT}, [target], ing.Library())
        self.assertIsNone(found)
        self.assertTrue(detail["ambiguous"])
        for ref in ({"title": SHORT, "first_author": AUTHOR}, {"title": SHORT, "year": "2024"},
                    {"title": SHORT, "doi": ENTRY["doi"]}):
            with self.subTest(ref=ref):
                self.assertIsNotNone(ing.match_reference(ref, [target], ing.Library())[0])

    def test_full_chinese_name_matches_explicit_split_name(self):
        found, detail = ing.match_reference({"doi": ENTRY["doi"], "first_author": AUTHOR},
                                            [self.chinese_item()], ing.Library())
        self.assertIsNotNone(found)
        self.assertFalse(detail["ambiguous"])
        self.assertIn(AUTHOR, ing.author_variants(ENTRY["creators"]))

    def test_chinese_name_with_spacing_and_compound_surname(self):
        self.assertEqual(ing.author_name("\u5f20 \u4e09"), AUTHOR)
        value = {"lastName": "\u6b27\u9633", "firstName": "\u4fee"}
        self.assertIn("\u6b27\u9633\u4fee", ing.author_variants(value))

    def test_other_author_or_conflicting_doi_does_not_match(self):
        for fields in ({"first_author": "\u674e\u56db"}, {"doi": "10.1000/other"}):
            found, detail = ing.match_reference({"title": SHORT, "year": "2024", **fields},
                                                [self.chinese_item()], ing.Library())
            self.assertIsNone(found)
            self.assertTrue(detail["ambiguous"])

    def test_multiple_short_title_matches_stay_pending(self):
        backend = MemoryBackend([self.chinese_item("A"), self.chinese_item("B")])
        result, backend = self.run_import(sources({"title": SHORT, "first_author": AUTHOR}), backend)
        self.assertFalse(result["complete"])
        self.assertEqual((backend.adds, backend.imports), (0, 0))

    def test_target_existing_chinese_item_has_no_lookup(self):
        backend = MemoryBackend([self.chinese_item(members=("TARGET",))])
        result, _ = self.run_import(sources({"title": SHORT, "first_author": AUTHOR}), backend,
                                    Mock(side_effect=AssertionError("No lookup expected")))
        self.assertEqual(result["counts"], {"target_existing": 1})

    def test_chinese_reuse_preserves_collections(self):
        backend = MemoryBackend([self.chinese_item()])
        result, _ = self.run_import(sources({"title": SHORT, "first_author": AUTHOR}), backend)
        self.assertEqual(result["counts"], {"reused_added": 1})
        self.assertEqual(set(backend.rows[0]["data"]["collections"]), {"OTHER", "TARGET"})

    def test_chinese_reuse_unavailable_or_failed_maps_verified_fallback(self):
        for mode in ("unavailable", "failed", "noop"):
            with self.subTest(mode=mode):
                backend = ChineseBackend([self.chinese_item()], add_mode=mode)
                result, _ = self.run_import(sources(self.supplied()), backend)
                row = result["mappings"][0]
                self.assertTrue(result["complete"])
                self.assertEqual(row["outcome"], "fallback_import")
                self.assertEqual(row["item"]["item_key"], "NEW1")
                self.assertEqual(row["known_duplicates"][0]["item_key"], "OLD")
                self.assertEqual(row["metadata_provenance"], PROVENANCE)
                self.assertTrue(row["membership_verified"])
                self.assertEqual(backend.rows[0]["data"]["collections"], ["OTHER"])
                self.assertEqual(backend.imports, 1)

    def test_chinese_lost_add_reply_is_verified_without_new_import(self):
        backend = ChineseBackend([self.chinese_item()], add_mode="lost_reply")
        result, _ = self.run_import(sources({"title": SHORT, "first_author": AUTHOR}), backend)
        self.assertEqual(result["counts"], {"reused_added": 1})
        self.assertEqual(result["mappings"][0]["item"]["item_key"], "OLD")
        self.assertEqual(backend.imports, 0)

    def test_chinese_permission_denial_does_not_trigger_fallback(self):
        backend = ChineseBackend([self.chinese_item()], add_mode="denied")
        result, _ = self.run_import(sources(self.supplied()), backend)
        self.assertEqual(result["counts"], {"failed": 1})
        self.assertEqual(backend.imports, 0)

    def test_source_metadata_import_without_external_lookup(self):
        result, backend = self.run_import(sources(self.supplied(doi=None)))
        self.assertTrue(result["complete"])
        self.assertEqual(backend.imports, 1)
        self.assertEqual(result["mappings"][0]["metadata_provenance"], PROVENANCE)
        self.assertEqual(backend.rows[0]["data"]["title"], SHORT)

    def test_supplied_metadata_conflict_provenance_or_authors_blocks_import(self):
        cases = [self.supplied(), self.supplied(), self.supplied(), self.supplied()]
        cases[0]["doi"] = "10.1000/wrong"
        cases[1].pop("metadata_provenance")
        cases[2]["metadata"]["creators"] = []
        cases[3]["metadata_provenance"]["url"] = "file:///untrusted"
        for ref in cases:
            with self.subTest(ref=ref):
                result, backend = self.run_import(sources(ref))
                self.assertFalse(result["complete"])
                self.assertEqual(backend.imports, 0)

    def test_source_metadata_only_reference_can_supply_identity(self):
        ref = {"metadata": deepcopy(ENTRY), "metadata_provenance": deepcopy(PROVENANCE)}
        result, _ = self.run_import(sources(ref))
        self.assertTrue(result["complete"])
        backend = ChineseBackend([self.chinese_item(members=("TARGET",))])
        result, _ = self.run_import(sources(ref), backend)
        self.assertEqual(result["counts"], {"target_existing": 1})
        self.assertEqual(result["mappings"][0]["identifiers"]["doi"], ENTRY["doi"])

    def test_missing_abstract_is_reported_not_fabricated(self):
        result, backend = self.run_import(sources(self.supplied(doi=None, abstract="")))
        self.assertTrue(result["complete"])
        self.assertEqual(backend.rows[0]["data"]["abstractNote"], "")
        self.assertTrue(result["mappings"][0]["anomalies"])

    def test_proven_bilingual_title_alias_links_actual_item(self):
        english = "A clinical study of diabetes prevention"
        ref = self.supplied(doi=None, title=english, title_aliases=[SHORT])
        ref["title"] = SHORT
        result, backend = self.run_import(sources(ref))
        self.assertTrue(result["complete"])
        self.assertEqual(backend.rows[0]["data"]["title"], english)

    def test_unproven_translated_title_is_not_used(self):
        ref = {"title": SHORT, "first_author": AUTHOR, "title_aliases": ["An English clinical study"]}
        candidate = self.chinese_item()
        candidate["data"]["title"] = "An English clinical study"
        found, _ = ing.match_reference(ref, [candidate], ing.Library())
        self.assertIsNone(found)

    def test_ris_import_strips_children_keeps_chinese_url_and_type(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cn.ris"
            text = self.ris(doi=None, ris_type="THES", url=PROVENANCE["url"])
            text = text.replace("ER  -", "N1  - unwanted note\nL1  - file.pdf\nER  -")
            path.write_text(text, encoding="utf-8")
            refs = ing.file_sources([path])
            result, backend = self.run_import(refs)
            self.assertTrue(result["complete"])
            supplied = rm.checked_metadata(refs[0])
            ris = z.clean_ris_for_import(z._doi_to_full_ris(supplied))
            self.assertIn("TY  - THES", ris)
            self.assertIn("UR  - " + PROVENANCE["url"], ris)
            self.assertIn("AB  - " + ABSTRACT, ris)
            self.assertNotIn("N1  -", ris)
            self.assertNotIn("L1  -", ris)
            self.assertEqual(backend.imports, 1)

    def test_ris_incomplete_missing_title_and_missing_authors(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.ris"
            path.write_text("TY  - JOUR\nTI  - " + SHORT, encoding="utf-8")
            with self.assertRaises(ValueError):
                ing.file_sources([path])
            for text in ("TY  - JOUR\nTI  - " + SHORT + "\nER  -\n",
                         "TY  - JOUR\nAU  - " + AUTHOR + "\nER  -\n"):
                path.write_text(text, encoding="utf-8")
                result, backend = self.run_import(ing.file_sources([path]))
                self.assertFalse(result["complete"])
                self.assertEqual(backend.imports, 0)

    def test_ris_legacy_pmid_abstract_moves_to_identifier(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pmid.ris"
            path.write_text(self.ris(abstract="PMID: 12345678"), encoding="utf-8")
            entry = rm.references_from_ris(path)[0]["metadata"]
            self.assertEqual(entry["pmid"], "12345678")
            self.assertEqual(entry["abstract"], "")

    def test_supplied_legacy_abstract_is_identifier_not_abstract(self):
        ref = self.supplied(abstract="PMID: 12345678")
        result, backend = self.run_import(sources(ref))
        self.assertTrue(result["complete"])
        self.assertEqual(backend.rows[0]["data"]["abstractNote"], "")
        self.assertEqual(backend.rows[0]["data"]["extra"], "PMID: 12345678")
        self.assertTrue(result["mappings"][0]["anomalies"])
        result, backend = self.run_import(sources(self.supplied(pmid="87654321", abstract="PMID: 12345678")))
        self.assertFalse(result["complete"])
        self.assertEqual(backend.imports, 0)

    def test_utf8_bom_and_gb18030_csv_decode_without_replacement(self):
        text = "\u9898\u540d,\u4f5c\u8005,\u5e74\u4efd\n" + SHORT + "," + AUTHOR + ",2024\n"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "refs.csv"
            for encoding in ("utf-8-sig", "gb18030"):
                with self.subTest(encoding=encoding):
                    path.write_bytes(text.encode(encoding))
                    refs = z.extract_source_references(path)
                    self.assertEqual(len(refs), 1)
                    self.assertEqual(refs[0]["title"], SHORT)
                    self.assertEqual(refs[0]["source_encoding"], encoding)
                    self.assertNotIn("\ufffd", refs[0]["raw_text"])

    def test_explicit_encoding_and_decode_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "refs.txt"
            path.write_bytes(("[3] " + SHORT).encode("utf-16"))
            self.assertEqual(z.extract_source_references(path, encoding="utf-16")[0]["title"], SHORT)
            path.write_bytes(b"\xff")
            with self.assertRaises(ValueError):
                z.extract_source_references(path)

    def test_sparse_xlsx_keeps_empty_columns_and_chinese_headers(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "refs.xlsx"
            headers = ["\u9898\u540d", "DOI", "\u4f5c\u8005", "\u5e74\u4efd"]
            cells = ''.join(f'<c r="{chr(65+i)}1" t="inlineStr"><is><t>{escape(v)}</t></is></c>' for i, v in enumerate(headers))
            data = ''.join(f'<c r="{col}2" t="inlineStr"><is><t>{escape(v)}</t></is></c>' for col, v in [("A", SHORT), ("C", AUTHOR), ("D", "2024")])
            xml = '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row>' + cells + '</row><row>' + data + '</row></sheetData></worksheet>'
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("xl/worksheets/sheet1.xml", xml)
            ref = z.extract_source_references(path)[0]
            self.assertIsNone(ref["doi"])
            self.assertEqual((ref["title"], ref["first_author"], ref["year"]), (SHORT, AUTHOR, "2024"))

    def test_chinese_docx_preserves_label_raw_text_and_parsed_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "refs.docx"
            raw = "[3] " + AUTHOR + ". " + SHORT + "[J]. " + JOURNAL + ",2024."
            xml = '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>' + escape(raw) + '</w:t></w:r></w:p></w:body></w:document>'
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("word/document.xml", xml)
            ref = z.extract_source_references(path)[0]
            self.assertEqual((ref["raw_text"], ref["label"], ref["title"]), (raw, "[3]", SHORT))

    def test_catalog_supplements_ordinary_csv_without_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, catalog = root / "refs.csv", root / "cnki.json"
            source.write_text("\u9898\u540d,\u4f5c\u8005,\u5e74\u4efd\n" + SHORT + "," + AUTHOR + ",2024\n", encoding="utf-8")
            catalog.write_text(json.dumps({"records": [self.supplied(doi=None)]}), encoding="utf-8")
            backend = ChineseBackend()
            args = z.build_parser().parse_args(["import-references", "--source", str(source), "--metadata-catalog", str(catalog), "--collection", "TARGET", "--yes"])
            with patch.object(ing, "LocalBackend", return_value=backend), patch.object(ing, "Plugin"), patch("sys.stdout", new_callable=io.StringIO) as output:
                args.func(args)
            self.assertTrue(json.loads(output.getvalue())["complete"])
            self.assertEqual(backend.imports, 1)

    def test_catalog_ris_lookup_and_duplicate_ambiguity(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.ris"
            path.write_text(self.ris(doi=None), encoding="utf-8")
            resolver = rm.MetadataCatalog(path, Mock(side_effect=AssertionError("No fallback expected")))
            ref = {"title": SHORT, "first_author": AUTHOR}
            self.assertEqual(resolver(ref)["title"], SHORT)
            path.write_text(self.ris(doi=None) * 2, encoding="utf-8")
            with self.assertRaises(ing.Uncertain):
                rm.MetadataCatalog(path, Mock())(ref)

    def test_catalog_miss_falls_back_to_original_lookup(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "empty.json"
            path.write_text('{"records": []}', encoding="utf-8")
            fallback = Mock(return_value=deepcopy(ENTRY))
            ref = {"doi": ENTRY["doi"]}
            self.assertEqual(rm.MetadataCatalog(path, fallback)(ref), ENTRY)
            fallback.assert_called_once_with(ref)

    def test_multiple_source_occurrences_keep_locations_and_provenance(self):
        docs = [{"document_id": name, "references": [{**self.supplied(doi=None), "reference_id": "3",
                "source_location": {"paragraph": n}}]} for n, name in enumerate(("a", "b"))]
        result, backend = self.run_import(ing.normalize_sources(docs))
        self.assertTrue(result["complete"])
        self.assertEqual(backend.imports, 1)
        self.assertEqual([r["source"]["source_location"] for r in result["mappings"]], [{"paragraph": 0}, {"paragraph": 1}])
        self.assertEqual([r["item"]["item_key"] for r in result["mappings"]], ["NEW1", "NEW1"])
        self.assertTrue(all(r["metadata_provenance"] == PROVENANCE for r in result["mappings"]))

    def test_explicit_html_citation_tags_preserve_chinese_metadata(self):
        values = {"title": SHORT, "author": AUTHOR, "publication_date": "2024-06-01",
                  "journal_title": JOURNAL, "abstract": ABSTRACT, "doi": ENTRY["doi"]}
        text = ''.join(f'<meta name="citation_{key}" content="{escape(value)}">' for key, value in values.items())
        record = rm.metadata_from_html(text, PROVENANCE["url"], kind="database")
        result, _ = self.run_import(sources(record))
        self.assertTrue(result["complete"])
        self.assertEqual(record["metadata"]["abstract"], ABSTRACT)
        self.assertEqual(record["metadata_provenance"], PROVENANCE)

    def test_html_homepage_without_citation_tags_is_not_an_article_record(self):
        with self.assertRaises(ing.Uncertain):
            rm.metadata_from_html('<title>Portal</title><meta name="description" content="Search papers">', PROVENANCE["url"])

    def test_html_missing_abstract_does_not_take_generic_description(self):
        text = '<meta name="citation_title" content="' + SHORT + '"><meta name="citation_author" content="' + AUTHOR + '"><meta name="description" content="Portal advertisement">'
        record = rm.metadata_from_html(text, PROVENANCE["url"])
        self.assertEqual(record["metadata"]["abstract"], "")

    def test_html_extraction_command_needs_no_zotero_or_plugin(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "article.html"
            path.write_text('<meta name="citation_title" content="' + SHORT + '"><meta name="citation_author" content="' + AUTHOR + '">', encoding="utf-8")
            args = z.build_parser().parse_args(["metadata-from-html", "--file", str(path), "--url", PROVENANCE["url"]])
            with patch.object(ing, "LocalBackend", side_effect=AssertionError("No Zotero required")), patch("sys.stdout", new_callable=io.StringIO) as output:
                args.func(args)
            self.assertEqual(json.loads(output.getvalue())["records"][0]["metadata"]["title"], SHORT)


if __name__ == "__main__":
    unittest.main()
