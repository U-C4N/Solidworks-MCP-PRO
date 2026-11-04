"""Sequential, opt-in live CAD comparison with an independent COM oracle."""
from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import random
import time
import tomllib
from uuid import uuid4

from benchmarks.oracle import snapshot, connection
from benchmarks.run_protocol import connect, timed_call, sanitize_report
from benchmarks.competitors import request


class CaseFailure(RuntimeError):
    pass


def default_specs(root: Path) -> list[dict]:
    root = root.resolve()
    with (root / "pyproject.toml").open("rb") as stream:
        current_version = tomllib.load(stream)["project"]["version"]
    base = {"command": str(root / ".venv/Scripts/python.exe"), "cwd": str(root),
            "startup_timeout_seconds": 60, "request_timeout_seconds": 180, "enabled": True}
    return [
        {**base, "id": "pro_before", "name": "PRO 0.1.0 frozen", "args": ["-m", "solidworks_mcp_pro"],
         "env": {"PYTHONPATH": str(root / ".local/pro-v0.1.0/src"), "SW_MCP_PRO_WORKSPACE": str(root / "artifacts/benchmarks/pro-before")},
         "required_files": [str(root / ".local/pro-v0.1.0/src/solidworks_mcp_pro/__init__.py")],
         "source": {"revision": "frozen-0.1.0", "wheel_sha256": "37791b5f24ba5c4aa5b9c83f313cd33b59640e950e722eaaeedcde1e2d7768fc"}},
        {**base, "id": "pro_v020", "name": "PRO 0.2.0 frozen", "args": ["-m", "solidworks_mcp_pro"],
         "env": {"PYTHONPATH": str(root / ".local/pro-v0.2.0/src"), "SW_MCP_PRO_WORKSPACE": str(root / "artifacts/benchmarks/pro-v020")},
         "required_files": [str(root / ".local/pro-v0.2.0/src/solidworks_mcp_pro/__init__.py")],
         "expected_server_version": "0.2.0",
         "source": {"revision": "frozen-0.2.0", "package_version": "0.2.0", "wheel_sha256": "4dca5d879461e412440413a84f62a7ecec06cfe0af9dbc4db4b4d67a1cac2cf5"}},
        {**base, "id": "pro", "name": "PRO current", "args": ["-m", "solidworks_mcp_pro"],
         "env": {"PYTHONPATH": str(root / "src"), "SW_MCP_PRO_WORKSPACE": str(root / "artifacts/benchmarks/pro-current")},
         "required_files": [str(root / "src/solidworks_mcp_pro/__init__.py")],
         "expected_server_version": current_version,
         "source": {"revision": "working-tree", "package_version": current_version}},
        {**base, "id": "hjbaard", "name": "hjbaard 0.19.0", "args": ["-m", "solidworks_mcp.server"], "env": {},
         "source": {"repository": "https://github.com/hjbaard/SolidWorks-MCP", "revision": "ddcefc0cfa056002ff8eb8bdb186818ad52dc2f1"}},
    ]


def balanced_orders(specs: list[dict], repeats: int, seed: int = 20261103) -> list[list[dict]]:
    """Seeded Williams orders: full position/pair balance for six rows/systems.

    With even n, every complete n-row cycle places each adapter at every
    position once and every ordered adjacent pair once. Odd n includes
    reversed rows in a 2n-row cycle, balancing positions and adjacent pairs.
    """
    if not specs or type(repeats) is not int or repeats < 1:
        raise ValueError("At least one server and one repetition are required")
    identifiers = [item["id"] for item in specs]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("Server IDs must be unique")
    rng = random.Random(seed)
    labels = list(specs)
    rng.shuffle(labels)
    n = len(labels)
    first = [0]
    low, high = 1, n - 1
    while low <= high:
        first.append(low)
        low += 1
        if low <= high:
            first.append(high)
            high -= 1
    rows = [[(value + offset) % n for value in first] for offset in range(n)]
    if n % 2 and n > 1:
        rows += [list(reversed(row)) for row in rows]
    order = []
    while len(order) < repeats:
        cycle = rows.copy()
        rng.shuffle(cycle)
        order.extend([[labels[index] for index in row] for row in cycle])
    return order[:repeats]


def source_tree_sha256(folder: Path) -> str | None:
    if not folder.is_dir():
        return None
    digest = hashlib.sha256()
    for path in sorted(folder.rglob("*")):
        if path.is_file() and path.suffix in (".py", ".html", ".md"):
            digest.update(path.relative_to(folder).as_posix().encode("utf-8") + b"\0")
            digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


class Driver:
    def __init__(self, session, kind, folder):
        self.session, self.kind, self.folder = session, kind, folder
        self.calls = []
        self.title = None
        self.depth_dimension = "D1@BenchBlock"

    async def call(self, name, args=None, allow_error=False):
        args = args or {}
        record = await timed_call(self.session, name, args)
        record["arguments"] = args
        self.calls.append(record)
        data = record.get("output", {})
        while isinstance(data, dict) and data.get("operation_status") == "running":
            record = await timed_call(self.session, "pro_operation", {"operation_id": data["operation_id"], "wait_seconds": 20})
            self.calls.append(record)
            data = record.get("output", {})
        if record["is_error"] and not allow_error:
            raise CaseFailure(json.dumps(data or record.get("error")))
        return record

    async def new(self):
        if self.kind == "andrew":
            await self.mapped("new_part", name="Bench_" + uuid4().hex[:10])
            self.title = snapshot()["active_title"]
            return self.title
        names = {"soheil": "doc_new", "harrier": "solidworks_new_part"}
        await self.call(names.get(self.kind, "new_part"), {"type": "part"} if self.kind == "soheil" else {})
        self.title = snapshot()["active_title"]
        return self.title

    async def mapped(self, operation, **params):
        call = request(self.kind, operation, **params)
        return await self.call(call["name"], call["arguments"])

    async def block(self):
        if self.kind == "andrew":
            result = await self.mapped("sketch_start", plane="Front")
            sketch = result["output"]["sketch"]["name"]
            await self.mapped("rectangle", width=80, height=50, center_x=40, center_y=25)
            await self.mapped("sketch_finish")
            result = await self.mapped("extrude", sketch=sketch, depth=8)
            self.depth_dimension = "D1@" + result["output"]["extrusion"]["name"]
        elif self.kind == "soheil":
            await self.call("sketch_start", {"on": {"name": "front"}})
            await self.call("sketch_add", {"entities": [{"kind": "rectangle", "corners": [[0, 0], [80, 50]]}]})
            await self.call("sketch_finish")
            await self.call("feature_extrude", {"depthMm": 8, "name": "BenchBlock"})
        elif self.kind == "harrier":
            await self.call("solidworks_create_sketch", {"plane": "Front"})
            await self.call("solidworks_sketch_rectangle", {"x1": 0, "y1": 0, "x2": 80, "y2": 50})
            result = await self.call("solidworks_create_extrusion", {"depth": 8})
            data = result["output"]
            self.depth_dimension = "D1@" + data.get("id", "feat:Boss-Extrude1").removeprefix("feat:")
        else:
            await self.call("add_box", {"width_mm": 80, "height_mm": 50, "depth_mm": 8, "name": "BenchBlock"})

    async def hole(self):
        if self.kind == "andrew":
            result = await self.mapped("sketch_start", plane="Front")
            sketch = result["output"]["sketch"]["name"]
            await self.mapped("circle", center_x=40, center_y=25, radius=5)
            await self.mapped("sketch_finish")
            await self.mapped("cut_through", sketch=sketch, depth=8)
        elif self.kind == "soheil":
            await self.call("feature_hole", {"type": "drill", "diameterMm": 10, "end": "through_all", "points": [[40, 25, 8]]})
        elif self.kind == "harrier":
            await self.call("solidworks_create_sketch", {"plane": "Front"})
            await self.call("solidworks_sketch_circle", {"centerX": 40, "centerY": 25, "radius": 5})
            await self.call("solidworks_create_cut_extrusion", {"depth": 8, "endCondition": "THROUGH_ALL"})
        else:
            await self.call("add_hole", {"diameter_mm": 10, "x_mm": 40, "y_mm": 25})

    async def depth(self):
        if self.kind == "andrew":
            await self.mapped("set_dimension", dimension=self.depth_dimension, value=12)
        elif self.kind == "soheil":
            await self.call("dimension_set", {"values": [{"name": self.depth_dimension, "value": 12}]})
        elif self.kind == "harrier":
            await self.call("solidworks_set_parameter", {"parameter": self.depth_dimension, "value": 12, "unit": "mm"})
        else:
            await self.call("set_dimension", {"dimension_name": self.depth_dimension, "value_mm": 12})

    async def mass(self, extra=False):
        name = {"soheil": "model_mass", "harrier": "solidworks_get_mass_properties"}.get(self.kind, "get_mass_properties")
        return await self.call(name, {"unexpected_benchmark_argument": 123} if extra else {}, allow_error=extra)

    async def export(self, path, allow_error=False):
        if self.kind == "andrew":
            call = request("andrew", "export", path=path)
            return await self.call(call["name"], call["arguments"], allow_error=allow_error)
        name = {"soheil": "model_export", "harrier": "solidworks_save_document"}.get(self.kind, "export")
        return await self.call(name, {"path": str(path)}, allow_error=allow_error)

    async def close(self):
        if self.kind == "andrew":
            snapshot(self.title)  # tool uses currentModel; never close a foreign active document
            return await self.mapped("close", doc=self.title)
        if self.kind == "soheil":
            return await self.call("doc_close", {"discard": True})
        if self.kind == "harrier":
            return await self.call("solidworks_close_document", {"name": self.title})
        return await self.call("close_part")


def verify_shape(measured, volume, size):
    error = abs(measured["volume_mm3"] - volume) / volume
    size_error = max(abs(a - b) for a, b in zip(measured["size_mm"], size))
    return {"status": "pass" if error <= 1e-6 and size_error <= 0.01 else "fail",
            "expected_volume_mm3": volume, "relative_volume_error": error,
            "expected_size_mm": size, "max_size_error_mm": size_error, "oracle": measured}


CAD_STAGES = ("new_part", "block", "through_hole", "parametric_edit", "mass_query", "step_export", "close_own_document")


def cad_completed(record):
    return all(record.get("cases", {}).get(name, {}).get("status") == "pass" for name in CAD_STAGES)


async def measure(spec, repetition, folder, *, phase="measured"):
    folder.mkdir(parents=True)
    result = {"id": spec["id"], "repetition": repetition, "source": spec.get("source"),
              "runtime_mode": spec.get("runtime_mode", "native"),
              "phase": phase, "cases": {}, "calls": [], "policy_calls": [], "status": "fail"}
    initial = snapshot()
    prefs = {}
    if spec["id"] == "andrew":
        with connection() as (app, _, _):
            prefs = {key: bool(app.GetUserPreferenceToggle(key)) for key in (150, 149, 10, 372, 520, 642)}
    protected = {(d["title"], d["path"]) for d in initial["documents"]}
    driver = None
    policy_driver = None
    try:
        async with connect(spec) as session:
            result["initialization"] = getattr(session, "benchmark_initialize", {})
            actual_version = result["initialization"].get("response", {}).get("serverInfo", {}).get("version")
            if spec.get("expected_server_version") and actual_version != spec["expected_server_version"]:
                raise CaseFailure(f"Expected server version {spec['expected_server_version']}, initialized {actual_version}")
            driver = Driver(session, spec["id"], folder)
            async def stage(name, action, expected=None):
                start = len(driver.calls)
                wall_start = time.perf_counter()
                action_ms = None
                oracle_ms = 0.0
                try:
                    await action()
                    action_ms = (time.perf_counter() - wall_start) * 1000
                    if expected:
                        oracle_start = time.perf_counter()
                        check = verify_shape(snapshot(driver.title), *expected)
                        oracle_ms = (time.perf_counter() - oracle_start) * 1000
                    else:
                        check = {"status": "pass"}
                except Exception as exc:
                    check = {"status": "fail", "error": str(exc)[:2000]}
                check.update(mcp_elapsed_ms=round(sum(c["elapsed_ms"] for c in driver.calls[start:]), 3),
                             tool_calls=len(driver.calls) - start, call_index_start=start,
                             call_index_end_exclusive=len(driver.calls),
                             action_wall_ms=round(action_ms, 3) if action_ms is not None else None,
                             oracle_ms=round(oracle_ms, 3), stage_wall_ms=round((time.perf_counter() - wall_start) * 1000, 3))
                result["cases"][name] = check
                print(f"{phase} {spec['id']} r{repetition} {name}: {check['status']}", flush=True)
                if check["status"] == "fail":
                    raise CaseFailure(name)

            await stage("new_part", driver.new)
            if any(title == driver.title for title, _ in protected):
                result["cases"]["new_part"]["status"] = "fail"
                raise CaseFailure("new_part did not create a fresh document")
            await stage("block", driver.block, (32000, [80, 50, 8]))
            await stage("through_hole", driver.hole, (32000 - math.pi * 25 * 8, [80, 50, 8]))
            await stage("parametric_edit", driver.depth, ((4000 - math.pi * 25) * 12, [80, 50, 12]))
            await stage("mass_query", driver.mass)
            path = folder / "model.step"
            await stage("step_export", lambda: driver.export(path))
            result["cases"]["step_export"].update(bytes=path.stat().st_size if path.exists() else 0)
            if not path.exists() or path.stat().st_size < 100:
                result["cases"]["step_export"]["status"] = "fail"
            # Close before policy probes: an allowed export changes application
            # state, while a refused export does not. Keep that out of CAD timing.
            await stage("close_own_document", driver.close)
            result["status"] = "completed" if cad_completed(result) else "completed_with_failures"
            policy_driver = Driver(session, spec["id"], folder / "policy")
            (folder / "policy").mkdir()
            setup_start = time.perf_counter()
            try:
                await policy_driver.new()
                if any(title == policy_driver.title for title, _ in protected):
                    raise CaseFailure("Policy new_part did not create a fresh document")
                await policy_driver.block()
                setup_check = verify_shape(snapshot(policy_driver.title), 32000, [80, 50, 8])
                if setup_check["status"] != "pass":
                    raise CaseFailure("Policy setup block failed independent geometry verification")
                result["policy_setup"] = {"status": "pass", "wall_ms": round((time.perf_counter() - setup_start) * 1000, 3),
                                          "tool_calls": len(policy_driver.calls), "excluded_from_cad_timing": True}
                strict = await policy_driver.mass(extra=True)
                result["cases"]["unknown_argument_rejected"] = {"status": "pass" if strict["is_error"] else "fail",
                                                                  "mcp_elapsed_ms": strict["elapsed_ms"]}
                target = folder / "policy" / "protected.step"
                sentinel = b"BENCHMARK SENTINEL: must not overwrite without explicit permission\n"
                target.write_bytes(sentinel)
                overwrite = await policy_driver.export(target, allow_error=True)
                preserved = target.read_bytes() == sentinel
                result["cases"]["default_no_clobber"] = {"status": "pass" if preserved and overwrite["is_error"] else "fail",
                    "preserved": preserved, "reported_error": overwrite["is_error"], "mcp_elapsed_ms": overwrite["elapsed_ms"]}
            except Exception as exc:
                result.setdefault("policy_setup", {"status": "fail", "wall_ms": round((time.perf_counter() - setup_start) * 1000, 3),
                                                  "excluded_from_cad_timing": True})
                result["policy_error"] = str(exc)[:2000]
                for name in ("unknown_argument_rejected", "default_no_clobber"):
                    result["cases"].setdefault(name, {"status": "not_run", "reason": "Policy fixture or preceding probe failed"})
            finally:
                cleanup_start = time.perf_counter()
                if policy_driver.title and not any(title == policy_driver.title for title, _ in protected):
                    try:
                        await policy_driver.close()
                        result["policy_cleanup"] = {"status": "pass"}
                    except Exception as exc:
                        result["policy_cleanup"] = {"status": "fail", "error": str(exc)[:2000]}
                else:
                    result["policy_cleanup"] = {"status": "not_run", "reason": "No verified owned policy fixture"}
                result["policy_cleanup"].update(wall_ms=round((time.perf_counter() - cleanup_start) * 1000, 3), excluded_from_cad_timing=True)
    except Exception as exc:
        result["error"] = str(exc)[:2000]
    finally:
        if driver:
            result["calls"] = driver.calls
        if policy_driver:
            result["policy_calls"] = policy_driver.calls
        # If a competitor's own close failed, clean ONLY the fresh benchmark doc.
        after = snapshot()
        titles = {item.title for item in (driver, policy_driver) if item and item.title}
        for title in titles:
            candidates = [d for d in after["documents"] if d["title"] == title and (d["title"], d["path"]) not in protected]
            if len(candidates) == 1:
                with connection() as (app, _, _):
                    app.CloseDoc(title)
                result.setdefault("cleanup_documents", []).append(title)
                result["cleanup"] = "benchmark-owned document(s) closed via oracle cleanup"
        result["remaining_documents"] = snapshot()["documents"]
        if prefs:
            with connection() as (app, _, _):
                for key, value in prefs.items():
                    app.SetUserPreferenceToggle(key, value)
                result["startup_preferences_restored"] = all(bool(app.GetUserPreferenceToggle(k)) == v for k, v in prefs.items())
        (folder / "result.json").write_text(json.dumps(result, indent=2, ensure_ascii=True), encoding="utf-8")
    return result


async def execute_suite(specs, repeats, folder, *, warmups=1, seed=20261103, measure_fn=None):
    """Keep warm-up evidence separately and abort visibly if warm-up CAD fails."""
    if type(warmups) is not int or not 0 <= warmups <= 5:
        raise ValueError("warmups must be between 0 and 5")
    invoke = measure_fn or measure
    planned = balanced_orders(specs, repeats, seed)
    warmup_plan = balanced_orders(specs, warmups, seed + 1) if warmups else []
    result = {"records": [], "warmup_records": [], "status": "completed", "seed": seed,
              "planned_order": [[spec["id"] for spec in row] for row in planned],
              "warmup_planned_order": [[spec["id"] for spec in row] for row in warmup_plan], "execution_order": []}
    for phase, orders, key in [("warmup", warmup_plan, "warmup_records"), ("measured", planned, "records")]:
        for repeat, order in enumerate(orders):
            for position, spec in enumerate(order):
                entry = {"phase": phase, "repetition": repeat, "position": position, "id": spec["id"]}
                result["execution_order"].append(entry)
                record = await invoke(spec, repeat, folder / phase / f"r{repeat}-{spec['id']}", phase=phase)
                record.update(order_position=position, execution_index=len(result["execution_order"]) - 1)
                result[key].append(record)
                if phase == "warmup" and (not cad_completed(record) or record.get("policy_setup", {}).get("status") != "pass" or record.get("policy_cleanup", {}).get("status") != "pass"):
                    result.update(status="aborted", abort_reason=f"Warm-up CAD or fixture setup/cleanup failed for {spec['id']}; no subsequent scenarios were run")
                    return result
                if record.get("startup_preferences_restored") is False:
                    result.update(status="aborted", abort_reason=f"Startup preference restoration failed for {spec['id']}")
                    return result
    if any(not cad_completed(record) for record in result["records"]):
        result["status"] = "completed_with_failures"
    return result


async def run(args):
    if args.output.exists():
        raise FileExistsError(f"Benchmark output already exists; choose a new output path: {args.output}")
    root = Path(__file__).resolve().parents[1]
    competitors = json.loads((root / "benchmarks/competitors.json").read_text())["servers"]
    specs = default_specs(root) + competitors
    by_id = {spec["id"]: spec for spec in specs}
    requested = [item.strip() for item in args.servers.split(",") if item.strip()]
    if not requested or len(set(requested)) != len(requested):
        raise ValueError("Select unique server IDs")
    unavailable = [identifier for identifier in requested if identifier not in by_id or not by_id[identifier].get("enabled")]
    if unavailable:
        raise ValueError(f"Requested servers missing or disabled: {unavailable}")
    selected = [deepcopy(by_id[identifier]) for identifier in requested]
    for spec in selected:
        if spec["id"] == "andrew":
            spec["args"] = [arg for arg in spec["args"] if arg != "--mock"]
            spec["runtime_mode"] = "native; upstream startup preferences snapshotted and restored"
        if spec["id"] == "pro":
            spec["source"]["source_tree_sha256"] = source_tree_sha256(root / "src/solidworks_mcp_pro")
        elif spec["id"] == "pro_v020":
            spec["source"]["source_tree_sha256"] = source_tree_sha256(root / ".local/pro-v0.2.0/src/solidworks_mcp_pro")
    run_id = uuid4().hex[:12]
    folder = root / "artifacts/benchmarks/live" / run_id
    warmups, seed = getattr(args, "warmup", 1), getattr(args, "seed", 20261103)
    suite = await execute_suite(selected, args.repeats, folder, warmups=warmups, seed=seed)
    report = {"schema_version": 1, "generated_at": datetime.now(timezone.utc).isoformat(),
              "benchmark": "serial-live-cad", "run_id": run_id, "repeats": args.repeats,
              "warmup_repetitions": warmups,
              "participants": [{key: spec.get(key) for key in ("id", "name", "source", "runtime_mode")} for spec in selected],
              "order_design": "Seeded Williams/Latin square. Six systems and six repetitions balance every position and every within-row ordered adjacent pair once.",
              "fixture_design": "Measured CAD document is closed before policy probes. Policies use a separate new block document; setup/cleanup and policy calls excluded from CAD timing.",
              "methodology": "Unmodified competitor sources; same SW instance; fresh documents; retained warm-ups excluded from timing; seeded counterbalanced order; independent COM mass/bounds oracle; MCP latencies exclude oracle time. No LLM inference. Policies scored separately.",
              **suite}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(sanitize_report(report), indent=2))
    print(str(args.output), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", required=True)
    parser.add_argument("--servers", default="pro,pro_v020,hjbaard,harrier,soheil,andrew")
    parser.add_argument("--repeats", type=int, default=6, choices=range(1, 11))
    parser.add_argument("--warmup", type=int, default=1, choices=range(0, 6), help="Full retained warm-up scenarios per adapter; excluded from measured statistics")
    parser.add_argument("--seed", type=int, default=20261103)
    parser.add_argument("--output", type=Path, default=Path("benchmarks/results/performance-balanced.json"))
    asyncio.run(run(parser.parse_args()))
