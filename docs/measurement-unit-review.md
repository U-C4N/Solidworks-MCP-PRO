# Post-hoc inertia unit review — 3 November 2026

This is a diagnostic follow-up to the recorded volume checks, **not a
predeclared/scored benchmark case or an overall ranking**. Raw responses,
timings and case outcomes have not been changed. PRO's inherited mass tool
does not return inertia, so this observation does not establish that PRO is
better at inertia reporting; that remains a feature gap.

## Recorded observation

In all three Andrew samples in
[`live-after.json`](../benchmarks/results/live-after.json), run
`0adfacf67f63`, the empty-arguments `get_mass_properties` response labels its
inertia values `kg·mm²` but reports approximately:

| Diagonal | Recorded value | Recorded value × 10⁶ | Analytical value, kg·mm² |
|---|---:|---:|---:|
| Ixx | 1.05587997802216e−5 | 10.5587997802216 | 10.558799780221596 |
| Iyy | 2.61587997802216e−5 | 26.1587997802216 | 26.1587997802216 |
| Izz | 3.55882190275490e−5 | 35.5882190275490 | 35.58821902754904 |

Multiplying the recorded diagonal values by 10⁶ agrees with the analytical
calculation within a maximum relative error of `5.05e-16` across these three
samples. Harrier's text output reports `10.5588`, `26.1588`, `35.5882` kg·mm²;
soheil's structured output reports the same values at its stated precision.
These agreement checks apply to this one symmetric fixture.

The raw file's SHA-256 is
`c14685a29e9e643a46f47ccfe094944e977b851a607cac7ee4140da0aa9710fd`.
The separate [derived volume report](../benchmarks/results/mass-volume-checks.json)
records the same source hash and checks all 21 returned volumes. It does not
score inertia.

## Analytical check and reference frame

The post-edit fixture is an 80 × 50 × 12 mm rectangular plate with a centered
Ø10 mm through-hole along Z. At density 1000 kg/m³, the uncut box mass is
`0.048 kg` and the removed cylinder mass is
`π × 5² × 12 × 10⁻⁶ = 0.0009424777960769379 kg`. The recorded mass/volume ratio
also corresponds to density 1000 kg/m³. The centered hole leaves the center
of mass at `(40,25,6)` mm, so the component moments can be subtracted directly
about that common center:

```python
from math import pi

box_mass = 0.048
removed_mass = pi * 25 * 12 * 1e-6
Ixx = box_mass * (50**2 + 12**2) / 12 - removed_mass * (3*25 + 12**2) / 12
Iyy = box_mass * (80**2 + 12**2) / 12 - removed_mass * (3*25 + 12**2) / 12
Izz = box_mass * (80**2 + 50**2) / 12 - removed_mass * 25 / 2
# kg·mm²: 10.558799780221596, 26.1587997802216, 35.58821902754904
```

The API parameter used in the inspected source is `GetMomentOfInertia(0)`.
SOLIDWORKS defines enum value 0 as the center-of-mass frame. This avoids
confusing a model-origin inertia with a center-of-mass inertia in this
comparison. [Official `swMassPropertyMoment_e` documentation](https://help.solidworks.com/2026/English/api/swconst/SolidWorks.Interop.swconst~SolidWorks.Interop.swconst.swMassPropertyMoment_e.html).

## Pinned source and unit evidence

The inspected revision is
[`andrewbartels1/SolidworksMCP-python@126beed0e7a13214ebdd5724929f4c095cf2642f`](https://github.com/andrewbartels1/SolidworksMCP-python/tree/126beed0e7a13214ebdd5724929f4c095cf2642f).
The tracked source was unchanged in the local benchmark checkout.

- [`adapters/solidworks/io.py:2218`](https://github.com/andrewbartels1/SolidworksMCP-python/blob/126beed0e7a13214ebdd5724929f4c095cf2642f/src/solidworks_mcp/adapters/solidworks/io.py#L2218)
  creates an `IMassProperty` object. This path multiplies volume by 10⁹, area
  by 10⁶ and center coordinates by 1000.
- [`io.py:2235`](https://github.com/andrewbartels1/SolidworksMCP-python/blob/126beed0e7a13214ebdd5724929f4c095cf2642f/src/solidworks_mcp/adapters/solidworks/io.py#L2235)
  retrieves `GetMomentOfInertia(0)`, and
  [`io.py:2275`](https://github.com/andrewbartels1/SolidworksMCP-python/blob/126beed0e7a13214ebdd5724929f4c095cf2642f/src/solidworks_mcp/adapters/solidworks/io.py#L2275)
  forwards its entries without a scale conversion. This function does not
  set `UseSystemUnits`.
- [`tools/analysis.py:398`](https://github.com/andrewbartels1/SolidworksMCP-python/blob/126beed0e7a13214ebdd5724929f4c095cf2642f/src/solidworks_mcp/tools/analysis.py#L398)
  labels those forwarded values `kg·mm²`.

SOLIDWORKS documents the `UseSystemUnits` default as true, with meters and
kilograms used for values. Converting inertia from kg·m² to kg·mm² therefore
requires a factor of 10⁶. [Official `UseSystemUnits` documentation](https://help.solidworks.com/2025/english/api/sldworksapi/SolidWorks.interop.sldworks~SolidWorks.interop.sldworks.IMassProperty~UseSystemUnits.html).

For this fixture, the recorded numbers are consistent with unscaled kg·m²
values beneath a kg·mm² label. The source path and analytical check support
that interpretation. This document records the evidence without changing any
benchmark score or claiming a defect across all versions, adapters, reference
frames or mass-property override configurations. A dedicated predeclared
inertia test with its own direct COM oracle would be needed for that broader
comparison.
