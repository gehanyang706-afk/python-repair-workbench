"""Versioned, application-seeded diagnostic skills for Deep Agents StateBackend.

The graph must expose only read_file for virtual files. This module provides no
Agent-facing writer, registration, or automatic promotion path. Skill sources
alone do not populate StateBackend: initial_skill_files() must be supplied in
the invocation's files state.
"""
from __future__ import annotations

import hashlib
import json
from importlib.resources import files

from deepagents.backends.protocol import FileData
from deepagents.backends.utils import create_file_data


SKILL_SOURCES = ["/skills/"]
SKILL_BUNDLE_VERSION = "diagnostic-strategies-v1"
SKILL_VERSION = "0.1.0-candidate"
_ASSET_TIMESTAMP = "2026-10-31T00:00:00+00:00"
_SKILLS = (
    ("cpu-hotspot", "c131fb5708ce5a63b4ea56dffa382e048c620298afb3c5d827ce3f15cd6438f3",
     "Computational cost needs discrimination using function time and call counts; not a preset cause."),
    ("python-allocation", "229cf0ab92e42c4bc722c4bbf94a5d880f10f1da6db6377d439a69db6eae101a",
     "A Python allocation goal or existing evidence makes traced allocation worth investigating."),
    ("behavior-preservation", "ac58c0f9609bbd07f9a9766bd6d1d219c027cb069355ddbace2a75fb5138faf5",
     "Before approved source changes, or when interpreting semantic validation failures."),
)


def _contents():
    package = files("repair_workbench")
    result = {}
    for name, expected_hash, _ in _SKILLS:
        raw = package.joinpath("skills", name, "SKILL.md").read_bytes()
        if len(raw) > 12000 or hashlib.sha256(raw).hexdigest() != expected_hash:
            raise ValueError(f"trusted_skill_content_mismatch:{name}")
        result[f"/skills/{name}/SKILL.md"] = raw.decode("utf-8")
    return result


def initial_skill_files() -> dict[str, FileData]:
    """Return fresh FileData objects for graph.invoke({'files': ...}).

    Uses the installed 0.7.20 create_file_data API. Stable asset timestamps are
    metadata for a fixed bundle, not evidence of when the Agent loaded a file.
    """
    seeded = {}
    for path, content in _contents().items():
        data = create_file_data(content, created_at=_ASSET_TIMESTAMP)
        data["modified_at"] = _ASSET_TIMESTAMP
        seeded[path] = data
    return seeded


def trusted_skill_manifest() -> dict:
    """Describe candidate assets and verify their bytes before publishing hashes."""
    _contents()
    manifest = {
        "bundle_version": SKILL_BUNDLE_VERSION,
        "sources": list(SKILL_SOURCES),
        "validation_status": "offline_wiring_only",
        "generalization_verified": False,
        "write_policy": "application_seed_only; graph excludes virtual filesystem write tools",
        "promotion_policy": "no_agent_registration_or_automatic_promotion",
        "skills": [{"name": name, "version": SKILL_VERSION,
                    "path": f"/skills/{name}/SKILL.md", "sha256": digest,
                    "applicability": applicability}
                   for name, digest, applicability in _SKILLS],
    }
    manifest["sha256"] = hashlib.sha256(json.dumps(manifest, sort_keys=True,
                                                  ensure_ascii=False).encode()).hexdigest()
    return manifest
