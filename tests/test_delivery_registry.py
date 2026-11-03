"""Delivery registry dispatch without loading target code or independent bug answers."""
import copy
import hashlib
import importlib.util
import json
import shutil
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


PROJECT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("delivery_registry_check", PROJECT / "scripts/check_delivery.py")
DELIVERY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DELIVERY)


class DeliveryRegistryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        self.benchmarks = self.root / "benchmarks"
        self.benchmarks.mkdir()
        self.ugc = self.benchmarks / "ugc-diagnostics-dev-v1"
        shutil.copytree(PROJECT / "benchmarks/ugc-diagnostics-dev-v1", self.ugc,
                        ignore=shutil.ignore_patterns("__pycache__"))
        self.manifest = json.loads((self.ugc / "manifest.json").read_text())
        self.root_patch = patch.object(DELIVERY, "ROOT", self.root)
        self.root_patch.start()
        self.addCleanup(self.root_patch.stop)

    def test_ugc_dev_schema_checks_all_pinned_bytes_without_executing_fixture(self):
        marker = self.root / "untrusted-code-ran"
        # Data-directory Python cannot choose the code of the trusted registry.
        (self.ugc / "qualification.py").write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
        reads = []
        original_read = Path.read_bytes
        original_exec = exec

        def record_bytes(path):
            reads.append(path.resolve())
            return original_read(path)

        def no_target_execution(code, globals=None, locals=None):
            if isinstance(code, types.CodeType) and "/source/" in code.co_filename:
                raise AssertionError("Delivery integrity check executed target source")
            return original_exec(code, globals, locals)

        with patch.object(DELIVERY, "load_dataset", side_effect=AssertionError("Bug schema loader received UGC")), \
                patch.object(Path, "read_bytes", record_bytes), \
                patch("builtins.exec", no_target_execution):
            result = DELIVERY.check()
        self.assertFalse(marker.exists())
        self.assertEqual(result["real_model_calls"], 0)
        self.assertEqual(len(result["datasets"]), 1)
        row = result["datasets"][0]
        self.assertEqual(row["dataset"], "ugc-diagnostics-dev-v1")
        self.assertEqual(row["tasks"], 4)
        self.assertEqual(row["split"], "dev")
        self.assertTrue(row["synthetic"])
        self.assertTrue(row["public"])
        self.assertFalse(row["independent_evaluation"])
        expected_paths = {self.ugc / task["source_dir"] / relative
                          for task in self.manifest["tasks"] for relative in task["files"]}
        self.assertEqual(row["verified_files"], len(expected_paths))
        self.assertTrue(expected_paths <= set(reads))
        self.assertEqual(row["sha256"], hashlib.sha256((self.ugc / "manifest.json").read_bytes()).hexdigest())

    def test_each_source_test_and_readme_hash_is_verified(self):
        # Mutating every registered file in turn proves none is silently omitted.
        for task in self.manifest["tasks"]:
            for relative in task["files"]:
                path = self.ugc / task["source_dir"] / relative
                original = path.read_bytes()
                with self.subTest(case=task["id"], path=relative):
                    path.write_bytes(original + b"\n# altered delivery fixture\n")
                    try:
                        with self.assertRaisesRegex(ValueError, "fingerprint mismatch"):
                            DELIVERY.check()
                    finally:
                        path.write_bytes(original)

    def test_ugc_metadata_and_self_reported_hash_changes_cannot_redefine_contract(self):
        path = self.ugc / "manifest.json"
        for key, value in (("split", "test"), ("synthetic", False), ("public", False),
                           ("independent_evaluation", True), ("dataset_id", "other")):
            altered = copy.deepcopy(self.manifest)
            altered[key] = value
            with self.subTest(key=key):
                path.write_text(json.dumps(altered))
                with self.assertRaises(ValueError):
                    DELIVERY.check()
        altered = copy.deepcopy(self.manifest)
        altered["tasks"][0]["files"]["map_exporter.py"] = "0" * 64
        path.write_text(json.dumps(altered))
        with self.assertRaisesRegex(ValueError, "fixture contract or fingerprint"):
            DELIVERY.check()

    def test_unknown_manifest_schema_is_rejected_without_importing_its_python(self):
        directory = self.benchmarks / "unknown-schema"
        directory.mkdir()
        (directory / "manifest.json").write_text(json.dumps({
            "dataset_id": "ugc-diagnostics-dev-v1", "version": 1, "tasks": [{"id": "malicious"}],
        }))
        marker = self.root / "unknown-registry-ran"
        (directory / "qualification.py").write_text(f"from pathlib import Path\nPath({str(marker)!r}).touch()\n")
        with self.assertRaisesRegex(ValueError, "unsupported or empty dataset"):
            DELIVERY.check()
        self.assertFalse(marker.exists())

    def test_existing_bug_schema_still_rejects_invalid_pinned_commit(self):
        directory = self.benchmarks / "unknown-schema"
        directory.mkdir()
        (directory / "manifest.json").write_text(json.dumps({
            "schema_version": 1,
            "tasks": [{"id": "invalid-bug", "base_commit": "main", "fix_commit": "latest"}],
        }))
        with self.assertRaisesRegex(ValueError, "upstream commit must be pinned"):
            DELIVERY.check()

    def test_ugc_symlink_cannot_redirect_file_integrity_reads(self):
        task = self.manifest["tasks"][0]
        path = self.ugc / task["source_dir"] / "map_exporter.py"
        outside = self.root / "outside.py"
        outside.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(outside)
        with self.assertRaisesRegex(ValueError, "symlink"):
            DELIVERY.check()


if __name__ == "__main__":
    unittest.main()
