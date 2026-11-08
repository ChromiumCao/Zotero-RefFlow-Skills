"""Child-note semantics tested in isolated Python and native-script JS simulations."""
from copy import deepcopy
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import citation_handoff as ch
import ingest as ing
import provenance_notes as pn
import zotero as z
from test_citations import payload, SENTENCE
from test_ingest import MemoryBackend


class MetadataMemoryBackend(MemoryBackend):
    def import_entry(self, entry, collection):
        super().import_entry(entry, collection)
        self.rows[-1]["data"].update(creators=deepcopy(entry["creators"]), date=str(entry.get("year") or ""))


def bundle_and_mapping(data=None):
    bundle = ch.prepare(data or payload())
    backend = MetadataMemoryBackend()
    mapping = ch.import_bundle(bundle, backend, "TARGET", approved=True, checks=1, delay=0)
    return bundle, mapping, backend.rows[0]


def job_for(data=None):
    bundle, mapping, parent = bundle_and_mapping(data)
    return pn.prepare_notes(bundle, mapping, approved=True, cost_warning_acknowledged=True), parent


class NotesMemory:
    def __init__(self, parent, mode="ok", rows=()):
        self.paper = deepcopy(parent)
        self.mode, self.rows, self.creates, self.reads = mode, deepcopy(list(rows)), 0, 0

    def parent(self, note):
        return deepcopy(self.paper)

    def children(self, note):
        self.reads += 1
        if self.mode == "unreadable" and self.creates:
            raise ing.Uncertain("Read connection lost")
        return deepcopy(self.rows)

    def can_create(self):
        if self.mode == "denied":
            raise ing.PermissionDenied("Permission denied")
        return self.mode != "unavailable"

    def create(self, note):
        self.creates += 1
        if self.mode in {"false_success", "failed"}:
            return {"success": True}
        self.rows.append({"key": "NOTE" + str(self.creates), "library": deepcopy(note["item"]["library"]),
                          "data": {"itemType": "note", "parentItem": note["item"]["item_key"], "note": note["html"]}})
        if self.mode == "wrong_parent":
            self.rows[-1]["data"]["parentItem"] = "OTHER"
        if self.mode == "changed_collection":
            self.paper["data"]["collections"] = []
        if self.mode == "lost_reply":
            raise TimeoutError("Lost response after commit")


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.guard = patch.object(z.urllib.request, "urlopen", side_effect=AssertionError("Real network blocked"))
        self.guard.start()
        self.addCleanup(self.guard.stop)

    def test_notes_require_separate_consent_and_cost_warning(self):
        bundle, mapping, _ = bundle_and_mapping()
        for approved, warned in ((False, False), (True, False), (False, True)):
            with self.assertRaises(PermissionError):
                pn.prepare_notes(bundle, mapping, approved=approved, cost_warning_acknowledged=warned)

    def test_note_has_original_document_sentence_quote_and_abstract_location(self):
        job, _ = job_for()
        note = job["notes"][0]
        self.assertIn(SENTENCE, note["html"])
        self.assertIn("Synthetic manuscript", note["html"])
        self.assertIn("improved the measured outcome", note["html"])
        self.assertIn("Section: Abstract", note["html"])
        self.assertIn("PDF file page index: Not available", note["html"])
        self.assertEqual(pn.note_text(note["html"]), note["expected_text"])

    def test_fulltext_section_pdf_index_and_printed_page_are_distinct(self):
        data = payload()
        unit = data["references"][0]["source_units"][0]
        unit.update(kind="full_text", section="Results", pdf_page_index=3, printed_page_label="S12", locator_verified=True)
        job, _ = job_for(data)
        note = job["notes"][0]
        self.assertIn("Section: Results", note["html"])
        self.assertIn("PDF file page index: 3", note["html"])
        self.assertIn("Printed page label: S12", note["html"])

    def test_wrong_bundle_mapping_and_unverified_items_do_not_attach(self):
        bundle, mapping, _ = bundle_and_mapping()
        mapping["citation_bundle_id"] = "another"
        with self.assertRaises(ValueError):
            pn.prepare_notes(bundle, mapping, approved=True, cost_warning_acknowledged=True)
        mapping["citation_bundle_id"] = bundle["bundle_id"]
        mapping["mappings"][0]["membership_verified"] = False
        job = pn.prepare_notes(bundle, mapping, approved=True, cost_warning_acknowledged=True)
        self.assertFalse(job["complete"])
        self.assertFalse(job["notes"])
        self.assertTrue(job["unresolved"])

    def test_fallback_notes_use_final_adopted_item_not_old_duplicate(self):
        bundle, mapping, parent = bundle_and_mapping()
        mapping["mappings"][0]["outcome"] = "fallback_import"
        mapping["mappings"][0]["known_duplicates"] = [{"item_key": "OLD", "library": mapping["library"]}]
        job = pn.prepare_notes(bundle, mapping, approved=True, cost_warning_acknowledged=True)
        self.assertEqual(job["notes"][0]["item"]["item_key"], parent["key"])
        self.assertNotEqual(job["notes"][0]["item"]["item_key"], "OLD")

    def test_same_document_multiple_citations_are_one_note(self):
        data = payload()
        doc = data["documents"][0]
        doc["text"] = SENTENCE + " " + SENTENCE
        first = doc["citations"][0]
        first.update(start=0, end=len(SENTENCE))
        other = deepcopy(first)
        other.update(citation_id="C2", start=len(SENTENCE)+1, end=len(doc["text"]))
        doc["citations"].append(other)
        job, _ = job_for(data)
        self.assertEqual(len(job["notes"]), 1)
        self.assertEqual(len(job["notes"][0]["usages"]), 2)

    def test_different_documents_share_item_but_keep_separate_notes(self):
        data = payload()
        other = deepcopy(data["documents"][0])
        other.update(document_id="D2", title="Another document")
        data["documents"].append(other)
        job, _ = job_for(data)
        self.assertEqual(len(job["notes"]), 2)
        self.assertEqual(len({n["note_id"] for n in job["notes"]}), 2)
        self.assertEqual(len({n["item"]["item_key"] for n in job["notes"]}), 1)

    def test_job_tampering_and_unapproved_jobs_block_writes(self):
        job, _ = job_for()
        job["notes"][0]["item"]["item_key"] = "ANOTHER"
        with self.assertRaises(ValueError):
            pn.write_notes(job, Mock())
        job, _ = job_for()
        job["consent"]["notes"] = False
        job["integrity"] = ch.digest({k:v for k,v in job.items() if k != "integrity"})
        with self.assertRaises(PermissionError):
            pn.write_notes(job, Mock())

    def test_created_notes_are_verified_and_reruns_do_not_duplicate(self):
        job, parent = job_for()
        backend = NotesMemory(parent)
        result = pn.write_notes(job, backend, checks=1, delay=0)
        self.assertTrue(result["complete"])
        self.assertEqual(result["counts"], {"created": 1})
        self.assertTrue(pn.write_notes(job, backend, checks=1, delay=0)["complete"])
        self.assertEqual(backend.creates, 1)

    def test_lost_response_is_recovered_by_actual_child_content(self):
        job, parent = job_for()
        backend = NotesMemory(parent, "lost_reply")
        result = pn.write_notes(job, backend, checks=1, delay=0)
        self.assertTrue(result["complete"])
        self.assertIn("Response abnormal", result["notes"][0]["warning"])
        self.assertEqual(backend.creates, 1)

    def test_unavailable_denied_false_success_and_wrong_parent_do_not_claim_success(self):
        job, parent = job_for()
        for mode in ("unavailable", "denied", "false_success", "wrong_parent", "unreadable", "changed_collection"):
            with self.subTest(mode=mode):
                backend = NotesMemory(parent, mode)
                result = pn.write_notes(job, backend, checks=1, delay=0)
                self.assertFalse(result["complete"])
                self.assertFalse(result["notes"][0]["membership_verified"])
                self.assertLessEqual(backend.creates, 1)
                if mode == "denied":
                    self.assertEqual(result["counts"], {"failed": 1})

    def test_uncertain_journal_prevents_copies_across_invocations(self):
        job, parent = job_for()
        backend = NotesMemory(parent, "false_success")
        with tempfile.TemporaryDirectory() as root:
            journal = Path(root) / "journal.json"
            for _ in range(2):
                self.assertFalse(pn.write_notes(job, backend, checks=1, delay=0, journal_path=journal)["complete"])
            self.assertEqual(backend.creates, 1)
            self.assertEqual(json.loads(journal.read_text())[job["notes"][0]["note_id"]], "uncertain")

    def test_existing_marker_with_modified_content_is_not_overwritten(self):
        job, parent = job_for()
        backend = NotesMemory(parent)
        backend.create(job["notes"][0])
        backend.rows[0]["data"]["note"] += "<p>Modified by user</p>"
        result = pn.write_notes(job, backend, checks=1, delay=0)
        self.assertFalse(result["complete"])
        self.assertEqual(backend.creates, 1)

    def test_duplicate_note_markers_are_pending_without_another_write(self):
        job, parent = job_for()
        backend = NotesMemory(parent)
        backend.create(job["notes"][0])
        backend.rows.append({**deepcopy(backend.rows[0]), "key": "ANOTHER"})
        self.assertFalse(pn.write_notes(job, backend, checks=1, delay=0)["complete"])
        self.assertEqual(backend.creates, 1)

    def test_read_only_verify_does_not_call_writer(self):
        job, parent = job_for()
        backend = NotesMemory(parent)
        backend.create(job["notes"][0])
        backend.create = Mock(side_effect=AssertionError("No writes"))
        self.assertTrue(pn.verify_notes(job, backend)["complete"])
        backend.create.assert_not_called()

    def test_other_library_same_note_key_cannot_satisfy_verification(self):
        job, parent = job_for()
        backend = NotesMemory(parent)
        backend.create(job["notes"][0])
        backend.rows[0]["library"] = {"type": "group", "id": 55}
        self.assertFalse(pn.verify_notes(job, backend)["complete"])

    def test_html_and_code_payloads_are_escaped_not_executed(self):
        data = payload()
        data["documents"][0]["title"] = '<script>alert("bad")</script> & literal &lt;'
        job, parent = job_for(data)
        self.assertNotIn("<script>", job["notes"][0]["html"])
        self.assertIn("&lt;script&gt;", job["notes"][0]["html"])
        self.assertEqual(pn.note_text(job["notes"][0]["html"]), job["notes"][0]["expected_text"])
        self.assertTrue(pn.write_notes(job, NotesMemory(parent), checks=1, delay=0)["complete"])

    def test_plugin_schema_and_call_use_explicit_library_parent_and_html(self):
        job, _ = job_for()
        plugin = Mock()
        plugin.tools = {"write_note": {"inputSchema": {"properties": {"action": {"enum": ["create"]}}}}}
        plugin.supports.return_value = True
        local = Mock(plugin=plugin)
        local.internal_library_id.return_value = 7
        with patch.object(ing, "LocalBackend", return_value=local), patch.object(ing, "Plugin", return_value=plugin):
            backend = pn.NoteBackend(job)
            backend.create(job["notes"][0])
        args = plugin.call.call_args.args
        self.assertEqual(args[0], "write_note")
        self.assertEqual(args[1]["libraryID"], 7)
        self.assertEqual(args[1]["parentKey"], job["notes"][0]["item"]["item_key"])
        self.assertEqual(args[1]["action"], "create")
        self.assertEqual(args[1]["content"], job["notes"][0]["html"])

    def test_without_plugin_job_and_native_script_are_self_contained(self):
        job, _ = job_for()
        with tempfile.TemporaryDirectory() as root:
            paths = pn.write_job(job, Path(root))
            script = paths[1].read_text(encoding="utf-8")
            self.assertNotIn("__JOB_JSON__", script)
            self.assertIn("new Zotero.Item", script)
            self.assertNotIn("Zotero.DB.", script)
            with self.assertRaises(FileExistsError):
                pn.write_job(job, Path(root))

    def test_pdf_pages_are_bounded_and_require_consent(self):
        with patch.object(pn.subprocess, "run", side_effect=AssertionError("No extraction")):
            with self.assertRaises(PermissionError):
                pn.extract_pdf_pages(Path("missing.pdf"), [1])
            for pages in ([], [0], list(range(1, 7)), [True]):
                with self.assertRaises(ValueError):
                    pn.extract_pdf_pages(Path("missing.pdf"), pages, approved=True, cost_warning_acknowledged=True)

    def test_pdf_selected_page_preserves_actual_file_index_not_printed_page(self):
        with tempfile.TemporaryDirectory() as root:
            pdf = Path(root) / "source.pdf"
            pdf.write_bytes(b"%PDF-1.7\nsynthetic test")
            result = Mock(stdout="Text from the selected physical page", stderr="", returncode=0)
            with patch.object(pn.shutil, "which", return_value="pdftotext"), patch.object(pn.subprocess, "run", return_value=result) as runner:
                units = pn.extract_pdf_pages(pdf, [3, 3], approved=True, cost_warning_acknowledged=True)
            self.assertEqual(len(units), 1)
            self.assertEqual(units[0]["pdf_page_index"], 3)
            self.assertIsNone(units[0]["printed_page_label"])
            command = runner.call_args.args[0]
            self.assertEqual(command[command.index("-f"):command.index("-f") + 4], ["-f", "3", "-l", "3"])

    def test_pdf_missing_tool_has_actionable_non_plugin_alternative(self):
        import pdf_reader
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "source.pdf"
            path.write_bytes(b"%PDF-1.7\nsynthetic test")
            with patch.object(pdf_reader.shutil, "which", return_value=None), \
                 patch.object(pdf_reader.importlib, "import_module", side_effect=ImportError("No optional reader")):
                with self.assertRaisesRegex(RuntimeError, "verified text export"):
                    pn.extract_pdf_pages(path, [1], approved=True, cost_warning_acknowledged=True)


@unittest.skipUnless(shutil.which("node"), "Node needed only for native-script simulation tests")
class NativeScriptTests(unittest.TestCase):
    def run_native(self, mode="ok", data=None, library=None):
        job, _ = job_for(data)
        if library:
            for note in job["notes"]:
                note["item"]["library"] = deepcopy(library)
                note["target_collection"]["library"] = deepcopy(library)
            job["integrity"] = ch.digest({k: v for k, v in job.items() if k != "integrity"})
        program = Path(__file__).with_name("test_native_notes.cjs")
        result = subprocess.run([shutil.which("node"), str(program)], input=json.dumps({"script": pn.native_script(job), "job": job, "mode": mode}),
                                text=True, encoding="utf-8", capture_output=True, timeout=15, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_native_without_plugin_creates_one_child_and_rerun_reuses_it(self):
        result = self.run_native()
        self.assertTrue(result["first"]["complete"])
        self.assertTrue(result["second"]["complete"])
        self.assertEqual(result["creates"], 1)

    def test_native_lost_response_does_not_duplicate(self):
        result = self.run_native("lost_reply")
        self.assertTrue(result["first"]["complete"])
        self.assertEqual(result["creates"], 1)

    def test_native_false_success_and_unreadable_writes_do_not_repeat(self):
        for mode in ("false_success", "unreadable"):
            with self.subTest(mode=mode):
                result = self.run_native(mode)
                self.assertFalse(result["first"]["complete"])
                self.assertFalse(result["second"]["complete"])
                self.assertEqual(result["creates"], 1)

    def test_native_wrong_identity_and_collection_block_all_writes(self):
        for mode in ("wrong_doi", "wrong_pmid", "wrong_author", "wrong_year", "wrong_title", "wrong_library", "wrong_collection"):
            with self.subTest(mode=mode):
                result = self.run_native(mode)
                self.assertTrue(result["error"])
                self.assertEqual(result["creates"], 0)

    def test_native_public_group_and_personal_ids_resolve_without_plugin(self):
        for library in (ing.Library("group", 55).json(), ing.Library("user", 1234).json()):
            with self.subTest(library=library):
                result = self.run_native(library=library)
                self.assertTrue(result["first"]["complete"])
                self.assertEqual(result["creates"], 1)
        result = self.run_native("wrong_group", library=ing.Library("group", 55).json())
        self.assertTrue(result["error"])
        self.assertEqual(result["creates"], 0)

    def test_native_chinese_full_and_split_author_names_agree(self):
        data = payload()
        data["references"][0]["metadata"]["creators"] = [{"lastName": "\u5f20\u4f1f", "firstName": ""}]
        result = self.run_native("split_cjk_author", data=data)
        self.assertTrue(result["first"]["complete"])
        self.assertEqual(result["creates"], 1)

    def test_native_readonly_wrong_parent_and_collection_change_never_claim_success(self):
        for mode in ("readonly", "wrong_parent", "changed_collection"):
            with self.subTest(mode=mode):
                result = self.run_native(mode)
                self.assertFalse(result["first"]["complete"])
                self.assertLessEqual(result["creates"], 1)

    def test_native_safe_html_round_trip(self):
        data = payload()
        data["documents"][0]["title"] = '<script>unsafe()</script> & literal &lt;'
        result = self.run_native(data=data)
        self.assertTrue(result["first"]["complete"])
        self.assertEqual(result["creates"], 1)


if __name__ == "__main__":
    unittest.main()
