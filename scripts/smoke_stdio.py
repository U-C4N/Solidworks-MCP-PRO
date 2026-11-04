"""Protocol smoke; --live creates/saves a new plate, --render also runs Visualize.

Never closes user documents. Outputs evidence under artifacts/acceptance/<run>.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
from pathlib import Path
import sys
from uuid import uuid4

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from solidworks_mcp_pro.server import pro_tools


async def run(live=False, render=False):
    root = Path(__file__).resolve().parents[1]
    output = root / "artifacts" / "acceptance" / uuid4().hex[:12]
    output.mkdir(parents=True)
    records = []
    parameters = StdioServerParameters(command=sys.executable, args=["-m", "solidworks_mcp_pro", "--workspace", str(output)])
    try:
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                catalog = await session.list_tools()
                assert len(catalog.tools) == 88 + len(pro_tools()), len(catalog.tools)
                assert init.serverInfo.name == "solidworks-mcp-pro"
                assert len((await session.list_prompts()).prompts) == 1
                assert (await session.read_resource("solidworks-pro://guide")).contents
                print(f"Protocol initialize / {len(catalog.tools)} tools / prompts / guide: PASS", flush=True)

                async def call(name, args=None, *, allow_error=False):
                    result = await session.call_tool(name, args or {})
                    data = result.structuredContent
                    if data is None:
                        data = json.loads(next(c.text for c in result.content if c.type == "text"))
                    while data.get("operation_status") == "running":
                        print(f"{name}: running {data['operation_id']}", flush=True)
                        result = await session.call_tool("pro_operation", {"operation_id": data["operation_id"], "wait_seconds": 10})
                        data = result.structuredContent
                    records.append({"tool": name, "arguments": args or {}, "result": data,
                                    "content_types": [c.type for c in result.content]})
                    label = "EXPECTED REJECTION" if result.isError and allow_error else ("FAIL" if result.isError else "PASS")
                    print(f"{name}: {label}", flush=True)
                    if result.isError and not allow_error:
                        raise RuntimeError(json.dumps(data))
                    return data

                await call("pro_capabilities")
                invalid = await call("pro_run_plan", {"steps": [{"tool": "add_box", "arguments": {}}],
                                                       "request_id": "invalid", "dry_run": True}, allow_error=True)
                assert invalid.get("ok") is False
                if live:
                    await call("get_status")
                    steps = [{"tool": "new_part", "arguments": {}},
                             {"tool": "add_box", "arguments": {"width_mm": 80, "height_mm": 50, "depth_mm": 8}},
                             {"tool": "add_hole", "arguments": {"diameter_mm": 10, "x_mm": 40, "y_mm": 25}},
                             {"tool": "set_appearance", "arguments": {"rgb": [35, 110, 180]}}]
                    await call("pro_run_plan", {"steps": steps, "request_id": "plate", "dry_run": True})
                    plan = await call("pro_run_plan", {"steps": steps, "request_id": "plate", "dry_run": False})
                    repeated = await call("pro_run_plan", {"steps": steps, "request_id": "plate", "dry_run": False})
                    assert repeated["operation_id"] == plan["operation_id"]
                    await call("pro_verify_volume", {"expected_mm3": 80 * 50 * 8 - math.pi * 25 * 8})
                    await call("save_part", {"path": str(output / "acceptance-plate.sldprt")})
                    await call("export", {"path": str(output / "acceptance-plate.step")})
                    await call("pro_capture_view")
                    preview = await call("pro_preview_model")
                    await session.read_resource(preview["resource_uri"])
                    status = await call("pro_visualize_status", {"load_if_missing": render}, allow_error=not render)
                    if render:
                        assert status["available"]
                        await call("pro_visualize_render", {"request_id": "render-plate", "width": 640,
                                                            "height": 360, "passes": 50, "image_format": "png"})
                        await call("pro_visualize_export", {"grouping": "part"})
    finally:
        (output / "evidence.json").write_text(json.dumps(records, indent=2, ensure_ascii=True), encoding="utf-8")
        print("Evidence: " + str(output / "evidence.json"), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--render", action="store_true")
    options = parser.parse_args()
    asyncio.run(run(options.live or options.render, options.render))
