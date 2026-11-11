# LLM Client 모델 전환 통합 계획

갱신일: 2026-11-10. 상태: **M3/M4 로컬 완료, M5~M7 부분 진행, 운영 전환 미검증**. 현재 단계와 실행 순서는 [실행 체크리스트](#7-실행-체크리스트와-완료-조건)를 따른다.

후속 설계: [운영 ai-model 분석 및 CLI/API 공용 실행부 설계](llm-model-switch-design.md). 사용자가 제공한 실제 원문에서 확인된 실패 경로와 변경 구조를 기록한다.

M2-02의 확정된 목표 조율 계약과 운영 환경 차이: [gate·Studio 공용 조율 계약](llm-model-switch-coordination.md). 이 계약은 후보 코드에 이미 모두 구현됐다는 뜻이 아니다.

M2-03의 모델별 설정·요청 예산·미검증 기능 처리 기준: [모델별 추론 정책](llm-model-switch-model-policy.md). 각 모델의 실제 추론 호환은 별도로 검증한다.

이 문서가 두 프로젝트의 **LLM 모델 전환 작업에 대한 단일 기준 문서**다. imagegen의 과거 API 전용 계획, T1~T6 배포 체크리스트, 조사·시험 기록보다 현재 목표와 작업 순서에서 우선한다. 과거 시험 결과와 실제 제약을 폐기하거나 미완료를 완료로 바꾸는 뜻은 아니다. 이미지 생성 기능 자체의 계획은 각 기존 문서를 따른다.

## 1. 확정된 사용자 요구와 프로젝트 경계

- `bc250-imagegen`: stable-diffusion 기반 AI Studio. 이미지 생성이 본래 역할이다.
- `bc250-llm-client`: llama-server의 **모델 선택 UI와 서비스 전환 API**를 제공할 주 프로젝트다.
- 운영자는 서버 콘솔에서 기존 `ai-model` 명령으로 모델을 전환해 왔다. 이 운영 방식을 UI/API로 연결한다.
- 필요하면 imagegen의 기존 전환 코드를 llm-client로 옮길 수 있다. 코드 이관 자체보다 실제 기능 완성이 목표다.
- 이번 갱신은 남은 작업과 완료 조건의 문서화다. 코드·운영 서비스·시험 환경은 변경하지 않는다.

최종 완료 기준: **LLM Client에서 설치 모델 B를 선택 → llama-server가 A에서 B로 실제 전환 → 현재 모델 표시 갱신 → 다음 대화가 B로 정상 응답**. 실패하면 이전 모델 복구 여부와 관리자 조치 필요 상태가 표시돼야 한다. UI 없는 관리 API 배포나 전환 비활성 응답 확인만으로 완료하지 않는다.

서버 전체 활성 LLM을 전환하는 기능이다. 대화마다 서로 다른 모델을 동시에 올리는 기능, 이미지 모델 선택, 모델 다운로드·삭제·설정 편집은 이번 범위가 아니다.

독립 Wiki API와 UI의 Client 통합은 사용자가 별도 후속 작업으로 정했다. 현재 Wiki의 llama 직접 호출은 이번 통합의 명시적 예외이며, Wiki 통합·gate 경유 전환을 M1~M8의 완료 조건으로 두지 않는다. 이 예외는 Wiki 요청이 전환 중 중단되거나 실패할 수 있고, Wiki의 직접 호출은 gate drain의 보호를 받지 못한다는 뜻이다. 운영 확인 시 이 한계를 기록한다. Client 내부의 Wiki 메모리 등 자체 LLM 요청 경로는 계속 모델 추종·요청 제어 범위에 포함한다.

## 2. 현황과 근거의 한계

2026-11-07 운영 원문과 읽기 전용 환경을 확인하고 Client에 M3/M4 로컬 후보를 구현했다. 실제 전환 기능은 아직 완료되지 않았다. 상세 증거와 미확인 사항은 [작업 기록](llm-model-switch-tasks.md)을 따른다.

| 영역 | 현재 상태 | 남은 판단 |
| --- | --- | --- |
| Client UI/API | 운영자 인증을 추가한 관리 API와 화면을 로컬 구현·가짜 작업자 흐름 검증 | 실제 worker·운영 UI 검증 |
| 공용 작업자 | root 경계·CLI/socket·작업 기록·복구·조율 어댑터 로컬 구현 | 관리자 복구 진입, Linux 권한과 실제 gate/Studio 통합 검증 |
| 운영 서비스 | 작업자 미설치, 현재 gate는 owned hold 미지원 | 운영 조율 계약, 설치와 복구 조건 확인 후 별도 적용 |
| 모델 요청 | snapshot/epoch 및 전환 충돌 처리 구현 | 모델별 예산·옵션과 실제 호출자 조율 |

기존 로컬 시험 기록은 219개 통과·1개 건너뜀과 프런트 lint/build 통과다. macOS에서 peer UID·xattr 및 운영 연동 일부는 대역을 사용했다. 이는 실제 GPU/운영 성공이나 최종 후보 회귀의 증거가 아니다. 이번 문서 갱신에서는 시험을 재실행하지 않았다.

## 3. 제어 구조

```text
브라우저의 LLM Client UI
  → Client 백엔드의 인증된 모델 관리 API (:8010, /llm/api/v1/...)
  → 제한된 로컬 모델 제어 작업자 ─→ llama-server.service
                         ↑
              기존 ai-model CLI shim

전환 실행부 ↔ LLM gate 및 공유 GPU 조율 ↔ 이미지 작업
```

운영 ai-model 원문 분석 후 기본안을 구체화했다. **기존 ai-model 명령 인터페이스를 유지하고 CLI/API가 하나의 공용 작업자를 호출하도록 내부를 정리한다.** 원본 Bash를 그대로 호출하는 단순 래퍼는 즉시 restart 실패·미검증 복구·동시 전환 문제를 해결하지 못하므로 채택하지 않는다. CLI 교체는 향후 검증된 배포 단계의 변경이며 현재 원본은 수정하지 않았다. **ai-model의 설정과 별개인 활성 모델 체계를 추가하지 않는다.** 같은 설정에 대한 경쟁하는 두 전환 구현도 피한다.

Client 웹 서버 전체를 root로 실행하지 않는다. 고정된 실행 파일과 검증된 모델 이름을 인자 배열로 전달하고, 임의 셸 명령·경로·서비스명을 API 입력으로 받지 않는다. 권한 실행부의 코드와 실행 환경은 일반 웹 서비스가 수정할 수 없도록 분리한다.

별도 8091 HTTP 서비스는 필수가 아니다. Client→Unix 소켓의 직접 연결을 우선 검토하며, 기존 bridge의 허용 UID가 `ai-studio`에 묶여 있으므로 Client의 실제 서비스 계정으로 재설계한다. UI가 socket이나 서버 관리 키를 직접 다루지 않는다.

gate와 이미지 GPU 조율은 실제 동시 실행 충돌을 막기 위해 필요하다. 반면 Studio DB 전체 이전, 모든 서비스의 일괄 배포, 유지보수 부팅, 통합 Actions 자동 배포를 기능 구현의 기본 선행 조건으로 두지 않는다. 필요한 gate/Studio 변경이 있다면 영향·호환성·설치·복구 범위를 별도 명시한다.

## 4. 재사용·이관 목록

아래 원본 경로는 모두 **bc250-imagegen 저장소 기준**이다. Client `backend/app/model_control/`에 핵심 후보를 선별 이관했으며 아래 목록은 이관 판단과 남은 점검의 기준이다.

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

Client에 로컬 구현된 서버 내부 관리 경로다. 브라우저에서는 기존 Caddy 규칙에 따라 `/llm` 접두사가 붙는다. 운영 배포나 실제 전환 검증을 뜻하지 않는다.

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

## 6. 남은 환경 확인 사항

| 확인 | 이유와 종료 조건 |
| --- | --- |
| 실제 ai-model 원본과 llama unit | `/usr/local/bin/ai-model` 링크와 `/opt/ai/scripts/ai-model`, list/status/switch 입력·출력·종료 코드·복구 동작 확인. 비밀값 제외 |
| Client 실제 계정·권한 | 저장소 service 템플릿 계정은 placeholder. socket 호출 주체와 제한 실행부 설치 방식 확정 |
| 실제 gate 기능·키·잠금 | owned hold는 opt-in 후보. 현재 호환 여부와 키 불일치 기록 재확인. 기존 pause를 전환 종료 시 임의 해제하지 않음 |
| 이미지 동시 실행 | status를 한 번 조회하는 것만으로 경쟁을 막지 못함. 모든 자동 GPU 경로가 같은 잠금/차단 계약을 따르는지 확인 |
| 직접 llama 호출 | Wiki의 8080 직접 호출은 사용자 지정 예외로 기록한다. Wiki 외 호출 경로는 확인·조율한다. gate drain이 Wiki 요청까지 보호한다고 주장하지 않는다 |
| Client 모델 고정 설정 | `LLM_MODEL_ID`가 고정이면 전환 후 오류 가능. 활성 모델 추종과 수동 콘솔 변경 감지 정책 확정 |
| 모델별 추론 설정 | 공통 `llm_context_tokens`, reasoning/JSON schema 옵션의 호환성 확인. 필요한 모델 메타데이터 또는 보수적 공통 설정 채택 |
| 실패와 중단 | ai-model timeout 뒤 실제 재시작이 진행 중일 수 있음. 결과 불명 시 추가 전환/자동 해제 금지, 복구 기록과 확인 절차 확보 |
| 수동 콘솔 전환 | CLI/API의 상태 일치 확인. 콘솔 호출이 잠금을 우회하는지 확인하고, 공용 조율 또는 동시 사용 금지 등 운영 정책 명시 |

이미 확인한 unit·계정·모델 예시는 [M1 기록](llm-model-switch-tasks.md#m1-운영-읽기-전용-조사)을 따르며, 남은 자료 확인도 읽기 전용으로 시작한다. 실패 복구·SELinux·파일 권한에 관한 과거 발견은 실제 채택한 경로에 관련되면 계속 해결해야 한다. 범위 축소를 이유로 무조건 성공 처리하거나 안전 검사를 제거하지 않는다.

## 7. 실행 체크리스트와 완료 조건

이 절이 현재 상태와 남은 작업의 기준이다. `완료`는 명시된 산출물과 검증을 충족한 경우에만 사용한다. `로컬 완료`는 운영 적용 가능이나 실제 GPU 검증을 뜻하지 않는다. 다음 표의 ID는 다른 단계의 선행 조건을 참조하는 안정적인 작업 식별자다. [상세 구현·과거 시험 근거](llm-model-switch-tasks.md)는 이 표의 상태를 보완한다.

2026-11-09 가정용 단일 관리자 결정: 사용자와 관리자는 동일하며 동일 origin의 모든 자산·응답·배포 권한을 별도 검증하거나 서비스 UID·Studio 배포 방식을 강제로 분리하지 않는다. 동일 관리자 계정의 SSH 접근은 worker 직접 호출 권한과 분리된 경계로 취급하지 않는다. 기존 앱·관리자 인증은 유지하고, GPU 공유 잠금·전환 journal·gate admission/drain·작업 상태와 실패 복구는 실제 기능의 필수 조건으로 유지한다. 이 결정은 원래의 엄격한 origin 신뢰 경계 요구보다 우선한다. 미검증 자산을 안전하다고 인증하는 것이 아니라 해당 검증을 이번 완료 게이트에서 제외한다.

2026-11-08 범위 재분할: 기존 M4-02/03의 완료 조건에서 아직 구현되지 않은 origin 검증과 불일치 복구 종결을 M4-04로 옮겼다. M4-02/03의 로컬 완료는 조정된 기본 범위에만 해당하며, 기존 조건 전체를 이미 충족했거나 운영 활성화가 가능하다는 뜻이 아니다.

| 단계 | 현재 상태 | 단계 완료 조건 |
| --- | --- | --- |
| M0 통합 계획 | 완료 | 목표·소유 저장소·과거 자료 우선순위가 정리됨 |
| M1 명령·환경 계약 | 완료 | M1-01~03에서 운영 구성과 알려진 범위 내 호출자 확인, 명시된 Wiki 예외 및 조사 한계 기록. 실제 조율 구현·검증은 M2/M4~M7 |
| M2 구현·공용 변경 설계 | 설계 완료 | M2-01~03의 로컬 계약·운영 조율·모델 정책 확정. 실제 조율 구현과 모델별 추론 검증은 M4~M6 |
| M3 Client 사용자 흐름 | 로컬 완료 | M3-01~02의 API 및 가짜 작업자 기반 브라우저 흐름 검증. 최종 후보·운영 연동은 M6/M8 |
| M4 공용 제어 실행부 | 로컬 완료 | M4-01~04의 구현·로컬 검증 완료. Linux·운영 경계의 최종 검증은 M6~M8 |
| M5 전환 후 Client 호환 | 부분 구현 | M5-01~03에서 모델 추종·요청 경합·다른 호출자 조율 |
| M6 최종 통합 검증 | 격리 후보 완료 | M6-01~02의 기능·Linux 경계 검증 완료. 실제 운영 설치·GPU 통합은 M7~M8 |
| M7 배포·복구 준비 | 조건부 판정 | M7-01 후보 준비, M7-02에서 비활성 설치 조건부 준비·기능 활성화 불가 판정. 실제 이관·모델 검증은 M8 |
| M8 운영 기능 확인·정리 | 미착수 | M8-01~03에서 실제 전환·후속 대화와 정리 완료 |

### 세부 작업 추적

| ID | 선행 | 작업·산출물 | 완료 조건 | 현재 상태·근거 / 남은 점 |
| --- | --- | --- | --- | --- |
| M0-01 | 없음 | 목표·소유권과 과거 계획 분류 | 단일 기준 문서로 정리 | 완료: 이 문서 §§1, 8 |
| M1-01 | M0-01 | 명령·unit·계정·모델 매핑 조사 | 원문, 설치 unit, 네 모델 설정과 알려진 CLI 호출·종료 코드 의존 확인 | 완료: [M1 조사](llm-model-switch-tasks.md#m1-추가-조사--2026-11-07); root 검색·벤치마크·래퍼 확인, 외부 SSH 자동화는 사용자가 없는 것으로 확인. 컨테이너 overlay의 끊어진 링크 등 조사 범위 한계 기록 |
| M1-02 | M1-01 | 보호 설정·잠금·journal·키 역할·계정·namespace 계약표 | 비밀을 노출하지 않고 실제 설정 및 미확인 사유 확인 | 완료: [운영 계약표](llm-model-switch-tasks.md#m1-02-운영-계약표--2026-11-07) 및 사용자 root 조회 기록. 키 불일치와 namespace 확인; lock/journal 부재로 파일 inode·권한 미검증 사유를 기록하고 M4/M6에 실제 파일 검증 인계 |
| M1-03 | M1-01 | Client 접속·고정 ID 및 직접 8080·GPU 호출자 목록 | Wiki 예외를 기록하고 알려진 범위 내 경로의 조율 또는 차단 방안 확인 | 완료: [호출 경로 조사](llm-model-switch-tasks.md#m1-03-호출-경로-조사--2026-11-07); Client·Studio·Wiki·벤치마크, root cron·timer 및 Podman 현황 확인. 알려지지 않은 외부 호출 등 조사 한계 기록; 실제 조율은 M2/M5/M6에서 구현·검증 |
| M2-01 | M0-01 | CLI/API/socket·작업 상태·인증 계약 | 공용 단일 작업자 및 실패 상태를 문서화 | 로컬 완료: [계약·시험 근거](llm-model-switch-tasks.md#m2-01-로컬-계약-확인--2026-11-07). 중복 키 문법·미설치 모델 오류 정합, 관련 시험 38개 통과. Linux·운영 적용 증거 아님 |
| M2-02 | M1-02, M1-03 | gate owned hold·기존 pause·stale 요청·Studio barrier/키·계정·배포 소유권 설계 | 공용 변경과 legacy 우회 방지 정책 확정; bool fallback 금지 | 설계 완료: [공용 조율 계약](llm-model-switch-coordination.md). 실제 gate·Studio·worker 변경과 Linux/운영 검증은 M4~M8; 운영 gate는 현재 미지원 |
| M2-03 | M1-01, M1-03 | 모델별 관리명·ID·context·reasoning·schema 호환표와 active-follow 정책 | 네 모델별 설정과 미검증 기능 처리·검증 기준 확정 | 설계 완료: [모델별 추론 정책](llm-model-switch-model-policy.md). 현재 모델의 ID/slot context 읽기 전용 확인, 나머지 모델 로드 및 모든 모델의 옵션 호환은 M6에서 실제 검증 |
| M3-01 | M2-01 | 운영자 인증 API·기본 비활성 transport·작업 조회 | 인증·중복·오류·unavailable API 검증 | 로컬 완료: [재검증 기록](llm-model-switch-tasks.md#m3-01-api-재검증--2026-11-07). 전체 226 통과·1 건너뜀 및 최종 변경 관련 13 통과; 운영 미배포 |
| M3-02 | M3-01, M4-01 | 선택·진행·terminal·새로고침·응답 유실 및 인증 UI | 브라우저에서 재제출 방지·작업 추적·불명 요청 식별/관리자 해소 안내 검증 | 로컬 완료: [UI 검증](llm-model-switch-tasks.md#m3-02-ui-검증--2026-11-08). Edge+가짜 HTTP 작업자 및 자동 시험 10개, lint/build 통과. 실제 worker·운영 통합은 M6/M8 |
| M4-01 | M2-01 | 영속 DB·coordinator·registry/systemd·worker·Unix·CLI 핵심 | 단일 작업·복구·연결 유실을 대역 및 로컬 socket에서 검증 | 로컬 완료: [핵심 재검증](llm-model-switch-tasks.md#m4-01-핵심-재검증--2026-11-08). journal 정리 실패 및 비활성 중 기존 키 조회 보완; 전체 229 통과·1 건너뜀. Linux·운영 별도 |
| M4-02 | M2-02, M4-01 | preflight·bootstrap·unit/socket 기본 경계 | protocol 검사·설치 템플릿 및 관련 로컬 대역 검증 | **범위 조정 후 로컬 완료**: [사전 검사·설치 경계](llm-model-switch-tasks.md#m4-02-사전-검사설치-경계-진행--2026-11-08). 과거 origin 검증과 전용 계정 강제는 사용자 결정으로 이번 완료 조건에서 제외 |
| M4-03 | M2-02, M4-01 | root 복구 진입·상관관계·안전 차단의 기본 구현 | root 조사·복구 후보, hold ID 영속 연결, 일치·빈 journal 대역 시험 및 불일치·손상 시 보존·차단 | **범위 조정 후 로컬 완료**: [복구·조사](llm-model-switch-tasks.md#m4-03-root-복구조사-진행). 기존 orphan·불일치 후속 처리·종결 구현은 M4-04로 이관; 실제 gate·Studio 구현과 통합은 M5/M6 |
| M4-04 | M4-02, M4-03 | 가정용 환경의 공용 전환·복구 경로 완성 | 기존 관리자 계정과 실제 서비스 구성을 지원하고, 공유 GPU 잠금·journal·gate/Studio 인증·상태 확인 및 안전한 복구 진입을 로컬 검증. 소유권 불명 hold는 자동 해제하지 않음. [상세 추적](llm-model-switch-tasks.md#m4-04-이관된-미완료-작업) 참조 | 로컬 완료: 일부 서비스 신원·모델·Studio 경로 검사는 대역. 실제 gate·Studio 통합은 M5-02/03, Linux 최종 후보는 M6, 배포·복구 절차는 M7 |
| M5-01 | M2-03, M4-01 | 공통 모델 해석과 active-follow, 모델별 옵션 | 채팅·요약·제목·이미지 의도/프롬프트가 실제 모델에 맞는 예산을 사용하고 호환 회귀 통과 | 부분 구현: 전환 활성 상태의 고정 LLM_MODEL_ID를 설정 검증에서 거절. M5-01-01 로컬 완료: gate snapshot에 실제 slot/로드 context를 epoch와 함께 연결, 요청별 상한과 매 시도 최종 payload 추정 검사. M5-01-02 로컬 완료: 최종 payload의 실제 tokenizer 계산·채팅/요약 턴 재선정. M5-01-03 로컬 완료: conf·epoch·로드 ID 결합 및 기능 검증 기록/미검증 차단. 네 모델별 실제 검증과 기록 설치, 운영 통합은 M6~M8에 남음 |
| M5-02 | M2-02, M3-01 | Client admission과 gate의 stale 요청 처리 | 신규 요청 409·초안 보존, 모델 snapshot epoch 검증, 기존 요청 drain, 백그라운드 보류 및 동시 진입 경합 검증. 복구용 배타 예약과 독립 hold·epoch 조건부 자기 hold 해제의 원자적 영속 처리 | 로컬 완료: [gate_candidate](../../gate_candidate/README.md)의 protocol 3 admission·hold·reservation, stale epoch 거절과 Client 충돌·초안 흐름 검증. worker 큐 경합을 409로 처리하고 background는 안전한 준비 단계 또는 gate가 입장 전에 거절했다고 확인한 경우만 대기 후 재시도. 실제 gate 이전·Studio 통합 및 운영 확인은 M6~M8에 남음 |
| M5-03 | M1-03, M2-02 | 범위 내 다른 LLM/GPU 호출자 및 gate/Studio 최소 연동 | 독립 Wiki를 제외한 자동 경로가 공용 잠금·journal·owned hold를 따르거나 명확히 차단됨. Studio 복구 전용 인증 조회와 journal·GPU·독립 복구 상태 계약 구현 | 로컬 후보 완료: 운영 Studio 소스 복사본의 fake 엔진·gate/worker 계약과 다른 hold 보존 확인. [벤치마크 후보](../../deploy/benchmark-candidate/README.md)는 gate epoch 및 공용 CLI를 사용하고 불명 결과에서 중단. Studio 공유 설정, gate/worker와 벤치마크 후보의 실제 적용·GPU 검증은 M6~M8에 남음; Wiki 통합은 후속 예외 |
| M6-01 | M3-02, M4-04, M5-01, M5-02, M5-03 | 같은 최종 후보의 UI/API/CLI→worker 통합 회귀 | 중복·동시성·hold·cleanup crash window·불명 결과·재시작과 기존 회귀 통과 | 격리 후보 완료: [최종 후보 회귀](llm-model-switch-tasks.md#m6-01-최종-후보-통합-회귀--2026-11-10), 실제 Edge UI→Client→socket worker→protocol 3 gate의 A→B·후속 대화와 cleanup 다섯 지점 SIGKILL 복구. 로컬 backend 812 통과·5 건너뜀, frontend lint·14 tests·build 및 Linux 격리 70 통과. llama·Studio preflight는 대역; 실제 root runtime·GPU·서비스 확인은 M6-02/M8에 남음 |
| M6-02 | M4-04, M7-01 | 최소 Linux 격리 시험 | 실제 SO_PEERCRED·socket activation·root bootstrap·ACL/SELinux/xattr·CAP_SYS_PTRACE/proc namespace·재시작·원자 교체 검증 | 격리 후보 완료: [Linux 경계 시험](llm-model-switch-tasks.md#m6-02-linux-경계-시험--2026-11-10). 실제 root transient service·socket activation/재시작·Studio namespace inode·enforcing SELinux 원자 교체 확인. 후보 경로와 credential은 임시 대역, 실제 운영 설치·GPU는 M7~M8. 과거 유지보수 VM/DB 차단 시험은 재개하지 않음 |
| M7-01 | M2-02, M4-04 | 패키지·credential·state 초기화, CLI 백업/교체, 공용 변경·비활성 설치·복구/rollback 절차 | 미해결 상태와 기존 pause 보존, 중복 controller 방지, 실제 Client·gate·Studio 호환 구성의 설치·복구 runbook | 후보 준비 완료: [서버별 절차·점검 도구](llm-model-switch-install-recovery.md#m7-01-대상-서버-실행-순서와-점검-도구--2026-11-10), 상태 이전·credential·백업·복원·startup 해제 절차 및 격리 시험. 실제 설치·복원 명령의 전체 조합과 운영 적용 직전 상태는 M7-02/M8에서 확인. root 자산 출처 인증이나 기존 Studio 배포 방식 유지는 필수 아님 |
| M7-02 | M1-02, M1-03, M6-01, M6-02, M7-01 | 배포 준비 판정표 | 후보 commit/hash·시험 근거·구체 변경/복구 목록이 일치하고 미확인 해소·fail-closed 확인 | 부분 완료: [11/11 준비 판정](llm-model-switch-readiness.md). 설치 전 준비 판정은 완료했고 비활성 설치는 조건부 진행 가능. 실제 공용 계약·모델별 기능 기록은 설치 후 M8에서 확인해야 하므로 전체 미확인 해소 및 Client 활성화 판정은 미완료 |
| M8-01 | M7-02의 비활성 설치 준비 판정 | 허용된 운영 단계에서 비활성 설치→준비 확인→단일 CLI/API 경로 활성화 | 원본 CLI 백업, Wiki를 제외한 제어 우회 제거 및 실제 gate/Studio owned 계약 확인. Wiki 예외와 영향 기록 | 진행: protocol 3 공용 연결·실제 preflight·startup 해제, 관리자 worker 활성화, 네 모델 실제 기능 기록·slot context 확인 및 원래 Qwen3.6 복귀 완료. 활성 설정 파일의 SELinux 라벨 차이로 인한 첫 전환 차단도 해결했다. Client UI 활성화·실제 API 확인은 미완료 |
| M8-02 | M8-01 | 실제 UI A→B 전환과 B 후속 대화 | 현재 모델 갱신, 같은 모델·CLI/API 일치·이미지/채팅 경합 및 운영 회귀 증거 확보 | 미착수: 사용자 기능의 최종 완료 게이트 |
| M8-03 | M8-02 | 최종 운영 문서·복구 증거·중복 entrypoint 정리 | 잔여 위험과 복구 근거 기록, 검증 후 범위 내 정리 | 미착수: 보존물은 검증 전에 삭제하지 않음 |

선행 조건은 위 표를 따른다. 특히 M7-01의 검토용 절차는 M6-02의 Linux 시험 준비에 필요하므로 M6과 M7은 단순 일렬이 아니다. 파괴적 실패 주입은 격리된 M6 증거와 운영 readiness를 구분하며 운영에서 무차별 실행하지 않는다. 각 작업 완료 시 증거·날짜·남은 점을 갱신한다.

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

운영 ai-model 원본·unit·실행 계정은 확인했다. 다음 작업은 **§7 세부 태스크의 M1 미확인 환경과 M3/M4 미완료 검증, M5 구현**을 선행관계에 따라 진행하는 것이다. 기존 T2 DB 차단/유지보수 VM 반복 작업을 자동 재개하지 않는다. 제공받은 실제 원문 분석은 상세 설계를 따른다. recovery-candidate를 운영 원본으로 대신 판단하지 않는다. 현재 문서·로컬 구현은 운영 전환 실행을 뜻하지 않는다.

변경 기록: 2026-11-07 — 사용자 설명에 따라 Client 중심으로 목표 복원, imagegen 코드 이관 허용 반영, 과거 API 전용/전체 배포 계획과 현재 계획 구분. 문서 외 구현은 변경하지 않음.

추가 기록: 2026-11-07 — 운영 ai-model 원문 수신. 즉시 restart 실패 시 복구 건너뜀, 복원 후 health 미확인, 같은 모델 조기 성공, 잠금/작업 기록 부재를 분석했다. CLI/API 공용 작업자 상세 설계를 추가했으며 M1·M2는 부분 진행 상태로 갱신했다.

## 10. Imagegen 독립 개발·배포 재개와의 관계

이 절의 초기 검토는 당시 시점 기록이며 현재 단계 상태는 §7과 아래 인계 결과를 따른다.

2026-11-07 사용자 추가 목표: imagegen에서는 AI Studio 개발을 계속하고, 모델 전환 추가 기능은 Client에서 진행한다. imagegen의 모델 전환 개발/시험 흐름은 종료·인계하되 운영 Studio/gate 서비스를 종료하는 뜻은 아니다.

[Studio 개발·배포 재개 검토](../../../bc250-imagegen/docs/STUDIO_RESUME_REVIEW.md)에 S0~S5 마무리 작업을 기록했다. 링크는 두 저장소가 같은 부모 디렉터리에 있는 로컬 작업 공간 기준이다. 현재 Studio Actions는 명시적으로 비활성이고 서버 배포 검사 추가 기록도 있으므로 **Client로 작업을 넘겼다고 Studio 배포 원복이 완료된 것은 아니다**.

Client M1/M2 설계는 계속 진행할 수 있다. 실제 코드 이관은 imagegen의 미커밋/미추적 작업 보존과 Studio의 model_switch import 의존 분리를 확인한다. 기존 후보를 삭제하거나 예전 커밋 전체로 되돌리지 않는다. gate가 Studio 코드/venv를 공유하는 배포 경계도 확인해야 한다.

이번 검토에서는 문서만 갱신했다. workflow 재활성화·서버 스크립트 원복·서비스 재시작·VM 종료는 아직 실행하지 않았다. imagegen의 S0~S5는 Studio 독립 작업 재개를 위한 제한된 정리이며, 과거 T1~T6 전체 배포 연구를 재개하는 단계가 아니다.

### Imagegen 인계 결과 — 2026-11-07

Imagegen의 Studio 분리 커밋 `434167a`를 운영 서버에 수동 배포했다. GitHub Actions 실행 `42335504458` 성공, Studio·gate·Client·llama active, 기존 gate 파일 해시 일치를 확인했다. 배포 백업은 `/var/backups/bc250-ai-studio.HRzeOwTA`다. 실제 이미지 생성과 LLM 모델 전환은 실행하지 않았다.

Studio 전환 보호 계약은 imagegen의 `backend/app/services/switch_barrier.py`, 이미지/GPU 코드는 `studio_config.py`, `studio_files.py`를 참고한다. 운영 gate는 기존 버전이며 owned-hold gate 후보는 `gate_candidate.py`에 보존돼 있어 운영에 적용된 것으로 간주하지 않는다. 기존 후보와 분리 후 작업 트리는 `docs/handoff/imagegen-20261107`, `docs/handoff/imagegen-closeout-20261107`에 보존했다.

이후 M1/M2의 llama unit·시작 스크립트·실제 호출자·CLI/API 제어 계약 확인과 Client 구현을 진행한다. Imagegen의 과거 DB 차단 VM 연구는 다시 시작하지 않는다.

### 2026-11-07 M1/M2 및 M3 로컬 착수

운영 읽기 전용 조사와 남은 미확인 항목, CLI/API 작업자 계약 및 M4~M8 차단 조건은 [구현 작업과 차단 조건](llm-model-switch-tasks.md)에 기록했다. M3의 기본 비활성 API/UI와 fake 작업자 시험을 로컬에 추가했다. 실제 작업자·owned hold·GPU 조율·운영 전환은 미완료이며 이 변경은 기능 완료나 배포 승인이 아니다.

### 2026-11-07 M4 로컬 구현

공용 worker·CLI 및 root 실행 경계의 로컬 구현과 대역 시험 근거는 [M4 로컬 구현 기록](llm-model-switch-tasks.md#m4-로컬-구현-기록--2026-11-07)을 따른다. 실제 gate/Studio 호환, Linux 권한 및 GPU 통합은 미검증이며 운영 서비스에는 설치하거나 전환하지 않았다. M5~M8은 남아 있다.
