# Benchmark coordination

이 디렉터리의 벤치마크는 [공용 모델 제어](../../docs/model-control.md)와 protocol 3 gate를 사용한다. 설치 경로의 실제 버전·설정은 실행 전에 확인한다.

`benchmark.py` binds each suite to one configured model, exact loaded GGUF ID
and gate epoch. Chat and token counting both go through port 8081 and carry that
epoch. A stale epoch, hold, timeout, lost response or malformed response stops
the suite without automatically retrying inference. A new run must obtain a new
snapshot. Token counting uses llama's chat token-count endpoint; it includes the chat template rather than only plain text.

`run-comparison.py` calls the shared `ai-model switch` for every selection,
including the current model. It does not directly start, stop or restart
llama-server and requires it to be running before beginning. It prints a unique
idempotency key before each switch, passes it with `--key`, and retains any
operation ID in a timeout error. Any switch failure, unknown result, epoch
change or interrupted suite stops further model changes and skips automatic
restoration. Inspect `ai-model status` and, when known, `ai-model status
--operation <id>` before deciding how to proceed. A timeout or killed CLI does
not cancel the worker operation.

After every suite completes successfully, the runner rechecks the last
snapshot before using the shared CLI to restore the original model. The recheck
and subsequent submission are not one atomic operation: in this single-admin
environment, do not issue concurrent manual or UI switches while a comparison
is running. Any conflicting hold remains controlled by the shared worker/gate;
the comparison never removes another owner's hold. The independent Wiki direct
llama caller remains the separately documented exception.
