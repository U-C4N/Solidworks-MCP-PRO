# Measured SolidWorks MCP results

This report summarizes recorded live benchmark runs. It separates CAD correctness, tool-call timing, cleanup, and two default policies; it does not combine them into a weighted score.

Six CAD cases are checked per repetition: new document, 80 × 50 × 8 mm block, Ø10 through-hole, depth edit to 12 mm, mass query, and STEP export. Geometry checks use an independent COM mass/bounding-box oracle. Export validation is limited to the evidence recorded by the harness; it does not imply a successful round trip into another CAD kernel.

## CAD acceptance

| Run | Adapter | Repetitions recorded/expected | Pass / total | Fail | Unsupported | Not run | Close pass/fail/unsupported/not run |
|---|---|---:|---:|---:|---:|---:|---:|
| performance | andrew | 6/6 | 36/36 | 0 | 0 | 0 | 6/0/0/0 |
| performance | soheil | 6/6 | 36/36 | 0 | 0 | 0 | 6/0/0/0 |
| performance | hjbaard | 6/6 | 36/36 | 0 | 0 | 0 | 6/0/0/0 |
| performance | pro_v020 | 6/6 | 36/36 | 0 | 0 | 0 | 6/0/0/0 |
| performance | harrier | 6/6 | 36/36 | 0 | 0 | 0 | 6/0/0/0 |
| performance | pro | 6/6 | 36/36 | 0 | 0 | 0 | 6/0/0/0 |

Absent stages and missing repetitions count as not run. Unsupported and not-run cases are never counted as passes. Adapter names identify the implemented call sequences; tool breadth and general modeling coverage are separate from this small common workload.

## MCP timing

The total includes new-document creation, block, hole, edit, mass query, STEP export, and close. It excludes policy probes, independent oracle reads, LLM inference, client startup, and retained warm-up scenarios. Only repetitions where all seven CAD stages passed enter complete-run timing; a fast failure cannot improve the median.

| Run | Adapter | Complete timing samples | CAD total median ms | Min–max ms | Mean ± sample SD ms | CAD tool calls median (min–max) |
|---|---|---:|---:|---:|---:|---:|
| performance | andrew | 6 | 19,438.759 | 16,650.091–22,494.115 | 19,188.954 ± 2,177.488 | 13.000 (13.000–13.000) |
| performance | soheil | 6 | 12,033.076 | 8,503.653–13,222.821 | 11,558.873 ± 1,709.772 | 10.000 (10.000–10.000) |
| performance | hjbaard | 6 | 10,072.386 | 8,791.674–11,189.627 | 10,009.731 ± 1,058.772 | 7.000 (7.000–7.000) |
| performance | pro_v020 | 6 | 9,578.293 | 8,338.335–10,679.525 | 9,601.400 ± 768.292 | 7.000 (7.000–7.000) |
| performance | harrier | 6 | 10,143.178 | 7,693.406–11,103.141 | 9,682.079 ± 1,347.539 | 11.000 (11.000–11.000) |
| performance | pro | 6 | 6,309.042 | 5,336.868–6,818.012 | 6,135.473 ± 543.495 | 7.000 (7.000–7.000) |

### Stdio process startup (excluded from CAD totals)

| Run | Adapter | Samples | Startup median ms | Min–max ms | Mean ± sample SD ms |
|---|---|---:|---:|---:|---:|
| performance | andrew | 6 | 6,245.773 | 5,648.789–7,202.074 | 6,231.711 ± 562.210 |
| performance | soheil | 6 | 449.120 | 443.677–571.278 | 469.892 ± 50.163 |
| performance | hjbaard | 6 | 1,155.207 | 995.729–1,309.302 | 1,159.369 ± 153.459 |
| performance | pro_v020 | 6 | 1,291.436 | 1,227.348–1,570.159 | 1,326.491 ± 123.505 |
| performance | harrier | 6 | 977.423 | 911.261–1,282.618 | 1,034.103 ± 150.236 |
| performance | pro | 6 | 1,538.444 | 1,448.486–1,805.326 | 1,581.383 ± 147.747 |

Every repetition launches a fresh MCP subprocess. Startup measures process launch through MCP initialization. Servers can attach to CAD eagerly or on the first native call, so initialization and new-part costs fall in different stages. The CAD sum alone is not complete user-perceived startup latency; both measurements are shown. Application/cache warm-up scenarios remain excluded.

### Current PRO and frozen PRO in the same run

| Run | Baseline | Metric | Frozen median ms | Current median ms | Observed latency change | Samples frozen/current |
|---|---|---|---:|---:|---:|---:|
| performance | pro_v020 | Mass query | 210.919 | 205.842 | -2.4% | 6/6 |
| performance | pro_v020 | Complete CAD sequence | 9,578.293 | 6,309.042 | -34.1% | 6/6 |

Negative latency change means a lower measured median; positive means higher. A faster mass query does not imply a faster full CAD sequence. Both observations are shown, without claiming statistical significance.

### Current PRO versus each same-run reference

| Run | Reference adapter | Reference CAD median ms | Current PRO CAD median ms | Observed latency change | Samples reference/current |
|---|---|---:|---:|---:|---:|
| performance | andrew | 19,438.759 | 6,309.042 | -67.5% | 6/6 |
| performance | soheil | 12,033.076 | 6,309.042 | -47.6% | 6/6 |
| performance | hjbaard | 10,072.386 | 6,309.042 | -37.4% | 6/6 |
| performance | pro_v020 | 9,578.293 | 6,309.042 | -34.1% | 6/6 |
| performance | harrier | 10,143.178 | 6,309.042 | -37.8% | 6/6 |

These are observed medians for the same verified shape sequence. The adapters expose different operations and use different call counts; this table does not rank their overall feature coverage or reliability.

### Stage medians (milliseconds)

| Run | Adapter | New | Block | Hole | Edit | Mass | Export | Close |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| performance | andrew | 6,795.188 | 4,807.476 | 5,055.235 | 442.698 | 478.524 | 458.812 | 1,477.399 |
| performance | soheil | 2,162.039 | 4,792.397 | 1,695.146 | 1,545.929 | 171.637 | 608.484 | 885.160 |
| performance | hjbaard | 1,934.575 | 2,116.661 | 1,536.379 | 1,287.899 | 718.360 | 1,012.861 | 1,463.626 |
| performance | pro_v020 | 1,654.724 | 2,035.979 | 1,578.322 | 1,328.731 | 210.919 | 977.685 | 1,748.101 |
| performance | harrier | 1,727.385 | 3,075.974 | 2,618.595 | 1,083.600 | 148.394 | 326.489 | 829.138 |
| performance | pro | 1,691.553 | 1,539.924 | 916.394 | 660.033 | 205.842 | 323.816 | 871.595 |

Stage medians use only passing samples for that stage and can have different sample counts; complete sample arrays, means, sample standard deviations, sample variances (ms²), and minimum/maximum values are retained in the analysis JSON. Sample variance is unavailable for a single observation. Do not sum stage medians to reconstruct the median total.

## Default policy probes

Counts below are **pass / fail / unsupported / not run**. Unknown-argument rejection and no-clobber behavior are distinct policy choices, not extra geometry cases.

| Run | Adapter | Unknown argument rejected | Existing export preserved and error reported |
|---|---|---:|---:|
| performance | andrew | 6/0/0/0 | 0/6/0/0 |
| performance | soheil | 6/0/0/0 | 6/0/0/0 |
| performance | hjbaard | 0/6/0/0 | 0/6/0/0 |
| performance | pro_v020 | 6/0/0/0 | 6/0/0/0 |
| performance | harrier | 0/6/0/0 | 0/6/0/0 |
| performance | pro | 6/0/0/0 | 6/0/0/0 |

## Separate verification of returned mass-query volumes

These checks were computed after the runs from captured mass-query responses and the independent COM oracle in the same record. Original stage outcomes and measured timings were not changed. Only normal mass queries are included; deliberate unknown-argument policy probes are excluded.

| Adapter | Samples | Pass | Fail | Maximum relative volume error |
|---|---:|---:|---:|---:|
| andrew | 6 | 6 | 0 | 0 |
| soheil | 6 | 6 | 0 | 4.33349e-09 |
| hjbaard | 6 | 6 | 0 | 0 |
| pro_v020 | 6 | 6 | 0 | 1.54618e-16 |
| harrier | 6 | 6 | 0 | 4.68347e-08 |
| pro | 6 | 6 | 0 | 1.54618e-16 |

Returned-volume checks passed: **36/36**. Relative-error tolerance: `1e-06`.

The derived evidence records returned units, oracle values, source record/call indices, and the SHA-256 of each raw input. See [returned-volume checks](../benchmarks/results/performance-volumes.json).

- Raw source `benchmarks/results/performance-balanced.json`; SHA-256 `0e6017eb73cc60e7d4f89080c64df45307b2c2cdc7646d345545094e2ba55469`.

## Sources and observed failures

### performance

Run ID: `d2f84132e68b`; recorded: 2026-11-03T10:53:50.518418+00:00; configured repetitions: 6.

Order design: Seeded Williams/Latin square. Six systems and six repetitions balance every position and every within-row ordered adjacent pair once.

Fixture design: Measured CAD document is closed before policy probes. Policies use a separate new block document; setup/cleanup and policy calls excluded from CAD timing.

| Repetition | Planned adapter order |
|---:|---|
| 0 | andrew → soheil → hjbaard → pro_v020 → harrier → pro |
| 1 | soheil → pro_v020 → andrew → pro → hjbaard → harrier |
| 2 | harrier → hjbaard → pro → andrew → pro_v020 → soheil |
| 3 | hjbaard → andrew → harrier → soheil → pro → pro_v020 |
| 4 | pro → harrier → pro_v020 → hjbaard → soheil → andrew |
| 5 | pro_v020 → pro → soheil → harrier → andrew → hjbaard |

Warm-ups are retained evidence and excluded from the measured tables above.

| Adapter | Warm-up repetition | CAD complete | Policy fixture setup | Policy fixture cleanup |
|---|---:|---|---|---|
| soheil | 0 | True | pass | pass |
| hjbaard | 0 | True | pass | pass |
| harrier | 0 | True | pass | pass |
| pro_v020 | 0 | True | pass | pass |
| pro | 0 | True | pass | pass |
| andrew | 0 | True | pass | pass |

Original mass-query stage validation: Call success only; returned mass/volume units were not separately asserted. Modeling-stage volume and bounds were checked by the independent oracle.

- **andrew**: [repository](https://github.com/andrewbartels1/SolidworksMCP-python); revision `126beed0e7a13214ebdd5724929f4c095cf2642f`; release `v1.5.0`; license `MIT`.
  Initialized server version(s): `1.5.0`; runtime: native; upstream startup preferences snapshotted and restored.
- **soheil**: [repository](https://github.com/soheil-mp/solidworks-mcp); revision `79fb529b67ca4a0587e5570024a772fad0b2c14d`; package `0.1.0`; license `MIT`.
  Initialized server version(s): `0.1.0`; runtime: native; lazy attach, auto launch disabled.
- **hjbaard**: [repository](https://github.com/hjbaard/SolidWorks-MCP); revision `ddcefc0cfa056002ff8eb8bdb186818ad52dc2f1`.
  Initialized server version(s): `1.30.0`; runtime: native.
- **pro_v020**: revision `frozen-0.2.0`; package `0.2.0`; frozen wheel SHA-256 `4dca5d879461e412440413a84f62a7ecec06cfe0af9dbc4db4b4d67a1cac2cf5`; source tree SHA-256 `0a5971770a406c03bcbfaf008ee4f30f8597e00784535da6010bd1a6f496dba3`.
  Initialized server version(s): `0.2.0`; runtime: native.
- **harrier**: [repository](https://github.com/HarrierPigeon/Solidworks-MCP-Server); revision `60c11fe45b6cfc60c4e2da8ad41fc725a48bf8ea`; release `v0.4.0`; license `MIT`.
  Initialized server version(s): `1.30.0`; runtime: native; connects to CAD only on tool invocation.
- **pro**: revision `working-tree`; source tree SHA-256 `8cc5f6dc91358d42eb9e59a910868f6dd59e3ef417c93eb4d5b40e6912e21f67`.
  Initialized server version(s): `0.3.0`; runtime: native.

The initialized version above is the server's literal MCP `serverInfo.version` value; some upstream servers return the MCP SDK version. Repository revisions, release/package metadata, and frozen hashes identify the compared code independently.

## Scope and limits

The recorded repetitions provide descriptive medians, ranges, and sample variance, not a confidence interval or evidence of a general performance advantage. Planned counterbalancing controls position and within-row preceding-adapter exposure when a full design is completed; it does not eliminate process startup effects, cache history, application state, thermal changes, or background load. Separate historical before/after runs are sequential observations and are not randomized paired trials.

This workload does not measure assemblies, drawings, sheet metal, arbitrary topology selection, undo/rollback, photorealistic rendering quality, or all supported CAD commands. Actual Visualize rendering and offline mesh preview require their own output evidence. More tools, stricter defaults, or faster results on these shapes do not establish universal superiority.

No unsupported, missing, or failed scenario is silently reclassified as successful.

Raw evidence: [performance](../benchmarks/results/performance-balanced.json).

[Derived analysis JSON](../benchmarks/results/performance-analysis.json).

## Reproduce the frozen 0.2.0 baseline

The source ZIP excludes `dist` and `.local`. Obtain the original PRO 0.2.0 wheel as a separate artifact; rebuilding current source is not a substitute for the measured baseline. Verify its hash before extracting it into the isolated import tree:

```powershell
$BaselineWheel = 'C:\path\to\solidworks_mcp_pro-0.2.0-py3-none-any.whl'
$ExpectedHash = '4dca5d879461e412440413a84f62a7ecec06cfe0af9dbc4db4b4d67a1cac2cf5'
if ((Get-FileHash -LiteralPath $BaselineWheel -Algorithm SHA256).Hash -ne $ExpectedHash) { throw 'Baseline wheel hash mismatch' }
$BaselineSource = Join-Path (Get-Location).Path '.local\pro-v0.2.0\src'
if (Test-Path -LiteralPath $BaselineSource) { throw 'Baseline extraction target already exists; inspect it before reuse' }
New-Item -ItemType Directory -Path $BaselineSource | Out-Null
.\.venv\Scripts\python.exe -m zipfile -e $BaselineWheel $BaselineSource
if ($LASTEXITCODE -ne 0) { throw 'Baseline extraction failed' }
```

The `pro_v020` spec sets `PYTHONPATH` only for its subprocess, requires the extracted package entry point, and checks initialized version `0.2.0`. Recreate the [competitor installations](competitor-review.md) and adjust manifest executable paths for your machine. Current and frozen PRO use the same dependency environment; their source trees remain separate.

The measured counterbalanced workload can then be rerun to a new output file:

```powershell
.\.venv\Scripts\python.exe -m benchmarks.run_live --live --servers pro,pro_v020,hjbaard,harrier,soheil,andrew --repeats 6 --warmup 1 --seed 20261103 --output benchmarks/results/performance-reproduction.json
```

Raw result files are not overwritten. Keep the warm-up evidence, planned/actual order, source hashes, and initialization responses with every reproduction.
