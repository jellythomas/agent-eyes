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

The `macos-arm64-py312-v0.10.0-*` files are the 2026-07-28 deterministic
release-candidate evidence from the working tree based on `6a18442`, before the
immutable release commit and exact-wheel live run. They cover:

- 30 known/discovery transaction samples after three warmups;
- deterministic call, scan, activation, external-write, shadow, event, and
  output-budget gates;
- cancellation at seven transaction boundaries and 32 queued foreground calls;
- 10,000 snapshot/reference/subscription/worker/resolver lifecycle cycles with
  the max(10 MiB, 5%) RSS gate; and
- the compact 16 KiB catalog and zero-fixed-orchestration-sleep gate.

The exact tagged-wheel live result is generated after the immutable commit so it
can carry the final wheel SHA-256 without creating circular build provenance.
