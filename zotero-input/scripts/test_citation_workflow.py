"""CLI/package integration with synthetic evidence and isolated Zotero backends."""
from contextlib import ExitStack, redirect_stdout
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import citation_handoff as ch
import provenance_notes as pn
from test_citations import payload, SENTENCE
from test_provenance_notes import MetadataMemoryBackend, NotesMemory


class WorkflowTests(unittest.TestCase):
    def test_cli_cite_import_notes_verify_with_separate_consent_and_final_mapping(self):
        backend = MetadataMemoryBackend()
        with tempfile.TemporaryDirectory() as root, ExitStack() as context:
            root = Path(root)
            review = root / "review.json"
            review.write_text(json.dumps(payload()), encoding="utf-8")
            context.enter_context(redirect_stdout(io.StringIO()))
            context.enter_context(patch.object(ch.ing, "LocalBackend", return_value=backend))
            context.enter_context(patch.object(ch.ing, "Plugin"))
            context.enter_context(patch.object(ch.z.urllib.request, "urlopen", side_effect=AssertionError("Real network blocked")))
            bundle_path = root / "cited" / "citation-bundle.json"
            mapping_path = root / "imported.json"
            self.assertEqual(ch.main(["prepare", "--review", str(review), "--outdir", str(root / "cited")]), 0)
            self.assertEqual(ch.main(["import", "--bundle", str(bundle_path), "--collection", "TARGET",
                                      "--mapping-out", str(mapping_path)]), 3)
            self.assertFalse(mapping_path.exists())
            self.assertEqual(backend.imports, 0)
            self.assertEqual(ch.main(["import", "--bundle", str(bundle_path), "--collection", "TARGET",
                                      "--mapping-out", str(mapping_path), "--approve-import"]), 0)
            self.assertEqual(backend.imports, 1)
            mapping = ch.read_json(mapping_path)
            self.assertEqual(mapping["mappings"][0]["item"]["item_key"], "NEW1")
            self.assertTrue(mapping["mappings"][0]["membership_verified"])
            arguments = ["notes-plan", "--bundle", str(bundle_path), "--mapping", str(mapping_path),
                         "--outdir", str(root / "notes")]
            self.assertEqual(ch.main(arguments + ["--approve-notes"]), 3)
            self.assertFalse((root / "notes").exists())
            self.assertEqual(ch.main(arguments + ["--approve-notes", "--acknowledge-token-cost"]), 0)
            notes = NotesMemory(backend.rows[0])
            context.enter_context(patch.object(pn, "NoteBackend", return_value=notes))
            job_path = root / "notes" / "notes-job.json"
            self.assertEqual(ch.main(["notes-write", "--job", str(job_path), "--result-out", str(root / "written.json")]), 0)
            self.assertEqual(ch.main(["notes-verify", "--job", str(job_path), "--result-out", str(root / "verified.json")]), 0)
            self.assertEqual(notes.creates, 1)
            self.assertEqual(backend.imports, 1)
            self.assertIn(SENTENCE + "[1]", (root / "cited" / "document-001.cited.md").read_text(encoding="utf-8"))
            self.assertTrue(ch.read_json(root / "verified.json")["complete"])

    def test_portable_skill_runtime_without_other_skills_or_site_packages(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            scripts = root / "only-skill" / "scripts"
            scripts.mkdir(parents=True)
            source = Path(__file__).parent
            for module in source.iterdir():
                if module.suffix in {".py", ".js"} and not module.name.startswith("test_"):
                    shutil.copyfile(module, scripts / module.name)
            (scripts / "_vendor").mkdir()
            shutil.copyfile(source / "_vendor" / "nature_citation.py", scripts / "_vendor" / "nature_citation.py")
            review = root / "review.json"
            review.write_text(json.dumps(payload()), encoding="utf-8")
            env = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH", "PYTHONHOME"}}
            executable = [sys.executable, "-S", "-X", "utf8", "-B", str(scripts / "citation_handoff.py")]
            for arguments in (["search", "--help"], ["prepare", "--review", str(review), "--outdir", str(root / "cited")],
                              ["notes-plan", "--bundle", "missing", "--mapping", "missing", "--outdir", "missing"]):
                result = subprocess.run(executable + arguments, cwd=root, env=env, text=True, encoding="utf-8",
                                        capture_output=True, timeout=20)
                self.assertEqual(result.returncode, 3 if arguments[0] == "notes-plan" else 0, result.stderr)
            bundle = ch.read_json(root / "cited" / "citation-bundle.json")
            self.assertTrue(bundle["complete"])
            self.assertFalse((root / "missing").exists())


if __name__ == "__main__":
    unittest.main()
