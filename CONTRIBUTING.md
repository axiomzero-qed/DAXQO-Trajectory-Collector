# Contributing

Use Python 3.10+ in a virtual environment. Install `.[dev]`, run `python -m unittest discover -s tests`, `python -m build` and `python examples/mcp_smoke.py`.

Keep public APIs independent of developer paths, harnesses and model providers. Add adapters at explicit boundaries. Use synthetic fixtures in automated tests; physical-device tests need an explicitly selected device.

Exclude datasets, pairing state, provider configuration, generated reports and website sources. Document protocol/schema changes and preserve cancellation/idempotency.
