"""Bounded presentation of complete, separately retained operation evidence."""
from copy import deepcopy


def summarize(data: dict, *, details: tuple[str, str] | None = None) -> dict:
    fields = {
        "ok", "status", "operation_id", "operation_status", "name", "stage", "failed_stage",
        "code", "error_code", "error_type", "healthy", "saved", "rebuilt", "verified", "dry_run",
        "plan_id", "requires_apply", "replayed", "counts", "duration_ms", "timings_ms", "timings",
        "mutation_attempted", "partial_state_possible", "outcome_may_be_indeterminate", "changes_may_remain",
        "executed_steps", "total_steps", "summary", "path", "uri",
        "visual_review_required", "settings", "preview_artifacts", "final_artifacts",
        "document_path", "expected_results", "model_state_changed", "idempotency_scope",
        "changed_count", "skipped_count",
        "passed", "source_unchanged", "active_document_restored", "csv_path", "csv_uri",
        "manifest_path", "manifest_uri", "contact_sheet", "manifest", "ranked_variants",
        "best_variant", "planned_variant_count", "source_document_unchanged", "state_unchanged",
        "render_route",
    }
    result = {key: deepcopy(value) for key, value in data.items() if key in fields}
    # Preserve actionable failures even if the operation stopped before producing
    # its normal report. Full native state/arguments stay in the evidence file.
    for key in ("error", "message"):
        if key in data:
            value = data[key]
            result[key] = value[:1200] if isinstance(value, str) else deepcopy(value)
    for key in ("errors", "warnings", "uninspected", "issues", "evidence_warnings", "artifact_warnings"):
        if isinstance(data.get(key), list):
            result[key + "_count"] = len(data[key])
            result[key] = deepcopy(data[key][:8])
    if isinstance(data.get("repair_plan"), dict):
        plan = data["repair_plan"]
        result["repair_plan"] = {key: deepcopy(plan[key]) for key in (
            "ok", "plan_id", "document_path", "requires_apply", "steps", "expected_results", "error", "code") if key in plan}
    if isinstance(data.get("diagnosis"), dict):
        result["diagnosis"] = summarize(data["diagnosis"])
    if isinstance(data.get("color_changes"), dict):
        result["color_changes"] = summarize(data["color_changes"])
    if isinstance(data.get("steps"), list):
        result["step_count"] = len(data["steps"])
        if "plan_id" in data:
            result["steps"] = deepcopy(data["steps"])
    if isinstance(data.get("operations"), list):
        result["operations"] = deepcopy(data["operations"][:20])
        result["operation_count"] = len(data["operations"])
    for key in ("checks", "assertions", "variants", "planned_variants", "shots"):
        if isinstance(data.get(key), list):
            result[key + "_count"] = len(data[key])
            if key == "shots":
                result[key] = [{field: deepcopy(shot[field]) for field in (
                    "name", "status", "aspect", "camera", "project_camera", "preview_settings", "final_settings", "images")
                    if field in shot} for shot in data[key][:8]]
            else:
                result[key] = deepcopy(data[key][:8])
    if isinstance(data.get("cleanup"), dict):
        result["cleanup"] = {name: {key: deepcopy(state[key]) for key in (
            "ok", "attempted", "verified", "error", "errors") if key in state}
            for name, state in data["cleanup"].items() if isinstance(state, dict)}
        for key in ("errors", "closed_owned_copies"):
            if isinstance(data["cleanup"].get(key), list):
                result["cleanup"][key + "_count"] = len(data["cleanup"][key])
                result["cleanup"][key] = deepcopy(data["cleanup"][key][:16])
    for name in ("rollback", "restoration"):
        if isinstance(data.get(name), dict):
            state = data[name]
            result[name] = {key: deepcopy(state[key]) for key in (
                "ok", "attempted", "verified", "error", "errors") if key in state}
            if isinstance(state.get("errors"), list):
                result[name]["error_count"] = len(state["errors"])
                result[name]["errors"] = deepcopy(state["errors"][:8])
    if isinstance(data.get("stages"), list):
        result["stage_timings_ms"] = {stage["stage"]: stage.get("duration_ms") for stage in data["stages"]
                                      if isinstance(stage, dict) and "stage" in stage}
    keys = data.get("image_artifacts", [])
    if keys:
        result["image_uris"] = ["solidworks-pro://artifact/" + key for key in keys]
    if data.get("artifact_warnings"):
        result["image_files"] = [{key: item[key] for key in ("path", "width", "height") if key in item}
                                 for item in data.get("images", []) + data.get("preview_images", [])]
    if details is not None:
        result["details_uri"], result["details_path"] = details
    return result
