"""Measure MCP discovery only; this runner never invokes CAD tools."""
from __future__ import annotations

import argparse
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import statistics
import sys
import tempfile
import time

from jsonschema.validators import validator_for
from mcp import ClientSession, StdioServerParameters, types
from mcp.client.stdio import stdio_client


def _error_text(error: BaseException) -> str:
    nested = getattr(error, "exceptions", None)
    if nested:
        return "; ".join(_error_text(item) for item in nested)[:4000]
    return f"{type(error).__name__}: {error}"[:4000]


def _seconds(spec: dict, key: str, default: int = 30) -> float:
    value = spec.get(key, default)
    if type(value) not in (float, int) or not math.isfinite(value) or not 0 < value <= 600:
        raise ValueError(f"{key} must be finite and between 0 and 600 seconds")
    return float(value)


@asynccontextmanager
async def connect(spec: dict):
    """Launch one stdio process, initialize it, and yield its official SDK session.

    The session has ``benchmark_initialize`` metadata with elapsed_ms and the
    initialize response. This context manager does not invoke a tool. It does
    not impose an overall session timeout; native calls may continue until the
    caller explicitly ends the context.
    """
    command = spec.get("command")
    arguments = spec.get("args", [])
    environment = spec.get("env", {})
    required_files = spec.get("required_files", [])
    if not isinstance(command, str) or not command:
        raise ValueError("A nonempty executable command is required")
    if not isinstance(arguments, list) or not all(isinstance(arg, str) for arg in arguments):
        raise ValueError("args must be an array of strings")
    if not isinstance(environment, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in environment.items()):
        raise ValueError("env must contain only string keys and values")
    if not isinstance(required_files, list) or not all(isinstance(path, str) and path for path in required_files):
        raise ValueError("required_files must be an array of nonempty absolute file paths")
    for value in required_files:
        path = Path(value)
        if not path.is_absolute():
            raise ValueError(f"Required file path must be absolute: {value}")
        if not path.is_file():
            raise ValueError(f"Required file missing; refusing to launch a potentially different implementation: {value}")
    parameters = StdioServerParameters(command=command, args=arguments, cwd=spec.get("cwd"), env={**os.environ, **environment})
    startup_timeout = _seconds(spec, "startup_timeout_seconds")
    request_timeout = _seconds(spec, "request_timeout_seconds")
    started = time.perf_counter()
    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=request_timeout),
                                 client_info=types.Implementation(name="solidworks-mcp-pro-benchmark", version="1.0")) as session:
            initialized = await asyncio.wait_for(session.initialize(), timeout=startup_timeout)
            session.benchmark_initialize = {
                "status": "measured",
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                "response": initialized.model_dump(mode="json", by_alias=True, exclude_none=True),
            }
            yield session


def _unpack_payload(value, depth=0, error_flags=None):
    if depth > 8:
        return value
    if isinstance(value, dict):
        if error_flags is not None and (value.get("isError") is True or value.get("ok") is False or value.get("success") is False):
            error_flags.append(True)
        if isinstance(value.get("structuredContent"), dict):
            return _unpack_payload(value["structuredContent"], depth + 1, error_flags)
        if set(value) == {"result"}:
            return _unpack_payload(value["result"], depth + 1, error_flags)
        if isinstance(value.get("content"), list) and set(value).issubset({"content", "isError", "_meta"}):
            for block in value["content"]:
                if isinstance(block, dict) and block.get("type") == "text":
                    try:
                        parsed = json.loads(block["text"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    if isinstance(parsed, (dict, list)):
                        return _unpack_payload(parsed, depth + 1, error_flags)
    return value


def normalize_result(result) -> dict:
    """Normalize official structuredContent and upstream JSON-in-text envelopes."""
    envelope = result.model_dump(mode="json", by_alias=True, exclude_none=True) if hasattr(result, "model_dump") else result
    error_flags = []
    output = _unpack_payload(envelope, error_flags=error_flags)
    error = bool(error_flags)
    if isinstance(output, dict):
        error = error or output.get("ok") is False or output.get("isError") is True or output.get("success") is False
    return {"is_error": error, "output": output}


async def timed_call(session: ClientSession, name: str, arguments: dict, *, timeout_seconds: float | None = None) -> dict:
    """Measure one explicitly requested tool call; never retry on any outcome.

    This helper is for separate live harnesses. The discovery runner below
    never calls it. A timeout is indeterminate, not proof of native rollback.
    """
    started = time.perf_counter()
    try:
        options = {} if timeout_seconds is None else {"read_timeout_seconds": timedelta(seconds=timeout_seconds)}
        result = await session.call_tool(name, arguments, **options)
        normalized = normalize_result(result)
        return {"tool": name, "status": "fail" if normalized["is_error"] else "pass", "measurement_status": "measured",
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3), **normalized}
    except Exception as exc:
        return {"tool": name, "status": "fail", "measurement_status": "measured", "is_error": True,
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                "error": _error_text(exc), "outcome_may_be_indeterminate": True, "automatic_retry": False}


def statistics_ms(samples: list[float]) -> dict:
    return {"status": "measured", "sample_count": len(samples), "samples_ms": samples,
            "median_ms": round(statistics.median(samples), 3), "min_ms": min(samples), "max_ms": max(samples)}


async def _list_all(session, method: str, field: str):
    cursor = None
    seen = set()
    entries = []
    for _ in range(100):
        result = await getattr(session, method)(cursor=cursor)
        entries.extend(getattr(result, field))
        if len(entries) > 10_000:
            raise ValueError("Discovery exceeds 10,000 entries")
        cursor = result.nextCursor
        if not cursor:
            return entries
        if cursor in seen:
            raise ValueError("Discovery pagination repeated a cursor")
        seen.add(cursor)
    raise ValueError("Discovery exceeds 100 pages")


def inspect_catalog(catalog) -> dict:
    schemas = []
    invalid = []
    missing_descriptions = []
    missing_annotations = []
    unknown_fields = {"explicitly_forbidden": 0, "allowed_or_unspecified": 0, "schema_defined": 0}
    for tool in catalog:
        schemas.append(tool.inputSchema)
        if not tool.description or not tool.description.strip():
            missing_descriptions.append(tool.name)
        if tool.annotations is None:
            missing_annotations.append(tool.name)
        try:
            validator_for(tool.inputSchema).check_schema(tool.inputSchema)
        except Exception as exc:
            invalid.append({"tool": tool.name, "error": _error_text(exc)})
        additional = tool.inputSchema.get("additionalProperties", True)
        if additional is False:
            unknown_fields["explicitly_forbidden"] += 1
        elif isinstance(additional, dict):
            unknown_fields["schema_defined"] += 1
        else:
            unknown_fields["allowed_or_unspecified"] += 1
    schema_bytes = len(json.dumps(schemas, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    catalog_bytes = len(json.dumps([tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in catalog],
                                  ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    return {"tool_count": len(catalog), "input_schema_bytes": schema_bytes, "catalog_bytes": catalog_bytes,
            "schema_validation": {"status": "fail" if invalid else "pass", "invalid": invalid},
            "missing_descriptions": missing_descriptions, "missing_annotations": missing_annotations,
            "unknown_argument_handling": {"status": "not_run", "reason": "Discovery-only run; no tools invoked. Static schema declarations do not prove runtime rejection.",
                                          "static_top_level_additional_properties": unknown_fields},
            "tool_names": [tool.name for tool in catalog]}


async def measure_server(spec: dict, repeats: int = 5, catalog_dir: Path | None = None) -> dict:
    if type(repeats) is not int or not 1 <= repeats <= 25:
        raise ValueError("repeats must be an integer between 1 and 25")
    report = {"id": spec.get("id", "unnamed"), "name": spec.get("name", spec.get("id", "unnamed")),
              "source": spec.get("source", {}), "runtime_mode": spec.get("runtime_mode", "unspecified"), "cad_tools_invoked": 0}
    if spec.get("enabled", True) is False or not spec.get("command"):
        return {**report, "status": "not_run", "reason": spec.get("not_run_reason", "No enabled executable configured")}
    try:
        async with connect(spec) as session:
            report["initialization"] = session.benchmark_initialize
            capabilities = session.benchmark_initialize["response"]["capabilities"]
            if "tools" in capabilities:
                samples = []
                counts = []
                catalog = []
                for _ in range(repeats):
                    started = time.perf_counter()
                    catalog = await _list_all(session, "list_tools", "tools")
                    samples.append(round((time.perf_counter() - started) * 1000, 3))
                    counts.append(len(catalog))
                report["tools_list"] = {**statistics_ms(samples), "stable_tool_count": len(set(counts)) == 1,
                                        "tool_counts": counts, **inspect_catalog(catalog)}
                if catalog_dir is not None:
                    identifier = spec.get("id", "unnamed")
                    if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", identifier):
                        raise ValueError("Catalog server IDs must contain 1..100 ASCII letters, digits, dots, underscores or hyphens")
                    catalog_dir.mkdir(parents=True, exist_ok=True)
                    catalog_path = catalog_dir / f"catalog-{identifier}.json"
                    snapshot = {"server_id": identifier, "source": spec.get("source", {}), "runtime_mode": spec.get("runtime_mode", "unspecified"),
                                "tools": [tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in catalog]}
                    catalog_path.write_text(json.dumps(sanitize_report(snapshot), indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
                    report["tools_list"]["catalog_file"] = str(catalog_path.resolve())
            else:
                report["tools_list"] = {"status": "unsupported", "reason": "Server does not declare tools capability"}
            for capability, method, field in [("resources", "list_resources", "resources"), ("prompts", "list_prompts", "prompts")]:
                if capability not in capabilities:
                    report[capability] = {"status": "unsupported", "reason": f"Server does not declare {capability} capability"}
                    continue
                started = time.perf_counter()
                try:
                    values = await _list_all(session, method, field)
                    report[capability] = {"status": "measured", "count": len(values), "elapsed_ms": round((time.perf_counter() - started) * 1000, 3)}
                except Exception as exc:
                    report[capability] = {"status": "fail", "error": _error_text(exc)}
            report["status"] = "measured"
    except Exception as exc:
        report["status"] = "fail"
        report["error"] = _error_text(exc)
    return report


def sanitize_report(value, replacements: dict[str, str] | None = None):
    """Replace task/user/temp roots in report values without changing public URLs."""
    roots = replacements if replacements is not None else {
        str(Path(__file__).resolve().parents[1]): "<REPO>",
        tempfile.gettempdir(): "<TEMP>",
        str(Path.home()): "<HOME>",
    }
    if isinstance(value, str):
        for path, replacement in sorted(roots.items(), key=lambda item: len(item[0]), reverse=True):
            variants = {path, path.replace("\\", "/"), path.replace("/", "\\")}
            for variant in sorted(variants, key=len, reverse=True):
                value = re.sub(re.escape(variant), lambda _: replacement, value, flags=re.IGNORECASE if os.name == "nt" else 0)
        return value
    if isinstance(value, list):
        return [sanitize_report(item, roots) for item in value]
    if isinstance(value, dict):
        return {key: sanitize_report(item, roots) for key, item in value.items()}
    return value


async def run_manifest(manifest: dict, repeats: int = 5, catalog_dir: Path | None = None) -> dict:
    if manifest.get("schema_version") != 1 or not isinstance(manifest.get("servers"), list):
        raise ValueError("Expected schema_version=1 and a servers array")
    ids = [spec.get("id") for spec in manifest["servers"]]
    if any(not isinstance(item, str) or not item for item in ids) or len(set(ids)) != len(ids):
        raise ValueError("Server IDs must be unique nonempty strings")
    # Sequential subprocesses reduce interference and make lifecycle attributable.
    results = [await measure_server(spec, repeats, catalog_dir) for spec in manifest["servers"]]
    return sanitize_report({"schema_version": 1, "benchmark": "mcp-discovery", "generated_at": datetime.now(timezone.utc).isoformat(),
                            "manifest_as_of": manifest.get("as_of"), "repeats": repeats,
                            "methodology": "One cold process initialization and repeated sequential tools/list requests. Discovery only; zero native CAD calls.",
                            "source_evidence": "Source revisions are supplied by the manifest; this runner does not independently verify Git revisions.",
                            "servers": results})


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--catalog-dir", type=Path, help="Tool schema snapshot folder (default: output JSON parent)")
    args = parser.parse_args(argv)
    report = asyncio.run(run_manifest(json.loads(args.manifest.read_text(encoding="utf-8-sig")), args.repeats, args.catalog_dir or args.output.parent))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=True, allow_nan=False) + "\n", encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="backslashreplace")
    print(args.output.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
