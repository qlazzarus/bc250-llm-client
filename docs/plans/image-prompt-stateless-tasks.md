# 이미지 프롬프트 독립 요청 UX — Terra Medium 수정 태스크

2026-10-26 후속 계획: 결과 카드 프롬프트 편집 및 실제 Studio 제출은 [image-generation-integration-tasks.md](image-generation-integration-tasks.md)의 GI01~GI08을 따른다. 아래 IG01~IG03은 초기 개략안이며 새 문서가 이를 대체한다. 로컬 Studio API 확인 결과, 정사각형 해상도 제한과 upstream 멱등성 부재를 반영했다.

## 사용자 의도와 리뷰 결론

이미지 프롬프트 화면은 매번 입력한 한국어 요청을 독립적으로 변환하는 도구다. 사용자는 시작 시 주제·구도·스타일·조명·색감·제외 요소 양식을 작성하지 않는다. 서버의 모델별 시스템 프롬프트가 영어 prompt와 생성 설정 JSON을 만든다. 결과는 화면과 DB에 기록하지만 다음 요청의 LLM 컨텍스트에는 넣지 않는다.

현재 구현은 이 의도와 다르다. frontend/src/main.tsx는 이미지 모드에 여섯 설정 필드를 표시하고, backend/app/main.py는 새 이미지 대화에 recent 정책을 부여한다. prompting.py는 저장 설정과 최근 완료 턴을 넣고 이전 결과의 수정도 지시한다. 따라서 UI만 숨기는 것으로 해결되지 않는다.

이 문서는 image-prompt-tasks.md의 기존 설정 반영·최근 턴·후속 수정 결정을 대체한다. 이전 IP01~IP06 완료 기록은 과거 구현 기록이며 새 요구사항 완료를 뜻하지 않는다. 이번 요청은 계획 작성이다. 아래 구현은 별도 실행 지시 후 수행한다.

## 확정할 동작

- 모드 ID image_prompt 유지. 화면은 한국어 요청 입력, 작성 상태, 영어 프롬프트/설정 결과 카드, 복사 기능으로 구성한다.
- 이미지 모드 시작·설정 화면에서 여섯 필드를 제거한다. 제목은 기존 기본값으로 시작할 수 있게 하며 응답 길이·출력 언어·이미지 모델 선택을 요구하지 않는다.
- 모델 입력은 서버의 고정 시스템 지시 + 현재 사용자 요청 한 건이다. 다른 메시지, 저장된 mode_settings, summary, 이전 이미지 결과는 넣지 않는다.
- 기록 저장과 컨텍스트 사용을 구분한다. 기록 조회·페이지 조회·삭제는 유지한다. 이전 메시지를 자동 참조하는 '더 어둡게' 요청은 지원한다고 안내하지 않는다. 원하는 전체 장면을 다시 적도록 입력창 주변에 설명한다.
- 기존 이미지 대화도 독립 요청 정책으로 전환한다. 이전 메시지·설정은 삭제하지 않고 보존하되 설정은 더 이상 생성에 사용하지 않는다.
- 대상 프로필은 z-image-nsfw-v2 하나다. 영어 prompt와 기존 6필드 JSON 계약, 크기/steps/CFG 허용값은 유지한다. 이는 제공받은 테스트 규칙 기반이며 실제 최적 품질·성능은 서버 샘플 검증 전까지 확정하지 않는다.
- 부족한 구도·조명·스타일은 현재 요청에 맞는 합리적인 기본값으로 보완하도록 시스템 지시한다. 사전 문답을 요구하지 않는다. 영어 언어·의미 품질은 JSON 스키마만으로 보장할 수 없다.
- non-streaming, 추론 OFF, temperature 0.5, 전용 토큰 예산, gate 경유·인증·JSON 검증·재시도 계약은 유지한다.

## 작업 순서와 경계

| ID | 작업 | 의존성 | 상태 |
|---|---|---|---|
| IS01 | 이미지 모드 독립 요청·기존 DB 정책 전환 | 없음 | 완료 |
| IS02 | 모델 프로필과 독립 변환 시스템 지시 | IS01 | 완료 |
| IS03 | 입력 화면 단순화·결과 재사용 UX 정리 | IS01, IS02 | 완료 |
| IS04 | 통합 회귀·실행 안내·점검 스크립트 교정 | IS03 | 완료 |
| IG01 | Studio 기존 API·GPU 조정 계약 조사 | IS04 | 향후, 실행 미승인 |
| IG02 | 서버 이미지 작업 제출·중복 방지·상태 저장 | IG01 | 향후, 실행 미승인 |
| IG03 | 결과 카드의 명시적 생성 버튼·작업 상태·결과 | IG02 | 향후, 실행 미승인 |

한 번에 한 태스크만 수행하고 관련 파일·사용자 변경을 먼저 확인한다. 각 종료 시 변경 파일·검증·미검증 사항을 기록한다. 원격 실행·배포·Studio 수정은 사용자 실행으로 남긴다. 새 계획을 구현할 때 기존 테스트 기대값도 새 계약에 맞게 바꾸되 quick/roleplay 회귀 검증은 유지한다.

## IS01 — 독립 요청과 데이터 호환

파일: backend/app/main.py, prompting.py, db.py, tests/test_api.py, tests/test_prompting.py.

- 새 image_prompt의 context_policy를 none으로 지정한다.
- 버전 관리되는 반복 실행 안전한 마이그레이션으로 기존 image_prompt도 none으로 전환한다. quick/roleplay 데이터는 변경하지 않는다.
- 이미지 모드는 begin_generation에서 히스토리 조회를 생략한다. build_prompt에서도 mode를 확인해 DB에 recent가 남아 있어도 이전 메시지를 넣지 않는다.
- 기존 mode_settings·summary·메시지는 보존하되 이미지 생성 프롬프트에서는 무시한다. 새 이미지 대화는 mode_settings={}만 허용한다. 이미지 설정 PATCH에서 비어 있지 않은 값은 명확한 422로 거절한다. 제목 변경 시 기존 저장 설정을 실수로 지우지 않는다.
- 완료 요청 replay는 기존 저장 결과를 돌려주는 의미를 유지한다. 재시도와 새 요청을 구분한다. 이미지 자동 요약은 계속 금지한다.

완료 조건: 같은 방에서 A 요청 후 B 요청을 보내도 payload는 system+B 두 메시지만 포함한다. legacy recent/설정/summary도 누출되지 않는다. 마이그레이션 2회 후 데이터 보존·다른 모드 불변, 인증·재시도 테스트 통과.

구현 기록: `backend/app/db.py`에 반복 실행 안전한 v5 마이그레이션을 추가했고, `main.py`와 `prompting.py`에서 이미지 모드의 저장된 정책값과 무관하게 히스토리 조회·삽입을 차단했다. 기존 `mode_settings`는 제목 변경 때 보존하지만 새 비어 있지 않은 이미지 설정은 422로 거절한다. `PYTHONPATH=. .venv/bin/pytest` 결과는 40 passed이며, 실제 BC-250 gate 호출은 하지 않았다.

## IS02 — 모델 프로필과 시스템 지시

파일: backend/app/image_prompt.py, prompting.py, config.py, tests/test_image_prompt.py, tests/test_prompting.py.

- 대상 모델 ID·스키마·기본 설정·시스템 지시가 같은 프로필을 참조하도록 정리한다. 지금은 단일 프로필만 구현하고 범용 플러그인 구조는 만들지 않는다.
- 이전 응답을 수정한다는 지시와 Configured preferences 부분을 제거한다. 현재 요청만 변환하고 빠진 묘사는 적절히 보완하도록 지시한다.
- 기존 영어 prompt/6필드 JSON, steps 6, CFG 1.0, 기본 768×768, 요청 시 preview 512×512 규칙을 유지한다. 현재 요청에 명시된 장면 제약은 반영한다.
- 이미지 대상 model_id와 /v1/models에서 확인한 LLM model을 구분한다. 브라우저 입력으로 모델 경로나 호출 URL을 선택하지 못하게 한다.
- schema 생성/검증과 입력 토큰 예산을 유지한다. 원문·reasoning 로그를 추가하지 않는다.

완료 조건: 프로필 상수와 스키마·시스템 지시 일치, 저장 설정 미반영, 영어/JSON 출력 규칙, 필수 입력 예산 초과 처리, quick/roleplay 프롬프트 회귀 테스트 통과. 실제 모델 품질은 별도 검증으로 명시.

구현 기록: `backend/app/image_prompt.py`에 단일 `IMAGE_PROMPT_PROFILE`을 두고 대상 이미지 모델 ID, JSON schema 이름·필드, 허용 크기/steps, 기본 768×768·6 steps·CFG 1.0을 함께 정의했다. schema와 system instruction은 이 프로필을 참조한다. 시스템 지시는 현재 요청만 변환하고, 빠진 묘사는 장면에 맞게 보완하되 후속 질문을 하지 않도록 바꿨다. 이전 응답 수정·저장 설정·Configured preferences 지시는 제거했다. `config.py`의 서버 전용 `IMAGE_PROMPT_MAX_TOKENS` 검증은 이미 이 계약에 맞으므로 변경하지 않았다. `PYTHONPATH=. .venv/bin/pytest tests/test_image_prompt.py tests/test_prompting.py tests/test_api.py` 결과는 41 passed이며, 실제 BC-250 품질 검증은 수행하지 않았다.

## IS03 — 화면 단순화와 요청 격리

파일: frontend/src/main.tsx, api.ts, components/MessageList.tsx, ImagePromptResult.tsx.

- 이미지 모드에서 ModeFields를 표시하지 않는다. 새 대화는 이미지 모드 선택 후 바로 시작 가능한 기본 제목을 제공한다.
- 설정 화면은 제목 정도만 제공한다. 과거 mode_settings를 재전송하지 않고 허용된 변경 필드만 PATCH한다.
- '최근 대화 포함', '수정 사항' 문구를 제거하고 '매 요청은 독립적으로 작성됩니다. 원하는 장면을 전체 문장으로 입력하세요'라고 안내한다.
- 기록·영어 프롬프트 카드·JSON 복사·HTTP 수동 복사를 유지한다. 카드 렌더링은 image_prompt 모드에만 적용한다.
- 진행 요청에 conversationId/requestId/controller를 함께 연결한다. 다른 방으로 이동해도 완료·오류 안내가 새 방을 덮어쓰지 않고 취소가 원래 방 요청을 대상으로 하게 한다. 현재 코드의 active.id 기반 취소 경로를 확인한다.
- 일반 질문과 롤플레잉은 기존 입력·설정·SSE를 유지한다. 이미지 생성 버튼은 아직 추가하지 않는다.

완료 조건: 신규/기존 이미지 대화에서 사전 설정 필드 없음, JSON POST와 복사 정상, 실패 후 같은 ID 재시도, 전환 중 취소·결과 격리 확인. lint/build 통과. 브라우저 미실행은 명시한다.

구현 기록: `frontend/src/main.tsx`에서 이미지 모드의 새 대화·설정 양식에 사전 설정 필드를 렌더링하지 않도록 했고, 저장 시 기존 `mode_settings`를 다시 PATCH하지 않는다. 이미지 모드 안내와 입력 placeholder는 전체 장면을 독립적으로 작성하도록 바꿨다. 이미지 JSON 결과 카드는 image_prompt 모드에서만 렌더링하도록 `MessageList`에 mode를 전달했다. 진행 중 요청은 conversation ID·request ID·AbortController를 한 묶음으로 추적해, 다른 대화방으로 이동한 뒤에도 취소 endpoint는 원래 요청을 대상으로 하고 완료·오류 안내는 새 방을 덮어쓰지 않는다. 실패 재시도 정보도 원래 방으로 돌아올 때만 표시한다. `frontend`의 `npm run lint`, `npm run build` 및 루트의 `PYTHONPATH=. .venv/bin/pytest`는 각각 통과했다. 실제 브라우저·BC-250 서버 실행은 수행하지 않았다.

## IS04 — 회귀와 사용자 실행 안내

파일: tests/, scripts/check_image_prompt.py, README.md, 이 문서.

- IP05의 '이전 완료 JSON을 참고한다' 테스트를 독립 요청 검증으로 대체한다. A/B 요청을 정확히 확인하고 실패·취소·다른 방·기존 설정이 모두 제외됨을 검증한다.
- 완료 replay, 잘린 JSON, 불일치 스키마, timeout, 응답 전 취소/대화 삭제, quick/roleplay 스트림을 검증한다. gate HTTP 오류를 무조건 스키마 미지원으로 단정하는 기존 안내는 상태별로 구분한다.
- 점검 스크립트는 앱과 같은 프로필 지시·스키마를 사용한다. 기본 조회 후 생성 1건만 수행하며 출력은 안전한 메타데이터로 제한한다. gate URL은 루프백 8081인지 확인하고 redirect로 다른 주소에 키를 보내지 않는다.
- 현재 실행 문서의 두 문제를 수정한다: root 소유 600 env는 deployuser로 source할 수 없으며, python scripts/check_image_prompt.py는 루트 패키지 import 경로를 보장하지 않는다. env 읽기 권한을 넓히지 않는 실행 방법과 루트에서 python -m scripts.check_image_prompt를 제공하고 로컬에서 모킹으로 검증한다.
- 백엔드/프런트 동시 패키징, 최종 경로 venv 생성, 중지 후 DB 백업, 마이그레이션, 상태 확인, 코드·DB·unit 복원 롤백을 사용자 명령으로 정리한다. Caddy Host 헤더를 포함한다.

완료 조건: 전체 pytest와 frontend lint/build 통과, 실제 서버 미검증 목록과 사용자 실행 체크 준비. 실제 요청 품질/Clipboard/image_priority 검증은 실행 전 완료 처리하지 않는다.

구현 기록: 독립 요청 A/B·기존 설정·실패/취소 제외와 완료 replay·재시도·quick/roleplay 스트림 회귀는 기존 및 IS01 테스트로 유지했고, gate의 이미지 schema 거절(400/422)과 인증 설정 실패(401/403)를 분리했다. `scripts/check_image_prompt.py`는 앱 프로필의 system instruction/schema를 재사용하고 `http://127.0.0.1:8081`만 허용하며 redirect를 거부한다. 모킹 테스트로 payload·URL 검증을 추가했다. README는 root 600 환경 파일을 `deployuser`가 source하지 않도록 중첩 sudo의 제한 환경 보존 명령, `python -m scripts.check_image_prompt`, 독립 요청 브라우저 점검, 패키징·DB 백업·롤백 절차를 정리했다. `PYTHONPATH=. .venv/bin/pytest`는 48 passed, `frontend`의 `npm run lint`와 `npm run build`는 통과했다. 실제 BC-250 gate/Studio/image_priority, HTTP Clipboard, Caddy reload과 배포는 사용자가 실행해야 한다.

## 향후 실제 이미지 생성으로 확장하는 리뷰

현재 결과 JSON은 생성 요청의 입력으로 재사용하기 좋다. 다만 JSON이 만들어졌다는 이유로 자동 생성하면 재시도/새로고침 때 GPU 작업을 중복 실행할 수 있다. 결과 확인 후 사용자가 특정 카드의 '이 설정으로 이미지 생성'을 눌러 별도 작업을 만드는 방식이 적합하다. 프롬프트 작성과 이미지 작업은 서로 다른 요청 ID·상태를 가진다.

브라우저 → FastAPI 이미지 작업 API → 기존 Studio의 공식 제출 API를 호출하는 구조를 우선 검토한다. Studio 주소·키는 서버에만 둔다. 이미지 작업 동안 LLM pause/resume을 이 앱이 임의 구현하지 않고 기존 Studio/gate의 image_priority 협약을 따른다. 실제 API가 확인되기 전 endpoint나 요청 형식을 추정하지 않는다.

### IG01 — 연동 계약 조사 (읽기 전용)

- 사용자가 제공하는 Studio API 문서/코드로 제출·조회·취소·이미지 조회·인증·모델 ID/파라미터·동시성·중복 방지 지원을 확인한다. 기존 /opt/bc250-ai-studio는 수정하지 않는다.
- 클라이언트의 6필드 JSON을 Studio 입력에 매핑하고 seed 등 추가 필드의 기본 정책을 명시한다. 모델 별칭이 다르면 서버 allowlist 매핑으로 처리한다.
- Studio 작업이 gate에 이미지 우선 상태를 알리는 기존 경로를 확인한다. 지원되지 않는 부분은 차단 조건으로 보고하고 운영 변경을 추정하지 않는다.

완료 조건: 확인된 endpoint·인증·작업 상태·GPU 조정 계약·매핑 문서. 접근이 없으면 IG02를 진행하지 않고 필요한 자료를 명시한다.

### IG02 — 작업 제출과 상태 저장

- IG01 계약에 맞춰 인증된 서버 API와 Studio adapter를 구현한다. 기본 비활성 환경변수로 배포한다.
- 브라우저는 source assistant message ID와 idempotency key만 제출한다. 서버가 저장된 JSON을 다시 검증하고 정확한 설정 스냅샷을 job에 보관한다. 이전 자연어·실패 메시지는 거절한다.
- 중복 클릭은 같은 job을 반환한다. Studio 제출 timeout은 '실패했으니 자동 재제출'하지 않고 unknown 상태로 두고 upstream idempotency/조회로 조정한다.
- queued/running/succeeded/failed/cancelled/unknown 상태와 제한된 polling을 구현한다. 앱 재시작 후 상태를 복구하고 취소는 Studio가 지원할 때만 제공한다.
- 이미지 반환은 인증된 프록시/허용된 artifact ID로 제한한다. LLM 출력의 임의 URL·파일 경로를 fetch하지 않는다. 기록 삭제 중 진행 작업 처리 정책도 정의한다.

완료 조건: mock Studio로 중복 방지·제출 timeout·재시작·취소·오류·인증 테스트. 실제 GPU 실행은 사용자 점검.

### IG03 — 명시적 생성 UX

- 검증된 결과 카드에만 '이 설정으로 이미지 생성'을 표시한다. 연동 활성화 및 해당 프로필 지원이 확인돼야 활성화한다.
- 클릭 전에 모델·해상도·steps가 보이게 하고 클릭 후 대기/생성/완료/실패를 표시한다. 제출 중 버튼을 막고 서버 idempotency와 결합한다.
- 완료 이미지 조회·다운로드, 지원 시 취소, 실패 원인을 제공한다. 단순 프롬프트 작성/복사 동작으로 생성이 시작되지 않게 한다.
- 동일 설정으로 다시 생성은 사용자의 새로운 명시적 작업으로 구분한다. 대화 전환·새로고침 후에도 기존 job을 복원한다.

완료 조건: mock UI 검증 후 사용자가 실제 이미지 1건을 실행해 Studio/gate image_priority와 결과를 확인. 원격 배포는 사용자가 수행한다.

## Terra Medium 실행 지시

```text
docs/plans/image-prompt-stateless-tasks.md를 읽고 IS01부터 한 태스크씩 진행해줘.
이미지 모드는 서버 시스템 프롬프트 + 현재 요청만 사용하는 독립 변환 화면이야.
기존 이미지 기록·설정 데이터는 보존하되 이전 대화나 설정을 LLM 입력에 넣지 마.
사전 설정 양식을 제거하고 영어 프롬프트·설정 JSON 결과와 복사 기능을 유지해.
각 태스크의 테스트 결과·변경 파일·미검증 항목을 문서와 답변에 남겨줘.
IG01~IG03은 향후 확장 계획이며 별도 지시 전 구현하지 마.
실제 서버 명령·배포·이미지 작업 실행은 사용자가 직접 해.
```
