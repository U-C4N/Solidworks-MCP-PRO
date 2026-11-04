"""Render benchmark evidence as Markdown without inventing missing measurements."""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def _cell(value):
    return str(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _number(value):
    return "—" if value is None else f"{value:,.3f}"


def render_markdown(report: dict) -> str:
    rows = ["# MCP discovery benchmark", "", f"Measured at: {_cell(report.get('generated_at', 'not recorded'))}", "",
            "| Server | Runtime mode | Status | Initialize ms | tools/list median ms | Min–max ms | Tools | Input schemas KiB |",
            "|---|---|---|---:|---:|---:|---:|---:|"]
    for server in report.get("servers", []):
        initialization = server.get("initialization", {})
        tools = server.get("tools_list", {})
        interval = f"{_number(tools.get('min_ms'))}–{_number(tools.get('max_ms'))}" if tools.get("status") == "measured" else "—"
        size = tools.get("input_schema_bytes")
        rows.append("| " + " | ".join([
            _cell(server.get("name", server.get("id", "unknown"))), _cell(server.get("runtime_mode", "unspecified")), _cell(server.get("status", "not_run")),
            _number(initialization.get("elapsed_ms")), _number(tools.get("median_ms")), interval,
            str(tools.get("tool_count", "—")), _number(size / 1024 if size is not None else None),
        ]) + " |")
    rows += ["", "One process initialization per server; tools/list repeats are warm requests within that process. These timings measure this machine and run, not general CAD speed or correctness.",
             "", "No CAD tools were invoked by this discovery runner. Mock-mode discovery establishes catalog/protocol behavior only; it is not native CAD evidence. Runtime unknown-argument rejection, geometry accuracy, rendering fidelity, recovery, and document safety require separately reported live tests. Unsupported and unrun features are never counted as passes.", "", "## Evidence and conformance", ""]
    for server in report.get("servers", []):
        rows.append(f"### {_cell(server.get('name', server.get('id', 'unknown')))}")
        rows.append("")
        source = server.get("source", {})
        rows.append(f"Source: {_cell(source.get('repository', 'not supplied'))}; revision: `{_cell(source.get('revision', 'not supplied'))}`; license: {_cell(source.get('license', 'not supplied'))}.")
        tools = server.get("tools_list", {})
        if server.get("status") == "not_run":
            rows.append(f"Not run: {_cell(server.get('reason', 'unspecified'))}.")
        elif server.get("status") == "fail":
            rows.append(f"Failure: {_cell(server.get('error', 'unspecified'))}.")
        if tools.get("status") == "measured":
            validation = tools.get("schema_validation", {})
            rows.append(f"Schema validation: **{_cell(validation.get('status', 'not_run'))}**. Missing descriptions: {len(tools.get('missing_descriptions', []))}; missing annotations: {len(tools.get('missing_annotations', []))}. Stable tool count across samples: {tools.get('stable_tool_count')}.")
            rows.append(f"Resources: {_cell(server.get('resources', {}).get('status', 'not_run'))}; prompts: {_cell(server.get('prompts', {}).get('status', 'not_run'))}. Unknown argument runtime handling: **not_run**.")
            for issue in validation.get("invalid", []):
                rows.append(f"- Invalid schema `{_cell(issue['tool'])}`: {_cell(issue['error'])}")
        rows.append("")
    rows += ["Source revisions are declared in the input manifest. See its primary-source evidence for verification. Tool count is an inventory measure; it is not an overall quality ranking.", ""]
    return "\n".join(rows)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    data = json.loads(args.input.read_text(encoding="utf-8"))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render_markdown(data), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
