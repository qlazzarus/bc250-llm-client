# Protocol 3 gate

이 디렉터리는 Client·Studio·모델 제어 worker가 공유하는 admission proxy와 영속 제어 상태를 구현한다. 전체 연결은 [모델 제어 구성](../docs/model-control.md), 설치 경계는 [배포 구성](../deploy/model-control/README.md)을 따른다.

`create_app`은 state 경로, upstream URL, admin/Studio/switch credential과 선택적 Client·upstream credential을 받는다. systemd runtime은 별도 Client credential을 요구한다. `provision(path)`는 startup hold가 있는 새 state를 배타적으로 생성하며 기존 state 위에 실행하지 않는다. gate는 한 프로세스가 수명 동안 파일 잠금을 보유한다.

## 요청과 제어

`/control`과 `/control/identity`는 protocol 3 역할 인증을 제공한다. `/model-snapshot`은 upstream 모델 목록을 읽는 전후의 상태를 확인하고 ready, epoch, models를 반환한다. `include_context=true`는 유효한 로드 meta.n_ctx와 슬롯 n_ctx의 최소값을 context_tokens로 반환한다.

추론과 계수 요청은 `X-Model-Epoch`를 전달한다. paused 또는 stale 요청은 입장 전에 409로 거절하고 대기열에 넣지 않는다. `X-BC250-Admission: rejected`는 입장 전 거절일 때만 gate가 설정하며 incoming/upstream의 같은 헤더는 제거한다. Client는 이 신호와 이미 upstream으로 보낸 요청의 불명 결과를 구분한다.

스트림은 종료까지 active로 유지한다. forwarding 결과나 upstream 종료가 불명하면 drain 완료를 주장하지 않고 gate를 unavailable로 유지한다.

## 영속 상태

프로세스 시작 시 startup hold를 추가하고 epoch를 바꾼다. switch·Studio hold 취득도 epoch를 바꾸며 기존 hold와 복구 예약은 재시작 후에도 유지한다. 쓰기는 uncertainty marker, atomic replacement와 fsync를 사용한다. 중단·불명 쓰기가 있으면 조사 전 기동을 거절하며 marker를 자동 삭제하지 않는다.

복구 예약은 경쟁하는 Studio/switch 진입을 차단한다. 조건부 해제는 epoch와 필요한 hold, 예약 소유자를 같은 admission lock 안에서 비교한다. Client pause/resume는 설정된 `client-<32hex>` hold만 변경하며 다른 역할의 hold를 제거하지 않는다.

## Runtime과 검사

`runtime.py`와 systemd unit은 별도 gate venv·credential·state 디렉터리를 사용한다. state 디렉터리와 상위 경로의 소유권·접근 제한은 배포 설정의 책임이다. service는 migration/provision을 자동 수행하지 않는다.

`migration.provision_from_legacy_snapshot`은 검증된 drained 상태의 기존 pause를 보존해 새 state로 변환하고 덮어쓰기를 거절한다. 이 함수 자체가 구 프로세스 정지나 외부 제어자의 동시 진입을 막지는 않는다.

```sh
PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_gate_candidate_protocol3.py tests/test_worker_gate.py tests/test_worker_recovery_reservation.py
```
