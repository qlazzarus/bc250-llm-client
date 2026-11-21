# 모델 제어 구성

## 사용자 경로

시스템 화면과 일반 앱 인증 API로 서버 공용 LLM 모델을 조회하고 전환한다. `MODEL_CONTROL_ENABLED=true`일 때 Client가 `MODEL_CONTROL_SOCKET`의 공용 worker를 사용한다. 기본값은 비활성이며 활성 상태에서는 고정 `LLM_MODEL_ID`를 허용하지 않는다.

| API | 역할 |
|---|---|
| `GET /api/v1/llm/models` | 관리 모델과 현재 상태 |
| `GET /api/v1/llm/switches/current` | 현재 전환 작업 |
| `GET /api/v1/llm/switches/{operation_id}` | 특정 작업 조회 |
| `POST /api/v1/llm/switches` | 전환 접수 |
| `GET /api/v1/admin/gate-status` | gate 상태 |
| `PUT /api/v1/admin/gate-control` | 운영자 키로 Client pause/resume |

`ai-model` CLI도 같은 Unix socket worker를 호출한다. `list`, `status`, `switch`, `inspect-recovery`와 복구 명령을 제공하며 자세한 인자는 `ai-model --help`를 따른다. 전환은 작업 ID와 멱등 키를 사용한다. CLI 종료나 HTTP timeout이 worker 작업 취소를 뜻하지 않으므로 같은 작업을 조회한다.

## 전환 실행

worker는 영속 작업 DB, 공유 GPU lock, 전환 journal을 사용한다. 일반 순서는 작업 접수 → GPU lock → preflight → 이전 모델 확인 → journal 기록 → 자신의 gate hold와 배타 예약 → drain → 설정 교체·서비스 재시작 → health/모델 ID 검증이다.

상태는 queued, waiting_for_gpu, pausing, draining, switching, verifying, rolling_back과 succeeded, failed, failed_rolled_back, recovery_required를 구분한다. 실패 복구는 이전 설정 복원뿐 아니라 이전 모델의 health/ID 확인까지 필요하다. 결과가 불명하면 성공이나 안전한 해제로 추정하지 않는다.

공유 파일 경로는 `/run/bc250-gpu/gpu.lock`과 `/var/lib/bc250-model-control/switch.json`이다. Studio와 worker가 같은 파일을 보고 잠그는지 namespace와 inode를 검증한다. Client의 asyncio 세마포어는 프로세스별이며 이 공용 GPU lock을 대체하지 않는다.

## Gate protocol 3

[gate_candidate](../gate_candidate/)는 protocol 3 gate 구현이다. 이름이 candidate라고 해서 별도의 브라우저 API를 제공하는 것은 아니다. Client·Studio·전환 worker·관리자는 역할별 credential을 사용한다.

- `/model-snapshot`은 모델 목록을 조회하는 전후의 gate 상태와 epoch를 확인한다.
- `include_context=true`는 로드 모델의 meta.n_ctx와 실제 슬롯 n_ctx의 유효한 최소값을 반환한다. training context로 대체하지 않는다.
- 프록시 요청은 snapshot의 `X-Model-Epoch`를 보낸다. pause 또는 stale epoch는 입장 전에 409로 거절한다.
- 입장 전에 거절된 요청은 `X-BC250-Admission: rejected`로 구분한다. 이미 전송된 요청의 불명 결과와 같게 취급하지 않는다.
- active 스트림은 닫힐 때까지 drain 대상이다. 전송·종료 결과가 불명하면 안전한 drain 완료로 처리하지 않는다.
- 시작 시 startup hold를 추가하고 epoch를 바꾼다. 소유자별 hold와 복구 예약은 영속 저장한다.
- 예약 해제는 epoch·hold 목록·소유권을 같은 상태 변경 경계에서 비교한다. 다른 작업의 hold는 제거하지 않는다.

Client pause는 `client-<32자리 hex>` hold를 사용한다. resume는 자기 hold만 제거하므로 다른 hold가 남아 있으면 계속 paused다. Client hold는 모델 전환 예약을 차단한다.

## 모델별 요청 정책

모델 목록과 실제 로드 ID, 설정 context, runtime identity 및 기능 검증 기록을 함께 사용한다. reasoning 제어와 JSON schema 지원이 필요한 요청은 해당 capability가 확인돼야 한다. 지원하지 않거나 미확인인 기능을 임의로 지원한다고 가정하지 않는다.

채팅·요약·제목·이미지 의도/프롬프트는 현재 모델을 해석하고 모델별 예산을 적용한다. 최종 payload는 epoch를 붙여 계수한다. 모델이 바뀌면 이전 모델의 토큰 수를 재사용하지 않는다. 채팅의 압축 정책은 [채팅과 컨텍스트](chat-context.md)를 따른다.

## 운영 경계와 복구

worker runtime은 root 실행과 Unix peer 검사를 사용한다. 현재 소스의 `CLIENT_USER`는 `yeppyshiba`다. 다른 계정을 사용하려면 runtime과 socket unit, 관련 서비스 계정을 함께 맞춰야 한다. 같은 UID의 프로세스는 같은 신뢰 범위에 있다.

배포 설정은 [deploy/model-control](../deploy/model-control/)에 있다. gate와 worker는 Client 일반 배포와 별도 runtime·credential·state를 사용한다. `MODEL_CONTROL_ENABLED`와 worker의 `MODEL_CONTROL_SWITCH_ENABLED`는 별도 설정이다.

journal 손상, 작업 DB·hold·예약 불일치, restart 또는 해제 결과 불명은 복구 필요 상태로 남긴다. 기록을 삭제하거나 임의로 hold를 해제해 해결하지 않는다. `ai-model status`, `status --operation`, 권한이 있는 `inspect-recovery`로 상태를 확인한다.

단일 관리자 환경에서는 전환과 배포·서비스 재시작·벤치마크를 동시에 실행하지 않는다. 자동 배포와 전환 worker 사이에 전역 강제 배포 잠금이 있는 것은 아니다. 독립 외부 Wiki의 직접 llama 호출은 공용 admission/drain의 보호를 받지 않는다. 내부 Wiki의 LLM 요청은 Client 경로를 따른다.

origin 진단·서명 자료·독립 배포 승격 관련 모듈은 기본 전환 필수 경로와 구분한다. [origin 검토 모듈](../deploy/model-control/origin-review-contract.md)은 선택적 별도 계약이며 일반 모델 전환 활성화 조건에 자동으로 추가하지 않는다.
