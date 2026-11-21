# 채팅과 컨텍스트 보존

## 대화와 생성

일반 대화(`quick`)와 롤플레잉(`roleplay`)은 완료된 이전 대화와 장기 기억 요약을 사용한다. 롤플레잉은 캐릭터·세계관·말투·현재 상황·규칙을 대화 설정에 저장한다. 이미지 프롬프트(`image_prompt`)는 매 요청을 독립적으로 처리한다.

일반 대화의 기본 temperature는 0.7이다. 응답 길이는 short 512, medium 1024, long 2048토큰이며 `MAX_COMPLETION_TOKENS` 상한을 적용한다. 새 대화의 기본 응답 길이는 medium이다. 이미지 프롬프트 출력 예산은 별도 `IMAGE_PROMPT_MAX_TOKENS`를 사용한다.

생성은 요청 ID로 관리한다. 같은 요청의 재조회와 완료 결과 재사용을 지원하고 같은 대화의 동시 pending 생성은 거절한다. 실패·취소된 생성 메시지는 완료 이력에서 제외한다. 스트리밍은 SSE로 최종 답변 delta만 표시한다. 이미지 프롬프트는 완성된 JSON을 반환하며 스트리밍하지 않는다.

메시지 목록은 cursor pagination과 화면 가상화를 사용한다. 기본 제목 `새 대화`는 첫 답변 완료 후 자동 생성하며, 생성 실패 시 첫 메시지 일부를 사용한다. 수동 제목은 유지한다.

## 입력 구성과 예산

```text
모드 system 지시
→ 기존 요약(user 역할의 비지시 참고자료)
→ 요약 커서 이후 완료 이력 전체
→ 현재 질문과 선택된 Wiki 참고자료
```

`completed_history_after_summary()`는 요약 커서의 created_at/id 이후 완료 이력을 조회한다. 커서 메시지가 없으면 복구 필요 오류를 반환한다. 원문은 요약으로 삭제하지 않는다.

`MAX_HISTORY_MESSAGES`의 기본값 200은 압축 트리거다. 이 값을 넘었다는 이유로 미요약 메시지를 제거하지 않는다. `build_prompt()`와 `fit_chat_payload()`도 예산 초과 시 이전 턴을 조용히 버리지 않는다.

최종 입력 + 출력 예약 + 안전 여유가 컨텍스트 안에 들어가야 한다. 안전 여유 기본값은 512토큰이다. model control 활성 시 gate의 최종 payload 토큰 계수와 모델 epoch를 사용한다. 비활성 시 UTF-8 바이트 기반 추정과 메시지 framing 여유를 사용하며, 실제 토크나이저의 상한을 보장하지 않는다. JSON schema가 있으면 예산에 포함한다.

Wiki 자료 때문에 필수 입력이 너무 크면 참고 문서·기억 자료를 줄일 수 있다. 미요약 채팅 이력은 별도의 strict 검사로 보호한다. 모델 epoch가 달라지면 이전 계수 결과로 추론하지 않는다.

## 압축 정책

답변 후 background 요약은 미요약 이력의 추정 5000토큰 또는 메시지 개수 압박을 기준으로 동작한다. 최근 6턴을 원문으로 남기는 것이 기본 목표이며 요약 출력 상한은 512토큰이다.

요청 전에는 전체 payload를 검사한다. 예산이나 메시지 개수 목표를 넘으면 오래된 완료 턴부터 압축하며, 필요하면 최근 유지 턴 수를 줄인다. 현재 사용자 질문은 요약하지 않는다. 성공 후 DB를 다시 읽어 요약 범위 원문을 중복 입력하지 않는다.

압축에 실패해도 전체 이력이 예산에 들어가면 모두 전달한다. 들어가지 않으면 `context_preservation_failed`(422)를 반환한다. 필수 입력 자체가 너무 크고 이전 이력이 없으면 `context_budget_exceeded`다. 한 턴이 요약 예산보다 커도 그 턴을 건너뛰지 않는다. 현재 구현은 턴 내부를 분할하지 않는다.

| 설정 | 기본값 | 제한 |
|---|---:|---|
| `CONTEXT_COMPACTION_MAX_ATTEMPTS` | 3 | 한 요청의 요약 작업 개수 |
| `CONTEXT_COMPACTION_TIMEOUT_SECONDS` | 30초 | 요청 전 준비 예산; 80% 압축, 나머지 재평가 |
| `SUMMARY_JOB_MAX_ATTEMPTS` | 3 | 작업별 영속 실행 시도 및 실행 내 epoch 재준비 상한 |
| `SUMMARY_JOB_TIMEOUT_SECONDS` | 180초 | background 요약 작업 실행 시간 |

시간 제한은 asyncio cancellation 기반이다. 동기 SQLite 처리와 OS 지연까지 하드 실시간으로 제한하지 않는다. 이력 전체를 메모리로 읽는다.

## 요약 검증과 복구

요약은 `finish_reason=stop`, 유효한 문자열·메시지 형태·길이를 확인한다. 잘림·빈 응답·잘못된 구조는 저장하지 않는다. 실패하면 더 작은 연속된 과거 범위로 제한적으로 재시도하며 기존 요약과 커서는 유지한다. 출력 형식 검사는 사실 보존 정확성까지 보장하지 않는다.

`summary_jobs`는 대화당 하나의 pending/running 작업만 허용한다. `summary_job_state`는 기준 수정 버전과 실행 횟수를 저장한다. 실행자는 `<DATABASE_PATH>.summary-locks/<job ID의 SHA-256>` 파일에 OS flock을 잡는다. 다른 worker가 잠금을 보유하면 회수하지 않는다. 프로세스 종료 시 잠금이 해제되고 시작 시 pending/running 작업을 검사한다. 실행 중 잠금 파일을 삭제하면 안 된다.

본문·커서·revision·작업 완료는 한 write transaction에서 갱신한다. 커밋 전에 기준 버전·본문·커서·처리 메시지의 연속성을 확인한다. 원문 중간을 건너뛰어 커서를 이동하지 않는다.

## 수동 편집과 API

대화 응답에는 `summary_revision`이 있다. 수정 요청의 `expected_summary_revision`이 다르면 `summary_revision_conflict`(409)를 반환한다. 프런트엔드는 편집 화면을 열었을 때의 버전을 전송한다. 버전 없는 기존 요청도 허용하지만 오래된 수동 편집끼리의 충돌 검출에는 버전 전송이 필요하다.

수동 수정은 revision을 올리고 기존 자동 작업을 무효화한다. 빈 요약 저장은 커서를 유지한다. 최종 채팅 계수 전후에 revision이 바뀌면 `context_changed`(409)를 반환한다. 요청 전 준비 오류는 SSE를 열기 전 HTTP 오류로 반환될 수 있다.

주요 API는 `/api/v1/conversations`, `/conversations/{id}/messages`, `/conversations/{id}/generations/{generation_id}`다. 관련 구현은 [main.py](../backend/app/main.py), [prompting.py](../backend/app/prompting.py), 회귀 검사는 [test_context_preservation.py](../tests/test_context_preservation.py)에 있다.
