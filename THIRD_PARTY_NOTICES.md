# Third-party notices

SolidWorks MCP PRO v0.7.0 contains its own CAD automation implementation in
`src/solidworks_mcp_pro/native/`. It does not include or require an external CAD
MCP implementation. The independent native session and transaction guard replace
the earlier engine dependency and adapted guard.

The current distribution uses these separately licensed libraries:

| Component | Purpose | License | Source |
|---|---|---|---|
| Python MCP SDK (`mcp`) | MCP protocol | MIT | https://github.com/modelcontextprotocol/python-sdk |
| jsonschema | Request and result validation | MIT | https://github.com/python-jsonschema/jsonschema |
| Pillow | Image handling | MIT-CMU | https://github.com/python-pillow/Pillow |
| pywin32 | Windows COM access | PSF | https://github.com/mhammond/pywin32 |
| comtypes | Native Visualize interface access | MIT | https://github.com/enthought/comtypes |

Dependency distributions retain their own license files. Development tools have
separate licenses and are not bundled into the CAD engine.

SOLIDWORKS and SOLIDWORKS Visualize are Dassault Systèmes products. The integration
loads type information from the user's installed SDK and automates their licensed
applications; it does not distribute the applications, their geometry kernel,
material libraries or SDK type libraries, or grant licenses to them.

Versioned validation reports, benchmark records, prior release archives and Git
history describe the dependencies and notices applicable at the time. They are
historical evidence, not dependencies or performance measurements of v0.7.0.
