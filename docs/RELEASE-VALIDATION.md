# Public release validation (2026-10-24)

- Regression: 61 tests passed on Python 3.12 / Windows, including existing dataset durability/privacy/ACK tests and new generic SDK/MCP tests.
- Independent wheel installation: Collector installed into a fresh environment with no Motor or DAXQO harness package.
- External client: official MCP Python SDK 1.30.0 negotiated 2025-11-25, recorded a synthetic action, retried it without duplication, queried statistics and verified an export manifest.
- The server ran from a temporary working directory, without an Android bridge, ADB, model provider or sibling source checkout.
- Native source staging against the explicit public DAXQO commit `3de65f9e9751c67d7118e72e789f898901a50ce6` succeeded.
- Wheel and source distributions built in isolated environments.

These release checks did not reinstall APKs, change device permissions or run new physical-phone/model tasks. Historical device acceptance remains separate. Runtime datasets, cursors, pairing state and generated reports are local operator data, not release contents.

CI repeats regression, package builds and the independent MCP client check on Linux/Windows with Python 3.10/3.12.
