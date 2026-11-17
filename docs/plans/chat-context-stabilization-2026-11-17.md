# 채팅 컨텍스트 보존 안정화 구현 보고

작성일: 2026-11-17 (Asia/Seoul)

## 1. 작업 전 조사와 기준선

이 보고서는 `chat-context-review-2026-11-17.md`에 이은 구현 결과다. 이전 문서는 변경 전 현황으로 보존한다. 운영 서버와 실제 대화 DB는 수정하지 않았다.

작업 전에 다음 기준선을 다시 실행했다.

```sh
.venv/bin/python -m pytest tests/test_prompting.py tests/test_model_context.py tests/test_summary_model_budget.py -q
```

결과: **38 passed in 0.98s**. 그 뒤 실패 재현 테스트를 추가해, 22개 미요약 메시지 중 20개만 조회되는 문제와 실측 예산 검사에서 payload를 직접 줄이는 문제를 각각 확인했다(2 failed).

| 조사 항목 | 변경 전 실제 코드 근거와 동작 |
|---|---|
| 개수 제한 위치 | `main.begin_generation()`의 완료 이력 SELECT에 `ORDER BY created_at DESC, id DESC LIMIT MAX_HISTORY_MESSAGES` 적용. 로컬 `.env` 값은 20 |
| 요약 이후 조회 | `summary_up_to_message_id`의 `created_at,id`보다 뒤인 메시지를 조회. generation_requests가 없거나 completed인 메시지만 포함 |
| 요약 트리거와 채팅 예산 | 답변 저장 후 `queue_roleplay_summary()` 호출. `summary_candidate()`는 기본 5,000 추정 토큰과 최근 6턴 보존 조건 사용. 질문·설정·Wiki·채팅 출력 예약과 분리 |
| 계수 방식 | model control 활성: 모델 epoch와 실제 input_tokens API. 비활성: UTF-8 바이트/2 추정. Wiki 경로는 활성 상태에서도 먼저 추정 기반 이력 제거 가능 |
| 요약 성공 | 비어 있지 않은 content만 확인. finish_reason 검사 없음 |
| 트랜잭션 | 최종 요약 본문·커서·작업 완료 UPDATE는 같은 SQLite connection transaction. 다만 선행 읽기에는 명시적 write lock이 없었음 |
| 충돌 | 대화당 pending 생성/active 요약 유일 인덱스. 수동 요약 수정 시 작업 failed. 결과 저장 시 본문·커서 비교. ABA 수정 검출용 버전 없음. 모델 epoch 불일치는 background 재준비 |
| 재시작 | lifespan에 summary_jobs 복구 없음. 이미지·Wiki 작업 복구와 별개 |
| 프로세스 | 배포 service/bootstrap은 별도 workers 인자 없는 단일 Uvicorn. asyncio.Semaphore와 task set은 프로세스 내부. DB 유일 인덱스는 여러 프로세스에 공유되지만 요약 실행 소유권은 없었음 |

앞선 현황 문서와 핵심 구조는 일치했다. 기존 재사용 함수와 SQLite를 유지했으며 새 외부 서비스나 벡터 DB는 추가하지 않았다.

## 2. 변경 전후

| 상황 | 변경 전 | 변경 후 |
|---|---|---|
| 개수 제한 초과 | 조회 때 미요약 원문 제거 | 전체 완료 이력을 조회하고 개수 제한을 압축 트리거로 사용 |
| 토큰 예산 초과 | 오래된 턴을 제거하고 답변 가능 | 요청 전 압축. 전체 이력을 보존할 수 없으면 명시적 실패 |
| 압축 실패지만 전체 이력이 들어감 | 잘린 일부 이력으로 답할 수 있음 | 개수 기준을 초과하더라도 전체 이력을 그대로 전달 |
| 6턴 이하의 긴 대화 | 요약 후보가 없음 | 압박 시 최근 유지 목표를 줄이고 가장 오래된 완료 턴부터 압축 |
| 단일 턴이 요약 예산 초과 | 처리 진전이 없을 수 있음 | 해당 턴을 건너뛰지 않고 제한된 실패. 분할은 2차 범위 |
| 요약 잘림/잘못된 구조 | 내용이 있으면 커서 이동 가능 | stop·문자열·비어 있지 않음·길이·메시지 형태 검사. 실패 시 커서 유지 |
| 재시작 | active 작업이 새 요약을 차단할 수 있음 | OS 실행 잠금을 얻을 수 있는 pending/running 작업만 복구 |
| 수동 수정 | 본문 비교 중심 | summary_revision 증가. 자동 작업 기준 버전 검사와 편집 API 낙관적 잠금 |
| Wiki | 추정 기반으로 채팅 이력 제거 가능 | 필수 payload에 자료가 너무 많으면 Wiki 자료만 줄이고, 전체 채팅 이력은 별도 strict 검증 |

요약 출력의 정상 종료 검사는 의미적 정확성을 보장하지 않는다. 중요한 사실을 잘 보존하는지는 실모델 품질 평가가 필요하다.

## 3. 수정 파일과 핵심 함수

- `backend/app/prompting.py`: `build_prompt()`가 예산 초과 시 미요약 턴을 버리지 않고 오류를 발생시킨다. DB 메타데이터를 모델 메시지에서 제거한다. 기존 요약은 system 메시지가 아닌 사용자 역할의 참고자료로 전달한다. 이미지 프롬프트 외 모드는 legacy `context_policy=none`으로 이력을 우회하지 않는다. `summary_candidate()`의 최근 유지 0 설정도 처리한다.
- `backend/app/main.py`: `begin_generation()`은 공통 완료 이력 조회를 사용한다. `check_payload_budget()`는 두 모드 모두 완성된 payload를 검증한다. `fit_chat_payload()`는 검증만 수행한다. `prepare_chat_context()`가 요청 전 압축·재조회·strict 실패를 담당한다.
- 같은 파일: `queue_roleplay_summary(force=True)`가 동적 최근 턴 보존을 적용한다. `_run_summary_attempt()`가 전체 요약 payload 예산과 정상 종료를 검증한다. `run_summary_job()`이 실행 잠금·시간 제한·재시도 횟수·더 작은 연속 범위 재시도를 관리한다.
- 같은 파일: `lifespan()`이 중단 작업을 시작 시 검사하고 종료 시 background task를 취소·회수한다. `update_conversation()`과 자동 커밋은 `BEGIN IMMEDIATE` 및 수정 버전으로 보호한다. `check_context_revision()`은 최종 계수 전후에 오래된 기억 snapshot을 검출한다.
- `backend/app/config.py`, `.env.example`: 압축·작업 한도 설정 추가.
- `backend/app/db.py`: v24 additive migration.
- `frontend/src/api.ts`, `frontend/src/main.tsx`: 요약 버전 수신, 편집 화면을 열 때의 버전 전송, strict 문맥 보존 안내.
- `tests/test_context_preservation.py`: 보존·압축·실패·동시성·복구 회귀 테스트.
- 기존 prompting/model/API/gate/summary/title/migration 테스트: 의도적으로 바뀐 strict 정책, stop 응답, 완전한 설정 fixture, 새 스키마 버전을 반영.
- `frontend/tests/NewConversation.test.tsx`: 요약 편집 버전 전송 및 409 충돌 시 입력 보존 검사.

## 4. 요청 전·후 압축 정책

1. 요청 후 background 압축을 유지한다. 토큰 임계값 또는 메시지 개수 압박으로 큐에 넣는다.
2. 다음 요청은 요약 이후의 완료 이력 전체를 가져온다. 현재 미완료 사용자 메시지는 압축 대상에 넣지 않는다.
3. 필수 입력과 전체 이력을 조합하고 최종 payload를 계수한다.
4. 실제 예산 초과 또는 메시지 개수 목표 초과 시 요청 전 압축을 시도한다.
5. 성공 후 DB를 다시 읽어 새 요약 범위 이전 원문을 중복 입력하지 않는다.
6. 압축 실패·시간 초과 뒤에도 전체 입력이 가능한지 재평가한다. 가능하면 모두 전달하고, 불가능하면 `context_preservation_failed`(422)를 반환한다. 이력이 없는 필수 입력 자체의 초과는 `context_budget_exceeded`다.
7. 모델 epoch가 바뀌면 이전 계수를 신뢰하지 않는다. 요약 작업은 제한된 재준비를 수행하고, 채팅 요청은 최종 계수/요청의 epoch 불일치를 명시적으로 반환한다.

| 설정 | 기본값 | 의미 |
|---|---:|---|
| `CONTEXT_COMPACTION_MAX_ATTEMPTS` | 3 | 한 사용자 요청이 처리할 요약 작업의 최대 개수 |
| `CONTEXT_COMPACTION_TIMEOUT_SECONDS` | 30 | 요청 전 준비의 시간 예산. 80%를 압축에 사용하고 나머지를 최종 재평가에 예약 |
| `SUMMARY_JOB_MAX_ATTEMPTS` | 3 | 작업별 저장된 실행 시도 상한. 실행 내 epoch 재준비도 이 값으로 제한 |
| `SUMMARY_JOB_TIMEOUT_SECONDS` | 180 | background 요약 작업의 실행 시간 상한 |

요청 전 작업은 위 30초 deadline에도 구속된다. 여러 요청이나 재시작에 걸친 실패 작업의 실행 횟수는 `summary_job_state.attempt_count`에 남는다. 새 사용자 요청이 새 작업을 만들 수 있으므로 대화 전체의 평생 재시도 횟수를 제한하는 설정은 아니다.

시간 제한은 asyncio cancellation 기반이며 SQLite 동기 조회 및 OS 스케줄링 지연까지 하드 실시간으로 보장하지 않는다. 아주 큰 가져오기 이력은 현재 전부 메모리로 읽으므로 2차에서 페이지 단위 계획을 검토한다.

## 5. DB와 충돌 처리

스키마 버전 **23 → 24**:

- `conversations.summary_revision INTEGER NOT NULL DEFAULT 0`
- 새 `summary_job_state(job_id PRIMARY KEY REFERENCES summary_jobs(id) ON DELETE CASCADE, base_revision, attempt_count)`

기존 summary_jobs 열은 그대로 유지한다. 상태·에러 본문은 기존 필드를 사용하므로 last_error_code를 추가하지 않았다. 실행 소유권은 OS 잠금으로 제공하므로 lease_owner/lease_expires_at도 추가하지 않았다.

잠금 파일은 `<database_path>.summary-locks/<job_id의 SHA-256>`에 생성한다. 모든 worker가 같은 로컬 DB와 잠금 디렉터리를 사용해야 한다. `flock(LOCK_EX|LOCK_NB)` 획득 실패 시 다른 worker의 작업은 회수하지 않는다. 프로세스 강제 종료 시 OS가 잠금을 해제하므로 재시작한 worker가 이어서 처리할 수 있다. 잠금 파일을 실행 중 삭제하면 다른 inode로 잠금이 분리되므로 삭제하지 않는다.

최종 커밋은 write transaction 안에서 작업 상태, 요약 수정 버전, 기존 요약, 시작 커서, 선택된 메시지 ID들의 연속성을 재확인한다. 통과한 경우에만 요약 본문·커서·버전·작업 완료를 함께 갱신한다. 수동 편집도 write transaction에서 버전을 올리고 이전 자동 작업을 무효화한다.

이 방식은 **한 호스트의 공유 로컬 파일시스템에 있는 SQLite/잠금**을 전제로 한다. 여러 호스트/NFS 환경을 지원한다고 주장하지 않는다. 기존 생성 세마포어는 여전히 프로세스별이므로 다중 worker의 총 LLM 동시성은 별도 운영 고려 대상이다.

## 6. API 호환성

- 대화 생성/조회/수정 응답에 `summary_revision`을 추가했다.
- 수정 요청에 선택적 `expected_summary_revision`을 추가했다. 일치하지 않으면 `summary_revision_conflict`(409).
- 기존 클라이언트의 버전 없는 수정은 계속 허용한다. write lock과 자동 작업 무효화는 적용되지만, 서로 다른 오래된 수동 편집 사이의 충돌 검출에는 버전 전송이 필요하다. 새 프런트엔드는 버전을 전송한다.
- 요약 삭제(빈 문자열 저장)는 기존처럼 커서를 유지한다. 임의로 원문 재생성 의미로 바꾸지 않았다.
- 새로운 명시적 오류에는 `context_preservation_failed`, `context_changed`, `summary_cursor_invalid`가 있다.
- 요청 전 준비 실패는 SSE를 열기 전 HTTP 422/409로 반환될 수 있다. 프런트엔드의 기존 HTTP 오류 처리를 사용한다.
- 이미지 프롬프트의 독립 입력은 유지한다. 일반적인 최종 payload 검사에는 기존 이미지 JSON schema 크기도 포함된다.

## 7. 검증 결과

검증 명령과 최종 결과는 아래에 기록한다. 실모델 호출 대신 합성 이력과 mock gate를 사용했다.

### 핵심/관련 백엔드

```sh
APP_API_KEY=client-key LLM_GATE_API_KEY=gate-key .venv/bin/python -m pytest \
  tests/test_context_preservation.py tests/test_prompting.py tests/test_model_context.py \
  tests/test_summary_model_budget.py tests/test_api.py tests/test_background_switch_deferral.py \
  tests/test_image_prompt.py tests/test_image_intent.py tests/test_wiki_evidence.py \
  tests/test_wiki_memory_integration.py tests/test_title_generation.py \
  tests/test_client_gate_protocol3_integration.py tests/test_wiki_paths.py \
  tests/test_wiki_cutover_rehearsal.py -q
```

실행 결과: **248 passed**. 앞선 기준선 38개와 별도로 실제 실행한 결과다.

### 전체 검사와 프런트엔드

- 최종 핵심 테스트 재실행: `tests/test_context_preservation.py tests/test_prompting.py tests/test_model_context.py tests/test_summary_model_budget.py` → **61 passed in 3.12s**. 이 중 새 보존 회귀 테스트는 23개다.
- 전체 `APP_API_KEY=client-key LLM_GATE_API_KEY=gate-key .venv/bin/python -m pytest -q` → **1,106 passed, 6 skipped, 14 failed, 4 errors**. 실패/오류 18건은 모두 샌드박스의 로컬 소켓 생성 PermissionError였다.
- 소켓 권한을 허용해 `tests/test_cleanup_process_crash.py tests/test_final_candidate_flow.py tests/test_worker_systemd_socket_lifecycle.py tests/test_worker_socket_integration.py`를 재실행 → **13 passed, 5 failed**. 남은 5건은 자식 Python 프로세스의 `backend` 모듈 탐색 경로 오류였다.
- `PYTHONPATH=. APP_API_KEY=client-key LLM_GATE_API_KEY=gate-key .venv/bin/python -m pytest tests/test_cleanup_process_crash.py -q`를 같은 소켓 권한으로 실행 → **5 passed in 7.40s**. 위 18건은 나눠 재실행하여 모두 통과했다. 전체 한 번의 clean run 결과라고 표현하지 않는다.
- 프런트엔드 최초 기본 병렬 실행은 84 passed / 8 failed(타임아웃 및 비동기 UI 조회 실패). `npm test -- --maxWorkers=1 --testTimeout=15000 --reporter=dot`로 순차 실행하여 **92 passed**. 새 요약 충돌 UI 테스트 추가 후 해당 파일은 **4 passed**. 최종 동일 명령으로 전체 재실행한 결과는 **13 files / 93 tests passed in 52.90s**다.
- `npm --prefix frontend run build` → 성공. 500 kB 초과 번들 경고는 남아 있다.
- `git diff --check` → 통과. 테스트 환경에 Starlette/httpx deprecation 및 Node localStorage 경고가 있으나 해당 테스트는 통과했다.

## 8. 배포·롤백·실기 검증

배포 시:

1. 모든 이전 버전 worker를 정지하고 SQLite backup API 또는 정지 상태에서 DB를 백업한다. WAL 사용 환경이라면 DB 파일만 무작정 복사하지 않는다.
2. 백엔드·프런트엔드를 함께 배포한다. 초기화 시 additive v24 migration이 실행된다.
3. 서비스 사용자가 DB와 옆의 `.summary-locks` 디렉터리에 쓰기 권한이 있는지 확인한다. 기존 worker와 새 worker를 혼합 실행하지 않는다.
4. gate가 요약 응답에 `finish_reason=stop`을 반환하는지 확인한다. 해당 필드가 없는 응답은 이제 실패 처리한다.
5. 로컬 `.env`는 수정하지 않았다. `.env.example`의 새 기본값을 참고해 운영 지연 허용 범위에 맞춰 설정한다.

롤백:

- 모든 새 worker를 정지하고 이전 코드/프런트엔드를 함께 복구한다. 추가 열/테이블은 원문을 변경하지 않으므로 보통 그대로 둘 수 있다. 이전 코드가 새 버전 마이그레이션을 어떻게 다루는지는 배포 전 복제 DB에서 확인한다.
- 정확한 이전 DB 상태가 필요하면 백업으로 복구하되 배포 후 생성된 대화는 별도 보존한다. 운영 DB에서 즉석 DROP COLUMN을 수행하지 않는다.
- 잠금 파일 정리는 모든 worker가 정지한 상태에서만 검토한다. 이전 코드로 돌아가면 silent-drop 방지 기능도 사라진다.

BC-250 실기에서 아직 확인하지 않은 항목:

- 실제 모델의 토큰 계수, chat template, user 역할 요약 자료와 연속 user 메시지 수용 여부.
- 4k/16k 등 실제 runtime context에서 Wiki·긴 출력 예약·긴 턴을 함께 사용한 결과.
- 요청 전 요약 지연, 이미지 우선 대기, 모델 교체, 실제 네트워크 timeout.
- 반복 요약 시 목표/수치/정정/미승인 제안 보존 품질.
- 실제 서비스 강제 종료·재시작, 배포 파일 권한, 다중 worker 운영 부하.

로컬 통과는 위 실기 검증 완료를 의미하지 않는다.

## 9. 2차 개선안

1. **초장문 단일 턴 분할**: 임시 조각별 요약을 별도로 저장하고 원래 턴의 모든 조각이 처리됐을 때만 커서를 이동한다. 1차는 명시적 실패로 보존한다.
2. **구조화 JSON 요약**: 목표·제약·확정 결정·미해결·최신 정정·미승인 제안을 분리한다. schema 유효성과 사실 정확성 평가는 별도로 수행한다.
3. **원문 출처 ID**: 항목별 메시지 ID를 기록하고 중요한 수치·코드는 원문으로 재조회한다. 보존 범위와 원문 인용을 UI에서 확인한다.
4. **SQLite FTS5 검색**: 요약이 놓친 과거 사실을 검색해 필요한 원문만 보강한다. 새 벡터 인프라 없이 먼저 평가한다.
5. **페이지 단위 예산 계획**: 매우 큰 이력의 메모리·SQL 비용을 줄이고 정확한 토큰 계수 호출 수를 줄인다.
6. **관측 정보**: context_status, included_message_count, omitted_uncompressed_count(정상 strict 응답은 0), token_count_source를 생성별로 저장·표시한다.
7. **품질 평가 세트**: 첫 턴 제약, 중간 정정, 수치·코드, assistant 미승인 제안, topic 전환을 포함한 합성 대화를 반복 압축해 원문 기준 보존율과 오기억 비율을 측정한다.
8. **분산 운영**: 다중 호스트가 실제로 필요해지면 OS 잠금 대신 lease/heartbeat/소유권 fencing을 설계하고 전역 생성 동시성도 함께 다룬다.
