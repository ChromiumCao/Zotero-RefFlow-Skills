"""Offline Markdown provenance and ordinary note-plan compatibility checks."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import citation_handoff as ch
import provenance_markdown as pm
import provenance_notes as pn
import local_state
import zotero as z
from test_citations import payload, SENTENCE, ABSTRACT
from test_provenance_notes import bundle_and_mapping


class MarkdownTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        guard = patch.object(z.urllib.request, "urlopen", side_effect=AssertionError("No network in local export"))
        guard.start()
        self.addCleanup(guard.stop)

    def test_standalone_keeps_verbatim_quote_context_and_missing_identity(self):
        bundle = ch.prepare(payload())
        before = deepcopy(bundle)
        text = pm.from_bundle(bundle)
        self.assertIn(pm.VERSION, text)
        self.assertIn(SENTENCE, text)
        self.assertIn(ABSTRACT, text)
        self.assertIn("improved the measured outcome", text)
        self.assertIn("10.1000/cited", text)
        self.assertIn("Abstract", text)
        self.assertIn("not_linked", text)
        self.assertIn("PDF 物理页序（从 1 起）：未取得", text)
        self.assertIn("最终采用的 item key：未取得", text)
        self.assertEqual(bundle, before)
        self.assertEqual(bundle["consent"], {"import": False, "notes": False})

    def test_document_path_and_fulltext_locators_are_distinct(self):
        data = payload()
        data["documents"][0]["source_path"] = "C:/research/original manuscript.docx"
        data["references"][0]["raw_text"] = "Original numbered bibliography [3]"
        unit = data["references"][0]["source_units"][0]
        unit.update(kind="full_text", section="Results", pdf_page_index=3, printed_page_label="S12", locator_verified=True)
        unit["text"] += "\nAdditional original paragraph."
        bundle, mapping, _ = bundle_and_mapping(data)
        text = pm.from_bundle(bundle, mapping)
        for content in ("Results", "S12", "` 3 `", "original manuscript.docx", "Original numbered bibliography [3]",
                        "Additional original paragraph.", bundle["documents"][0]["text_hash"]):
            self.assertIn(content, text)

    def test_all_occurrences_survive_same_item_and_same_citation_ids(self):
        data = payload()
        document = data["documents"][0]
        document["text"] = SENTENCE + " " + SENTENCE
        first = document["citations"][0]
        first.update(start=0, end=len(SENTENCE))
        second = deepcopy(first)
        second.update(citation_id="C2", start=len(SENTENCE) + 1, end=len(document["text"]))
        document["citations"].append(second)
        other = deepcopy(document)
        other.update(document_id="D2", title="Another document", text=SENTENCE, citations=[deepcopy(first)])
        data["documents"].append(other)
        bundle, mapping, _ = bundle_and_mapping(data)
        text = pm.from_bundle(bundle, mapping)
        self.assertEqual(text.count("## 引用对应关系 "), 3)
        self.assertIn("` D1 `", text)
        self.assertIn("` D2 `", text)
        self.assertEqual(text.count("引用位置 ID：` C1 `"), 2)
        self.assertEqual(text.count("最终采用的 item key：` NEW1 `"), 3)

    def test_fallback_uses_adopted_identity_and_retains_reason(self):
        bundle, mapping, _ = bundle_and_mapping()
        row = mapping["mappings"][0]
        row.update(outcome="fallback_import", fallback_reason="Reuse unavailable",
                   known_duplicates=[{"library": row["item"]["library"], "item_key": "OLD"}])
        text = pm.from_bundle(bundle, mapping)
        self.assertIn("最终采用的 item key：` NEW1 `", text)
        self.assertIn("Reuse unavailable", text)
        self.assertIn("OLD", text)
        row["membership_verified"] = False
        text = pm.from_bundle(bundle, mapping)
        self.assertIn("最终采用的 item key：未取得", text)
        self.assertIn("Zotero membership not verified", text)

    def test_wrong_bundle_duplicate_source_and_library_conflicts_rejected(self):
        bundle, mapping, _ = bundle_and_mapping()
        wrong = deepcopy(mapping)
        wrong["citation_bundle_id"] = "another"
        with self.assertRaises(ValueError):
            pm.from_bundle(bundle, wrong)
        wrong = deepcopy(mapping)
        wrong["mappings"].append(deepcopy(wrong["mappings"][0]))
        with self.assertRaises(ValueError):
            pm.from_bundle(bundle, wrong)
        wrong = deepcopy(mapping)
        wrong["mappings"][0]["item"]["library"]["type"] = "group"
        with self.assertRaises(ValueError):
            pm.from_bundle(bundle, wrong)

    def test_cli_is_offline_needs_no_notes_consent_and_never_overwrites(self):
        bundle = self.root / "bundle.json"
        bundle.write_text(json.dumps(ch.prepare(payload())), encoding="utf-8")
        destination = self.root / "provenance.md"
        args = ["provenance-export", "--bundle", str(bundle), "--out", str(destination)]
        with patch.object(pn, "NoteBackend", side_effect=AssertionError("Writer discovery forbidden")):
            self.assertEqual(ch.main(args), 0)
        self.assertIn(ABSTRACT, destination.read_text(encoding="utf-8"))
        original = destination.read_bytes()
        with self.assertRaises(FileExistsError):
            ch.main(args)
        self.assertEqual(destination.read_bytes(), original)
        with self.assertRaises(ValueError):
            ch.main(["provenance-export", "--bundle", str(bundle), "--out", str(bundle)])

    def test_note_plan_emits_markdown_before_any_writer(self):
        bundle, mapping, _ = bundle_and_mapping()
        job = pn.prepare_notes(bundle, mapping, approved=True, cost_warning_acknowledged=True)
        paths = pn.write_job(job, self.root / "notes", bundle=bundle, mapping=mapping)
        self.assertEqual(len(paths), 4)
        self.assertEqual(paths[-1].name, "citation-provenance.md")
        self.assertIn(ABSTRACT, paths[-1].read_text(encoding="utf-8"))
        old_job = deepcopy(job)
        plain_paths = pn.write_job(job, self.root / "job-only")
        self.assertIn("已取得的原文上下文\n\n未取得", plain_paths[-1].read_text(encoding="utf-8"))
        self.assertEqual(job, old_job)

    def test_preexisting_markdown_blocks_whole_note_plan_publication(self):
        bundle, mapping, _ = bundle_and_mapping()
        job = pn.prepare_notes(bundle, mapping, approved=True, cost_warning_acknowledged=True)
        destination = self.root / "citation-provenance.md"
        destination.write_text("Existing snapshot", encoding="utf-8")
        with self.assertRaises(FileExistsError):
            pn.write_job(job, self.root, bundle=bundle, mapping=mapping)
        self.assertEqual(list(self.root.iterdir()), [destination])

    def test_markdown_and_note_job_cannot_adopt_different_items(self):
        bundle, mapping, _ = bundle_and_mapping()
        job = pn.prepare_notes(bundle, mapping, approved=True, cost_warning_acknowledged=True)
        other = deepcopy(mapping)
        other["mappings"][0]["item"]["item_key"] = "DIFFERENT"
        with self.assertRaises(ValueError):
            pn.write_job(job, self.root / "mismatch", bundle=bundle, mapping=other)
        self.assertFalse((self.root / "mismatch").exists())
        with self.assertRaises(ValueError):
            pn.write_job(job, self.root / "missing-map", bundle=bundle)
        with self.assertRaises(ValueError):
            pn.write_job(job, self.root / "missing-bundle", mapping=mapping)

    def test_source_markdown_code_fences_remain_literal(self):
        value = "```\n<script>unsafe()</script>\n~~~~\n````"
        rendered = pm.literal(value)
        self.assertTrue(rendered.startswith("`````text\n"))
        self.assertIn(value, rendered)
        self.assertEqual(pm.inline("name`with``ticks"), "``` name`with``ticks ```")

    def test_atomic_text_publication_never_replaces_existing_snapshot(self):
        target = self.root / "snapshot.md"
        local_state.atomic_text(target, "Original snapshot", replace=False)
        with self.assertRaises(FileExistsError):
            local_state.atomic_text(target, "Replacement", replace=False)
        self.assertEqual(target.read_text(encoding="utf-8"), "Original snapshot")


if __name__ == "__main__":
    unittest.main()
