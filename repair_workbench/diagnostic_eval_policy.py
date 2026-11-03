"""Deterministic diagnostic feedback wiring, never a model-quality baseline.

This policy has no provider, credentials, known patch or acceptance logic. Its
name is part of the frozen offline evaluation identity. CPU function names
select a second diagnostic action; that branch does not establish a root cause.
"""
from __future__ import annotations

import json
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field


POLICY_ID = "diagnostic_probe_policy_v1"
_REQUIRED_TOOLS = frozenset({"read_source", "record_hypothesis", "run_probe", "conclude_diagnosis"})
_ALLOCATION_CUES = frozenset({"copy", "deepcopy", "__copy__", "__deepcopy__", "<listcomp>", "<dictcomp>"})


class DiagnosticProbePolicy(BaseChatModel):
    """A finite tool policy that consumes observations and leaves usage unknown."""

    model_name: str = POLICY_ID
    calls: int = 0
    bound_tools: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    hypothesis_id: str | None = None
    selected_followup: str | None = None

    @property
    def _llm_type(self):
        return POLICY_ID

    def bind_tools(self, tools, **kwargs):
        self.bound_tools = [tool.get("name", "") if isinstance(tool, dict) else getattr(tool, "name", "")
                            for tool in tools]
        return self

    def _remember(self, data):
        reference = data.get("evidence_ref")
        if isinstance(reference, str) and reference and reference not in self.evidence_refs:
            self.evidence_refs.append(reference)

    @staticmethod
    def _followup(summary):
        scales = summary.get("scales", {}) if isinstance(summary, dict) else {}
        for row in scales.values() if isinstance(scales, dict) else ():
            functions = row.get("top_functions", []) if isinstance(row, dict) else []
            for function in functions if isinstance(functions, list) else ():
                name = function.get("function") if isinstance(function, dict) else None
                if isinstance(name, str) and name.casefold() in _ALLOCATION_CUES:
                    return "memory"
        return "benchmark"

    def _reply(self, tool=None, arguments=None, text=""):
        # Deliberately no synthetic input/output/total token usage.
        message = AIMessage(content=text, tool_calls=[{
            "id": f"{POLICY_ID}-{self.calls}", "name": tool, "args": arguments,
        }] if tool else [])
        return ChatResult(generations=[ChatGeneration(message=message)])

    def _conclude(self, explanation):
        if not self.evidence_refs:
            return self._reply(text="Scripted diagnostic wiring stopped without usable evidence; no result was accepted.")
        return self._reply("conclude_diagnosis", {
            "decision": "insufficient_evidence", "explanation": explanation,
            "evidence_refs": list(self.evidence_refs),
        })

    def _generate(self, messages, stop=None, run_manager=None, **kwargs: Any):
        self.calls += 1
        if not _REQUIRED_TOOLS <= set(self.bound_tools):
            return self._reply(text="Scripted policy lacks the registered diagnostic tools; no execution or acceptance occurred.")
        latest = next((message for message in reversed(messages) if isinstance(message, ToolMessage)), None)
        if latest is None:
            return self._reply("read_source", {"path": "map_exporter.py"})
        try:
            data = json.loads(latest.content) if isinstance(latest.content, str) else None
        except (ValueError, TypeError):
            data = None
        if not isinstance(data, dict):
            return self._reply(text="Scripted policy received an unusable tool observation; no diagnostic conclusion was verified.")

        if latest.name == "read_source":
            self._remember(data)
            if not self.evidence_refs or not isinstance(data.get("content"), str):
                return self._reply(text="Source observation was unavailable; no patch or verified conclusion was produced.")
            return self._reply("record_hypothesis", {
                "claim": "The export workload may contain repeated work; its cause remains unconfirmed.",
                "status": "open", "evidence_refs": list(self.evidence_refs),
                "next_action": "Inspect CPU feedback to select one follow-up probe; this tests wiring only.",
            })
        if latest.name == "record_hypothesis":
            identity = data.get("hypothesis_id")
            if data.get("status") != "open" or not isinstance(identity, str) or not identity:
                return self._conclude("Hypothesis registration was unavailable; the scripted policy has insufficient evidence.")
            self.hypothesis_id = identity
            return self._reply("run_probe", {"probe": "cpu", "hypothesis_id": identity})
        if latest.name == "run_probe":
            self._remember(data)
            observation = data.get("observation", {})
            if not isinstance(observation, dict) or observation.get("status") != "ok":
                return self._conclude("A diagnostic execution did not succeed; that failure is not root-cause evidence.")
            if observation.get("probe") == "cpu":
                self.selected_followup = self._followup(observation.get("summary"))
                return self._reply("run_probe", {"probe": self.selected_followup,
                                                   "hypothesis_id": self.hypothesis_id})
            return self._conclude(
                "The scripted policy followed diagnostic feedback and retained its source references. "
                "No causal diagnosis, patch, performance improvement or healthy-task decision was established."
            )
        return self._reply(text="Offline feedback wiring finished with insufficient evidence. No candidate or model-quality result was produced.")


def create_scripted_model() -> BaseChatModel:
    """Return a fresh local policy; never loads settings or creates a provider."""
    return DiagnosticProbePolicy(cache=False)
