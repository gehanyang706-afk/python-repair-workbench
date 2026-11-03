"""Qualify only the four reviewed bundled fixtures; never load candidate code."""

from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parent
TRUSTED_FILES = {'case-01': {'map_exporter.py': 'b429e27d0cd44e2a653e07d3ffe671ce681bc616d4a542f936c059e997ef5422',
             'tests/test_export.py': '39395d15b2f0e5015499b448d24e6a41c8426b988021223c82969638ff5bc52b',
             'README.md': '5321279d0a923ac3f855b98a72265f2ad7237446efea5f988eae296f09898a41'},
 'case-02': {'map_exporter.py': 'ed51ae8264993e56eebc434af6eaf8060e13ae55ad1701eecd06ca2d7f926442',
             'tests/test_export.py': '39395d15b2f0e5015499b448d24e6a41c8426b988021223c82969638ff5bc52b',
             'README.md': '5321279d0a923ac3f855b98a72265f2ad7237446efea5f988eae296f09898a41'},
 'case-03': {'map_exporter.py': '0580947e6260b2ae0b46e796c7af3bddf3baa84b26032f5925980bf90a8f0604',
             'tests/test_export.py': '39395d15b2f0e5015499b448d24e6a41c8426b988021223c82969638ff5bc52b',
             'README.md': '5321279d0a923ac3f855b98a72265f2ad7237446efea5f988eae296f09898a41'},
 'case-04': {'map_exporter.py': '3609421ba5b08a9ad2e9da26cf01b23b4be813a06b517bb9027a23ac2bc8e1f8',
             'tests/test_export.py': '39395d15b2f0e5015499b448d24e6a41c8426b988021223c82969638ff5bc52b',
             'README.md': '5321279d0a923ac3f855b98a72265f2ad7237446efea5f988eae296f09898a41'}}



def fixture_manifest():
    """Return verified public metadata; no arbitrary repository path is accepted."""
    data = json.loads((ROOT / "manifest.json").read_text())
    if (data.get("dataset_id") != "ugc-diagnostics-dev-v1"
            or data.get("split") != "dev"
            or data.get("synthetic") is not True
            or data.get("public") is not True
            or data.get("independent_evaluation") is not False):
        raise ValueError("unexpected development dataset identity")
    tasks = data.get("tasks", [])
    if [task.get("id") for task in tasks] != list(TRUSTED_FILES):
        raise ValueError("unexpected bundled fixture registry")
    for task in tasks:
        case_id = task["id"]
        if (task.get("source_dir") != f"{case_id}/source"
                or task.get("entrypoint") != "map_exporter:export_map"
                or task.get("allowed_paths") != ["map_exporter.py"]
                or task.get("visible_tests") != ["tests/test_export.py"]
                or task.get("files") != TRUSTED_FILES[case_id]):
            raise ValueError("unexpected fixture contract or fingerprint")
    return data


def _trusted_bytes(case_id, relative):
    if case_id not in TRUSTED_FILES or relative not in TRUSTED_FILES[case_id]:
        raise ValueError("only the reviewed bundled fixture paths are accepted")
    path = ROOT / case_id / "source" / relative
    for parent in (ROOT / case_id, ROOT / case_id / "source", *path.parents):
        if parent == ROOT:
            break
        if parent.is_symlink():
            raise ValueError("bundled fixture symlinks are not accepted")
    if path.is_symlink():
        raise ValueError("bundled fixture symlinks are not accepted")
    content = path.read_bytes()
    if sha256(content).hexdigest() != TRUSTED_FILES[case_id][relative]:
        raise ValueError("bundled fixture fingerprint mismatch; refusing host execution")
    return content


def load_fixture(case_id):
    """Load a fixed, hash-pinned developer fixture, never an Agent candidate."""
    fixture_manifest()
    if case_id not in TRUSTED_FILES:
        raise ValueError("unknown reviewed bundled fixture")
    for relative in TRUSTED_FILES[case_id]:
        _trusted_bytes(case_id, relative)
    source = _trusted_bytes(case_id, "map_exporter.py")
    module = ModuleType(f"ugc_bundled_{case_id.replace('-', '_')}")
    module.__file__ = str(ROOT / case_id / "source" / "map_exporter.py")
    exec(compile(source, module.__file__, "exec"), module.__dict__)
    return module.export_map


def qualify():
    """Check fixture semantics on small inputs; do not claim measured speedups."""
    cases = []
    for case_id in TRUSTED_FILES:
        export = load_fixture(case_id)
        objects = [
            {"id": "o2", "resource_id": "r2", "meta": {"tags": ["spawn"]}},
            {"id": "o1", "resource_id": "r1"},
            {"id": "o1", "resource_id": "r2"},
        ]
        resources = [
            {"id": "r1", "path": "first.png"},
            {"id": "r2", "path": "shared.png"},
            {"id": "r1", "path": "later.png"},
        ]
        original = deepcopy((objects, resources))
        expected = [
            {"object_id": "o2", "resource_path": "shared.png"},
            {"object_id": "o1", "resource_path": "first.png"},
            {"object_id": "o1", "resource_path": "shared.png"},
        ]
        if export(objects, resources) != expected or (objects, resources) != original:
            raise AssertionError(f"{case_id}: ordering, duplicate ID or mutation failure")
        if export([], resources) != [] or export([], []) != []:
            raise AssertionError(f"{case_id}: empty input failure")
        missing_objects = objects + [{"id": "bad", "resource_id": "absent"}]
        missing_before = deepcopy((missing_objects, resources))
        try:
            export(missing_objects, resources)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{case_id}: missing resource did not raise ValueError")
        if (missing_objects, resources) != missing_before:
            raise AssertionError(f"{case_id}: exception path mutated input")
        cases.append({"id": case_id, "semantic_contract": "passed"})
    return {
        "dataset_id": "ugc-diagnostics-dev-v1", "qualified": True,
        "split": "dev", "synthetic": True, "public": True,
        "independent_evaluation": False, "real_model_calls": 0,
        "performance_measured": False, "cases": cases,
    }


if __name__ == "__main__":
    print(json.dumps(qualify(), ensure_ascii=False, indent=2))
