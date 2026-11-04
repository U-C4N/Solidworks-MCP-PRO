# Benchmark methodology

This project reports measurements and acceptance checks separately. A feature with no executable test is **not_run**. A server that does not expose a requested capability is **unsupported**. Neither status counts as a pass. The report does not claim that a greater tool count, smaller schema, or faster discovery proves overall CAD superiority.

## Discovery protocol

`benchmarks/run_protocol.py` uses the official Python MCP SDK's `stdio_client` and `ClientSession`. Each server runs in its own sequential subprocess with an explicit executable and argument array; no shell interpolation is used. It sends `initialize`, `tools/list`, `resources/list`, and `prompts/list` when the corresponding capability is declared. It never calls a CAD or rendering tool.

Each run measures process launch through completed initialization once. It then repeats the complete, paginated tools catalog request five times in the same process and reports every sample plus median, minimum, and maximum. This is a warm catalog measurement after cold process startup. Results depend on this machine's load, filesystem caches, Python environment, and dependency imports; they are not cross-machine performance claims. Pagination is bounded to 100 pages and 10,000 entries.

Input-schema bytes count UTF-8 compact JSON for the array of input schemas. Catalog bytes count the complete serialized tool descriptors. Schema validation uses the JSON Schema validator selected by each schema's declared dialect. Missing descriptions and absent annotation objects are counted explicitly. Top-level `additionalProperties` declarations are recorded statically; runtime unknown-argument rejection is **not_run**, because invoking a nominal status tool may still attach to live CAD.

## Run

```powershell
.\.venv\Scripts\python.exe benchmarks/run_protocol.py --manifest benchmarks/competitors.json --output artifacts/benchmarks/protocol.json
.\.venv\Scripts\python.exe benchmarks/report.py artifacts/benchmarks/protocol.json --output artifacts/benchmarks/protocol.md
```

Full tool descriptors are also saved as `catalog-{server-id}.json` beside the output report. Use `--catalog-dir` to select another snapshot folder. These files support later adapter development and schema-drift inspection without making native tool calls.

The manifest uses this shape:

```json
{
  "schema_version": 1,
  "as_of": "2026-11-03",
  "servers": [{
    "id": "example",
    "name": "Example server",
    "command": "C:/absolute/environment/Scripts/python.exe",
    "args": ["-m", "example_server"],
    "cwd": "C:/absolute/checkout",
    "env": {},
    "startup_timeout_seconds": 30,
    "request_timeout_seconds": 30,
    "enabled": true,
    "runtime_mode": "native",
    "source": {
      "repository": "https://github.com/owner/project",
      "revision": "exact-tag-or-commit",
      "license": "SPDX identifier"
    }
  }]
}
```

An unavailable installation remains in the manifest with `enabled: false` and `not_run_reason`; it is reported without launching. The runner preserves source metadata but does not fetch or independently verify revision claims. Record primary repository/release links and immutable revisions during research. Reports replace repository, user-home, and temporary-directory roots with labels. Review reports before sharing them; arbitrary external paths or environment-derived error text may require additional redaction.

Optional `required_files` lists absolute files that must exist before a subprocess can start. Frozen Python baselines use this preflight for their package entry point: if the archived source is missing, the runner fails instead of letting Python silently import the current installed package. This existence check does not substitute for source hashes or the initialized server's reported version.

`runtime_mode` is preserved in results and catalog snapshots. A server started with a mock option must be labeled accordingly; its discovery results do not demonstrate native CAD execution. If mode is omitted, it is reported as unspecified rather than inferred to be native.

## Live harness contract

`connect(spec)` is an async context manager that yields an initialized SDK `ClientSession`. Its `benchmark_initialize` attribute records startup timing and the initialize response. `timed_call(session, name, arguments)` records elapsed time, MCP and nested payload errors, and normalized structured/text JSON output. The helper makes exactly one request and never retries. A timeout or lost connection leaves native outcome potentially indeterminate; inspect the application before resubmitting.

Live geometry tests must use separate new documents and report the actual requested geometry, units, expected analytic volume/dimensions, measured values, file decoding evidence, and cleanup outcome. Live rendering tests must distinguish actual Visualize output from viewport screenshots or mesh previews. Preserve failed and unsupported cases in the denominator rather than silently dropping them. Report each tested server/version separately and scope conclusions to those measured scenarios.
