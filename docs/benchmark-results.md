# Measured SolidWorks MCP results

This report summarizes recorded live benchmark runs. It separates CAD correctness, tool-call timing, cleanup, and two default policies; it does not combine them into a weighted score.

Six CAD cases are checked per repetition: new document, 80 × 50 × 8 mm block, Ø10 through-hole, depth edit to 12 mm, mass query, and STEP export. Geometry checks use an independent COM mass/bounding-box oracle. Export validation is limited to the evidence recorded by the harness; it does not imply a successful round trip into another CAD kernel.

## CAD acceptance

| Run | Adapter | Repetitions recorded/expected | Pass / total | Fail | Unsupported | Not run | Close pass/fail/unsupported/not run |
|---|---|---:|---:|---:|---:|---:|---:|
| before | hjbaard | 3/3 | 18/18 | 0 | 0 | 0 | 3/0/0/0 |
| before | harrier | 3/3 | 18/18 | 0 | 0 | 0 | 3/0/0/0 |
| before | soheil | 3/3 | 18/18 | 0 | 0 | 0 | 3/0/0/0 |
| before | pro_before | 3/3 | 18/18 | 0 | 0 | 0 | 3/0/0/0 |
| after | pro | 3/3 | 18/18 | 0 | 0 | 0 | 3/0/0/0 |
| after | andrew | 3/3 | 18/18 | 0 | 0 | 0 | 3/0/0/0 |
| after | pro_before | 3/3 | 18/18 | 0 | 0 | 0 | 3/0/0/0 |

Absent stages and missing repetitions count as not run. Unsupported and not-run cases are never counted as passes. Adapter names identify the implemented call sequences; tool breadth and general modeling coverage are separate from this small common workload.

## MCP timing

The total includes new-document creation, block, hole, edit, mass query, STEP export, and close. It excludes both policy probes, independent oracle reads, LLM inference, and client startup. Only repetitions where all seven CAD stages passed enter complete-run timing; a fast failure cannot improve the median.

| Run | Adapter | Complete timing samples | CAD total median ms | Min–max ms | CAD tool calls median (min–max) |
|---|---|---:|---:|---:|---:|
| before | hjbaard | 3 | 8,374.067 | 8,177.248–8,574.644 | 7.000 (7.000–7.000) |
| before | harrier | 3 | 7,365.424 | 6,396.010–7,868.805 | 11.000 (11.000–11.000) |
| before | soheil | 3 | 8,285.588 | 7,332.441–8,484.828 | 10.000 (10.000–10.000) |
| before | pro_before | 3 | 7,913.868 | 7,431.570–8,196.375 | 7.000 (7.000–7.000) |
| after | pro | 3 | 7,508.313 | 6,761.471–8,249.341 | 7.000 (7.000–7.000) |
| after | andrew | 3 | 15,786.841 | 14,518.312–16,085.103 | 13.000 (13.000–13.000) |
| after | pro_before | 3 | 6,958.441 | 6,937.263–7,993.111 | 7.000 (7.000–7.000) |

### Current PRO and frozen PRO in the same run

| Run | Metric | Frozen median ms | Current median ms | Observed latency change | Samples frozen/current |
|---|---|---:|---:|---:|---:|
| after | Mass query | 430.069 | 104.869 | -75.6% | 3/3 |
| after | Complete CAD sequence | 6,958.441 | 7,508.313 | +7.9% | 3/3 |

Negative latency change means a lower measured median; positive means higher. A faster mass query does not imply a faster full CAD sequence. Both observations are shown, without claiming statistical significance.

### Stage medians (milliseconds)

| Run | Adapter | New | Block | Hole | Edit | Mass | Export | Close |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| before | hjbaard | 1,874.632 | 1,755.856 | 1,502.910 | 972.575 | 527.666 | 601.671 | 953.377 |
| before | harrier | 1,593.439 | 2,098.174 | 2,239.350 | 615.106 | 92.482 | 173.044 | 463.241 |
| before | soheil | 1,886.034 | 3,317.034 | 1,087.042 | 977.304 | 116.480 | 384.377 | 543.533 |
| before | pro_before | 1,873.552 | 1,620.831 | 1,511.629 | 1,015.801 | 492.892 | 612.267 | 810.206 |
| after | pro | 1,967.462 | 1,495.496 | 1,502.107 | 818.167 | 104.869 | 515.828 | 1,146.610 |
| after | andrew | 5,829.034 | 3,528.036 | 3,797.730 | 350.126 | 334.550 | 401.049 | 1,304.139 |
| after | pro_before | 1,540.774 | 1,569.344 | 1,435.326 | 794.505 | 430.069 | 530.284 | 841.376 |

Stage medians use only passing samples for that stage and can have different sample counts; complete sample arrays and minimum/maximum values are retained in the analysis JSON. Do not sum stage medians to reconstruct the median total.

## Default policy probes

Counts below are **pass / fail / unsupported / not run**. Unknown-argument rejection and no-clobber behavior are distinct policy choices, not extra geometry cases.

| Run | Adapter | Unknown argument rejected | Existing export preserved and error reported |
|---|---|---:|---:|
| before | hjbaard | 0/3/0/0 | 0/3/0/0 |
| before | harrier | 0/3/0/0 | 0/3/0/0 |
| before | soheil | 3/0/0/0 | 3/0/0/0 |
| before | pro_before | 3/0/0/0 | 0/3/0/0 |
| after | pro | 3/0/0/0 | 3/0/0/0 |
| after | andrew | 3/0/0/0 | 0/3/0/0 |
| after | pro_before | 3/0/0/0 | 0/3/0/0 |

## Separate verification of returned mass-query volumes

These checks were computed after the runs from captured mass-query responses and the independent COM oracle in the same record. Original stage outcomes and measured timings were not changed. Only normal mass queries are included; deliberate unknown-argument policy probes are excluded.

| Adapter | Samples | Pass | Fail | Maximum relative volume error |
|---|---:|---:|---:|---:|
| hjbaard | 3 | 3 | 0 | 0 |
| harrier | 3 | 3 | 0 | 4.68347e-08 |
| soheil | 3 | 3 | 0 | 4.33349e-09 |
| pro_before | 6 | 6 | 0 | 1.54618e-16 |
| pro | 3 | 3 | 0 | 0 |
| andrew | 3 | 3 | 0 | 0 |

Returned-volume checks passed: **21/21**. Relative-error tolerance: `1e-06`.

The derived evidence records returned units, oracle values, source record/call indices, and the SHA-256 of each raw input. See [returned-volume checks](../benchmarks/results/mass-volume-checks.json).

- Raw source `benchmarks/results/live-before.json`; SHA-256 `ef07c453a37491af49faa51dc5c52f518832e4851c3551f384eb89c227e11502`.
- Raw source `benchmarks/results/live-after.json`; SHA-256 `c14685a29e9e643a46f47ccfe094944e977b851a607cac7ee4140da0aa9710fd`.

## Sources and observed failures

### before

Run ID: `d04da1a18712`; recorded: 2026-11-03T10:11:31.553892+00:00; configured repetitions: 3.

Original mass-query stage validation: Call success only; returned mass/volume units were not separately asserted. Modeling-stage volume and bounds were checked by the independent oracle.

- **hjbaard**: [repository](https://github.com/hjbaard/SolidWorks-MCP); revision `ddcefc0cfa056002ff8eb8bdb186818ad52dc2f1`.
- **harrier**: [repository](https://github.com/HarrierPigeon/Solidworks-MCP-Server); revision `60c11fe45b6cfc60c4e2da8ad41fc725a48bf8ea`; release `v0.4.0`; license `MIT`.
- **soheil**: [repository](https://github.com/soheil-mp/solidworks-mcp); revision `79fb529b67ca4a0587e5570024a772fad0b2c14d`; package `0.1.0`; license `MIT`.
- **pro_before**: revision `frozen-0.1.0`; frozen wheel SHA-256 `37791b5f24ba5c4aa5b9c83f313cd33b59640e950e722eaaeedcde1e2d7768fc`.

### after

Run ID: `0adfacf67f63`; recorded: 2026-11-03T10:15:25.682695+00:00; configured repetitions: 3.

Original mass-query stage validation: Call success only; returned mass/volume units were not separately asserted. Modeling-stage volume and bounds were checked by the independent oracle.

- **pro**: revision `working-tree`.
- **andrew**: [repository](https://github.com/andrewbartels1/SolidworksMCP-python); revision `126beed0e7a13214ebdd5724929f4c095cf2642f`; release `v1.5.0`; license `MIT`.
- **pro_before**: revision `frozen-0.1.0`; frozen wheel SHA-256 `37791b5f24ba5c4aa5b9c83f313cd33b59640e950e722eaaeedcde1e2d7768fc`.

## Scope and limits

Three repetitions provide descriptive medians and ranges, not a confidence interval or evidence of a general performance advantage. Runs share one SOLIDWORKS instance and use a fixed seeded order; process startup, warm caches, application state, and background load can affect timings. Before/after runs are sequential observations and are not randomized paired trials.

This workload does not measure assemblies, drawings, sheet metal, arbitrary topology selection, undo/rollback, photorealistic rendering quality, or all supported CAD commands. Actual Visualize rendering and offline mesh preview require their own output evidence. More tools, stricter defaults, or faster results on these shapes do not establish universal superiority.

See [protocol discovery results](../benchmarks/results/protocol-all.md), [before raw evidence](../benchmarks/results/live-before.json), [after raw evidence](../benchmarks/results/live-after.json), and [derived live analysis](../benchmarks/results/live-analysis.json). No unsupported, missing, or failed scenario is silently reclassified as successful.
