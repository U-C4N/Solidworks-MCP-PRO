# Third-party notices

The CAD engine is the separately installed `solidworks-mcp==0.19.0` package by Henk Jan Baard, under the MIT license: https://github.com/hjbaard/SolidWorks-MCP . Its 88 native tools and modelling guide remain attributable to that project. The small `native_guard.py` function adapts its `run_guarded` implementation while preserving failure/rollback behavior; the engine itself remains a dependency. The applicable upstream notice is reproduced below.

Protocol implementation uses the official Python MCP SDK (`mcp==1.30.0`, MIT): https://github.com/modelcontextprotocol/python-sdk . Other direct dependencies: jsonschema (MIT), Pillow (MIT-CMU), comtypes (MIT), and Windows pywin32 through the CAD engine (PSF license).

SOLIDWORKS and SOLIDWORKS Visualize are Dassault Systèmes products; this is an independent integration and does not include or grant their software licenses.

## Adapted native guard — upstream MIT notice

Copyright (c) 2026 Henk Jan Baard

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
