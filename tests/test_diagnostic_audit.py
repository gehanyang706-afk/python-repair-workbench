"""Tamper detection for read-only diagnostic provenance audits; no model or Docker."""
import copy
import hashlib
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from audit_diagnostic_run import AuditError, audit_run, main


def canonical(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


class DiagnosticAuditTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.parent = Path(temp.name).resolve()
        self.root = self.parent / "agent"
        self.root.mkdir()
        self.events = []
        self.evidence = []
        self.base_id = "a" * 64
        self.emit("session_started", baseline_id=self.base_id, allowed_paths=["map_exporter.py"])
        baseline = self.add_evidence("baseline_tests", {"status": "tests_passed"}, self.base_id)
        self.emit("hypothesis_updated", hypothesis_id="H001", claim="Possible repeated work", status="open",
                  evidence_refs=[baseline["evidence_ref"]], next_action="CPU probe",
                  trust="model_judgement_not_verified_fact", revision=1)
        self.emit("diagnostic_started", probe="cpu", snapshot_id=self.base_id, run=1)
        self.emit("diagnostic_container", name="probe-1", state="reserved")
        self.emit("diagnostic_container", name="probe-1", state="cleaned")
        probe = self.make_probe(self.root / "diagnostics/probe-01", "cpu", self.base_id)
        observed = self.add_evidence("diagnostic_probe", {
            **{key: probe[key] for key in ("probe", "status", "cleanup_ok", "summary")},
            "report": "diagnostics/probe-01", "hypothesis_id": "H001"}, self.base_id)
        self.emit("diagnostic_finished", evidence_ref=observed["evidence_ref"], probe="cpu", status="ok", cleanup_ok=True)
        self.hypothesis = {"hypothesis_id": "H001", "claim": "CPU evidence observed", "status": "supported",
                           "evidence_refs": [observed["evidence_ref"]], "next_action": "candidate",
                           "trust": "model_judgement_not_verified_fact", "revision": 2}
        self.emit("hypothesis_updated", **self.hypothesis)
        self.emit("model_started", call=1)
        self.emit("model_finished", call=1, usage_reported=False, input_tokens=None, output_tokens=None)
        self.emit("attempt_started", number=1)
        self.emit("diagnostic_container", name="test-1", state="reserved")
        self.emit("diagnostic_container", name="test-1", state="cleaned")
        candidate = self.root / "attempt-01/candidate"
        verification = self.root / "attempt-01/verification"
        candidate_id = self.make_snapshot(candidate)
        self.make_snapshot(verification)
        self.verification_path = verification / "performance-verification.json"
        tests = {"status": "tests_passed", "cleanup_ok": True, "snapshot_id": candidate_id}
        write(verification / "reports/result.json", tests)
        measurements = []
        measurement_root = verification.with_name("verification-measurements")
        for round_number, order in ((1, ("baseline", "candidate")), (2, ("candidate", "baseline"))):
            for kind in ("benchmark", "memory"):
                for role in order:
                    report = self.make_probe(measurement_root / f"round-{round_number}-{kind}-{role}",
                                             kind, self.base_id if role == "baseline" else candidate_id)
                    measurements.append({"round": round_number, "probe": kind, "role": role, "report": report})
        self.raw = {"status": "passed", "reason": "passed_registered_performance_scope",
                    "base_snapshot_id": self.base_id, "submitted_snapshot_id": candidate_id,
                    "candidate_snapshot_id": candidate_id, "measurement_directory": str(measurement_root),
                    "baseline_test_result": {"cleanup_ok": True}, "candidate_test_result": tests,
                    "measurements": measurements, "metrics": {"large": {"speedup": .5}}}
        write(self.verification_path, self.raw)
        verified = self.add_evidence("candidate_verification", {
            "status": self.raw["status"], "reason": self.raw["reason"], "metrics": self.raw["metrics"],
            "report": "attempt-01/verification/performance-verification.json"}, candidate_id)
        self.emit("attempt_finished", number=1, status="passed", reason=self.raw["reason"])
        self.emit("agent_finished", stop_reason="agent_finished")
        self.result = {"task_type": "performance_optimization", "baseline_id": self.base_id,
                       "status": "passed", "reason": self.raw["reason"], "state": "completed",
                       "diagnostic_strategy": "adaptive", "diagnostic_probe_runs": 1,
                       "model_calls": 1, "tool_calls": 0,
                       "token_usage": {"input_tokens": 0, "output_tokens": 0, "reports": 0},
                       "usage_complete": False, "usage_missing_calls": 1, "final_decision": None,
                       "hypotheses": {"H001": self.hypothesis},
                       "attempts": [{**self.raw, "number": 1, "phase": "verification", "evidence_ref": verified["evidence_ref"]}],
                       "evidence_index": [{key: item[key] for key in ("evidence_ref", "kind", "snapshot_id", "sha256")}
                                          for item in self.evidence]}
        self.flush()
        self.provenance = self.parent / "acceptance.json"
        write(self.provenance, {"mode": "scripted_known_candidate_real_container", "real_model_calls": 0,
                                "result": self.result})

    def emit(self, event_name, **fields):
        self.events.append({"event": event_name, "elapsed_seconds": len(self.events) / 10, **fields})

    def add_evidence(self, kind, observation, snapshot):
        ref = f"E{len(self.evidence)+1:04d}"
        item = {"evidence_ref": ref, "kind": kind, "snapshot_id": snapshot,
                "observation": observation, "trust": "tool_observation"}
        item["sha256"] = canonical(item)
        self.evidence.append(item)
        write(self.root / "evidence" / f"{ref}.json", item)
        self.emit("evidence_registered", **{key: item[key] for key in ("evidence_ref", "kind", "snapshot_id", "sha256")})
        return item

    def make_snapshot(self, directory):
        content = b"def export_map(objects, resources):\n    return []\n"
        workspace = directory / "workspace"
        workspace.mkdir(parents=True)
        (workspace / "map_exporter.py").write_bytes(content)
        manifest = {"commit": "c" * 40, "base_snapshot_id": self.base_id,
                    "files": [{"path": "map_exporter.py", "sha256": hashlib.sha256(content).hexdigest()}]}
        write(directory / "manifest.json", manifest)
        return canonical({key: manifest[key] for key in ("commit", "files")})

    def make_probe(self, directory, kind, snapshot):
        worker = {"workload_id": "ugc-export-v1", "workload_fingerprint": "f" * 64,
                  "probe": kind, "summary": {"semantic_ok": True},
                  "samples": {} if kind == "cpu" else {"small": [.1], "large": [.2]},
                  "environment": {"machine": "test-machine"}}
        report = {**worker, "status": "ok", "cleanup_ok": True, "snapshot_id": snapshot, "returncode": 0}
        write(directory / "probe.json", worker)
        write(directory / "result.json", report)
        (directory / "probe.log").write_text("")
        return report

    def flush(self):
        write(self.root / "result.json", self.result)
        (self.root / "events.jsonl").write_text("".join(json.dumps(event) + "\n" for event in self.events))

    def assert_bad(self, reason):
        with self.assertRaisesRegex(AuditError, reason):
            audit_run(self.root)

    def make_mcp_archive(self):
        directory = self.root / "diagnostics/probe-01-mcp"
        report_id = "probe-" + "b" * 32
        name = "repair-probe-" + "c" * 16
        image = "sha256:" + "d" * 64
        for event in self.events:
            if event.get("name") == "probe-1":
                event["name"] = name
        report_path = self.root / "diagnostics/probe-01/result.json"
        report = json.loads(report_path.read_text())
        report.update(image_id=image, command=["docker", "run", "--name", name, "--mount",
                      f"type=bind,src={self.parent / 'baseline'}/workspace,dst=/workspace,readonly"])
        write(report_path, report)
        self.result["diagnostic_transport"] = "mcp"
        item = self.evidence[1]
        item["observation"]["transport"] = "mcp"
        item["sha256"] = canonical({key: value for key, value in item.items() if key != "sha256"})
        write(self.root / "evidence/E0002.json", item)
        self.result["evidence_index"][1]["sha256"] = item["sha256"]
        for event in self.events:
            if event.get("event") == "evidence_registered" and event.get("evidence_ref") == "E0002":
                event["sha256"] = item["sha256"]
        write(directory / "config.json", {
            "schema_version": 1, "snapshot": str(self.parent / "baseline"),
            "snapshot_id": self.base_id, "destination": str(report_path.parent),
            "image": image, "workload": "ugc-export-v1", "timeout_seconds": 30,
            "cancel_file": str(directory / "cancel"), "events_file": str(directory / "containers.jsonl"),
            "report_id": report_id})
        receipts = [{"event": "probe_started", "report_id": report_id},
                    {"name": name, "state": "reserved"}, {"name": name, "state": "cleaned"},
                    {"event": "probe_finished", "report_id": report_id, "status": "ok"}]
        (directory / "containers.jsonl").write_text("".join(json.dumps(row) + "\n" for row in receipts))
        schemas = {"list_diagnostics": {},
                   "run_probe": {"probe": {"type": "string", "enum": ["cpu", "memory", "benchmark"]}},
                   "read_report": {"report_id": {"type": "string"}}}
        write(directory / "transport.json", {
            "protocol": "mcp", "sdk_version": "2.3.0", "transport": "stdio", "report_id": report_id,
            "status": "completed", "real_model_calls": 0, "journal_valid": True, "cleanup_ok": True,
            "tools": [{"name": key, "inputSchema": {"type": "object", "properties": value,
                      "required": list(value)}} for key, value in schemas.items()],
            "containers": {name: "cleaned"}, "server_events": receipts})
        self.flush()
        return directory

    def test_mcp_archive_is_audited_without_counting_control_directory_as_a_probe(self):
        self.make_mcp_archive()
        output = audit_run(self.root)
        self.assertEqual(output["counts"]["physical_probe_runs"], 9)
        self.assertEqual(output["counts"]["mcp_jobs"], 1)
        self.assertIn("diagnostics/probe-01-mcp/containers.jsonl", output["artifact_sha256"])

    def test_mcp_control_suffix_does_not_hide_unreported_probe(self):
        self.make_mcp_archive()
        self.make_probe(self.root / "diagnostics/probe-02-mcp", "cpu", self.base_id)
        self.assert_bad("unreferenced_or_missing_physical_probe")

    def test_mcp_archive_cannot_hide_nested_probe_or_extra_file(self):
        directory = self.make_mcp_archive()
        self.make_probe(directory / "hidden", "cpu", self.base_id)
        self.assert_bad("unexpected_mcp_control_artifact")

    def test_mcp_config_is_bound_to_observed_snapshot_and_destination(self):
        directory = self.make_mcp_archive()
        config = json.loads((directory / "config.json").read_text())
        config["destination"] = str(self.root / "diagnostics/probe-02")
        write(directory / "config.json", config)
        self.assert_bad("mcp_config_binding_mismatch")

    def test_mcp_external_snapshot_path_is_bound_without_reading_it(self):
        directory = self.make_mcp_archive()
        config = json.loads((directory / "config.json").read_text())
        config["snapshot"] = str(self.parent / "unrelated")
        write(directory / "config.json", config)
        self.assert_bad("mcp_snapshot_mount_binding_mismatch")

    def test_registered_probe_cannot_hide_nested_unreported_measurement(self):
        self.make_probe(self.root / "diagnostics/probe-01/hidden", "cpu", self.base_id)
        self.assert_bad("unexpected_probe_artifact")

    def test_mcp_receipts_must_match_archive_and_parent_cleanup(self):
        directory = self.make_mcp_archive()
        archive = json.loads((directory / "transport.json").read_text())
        archive["server_events"].pop(2)
        write(directory / "transport.json", archive)
        self.assert_bad("mcp_journal_archive_mismatch")

    def test_mcp_truncated_journal_is_rejected(self):
        directory = self.make_mcp_archive()
        with (directory / "containers.jsonl").open("a") as stream:
            stream.write('{"name":')
        self.assert_bad("mcp_journal_incomplete")

    def test_mcp_tool_catalog_cannot_add_arbitrary_paths(self):
        directory = self.make_mcp_archive()
        archive = json.loads((directory / "transport.json").read_text())
        archive["tools"][1]["inputSchema"]["properties"]["path"] = {"type": "string"}
        write(directory / "transport.json", archive)
        self.assert_bad("mcp_tool_catalog_mismatch")

    def test_mcp_transport_cannot_be_declared_local_to_skip_archive_checks(self):
        self.make_mcp_archive()
        self.result["diagnostic_transport"] = "local"
        self.flush()
        self.assert_bad("diagnostic_transport_mismatch")

    def test_valid_audit_keeps_missing_usage_unknown_and_scripted_claim_separate(self):
        output = audit_run(self.root, self.provenance)
        self.assertEqual(output["mode"], "scripted_known_candidate")
        self.assertFalse(output["real_llm_performance_claim_allowed"])
        self.assertEqual(output["real_model_calls_declared"], 0)
        self.assertIsNone(output["usage"]["all_calls_total_tokens"])
        self.assertIsNone(output["usage"]["input_tokens_for_reported_calls"])
        self.assertEqual(output["counts"]["physical_probe_runs"], 9)
        self.assertEqual(output["counts"]["hypothesis_revisions"], 2)
        self.assertIn("diagnostics/probe-01/probe.json", output["artifact_sha256"])
        without = audit_run(self.root)
        self.assertEqual(without["mode"], "provider_not_attested")
        self.assertIsNone(without["real_model_calls_declared"])

    def test_evidence_content_tampering_is_rejected(self):
        item = copy.deepcopy(self.evidence[0])
        item["observation"]["status"] = "forged"
        write(self.root / "evidence/E0001.json", item)
        self.assert_bad("evidence_sha256_mismatch")

    def test_evidence_event_hash_tampering_is_rejected(self):
        self.events[1]["sha256"] = "0" * 64
        self.flush()
        self.assert_bad("evidence_event_hash_mismatch")

    def test_hypothesis_cannot_reference_future_evidence(self):
        event = next(event for event in self.events if event["event"] == "hypothesis_updated")
        event["evidence_refs"] = ["E0003"]
        self.flush()
        self.assert_bad("hypothesis_unknown_or_future_evidence")

    def test_hypothesis_revision_cannot_skip_history(self):
        events = [event for event in self.events if event["event"] == "hypothesis_updated"]
        events[-1]["revision"] = 4
        self.flush()
        self.assert_bad("hypothesis_revision_gap")

    def test_candidate_raw_verdict_tampering_is_rejected(self):
        self.raw["status"] = "failed"
        write(self.verification_path, self.raw)
        self.assert_bad("candidate_raw_result_mismatch")

    def test_candidate_source_tampering_is_rejected_without_execution(self):
        (self.root / "attempt-01/candidate/workspace/map_exporter.py").write_text("raise RuntimeError('never execute')")
        self.assert_bad("snapshot_content_changed")

    def test_complete_probe_report_tampering_is_rejected(self):
        path = self.root / "attempt-01/verification-measurements/round-1-benchmark-baseline/probe.json"
        payload = json.loads(path.read_text())
        payload["samples"]["large"] = [.0000001]
        write(path, payload)
        self.assert_bad("raw_probe_report_mismatch")

    def test_embedded_probe_report_tampering_is_rejected(self):
        path = self.root / "attempt-01/verification-measurements/round-1-memory-baseline/result.json"
        payload = json.loads(path.read_text())
        payload["samples"]["large"] = [1]
        write(path, payload)
        self.assert_bad("embedded_probe_report_mismatch")

    def test_probe_cleanup_failure_is_not_silently_accepted(self):
        path = self.root / "diagnostics/probe-01/result.json"
        payload = json.loads(path.read_text())
        payload["cleanup_ok"] = False
        write(path, payload)
        self.assert_bad("probe_cleanup_unconfirmed")

    def test_container_cleanup_receipt_cannot_be_omitted(self):
        self.events = [event for event in self.events if not (event["event"] == "diagnostic_container" and event["state"] == "cleaned")]
        self.flush()
        self.assert_bad("container_cleanup_unconfirmed")

    def test_missing_usage_cannot_be_relabelled_zero_cost_complete(self):
        self.result["usage_complete"] = True
        self.result["usage_missing_calls"] = 0
        self.flush()
        self.assert_bad("model_usage_accounting_mismatch")

    def test_observed_usage_is_summed_without_attesting_provider_identity(self):
        event = next(event for event in self.events if event["event"] == "model_finished")
        event.update(usage_reported=True, input_tokens=11, output_tokens=5)
        self.result.update(token_usage={"input_tokens": 11, "output_tokens": 5, "reports": 1},
                           usage_complete=True, usage_missing_calls=0)
        self.flush()
        output = audit_run(self.root)
        self.assertEqual(output["usage"]["all_calls_total_tokens"], 16)
        self.assertIsNone(output["real_model_calls_declared"])

    def test_scripted_provenance_cannot_claim_real_api_calls(self):
        write(self.provenance, {"mode": "scripted", "real_model_calls": 1})
        with self.assertRaisesRegex(AuditError, "scripted_provenance_real_call_conflict"):
            audit_run(self.root, self.provenance)

    def test_raw_probe_symlink_cannot_escape_run_directory(self):
        path = self.root / "diagnostics/probe-01/probe.json"
        path.unlink()
        outside = self.parent / "outside.json"
        write(outside, {"never": "read through symlink"})
        path.symlink_to(outside)
        self.assert_bad("artifact_outside_run|symlinked_artifact_path|unexpected_probe_artifact")

    def test_path_traversal_is_rejected_even_with_rehashed_evidence_chain(self):
        item = self.evidence[1]
        item["observation"]["report"] = "../outside"
        item["sha256"] = canonical({key: value for key, value in item.items() if key != "sha256"})
        write(self.root / "evidence/E0002.json", item)
        self.result["evidence_index"][1]["sha256"] = item["sha256"]
        event = next(event for event in self.events if event["event"] == "evidence_registered" and event["evidence_ref"] == "E0002")
        event["sha256"] = item["sha256"]
        self.flush()
        self.assert_bad("artifact_path_traversal")

    def test_passed_candidate_needs_all_eight_measurements(self):
        self.raw["measurements"].pop()
        write(self.verification_path, self.raw)
        self.result["attempts"][0]["measurements"] = self.raw["measurements"]
        self.flush()
        self.assert_bad("passed_candidate_incomplete_measurements")

    def test_reuse_counts_existing_observation_without_another_physical_run(self):
        self.emit("diagnostic_reused", evidence_ref="E0002", probe="cpu", hypothesis_id="H001")
        self.flush()
        output = audit_run(self.root)
        self.assertEqual(output["counts"]["reused_observations"], 1)
        self.assertEqual(output["counts"]["physical_probe_runs"], 9)

    def test_unreported_extra_probe_cannot_disappear_from_actual_run_count(self):
        self.make_probe(self.root / "diagnostics/probe-02", "cpu", self.base_id)
        self.assert_bad("unreferenced_or_missing_physical_probe")

    def test_nonboolean_usage_flag_is_rejected(self):
        event = next(event for event in self.events if event["event"] == "model_finished")
        event["usage_reported"] = "false"
        self.flush()
        self.assert_bad("invalid_usage_reporting_flag")

    def test_cli_outputs_machine_json_and_strict_nonzero_failure(self):
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main([str(self.root)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(buffer.getvalue())["audit_status"], "passed")
        self.result["status"] = "failed"
        self.flush()
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            code = main([str(self.root)])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(buffer.getvalue())["audit_status"], "failed")


if __name__ == "__main__":
    unittest.main()
