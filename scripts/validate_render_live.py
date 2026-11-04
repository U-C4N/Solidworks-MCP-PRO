"""Explicit native render acceptance; preserve user files and distinguish routes."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys
from uuid import uuid4

from solidworks_mcp_pro.render_validation import validate_render_live, validation_exit_code


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="explicitly authorize this native acceptance probe")
    parser.add_argument("--route", choices=("addin", "project"), default="addin",
                        help="addin: empty-session scratch part; project: owned copy, without CAD attachment")
    parser.add_argument("--project", type=Path, help="existing .svpj, required only for --route project")
    parser.add_argument("--load-addin", action="store_true", help="explicitly enable the add-in in the addin route")
    parser.add_argument("--width", type=int, default=640)
    parser.add_argument("--height", type=int, default=360)
    parser.add_argument("--passes", type=int, default=16)
    parser.add_argument("--no-denoiser", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if not args.live:
        parser.error("--live is required; without it no CAD or renderer is invoked")
    if (args.route == "project") != (args.project is not None):
        parser.error("--project is required only for --route project")
    if args.route == "project" and args.load_addin:
        parser.error("--route project does not load or attach to the CAD add-in")
    root = Path(__file__).resolve().parents[1]
    output = args.output or root / "artifacts" / "render-live" / uuid4().hex[:12]
    try:
        evidence = asyncio.run(validate_render_live(output.resolve(), route=args.route,
            project_path=args.project.resolve() if args.project is not None else None,
            width=args.width, height=args.height, passes=args.passes, denoiser=not args.no_denoiser,
            load_addin=args.load_addin))
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps({key: evidence.get(key) for key in (
        "ok", "status", "route", "execution", "render_verified", "addin_render_verified",
        "project_render_verified", "macro_render_verified", "code", "error", "evidence_path")}, ensure_ascii=True))
    return validation_exit_code(evidence)


if __name__ == "__main__":
    sys.exit(main())
