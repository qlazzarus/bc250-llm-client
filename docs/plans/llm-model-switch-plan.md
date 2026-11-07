# LLM Client 모델 전환 통합 계획

최종 정리: 2026-11-07. 상태: **운영 ai-model 원문 분석 완료, 상세 설계 진행 중. Client 구현·코드 이관·운영 적용 전**.

후속 설계: [운영 ai-model 분석 및 CLI/API 공용 실행부 설계](llm-model-switch-design.md). 사용자가 제공한 실제 원문에서 확인된 실패 경로와 변경 구조를 기록한다.

이 문서가 두 프로젝트의 **LLM 모델 전환 작업에 대한 단일 기준 문서**다. imagegen의 과거 API 전용 계획, T1~T6 배포 체크리스트, 조사·시험 기록보다 현재 목표와 작업 순서에서 우선한다. 과거 시험 결과와 실제 제약을 폐기하거나 미완료를 완료로 바꾸는 뜻은 아니다. 이미지 생성 기능 자체의 계획은 각 기존 문서를 따른다.

## 1. 확정된 사용자 요구와 프로젝트 경계

- `bc250-imagegen`: stable-diffusion 기반 AI Studio. 이미지 생성이 본래 역할이다.
- `bc250-llm-client`: llama-server의 **모델 선택 UI와 서비스 전환 API**를 제공할 주 프로젝트다.
- 운영자는 서버 콘솔에서 기존 `ai-model` 명령으로 모델을 전환해 왔다. 이 운영 방식을 UI/API로 연결한다.
- 필요하면 imagegen의 기존 전환 코드를 llm-client로 옮길 수 있다. 코드 이관 자체보다 실제 기능 완성이 목표다.
- 현재 요청은 진행 내용과 후속 계획의 통합 문서화다. 이 정리에서 기능 코드 이동·구현·서비스 변경·시험 VM 실행·배포는 하지 않는다.

최종 완료 기준: **LLM Client에서 설치 모델 B를 선택 → llama-server가 A에서 B로 실제 전환 → 현재 모델 표시 갱신 → 다음 대화가 B로 정상 응답**. 실패하면 이전 모델 복구 여부와 관리자 조치 필요 상태가 표시돼야 한다. UI 없는 관리 API 배포나 전환 비활성 응답 확인만으로 완료하지 않는다.

서버 전체 활성 LLM을 전환하는 기능이다. 대화마다 서로 다른 모델을 동시에 올리는 기능, 이미지 모델 선택, 모델 다운로드·삭제·설정 편집은 이번 범위가 아니다.

## 2. 현황과 근거의 한계

2026-11-07 로컬 검토 기준 imagegen HEAD는 `7e595b1`, Client HEAD는 `0e3b418`이었다. imagegen에는 전환·배포 관련 수정 및 미추적 파일이 다수 있다. Client에는 이 기능의 구현이 없다. 이번 정리의 새 문서를 제외한 기존 작업은 보존한다.

| 영역 | 확인한 상태 | 다음 판단 |
| --- | --- | --- |
| Client UI | 시스템 상태에서 활성 모델과 모델 ID 목록만 표시 | 선택·전환·진행·실패 UI 추가 |
| Client API | `/api/v1/models`가 gate `/v1/models`를 조회 | 설치 모델 관리와 전환 API 추가 |
| imagegen 제어 후보 | 8091 HTTP → Unix 소켓 → root 작업자, 전환·복구·작업 기록 코드 존재 | 필요한 핵심만 Client 소유 실행부로 이관/적응 |
| 실제 명령 연결 | 기존 후보는 활성 설정 직접 변경과 systemctl 사용. ai-model 호출 어댑터와 같지 않음 | 운영 ai-model 확인 후 단일 제어 경로 결정 |
| 운영 활성화 | 문서상 신규 서비스 미설치, 전환 기본 비활성 | 현 서버 재확인 필요 |
| 배포 시험 | T1 완료 기록, T2~T6 부분 구현·시험. DB 차단 연구까지 확장 | 과거 결과 보존, 기능 완료의 필수 작업 목록으로 승계하지 않음 |

운영/VM 시험 통과는 과거 운영자가 제공한 기록이다. 이번에는 서버 접속·시험 재실행을 하지 않았다. 과거 테스트 개수, 18/36(50%) 등의 수치는 새 계획의 기능 완성률이 아니다. 이 문서 작성으로 최종 작업 트리가 검증됐다고 주장하지 않는다.

## 3. 제어 구조

```text
브라우저의 LLM Client UI
  → Client 백엔드의 인증된 모델 관리 API (:8010, /llm/api/v1/...)
  → 제한된 로컬 모델 제어 실행부 (필요한 권한만 분리)
  → 기존 ai-model 명령 / 검토 후 정한 공용 제어 계층
  → llama-server.service

전환 실행부 ↔ LLM gate 및 공유 GPU 조율 ↔ 이미지 작업
```

운영 ai-model 원문 분석 후 기본안을 구체화했다. **기존 ai-model 명령 인터페이스를 유지하고 CLI/API가 하나의 공용 작업자를 호출하도록 내부를 정리한다.** 원본 Bash를 그대로 호출하는 단순 래퍼는 즉시 restart 실패·미검증 복구·동시 전환 문제를 해결하지 못하므로 채택하지 않는다. CLI 교체는 향후 검증된 배포 단계의 변경이며 현재 원본은 수정하지 않았다. **ai-model의 설정과 별개인 활성 모델 체계를 추가하지 않는다.** 같은 설정에 대한 경쟁하는 두 전환 구현도 피한다.

Client 웹 서버 전체를 root로 실행하지 않는다. 고정된 실행 파일과 검증된 모델 이름을 인자 배열로 전달하고, 임의 셸 명령·경로·서비스명을 API 입력으로 받지 않는다. 권한 실행부의 코드와 실행 환경은 일반 웹 서비스가 수정할 수 없도록 분리한다.

별도 8091 HTTP 서비스는 필수가 아니다. Client→Unix 소켓의 직접 연결을 우선 검토하며, 기존 bridge의 허용 UID가 `ai-studio`에 묶여 있으므로 Client의 실제 서비스 계정으로 재설계한다. UI가 socket이나 서버 관리 키를 직접 다루지 않는다.

gate와 이미지 GPU 조율은 실제 동시 실행 충돌을 막기 위해 필요하다. 반면 Studio DB 전체 이전, 모든 서비스의 일괄 배포, 유지보수 부팅, 통합 Actions 자동 배포를 기능 구현의 기본 선행 조건으로 두지 않는다. 필요한 gate/Studio 변경이 있다면 영향·호환성·설치·복구 범위를 별도 명시한다.

## 4. 재사용·이관 목록

아래 원본 경로는 모두 **bc250-imagegen 저장소 기준**이다. Client의 목적 경로는 제안이며 아직 생성하지 않았다.

| 원본 | 처리 | Client 쪽 적용 및 주의점 |
| --- | --- | --- |
| `backend/app/services/model_switch.py`, `switch_worker.py`, `switch_operations.py` | 핵심 재사용 후보 | `backend/app/model_control/` 등 별도 패키지. 전환 상태·중복 방지·복구 기록을 이관하되 Studio 의존 분리 |
| `model_control.py`, `systemd_model.py` | 부분 재사용 | 모델 이름·상태 검증 활용. 활성 설정 직접 쓰기와 ai-model 실행 중 한 제어 방식으로 통일 |
| `switch_adapters.py`, `backend/app/core/files.py`, `services/llm.py` | 필요한 의존만 추출 | 공용 잠금·gate drain·권한 검증. 전체 Studio 패키지 복사는 하지 않음 |
| `model_control_bridge.py`, `model_control_runtime.py` | 구조 재사용·수정 | UID·경로·credential·서비스 사전 검사와 복구 책임을 Client 실행부 기준으로 재정의 |
| `api/model_management.py`, `api/remote_model_management.py`, `services/model_management_server.py` | API 계약 참고 | Client의 기존 FastAPI 앱에 라우트 연결. 8091 서비스는 필요가 확인될 때만 유지 |
| `backend/tests/test_model_switch.py`, `test_switch_worker.py`, `test_switch_operations.py`, `test_switch_integration.py`, `test_model_management_api.py` 및 관련 테스트 | 관련 구현과 함께 이관 | import·fixtures·실행 환경 수정. 과거 통과는 이관 후 통과를 대신하지 않음 |
| `systemd/bc250-model-worker.*`, `bc250-gpu-lock.conf` | 설치 템플릿 참고 | 실행 경로·계정·권한·기존 잠금 호환성 재검토. 그대로 설치하지 않음 |
| `backend/app/gate.py`, Studio admission/worker 및 복구 연동 | 원본에 유지 | 변경이 꼭 필요하면 imagegen에서 별도 최소 변경. Client가 이미지 큐/DB를 소유하지 않음 |
| `deploy/ai-model-recovery-candidate` | 참고 자료 | 운영 ai-model 원본이 아니다. 단독 설치하거나 검증된 복구 구현으로 간주하지 않음 |
| `deploy/model-control/` 전체, VM/nft/DB fence 시험, 후보 Actions | 보존·후속 분리 | 새 설계에 실제 필요한 부분만 선택. 전부 이관하거나 시험 완료를 전제로 삼지 않음 |

이관 시 주의: 두 프로젝트 모두 `app` 패키지 이름을 쓰고 Client는 sqlite3 중심, imagegen은 자체 설정·파일 잠금·서비스 계층을 가진다. `app.services`만 복사하면 실행 경로와 의존성이 맞지 않는다. 재사용 모듈의 import·설정·테스트 의존 목록을 먼저 작성한다. 새 전환 작업 저장소는 Client 대화 DB나 Studio DB와 무조건 합치지 말고 권한 경계에 맞춰 둔다.

원본은 새 경로의 검증과 참조 전환이 완료될 때까지 보존한다. 최종 정리 때 소유자를 하나로 정하고 중복 실행 서비스 및 오래된 entrypoint를 제거/비활성화한다. 미추적 후보 파일도 삭제하거나 누락시키지 않는다.

## 5. 사용자 기능과 API 초안

Client가 제공할 서버 내부 경로의 제안이다. 브라우저에서는 기존 Caddy 규칙에 따라 `/llm` 접두사가 붙는다. 현재 구현된 API라는 뜻은 아니다.

| API | 역할 |
| --- | --- |
| `GET /api/v1/llm/models` | ai-model과 일치하는 설치 모델 목록, 설정상 선택 모델과 실제 로드 모델·준비 상태 |
| `POST /api/v1/llm/switches` | `{model: 관리용이름}`과 Idempotency-Key로 전환 접수, 202와 작업 ID |
| `GET /api/v1/llm/switches/{id}` | 진행 단계, 결과, 안전한 오류 코드 |
| `GET /api/v1/llm/switches/current` | 페이지 새로고침 후 진행/복구 필요 작업 재발견. 동적 ID 라우트보다 먼저 등록하거나 별도 경로로 설계 |

기존 추론 모델 조회 `/api/v1/models`와 이미지 모델 API는 의미를 바꾸지 않는다. 관리용 이름과 GGUF 경로 ID를 구별한다.

UI는 현재 모델, 설치 모델 선택, 전환 버튼, 전역 변경 안내, 진행/복구 상태와 결과를 제공한다. 같은 모델 재선택은 실제 health 확인 후 불필요한 재시작 없이 처리한다. 전환 도중 중복 버튼을 막되 API에서도 중복을 방지한다. HTTP 연결이나 페이지가 닫혀도 실행 중 작업을 잃지 않는다.

기존 상태 계약을 우선 재사용한다: `queued`, `waiting_for_gpu`, `pausing`, `draining`, `switching`, `verifying`, `rolling_back`, `succeeded`, `failed`, `failed_rolled_back`, `recovery_required`. 후속 상태 축소는 UI와 복구 의미를 보존할 때만 한다.

전환은 관리자에게 제한하는 기존 제안을 유지한다. Client의 `APP_AUTH_MODE=local`은 사용자 인증이 아니며 같은 LAN 접근자를 관리자와 구분하지 못한다. 첫 버전에 적용할 단일 운영자 인증/권한 경계를 설계하고, 일반 local 요청에 자동으로 전환 권한을 추가하지 않는다. 다중 사용자 계정 시스템 전체 구현까지 범위를 넓히지는 않는다.

## 6. 구현 전 해결할 확인 사항

| 확인 | 이유와 종료 조건 |
| --- | --- |
| 실제 ai-model 원본과 llama unit | `/usr/local/bin/ai-model` 링크와 `/opt/ai/scripts/ai-model`, list/status/switch 입력·출력·종료 코드·복구 동작 확인. 비밀값 제외 |
| Client 실제 계정·권한 | 저장소 service 템플릿 계정은 placeholder. socket 호출 주체와 제한 실행부 설치 방식 확정 |
| 실제 gate 기능·키·잠금 | owned hold는 opt-in 후보. 현재 호환 여부와 키 불일치 기록 재확인. 기존 pause를 전환 종료 시 임의 해제하지 않음 |
| 이미지 동시 실행 | status를 한 번 조회하는 것만으로 경쟁을 막지 못함. 모든 자동 GPU 경로가 같은 잠금/차단 계약을 따르는지 확인 |
| Wiki 등 직접 llama 호출 | 과거 직접 8080 호출 기록 존재. gate 경유 변경 또는 명확한 실행 제한 필요. Client만 멈췄다고 모든 요청이 멈춘 것으로 간주하지 않음 |
| Client 모델 고정 설정 | `LLM_MODEL_ID`가 고정이면 전환 후 오류 가능. 활성 모델 추종과 수동 콘솔 변경 감지 정책 확정 |
| 모델별 추론 설정 | 공통 `llm_context_tokens`, reasoning/JSON schema 옵션의 호환성 확인. 필요한 모델 메타데이터 또는 보수적 공통 설정 채택 |
| 실패와 중단 | ai-model timeout 뒤 실제 재시작이 진행 중일 수 있음. 결과 불명 시 추가 전환/자동 해제 금지, 복구 기록과 확인 절차 확보 |
| 수동 콘솔 전환 | CLI/API의 상태 일치 확인. 콘솔 호출이 잠금을 우회하는지 확인하고, 공용 조율 또는 동시 사용 금지 등 운영 정책 명시 |

자료 확인은 읽기 전용으로 시작한다. 실패 복구·SELinux·파일 권한에 관한 과거 발견은 실제 채택한 경로에 관련되면 계속 해결해야 한다. 범위 축소를 이유로 무조건 성공 처리하거나 안전 검사를 제거하지 않는다.

## 7. 실행 체크리스트와 완료 조건

문서 정리 외 모든 기능 단계는 미완료다. 과거 T번호와 혼동하지 않도록 새 작업은 M번호로 추적한다.

| 작업 | 상태 | 산출물과 완료 조건 |
| --- | --- | --- |
| M0 통합 계획 | 완료 | 사용자 목표·소유 저장소·기존 후보 분류·참고 문서 우선순위 정리 |
| M1 실제 명령/환경 계약 | 원문 분석 완료·환경 확인 대기 | ai-model·unit·계정·gate·GPU 호출자 읽기 전용 조사, 미확인 사항 기록. 콘솔 명령과 API 어댑터 계약 확정 |
| M2 구현/이관 설계 | 상세 설계안 작성·환경 의존 결정 대기 | 목적 파일·의존 모듈·권한 경계·인증·CLI 공존·실패 처리 결정. 필요한 gate/Studio 변경 별도 목록 |
| M3 Client 전체 사용자 흐름 | 미착수 | 가짜 실행부로 UI→API→작업→진행 조회→결과 표시. 인증·중복·새로고침 복원 포함 |
| M4 실제 제어 실행부 | 후보 존재, 미이관 | 기존 코드 선별 이관, ai-model 어댑터, 작업/복구 기록, root 경계, gate/GPU 조율. 고립된 환경에서 Client API 경로로 연결 |
| M5 전환 후 Client 호환 | 미착수 | 활성 모델 갱신·고정 ID 해소·컨텍스트 정책, 채팅·요약·제목·이미지 프롬프트의 전환 중 요청 제어 |
| M6 통합 검증 | 과거 부분 증거만 존재 | 아래 수용 시나리오를 같은 최종 후보로 검증. 가짜 실행과 실제 GPU 결과 구분 |
| M7 배포·복구 준비 | 기존 자료 참고, 새 안 미작성 | Client와 필요한 공용 구성만 포함한 설치·백업·롤백·사전 점검 절차 및 변경 목록. imagegen 전체 이전을 기본값으로 두지 않음 |
| M8 운영 기능 확인·정리 | 미착수 | 실제 UI 모델 전환과 후속 대화 성공, CLI/API 상태 일치, 원본/중복 후보 정리와 최종 운영 문서 갱신 |

순서: M1→M2, M3은 계약을 정하면 가짜 실행부로 진행 가능. M4·M5를 연결한 뒤 M6→M7→M8. 구현·문서가 준비되기 전에 운영 서비스나 모델을 바꾸지 않는다. 단계마다 완료/남음/증거/다음 작업을 이 문서에 갱신한다.

### 수용 시나리오

1. 설치된 A→B 전환과 같은 모델 선택, 목록·현재 모델·실제 모델 ID 일치, B로 후속 대화.
2. 이미지 작업 또는 진행 중 채팅과 전환이 겹쳐도 충돌하지 않음. 새 채팅/요약 등 전환 중 요청 처리 확인.
3. 중복 키는 같은 작업 반환, 다른 동시 전환은 거절, 잘못된 모델과 권한 없는 요청은 실행 전 거절.
4. 새 모델 시작/health 실패 시 이전 모델 복구와 health 확인. 복구 실패 시 상태 유지 및 새 전환 차단.
5. HTTP 유실·새로고침·작업자 종료/재시작 후 같은 작업 추적. 결과가 불명확하면 무조건 재실행하지 않음.
6. 기존 관리자 pause와 이미지 복구 hold 보존, CLI 수동 변경과 API 상태 일치, Client 기존 대화/이미지 연동 회귀.

검증은 변경 범위에 필요한 단위·통합·Client lint/build와 실제 환경 차이만 대상으로 한다. 통과한 시험은 관련 변경·새 실패·최종 연결의 필요가 있을 때 재실행한다. 새 시험을 추가할 때 해결할 결함과 수용 조건을 먼저 명시한다. 설계가 막히면 미세 VM 시험을 계속 늘리지 않고 설계 판단으로 돌아온다.

## 8. 과거 문서의 해석과 보존

다음 원본은 imagegen 저장소 `docs/`에 있으며 상단에서 이 문서를 안내한다. 원문은 당시 결정과 시험 증거를 위해 보존한다.

| 문서/묶음 | 현재 용도와 더 이상 적용하지 않는 부분 |
| --- | --- |
| `LLM_MODEL_SWITCH_PROPOSAL.md` | 최초 요구·설계 배경. “API/root 서비스 미구현” 등 시점 표현은 최신 현황이 아님 |
| `LLM_MODEL_SWITCH_WORKPLAN.md`, `LLM_MODEL_MANAGEMENT_API.md` | 과거 단계·API 계약 참고. “Client 제외/API까지만” 범위는 현재 목표에 적용하지 않음 |
| `LLM_MODEL_SWITCH_DEPLOYMENT_{TASKS,GAPS,DRAFT}.md`, `LLM_MODEL_SWITCH_ROLLOUT_PLAN.md` | 과거 imagegen 통합 배포 후보와 위험 기록. T1~T6가 Client 기능의 필수 경로라는 전제는 종료 |
| `LLM_MODEL_SWITCH_REMAINING_WORK.md` | 시점별 시험 이력. 과거 성공을 현재 최종 회귀 결과로 읽지 않음 |
| `LLM_MODEL_SWITCH_DB_EXCLUSION_DESIGN.md` | DB 차단 연구 보존. 새 기능의 다음 우선 작업이 아님 |
| `LLM_MODEL_SWITCH_DIRECTION_REVIEW.md` | 재정렬에 이른 조사 기록. 후속 작업은 본 통합 계획을 따름 |
| `BC250_SERVICE_INVENTORY.md` | 당시 서비스 구성. “이번 범위 Client 제외”는 과거 범위이며 실시간 구성도 아님 |
| `NEXT_PROJECT_LLM_CLIENT.md` | 최초 인계 기록. “ai-model은 콘솔 전용”은 이 기능 도입 전 상태 |
| `LLM_GATE.md`, 일반 Studio 배포/테스트 문서 | 실제 gate 계약과 이미지 기능 참고. 자동으로 폐기·수정하지 않음 |

## 9. 다음 세션 인계

먼저 이 문서와 두 저장소 `git status`를 읽는다. **주 작업 디렉터리는 `/Users/monoless/Works/bc250-llm-client`**이며, imagegen 자료는 `/Users/monoless/Works/bc250-imagegen`에 있다. imagegen의 미커밋·미추적 파일은 기존 사용자 작업을 포함하므로 덮어쓰거나 삭제하지 않는다.

운영 ai-model 원본은 사용자가 제공했으며 정적 분석을 완료했다. 다음 작업은 **M1의 남은 unit·실행 계정·gate/GPU 계약 확인과 M2 상세 설계 확정**이다. 기존 T2 DB 차단/유지보수 VM 반복 작업을 자동 재개하지 않는다. 제공받은 실제 원문 분석은 상세 설계를 따른다. recovery-candidate를 운영 원본으로 대신 판단하지 않는다. 현재 통합 문서 작성 완료가 기능 구현 시작 승인이나 운영 전환 실행을 뜻하지 않는다.

변경 기록: 2026-11-07 — 사용자 설명에 따라 Client 중심으로 목표 복원, imagegen 코드 이관 허용 반영, 과거 API 전용/전체 배포 계획과 현재 계획 구분. 문서 외 구현은 변경하지 않음.

추가 기록: 2026-11-07 — 운영 ai-model 원문 수신. 즉시 restart 실패 시 복구 건너뜀, 복원 후 health 미확인, 같은 모델 조기 성공, 잠금/작업 기록 부재를 분석했다. CLI/API 공용 작업자 상세 설계를 추가했으며 M1·M2는 부분 진행 상태로 갱신했다.

## 10. Imagegen 독립 개발·배포 재개와의 관계

2026-11-07 사용자 추가 목표: imagegen에서는 AI Studio 개발을 계속하고, 모델 전환 추가 기능은 Client에서 진행한다. imagegen의 모델 전환 개발/시험 흐름은 종료·인계하되 운영 Studio/gate 서비스를 종료하는 뜻은 아니다.

[Studio 개발·배포 재개 검토](../../../bc250-imagegen/docs/STUDIO_RESUME_REVIEW.md)에 S0~S5 마무리 작업을 기록했다. 링크는 두 저장소가 같은 부모 디렉터리에 있는 로컬 작업 공간 기준이다. 현재 Studio Actions는 명시적으로 비활성이고 서버 배포 검사 추가 기록도 있으므로 **Client로 작업을 넘겼다고 Studio 배포 원복이 완료된 것은 아니다**.

Client M1/M2 설계는 계속 진행할 수 있다. 실제 코드 이관은 imagegen의 미커밋/미추적 작업 보존과 Studio의 model_switch import 의존 분리를 확인한다. 기존 후보를 삭제하거나 예전 커밋 전체로 되돌리지 않는다. gate가 Studio 코드/venv를 공유하는 배포 경계도 확인해야 한다.

이번 검토에서는 문서만 갱신했다. workflow 재활성화·서버 스크립트 원복·서비스 재시작·VM 종료는 아직 실행하지 않았다. imagegen의 S0~S5는 Studio 독립 작업 재개를 위한 제한된 정리이며, 과거 T1~T6 전체 배포 연구를 재개하는 단계가 아니다.

### Imagegen 인계 결과 — 2026-11-07

Imagegen의 Studio 분리 커밋 `434167a`를 운영 서버에 수동 배포했다. GitHub Actions 실행 `42335504458` 성공, Studio·gate·Client·llama active, 기존 gate 파일 해시 일치를 확인했다. 배포 백업은 `/var/backups/bc250-ai-studio.HRzeOwTA`다. 실제 이미지 생성과 LLM 모델 전환은 실행하지 않았다.

Studio 전환 보호 계약은 imagegen의 `backend/app/services/switch_barrier.py`, 이미지/GPU 코드는 `studio_config.py`, `studio_files.py`를 참고한다. 운영 gate는 기존 버전이며 owned-hold gate 후보는 `gate_candidate.py`에 보존돼 있어 운영에 적용된 것으로 간주하지 않는다. 기존 후보와 분리 후 작업 트리는 `docs/handoff/imagegen-20261107`, `docs/handoff/imagegen-closeout-20261107`에 보존했다.

이후 M1/M2의 llama unit·시작 스크립트·실제 호출자·CLI/API 제어 계약 확인과 Client 구현을 진행한다. Imagegen의 과거 DB 차단 VM 연구는 다시 시작하지 않는다.
