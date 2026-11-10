"""Frozen, evaluator-owned experiment plans; no model or task code is executed.

``create_manifest`` accepts one shared configuration, tasks, named policies and
explicit file bindings. Example shared configuration::

    {"model": {"id": "qwen-max", "region": "cn-beijing"},
     "tools": ["read_source", "submit_ref_edits"],
     "limits": {"max_total_tokens": 120000, "max_model_calls": 24,
                "max_tool_calls": 120, "deadline_seconds": 900}}

Additional configuration (dataset revision, image identity, scoring, stopping,
etc.) is included in the hash. Do not put credentials in a plan. Each task has
``id``, ``cluster_id``, ``split`` (dev/validation/test), ``public`` (repo,
base_commit, problem_statement), and optional private ``evaluator`` metadata.
Variants contain only ``id`` and a named ``policy``: resource/tool/model
overrides cannot be supplied per variant. Policy implementation and whether it
honors the shared limits remain the runner's responsibility.

``agent_task`` is the only public projection. Never pass the full manifest to
an Agent. File hashes cover only explicitly bound files, not whole directory
trees, running services or container images; bind trusted inventory/inspection
receipts and check live state in the corresponding runner. Hashes detect drift,
not coordinated rewriting by a trusted operator. Keep the original digest
outside the writable plan and pass it to ``validate_manifest`` before runs.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
import stat


_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}\Z")
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
_PUBLIC = {"repo", "base_commit", "problem_statement"}
_EXPOSURE_KINDS = {"root_cause", "reference_patch", "hidden_feedback",
                   "task_tuning", "qualification_read"}
_MATERIAL_FIELDS = {"schema_version", "config", "config_sha256", "tasks",
                    "variants", "files", "exposures", "repeats", "seed", "schedule"}


def _json_value(value):
    if value is None or type(value) in (str, int, bool):
        return
    if type(value) is float and math.isfinite(value):
        return
    if type(value) is list:
        for item in value:
            _json_value(item)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _json_value(item)
        return
    raise ValueError("only finite JSON values and string object keys are allowed")


def canonical_bytes(value) -> bytes:
    """Canonical UTF-8 JSON: sorted keys, preserved array order, no NaN/coercion."""
    _json_value(value)
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def configuration_hash(config: dict) -> str:
    """Hash validated shared configuration; mapping insertion order is immaterial."""
    return _hash(_config(config))


def _hash(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _copy(value):
    return json.loads(canonical_bytes(value))


def _identifier(value, field):
    if type(value) is not str or not _ID.fullmatch(value):
        raise ValueError(f"invalid {field}")
    return value


def _integer(value, field, minimum=1, maximum=None):
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise ValueError(f"invalid {field}")
    return value


def _config(config):
    if type(config) is not dict or not {"model", "tools", "limits"} <= config.keys():
        raise ValueError("config requires shared model, tools and limits")
    result = _copy(config)
    model = result["model"]
    if (type(model) is not dict or any(type(model.get(key)) is not str or not model[key].strip()
                                     for key in ("id", "region"))):
        raise ValueError("model requires id and region")
    tools = result["tools"]
    if (type(tools) is not list or not tools
            or any(type(tool) is not str or not tool.strip() for tool in tools)
            or len(set(tools)) != len(tools)):
        raise ValueError("tools must be a nonempty unique list of names")
    limits = result["limits"]
    if type(limits) is not dict or not limits:
        raise ValueError("limits must be a nonempty object")
    for key, value in limits.items():
        _identifier(key, "limit name")
        if key.endswith("_seconds"):
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"invalid limit {key}")
        else:
            _integer(value, f"limit {key}")
    return result


def _tasks(tasks):
    if type(tasks) is not list or not tasks:
        raise ValueError("tasks must be a nonempty list")
    result, ids, clusters = [], set(), {}
    for task in tasks:
        if (type(task) is not dict or not {"id", "cluster_id", "split", "public"} <= task.keys()
                or not task.keys() <= {"id", "cluster_id", "split", "public", "evaluator"}):
            raise ValueError("invalid task fields")
        identifier = _identifier(task["id"], "task id")
        cluster = _identifier(task["cluster_id"], "cluster id")
        split = task["split"]
        if type(split) is not str or split not in {"dev", "validation", "test"}:
            raise ValueError("invalid task split")
        if identifier in ids:
            raise ValueError("duplicate task id")
        if cluster in clusters and clusters[cluster] != split:
            raise ValueError("defect cluster crosses task splits")
        public = task["public"]
        if (type(public) is not dict or set(public) != _PUBLIC
                or any(type(value) is not str or not value.strip() for value in public.values())):
            raise ValueError("public task requires only repo, base_commit and problem_statement")
        if "evaluator" in task and type(task["evaluator"]) is not dict:
            raise ValueError("evaluator metadata must be an object")
        ids.add(identifier)
        clusters[cluster] = split
        result.append(_copy(task))
    return sorted(result, key=lambda item: item["id"])


def _variants(variants):
    if type(variants) is not list or not 2 <= len(variants) <= 8:
        raise ValueError("an experiment requires 2–8 variants")
    result, ids = [], set()
    for variant in variants:
        if type(variant) is not dict or set(variant) != {"id", "policy"}:
            raise ValueError("variants allow id and named policy only; shared configuration cannot be overridden")
        identifier = _identifier(variant["id"], "variant id")
        _identifier(variant["policy"], "policy name")
        if identifier in ids:
            raise ValueError("duplicate variant id")
        ids.add(identifier)
        result.append(dict(variant))
    return sorted(result, key=lambda item: item["id"])


def _exposures(events, tasks):
    if type(events) is not list:
        raise ValueError("exposures must be a list")
    by_id = {task["id"]: task for task in tasks}
    result = []
    for event in events:
        if (type(event) is not dict or not {"task_id", "actor", "kind"} <= event.keys()
                or not event.keys() <= {"task_id", "actor", "kind", "note"}):
            raise ValueError("invalid exposure event")
        task_id = _identifier(event["task_id"], "exposure task id")
        if task_id not in by_id:
            raise ValueError("exposure references an unknown task")
        if (type(event["actor"]) is not str or event["actor"] not in {"developer", "evaluator"}
                or type(event["kind"]) is not str or event["kind"] not in _EXPOSURE_KINDS):
            raise ValueError("invalid exposure actor or kind")
        if "note" in event and type(event["note"]) is not str:
            raise ValueError("exposure note must be a string")
        if event["actor"] == "evaluator":
            if event["kind"] != "qualification_read":
                raise ValueError("only automated qualification reads are evaluator exposure events")
        elif by_id[task_id]["split"] != "dev":
            raise ValueError("developer-exposed tasks must belong to dev")
        result.append(_copy(event))
    return sorted(result, key=canonical_bytes)


def _path(value):
    path = Path(value).absolute()
    if ".." in path.parts or any(item.is_symlink() for item in (path, *path.parents)):
        raise ValueError("file bindings and manifest paths cannot traverse symlinks or '..'")
    return path


def _file_receipt(path):
    path = _path(path)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(descriptor, "rb") as stream:
        before = os.fstat(stream.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("file bindings require regular files")
        digest = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
        after = os.fstat(stream.fileno())
    if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError("bound file changed while hashing")
    return {"path": str(path), "size": after.st_size, "sha256": digest.hexdigest()}


def _schedule(tasks, variants, repeats, seed):
    rng = random.Random(seed)
    pairs = [(task["id"], repeat) for task in tasks for repeat in range(1, repeats + 1)]
    rng.shuffle(pairs)
    schedule = []
    for task_id, repeat in pairs:
        order = [variant["id"] for variant in variants]
        rng.shuffle(order)
        for variant_id in order:
            schedule.append({"run_id": _hash([task_id, repeat, variant_id]),
                             "task_id": task_id, "repeat": repeat, "variant_id": variant_id})
    return schedule


def create_manifest(config: dict, tasks: list, variants: list, files: dict, *,
                    exposures=None, repeats=1, seed=0) -> dict:
    """Build an evaluator-only frozen plan from explicit files (logical label→path).

    All tasks receive every variant on each repeat, in seeded paired order.
    Numeric resource limits must be positive integers, except ``*_seconds`` may
    be positive finite floats. Booleans are never accepted as numeric limits.
    Developer exposure to privileged details forces a task into dev; automated
    evaluator qualification reads alone do not. Exposure reports are trusted
    declarations, not a mechanism for detecting undisclosed access.
    """
    shared = _config(config)
    selected, policies = _tasks(tasks), _variants(variants)
    repeats = _integer(repeats, "repeats", maximum=1000)
    seed = _integer(seed, "seed", minimum=0, maximum=2**63 - 1)
    if len(selected) * len(policies) * repeats > 100000:
        raise ValueError("schedule exceeds 100000 runs")
    if type(files) is not dict or not files:
        raise ValueError("files must be a nonempty explicit binding map")
    bindings = []
    for label, path in files.items():
        _identifier(label, "file label")
        bindings.append({"label": label, **_file_receipt(path)})
    if len({item["path"] for item in bindings}) != len(bindings):
        raise ValueError("duplicate bound file path")
    material = {"schema_version": 1, "config": shared, "config_sha256": _hash(shared),
                "tasks": selected, "variants": policies, "files": sorted(bindings, key=lambda item: item["label"]),
                "exposures": _exposures([] if exposures is None else exposures, selected),
                "repeats": repeats, "seed": seed,
                "schedule": _schedule(selected, policies, repeats, seed)}
    return {**material, "manifest_sha256": _hash(material)}


def _validated_structure(manifest):
    if type(manifest) is not dict or set(manifest) != _MATERIAL_FIELDS | {"manifest_sha256"}:
        raise ValueError("invalid manifest fields")
    material = {key: manifest[key] for key in _MATERIAL_FIELDS}
    if type(manifest["schema_version"]) is not int or manifest["schema_version"] != 1:
        raise ValueError("unsupported manifest schema")
    if manifest["manifest_sha256"] != _hash(material):
        raise ValueError("manifest_hash_mismatch")
    shared, tasks, variants = _config(manifest["config"]), _tasks(manifest["tasks"]), _variants(manifest["variants"])
    repeats = _integer(manifest["repeats"], "repeats", maximum=1000)
    seed = _integer(manifest["seed"], "seed", minimum=0, maximum=2**63 - 1)
    if len(tasks) * len(variants) * repeats > 100000:
        raise ValueError("schedule exceeds 100000 runs")
    if (manifest["config_sha256"] != _hash(shared) or tasks != manifest["tasks"]
            or variants != manifest["variants"]
            or _exposures(manifest["exposures"], tasks) != manifest["exposures"]
            or canonical_bytes(_schedule(tasks, variants, repeats, seed)) != canonical_bytes(manifest["schedule"])):
        raise ValueError("manifest configuration, task ordering or paired schedule mismatch")
    files = manifest["files"]
    if type(files) is not list or not files:
        raise ValueError("missing file bindings")
    labels, paths = set(), set()
    for item in files:
        if type(item) is not dict or set(item) != {"label", "path", "size", "sha256"}:
            raise ValueError("invalid file binding")
        label = _identifier(item["label"], "file label")
        if type(item["path"]) is not str or not Path(item["path"]).is_absolute():
            raise ValueError("bound paths must be absolute")
        _integer(item["size"], "file size", minimum=0)
        if type(item["sha256"]) is not str or not _DIGEST.fullmatch(item["sha256"]):
            raise ValueError("invalid bound file digest")
        if label in labels or item["path"] in paths:
            raise ValueError("duplicate file binding")
        labels.add(label)
        paths.add(item["path"])
    if files != sorted(files, key=lambda item: item["label"]):
        raise ValueError("file bindings are not canonical")
    return _copy(manifest)


def validate_manifest(manifest: dict, *, current_config=None, expected_sha256=None) -> dict:
    """Validate the plan and rehash bound files immediately before each run/call.

    Pass the actual runner configuration and original externally retained digest
    to detect config drift and replacement by another valid plan. Missing files,
    content drift and symlink substitution fail closed. Returns a detached copy.
    This check alone neither authorizes model calls nor locks files for a run.
    """
    frozen = _validated_structure(manifest)
    if expected_sha256 is not None:
        if (type(expected_sha256) is not str or not _DIGEST.fullmatch(expected_sha256)
                or frozen["manifest_sha256"] != expected_sha256):
            raise ValueError("unexpected manifest digest")
    if current_config is not None and configuration_hash(current_config) != frozen["config_sha256"]:
        raise ValueError("runtime_configuration_drift")
    for item in frozen["files"]:
        if _file_receipt(item["path"]) != {key: item[key] for key in ("path", "size", "sha256")}:
            raise ValueError(f"bound_file_drift: {item['label']}")
    return frozen


def write_manifest(path, manifest: dict) -> None:
    """Validate and exclusively create a private plan; never overwrite any path."""
    frozen = validate_manifest(manifest)
    path = _path(path)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(canonical_bytes(frozen) + b"\n")
        stream.flush()
        os.fsync(stream.fileno())


def _unique_pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON object key")
        value[key] = item
    return value


def load_manifest(path, *, current_config=None, expected_sha256=None) -> dict:
    """Load a private plan and perform the same checks as ``validate_manifest``."""
    path = _path(path)
    if not path.is_file():
        raise ValueError("manifest must be a regular file")
    value = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique_pairs)
    return validate_manifest(value, current_config=current_config, expected_sha256=expected_sha256)


def agent_task(manifest: dict, task_id: str) -> dict:
    """Return only id/repo/base_commit/problem_statement, with no file IO.

    Evaluator paths, hidden metadata, grouping, exposure and policy configuration
    never enter this projection. The operator must supply truthful public text;
    field allowlisting cannot detect an answer deliberately embedded in an Issue.
    The runner must separately call ``validate_manifest`` before execution.
    """
    frozen = _validated_structure(manifest)
    _identifier(task_id, "task id")
    for task in frozen["tasks"]:
        if task["id"] == task_id:
            return {"id": task_id, **_copy(task["public"])}
    raise ValueError("unknown task id")
