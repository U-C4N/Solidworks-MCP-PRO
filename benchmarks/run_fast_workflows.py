"""Discovery plus deterministic workflow-envelope measurements; no live CAD calls.

The native backend is used only for tools/list. Workflow calls use the synthetic
backend below and the real PRO application/orchestrator. Their JSON byte counts
and public-call counts do not measure LLM tokens, COM latency or render speed.
"""
from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import importlib.util
from importlib import metadata
import json
from pathlib import Path
import platform
import re
import tempfile
from unittest.mock import patch

from mcp import types
from PIL import Image

from solidworks_mcp_pro import __version__
from solidworks_mcp_pro.backend import NativeBackend
from solidworks_mcp_pro.server import ProApplication
from solidworks_mcp_pro.storage import CadLease


SCENARIOS = ("diagnosis", "color_32", "normal_preview_final", "exploded_preview_final")
PREVIEW = {"width": 960, "height": 540, "passes": 50, "denoiser": True}
FINAL = {"width": 1920, "height": 1080, "passes": 200, "denoiser": True}
IDENTITY = [1., 0., 0., 0., 1., 0., 0., 0., 1., 0., 0., 0., 1., 0., 0., 0.]


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _bytes(value) -> int:
    return len(_json(value).encode("utf-8"))


def optional_tokenizer(encoding_name="cl100k_base"):
    """Use an already cached tokenizer only; never download its vocabulary."""
    if importlib.util.find_spec("tiktoken") is None:
        return None, {"status": "not_measured", "reason": "Optional tiktoken package is unavailable."}
    try:
        import tiktoken
        # read_file_cached bypasses this function when a valid local cache is
        # present. Cache misses fail here instead of opening any network URL.
        with patch("tiktoken.load.read_file", side_effect=RuntimeError("Tokenizer network access disabled")):
            encoding = tiktoken.get_encoding(encoding_name)
        return lambda text: len(encoding.encode(text)), {
            "status": "measured", "encoding": encoding_name,
            "scope": "Selected tokenizer's text-envelope counts, not client usage or billing; image tokens unmeasured."}
    except Exception as exc:
        return None, {"status": "not_measured", "reason": f"Offline tokenizer unavailable: {type(exc).__name__}: {exc}"}


class _CatalogBackend:
    instructions = "Discovery only; no CAD methods may execute."

    def __init__(self, tools):
        self.tools = tools

    async def list_tools(self):
        return deepcopy(self.tools)

    async def call(self, name, arguments):
        raise AssertionError("Discovery attempted a CAD call")


async def measure_catalogs(root: Path, *, native_backend=None, count_tokens=None):
    """Read actual upstream descriptors without attaching to SOLIDWORKS."""
    owns_backend = native_backend is None
    backend = NativeBackend() if owns_backend else native_backend
    try:
        native = await backend.list_tools()
    finally:
        if owns_backend:
            backend.close()
    profiles = {}
    for profile in ("full", "compact"):
        app = ProApplication(_CatalogBackend(native), root / profile, tool_profile=profile)
        await app.initialize()
        descriptors = [tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in app.tools]
        schemas = [tool.inputSchema for tool in app.tools]
        item = {"tool_count": len(descriptors), "catalog_bytes": _bytes(descriptors),
                "input_schema_bytes": _bytes(schemas), "tool_names": [tool.name for tool in app.tools]}
        if count_tokens is not None:
            item["catalog_text_tokens"] = count_tokens(_json(descriptors))
            item["input_schema_text_tokens"] = count_tokens(_json(schemas))
        profiles[profile] = item
    return {"status": "measured", "scope": "Actual current source descriptors from NativeBackend.list_tools; compact UTF-8 JSON.",
            "native_tools_list_calls": 1, "native_cad_tools_invoked": 0, "profiles": profiles}, native


def _fixture_tools():
    """Self-contained schemas for tests or standalone synthetic scenarios."""
    specs = {"get_status": {}, "list_features": {}, "list_equations": {}, "list_components": {},
             "set_appearance": {"component": {"type": "string"}, "rgb": {"type": "array", "items": {"type": "number"}}}}
    return [types.Tool(name=name, description="Synthetic fixture only.",
                       inputSchema={"type": "object", "properties": properties, "additionalProperties": False})
            for name, properties in specs.items()]


def scenario_spec(name: str) -> dict:
    if name not in SCENARIOS:
        raise ValueError("Unknown scenario")
    result = {"scenario": name, "document_path": "C:/Benchmark/model.sldasm", "deep": False}
    if name == "color_32":
        result["assignments"] = [{"component": f"Part-{index + 1}",
                                   "color_rgb": [index * 7 % 256, index * 11 % 256, index * 13 % 256]}
                                  for index in range(32)]
    if name.endswith("preview_final"):
        result.update(view="isometric", preview=deepcopy(PREVIEW), final=deepcopy(FINAL))
    if name == "exploded_preview_final":
        result["transforms"] = [{"component": f"Part-{index + 1}", "position_mm": [index * 10, 100 + index * 5, 0],
                                 "rotation_deg": [0, 0, 0]} for index in range(8)]
    return result


class SyntheticBackend(_CatalogBackend):
    """Deterministic application fixture; never creates or accesses a CAD session."""
    instructions = "Synthetic envelope benchmark; no native CAD or rendering."

    def __init__(self, native_tools=None):
        super().__init__(_fixture_tools() if native_tools is None else native_tools)
        self.calls = []
        self.colors = {}
        self.camera = {"view": "front"}
        self.poses = {f"Part-{i + 1}": deepcopy(IDENTITY) for i in range(32)}
        self.render_settings = []
        self.render_gate = None
        self.features = [{"name": f"Boss-Extrude{i + 1}", "type": "Boss", "error_code": 0, "warning": False}
                         for i in range(64)]
        self.equations = [{"index": i, "equation": f'"D{i + 1}@Sketch1" = {i + 1}', "value": i + 1,
                           "native_status": 0} for i in range(8)]
        self.components = [{"component": f"Part-{i + 1}", "path": f"C:/Benchmark/Part{i + 1}.sldprt",
                            "suppression": 2, "configuration": "Default", "virtual": False} for i in range(32)]

    def _record(self, name, arguments):
        self.calls.append((name, deepcopy(arguments)))

    async def call(self, name, arguments):
        self._record(name, arguments)
        if name == "get_status":
            return {"ok": True, "connected": True, "document_type": "assembly", "active_document": "model.sldasm"}
        if name == "list_features":
            return {"ok": True, "features": deepcopy(self.features)}
        if name == "list_equations":
            return {"ok": True, "equations": deepcopy(self.equations)}
        if name == "list_components":
            return {"ok": True, "components": deepcopy(self.components)}
        if name == "set_appearance":
            self.colors[arguments["component"]] = list(arguments["rgb"])
            return {"ok": True, "component": arguments["component"], "rgb": list(arguments["rgb"]), "transparency": 0.0}
        raise AssertionError("Unexpected fixture tool: " + name)

    async def health(self, action, **kwargs):
        self._record(action, kwargs)
        assert action == "diagnose"
        return {"ok": True, "healthy": True, "saved": False, "errors": [], "warnings": [], "uninspected": [],
                "counts": {"features": 64, "equations": 8, "components": 32},
                "features": deepcopy(self.features), "equations": deepcopy(self.equations),
                "components": deepcopy(self.components)}

    async def colorize(self, **kwargs):
        self._record("color_preflight" if kwargs.get("dry_run") else "colorize", kwargs)
        if not kwargs.get("dry_run"):
            self.colors.update({item["component"]: list(item["color_rgb"]) for item in kwargs["assignments"]})
        return {"ok": True, "saved": False, "verified": True, "changed_count": len(kwargs["assignments"]),
                "after": deepcopy(kwargs["assignments"])}

    async def camera_state(self, action, **kwargs):
        self._record("camera_" + action, kwargs)
        if action == "capture":
            return {"ok": True, "state": deepcopy(self.camera)}
        self.camera = deepcopy(kwargs["state"])
        return {"ok": True, "verified": True}

    async def pose_state(self, action, **kwargs):
        self._record("pose_" + action, kwargs)
        assert action == "restore"
        for item in kwargs["states"]:
            self.poses[item["component"]] = deepcopy(item["transform"])
        return {"ok": True, "verified": True}

    async def animation(self, action, **kwargs):
        self._record("pose_preflight" if kwargs.get("dry_run") else action, kwargs)
        assert action == "pose"
        if kwargs.get("dry_run"):
            return {"ok": True, "read_only": False, "components": [
                {"component": item["component"], "transform": deepcopy(self.poses[item["component"]]),
                 "rigid_unit_scale": True, "suppressed": False} for item in kwargs["transforms"]]}
        for item in kwargs["transforms"]:
            matrix = deepcopy(IDENTITY)
            matrix[9:12] = [value / 1000 for value in item["position_mm"]]
            self.poses[item["component"]] = matrix
        return {"ok": True, "verified": True}

    async def visualize(self, operation, **kwargs):
        self._record(operation, kwargs)
        if operation == "configure_camera":
            if kwargs.get("action") == "inspect":
                return {"ok": True, "state": deepcopy(self.camera)}
            self.camera = {"view": kwargs["view"]}
        if operation == "render":
            if self.render_gate is not None:
                await self.render_gate.wait()
            settings = {key: kwargs[key] for key in ("width", "height", "passes", "denoiser")}
            self.render_settings.append(settings)
            directory = Path(kwargs["output_dir"])
            directory.mkdir(parents=True, exist_ok=False)
            path = directory / "render.png"
            Image.new("RGB", (settings["width"], settings["height"]), "navy").save(path)
            return {"ok": True, "images": [{"path": str(path)}]}
        return {"ok": True, "verified": True}


def _zero_timings(value):
    if isinstance(value, dict):
        return {key: _zero_timings(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_zero_timings(item) for item in value]
    return 0 if type(value) in (int, float) else value


def _normalize(value, replacements):
    if isinstance(value, dict):
        return {key: (_zero_timings(item) if key.endswith("_ms") else
                      "2000-01-01T00:00:00+00:00" if key in {"started_at", "finished_at", "created_at"} else
                      _normalize(item, replacements)) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize(item, replacements) for item in value]
    if not isinstance(value, str):
        return value
    if value.startswith(("{", "[")):
        try:
            return _json(_normalize(json.loads(value), replacements))
        except (ValueError, TypeError):
            pass
    for original, canonical in replacements:
        value = value.replace(original, canonical).replace(original.replace("\\", "/"), canonical)
    return re.sub(r"(?<![0-9a-f])[0-9a-f]{32}(?![0-9a-f])", "0" * 32, value)


class _Recorder:
    def __init__(self, app, replacements):
        self.app, self.replacements = app, replacements
        self.records = []

    async def call(self, tool, arguments):
        gate = None
        if tool in {"pro_visualize_render", "pro_render_job"}:
            # Model an asynchronous native renderer: it cannot finish before
            # the start request returns. No synthetic elapsed-time claim follows.
            gate = asyncio.Event()
            self.app.backend.render_gate = gate
        response = await self.app.call(tool, arguments)
        if gate is not None:
            gate.set()
        if response.isError:
            raise RuntimeError(f"Fixture {tool} failed: {response.structuredContent}")
        request = {"method": "tools/call", "params": {"name": tool, "arguments": arguments}}
        result = response.model_dump(mode="json", by_alias=True, exclude_none=True)
        images = sum(isinstance(item, types.ImageContent) for item in response.content)
        # Binary transport bytes are separately reported; text token counts below
        # omit base64 data because it is not the model's image-token encoding.
        text_result = deepcopy(result)
        for block in text_result["content"]:
            if block.get("type") == "image":
                block["data"] = "<binary-image-omitted>"
        self.records.append({"tool": tool, "request": _normalize(request, self.replacements),
                             "response": _normalize(result, self.replacements),
                             "text_response": _normalize(text_result, self.replacements), "image_blocks": images})
        return response.structuredContent

    async def finish(self, data):
        if data.get("operation_status") == "running":
            return await self.call("pro_operation", {"operation_id": data["operation_id"], "wait_seconds": 20})
        return data


async def _legacy(recorder, spec, document):
    if spec["scenario"] == "color_32":
        for item in spec["assignments"]:
            await recorder.call("set_appearance", {"component": item["component"], "rgb": item["color_rgb"]})
        return
    for tool in ("get_status", "list_features", "list_equations", "list_components"):
        await recorder.call(tool, {})
    if spec["scenario"] == "diagnosis":
        return
    await recorder.call("pro_visualize_status", {})
    await recorder.call("pro_visualize_camera", {"document_path": document, "action": "inspect"})
    if "transforms" in spec:
        await recorder.call("pro_assembly_pose", {"document_path": document, "transforms": spec["transforms"], "dry_run": True})
        await recorder.call("pro_assembly_pose", {"document_path": document, "transforms": spec["transforms"]})
    await recorder.call("pro_visualize_camera", {"document_path": document, "action": "apply", "view": spec["view"], "fit": True})
    for phase in ("preview", "final"):
        started = await recorder.call("pro_visualize_render", {"request_id": phase, **spec[phase], "image_format": "png"})
        await recorder.finish(started)
    if "transforms" in spec:
        original = [{**item, "position_mm": [0, 0, 0]} for item in spec["transforms"]]
        await recorder.call("pro_assembly_pose", {"document_path": document, "transforms": original})
    await recorder.call("pro_visualize_camera", {"document_path": document, "action": "apply", "view": "front", "fit": False})


async def _high_level(recorder, spec, document):
    if spec["scenario"] == "diagnosis":
        return await recorder.finish(await recorder.call("pro_diagnose", {"document_path": document, "deep": False}))
    if spec["scenario"] == "color_32":
        return await recorder.finish(await recorder.call("pro_colorize", {
            "document_path": document, "assignments": spec["assignments"], "request_id": "colors"}))
    arguments = {"document_path": document, "request_id": "render", "view": spec["view"],
                 "quality": "standard", **spec["final"]}
    if "transforms" in spec:
        arguments.update(mode="exploded", transforms=spec["transforms"])
    done = await recorder.finish(await recorder.call("pro_render_job", arguments))
    await recorder.call("pro_result", {"operation_id": done["operation_id"], "kind": "image"})
    return done


def _metrics(records, count_tokens):
    item = {"public_calls": len(records), "request_bytes": sum(_bytes(row["request"]) for row in records),
            "response_bytes": sum(_bytes(row["response"]) for row in records),
            "response_text_envelope_bytes": sum(_bytes(row["text_response"]) for row in records),
            "image_blocks": sum(row["image_blocks"] for row in records),
            "poll_image_blocks": sum(row["image_blocks"] for row in records if row["tool"] == "pro_operation"),
            "tool_sequence": [row["tool"] for row in records]}
    item["total_bytes"] = item["request_bytes"] + item["response_bytes"]
    if count_tokens is not None:
        item["request_text_tokens"] = sum(count_tokens(_json(row["request"])) for row in records)
        item["response_text_envelope_tokens"] = sum(count_tokens(_json(row["text_response"])) for row in records)
    return item


async def measure_scenario(root: Path, name: str, *, high_level: bool, native_tools=None, count_tokens=None):
    spec = scenario_spec(name)
    document = root / "model.sldasm"
    document.parent.mkdir(parents=True, exist_ok=True)
    document.write_bytes(b"SYNTHETIC FIXTURE; not a CAD file")
    backend = SyntheticBackend(native_tools)
    profile = "compact" if high_level else "full"
    app = ProApplication(backend, root / "output", tool_profile=profile)
    await app.initialize()
    # Isolate the synthetic application's lease from any real PRO session.
    class FixtureLease(CadLease):
        def __init__(self):
            super().__init__(root / "synthetic-cad.lock")
    app.engine._execution_scope = FixtureLease

    async def synthetic_locked(factory):
        async with app.engine.execution_lock:
            with FixtureLease():
                return await factory()
    app._locked = synthetic_locked
    replacements = [(str(app.store.root), "C:/Benchmark/output"), (str(document), spec["document_path"]),
                    (str(root), "C:/Benchmark")]
    recorder = _Recorder(app, replacements)
    try:
        if high_level:
            done = await _high_level(recorder, spec, str(document))
        else:
            await _legacy(recorder, spec, str(document))
            done = None
        primary = _metrics(recorder.records, count_tokens)
        evidence = {"status": "not_applicable", "reason": "Legacy responses already contain their full fixture results."}
        if high_level:
            before = len(backend.calls)
            await recorder.call("pro_result", {"operation_id": done["operation_id"], "kind": "details"})
            evidence = {"status": "measured", **_metrics(recorder.records[primary["public_calls"]:], count_tokens),
                        "backend_calls_added": len(backend.calls) - before}
        return {"scenario_spec": spec, "measurement_kind": "synthetic_application_envelope",
                "primary": primary, "optional_evidence_retrieval": evidence,
                "render_settings": backend.render_settings, "synthetic_render_calls": len(backend.render_settings),
                "final_colors": backend.colors, "temporary_state_restored": backend.camera == {"view": "front"}
                and all(matrix == IDENTITY for matrix in backend.poses.values()),
                "native_cad_wall_ms": None, "native_render_wall_ms": None}
    finally:
        await app.registry.drain()


async def run_benchmark(root: Path, *, repeats=3, native_backend=None, count_tokens=None, tokenizer_status=None):
    if type(repeats) is not int or not 1 <= repeats <= 25:
        raise ValueError("repeats must be an integer in 1..25")
    catalogs, native_tools = await measure_catalogs(root / "catalogs", native_backend=native_backend, count_tokens=count_tokens)
    scenarios = []
    for name in SCENARIOS:
        samples = []
        for index in range(repeats):
            sample = {}
            for label, high_level in (("legacy", False), ("high_level", True)):
                sample[label] = await measure_scenario(root / name / str(index) / label, name,
                    high_level=high_level, native_tools=native_tools, count_tokens=count_tokens)
            if sample["legacy"]["scenario_spec"] != sample["high_level"]["scenario_spec"]:
                raise AssertionError("Scenario settings diverged")
            samples.append(sample)
        scenarios.append({"scenario": name, "samples": samples,
                          "normalized_metrics_stable": all(
                              sample[label]["primary"] == samples[0][label]["primary"]
                              for sample in samples for label in ("legacy", "high_level"))})
    packages = {}
    for package in ("solidworks-mcp", "mcp", "Pillow"):
        try:
            packages[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            packages[package] = None
    return {"version": __version__, "measured_at_utc": datetime.now(timezone.utc).isoformat(),
            "environment": {"python": platform.python_version(), "platform": platform.platform(), "packages": packages},
            "repeats": repeats, "catalogs": catalogs, "scenarios": scenarios,
            "tokenizer": tokenizer_status or {"status": "not_measured", "reason": "No tokenizer supplied."},
            "limits": ["No native CAD tools or real renderer invoked. Native CAD/render wall times unmeasured.",
                       "Workflow bytes are canonicalized synthetic application JSON, not measured stdio traffic or LLM usage.",
                       "Fixture paths, UUIDs and timing fields normalized; JSON-RPC framing, client inference and image tokens excluded.",
                       "Legacy pattern uses four inventories, individual RGB calls and one terminal poll per render; other valid patterns differ.",
                       "Legacy renderer polls deliver preview and final images; high-level poll delivers no images and final is fetched once.",
                       "Legacy restoration uses fixture's known front view and zero rotations; this does not establish general native restoration parity.",
                       "Matching settings and synthetic outputs do not establish native geometry correctness, scene fidelity or speed."]}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--encoding", default="cl100k_base", help="Optional already-cached tiktoken encoding; no downloads")
    args = parser.parse_args(argv)
    count_tokens, tokenizer_status = optional_tokenizer(args.encoding)
    with tempfile.TemporaryDirectory(prefix="sw-pro-fast-") as directory:
        report = asyncio.run(run_benchmark(Path(directory), repeats=args.repeats,
                                          count_tokens=count_tokens, tokenizer_status=tokenizer_status))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n", encoding="utf-8")
    print(f"Wrote discovery and synthetic workflow evidence: {args.output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
