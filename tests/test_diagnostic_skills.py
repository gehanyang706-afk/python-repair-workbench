"""Real Deep Agents metadata/read wiring using deterministic models, without APIs."""
import copy
import hashlib
import json
import unittest
from unittest.mock import patch

try:
    from deepagents import (GeneralPurposeSubagentProfile, HarnessProfile,
                            create_deep_agent, register_harness_profile)
    from deepagents.backends import StateBackend
    from deepagents.middleware.filesystem import FilesystemMiddleware
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langsmith import tracing_context
    from pydantic import Field
    from repair_workbench.diagnostic_skills import (SKILL_SOURCES, initial_skill_files,
                                                   trusted_skill_manifest)
    AVAILABLE = True
except ImportError:
    AVAILABLE = False


if AVAILABLE:
    class SkillReaderModel(BaseChatModel):
        """Select a known virtual path to verify framework behavior, not model skill."""
        calls: int = 0
        model_name: str = "repair-skills:offline-contract"
        selected_path: str | None = None
        tools_seen: list = Field(default_factory=list)
        inputs_seen: list = Field(default_factory=list)

        @property
        def _llm_type(self):
            return "offline-trusted-skills-contract"

        def bind_tools(self, tools, **kwargs):
            self.tools_seen = [item.name for item in tools]
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            self.inputs_seen.append(list(messages))
            self.calls += 1
            if self.calls == 1 and self.selected_path:
                response = AIMessage(content="", tool_calls=[{
                    "name": "read_file", "args": {"file_path": self.selected_path, "limit": 1000},
                    "id": "load-selected-candidate-skill"}])
            else:
                response = AIMessage(content="Offline skill loading contract completed.")
            return ChatResult(generations=[ChatGeneration(message=response)])


@unittest.skipUnless(AVAILABLE, "requires the pinned optional Agent dependencies")
class DiagnosticSkillTests(unittest.TestCase):
    def graph(self, model):
        register_harness_profile("repair-skills:offline-contract", HarnessProfile(
            general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False)))
        backend = StateBackend()
        return create_deep_agent(
            model=model, backend=backend, skills=list(SKILL_SOURCES), subagents=[],
            system_prompt="Candidate strategies are read-only and not generalization-validated.",
            middleware=[FilesystemMiddleware(backend=backend, tools=["read_file"])])

    def run_graph(self, model, *, seed=True):
        payload = {"messages": [{"role": "user", "content": "Inspect the available candidate diagnostic strategies."}]}
        if seed:
            payload["files"] = initial_skill_files()
        with tracing_context(enabled=False):
            streamed = list(self.graph(model).stream(payload, stream_mode=["updates", "values"],
                                                    config={"max_concurrency": 1, "recursion_limit": 12}))
        updates = [item for mode, item in streamed if mode == "updates"]
        self.last_graph_state = [item for mode, item in streamed if mode == "values"][-1]
        metadata = [item["SkillsMiddleware.before_agent"] for item in updates
                    if "SkillsMiddleware.before_agent" in item]
        self.assertEqual(len(metadata), 1)
        return updates, metadata[0]

    def test_manifest_and_seed_are_versioned_fresh_and_content_verified(self):
        first, second = initial_skill_files(), initial_skill_files()
        self.assertEqual(first, second)
        manifest = trusted_skill_manifest()
        self.assertFalse(manifest["generalization_verified"])
        self.assertEqual(manifest["validation_status"], "offline_wiring_only")
        unsigned = {key: value for key, value in manifest.items() if key != "sha256"}
        self.assertEqual(manifest["sha256"], hashlib.sha256(json.dumps(unsigned, sort_keys=True,
                                                                     ensure_ascii=False).encode()).hexdigest())
        self.assertEqual(set(first), {item["path"] for item in manifest["skills"]})
        self.assertEqual(len(first), 3)
        for item in manifest["skills"]:
            data = first[item["path"]]
            self.assertEqual(data["encoding"], "utf-8")
            self.assertEqual(hashlib.sha256(data["content"].encode()).hexdigest(), item["sha256"])
            self.assertTrue(item["applicability"])
            self.assertIn("候选", data["content"])
        first[next(iter(first))]["content"] = "caller mutation"
        self.assertNotEqual(first, second)
        self.assertEqual(second, initial_skill_files())

    def test_corrupted_bundled_skill_is_rejected_before_seed_or_manifest(self):
        with patch("repair_workbench.diagnostic_skills.files") as resources:
            resources.return_value.joinpath.return_value.read_bytes.return_value = b"forged strategy"
            with self.assertRaisesRegex(ValueError, "trusted_skill_content_mismatch"):
                initial_skill_files()
            with self.assertRaisesRegex(ValueError, "trusted_skill_content_mismatch"):
                trusted_skill_manifest()

    def test_real_framework_discovers_all_metadata_without_injecting_full_bodies(self):
        model = SkillReaderModel()
        _, state_update = self.run_graph(model)
        discovered = state_update["skills_metadata"]
        self.assertEqual(state_update["skills_load_errors"], [])
        manifest = trusted_skill_manifest()
        self.assertEqual({item["path"] for item in discovered}, {item["path"] for item in manifest["skills"]})
        self.assertEqual({item["name"] for item in discovered}, {item["name"] for item in manifest["skills"]})
        for item in discovered:
            self.assertEqual(item["metadata"]["version"], "0.1.0-candidate")
            self.assertEqual(item["metadata"]["validation"], "offline-wiring-only")
        systems = "\n".join(str(item.content) for item in model.inputs_seen[0] if isinstance(item, SystemMessage))
        for skill in manifest["skills"]:
            self.assertIn(skill["path"], systems)
        self.assertNotIn("区分累计耗时与自身耗时", systems)
        self.assertNotIn("追踪峰值只覆盖", systems)
        self.assertNotIn("缩小实际处理范围", systems)
        self.assertFalse(any(isinstance(item, ToolMessage) for item in model.inputs_seen[0]))

    def test_real_read_file_loads_only_selected_skill_on_demand(self):
        path = "/skills/cpu-hotspot/SKILL.md"
        seeded_before = copy.deepcopy(initial_skill_files())
        model = SkillReaderModel(selected_path=path)
        updates, _ = self.run_graph(model)
        self.assertEqual(model.calls, 2)
        tool_messages = [message for message in model.inputs_seen[1] if isinstance(message, ToolMessage)]
        self.assertEqual(len(tool_messages), 1)
        self.assertEqual(tool_messages[0].name, "read_file")
        self.assertEqual(tool_messages[0].status, "success")
        self.assertIn("区分累计耗时与自身耗时", str(tool_messages[0].content))
        self.assertNotIn("追踪峰值只覆盖", str(tool_messages[0].content))
        self.assertNotIn("缩小实际处理范围", str(tool_messages[0].content))
        self.assertNotIn("write_file", model.tools_seen)
        self.assertNotIn("edit_file", model.tools_seen)
        self.assertNotIn("execute", model.tools_seen)
        self.assertNotIn("task", model.tools_seen)
        self.assertEqual(self.last_graph_state["files"], seeded_before)
        # The evidence is the real tool response in the second model request,
        # not content returned by the asset helper itself.
        self.assertTrue(any("tools" in update for update in updates))

    def test_sources_without_seed_really_load_no_skills(self):
        model = SkillReaderModel()
        _, state_update = self.run_graph(model, seed=False)
        self.assertEqual(state_update["skills_metadata"], [])
        self.assertEqual(state_update["skills_load_errors"], [])
        systems = "\n".join(str(item.content) for item in model.inputs_seen[0] if isinstance(item, SystemMessage))
        self.assertIn("No skills available", systems)
        self.assertNotIn("/skills/cpu-hotspot/SKILL.md", systems)

    def test_unseeded_path_read_cannot_fall_back_to_host_files(self):
        model = SkillReaderModel(selected_path="/etc/passwd")
        self.run_graph(model, seed=False)
        tool_messages = [item for item in model.inputs_seen[1] if isinstance(item, ToolMessage)]
        self.assertEqual(len(tool_messages), 1)
        self.assertIn("not found", str(tool_messages[0].content).lower())
        self.assertNotIn("root:", str(tool_messages[0].content))


if __name__ == "__main__":
    unittest.main()
