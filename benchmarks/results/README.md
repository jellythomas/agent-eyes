# Benchmark results

These files are the accepted post-tag Agent Eyes 0.9.0-version branch reference
from 2026-07-21 and 2026-07-22 on macOS arm64 with CPython 3.12.11. They do not
describe the immutable v0.9.0 tag or published PyPI artifact. The schema 4
setup-lock stress result was refreshed after the final Windows process-lock
hardening. Startup was refreshed from implementation commit `0f26f8e`; runtime,
context-budget, and the executable no-fixed-sleep gate were refreshed from
commit `5a0fb16`. Benchmarks use 30 measured samples after 3 warmups unless the
result records a different protocol. Fixtures are deterministic and exclude
live browser rendering and network latency.

Reproduce them from the repository root with the commands in the main README.
The comparison input is
`../baselines/macos-arm64-py312-pre-hardening.json`.
