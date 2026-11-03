"""Qualification of public synthetic UGC fixtures, with no candidate execution."""

from copy import deepcopy
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "benchmarks" / "ugc-diagnostics-dev-v1"
SPEC = importlib.util.spec_from_file_location(
    "ugc_fixture_qualification", CORPUS / "qualification.py"
)
QUALIFICATION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(QUALIFICATION)


class UGCWorkloadTests(unittest.TestCase):
    def test_registry_is_public_synthetic_development_only(self):
        data = QUALIFICATION.fixture_manifest()
        self.assertEqual(data["split"], "dev")
        self.assertTrue(data["synthetic"])
        self.assertTrue(data["public"])
        self.assertFalse(data["independent_evaluation"])
        self.assertEqual(len(data["tasks"]), 4)
        for task in data["tasks"]:
            self.assertEqual(task["allowed_paths"], ["map_exporter.py"])
            self.assertEqual(task["visible_tests"], ["tests/test_export.py"])
            self.assertNotIn("root_cause", task)
            self.assertNotIn("reference_patch", task)

    def test_all_reviewed_fixtures_satisfy_small_semantic_contract(self):
        result = QUALIFICATION.qualify()
        self.assertTrue(result["qualified"])
        self.assertFalse(result["performance_measured"])
        self.assertEqual(result["real_model_calls"], 0)
        self.assertEqual(len(result["cases"]), 4)
        self.assertTrue(all(row["semantic_contract"] == "passed" for row in result["cases"]))

    def test_valid_empty_path_and_unreferenced_resources_are_preserved(self):
        objects = [{"id": "x", "resource_id": "r"}]
        resources = [
            {"id": "unused", "path": "unused.png"},
            {"id": "r", "path": ""},
            {"id": "r", "path": "second.png"},
        ]
        expected = [{"object_id": "x", "resource_path": ""}]
        for case_id in QUALIFICATION.TRUSTED_FILES:
            with self.subTest(case_id=case_id):
                before = deepcopy((objects, resources))
                self.assertEqual(QUALIFICATION.load_fixture(case_id)(objects, resources), expected)
                self.assertEqual((objects, resources), before)

    def test_qualification_has_no_arbitrary_repository_or_candidate_loader(self):
        for invalid in ("../case-01", "../../runtime/candidate", "/tmp/repository", "unknown"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                QUALIFICATION.load_fixture(invalid)
        with self.assertRaises(TypeError):
            QUALIFICATION.qualify(CORPUS)

    def test_hash_mismatch_prevents_fixture_compilation(self):
        original = QUALIFICATION._trusted_bytes

        def altered_bytes(case_id, relative):
            if relative == "map_exporter.py":
                raise ValueError("bundled fixture fingerprint mismatch")
            return original(case_id, relative)

        with patch.object(QUALIFICATION, "_trusted_bytes", side_effect=altered_bytes), \
                patch("builtins.compile") as compile_source:
            with self.assertRaises(ValueError):
                QUALIFICATION.load_fixture("case-01")
        compile_source.assert_not_called()

    def test_trusted_source_hash_is_checked_before_host_execution(self):
        with patch.object(QUALIFICATION, "sha256") as digest:
            digest.return_value.hexdigest.return_value = "0" * 64
            with self.assertRaisesRegex(ValueError, "fingerprint mismatch"):
                QUALIFICATION._trusted_bytes("case-01", "map_exporter.py")


if __name__ == "__main__":
    unittest.main()
