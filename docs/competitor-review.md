# Competitor benchmark preparation and source review

Checked 2026-11-03. Initial preparation covered three independent implementations; the completed comparison also includes the hjbaard upstream used by PRO and a frozen PRO 0.1.0 baseline. No CAD tool was invoked during preparation. The separate serialized live harness subsequently tested all four rivals: see [live results](benchmark-results.md) and [final six-server protocol results](../benchmarks/results/protocol-all.md). Andrew discovery used its upstream mock adapter; its recorded live CAD run used the native adapter. Discovery success is not evidence of CAD correctness.

## Revisions and reproducibility

| Implementation | Pinned revision | Commit UTC | Release/package identity | License |
|---|---|---|---|---|
| [hjbaard](https://github.com/hjbaard/SolidWorks-MCP/tree/ddcefc0cfa056002ff8eb8bdb186818ad52dc2f1) | `ddcefc0cfa056002ff8eb8bdb186818ad52dc2f1` | 2026-11-03 06:44:07 | PyPI solidworks-mcp 0.19.0 / release v0.19.0 | MIT |
| [HarrierPigeon](https://github.com/HarrierPigeon/Solidworks-MCP-Server/tree/60c11fe45b6cfc60c4e2da8ad41fc725a48bf8ea) | `60c11fe45b6cfc60c4e2da8ad41fc725a48bf8ea` | 2026-08-07 07:44:34 | Release v0.4.0; source pyproject reports 0.2.0 | MIT |
| [soheil-mp](https://github.com/soheil-mp/solidworks-mcp/tree/79fb529b67ca4a0587e5570024a772fad0b2c14d) | `79fb529b67ca4a0587e5570024a772fad0b2c14d` | 2026-10-31 22:01:10 | Installed 0.1.0-79fb529 build; no published release found in original API snapshot | MIT |
| [Andrew Bartels](https://github.com/andrewbartels1/SolidworksMCP-python/tree/126beed0e7a13214ebdd5724929f4c095cf2642f) | `126beed0e7a13214ebdd5724929f4c095cf2642f` | 2026-11-03 05:52:57 | Version 1.5.0; release published 2026-11-02 02:44:37 UTC | MIT |

Each local HEAD matched a fresh `git ls-remote <repository> HEAD` and the earlier `docs/research-snapshot.json`. `git diff --quiet` returned zero for all three tracked source trees. Their source code was not patched. Build outputs, local virtual environments and runtime directories are separate untracked preparation artifacts.

Exact machine-local launch commands, arguments, working directories and environment variables are in `benchmarks/competitors.json`. They deliberately contain absolute local paths and must be regenerated or edited on another machine. `benchmarks/competitors.py` translates benchmark operations to actual upstream tool names and arguments; it does not import or modify competitor code. All 45 generated examples (15 operations per implementation) were validated against the independently discovered tool schemas.

Harrier was cloned into `.local/competitors/harrier`, with Python 3.12.16 and an isolated environment containing MCP 1.30.0, pywin32 311 and Pillow 12.2.0. These satisfy its declared dependencies. Its static setuptools package declaration and server startup were inspected before installation. `server.py` initializes the Python COM apartment and tool modules but does not connect to SOLIDWORKS until a tool call. Its optional development/watch scripts and CAD tests were not run. The MCP server advertises version 1.30.0, which matches the SDK, not the repository release; comparisons identify the commit instead.

Soheil uses the already installed, framework-dependent .NET 10 binary at `%LOCALAPPDATA%/solidworks-mcp/bin/0.1.0-79fb529/solidworks-mcp.dll`, launched with that installation's own `dotnet/dotnet.exe`. Source resides in `%LOCALAPPDATA%/solidworks-mcp/source`. Startup registers tools and a lazy session; it does not attach to CAD at discovery. `SWMCP_AUTO_LAUNCH=off`, `SWMCP_VBA=off` and a new `SWMCP_DATA_DIR` isolate benchmark behavior. No source rebuild or installation change was needed.

Andrew was cloned into `.local/competitors/andrew`. Its declared minimum is Python 3.13. An isolated Python 3.13.18 environment was prepared with uv 0.13.7, and its inspected Hatch metadata build produced an editable 1.5.0 installation. The repository's dependency ranges resolved to FastMCP 4.1.6, MCP 2.3.0, pywin32 312 and comtypes 1.4.17. No dependency compatibility patch was required. Exact installed dependency lists are under each Python competitor's `benchmark-dependencies.txt`. A project-local data directory and empty model-provider API-key environment values keep this discovery run independent of the user's account configuration.

## Measured protocol evidence

The initial preparation record is `artifacts/benchmarks/competitors-protocol.json`, with full schemas alongside it. This artifact directory is not bundled. The final bundled discovery evidence is `benchmarks/results/protocol-all.json` and its `catalog-*.json` files; use those for the final six-server comparison. The earlier preparation run, retained here as historical context, reported:

| Implementation | Mode | Initialize ms | tools/list median ms, n=5 | Tools | Catalog JSON bytes |
|---|---|---:|---:|---:|---:|
| Harrier | Native server, no CAD calls | 1,227.480 | 6.302 | 95 | 66,881 |
| Soheil | Native server, no CAD calls | 602.524 | 36.812 | 161 | 234,279 |
| Andrew | Upstream mock adapter | 10,236.758 | 72.597 | 154 | 175,223 |

All three initialized, exposed stable tool counts, supplied tool descriptions, and returned JSON Schemas accepted by the validator. Soheil supplied annotations for all 161 tools; Harrier and Andrew supplied none in these catalogs. Andrew explicitly forbids unknown top-level arguments; many nested compatibility input objects allow them. The other two catalogs leave top-level additional properties unspecified. These are schema observations, not results of invalid live calls. Harrier's measured 95-tool runtime catalog takes precedence over README counts used during the earlier search.

These are one-machine discovery measurements, with one process start and five repeated catalog requests each. Cold import caches, concurrent preparation activity, runtime/language and mock/native differences affect timing. They do not establish a general speed ranking or native modeling reliability. No simulation, rendering, screenshot fidelity, dimension correctness or timeout recovery is inferred from discovery. Native scenario evidence belongs to the separate serialized live harness and its saved oracle outputs.

## Source-verified operation mappings

Coordinates and lengths below are millimeters. The live harness must preserve returned sketch, feature, dimension and document identities instead of guessing localized names.

| Operation | Harrier | Soheil | Andrew |
|---|---|---|---|
| New part | `solidworks_new_part {}` | `doc_new {type:"part",units:"mmgs",name:...}` | `create_part {input_data:{name:...,units:"mm"}}` |
| Start sketch | `solidworks_create_sketch {plane:"Front"}` | `sketch_start {on:{name:"front"}}` | `create_sketch {input_data:{plane:"Front"}}` |
| Rectangle | `solidworks_sketch_rectangle {width,height,centerX,centerY}` | `sketch_add {entities:[{kind:"rectangle",center:[x,y],widthMm,heightMm}]}` | `add_rectangle {input_data:{x1,y1,x2,y2}}` |
| Circle | `solidworks_sketch_circle {centerX,centerY,radius}` | `sketch_add {entities:[{kind:"circle",center:[x,y],radiusMm}]}` | `add_circle {input_data:{center_x,center_y,radius}}` |
| Finish sketch | `solidworks_exit_sketch {}` | `sketch_finish {}` | `exit_sketch {}` |
| Boss | `solidworks_create_extrusion {depth}` | `feature_extrude {depthMm}` | `create_extrusion {input_data:{sketch_name,depth}}` |
| Through cut | `solidworks_create_cut_extrusion {depth,endCondition:"THROUGH_ALL"}` | `feature_extrude {cut:true,end:"through_all"}` | `create_cut_extrude {input_data:{sketch_name,depth,end_condition:"ThroughAll"}}` |
| Mass / volume | `solidworks_get_mass_properties {}` | `model_mass {}` | `get_mass_properties {}` |
| Bounds | `solidworks_get_body_info {}` | `body_list {}` | `verify_model {}` (report mode) |
| Enumerate dimensions | `solidworks_list_parameters {}` | `dimension_list {}` | `list_model_dimensions {}` |
| Change dimension | `solidworks_set_parameter {parameter,value,unit:"mm"}` | `dimension_set {values:[{name,value}]}` | `set_dimension {input_data:{name,value}}` |
| STEP / STL export | `solidworks_save_document {path}` | `model_export {path}` | `export_step` / `export_stl`, `{input_data:{file_path,format_type}}` |
| Screenshot | `solidworks_look_at_model {view:"isometric",labels:false}` | `view_screenshot {view:"iso",file:...}` | `export_image {input_data:{file_path,format_type:"png",view_orientation:"isometric",width:1024,height:768}}` |
| Native save | `solidworks_save_document {path}` | `doc_save {mode:"as",path}` | `save_as {input_data:{file_path,overwrite:false}}` |
| Close own part | `solidworks_close_document {name:exactTitle}` | `doc_close {doc:ownedHandle,discard:true}` | `close_model {input_data:{save:false}}`, after identity check |

For Soheil, pass the returned `doc` handle on each subsequent operation. Harrier's tools predominantly use the active document; its close tool discards unsaved edits. Andrew's close tool uses its current model object and has no document-name argument. Therefore the harness must verify the target is its newly created benchmark document before invoking either close operation. It must never use any implementation's close-all utilities.

Harrier internally retries a cut once with the opposite direction if the first call returns no feature. Andrew's cut implementation also documents direction fallback. Those are upstream behaviors, not hidden benchmark retries. A failed or timed-out mutation must be checked with the independent CAD oracle before any additional mutation.

The mappings are grounded in the pinned `solidworks/{modeling,sketching,document_manager,geometry_query}.py` files for Harrier; `src/SolidWorksMcp.Server/Tools/{Document,Sketch,Feature,FeatureManagement,Measure,Export,View}Tools.cs` for Soheil; and `src/solidworks_mcp/tools/{modeling,sketching,analysis,export,file_management}.py` for Andrew, plus the discovered schemas. The actual Andrew unit aliases were checked in `adapters/solidworks/io.py`: use `mm`, not `mmgs`.

## Native startup considerations and useful comparisons

Andrew's `server.start()` calls `adapter.connect()` even before stdio starts. Its native connection sets SOLIDWORKS visible and changes user preference toggles 150, 149, 10, 372, 520 and 642; its disconnect explicitly leaves those preferences unchanged. The native harness should snapshot and restore them outside the measured scenario. The manifest's `--mock` must be removed only for the serialized native test. The mock startup log says "Connected to SolidWorks" even though the selected adapter is mock; this message alone is not evidence of a native connection.

Soheil provides strong source-level comparison targets: document ownership, verified feature creation, batch dimension edits with readback and rollback, export-file verification, and screenshot view restoration. Only the subset exercised by the live harness has comparative native evidence; the rest remains source-level assessment. Harrier offers compact, direct modeling tools and a comparatively small runtime catalog. Andrew offers broad tools and typed inputs with compatibility aliases, and its latest commit adds installed-type-library API lookup. These are useful strengths to assess independently; tool count alone is not a quality metric.

For PRO, prioritize measured scenario gaps in dimension readback, precise output verification, CAD state preservation and number of client round trips. Separate a missing capability from a broken supported call, an adapter mistake, a prerequisite failure and a mock-only result. Record the original failure before any compatibility workaround; label any later adapted run separately. No global superiority claim is supported by this preparation report.

## Reproducing the comparison on another Windows machine

The source ZIP contains the benchmark code, manifests and sanitized evidence under `benchmarks/results`. It **does not contain** `.venv`, `.local`, `artifacts` or `dist`. Consequently it does not contain the competitor checkouts/environments, the installed Soheil binary, the frozen baseline source, the original rendered/CAD output files, or the baseline wheel. The existing manifests contain this machine's absolute paths; they are examples, not portable launch configurations. The recorded JSON results remain inspectable without those installations.

Prerequisites for a fresh native run are Windows, Git, Python, .NET 10 SDK, a licensed installed SOLIDWORKS instance and its `api/redist` interop assemblies. The recorded CAD run used SOLIDWORKS revision 34.4.1 (2026). Visualize is an additional installed/licensed requirement for the separate render acceptance, not for the common CAD benchmark. These commands describe a **fresh checkout**, create local build environments, and do not launch CAD. Run them from the PRO repository root. Do not replace an existing checkout or its uncommitted work with these setup instructions.

```powershell
py -m venv .local\benchmark-bootstrap
& .local\benchmark-bootstrap\Scripts\python.exe -m pip install uv==0.13.7
$benchUv = (Resolve-Path .local\benchmark-bootstrap\Scripts\uv.exe).Path
& $benchUv python install 3.12.16 3.13.18
& $benchUv venv --python 3.12.16 .venv
& $benchUv pip install --python .venv\Scripts\python.exe -e .

# hjbaard: the actual measured package is already pinned by PRO's dependency.
& $benchUv pip install --python .venv\Scripts\python.exe solidworks-mcp==0.19.0 mcp==1.30.0 pywin32==312 Pillow==12.3.0 comtypes==1.4.17
# Audit its corresponding pinned repository without replacing PRO's dependency.
git clone https://github.com/hjbaard/SolidWorks-MCP.git .local\competitors\hjbaard
git -C .local\competitors\hjbaard checkout --detach ddcefc0cfa056002ff8eb8bdb186818ad52dc2f1

git clone https://github.com/HarrierPigeon/Solidworks-MCP-Server.git .local\competitors\harrier
git -C .local\competitors\harrier checkout --detach 60c11fe45b6cfc60c4e2da8ad41fc725a48bf8ea
& $benchUv venv --python 3.12.16 .local\competitors\harrier\.venv
& $benchUv pip install --python .local\competitors\harrier\.venv\Scripts\python.exe mcp==1.30.0 pywin32==311 Pillow==12.2.0

git clone https://github.com/andrewbartels1/SolidworksMCP-python.git .local\competitors\andrew
git -C .local\competitors\andrew checkout --detach 126beed0e7a13214ebdd5724929f4c095cf2642f
& $benchUv venv --python 3.13.18 .local\competitors\andrew\.venv
& $benchUv pip install --python .local\competitors\andrew\.venv\Scripts\python.exe -e .local\competitors\andrew fastmcp==4.1.6 mcp==2.3.0 pywin32==312 comtypes==1.4.17 pydantic-ai-slim==1.107.5

git clone https://github.com/soheil-mp/solidworks-mcp.git .local\competitors\soheil
git -C .local\competitors\soheil checkout --detach 79fb529b67ca4a0587e5570024a772fad0b2c14d
$env:SOLIDWORKS_DIR = 'C:\Program Files\SOLIDWORKS Corp\SOLIDWORKS'
Push-Location .local\competitors\soheil
dotnet publish src\SolidWorksMcp.Server -c Release -o bin
Pop-Location
```

The Soheil publish command follows its pinned project and README: target `net10.0-windows`, SDK 10.0.100 with `latestFeature` roll-forward, and embedded interop types from the local SOLIDWORKS installation. Set `SOLIDWORKS_DIR` to the real installation path. A fresh publish is not asserted to be byte-identical to this machine's previously installed binary. Keep the original revision in the manifest and separately record the SDK/runtime and binary hash used in the reproduced run. Clone/check out commands above are source acquisition, not execution of the repositories' setup scripts or CAD tests.

The commands pin the observed main runtime packages. Transitive dependencies can otherwise change. The complete two isolated Python resolutions are included in the appendix below; save those blocks as constraint files and add `--constraint <file>` to the corresponding `uv pip install` command when reproducing the original dependency versions. The original local `benchmark-dependencies.txt` files are excluded with `.local`. The original project environment was shared by hjbaard, current PRO and frozen PRO; the baseline comparison holds that dependency environment constant rather than evaluating each version with different dependencies.

### Restoring the frozen PRO baseline

The local original is `dist/solidworks_mcp_pro-0.1.0-py3-none-any.whl`. Its SHA-256 was rechecked as:

```text
37791b5f24ba5c4aa5b9c83f313cd33b59640e950e722eaaeedcde1e2d7768fc
```

**This wheel must be supplied separately.** A version string or rebuilding current 0.2.0 source as 0.1.0 does not recreate it. If the wheel is unavailable, mark `pro_before` disabled with an explicit reason in the protocol manifest and omit it from `--servers` for live runs. Do not treat missing baseline artifacts as a successful baseline test. In particular, an absent `PYTHONPATH` directory can otherwise make Python import the installed current PRO under the frozen label.

With the separately obtained wheel placed at the path above, verify and unpack it into the directory expected by the harness. This only imports the package's version module for verification; it does not initialize CAD:

```powershell
@'
import hashlib, os, subprocess, sys, zipfile
from pathlib import Path
wheel = Path('dist/solidworks_mcp_pro-0.1.0-py3-none-any.whl')
expected = '37791b5f24ba5c4aa5b9c83f313cd33b59640e950e722eaaeedcde1e2d7768fc'
assert hashlib.sha256(wheel.read_bytes()).hexdigest() == expected, 'Wrong baseline wheel'
target = Path('.local/pro-v0.1.0/src').resolve()
assert not target.exists(), 'Use an empty baseline target; do not replace existing files'
target.mkdir(parents=True)
with zipfile.ZipFile(wheel) as archive:
    archive.extractall(target)
env = dict(os.environ, PYTHONPATH=str(target))
check = "import solidworks_mcp_pro as p; from pathlib import Path; import os; assert p.__version__ == '0.1.0'; assert Path(p.__file__).resolve().is_relative_to(Path(os.environ['PYTHONPATH']).resolve()); print(p.__version__, p.__file__)"
subprocess.run([sys.executable, '-c', check], env=env, check=True)
'@ | .\.venv\Scripts\python.exe -
```

### Updating launch paths

Update both `benchmarks/competitors.json` and `benchmarks/protocol-all.json` before running discovery. The following local adaptation retains the source identity and environment policies while replacing the original machine's paths. It uses the freshly built Soheil binary. If the frozen package is unavailable it records `pro_before` as not run.

```powershell
@'
import json, shutil
from pathlib import Path
root = Path.cwd().resolve()
for filename in ('competitors.json', 'protocol-all.json'):
    path = root / 'benchmarks' / filename
    manifest = json.loads(path.read_text(encoding='utf-8-sig'))
    for spec in manifest['servers']:
        ident = spec['id']
        env = spec.setdefault('env', {})
        if ident in {'harrier', 'andrew'}:
            folder = root / '.local/competitors' / ident
            spec.update(command=str(folder / '.venv/Scripts/python.exe'), cwd=str(folder))
            if ident == 'harrier':
                spec['args'] = [str(folder / 'server.py')]
            else:
                env['SOLIDWORKS_MCP_DATA_DIR'] = str(folder / 'runtime')
                # Retain --mock for catalog discovery. run_live removes it explicitly.
        elif ident == 'soheil':
            folder = root / '.local/competitors/soheil'
            executable = shutil.which('dotnet')
            assert executable, '.NET must be available'
            spec.update(command=executable, args=[str(folder / 'bin/solidworks-mcp.dll')], cwd=str(folder))
            env['SWMCP_DATA_DIR'] = str(folder / 'runtime')
        else:
            spec.update(command=str(root / '.venv/Scripts/python.exe'), cwd=str(root))
            if ident in {'pro', 'pro_before'}:
                env['SW_MCP_PRO_WORKSPACE'] = str(root / 'artifacts/benchmarks' / ident)
            if ident == 'pro_before':
                frozen = root / '.local/pro-v0.1.0/src'
                env['PYTHONPATH'] = str(frozen)
                if not (frozen / 'solidworks_mcp_pro/__init__.py').is_file():
                    spec.update(enabled=False, not_run_reason='Original frozen wheel not supplied')
    path.write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
'@ | .\.venv\Scripts\python.exe -
```

The `pro` source-tree hash in the bundled original protocol manifest identifies the measured snapshot. If current source differs, recalculate that identity or remove the old hash and label the new run accordingly; do not attribute a different tree to the recorded hash. `run_live.py` constructs PRO and hjbaard launch paths relative to its repository root and reads the other three launch specifications from `competitors.json`.

The source operation map earlier in this document is a general translation aid. `run_live.py` is authoritative for the measured scenario: for example, it uses Soheil's native `feature_hole` operation rather than the generic circle-plus-cut mapping. Different tool counts are reported rather than artificially normalized by inserting calls.

### Repeating discovery, live CAD, and report generation

Discovery is independent of CAD modeling and keeps Andrew in mock mode:

```powershell
.\.venv\Scripts\python.exe -m benchmarks.run_protocol --manifest benchmarks\protocol-all.json --output artifacts\reproduction\protocol-all.json
```

For the following native runs, start the licensed SOLIDWORKS application yourself, finish any interactive operation, and run only one live harness at a time. The harness creates fresh documents, serializes competitors, records existing document identities and restores the six Andrew startup preference toggles. It explicitly removes `--mock` for Andrew. These commands write new result files rather than replace the original evidence:

```powershell
# Requires the verified frozen wheel unpacked above.
.\.venv\Scripts\python.exe -m benchmarks.run_live --live --servers pro_before,hjbaard,harrier,soheil --repeats 3 --output artifacts\reproduction\live-before.json
.\.venv\Scripts\python.exe -m benchmarks.run_live --live --servers pro,andrew,pro_before --repeats 3 --output artifacts\reproduction\live-after.json
.\.venv\Scripts\python.exe -m benchmarks.report_live --before artifacts\reproduction\live-before.json --after artifacts\reproduction\live-after.json --output artifacts\reproduction\benchmark-results.md --json-output artifacts\reproduction\live-analysis.json
```

Without the frozen wheel, use `--servers hjbaard,harrier,soheil` for the first command and `--servers pro,andrew` for the second, and state that before/after PRO regression comparison was not reproduced. A fresh run will have different run IDs, new CAD files and timings. Reproducing the protocol and scenario is not a promise of identical timing. To regenerate only the written analysis from bundled evidence, replace the two input paths of `benchmarks.report_live` with `benchmarks/results/live-before.json` and `benchmarks/results/live-after.json`; this requires no native CAD calls.

## Claims supported by the completed measurements

All four rivals, frozen PRO and current PRO completed all 18 recorded common CAD acceptance checks and their three document closes. Therefore this workload establishes no geometry-correctness superiority. Current PRO preserved an existing export and reported an error in all three no-clobber probes, matching Soheil; hjbaard, Harrier, Andrew and frozen PRO did not meet that default policy in their three probes. Current PRO's unknown top-level argument rejection matched Soheil and Andrew and differed from hjbaard and Harrier. These are specific default-policy comparisons, not proof that every operation is safe or that rivals cannot be configured or called differently. See [the complete live report](benchmark-results.md).

In the same after-run, current PRO's mass-query median was 104.869 ms versus frozen PRO's 430.069 ms, a 75.6% reduction; its complete CAD sequence median was 7,508.313 ms versus 6,958.441 ms, a 7.9% increase. Report both. Three repetitions are insufficient for broad statistical or overall speed claims, and these mass-query cases checked call success rather than independently asserting every returned mass/unit field.

PRO has separate evidence of an actual `IVisualizeAddinManager.Render` call producing a newly decoded 640 × 360 PNG at 50 passes, with a recorded file hash in [acceptance evidence](acceptance-evidence.json). Dedicated Visualize status/render/project-export tools are present in its discovered catalog and absent in the four rival catalogs at these pins. This supports saying that PRO adds a verified, dedicated SOLIDWORKS Visualize integration to this comparison. It does not establish better rendering quality or speed than rivals, exclude every generic macro/code route, or claim rivals can never render. This capability also existed in frozen PRO 0.1.0; it is not a new 0.2.0 benchmark improvement.

Remaining comparison gaps include complex parts, assemblies, drawings, sheet metal, topology selection, interrupted native calls, rollback, long-running stability, material/lighting fidelity, comparative render time, export round-trip geometry, and broader overwrite/path behavior. Soheil already matches the measured strict defaults and exposes substantial verified-workflow features. Andrew and Soheil expose more tools than PRO in these catalogs. The defensible conclusion is stronger support for selected workflows and policies, with explicit evidence and limits, rather than a universally better MCP.

## Appendix: observed isolated Python dependency constraints

These are version constraints, not executable upstream code. Save each block as a UTF-8 text file and pass it with `--constraint` during installation. The editable Andrew package itself is deliberately omitted from the constraints: install the pinned local checkout with `-e` as shown above. This pins the resolved package versions; it does not include wheel hashes, the local Python binaries, SOLIDWORKS, or .NET.

### harrier

```text
annotated-types==0.8.0
anyio==4.16.0
attrs==26.1.0
certifi==2026.10.23
cffi==2.1.1
click==8.5.0
cryptography==50.0.2
h11==0.16.0
httpcore==1.0.9
httpx==0.28.1
httpx-sse==0.4.3
idna==3.20
jsonschema==4.26.0
jsonschema-specifications==2025.9.1
mcp==1.30.0
pillow==12.2.0
pycparser==3.12
pydantic==2.14.2
pydantic-settings==2.16.2
pydantic_core==2.50.2
PyJWT==2.15.1
python-dotenv==1.2.4
python-multipart==0.0.33
pywin32==311
referencing==0.37.0
rpds-py==2026.9.1
sse-starlette==3.6.0
starlette==1.10.0
typing-inspection==0.4.4
typing_extensions==4.16.0
uvicorn==0.54.0
```

### andrew

```text
aiofile==3.12.3
aiosqlite==0.22.1
annotated-doc==0.0.5
annotated-types==0.8.0
anthropic==2.4.0
anyio==4.16.0
attrs==26.1.0
authlib==1.9.3
beartype==0.23.1
cachetools==7.2.8
caio==0.12.10
certifi==2026.10.23
cffi==2.1.1
chardet==5.2.0
charset-normalizer==3.5.2
click==8.5.0
colorama==0.4.6
comtypes==1.4.17
cryptography==50.0.2
cyclopts==5.3.3
defusedxml==0.7.1
dnspython==2.9.1
docstring-parser==0.18.0
email-validator==2.3.0
exceptiongroup==1.3.1
fastmcp==4.1.6
fastmcp-slim==4.1.6
genai-prices==0.2.1
griffelib==2.4.1
h11==0.16.0
httpcore==1.0.9
httpcore2==2.16.0
httpx==0.28.1
httpx2==2.16.0
idna==3.20
jinja2==3.1.6
jiter==0.18.0
joserfc==1.7.7
jsonref==1.1.0
jsonschema==4.26.0
jsonschema-path==0.5.0
jsonschema-specifications==2025.9.1
logfire-api==5.4.0
loguru==0.7.3
markdown-it-py==4.2.0
markupsafe==3.0.4
mcp==2.3.0
mcp-types==2.3.0
mdurl==0.1.2
openai==3.42.1
openapi-pydantic==0.6.0
opentelemetry-api==1.45.2
packaging==26.3
pathable==0.6.0
platformdirs==4.14.3
py-key-value-aio==0.4.6
pyasn1==0.6.5
pycparser==3.12
pydantic==2.14.2
pydantic-ai-slim==1.107.5
pydantic-core==2.50.2
pydantic-graph==1.107.5
pydantic-settings==2.16.2
pygments==2.21.0
pyjwt==2.15.1
pyperclip==1.11.0
python-dotenv==1.2.4
python-multipart==0.0.33
pywin32==312
pywin32-ctypes==0.2.3
pyyaml==6.0.3
referencing==0.37.0
regex==2026.9.29
requests==2.34.2
rich==15.0.0
rich-rst==2.2.0
rpds-py==2026.9.1
shellingham==1.5.4
sniffio==1.3.1
sqlalchemy==2.1.6
sqlmodel==0.0.48
sse-starlette==3.6.0
starlette==1.10.0
tiktoken==0.14.0
truststore==0.10.5
typer==0.27.3
typing-extensions==4.16.0
typing-inspection==0.4.4
uncalled-for==0.4.1
urllib3==2.8.0
uvicorn==0.54.0
watchfiles==1.3.0
websockets==17.4
win32-setctime==1.2.1
```
