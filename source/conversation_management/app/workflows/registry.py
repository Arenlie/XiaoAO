from __future__ import annotations

from typing import Any
from uuid import uuid4

from app.orchestration.supervisor.contracts import AgentCall, SupervisorPlan
from app.tools.registry import ToolRegistry
from app.workflows.contracts import (
    WorkflowDefinition,
    WorkflowIntentClassification,
    WorkflowSelection,
    WorkflowVariantDefinition,
)
from app.workflows.definitions import default_workflow_definitions


class BusinessWorkflowRegistry:
    """Versioned, inspectable optional Recipe registry.

    Recipes are reusable stable execution plans, not the top-level intent taxonomy.
    Normal ReAct may reuse one only after Task Understanding marks a high-confidence fit;
    otherwise it composes MCP capabilities dynamically.
    """

    def __init__(self, definitions: list[WorkflowDefinition] | None = None) -> None:
        values = definitions or default_workflow_definitions()
        self._definitions = {item.workflow_id: item for item in values}
        if len(self._definitions) != len(values):
            raise ValueError("duplicate workflow_id")
        for definition in values:
            for variant in definition.variants:
                step_ids = {step.step_id for step in variant.steps}
                if len(step_ids) != len(variant.steps):
                    raise ValueError(
                        f"duplicate workflow step: {definition.workflow_id}/{variant.variant_id}"
                    )
                for step in variant.steps:
                    missing = set(step.depends_on) - step_ids
                    if missing:
                        raise ValueError(
                            f"unknown dependencies {sorted(missing)} in {definition.workflow_id}/{variant.variant_id}/{step.step_id}"
                        )

    def list_definitions(self) -> list[WorkflowDefinition]:
        return sorted(self._definitions.values(), key=lambda item: item.match_priority)

    def get(self, workflow_id: str) -> WorkflowDefinition:
        try:
            return self._definitions[workflow_id]
        except KeyError as exc:
            raise KeyError(f"workflow not found: {workflow_id}") from exc

    def classifier_catalog(self) -> list[dict[str, Any]]:
        """Return the model-facing semantic catalog without executable internals."""

        return [
            {
                "workflow_id": definition.workflow_id,
                "display_name": definition.display_name,
                "description": definition.description,
                "execution_role": "optional_recipe",
                "variants": [
                    {
                        "variant_id": variant.variant_id,
                        "display_name": variant.display_name,
                        "selection_rule": variant.selection_rule,
                        "required_entity_level": variant.required_entity_level,
                    }
                    for variant in definition.variants
                ],
            }
            for definition in self.list_definitions()
            if definition.enabled
        ]

    def select_from_classification(
        self,
        value: WorkflowIntentClassification | dict[str, Any],
    ) -> WorkflowSelection | None:
        """Validate a model decision and bind it to a registered workflow variant."""

        classification = (
            value
            if isinstance(value, WorkflowIntentClassification)
            else WorkflowIntentClassification.model_validate(value)
        )
        if classification.workflow_id == "none":
            return None
        definition = self.get(classification.workflow_id)
        if not definition.enabled:
            return None
        variant = self._variant(definition, classification.variant_id)
        reason = classification.reason.strip() or "主控模型选择高匹配度稳定 Recipe"
        return WorkflowSelection(
            workflow_id=definition.workflow_id,
            workflow_version=definition.version,
            display_name=definition.display_name,
            variant_id=variant.variant_id,
            variant_display_name=variant.display_name,
            required_entity_level=(
                "none" if classification.sensor_query.scope == "global" else classification.sensor_query.scope
            ) if definition.workflow_id == "sensor_information_query" else variant.required_entity_level,
            supports_collection=variant.supports_collection,
            selection_reason=f"RECIPE_MATCH: {reason}",
            source_file=definition.source_file,
            time_window_hours=classification.time_window_hours,
        )

    @staticmethod
    def _variant(definition: WorkflowDefinition, variant_id: str) -> WorkflowVariantDefinition:
        for variant in definition.variants:
            if variant.variant_id == variant_id:
                return variant
        raise KeyError(f"workflow variant not found: {definition.workflow_id}/{variant_id}")

    def selected_variant(self, selection: dict[str, Any]) -> WorkflowVariantDefinition:
        definition = self.get(str(selection["workflow_id"]))
        return self._variant(definition, str(selection["variant_id"]))

    def build_plan(self, state: dict[str, Any], selection: dict[str, Any]) -> SupervisorPlan:
        variant = self.selected_variant(selection)
        call_ids = {step.step_id: str(uuid4()) for step in variant.steps}
        calls: list[AgentCall] = []
        for step in variant.steps:
            arguments = dict(step.arguments)
            if (
                str(selection.get("workflow_id") or "") == "diagnosis_analysis"
                and str(selection.get("variant_id") or "") == "device"
                and step.step_id == "point_data_batch"
            ):
                explicit_hours = selection.get("time_window_hours")
                if explicit_hours not in (None, ""):
                    arguments["trend_hours"] = float(explicit_hours)
                    arguments.pop("fallback_trend_hours", None)
                else:
                    arguments.setdefault("trend_hours", 24.0)
                    arguments.setdefault("fallback_trend_hours", 12.0)
            arguments["required_entity_level"] = step.required_entity_level
            if selection.get("workflow_id") == "sensor_information_query":
                arguments["required_entity_level"] = selection["required_entity_level"]
            calls.append(
                AgentCall(
                    call_id=call_ids[step.step_id],
                    call_type=step.call_type,
                    agent_id=step.target_id if step.call_type == "agent" else "",
                    tool_id=step.target_id if step.call_type == "tool" else "",
                    objective=step.objective,
                    arguments=arguments,
                    depends_on=[call_ids[item] for item in step.depends_on],
                    workflow_id=str(selection["workflow_id"]),
                    workflow_step_id=step.step_id,
                )
            )
        return SupervisorPlan(
            user_goal=str(state.get("query") or ""),
            required_evidence=[step.display_name for step in variant.steps],
            calls=calls,
            planning_note=(
                f"DECLARATIVE_WORKFLOW:{selection['workflow_id']}/{selection['variant_id']}"
            ),
        )

    def next_recipe_calls(
        self, state: dict[str, Any], selection: dict[str, Any]
    ) -> list[AgentCall]:
        variant = self.selected_variant(selection)
        completed: set[str] = set()
        failed: set[str] = set()
        for item in state.get("observations") or []:
            if not isinstance(item, dict) or item.get("workflow_id") != selection.get(
                "workflow_id"
            ):
                continue
            step_id = str(item.get("workflow_step_id") or "")
            status = str(item.get("status") or "").upper()
            if status in {"SUCCESS", "COMPLETED"}:
                completed.add(step_id)
            elif status in {"FAILED", "ERROR", "TIMEOUT", "NEEDS_INPUT", "REJECTED"}:
                failed.add(step_id)

        by_id = {step.step_id: step for step in variant.steps}
        ready: list[AgentCall] = []
        plan = self.build_plan(state, selection)
        for step in variant.steps:
            if step.step_id in completed or step.step_id in failed:
                continue
            hard_failed = [
                dep
                for dep in step.depends_on
                if dep in failed and by_id[dep].failure_policy == "stop"
            ]
            if hard_failed:
                continue
            if all(
                dep in completed or (dep in failed and by_id[dep].failure_policy == "continue")
                for dep in step.depends_on
            ):
                call = next(item for item in plan.calls if item.workflow_step_id == step.step_id)
                call.depends_on = []
                ready.append(call)
        return ready

    def next_recipe_call(self, state, selection):
        calls = self.next_recipe_calls(state, selection)
        return calls[0] if calls else None

    def is_hard_failure(self, selection: dict[str, Any], step_id: str) -> bool:
        variant = self.selected_variant(selection)
        step = next((item for item in variant.steps if item.step_id == step_id), None)
        return bool(step is None or step.failure_policy == "stop")

    def describe(self, tool_registry: ToolRegistry | None = None) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for definition in self.list_definitions():
            item = definition.model_dump(mode="json")
            for variant in item["variants"]:
                for step in variant["steps"]:
                    if step["call_type"] != "tool" or tool_registry is None:
                        step["target_registered"] = True
                        step["target_enabled"] = True
                        continue
                    try:
                        descriptor = tool_registry.get_descriptor(step["target_id"])
                    except Exception:
                        step["target_registered"] = False
                        step["target_enabled"] = False
                    else:
                        step["target_registered"] = True
                        step["target_enabled"] = descriptor.enabled
            rows.append(item)
        return rows
