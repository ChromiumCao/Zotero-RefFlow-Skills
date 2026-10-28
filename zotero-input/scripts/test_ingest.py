"""Isolated stateful tests. Never contact or mutate the user's Zotero library."""
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import ingest as ing
import zotero as z


ENTRY = {"doi": "10.1000/study", "pmid": "12345678", "title": "A controlled study",
         "creators": [{"firstName": "Anna", "lastName": "Smith"}], "year": "2024",
         "journal": "Test Journal", "abstract": "The measured results of this study."}


def item(key="OLD", members=("OTHER",), library=ing.Library(), **overrides):
    data = {"key": key, "itemType": "journalArticle", "DOI": ENTRY["doi"],
            "extra": "PMID: " + ENTRY["pmid"], "title": ENTRY["title"],
            "date": "2024", "creators": deepcopy(ENTRY["creators"]),
            "abstractNote": ENTRY["abstract"], "collections": list(members)}
    data.update(overrides)
    return {"key": key, "library": library.json(), "data": data}


def sources(*refs):
    return ing.normalize_sources([{"document_id": "doc-a", "references": list(refs) or [{"doi": ENTRY["doi"]}]}])


class MemoryBackend:
    def __init__(self, rows=(), *, add_mode="ok", import_mode="ok", library=ing.Library()):
        self.library, self.rows = library, deepcopy(list(rows))
        self.add_mode, self.import_mode = add_mode, import_mode
        self.adds, self.imports, self.reads, self.library_reads = 0, 0, 0, 0
        self.fail_reads = False

    def collection(self, key):
        if key != "TARGET":
            raise ing.Uncertain("Unknown target")
        return {"key": key, "library": self.library.json(), "data": {"name": "Target"}}

    def target_items(self, key):
        self.reads += 1
        if self.fail_reads:
            raise ing.Uncertain("Read connection lost")
        if self.add_mode == "late_success" and self.adds and self.reads == 5:
            self.rows[0]["data"]["collections"].append(key)
        return deepcopy([r for r in self.rows if ing.scoped(r, self.library) and key in r["data"]["collections"]])

    def library_items(self):
        self.library_reads += 1
        return deepcopy(self.rows)

    def get_item(self, key):
        return deepcopy(next(r for r in self.rows if r["key"] == key and ing.scoped(r, self.library)))

    def can_add(self):
        return self.add_mode != "unavailable"

    def add(self, key, collection):
        self.adds += 1
        if self.add_mode == "denied":
            raise ing.PermissionDenied("Collection is read-only")
        if self.add_mode in {"failed", "late_success"}:
            raise RuntimeError("Write response failed")
        if self.add_mode == "noop":
            return {"success": True}
        row = next(r for r in self.rows if r["key"] == key)
        row["data"]["collections"].append(collection)
        if self.add_mode == "destructive":
            row["data"]["collections"] = [collection]
        if self.add_mode == "unreadable":
            self.fail_reads = True
        if self.add_mode == "lost_reply":
            raise TimeoutError("Reply lost after commit")

    def import_entry(self, entry, collection):
        self.imports += 1
        if self.import_mode == "denied":
            raise ing.PermissionDenied("Import denied")
        if self.import_mode == "failed":
            raise TimeoutError("Uncertain import")
        self.rows.append(item("NEW" + str(self.imports), (collection,), self.library,
                              DOI=entry.get("doi"), extra="PMID: " + entry.get("pmid", ""),
                              title=entry["title"], abstractNote=entry.get("abstract", "")))
        if self.import_mode == "lost_reply":
            raise TimeoutError("Import reply lost after commit")


class ReconcileTests(unittest.TestCase):
    def run_import(self, backend, refs=None, lookup=None, collection="TARGET"):
        return ing.reconcile(refs or sources(), backend, collection,
                             lookup=lookup or (lambda _: deepcopy(ENTRY)), checks=3, delay=0)

    def test_target_existing_no_lookup_or_library_scan(self):
        backend = MemoryBackend([item(members=("TARGET",))])
        result = self.run_import(backend, lookup=Mock(side_effect=AssertionError("Unnecessary lookup")))
        self.assertTrue(result["complete"])
        self.assertEqual(result["counts"], {"target_existing": 1})
        self.assertEqual((backend.adds, backend.imports, backend.library_reads), (0, 0, 0))

    def test_reuse_preserves_same_item_and_original_members(self):
        backend = MemoryBackend([item(members=("OTHER", "ANOTHER"))])
        result = self.run_import(backend, lookup=Mock(side_effect=AssertionError("Unnecessary lookup")))
        self.assertEqual(result["counts"], {"reused_added": 1})
        self.assertEqual(result["mappings"][0]["item"]["item_key"], "OLD")
        self.assertEqual(set(backend.rows[0]["data"]["collections"]), {"OTHER", "ANOTHER", "TARGET"})
        self.assertEqual((backend.adds, backend.imports), (1, 0))

    def test_unavailable_failed_and_false_success_fall_back(self):
        for mode in ("unavailable", "failed", "noop"):
            with self.subTest(mode=mode):
                backend = MemoryBackend([item()], add_mode=mode)
                result = self.run_import(backend)
                row = result["mappings"][0]
                self.assertTrue(result["complete"])
                self.assertEqual(row["outcome"], "fallback_import")
                self.assertEqual(row["item"]["item_key"], "NEW1")
                self.assertEqual(row["known_duplicates"][0]["item_key"], "OLD")
                self.assertTrue(row["fallback_reason"])
                self.assertTrue(row["membership_verified"])
                self.assertEqual(backend.imports, 1)
                self.assertEqual(backend.rows[0]["data"]["collections"], ["OTHER"])

    def test_lost_add_response_is_success_without_import(self):
        backend = MemoryBackend([item()], add_mode="lost_reply")
        result = self.run_import(backend)
        self.assertEqual(result["counts"], {"reused_added": 1})
        self.assertEqual(backend.imports, 0)
        self.assertIn("response abnormal", result["mappings"][0]["anomalies"][0])

    def test_late_success_found_by_pre_fallback_check(self):
        backend = MemoryBackend([item()], add_mode="late_success")
        result = self.run_import(backend)
        self.assertEqual(result["counts"], {"reused_added": 1})
        self.assertEqual(backend.imports, 0)
        self.assertIsNone(result["mappings"][0]["fallback_reason"])

    def test_new_article_imported_once(self):
        backend = MemoryBackend()
        result = self.run_import(backend)
        self.assertEqual(result["counts"], {"new_import": 1})
        self.assertEqual((backend.adds, backend.imports), (0, 1))

    def test_lost_import_reply_is_verified_without_retry(self):
        backend = MemoryBackend(import_mode="lost_reply")
        result = self.run_import(backend)
        self.assertTrue(result["complete"])
        self.assertEqual(backend.imports, 1)

    def test_all_source_occurrences_and_locations_survive(self):
        refs = ing.normalize_sources([
            {"document_id": name, "references": [{"reference_id": "3", "label": "[3]",
             "raw_text": "Original reference text", "doi": ENTRY["doi"],
             "source_location": {"opaque": [name, 6, {"field": "anything"}]}}]}
            for name in ("doc-a", "doc-b")])
        backend = MemoryBackend()
        result = self.run_import(backend, refs)
        self.assertEqual(len(result["mappings"]), 2)
        self.assertEqual(backend.imports, 1)
        self.assertEqual([r["item"]["item_key"] for r in result["mappings"]], ["NEW1", "NEW1"])
        for source, row in zip(refs, result["mappings"]):
            self.assertEqual(row["source"]["source_location"], source["source_location"])
            self.assertEqual(row["source"]["label"], "[3]")
            self.assertEqual(row["source"]["raw_text"], "Original reference text")

    def test_multiple_candidates_are_pending_without_writes(self):
        backend = MemoryBackend([item("A"), item("B")])
        result = self.run_import(backend)
        self.assertFalse(result["complete"])
        self.assertTrue(result["mappings"][0]["match"]["ambiguous"])
        self.assertEqual((backend.adds, backend.imports), (0, 0))

    def test_conflicting_identifiers_author_or_year_never_associated(self):
        for fields in ({"pmid": "87654321"}, {"first_author": "Jones"}, {"year": "2010"},
                       {"doi": "10.1000/other", "title": ENTRY["title"]}):
            with self.subTest(fields=fields):
                backend = MemoryBackend([item()])
                result = self.run_import(backend, sources({"doi": ENTRY["doi"], **fields}))
                self.assertTrue(result["mappings"][0]["match"]["ambiguous"])
                self.assertIsNone(result["mappings"][0]["item"])
                self.assertEqual((backend.adds, backend.imports), (0, 0))

    def test_metadata_disagreement_is_not_imported(self):
        backend = MemoryBackend()
        result = self.run_import(backend, lookup=lambda _: {**ENTRY, "doi": "10.1000/wrong"})
        self.assertFalse(result["complete"])
        self.assertEqual(backend.imports, 0)

    def test_group_and_personal_same_key_are_not_same_item(self):
        group = ing.Library("group", 456)
        backend = MemoryBackend([item("SAME", library=ing.Library()),
                                 item("SAME", library=group, DOI="10.1000/other", title="Unrelated", extra="")], library=group)
        result = self.run_import(backend)
        self.assertEqual(result["counts"], {"new_import": 1})
        self.assertEqual(result["mappings"][0]["item"]["library"], group.json())
        self.assertEqual(backend.adds, 0)

    def test_explicit_identity_wrong_library_or_missing_is_pending(self):
        for identity in ({"library": {"type": "group", "id": 456}, "item_key": "A"},
                         {"library": {"type": "user", "id": 0}, "item_key": "MISSING"}):
            backend = MemoryBackend()
            result = self.run_import(backend, sources({"doi": ENTRY["doi"], "zotero_item": identity}))
            self.assertFalse(result["complete"])
            self.assertEqual(backend.imports, 0)

    def test_explicit_old_identity_does_not_pin_fallback_mapping(self):
        backend = MemoryBackend([item()], add_mode="unavailable")
        refs = sources({"zotero_item": {"library": ing.Library().json(), "item_key": "OLD"}})
        result = self.run_import(backend, refs)
        self.assertTrue(result["complete"])
        self.assertEqual(result["mappings"][0]["item"]["item_key"], "NEW1")

    def test_identifier_enrichment_finds_doi_only_existing_item(self):
        backend = MemoryBackend([item(extra="")])
        result = self.run_import(backend, sources({"pmid": ENTRY["pmid"]}))
        self.assertEqual(result["counts"], {"reused_added": 1})
        self.assertEqual(backend.imports, 0)

    def test_permission_denial_does_not_trigger_alternate_write(self):
        backend = MemoryBackend([item()], add_mode="denied")
        result = self.run_import(backend)
        self.assertEqual(result["counts"], {"failed": 1})
        self.assertEqual(backend.imports, 0)

    def test_unknown_membership_blocks_fallback(self):
        backend = MemoryBackend([item()], add_mode="unreadable")
        result = self.run_import(backend)
        self.assertFalse(result["complete"])
        self.assertEqual(backend.imports, 0)
        self.assertLess(backend.reads, 10)

    def test_destructive_reuse_is_not_reported_as_success(self):
        backend = MemoryBackend([item()], add_mode="destructive")
        result = self.run_import(backend)
        self.assertFalse(result["complete"])
        self.assertEqual(backend.imports, 0)

    def test_failed_import_does_not_repeat_for_later_source(self):
        backend = MemoryBackend(import_mode="failed")
        result = self.run_import(backend, sources({"doi": ENTRY["doi"]}, {"pmid": ENTRY["pmid"]}))
        self.assertFalse(result["complete"])
        self.assertEqual(backend.imports, 1)
        self.assertEqual(len(result["mappings"]), 2)

    def test_final_snapshot_failure_revokes_success(self):
        backend = MemoryBackend([item(members=("TARGET",))])
        original = backend.target_items
        def read(key):
            if backend.reads == 2:
                return []
            return original(key)
        backend.target_items = read
        result = self.run_import(backend)
        self.assertFalse(result["complete"])
        self.assertFalse(result["mappings"][0]["membership_verified"])

    def test_missing_target_returns_uncompleted_mapping(self):
        backend = MemoryBackend()
        result = self.run_import(backend, collection="MISSING")
        self.assertFalse(result["complete"])
        self.assertEqual(backend.imports, 0)

    def test_duplicate_source_identity_rejected(self):
        with self.assertRaises(ValueError):
            sources({"reference_id": "3"}, {"reference_id": "3"})


class InterfaceTests(unittest.TestCase):
    def test_ordinary_file_import_without_mapping_or_plugin(self):
        backend = MemoryBackend()
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "refs.md"
            source.write_text("[3] DOI: " + ENTRY["doi"], encoding="utf-8")
            args = z.build_parser().parse_args(["import-references", "--source", str(source),
                                                "--collection", "TARGET", "--yes"])
            with patch.object(ing, "LocalBackend", return_value=backend), \
                 patch.object(ing, "Plugin"), patch.object(z, "_lookup_by_doi", return_value=deepcopy(ENTRY)), \
                 patch("sys.stdout", new_callable=io.StringIO) as output:
                args.func(args)
            result = json.loads(output.getvalue())
            self.assertTrue(result["complete"])
            self.assertIsNone(result["mapping_file"])
            self.assertEqual(len(list(Path(directory).iterdir())), 1)
            self.assertEqual(ing.file_sources([source])[0]["label"], "[3]")
            self.assertTrue(ing.file_sources([source])[0]["raw_text"].startswith("[3]"))

    def test_json_input_optional_mapping_round_trip(self):
        backend = MemoryBackend([item(members=("TARGET",))])
        with tempfile.TemporaryDirectory() as directory:
            source, dest = Path(directory) / "in.json", Path(directory) / "map.json"
            source.write_text(json.dumps({"documents": [{"document_id": "D", "references": [
                {"reference_id": "3", "doi": ENTRY["doi"], "source_location": [1, {"field": "a"}]}]}]}), encoding="utf-8")
            args = z.build_parser().parse_args(["import-references", "--references-json", str(source),
                "--mapping-out", str(dest), "--collection", "TARGET", "--yes"])
            with patch.object(ing, "LocalBackend", return_value=backend), patch.object(ing, "Plugin"), \
                 patch("sys.stdout", new_callable=io.StringIO):
                args.func(args)
            result = json.loads(dest.read_text(encoding="utf-8"))
            self.assertEqual(result["schema_version"], ing.SCHEMA_VERSION)
            self.assertEqual(result["mappings"][0]["source"]["source_location"], [1, {"field": "a"}])
            self.assertIsNone(result["mappings"][0]["item"]["uri"])


class ProtocolTests(unittest.TestCase):
    def plugin(self, names=("get_libraries", "add_items_to_collection", "create_collection")):
        schemas = {
            "get_libraries": {"limit": {}, "offset": {}},
            "add_items_to_collection": {"libraryID": {}, "collectionKey": {}, "itemKeys": {}},
            "create_collection": {"libraryID": {}, "name": {}, "parentCollection": {}}}
        plugin = ing.Plugin.__new__(ing.Plugin)
        plugin.denied, plugin.reason = False, None
        plugin.tools = {name: {"inputSchema": {"properties": schemas[name]}} for name in names}
        plugin.call = Mock(return_value=[{"libraryID": 1, "libraryType": "user"}])
        return plugin

    def test_add_uses_supported_additive_plugin_tool_and_internal_id(self):
        plugin = self.plugin()
        backend = ing.LocalBackend(ing.Library("user", 789), plugin)
        backend.add("ITEM", "TARGET")
        self.assertEqual(plugin.call.call_args.args,
                         ("add_items_to_collection", {"libraryID": 1, "collectionKey": "TARGET", "itemKeys": ["ITEM"]}))

    def test_group_id_is_not_used_as_internal_library_id(self):
        plugin = self.plugin()
        plugin.call.return_value = [{"libraryID": 9, "libraryType": "group", "name": "Research"}]
        backend = ing.LocalBackend(ing.Library("group", 456), plugin)
        backend.read = Mock(return_value=[{"id": 456, "data": {"name": "Research"}}])
        self.assertEqual(backend.internal_library_id(), 9)

    def test_duplicate_group_names_are_not_guessed(self):
        plugin = self.plugin()
        plugin.call.return_value = [{"libraryID": 9, "libraryType": "group", "name": "Research"}]
        backend = ing.LocalBackend(ing.Library("group", 456), plugin)
        backend.read = Mock(return_value=[{"id": n, "data": {"name": "Research"}} for n in (456, 789)])
        with self.assertRaises(ing.Unavailable):
            backend.internal_library_id()

    def test_local_alias_resolves_actual_user_identity(self):
        backend = ing.LocalBackend(plugin=self.plugin(()))
        response = {"key": "TARGET", "library": {"type": "user", "id": 789}, "data": {}}
        backend.read = Mock(return_value=response)
        backend.collection("TARGET")
        self.assertEqual(backend.library, ing.Library("user", 789))
        self.assertEqual(ing.identity(item(library=backend.library), backend.library)["library"]["id_kind"], "user_id")

    def test_uninstalled_plugin_needs_no_dependency(self):
        with patch.object(ing.Plugin, "rpc", side_effect=ConnectionRefusedError("Not installed")):
            plugin = ing.Plugin()
        self.assertFalse(plugin.supports("add_items_to_collection", {"libraryID", "collectionKey", "itemKeys"}))

    def test_probe_permission_denial_not_treated_as_unavailable(self):
        with patch.object(ing.Plugin, "rpc", side_effect=ing.PermissionDenied("Forbidden")):
            plugin = ing.Plugin()
        with self.assertRaises(ing.PermissionDenied):
            plugin.supports("add_items_to_collection", {"libraryID", "collectionKey", "itemKeys"})

    def test_unknown_required_tool_parameter_disables_reuse(self):
        plugin = self.plugin()
        plugin.tools["add_items_to_collection"]["inputSchema"]["required"] = ["secret"]
        self.assertFalse(ing.LocalBackend(plugin=plugin).can_add())

    def test_sse_response_and_wrong_request_id(self):
        self.assertEqual(ing.parse_rpc_body('event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"success":true}}\n\n', 1), {"success": True})
        with self.assertRaises(ing.Uncertain):
            ing.parse_rpc_body('{"id":2,"result":{}}', 1)

    def selected_backend(self):
        backend = ing.LocalBackend(plugin=self.plugin(()))
        backend.collection = Mock(return_value={"key": "TARGET", "library": {"name": "My Library"}})
        backend.all = Mock(return_value=[{"key": "PARENT", "data": {"name": "Thesis", "parentCollection": False}},
                                        {"key": "TARGET", "data": {"name": "Final", "parentCollection": "PARENT"}}])
        backend.read = Mock(return_value=[])
        selected = {"libraryID": 1, "id": 22, "editable": True, "libraryEditable": True,
                    "targets": [{"id": "L1", "level": 0, "name": "My Library"},
                                {"id": "C21", "level": 1, "name": "Thesis"},
                                {"id": "C22", "level": 2, "name": "Final"}]}
        return backend, selected

    def test_no_plugin_connector_selection_proven_by_full_hierarchy(self):
        backend, selected = self.selected_backend()
        with patch.object(z, "connector_post", return_value=Mock(ok=True, status=200, text=json.dumps(selected))):
            backend.assert_selected("TARGET")

    def test_wrong_connector_target_blocks_import(self):
        backend, selected = self.selected_backend()
        selected["id"] = 21
        with patch.object(z, "connector_post", return_value=Mock(ok=True, status=200, text=json.dumps(selected))) as post:
            with self.assertRaises(ing.Unavailable):
                backend.import_entry(ENTRY, "TARGET")
        self.assertEqual(post.call_count, 1)
        self.assertEqual(post.call_args.args[0], "/connector/getSelectedCollection")

    def test_native_connector_import_without_plugin_sanitizes_ris(self):
        backend, selected = self.selected_backend()
        responses = [Mock(ok=True, status=200, text=json.dumps(selected)), Mock(ok=True, status=201, text="[]")]
        with patch.object(z, "connector_post", side_effect=responses) as post:
            backend.import_entry(ENTRY, "TARGET")
        self.assertTrue(post.call_args.args[0].startswith("/connector/import?session="))
        ris = post.call_args.args[1]
        self.assertIn("AB  - " + ENTRY["abstract"], ris)
        self.assertIn("M2  - PMID: " + ENTRY["pmid"], ris)
        for tag in ("N1  -", "N2  - PMID", "L1  -"):
            self.assertNotIn(tag, ris)

    def test_create_collection_lost_reply_checks_before_retry(self):
        plugin = self.plugin()
        backend = ing.LocalBackend(plugin=plugin)
        backend.internal_library_id = Mock(return_value=1)
        backend.all = Mock(side_effect=[[], [{"key": "NEWCOL", "library": ing.Library().json(),
                                            "data": {"name": "New", "parentCollection": False}}]])
        plugin.call.side_effect = TimeoutError("Lost reply")
        result = ing.create_collection(backend, "New", delay=0)
        self.assertTrue(result["verified"])
        self.assertEqual(result["key"], "NEWCOL")
        self.assertEqual(plugin.call.call_count, 1)

    def test_create_collection_without_plugin_is_explicitly_unavailable(self):
        backend = ing.LocalBackend(plugin=self.plugin(()))
        backend.all = Mock(return_value=[])
        with self.assertRaises(ing.Unavailable):
            ing.create_collection(backend, "New", delay=0)


if __name__ == "__main__":
    unittest.main()
