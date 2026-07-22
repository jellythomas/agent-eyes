# Benchmark results

These files are the accepted Agent Eyes 0.9.0 reference run from 2026-07-21 on
macOS arm64 with CPython 3.12.11. The schema 4 setup-lock stress result was
refreshed on 2026-07-22 after the final Windows process-lock hardening. Latency
benchmarks use 30 measured samples after 3 warmups unless the result records a
different protocol. Fixtures are deterministic and exclude live browser
rendering and network latency.

Reproduce them from the repository root with the commands in the main README.
The comparison input is
`../baselines/macos-arm64-py312-pre-hardening.json`.
