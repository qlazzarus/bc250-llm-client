# llama-server 모델 전환 상세 설계

기준: 2026-11-07 사용자가 제공한 운영 `/usr/local/bin/ai-model` 원문과 로컬 Client 코드. 상위 기준은 [통합 계획](llm-model-switch-plan.md)이다. **설계 기준이다. M3/M4 핵심은 로컬에 구현했으며 설치·운영 명령 교체는 하지 않았다. 현재 상태와 남은 태스크는 [통합 계획 §7](llm-model-switch-plan.md#7-실행-체크리스트와-완료-조건)을 따른다.**

M2-02에서 확정한 gate·Studio·credential·요청 epoch 및 전환/복구 계약은 [공용 조율 계약](llm-model-switch-coordination.md)을 따른다. 기존 후보의 구현 상태와 목표 계약은 구별한다.

## 1. 운영 원본에서 확인된 계약

| 항목 | 확인 내용 |
| --- | --- |
| 명령 | Bash `set -euo pipefail`, `list`, `status`, `switch MODEL` |
| 설정 | `/opt/ai/config/active-model.conf`의 `ACTIVE_MODEL`, `models/*.conf`의 `MODEL_PATH` |
| 서비스 | `llama-server.service` |
| 전환 | 모델 이름·설정 파일·GGUF 비어 있지 않음 검사 → 활성 설정 직접 덮어쓰기 → systemctl restart → health 확인 |
| 준비 검사 | `http://127.0.0.1:8080/health`를 최대 120회 요청, 요청당 최대 2초 및 반복당 sleep 2초. 단순 총 240초 제한이 아니며 systemctl 시간도 별도 |
| 정상 종료 | 이미 선택된 이름이거나 health 본문에 정확한 문자열 `"status":"ok"`가 있으면 0 |
| 실패 복구 | health 대기 실패 시 이전 이름을 다시 쓰고 restart 후 1. 복구 health는 확인하지 않음 |

이 원문으로 **명령 로직 확인은 완료**했다. 이후 설치 링크·소유권·unit·서비스 계정을 읽기 전용으로 확인했다. gate/Studio 보호 설정 등 미확인 항목은 [M1 조사](llm-model-switch-tasks.md#m1-운영-읽기-전용-조사)를 따른다. 사용자 제공 원문은 운영 전환 실행 검증이 아니다.

## 2. 그대로 HTTP로 감싸면 생기는 문제

1. `systemctl restart`가 실패하면 `set -e`로 즉시 종료해 이전 모델 복구 분기를 건너뛴다. 활성 설정은 이미 새 이름일 수 있다.
2. health timeout 후 이전 모델을 restart해도 준비 여부와 실제 로드 모델 ID를 확인하지 않는다. 종료 코드 1만으로 검증 실패·전환 실패·복구 성공·복구 실패를 구별할 수 없다.
3. 같은 이름이면 서비스가 내려갔거나 다른 모델이 실제 로드돼 있어도 0이다. API의 성공 판정으로 사용할 수 없다.
4. health는 JSON 파싱이 아닌 정확한 부분 문자열 검색이다. 공백 차이로 정상 응답을 실패로 오인할 수 있고, 정상 health만으로 목표 모델이 로드됐다고 증명하지 못한다.
5. GPU 잠금, 진행 중 요청 drain, 중복 전환 방지, 재시작 복구 기록이 없다. 동시에 실행하면 활성 설정과 실제 모델이 어긋날 수 있다.
6. 활성 설정을 직접 truncate/write하므로 중간 종료 시 완전한 파일을 보장하지 않는다. 이전 모델 이름·설정의 유효성도 전환 전에 검사하지 않는다.
7. `status`는 사람이 읽는 systemctl 출력을 섞고 파이프 종료 상태의 영향도 받는다. 구조화된 상태 API로 파싱하지 않는다. `list`는 glob 무매칭과 파일 종류 검사를 처리하지 않는다.

따라서 원본 명령을 subprocess로 호출하고 반환 코드만 전달하는 API는 채택하지 않는다. 그렇다고 기존 실패 경로를 감싸기 위해 별도의 중복 복구 로직 두 벌을 만들지도 않는다.

## 3. 채택할 구조: CLI와 API의 단일 제어 실행부

```text
Client UI → Client FastAPI → Unix socket ─┐
                                        ├→ model worker → 기존 활성 설정 + llama-server
ai-model list/status/switch → CLI shim ──┘
```

**기존 `ai-model list/status/switch MODEL` 사용법과 설정 경로를 유지하면서 내부를 공용 작업자 호출로 통일하는 설계**를 채택한다. 사용자가 익숙한 ai-model은 계속 제공한다. 현재 Bash 원본은 배포 시 백업하고, 작업자가 다시 CLI를 호출하는 순환 구조는 만들지 않는다. CLI 교체는 구현·검증·롤백 절차가 준비된 운영 적용 단계의 변경이다.

작업자는 기존 imagegen의 전환·상태·복구 코드를 선별 이관해 직접 설정 갱신과 고정 서비스 제어를 수행한다. 실제 unit과 설정 문법 확인 후 어댑터를 확정한다. 원본을 영구적으로 손대지 않아야 하는 제약이 추가되면 이 구조를 재검토하며, 원본 Bash와 새 작업자가 동시에 독립 전환하는 구성을 정상 운영안으로 삼지 않는다.

CLI `switch`는 작업 접수 후 완료까지 기다리고 진행을 출력한다. 터미널 종료는 작업 취소가 아니다. CLI 결과는 0=목표 모델 검증 완료, 1=요청/전환 실패(복구 결과 표시), 2=복구 필요로 구분하는 제안이다. API는 숫자 종료 코드 대신 구조화된 상태를 사용한다. 자동화에서 기존 종료 코드 1에 의존하는지 M1에서 확인한다.

초기 배포에 별도 8091 HTTP 서비스는 추가하지 않는다. Client API가 소켓을 호출한다. 후보 서비스 이름 `bc250-model-worker.service/socket`, 상태 경로 `/var/lib/bc250-model-control`, socket 경로 `/run/bc250-model-control/control.sock`는 재사용하되 실제 설치 유무·충돌을 확인한다. root 실행 코드는 Client가 쓰기 가능한 소스/venv에서 실행하지 않고 별도의 root 소유 설치본을 사용한다.

socket에서는 peer UID를 검사한다. Client 서비스 UID와 운영 CLI의 root 호출을 구분하고, 일반 로컬 UID의 변조·직접 요청을 허용하지 않는다. CLI의 필요한 sudo는 해당 CLI 호출에 한정하며 웹 서버에는 일반 sudo를 부여하지 않는다. Client unit의 `NoNewPrivileges=true`를 유지할 수 있다.

## 4. 실행·복구 순서

1. **접수:** Client 인증 및 관리자 권한 확인, 모델 이름/설치 여부 검사. 작업자에서 idempotency와 미완료 작업 충돌을 원자적으로 검사하고 queued 기록을 영속화한 뒤 202 반환. 실행부 미연결은 명확히 unavailable/disabled로 응답한다.
2. **대기:** 단일 작업자가 공유 GPU 잠금을 기다린다. Studio와 잠금 파일 경로·inode·소유 규칙이 같아야 한다. 대기 중 UI는 새 LLM 요청을 제한하며 이미지 작업 완료를 기다린다.
3. **전환 준비:** 잠금 안에서 현재 설정과 실제 모델을 재조회해 이전 모델을 결정한다. preflight 성공 후 영속 journal에 작업 ID·이전/목표 모델·hold ID·변경 시작 여부를 기록하고 자기 gate hold를 획득한다. 다른 보류를 임의 해제하지 않는다.
4. **배출:** 범위 내 새 추론의 유입을 차단한 뒤 기존 요청과 llama 슬롯이 비워질 때까지 기다린다. 단순 Client의 busy 변수는 다른 프로세스의 요청을 막지 못하므로 gate와 범위 내 실제 호출자 조율이 필요하다. 독립 Wiki는 사용자 지정 예외로 gate를 우회하며 전환 중 요청이 중단·실패할 수 있다. 시간 초과로 변경하지 않았다면 기존 모델 상태를 검증하고 자기 hold만 정리한다.
5. **변경:** 변경 의도를 durable 기록한 후 같은 디렉터리 임시 파일+원자 교체로 ACTIVE_MODEL을 갱신하고 고정 서비스 restart. 소유권·권한·ACL·SELinux 보호 속성 보존을 확인한다. root가 source하는 모델 설정을 웹 요청으로 작성하지 않는다.
6. **검증:** 서비스 상태, JSON health, `/v1/models`의 목표 GGUF ID를 확인한다. 설정 이름 일치만으로 성공하지 않는다. 모델 별칭은 원본 launch 설정에서 확인하고 명시적으로 매핑한다.
7. **정상 정리:** 검증 완료 사실을 기록하고 자기 gate hold와 journal을 복구 가능한 순서로 정리한 뒤 작업을 성공으로 마무리하고 GPU 잠금을 해제한다. 정리 중 중단도 startup reconciliation 대상으로 한다. 기존 관리자 pause는 유지한다.
8. **실패:** restart 실패와 health/ID 검증 실패 모두 명시적으로 잡는다. systemd 작업의 종료를 확인한 후 이전 설정을 복원·재시작하고 이전 모델의 health와 ID까지 검증한다. 성공은 `failed_rolled_back`, 확인 불가는 `recovery_required`다.

프로세스 종료 시 flock은 풀린다. 따라서 미해결 journal이 있는 동안 Studio와 새 작업자가 GPU 작업을 시작하지 않는 공통 복구 차단 계약이 필요하다. 기존 후보를 적용할 때 이 연동을 생략하지 않는다. worker 시작 시 작업 DB/journal/hold/systemd 상태를 대조해 미완료를 재조정한다. 서비스 timeout이나 DB/journal 불일치에 대해 무조건 다시 switch하거나 보류를 해제하지 않는다.

초기 자동 복구는 상태가 일치하고 이전 모델과 systemd 종료가 확인된 경우에만 수행한다. 나머지는 root 전용 복구 명령으로 조사·복구한다. 브라우저에서 임의 설정 변경이나 “강제 보류 해제”를 제공하지 않는다.

## 5. Client API/UI와 요청 경합

통합 계획의 `/api/v1/llm/models`, `/llm/switches`, `/llm/switches/current`, `/llm/switches/{id}` 계약을 사용한다. 아래 경로의 `/llm`은 `/api/v1` 뒤의 모델 관리 하위 경로이며 Caddy의 브라우저 접두사 `/llm`과 별개다.

목록 응답은 `models[{name,current}]`, `configured_model`, `loaded_model`(관리 이름 또는 null), `ready`, `switch_enabled`, `active_operation_id`를 제공하는 초안이다. 원본 GGUF 경로·systemctl 출력·비밀값은 UI에 노출할 필요가 없다. 유효한 설치 목록과 실제 로드 모델 확인은 별개로 처리한다.

작업 응답은 기존 `id`, `model`, `state`, `error_code`, `created_at`, `updated_at`에 `previous_model`과 검증된 `current_model`을 추가한다. 아직 확인되지 않은 값은 null이다. GET current는 미완료/복구 필요 작업 또는 null을 반환한다. 관리 UI는 진행 중 1~2초 간격으로 조회하고 네트워크 유실 시 작업을 새로 제출하지 않는다.

전환 중 새 채팅 제출은 `409 model_switch_in_progress`로 거절하고 입력 초안을 보존하는 단순한 첫 버전을 제안한다. 이미 실행 중인 요청은 drain 대상으로 완료시킨다. 아직 추론을 시작하지 않은 제목·요약 등 백그라운드 작업은 보류하고 전환 후 모델을 다시 조회한다. 기존 Client 요청 생성/추론 진입과 전환 admission을 같은 로컬 동기화 경계로 조율하고, 마지막 경합은 gate에서 막는다.

기존 gate가 대기 요청의 오래된 model ID를 전환 후 그대로 전달하는지 확인한다. 그런 동작이면 새 추론은 다시 모델을 확인해 제출하도록 gate 계약을 보완하거나 해당 대기 요청을 명확히 거절해야 한다. 응답 일부가 전달된 채팅은 자동 재제출하지 않는다. 단순 API 앞단 검사만으로 경합 처리가 끝났다고 보지 않는다.

`LLM_MODEL_ID` 고정값은 전환 활성화 전에 해제/활성 모델 추종으로 정리한다. 현재 모델 표시는 다시 조회한다. 채팅·요약·제목·이미지 의도/프롬프트가 사용하는 모델 해석 경로를 공통화하고 모델별 컨텍스트 및 옵션 호환성을 적용한다. 이미지 모델 선택은 변경하지 않는다.

관리자 인증은 첫 버전에서 기존 앱 인증에 추가한 **별도 운영자 자격 증명 검사**를 제안한다. 브라우저 입력은 메모리에만 보관하며 서버 실행부/gate 키를 공유하지 않는다. local 모드는 관리자 인증으로 취급하지 않는다. 자격 증명을 전송할 운영 접속은 HTTPS 또는 SSH 터널로 제한한다. 다중 사용자 계정 시스템이나 장기 세션 DB는 이번 범위에 추가하지 않는다. 정확한 배포 접속 방식은 M1에서 확인한다.

## 6. 이관 모듈과 작업 구분

Client의 `backend/app/model_control/`에 `schemas.py`, `client.py`(소켓), `operations.py`, `coordinator.py`, `registry.py`, `systemd.py`, `admission.py`, `runtime.py`, `cli.py`를 두는 안이다. 권한 실행부를 별도 root 소유 설치본으로 패키징한다. API 라우터와 UI 모델 패널은 기존 Client 앱에 연결한다. 패키지 이름/세부 분리는 구현 시 조정 가능하다.

imagegen의 `model_switch`, `switch_worker`, `switch_operations`, `model_control_bridge`와 관련 테스트를 우선 재사용한다. `coordinated_studio_is_ready` 등 기존 전체 배포 검사에 대한 의존은 실제 필요한 GPU 잠금/복구 계약 확인으로 분리한다. 검사 삭제로 대신하지 않는다. Studio DB fence, 유지보수 부팅, Caddy 전체 이관, Actions 인증 도구는 이관 목록에 넣지 않는다.

## 7. 실제 원본에 대응하는 최소 검증

| 원본의 취약한 동작 | 새 실행부에서 확인할 결과 |
| --- | --- |
| 즉시 restart 실패 | 이전 설정 복원과 이전 모델 검증 후 failed_rolled_back, 복구 실패는 recovery_required |
| health 문자열/다른 모델 | 공백 있는 JSON 정상 해석, 다른 모델 ID면 성공 거절 |
| 같은 이름 조기 성공 | health/ID 불일치이면 재시작 없는 성공 처리 금지. 첫 버전은 복구 필요 상태로 안내 |
| timeout/작업자 종료 | systemd 작업 불명일 때 겹치는 restart 금지, journal/hold 유지 및 재시작 복구 |
| 동시 CLI/API | 하나의 작업만 전환, 반복 키는 같은 작업, 다른 요청은 충돌 |
| UI 종료/새로고침 | 작업이 계속되며 current/id 조회로 복원 |
| 이미지/채팅 동시 진입 | 공유 잠금·gate로 상호 배제, 기존 pause와 복구 차단 유지 |

먼저 가짜 systemd/health/잠금/소켓으로 UI와 API 및 실패 처리를 검증한다. 이후 필요한 systemd·권한·SELinux 경계만 격리 환경에서 확인하고 마지막에 실제 GPU 전환을 확인한다. 이 문서 작성 중에는 테스트나 서비스 재시작을 실행하지 않았다.

## 8. 남은 확인과 다음 작업

현재 M1 환경 조사와 M2 설계는 부분 완료이며 M3/M4 핵심은 로컬 구현됐다. 실제 설정·호출자, root 전용 복구 진입, gate/Studio 통합 및 Linux 경계는 남아 있다. 구체적인 순서와 완료 조건은 [통합 계획 §7](llm-model-switch-plan.md#7-실행-체크리스트와-완료-조건)을 따른다. 운영 적용 가능 판정은 해당 선행 조건과 검증이 끝나기 전까지 내리지 않는다.
