# Benchmark coordination candidates

These candidates were derived from the original operating files at
`/opt/ai-benchmark` and installed as part of the M8 coordinated rollout. The
pre-rollout source SHA-256s were:

- `benchmark.py`: `d44a3030911195d8a950542eb588cbe8e7955227b52abe8e1213170db28534d6`
- `run-comparison.py`: `f7db2d20b5c2b16de38547202c510498dd23c004415c0b7a47a59b708dc208f1`

The original files are included in the root-only pre-rollout backup; an actual
full production restore has not been tested. Recheck installed versions and
coordination before any later replacement. The existing
`benchmark-memory.py` launches `benchmark.py`; its observation-only systemd
queries do not need a separate model-control path. No prompt file or benchmark
results are included here.

`benchmark.py` binds each suite to one configured model, exact loaded GGUF ID
and gate epoch. Chat and token counting both go through port 8081 and carry that
epoch. A stale epoch, hold, timeout, lost response or malformed response stops
the suite without automatically retrying inference. A new run must obtain a new
snapshot. The new count uses llama's chat token-count endpoint; it is not the
old plain-text `/tokenize` approximation.

`run-comparison.py` calls the shared `ai-model switch` for every selection,
including the current model. It does not directly start, stop or restart
llama-server and requires it to be running before beginning. It prints a unique
idempotency key before each switch, passes it with `--key`, and retains any
operation ID in a timeout error. Any switch failure, unknown result, epoch
change or interrupted suite stops further model changes and skips automatic
restoration. Inspect `ai-model status` and, when known, `ai-model status
--operation <id>` before deciding how to proceed. A timeout or killed CLI does
not cancel the worker operation.

After every suite completes successfully, the candidate rechecks the last
snapshot before using the shared CLI to restore the original model. The recheck
and subsequent submission are not one atomic operation: in this single-admin
environment, do not issue concurrent manual or UI switches while a comparison
is running. Any conflicting hold remains controlled by the shared worker/gate;
the comparison never removes another owner's hold. The independent Wiki direct
llama caller remains the separately documented exception.
