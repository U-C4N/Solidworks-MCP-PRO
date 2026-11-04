# MCP discovery benchmark

Measured at: 2026-11-03T10:17:32.018824+00:00

| Server | Runtime mode | Status | Initialize ms | tools/list median ms | Min–max ms | Tools | Input schemas KiB |
|---|---|---|---:|---:|---:|---:|---:|
| PRO 0.1.0 frozen | native adapter; discovery only, no CAD tool calls | measured | 1,193.423 | 6.551 | 5.772–7.725 | 101 | 31.894 |
| PRO 0.2.0 current | native adapter; discovery only, no CAD tool calls | measured | 1,282.637 | 9.654 | 9.464–11.198 | 101 | 41.846 |
| hjbaard 0.19.0 | native adapter; discovery only, no CAD tool calls | measured | 1,050.757 | 6.002 | 5.114–7.161 | 88 | 26.793 |
| HarrierPigeon/Solidworks-MCP-Server | native; connects to CAD only on tool invocation | measured | 936.810 | 6.079 | 5.340–6.369 | 95 | 38.755 |
| soheil-mp/solidworks-mcp | native; lazy attach, auto launch disabled | measured | 450.389 | 28.640 | 27.071–37.563 | 161 | 148.979 |
| andrewbartels1/SolidworksMCP-python | upstream mock adapter; discovery only; not native CAD evidence | measured | 4,228.829 | 54.564 | 52.287–661.515 | 154 | 104.348 |

One process initialization per server; tools/list repeats are warm requests within that process. These timings measure this machine and run, not general CAD speed or correctness.

No CAD tools were invoked by this discovery runner. Mock-mode discovery establishes catalog/protocol behavior only; it is not native CAD evidence. Runtime unknown-argument rejection, geometry accuracy, rendering fidelity, recovery, and document safety require separately reported live tests. Unsupported and unrun features are never counted as passes.

## Evidence and conformance

### PRO 0.1.0 frozen

Source: not supplied; revision: `frozen-0.1.0`; license: MIT.
Schema validation: **pass**. Missing descriptions: 0; missing annotations: 0. Stable tool count across samples: True.
Resources: measured; prompts: measured. Unknown argument runtime handling: **not_run**.

### PRO 0.2.0 current

Source: not supplied; revision: `working-tree`; license: MIT.
Schema validation: **pass**. Missing descriptions: 0; missing annotations: 0. Stable tool count across samples: True.
Resources: measured; prompts: measured. Unknown argument runtime handling: **not_run**.

### hjbaard 0.19.0

Source: https://github.com/hjbaard/SolidWorks-MCP; revision: `ddcefc0cfa056002ff8eb8bdb186818ad52dc2f1`; license: MIT.
Schema validation: **pass**. Missing descriptions: 0; missing annotations: 88. Stable tool count across samples: True.
Resources: measured; prompts: measured. Unknown argument runtime handling: **not_run**.

### HarrierPigeon/Solidworks-MCP-Server

Source: https://github.com/HarrierPigeon/Solidworks-MCP-Server; revision: `60c11fe45b6cfc60c4e2da8ad41fc725a48bf8ea`; license: MIT.
Schema validation: **pass**. Missing descriptions: 0; missing annotations: 95. Stable tool count across samples: True.
Resources: unsupported; prompts: unsupported. Unknown argument runtime handling: **not_run**.

### soheil-mp/solidworks-mcp

Source: https://github.com/soheil-mp/solidworks-mcp; revision: `79fb529b67ca4a0587e5570024a772fad0b2c14d`; license: MIT.
Schema validation: **pass**. Missing descriptions: 0; missing annotations: 0. Stable tool count across samples: True.
Resources: unsupported; prompts: unsupported. Unknown argument runtime handling: **not_run**.

### andrewbartels1/SolidworksMCP-python

Source: https://github.com/andrewbartels1/SolidworksMCP-python; revision: `126beed0e7a13214ebdd5724929f4c095cf2642f`; license: MIT.
Schema validation: **pass**. Missing descriptions: 0; missing annotations: 154. Stable tool count across samples: True.
Resources: measured; prompts: measured. Unknown argument runtime handling: **not_run**.

Source revisions are declared in the input manifest. See its primary-source evidence for verification. Tool count is an inventory measure; it is not an overall quality ranking.
