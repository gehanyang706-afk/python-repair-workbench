"""Read-only provenance audit for diagnostic sessions; never executes repository code."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from pathlib import Path


class AuditError(ValueError):
    pass


def _require(condition, reason):
    if not condition:
        raise AuditError(reason)


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        _require(key not in result, "duplicate_json_key")
        result[key] = value
    return result


def _json(text):
    def invalid_constant(value):
        raise AuditError("nonfinite_json_number")
    return json.loads(text, object_pairs_hook=_pairs, parse_constant=invalid_constant)


def _load(path):
    _require(path.is_file() and not path.is_symlink(), "missing_or_symlinked_artifact")
    _require(path.stat().st_size <= 20 * 1024 * 1024, "oversized_json_artifact")
    return _json(path.read_text(encoding="utf-8"))


def _inside(root, value):
    _require(isinstance(value, str) and 0 < len(value) <= 4096, "invalid_artifact_path")
    path = Path(value)
    _require(".." not in path.parts, "artifact_path_traversal")
    path = path if path.is_absolute() else root / path
    resolved = path.resolve()
    _require(resolved == root or root in resolved.parents, "artifact_outside_run")
    relative = path.relative_to(root)
    cursor = root
    for part in relative.parts:
        cursor = cursor / part
        _require(not cursor.is_symlink(), "symlinked_artifact_path")
    return resolved


def _hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _integer(value):
    return type(value) is int and value >= 0


def audit_run(run_directory, provenance=None):
    """Validate cross-artifact consistency and return a bounded machine-readable review.

    Hashes are an inventory of the files observed now, not a signature or an
    authenticated historical root. A coherent rewrite of every artifact cannot
    be detected without separately retained trusted hashes.
    """
    root = Path(run_directory).resolve(strict=True)
    _require(root.is_dir(), "run_directory_required")
    artifacts = {}

    def artifact(path):
        path = _inside(root, str(path))
        _require(path.is_file(), "missing_artifact")
        artifacts[path.relative_to(root).as_posix()] = _hash(path)
        return path

    def load(path):
        return _load(artifact(path))

    result = load(root / "result.json")
    _require(isinstance(result, dict) and result.get("task_type") == "performance_optimization",
             "not_a_diagnostic_session")
    if (root / "model_response.txt").exists():
        artifact(root / "model_response.txt")
    events_path = artifact(root / "events.jsonl")
    _require(events_path.stat().st_size <= 20 * 1024 * 1024, "oversized_event_log")
    events = [_json(line) for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    _require(events and len(events) <= 20000 and all(isinstance(item, dict) for item in events),
             "invalid_event_log")
    previous_time = -1
    for event in events:
        elapsed = event.get("elapsed_seconds")
        _require(type(elapsed) in {int, float} and math.isfinite(elapsed) and elapsed >= previous_time,
                 "event_time_order_changed")
        previous_time = elapsed
    _require(events[0].get("event") == "session_started"
             and events[0].get("baseline_id") == result.get("baseline_id"), "session_baseline_mismatch")

    evidence = {}
    directory = _inside(root, "evidence")
    _require(directory.is_dir(), "missing_evidence_directory")
    paths = sorted(directory.iterdir())
    _require(len(paths) <= 512, "excessive_evidence_files")
    for path in paths:
        _require(re.fullmatch(r"E\d{4}\.json", path.name), "invalid_evidence_filename")
        item = load(path)
        ref = path.stem
        _require(isinstance(item, dict) and item.get("evidence_ref") == ref, "evidence_identity_mismatch")
        _require(item.get("trust") == "tool_observation", "evidence_trust_changed")
        expected = _canonical({key: value for key, value in item.items() if key != "sha256"})
        _require(item.get("sha256") == expected, "evidence_sha256_mismatch")
        evidence[ref] = item
    index = result.get("evidence_index")
    _require(isinstance(index, list) and all(isinstance(item, dict) for item in index), "invalid_evidence_index")
    _require(len(index) == len(evidence) and len({item.get("evidence_ref") for item in index}) == len(index),
             "evidence_index_count_mismatch")
    for item in index:
        _require(item.get("evidence_ref") in evidence, "unknown_index_reference")
        actual = evidence[item["evidence_ref"]]
        _require(item == {key: actual[key] for key in ("evidence_ref", "kind", "snapshot_id", "sha256")},
                 "evidence_index_mismatch")

    registered, hypotheses, revisions = set(), {}, 0
    containers, diagnostic_starts, diagnostic_finishes, reuse = {}, [], [], []
    model_starts, model_finishes, model_failures, tools = {}, {}, {}, []
    attempts_started, attempts_finished = {}, {}
    conclusions = []
    for position, event in enumerate(events):
        kind = event.get("event")
        if kind == "evidence_registered":
            ref = event.get("evidence_ref")
            _require(ref in evidence and ref not in registered, "duplicate_or_unknown_evidence_event")
            _require(all(event.get(key) == evidence[ref].get(key) for key in
                         ("kind", "snapshot_id", "sha256")), "evidence_event_hash_mismatch")
            registered.add(ref)
            if evidence[ref]["kind"] == "source_excerpt":
                observation = evidence[ref]["observation"]
                _require(any(prior.get("event") == "source_read" and all(
                    prior.get(key) == observation.get(key) for key in ("path", "start_line", "content_sha256"))
                    for prior in events[:position]), "source_evidence_not_observed")
        elif kind == "hypothesis_updated":
            identity = event.get("hypothesis_id")
            _require(isinstance(identity, str) and re.fullmatch(r"H\d{3}", identity), "invalid_hypothesis_id")
            prior = hypotheses.get(identity)
            _require(event.get("revision") == (prior["revision"] + 1 if prior else 1),
                     "hypothesis_revision_gap")
            refs = event.get("evidence_refs")
            _require(isinstance(refs, list) and all(ref in registered for ref in refs),
                     "hypothesis_unknown_or_future_evidence")
            _require(event.get("status") in {"open", "supported", "rejected"}
                     and (event["status"] == "open" or refs), "hypothesis_status_without_evidence")
            _require(event.get("trust") == "model_judgement_not_verified_fact", "hypothesis_trust_changed")
            hypotheses[identity] = {key: value for key, value in event.items() if key not in {"event", "elapsed_seconds"}}
            revisions += 1
        elif kind == "diagnostic_container":
            name, state = event.get("name"), event.get("state")
            _require(isinstance(name, str) and state in {"reserved", "cleaned"}, "invalid_container_event")
            if state == "reserved":
                _require(name not in containers, "duplicate_container_reservation")
            else:
                _require(containers.get(name) == "reserved", "container_cleanup_without_reservation")
            containers[name] = state
        elif kind == "diagnostic_started":
            _require(event.get("run") == len(diagnostic_starts) + 1, "diagnostic_run_sequence_mismatch")
            diagnostic_starts.append(event)
        elif kind == "diagnostic_finished":
            _require(event.get("evidence_ref") in registered, "diagnostic_finish_without_evidence")
            diagnostic_finishes.append(event)
        elif kind == "diagnostic_reused":
            ref = event.get("evidence_ref")
            _require(ref in registered and evidence[ref]["kind"] == "diagnostic_probe", "reuse_unknown_evidence")
            observation = evidence[ref]["observation"]
            _require(observation.get("probe") == event.get("probe") and observation.get("status") == "ok"
                     and observation.get("cleanup_ok") is True, "reuse_failed_or_wrong_probe")
            reuse.append(event)
        elif kind in {"model_started", "model_finished", "model_failed"}:
            call = event.get("call")
            _require(_integer(call) and call > 0, "invalid_model_call")
            if kind == "model_started":
                _require(call == len(model_starts) + 1, "model_call_sequence_mismatch")
                model_starts[call] = event
            else:
                _require(call in model_starts and call not in model_finishes and call not in model_failures,
                         "duplicate_or_unstarted_model_receipt")
                (model_finishes if kind == "model_finished" else model_failures)[call] = event
        elif kind == "tool_started":
            _require(event.get("call") == len(tools) + 1, "tool_call_sequence_mismatch")
            tools.append(event)
        elif kind in {"attempt_started", "attempt_finished"}:
            number = event.get("number")
            if kind == "attempt_started":
                _require(number == len(attempts_started) + 1, "candidate_sequence_mismatch")
                attempts_started[number] = event
            else:
                _require(number in attempts_started and number not in attempts_finished, "candidate_finish_mismatch")
                attempts_finished[number] = event
        elif kind == "diagnosis_concluded":
            _require(event.get("evidence_refs") and all(ref in registered for ref in event["evidence_refs"]),
                     "conclusion_unknown_evidence")
            conclusions.append({key: value for key, value in event.items() if key not in {"event", "elapsed_seconds"}})
    _require(registered == set(evidence), "unregistered_evidence_file")
    _require(hypotheses == result.get("hypotheses"), "final_hypothesis_table_mismatch")
    _require(all(state == "cleaned" for state in containers.values()), "container_cleanup_unconfirmed")
    _require(result.get("tool_calls") == len(tools), "tool_call_count_mismatch")
    _require(result.get("diagnostic_probe_runs") == len(diagnostic_starts) == len(diagnostic_finishes),
             "incomplete_diagnostic_runs")
    if conclusions:
        _require(len(conclusions) == 1 and result.get("final_decision") == conclusions[0], "final_conclusion_mismatch")
    else:
        _require(result.get("final_decision") is None, "unobserved_final_conclusion")

    probe_paths, mcp_control_paths, mcp_report_ids = set(), set(), set()

    def audit_mcp(directory, report):
        control = _inside(root, str(directory.with_name(directory.name + "-mcp")))
        _require(control.is_dir(), "missing_mcp_control_directory")
        allowed = {"config.json", "transport.json", "containers.jsonl", "cancel"}
        for path in control.iterdir():
            _require(path.name in allowed and path.is_file() and not path.is_symlink(),
                     "unexpected_mcp_control_artifact")
        config, transport = (load(control / name) for name in ("config.json", "transport.json"))
        _require(isinstance(config, dict) and set(config) == {
            "schema_version", "snapshot", "snapshot_id", "destination", "image", "workload",
            "timeout_seconds", "cancel_file", "events_file", "report_id"}, "invalid_mcp_config")
        _require(type(config.get("schema_version")) is int and config["schema_version"] == 1
                 and config.get("snapshot_id") == report.get("snapshot_id")
                 and config.get("workload") == report.get("workload_id")
                 and config.get("destination") == str(directory)
                 and config.get("events_file") == str(control / "containers.jsonl")
                 and config.get("cancel_file") == str(control / "cancel"), "mcp_config_binding_mismatch")
        image, timeout, snapshot = (config.get(key) for key in ("image", "timeout_seconds", "snapshot"))
        _require(isinstance(image, str) and re.fullmatch(r"sha256:[a-f0-9]{64}", image)
                 and (report.get("image_id") == image or report.get("status") != "ok" and report.get("image_id") is None),
                 "mcp_image_binding_mismatch")
        _require(type(timeout) in {int, float} and math.isfinite(timeout) and 0 < timeout <= 120
                 and isinstance(snapshot, str) and Path(snapshot).is_absolute() and ".." not in Path(snapshot).parts,
                 "invalid_mcp_execution_config")
        report_id = config.get("report_id")
        _require(isinstance(report_id, str) and re.fullmatch(r"probe-[a-f0-9]{32}", report_id),
                 "invalid_mcp_report_id")
        _require(report_id not in mcp_report_ids, "reused_mcp_report_id")
        mcp_report_ids.add(report_id)
        _require(isinstance(transport, dict) and transport.get("protocol") == "mcp"
                 and transport.get("transport") == "stdio" and transport.get("report_id") == report_id
                 and transport.get("status") == "completed" and transport.get("real_model_calls") == 0
                 and transport.get("journal_valid") is True and transport.get("cleanup_ok") is True,
                 "mcp_transport_unconfirmed")
        catalog = transport.get("tools")
        _require(isinstance(catalog, list) and len(catalog) == 3
                 and all(isinstance(tool, dict) for tool in catalog), "mcp_tool_catalog_mismatch")
        tools_by_name = {tool.get("name"): tool.get("inputSchema") for tool in catalog}
        parameters = {"list_diagnostics": set(), "run_probe": {"probe"}, "read_report": {"report_id"}}
        _require(set(tools_by_name) == set(parameters), "mcp_tool_catalog_mismatch")
        for name, keys in parameters.items():
            schema = tools_by_name[name]
            _require(isinstance(schema, dict) and schema.get("type") == "object"
                     and isinstance(schema.get("properties"), dict)
                     and set(schema["properties"]) == keys and set(schema.get("required", [])) == keys,
                     "mcp_tool_catalog_mismatch")
        _require(tools_by_name["run_probe"]["properties"]["probe"].get("type") == "string"
                 and tools_by_name["run_probe"]["properties"]["probe"].get("enum") == ["cpu", "memory", "benchmark"]
                 and tools_by_name["read_report"]["properties"]["report_id"].get("type") == "string",
                 "mcp_tool_catalog_mismatch")
        journal = artifact(control / "containers.jsonl")
        _require(journal.stat().st_size <= 128 * 1024, "oversized_mcp_journal")
        text = journal.read_text(encoding="utf-8")
        try:
            receipts = [_json(line) for line in text.splitlines() if line.strip()]
        except (ValueError, TypeError):
            raise AuditError("mcp_journal_incomplete") from None
        _require(receipts == transport.get("server_events"), "mcp_journal_archive_mismatch")
        _require(len(receipts) >= 2 and receipts[0] == {"event": "probe_started", "report_id": report_id}
                 and receipts[-1] == {"event": "probe_finished", "report_id": report_id, "status": report.get("status")},
                 "mcp_probe_lifecycle_mismatch")
        states = {}
        for receipt in receipts[1:-1]:
            _require(isinstance(receipt, dict) and set(receipt) == {"name", "state"}, "invalid_mcp_container_receipt")
            name, state = receipt["name"], receipt["state"]
            _require(isinstance(name, str) and re.fullmatch(r"repair-probe-[a-f0-9]{16}", name)
                     and (state == "reserved" and name not in states
                          or state == "cleaned" and states.get(name) == "reserved"), "invalid_mcp_container_lifecycle")
            states[name] = state
        _require(states == transport.get("containers") and len(states) <= 1
                 and all(state == "cleaned" for state in states.values()), "mcp_container_cleanup_mismatch")
        parent_receipts = [{"name": event["name"], "state": event["state"]} for event in events
                           if event.get("event") == "diagnostic_container" and event.get("name") in states]
        _require(parent_receipts == receipts[1:-1], "mcp_parent_container_receipt_mismatch")
        command = report.get("command", [])
        if states or report.get("status") == "ok":
            _require(isinstance(command, list) and command.count("--name") == 1
                     and command.index("--name") + 1 < len(command)
                     and {command[command.index("--name") + 1]} == set(states), "mcp_probe_container_binding_mismatch")
            mount = f"type=bind,src={snapshot}/workspace,dst=/workspace,readonly"
            mounts = [command[index + 1] for index, value in enumerate(command[:-1]) if value == "--mount"]
            _require(mount in mounts, "mcp_snapshot_mount_binding_mismatch")
        if (control / "cancel").exists():
            _require(artifact(control / "cancel").stat().st_size == 0, "invalid_mcp_cancel_marker")
        mcp_control_paths.add(control)

    def audit_probe(directory, expected_snapshot, embedded=None):
        directory = _inside(root, str(directory))
        _require(directory not in probe_paths, "reused_physical_probe_directory")
        probe_paths.add(directory)
        for path in directory.iterdir():
            _require(path.name in {"result.json", "probe.json", "probe.log", "cpu-small.pstats", "cpu-large.pstats"}
                     and path.is_file() and not path.is_symlink(), "unexpected_probe_artifact")
        report = load(directory / "result.json")
        _require(isinstance(report, dict) and report.get("snapshot_id") == expected_snapshot,
                 "probe_snapshot_mismatch")
        _require(report.get("cleanup_ok") is True, "probe_cleanup_unconfirmed")
        if embedded is not None:
            _require(report == embedded, "embedded_probe_report_mismatch")
        worker_path = directory / "probe.json"
        if report.get("status") == "ok":
            worker = load(worker_path)
            _require(all(report.get(key) == worker.get(key) for key in
                         ("workload_id", "workload_fingerprint", "probe", "summary", "samples", "environment")),
                     "raw_probe_report_mismatch")
            _require(report.get("returncode") == 0, "successful_probe_nonzero_exit")
        elif worker_path.exists():
            load(worker_path)
        for name in ("probe.log", "cpu-small.pstats", "cpu-large.pstats"):
            if (directory / name).exists():
                artifact(directory / name)
        return report

    for start, finish in zip(diagnostic_starts, diagnostic_finishes):
        ref = finish["evidence_ref"]
        item = evidence[ref]
        _require(item["kind"] == "diagnostic_probe" and item["snapshot_id"] == start.get("snapshot_id"),
                 "diagnostic_evidence_snapshot_mismatch")
        observation = item["observation"]
        report = audit_probe(_inside(root, observation["report"]), item["snapshot_id"])
        transport = observation.get("transport", "local")
        _require(transport in {"local", "mcp"} and transport == result.get("diagnostic_transport", "local"),
                 "diagnostic_transport_mismatch")
        if transport == "mcp":
            audit_mcp(_inside(root, observation["report"]), report)
        _require(all(observation.get(key) == report.get(key) for key in ("probe", "status", "summary", "cleanup_ok")),
                 "diagnostic_evidence_report_mismatch")
        if 'report_sha256' in observation:
            _require(observation['report_sha256'] == _hash(_inside(root, observation['report']) / 'result.json'),
                     'diagnostic_raw_report_hash_changed')
        _require(start.get("probe") == report.get("probe") == finish.get("probe")
                 and all(finish.get(key) == report.get(key) for key in ("status", "cleanup_ok")),
                 "diagnostic_event_report_mismatch")

    def snapshot(directory):
        directory = _inside(root, str(directory))
        manifest = load(directory / "manifest.json")
        _require(isinstance(manifest, dict) and isinstance(manifest.get("files"), list), "invalid_snapshot_manifest")
        expected = {}
        for item in manifest["files"]:
            name = item.get("path")
            _require(isinstance(name, str) and not Path(name).is_absolute()
                     and not any(part in {".git", ".venv", "node_modules"} or part.startswith(".env")
                                 for part in Path(name).parts), "forbidden_snapshot_path")
            _require(name not in expected, "duplicate_snapshot_file")
            path = _inside(root, str(directory / "workspace" / name))
            _require(path.is_file() and _hash(path) == item.get("sha256"), "snapshot_content_changed")
            expected[name] = item["sha256"]
        actual = {path.relative_to(directory / "workspace").as_posix()
                  for path in (directory / "workspace").rglob("*") if path.is_file()}
        _require(actual == set(expected), "snapshot_file_set_changed")
        return _canonical({"commit": manifest["commit"], "files": sorted(manifest["files"], key=lambda item: item["path"])})

    attempts = result.get("attempts")
    _require(isinstance(attempts, list) and len(attempts) == len(attempts_started) == len(attempts_finished),
             "incomplete_candidate_events")
    verification_probes = 0
    for number, attempt in enumerate(attempts, 1):
        _require(attempt.get("number") == number and all(attempt.get(key) == attempts_finished[number].get(key)
                                                        for key in ("status", "reason")), "candidate_event_status_mismatch")
        if attempt.get("phase") != "verification" or "evidence_ref" not in attempt:
            _require(attempt.get("status") != "passed", "passed_candidate_without_verification")
            continue
        ref = attempt["evidence_ref"]
        _require(ref in evidence and evidence[ref]["kind"] == "candidate_verification", "unknown_candidate_evidence")
        item, observation = evidence[ref], evidence[ref]["observation"]
        raw_path = _inside(root, observation["report"])
        _require(raw_path == root / f"attempt-{number:02d}/verification/performance-verification.json",
                 "candidate_report_path_mismatch")
        raw = load(raw_path)
        _require(all(key in attempt and attempt[key] == value for key, value in raw.items()),
                 "candidate_raw_result_mismatch")
        _require(all(key == "report" or raw.get(key) == value for key, value in observation.items()),
                 "candidate_evidence_report_mismatch")
        submitted = snapshot(root / f"attempt-{number:02d}/candidate")
        rebuilt = snapshot(raw_path.parent)
        _require(raw.get("base_snapshot_id") == result.get("baseline_id")
                 and raw.get("submitted_snapshot_id") == item["snapshot_id"] == submitted
                 and raw.get("candidate_snapshot_id") == rebuilt, "candidate_snapshot_mismatch")
        for key in ("baseline_test_result", "candidate_test_result"):
            _require(raw.get(key, {}).get("cleanup_ok") is True, "candidate_test_cleanup_unconfirmed")
        _require(raw.get("candidate_test_result") == load(raw_path.parent / "reports/result.json"),
                 "candidate_raw_test_result_mismatch")
        for directory, names in ((raw_path.parent, ("patch.diff", "reports/pytest.xml", "reports/pytest.log")),
                                 (root / f"attempt-{number:02d}/candidate", ("patch.diff",))):
            for name in names:
                if (directory / name).exists():
                    artifact(directory / name)
        measurements = raw.get("measurements", [])
        _require(isinstance(measurements, list), "invalid_measurements")
        measurement_root = _inside(root, raw.get("measurement_directory"))
        _require(measurement_root == raw_path.parent.with_name("verification-measurements"),
                 "measurement_directory_mismatch")
        seen = set()
        for measurement in measurements:
            round_number, probe, role = (measurement.get(key) for key in ("round", "probe", "role"))
            _require(round_number in {1, 2} and probe in {"benchmark", "memory"} and role in {"baseline", "candidate"},
                     "invalid_measurement_identity")
            identity = (round_number, probe, role)
            _require(identity not in seen, "duplicate_measurement")
            seen.add(identity)
            expected_snapshot = result["baseline_id"] if role == "baseline" else rebuilt
            report = audit_probe(measurement_root / f"round-{round_number}-{probe}-{role}",
                                 expected_snapshot, measurement.get("report"))
            _require(report.get("probe") == probe, "measurement_probe_mismatch")
        verification_probes += len(measurements)
        _require(raw.get("status") != "passed" or len(seen) == 8, "passed_candidate_incomplete_measurements")
    physical = set()
    parents = [root / "diagnostics", *(root / f"attempt-{number:02d}/verification-measurements"
                                       for number in range(1, len(attempts) + 1))]
    for directory in parents:
        if directory.exists():
            directory = _inside(root, str(directory))
            for path in directory.iterdir():
                if path.is_dir() or path.is_symlink():
                    physical.add(_inside(root, str(path)))
    _require(physical == probe_paths | mcp_control_paths, "unreferenced_or_missing_physical_probe")
    passed = next((attempt for attempt in attempts if attempt.get("status") == "passed"), None)
    expected_status = "passed" if passed else attempts[-1]["status"] if attempts else "inconclusive"
    expected_reason = passed["reason"] if passed else attempts[-1].get("reason", "attempt_in_progress") if attempts else "no_candidate"
    _require(result.get("status") == expected_status and result.get("reason") == expected_reason,
             "overall_result_status_mismatch")

    _require(result.get("model_calls") == len(model_starts), "model_call_count_mismatch")
    _require(all(type(event.get("usage_reported")) is bool for event in model_finishes.values()),
             "invalid_usage_reporting_flag")
    reported = [event for event in model_finishes.values() if event.get("usage_reported") is True]
    _require(all(_integer(event.get(key)) for event in reported for key in ("input_tokens", "output_tokens")),
             "invalid_reported_model_usage")
    _require(all(event.get("input_tokens") is None and event.get("output_tokens") is None
                 for event in model_finishes.values() if event.get("usage_reported") is not True),
             "unreported_usage_must_be_unknown")
    token_usage = result.get("token_usage", {})
    known_input, known_output = (sum(event[key] for event in reported) for key in ("input_tokens", "output_tokens"))
    missing = len(model_starts) - len(reported)
    _require(token_usage == {"input_tokens": known_input, "output_tokens": known_output, "reports": len(reported)}
             and result.get("usage_complete") == (missing == 0) and result.get("usage_missing_calls") == missing,
             "model_usage_accounting_mismatch")

    report_queries = [event for event in events if event.get('event') == 'diagnostic_report_queried']
    if report_queries:
        from repair_workbench.diagnostic_reports import ReportStore, freeze_probe
        from repair_workbench.tool_runtime import digest
        for query in report_queries:
            ref = query.get('evidence_ref')
            _require(ref in evidence, 'query_unknown_evidence')
            path = artifact(_inside(root, query.get('bundle')))
            _require(path.parent == root / 'report_queries', 'query_bundle_outside_registry')
            try:
                store = ReportStore(path, query.get('bundle_sha256'))
                frozen = freeze_probe(root, evidence[ref])
                _require(store.reports == {frozen['report_id']: frozen}, 'query_bundle_evidence_mismatch')
                page = store.query(**query['arguments'])
            except (ValueError, KeyError, TypeError) as exc:
                raise AuditError('query_bundle_or_arguments_invalid') from exc
            _require(digest(page) == query.get('result_sha256'), 'query_result_digest_changed')
            _require(len(json.dumps(page, ensure_ascii=False).encode()) == query.get('response_bytes'),
                     'query_response_size_changed')
            _require(query.get('transport') == result.get('diagnostic_transport', 'local'),
                     'query_transport_changed')

    mode = "provider_not_attested"
    real_calls = None
    provenance_sha256 = None
    if provenance is not None:
        provenance = Path(provenance).resolve(strict=True)
        meta = _load(provenance)
        provenance_sha256 = _hash(provenance)
        _require(isinstance(meta, dict), "invalid_provenance")
        if "result" in meta:
            _require(meta["result"] == result, "provenance_result_mismatch")
        declared_mode = meta.get("mode", "")
        if isinstance(declared_mode, str) and "scripted" in declared_mode:
            mode = "scripted_known_candidate" if "known_candidate" in declared_mode else "scripted"
            _require(meta.get("real_model_calls") == 0, "scripted_provenance_real_call_conflict")
            real_calls = 0
        elif any(event.get("event") == "model_configured" for event in events):
            mode = "provider_configured_not_independently_attested"
    elif any(event.get("event") == "model_configured" for event in events):
        mode = "provider_configured_not_independently_attested"

    return {"audit_status": "passed", "schema_version": 1,
            "run_status": result["status"], "run_reason": result["reason"],
            "strategy": result.get("diagnostic_strategy"), "mode": mode,
            "real_model_calls_declared": real_calls,
            "real_llm_performance_claim_allowed": False,
            "usage": {"model_wrapper_calls": len(model_starts), "reported_calls": len(reported),
                      "missing_usage_calls": missing,
                      "input_tokens_for_reported_calls": known_input if reported else None,
                      "output_tokens_for_reported_calls": known_output if reported else None,
                      "all_calls_total_tokens": known_input + known_output if not missing else None},
            "counts": {"evidence": len(evidence), "hypotheses": len(hypotheses),
                       "hypothesis_revisions": revisions, "exploratory_probe_runs": len(diagnostic_starts),
                       "reused_observations": len(reuse), "verification_probe_runs": verification_probes,
                       "physical_probe_runs": len(probe_paths), "mcp_jobs": len(mcp_control_paths), "candidates": len(attempts),
                       "containers_confirmed_cleaned": len(containers), "report_queries": len(report_queries)},
            "artifact_sha256": dict(sorted(artifacts.items())), "provenance_sha256": provenance_sha256,
            "limits": ["Checks provenance and cross-file consistency; does not re-execute code or independently score repairs.",
                       "Hashes are observed now, without an authenticated historical root; coherent rewrites are not detectable.",
                       "Model identity and actual external billing are not independently attested.",
                       "Scripted known candidates establish mechanism behavior, not autonomous LLM quality."]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_directory", type=Path)
    parser.add_argument("--provenance", type=Path, help="Optional explicitly selected acceptance metadata")
    args = parser.parse_args(argv)
    try:
        report = audit_run(args.run_directory, args.provenance)
    except (AuditError, OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        report = {"audit_status": "failed", "reason": str(exc) if isinstance(exc, AuditError) else type(exc).__name__}
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
