# Compact discovery and workflow-envelope evidence

Measured on **3 November 2026**, source version **0.5.0**, Python 3.12.16,
Windows 11, `solidworks-mcp==0.19.0`, `mcp==1.30.0`, Pillow 12.3.0.
This benchmark makes **zero native CAD tool calls**. It does not measure COM,
GPU rendering, client inference, billed tokens, or end-to-end native speed.

## Reproduce

From the repository root:

```powershell
.\.venv\Scripts\python.exe -m benchmarks.run_fast_workflows --output artifacts/benchmarks/fast-workflows-v050-final.json --repeats 3
.\.venv\Scripts\python.exe -m pytest tests/test_fast_benchmark.py -q
```

The harness calls `NativeBackend.list_tools` once to obtain the actual installed
upstream descriptors. It initializes the real PRO application in both profiles
without invoking native tools. All subsequent workflow calls use the bundled
synthetic backend and real application validation, operation tracking, result
presentation, artifact registration and render orchestration. Synthetic PNGs
have the requested dimensions; their solid color is not native render evidence.
The synthetic lease uses its own temporary lock file.

The current local JSON evidence is `artifacts/benchmarks/fast-workflows-v050-final.json`,
recorded at `2026-11-03T20:14:04.385923+00:00`. The earlier report remains intact.
Its SHA-256 for this run is
`f2f0c70d41abd0f0a57160564bb8c77fbddc735e8601cad360c700e1a683444f`.
The source packager excludes `artifacts`; regenerate the evidence with the
command above or retain the JSON separately. Reruns have different timestamps.

## Actual descriptor measurements

| Profile | Published tools | Catalog bytes | Input-schema bytes |
|---|---:|---:|---:|
| Full | 119 | 119,060 | 56,901 |
| Compact | 10 | 7,916 | 4,926 |

Values count compact UTF-8 JSON for the actual descriptors and schema arrays.
They exclude server instructions and resources. The full profile contains 88
inherited tools and 31 PRO tools. Byte reductions do not establish token
reductions: optional `tiktoken` was unavailable, so tokenizer counts are explicitly
**not measured**. If installed, the harness accepts `--encoding` and uses only an
already cached vocabulary; it never downloads one. Such counts describe that
chosen text tokenizer, not a client's tool transformation or image-token cost.

## Synthetic workflow measurements

Each scenario ran three times. Normalized call counts and byte metrics were
identical across all repetitions. Both paths share one scenario specification:
64 features, eight equations and 32 components for diagnosis; the same 32 RGB
assignments for coloring; 960×540/50-pass preview followed by 1920×1080/200-pass
final, with denoiser enabled, for rendering. Exploded rendering moves eight exact
component names with explicit translations and zero rotations.

| Scenario | Legacy public calls | High-level public calls | Legacy primary bytes | High-level primary bytes | Optional full-details bytes |
|---|---:|---:|---:|---:|---:|
| Diagnosis | 4 | 1 | 20,960 | 1,673 | 20,402 |
| RGB, 32 components | 32 | 1 | 17,920 | 2,452 | 3,850 |
| Normal preview + final | 12 | 3 | 41,408 | 15,968 | 44,260 |
| Exploded preview + final | 15 | 3 | 46,991 | 16,744 | 47,394 |

“Primary bytes” count canonicalized application requests and complete response
JSON, including text/structured-result duplication and any base64 image blocks.
They are **synthetic envelope bytes**, not measured stdio traffic or LLM tokens.
Fixture paths, UUIDs, timestamps and elapsed-time fields are normalized;
JSON-RPC framing and client behavior are excluded. The report separately records
response text-envelope bytes with binary image data replaced by a marker.

The legacy example reads four inventories, colors components individually,
inspects/applies/restores the known camera, and polls each render once to
completion. Exploded rendering adds pose inspection, application and restoration.
Other valid legacy call patterns can have different counts. The fixture gates
render completion until its start response, ensuring one terminal poll per render
without inventing renderer wall times. High-level rendering uses one start call,
one terminal poll and one explicit final-image fetch. Both paths generate two
images with identical render parameters; the legacy terminal polls deliver both
images, while the high-level poll delivers no image and its explicit fetch
delivers the final image once. This delivery-policy change contributes to the
byte difference.

Full evidence retrieval costs one additional call and added **zero synthetic
backend calls** in every scenario. Requesting all details routinely can erase
the summary savings: normal rendering's 15,968 primary bytes plus 44,260 details
bytes exceed the legacy example's 41,408 bytes. Detailed evidence remains useful
on demand; these results do not claim every usage pattern is cheaper.

Tests check shared scenario parameters, color equality, identical render settings,
temporary-state restoration, public-call counts, image-free high-level polls,
and evidence retrieval without rerunning the backend. Legacy restoration uses
the fixture's known front view and zero rotations. These checks do not prove
equivalent diagnostic coverage, arbitrary native restoration, model correctness,
visual quality, or native render speed. Those require separately recorded live
SOLIDWORKS validation. See [v0.5.0 native validation](validation-v0.5.0.md) for
the separate scope and evidence for diagnostics, repair, RGB and cleanup. Native
render execution was blocked by macro execution in the tested environment;
successful native rendering and native render speed remain **unverified** for
this release validation. The focused benchmark suite passed **13 tests**; Ruff passed.
