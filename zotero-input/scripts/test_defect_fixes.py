"""Observable regressions for defect fixes and opt-in recovery/removal paths."""
from copy import deepcopy
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import citation_handoff as ch
import collection_sync as cs
import ingest as ing
import local_state as state
import provenance_notes as pn
import zotero as z
from test_ingest import ENTRY, MemoryBackend, item, sources
from test_citations import payload
from test_provenance_notes import MetadataMemoryBackend, NotesMemory, bundle_and_mapping


class Fixes(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.journal = self.root / "journal.json"
        self.addCleanup(patch.stopall)
        patch.object(state, "default_import_journal", return_value=self.journal).start()
        patch.object(z.urllib.request, "urlopen", side_effect=AssertionError("Unexpected network")).start()

    def test_disabled_policy_forms_fail_closed(self):
        errors = ["Write operations are currently disabled", "Write access is disabled", "Write operation not enabled",
                  {"code": "WRITE_DISABLED", "message": "Please enable this feature"}, {"write_enabled": False}]
        for error in errors:
            with self.subTest(error=error), self.assertRaises(ing.PermissionDenied):
                ing.parse_rpc_body(json.dumps({"id": 1, "error": error}), 1)
        with self.assertRaises(RuntimeError) as raised:
            ing.parse_rpc_body(json.dumps({"id": 1, "error": {"code": -32603, "message": "Internal calculation error"}}), 1)
        self.assertNotIsInstance(raised.exception, ing.PermissionDenied)

    def test_policy_refusal_latches_plugin(self):
        plugin = object.__new__(ing.Plugin)
        plugin.denied, plugin.reason = False, None
        plugin.rpc = lambda *a: {"isError": True, "content": [{"type": "text", "text": "Write operations are currently disabled"}]}
        with self.assertRaises(ing.PermissionDenied):
            plugin.call("add_items_to_collection", {})
        plugin.rpc = lambda *a: self.fail("A denied writer was called again")
        with self.assertRaises(ing.PermissionDenied):
            plugin.call("write_note", {})

    def test_batch_denial_blocks_later_writes(self):
        backend = MemoryBackend([item("OLD", ["OTHER"])])
        backend.add = lambda *a: (_ for _ in ()).throw(ing.PermissionDenied("Write operations are currently disabled"))
        result = ing.reconcile(sources({"doi": ENTRY["doi"]}, {"doi": "10.1000/second"}), backend, "TARGET", checks=1, delay=0)
        self.assertFalse(result["complete"])
        self.assertEqual(backend.imports, 0)
        self.assertEqual(result["counts"]["failed"], 2)

    def test_csv_single_column_crlf_and_embedded_newline(self):
        single = self.root / "single.csv"
        single.write_bytes(b"DOI\r\n10.1000/one\r\n")
        self.assertEqual(z.extract_source_references(single)[0]["doi"], "10.1000/one")
        multi = self.root / "multi.csv"
        multi.write_bytes(b'title,DOI\r\n"First line\r\nSecond line",10.1000/two\r\n')
        ref = z.extract_source_references(multi)[0]
        self.assertEqual(ref["title"], "First line\r\nSecond line")
        self.assertEqual(ref["source_location"]["end_line"], 3)

    def test_xlsx_workbook_order_and_hidden_locations(self):
        workbook = self.root / "ordered.xlsx"
        namespace = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
        relation = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
        def sheet(doi):
            return ('<worksheet xmlns="' + namespace + '"><sheetData>'
                    '<row r="2"><c r="A2" t="inlineStr"><is><t>DOI</t></is></c></row>'
                    '<row r="9"><c r="A9" t="inlineStr"><is><t>' + doi + '</t></is></c></row>'
                    '</sheetData></worksheet>')
        with zipfile.ZipFile(workbook, "w") as archive:
            archive.writestr("xl/workbook.xml", '<workbook xmlns="' + namespace + '" xmlns:r="' + relation + '"><sheets>'
                            '<sheet name="Second physical sheet" sheetId="2" state="hidden" r:id="r2"/>'
                            '<sheet name="First physical sheet" sheetId="1" r:id="r1"/></sheets></workbook>')
            archive.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                            '<Relationship Id="r1" Target="worksheets/sheet1.xml"/>'
                            '<Relationship Id="r2" Target="/xl/worksheets/sheet2.xml"/></Relationships>')
            archive.writestr("xl/worksheets/sheet1.xml", sheet("10.1000/first"))
            archive.writestr("xl/worksheets/sheet2.xml", sheet("10.1000/second"))
        refs = z.extract_source_references(workbook)
        self.assertEqual([ref["doi"] for ref in refs], ["10.1000/second", "10.1000/first"])
        self.assertEqual(refs[0]["source_location"]["sheet"], "Second physical sheet")
        self.assertEqual(refs[0]["source_location"]["row"], 9)
        self.assertEqual(refs[0]["source_location"]["visibility"], "hidden")
        self.assertEqual(refs[1]["source_location"]["visibility"], "visible")

    def test_xlsx_uncached_formula_row_retained_pending(self):
        workbook = self.root / "formula.xlsx"
        with zipfile.ZipFile(workbook, "w") as archive:
            archive.writestr("xl/worksheets/sheet1.xml", '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                            '<sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>DOI</t></is></c></row>'
                            '<row r="5"><c r="A5"><f>"10.1000/from-formula"</f></c></row></sheetData></worksheet>')
        refs = z.extract_source_references(workbook)
        self.assertEqual(len(refs), 1)
        self.assertIn("Formula has no cached value at A5", refs[0]["extraction_errors"])
        self.assertEqual(refs[0]["source_location"]["row"], 5)
        backend = MemoryBackend()
        result = ing.reconcile(sources(refs[0]), backend, "TARGET", checks=1,
                               lookup=lambda ref: self.fail("Unresolved formula triggered metadata query"))
        self.assertFalse(result["complete"])
        self.assertEqual(backend.imports, 0)

    def test_ris_fields_cannot_create_control_tags(self):
        entry = deepcopy(ENTRY)
        for key in ("title", "journal", "year", "volume", "issue", "pages", "url"):
            entry[key] = "Metadata\nDO  - 10.1000/injected\r\nER  -"
        entry["creators"][0]["lastName"] = "Smith\nDO  - 10.1000/injected"
        for serializer in (z._doi_to_full_ris, z._pubmed_to_ris):
            text = serializer(entry)
            self.assertEqual(len(re.findall(r"^DO  -", text, re.M)), 1)
            self.assertEqual(len(re.findall(r"^ER  -", text, re.M)), 1)
            self.assertNotIn("\nDO  - 10.1000/injected", text)

    def test_legacy_cite_ambiguity_never_selects_first(self):
        with patch.object(z, "api_get", return_value=[item("ONE"), item("TWO")]), self.assertRaises(SystemExit):
            z.find_item(item_key=None, query_text="ambiguous")

    def test_unscoped_restart_does_not_touch_processes(self):
        with patch.object(z.subprocess, "run") as run, patch.object(z.subprocess, "Popen") as launch:
            self.assertFalse(z.restart_zotero(wait_for_api=False))
            run.assert_not_called()
            launch.assert_not_called()

    def test_restart_rejects_unverified_pid_profile(self):
        with patch.object(z, "_profile_process", return_value=None), patch.object(z.subprocess, "Popen") as launch:
            self.assertFalse(z.restart_zotero(False, profile=self.root, pid=123))
            launch.assert_not_called()

    def test_explicit_profile_preference_does_not_use_default(self):
        prefs = self.root / "prefs.js"
        prefs.write_text('user_pref("other", true);\n', encoding="utf-8")
        with patch.object(z, "profile_dir", side_effect=AssertionError("Default profile touched")):
            z.set_local_api_pref(True, self.root)
        self.assertIn(z.LOCAL_API_PREF, prefs.read_text())

    def test_bad_mapping_path_blocks_import(self):
        bundle = self.root / "bundle.json"
        bundle.write_text(json.dumps(ch.prepare(payload())), encoding="utf-8")
        backend = MetadataMemoryBackend()
        with patch.object(ing, "Plugin"), patch.object(ing, "LocalBackend", return_value=backend), self.assertRaises(FileNotFoundError):
            ch.main(["import", "--bundle", str(bundle), "--collection", "TARGET", "--approve-import",
                     "--mapping-out", str(self.root / "absent" / "mapping.json")])
        self.assertEqual(backend.imports, 0)

    def test_unverified_import_journal_blocks_new_process_attempt(self):
        backend = MemoryBackend()
        backend.import_entry = lambda *a: setattr(backend, "imports", backend.imports + 1)
        first = ing.reconcile(sources({"doi": ENTRY["doi"]}), backend, "TARGET", lookup=lambda r: ENTRY,
                              checks=1, delay=0, journal_path=self.journal)
        self.assertFalse(first["complete"])
        second = MemoryBackend()
        result = ing.reconcile(sources({"doi": ENTRY["doi"]}), second, "TARGET", lookup=lambda r: ENTRY,
                               checks=1, delay=0, journal_path=self.journal)
        self.assertFalse(result["complete"])
        self.assertEqual(second.imports, 0)
        self.assertEqual(backend.imports, 1)

    def test_lost_import_response_recovered_without_copy(self):
        backend = MemoryBackend()
        real_import = backend.import_entry
        def lost(entry, collection):
            real_import(entry, collection)
            raise TimeoutError("Lost reply")
        backend.import_entry = lost
        refs = sources({"doi": ENTRY["doi"]})
        first = ing.reconcile(refs, backend, "TARGET", lookup=lambda r: ENTRY, checks=1, delay=0, journal_path=self.journal)
        second = ing.reconcile(refs, backend, "TARGET", lookup=lambda r: self.fail("Unneeded lookup"), checks=1, delay=0, journal_path=self.journal)
        self.assertTrue(first["complete"] and second["complete"])
        self.assertEqual(backend.imports, 1)

    def test_truncated_journal_and_concurrent_lock_block_writes(self):
        self.journal.write_text('{"schema_version":', encoding="utf-8")
        backend = MemoryBackend()
        with self.assertRaises(ValueError):
            ing.reconcile(sources({"doi": ENTRY["doi"]}), backend, "TARGET", journal_path=self.journal)
        self.journal.unlink()
        with state.exclusive_lock(self.journal), self.assertRaises(RuntimeError):
            ing.reconcile(sources({"doi": ENTRY["doi"]}), backend, "TARGET", journal_path=self.journal)
        self.assertEqual(backend.imports, 0)

    def test_atomic_mapping_failure_retains_actual_item_for_recovery(self):
        bundle = self.root / "bundle.json"
        bundle.write_text(json.dumps(ch.prepare(payload())), encoding="utf-8")
        backend = MetadataMemoryBackend()
        output = self.root / "mapping.json"
        atomic = state.atomic_json
        def write(path, value, **kwargs):
            if Path(path) == output:
                raise OSError("Simulated disk output failure")
            return atomic(path, value, **kwargs)
        with patch.object(ing, "Plugin"), patch.object(ing, "LocalBackend", return_value=backend), patch.object(state, "atomic_json", side_effect=write):
            with self.assertRaises(OSError):
                ch.main(["import", "--bundle", str(bundle), "--collection", "TARGET", "--approve-import", "--mapping-out", str(output)])
        self.assertEqual(backend.imports, 1)
        journal = json.loads(self.journal.read_text())
        saved = next(r["mapping"] for r in journal["attempts"].values())
        self.assertEqual(saved["item"]["item_key"], "NEW1")
        with patch.object(ing, "Plugin"), patch.object(ing, "LocalBackend", return_value=backend), patch("sys.stdout", new=io.StringIO()):
            self.assertEqual(ch.main(["import", "--bundle", str(bundle), "--collection", "TARGET", "--approve-import", "--mapping-out", str(output)]), 0)
        self.assertEqual(backend.imports, 1)

    def test_map_refresh_preserves_fallback_history(self):
        bundle, mapping, parent = bundle_and_mapping()
        mapping["mappings"][0].pop("adoption_history", None)
        mapping["mappings"][0].update(outcome="fallback_import", fallback_reason="Unavailable writer", known_duplicates=[{"item_key": "OLD"}])
        result = ch.map_existing(bundle, mapping, MetadataMemoryBackend([parent]))
        row = result["mappings"][0]
        self.assertEqual(row["fallback_reason"], "Unavailable writer")
        self.assertEqual(row["adoption_history"][0]["outcome"], "fallback_import")
        refreshed = ch.map_existing(bundle, result, MetadataMemoryBackend([parent]))
        self.assertEqual(refreshed["mappings"][0]["adoption_history"], row["adoption_history"])

    def test_fallback_multiple_occurrences_share_history_without_losing_sources(self):
        backend = MemoryBackend([item("OLD", ["OTHER"])], add_mode="unavailable")
        refs = sources({"doi": ENTRY["doi"], "source_location": "first"}, {"doi": ENTRY["doi"], "source_location": "second"})
        result = ing.reconcile(refs, backend, "TARGET", lookup=lambda r: ENTRY, checks=1, delay=0, journal_path=self.journal)
        self.assertTrue(result["complete"])
        self.assertEqual(backend.imports, 1)
        self.assertEqual([r["source"]["source_location"] for r in result["mappings"]], ["first", "second"])
        self.assertEqual(result["mappings"][1]["adoption_history"][0]["outcome"], "fallback_import")
        repeated = ing.reconcile(refs, backend, "TARGET", checks=1, delay=0, journal_path=self.journal)
        self.assertTrue(repeated["complete"])
        self.assertEqual(repeated["mappings"][0]["fallback_reason"], result["mappings"][0]["fallback_reason"])

    def test_explicit_old_identity_rerun_adopts_own_verified_fallback(self):
        backend = MemoryBackend([item("OLD", ["OTHER"])], add_mode="unavailable")
        refs = sources({"doi": ENTRY["doi"], "zotero_item": ing.identity(backend.rows[0], backend.library)})
        first = ing.reconcile(refs, backend, "TARGET", lookup=lambda r: ENTRY, checks=1, delay=0, journal_path=self.journal)
        second = ing.reconcile(refs, backend, "TARGET", lookup=lambda r: self.fail("Unneeded metadata"),
                               checks=1, delay=0, journal_path=self.journal)
        self.assertTrue(first["complete"] and second["complete"])
        self.assertEqual(second["mappings"][0]["item"]["item_key"], "NEW1")
        self.assertEqual(backend.imports, 1)
        backend.rows[0]["data"]["DOI"] = "10.1000/changed"
        changed = ing.reconcile(refs, backend, "TARGET", checks=1, delay=0, journal_path=self.journal)
        self.assertFalse(changed["complete"])
        self.assertEqual(backend.imports, 1)

    def test_final_item_metadata_change_revokes_success(self):
        backend = MemoryBackend([item("EXISTING", ["TARGET"])])
        target = backend.target_items
        def changed(key):
            rows = target(key)
            if backend.reads >= 3:
                rows[0]["data"]["DOI"] = "10.1000/changed"
            return rows
        backend.target_items = changed
        result = ing.reconcile(sources({"doi": ENTRY["doi"]}), backend, "TARGET", checks=1, delay=0)
        self.assertFalse(result["complete"])
        self.assertFalse(result["mappings"][0]["membership_verified"])

    def test_note_regeneration_reuses_but_user_quote_edit_blocks(self):
        bundle, mapping, parent = bundle_and_mapping()
        first = pn.prepare_notes(bundle, mapping, approved=True, cost_warning_acknowledged=True)
        bundle["prepared_at"] = "2099-01-01T00:00:00+00:00"
        bundle["integrity"] = ch.bundle_integrity(bundle)
        second = pn.prepare_notes(bundle, mapping, approved=True, cost_warning_acknowledged=True)
        backend = NotesMemory(parent)
        self.assertTrue(pn.write_notes(first, backend, checks=1, delay=0)["complete"])
        self.assertTrue(pn.write_notes(second, backend, checks=1, delay=0)["complete"])
        self.assertEqual(backend.creates, 1)
        backend.rows[0]["data"]["note"] += "<p>User additional quotation</p>"
        self.assertFalse(pn.write_notes(second, backend, checks=1, delay=0)["complete"])
        self.assertEqual(backend.creates, 1)

    def test_scientific_pmid_is_pending_without_network(self):
        ref = z.reference_from_fields({"PMID": "1.23457E+07", "title": ENTRY["title"]})
        backend = MemoryBackend()
        result = ing.reconcile(sources(ref), backend, "TARGET", lookup=lambda r: self.fail("Invalid identifier queried"), checks=1)
        self.assertFalse(result["complete"])
        self.assertEqual(backend.imports, 0)


class RemovalMemory(MemoryBackend):
    def __init__(self, rows, mode="ok"):
        super().__init__(rows)
        self.mode, self.removes = mode, 0
    def can_remove(self):
        return self.mode != "unavailable"
    def remove(self, key, collection):
        self.removes += 1
        if self.mode == "denied":
            raise ing.PermissionDenied("Write operations are currently disabled")
        if self.mode != "false_success":
            next(row for row in self.rows if row["key"] == key)["data"]["collections"].remove(collection)
        if self.mode == "lost":
            raise TimeoutError("Lost response")


class Removals(unittest.TestCase):
    def backend(self, mode="ok"):
        return RemovalMemory([item("KEEP", ["TARGET"]), item("REMOVE", ["TARGET", "OTHER"], DOI="10.1000/removed", title="Another reference", extra="")], mode)
    def test_preview_never_writes_and_empty_source_blocks(self):
        backend = self.backend()
        plan = cs.plan_removals(sources({"doi": ENTRY["doi"]}), backend, "TARGET")
        self.assertEqual([r["item"]["item_key"] for r in plan["candidates"]], ["REMOVE"])
        self.assertEqual(backend.removes, 0)
        empty = cs.plan_removals([], backend, "TARGET")
        with self.assertRaises(ValueError):
            cs.apply_removals(empty, backend, approved=True, source_complete=True)
    def test_remove_preserves_item_and_other_memberships(self):
        backend = self.backend()
        plan = cs.plan_removals(sources({"doi": ENTRY["doi"]}), backend, "TARGET")
        result = cs.apply_removals(plan, backend, approved=True, source_complete=True, checks=1, delay=0)
        self.assertTrue(result["complete"])
        self.assertEqual(backend.get_item("REMOVE")["data"]["collections"], ["OTHER"])
        self.assertEqual(len(backend.rows), 2)
        self.assertTrue(cs.apply_removals(plan, backend, approved=True, source_complete=True, checks=1)["complete"])
        self.assertEqual(backend.removes, 1)
    def test_lost_reply_is_verified_and_false_success_stays_pending(self):
        for mode in ("lost", "false_success", "unavailable", "denied"):
            with self.subTest(mode=mode):
                backend = self.backend(mode)
                plan = cs.plan_removals(sources({"doi": ENTRY["doi"]}), backend, "TARGET")
                result = cs.apply_removals(plan, backend, approved=True, source_complete=True, checks=1, delay=0)
                self.assertEqual(result["complete"], mode == "lost")
                self.assertLessEqual(backend.removes, 1)
    def test_no_consent_changed_source_or_extraction_gap_blocks(self):
        backend = self.backend()
        refs = sources({"doi": ENTRY["doi"]})
        plan = cs.plan_removals(refs, backend, "TARGET")
        with self.assertRaises(PermissionError):
            cs.apply_removals(plan, backend)
        refs[0]["extraction_errors"] = ["Formula cache missing"]
        incomplete = cs.plan_removals(refs, backend, "TARGET")
        with self.assertRaises(ValueError):
            cs.apply_removals(incomplete, backend, approved=True, source_complete=True)
        with tempfile.TemporaryDirectory() as root:
            source = Path(root) / "source.txt"
            source.write_text("Original references", encoding="utf-8")
            plan = cs.plan_removals(sources({"doi": ENTRY["doi"]}), backend, "TARGET", sources=[source])
            source.write_text("Changed references", encoding="utf-8")
            with self.assertRaises(ValueError):
                cs.apply_removals(plan, backend, approved=True, source_complete=True)
        self.assertEqual(backend.removes, 0)
    def test_target_changes_block_before_any_write(self):
        backend = self.backend()
        plan = cs.plan_removals(sources({"doi": ENTRY["doi"]}), backend, "TARGET")
        backend.rows[1]["data"]["title"] = "Edited by user"
        with self.assertRaises(ing.Uncertain):
            cs.apply_removals(plan, backend, approved=True, source_complete=True)
        self.assertEqual(backend.removes, 0)


if __name__ == "__main__":
    unittest.main()
