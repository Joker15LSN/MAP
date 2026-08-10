# tests/e2e

The ASGI/FakeCore flow test that used to live here was renamed to BFF
integration per R2-P1-05 and moved to
`tests/integration/test_bff_minimal_flow.py`.

The real cross-service E2E (real BFF + real map_core containers, real
HTTP/SSE, deterministic fake LLM at the LLM boundary only) is the
repo-level Compose runner:

```bash
python3 e2e/run_e2e.py
```

See `<repo>/e2e/README.md` for scenarios, ID-consistency checks and the
report format.
