# 이미지 프롬프트 JSON — Terra Medium 실행 태스크

> 요구사항 변경: 이미지 모드의 사전 설정·최근 턴·후속 수정 정책은 [독립 요청 수정 계획](image-prompt-stateless-tasks.md)이 대체한다. 아래 완료 기록은 기존 구현의 이력이다.

## 목표와 범위

한국어 요청을 z-image-nsfw-v2용 영어 프롬프트와 생성 설정 JSON으로 변환한다. 실제 이미지는 생성하지 않는다. 이 문서는 구현 계획이며 모든 IP 태스크는 미착수다.

기존 T01~T10 기능 위에 적용한다. 기존 T11 배포 검증은 별도로 유지한다. 이미지 모드에 관해서는 이 문서가 chat-modes-tasks.md의 범용 출력·언어 선택 결정을 대체한다.

호출 경로: 브라우저 /llm/api/v1 → FastAPI → LLM gate → llama.cpp. Studio 연동·작업 제출·8080 직접 호출·모델 교체·운영 설정 변경·실제 배포는 포함하지 않는다.

## 확정 계약

- 모드 ID는 기존 image_prompt를 유지한다. 화면 이름은 이미지 프롬프트다.
- chat completion의 model은 gate /v1/models에서 확인한 활성 LLM ID다. 결과 JSON의 model_id는 대상 이미지 모델이며 항상 z-image-nsfw-v2다.
- image_prompt는 upstream stream:false, temperature:0.5, enable_thinking:false를 사용한다. 기본 출력 예산은 512, 서버 설정으로 조정하되 전역 출력 상한을 넘지 않는다. quick/roleplay는 기존 스트리밍을 유지한다.
- response_format은 type:json_schema, json_schema.name:image_generation_config, strict:true와 아래 스키마를 전달한다. gate가 거절하면 호환성 오류를 반환한다. 스키마 제거 후 자동 재호출하거나 8080으로 우회하지 않는다.
- 전체 JSON을 검증한 후에만 완료 응답으로 저장·노출한다. 실패·취소 시 부분 JSON이나 reasoning_content를 저장·표시하지 않는다.
- 기본 768×768, steps 6. 빠른 미리보기 요청은 512×512. 가로·세로 요청은 허용된 크기 조합을 이용한다. 이는 프롬프트 지시이며 실제 품질 보장은 아니다.
- 기존 APP_API_KEY 인증, 요청 크기·히스토리·출력·동시성 제한, image_priority, gate 경유 규칙을 유지한다. 로그에는 키·Authorization·대화·LLM 원문을 남기지 않는다.

```json
{
  "type": "object",
  "properties": {
    "model_id": {"type": "string", "enum": ["z-image-nsfw-v2"]},
    "prompt": {"type": "string", "minLength": 1},
    "width": {"type": "integer", "enum": [512, 768]},
    "height": {"type": "integer", "enum": [512, 768]},
    "steps": {"type": "integer", "enum": [4, 6, 8]},
    "cfg_scale": {"type": "number", "enum": [1.0]}
  },
  "required": ["model_id", "prompt", "width", "height", "steps", "cfg_scale"],
  "additionalProperties": false
}
```

영어 출력, subject/composition/lighting/environment/style 포함은 시스템 지시와 실제 샘플 검토로 확인한다. JSON 스키마만으로 언어·의미적 품질을 보장한다고 설명하지 않는다. prompt의 공백-only는 서버 검증에서 거절한다. 숫자 문자열과 boolean을 숫자로 강제 변환하지 않는다. cfg_scale은 JSON 숫자 1 또는 1.0을 허용한다.

## 실행 규칙

- 한 번에 하나의 IP 태스크를 수행한다. 작업 전 해당 코드와 기존 변경을 읽고 완료 조건을 확인한다.
- 파일 목록은 탐색 시작점이다. 현재 구현을 확인하고 최소 범위로 수정한다.
- 사용자 실행용 명령·문서 준비는 가능하지만 SCP, SSH 운영 변경, 배포는 수행하지 않는다.
- 실제 gate 검증은 사용자 실행으로 분리한다. 미실행 항목을 통과로 기록하지 않는다.
- 태스크 종료마다 변경 파일, 실행한 검증, 남은 제약, 다음 태스크를 이 문서와 사용자에게 보고한다.

## 진행 현황

| 태스크 | 내용 | 의존성 | 상태 |
|---|---|---|---|
| IP01 | 출력 계약·검증기 | 없음 | 완료 — 2026-10-25 pytest 32건, frontend lint/build 통과 |
| IP02 | 시스템 프롬프트·기존 설정 호환 | IP01 | 완료 — 2026-10-25 pytest 34건, frontend lint/build 통과 |
| IP03 | gate 호출·저장·오류 수명주기 | IP01, IP02 | 완료 — 2026-10-25 pytest 38건, frontend lint/build 통과. 실제 gate의 json_schema 지원은 IP06 사용자 점검으로 남김 |
| IP04 | 이미지 모드 요청·결과·복사 UI | IP03 | 완료 — 2026-10-25 pytest 38건, frontend lint/build 통과. 실제 브라우저·HTTP Clipboard fallback 점검은 IP06 사용자 검증으로 남김 |
| IP05 | 후속 수정·취소·격리 통합 검증 | IP03, IP04 | 완료 — 2026-10-25 pytest 40건, frontend lint/build 통과. 실제 브라우저·gate 동작은 IP06 사용자 검증으로 남김 |
| IP06 | 사용자 실행용 gate 점검·배포 안내 | IP05 | 완료 — 2026-10-25 pytest 40건, frontend lint/build 통과. 실제 BC-250 gate·브라우저·image_priority 점검은 사용자 실행으로 남김 |

## IP01 — 출력 계약과 검증기

시작 파일: backend/app/prompting.py, config.py, tests/test_prompting.py. 필요 시 backend/app/image_prompt.py 신설.

작업:
- 대상 모델 상수, gate 전달 스키마, 서버 검증 함수를 한 모듈에 모은다. 두 검증 계약이 달라지지 않도록 한다.
- 정확히 한 JSON 객체만 허용한다. Markdown fence, 앞뒤 설명, 배열, 추가 필드, 중복 JSON 키, NaN/Infinity를 거절한다.
- 파싱 오류와 필드 검증 오류를 내부적으로 구분하고 외부에는 안전한 오류 코드·안내만 제공한다.
- IMAGE_PROMPT_MAX_TOKENS=512 서버 설정을 정의하고 양수·전역 출력 상한과의 관계를 검증한다. 브라우저에는 gate 정보나 키를 넣지 않는다.

완료 조건: 정상 결과, 공백 prompt, 누락/추가 필드, 잘못된 enum, boolean/문자열 숫자, 중복 키·비표준 숫자·잘린 JSON에 대한 검증 테스트 통과. 실제 모델 호출 없음.

## IP02 — 전용 시스템 프롬프트와 기존 설정 호환

시작 파일: backend/app/prompting.py, main.py의 mode_settings 검증, tests/test_prompting.py.

작업:
- 사용자가 제공한 테스트의 규칙을 전용 시스템 지시로 구현한다. 영어 prompt, 정확한 필드, steps 기본 6, cfg_scale 1.0, Markdown·설명·negative_prompt 금지를 명시한다.
- subject/composition/style/lighting/palette와 현재 요청을 반영하고 최근 완료 턴을 유지한다. 후속 수정은 전체 JSON을 새로 반환하도록 지시한다.
- 기존 output_language 값은 DB에 보존하되 새 출력에서는 영어 고정 정책을 적용한다. 기존 negative_prompt 값은 제외 요소 작성 지시로만 사용하고 결과 필드로 반환하지 않는다.
- 기존 이미지 대화·메시지는 삭제하거나 일괄 재작성하지 않는다. 예전 자연어 답변도 히스토리로 읽을 수 있게 한다.
- image_prompt 출력 예약 예산을 전용 설정과 연결하고 UI 응답 길이 값은 이 모드에서 사용하지 않는다. quick/roleplay 동작은 유지한다.
- 이미지 모드는 자동 장기 기억 요약을 실행하지 않는다.

완료 조건: 고정 출력 지시, 기존 설정 호환, 최근 완료 턴·현재 질문 1회 포함, 입력 예산, 다른 모드 회귀 테스트 통과.

## IP03 — gate 호출과 생성 수명주기

시작 파일: backend/app/main.py, image_prompt.py, tests/test_api.py.

작업:
- 기존 messages API를 재사용하고 image_prompt 전용 non-streaming 경로를 구현한다. 이미지 모드에 stream:true가 오면 생성 레코드를 만들기 전에 일관된 422 image_prompt_stream_unsupported를 반환한다.
- 활성 LLM model 조회 후 gate에 스키마와 고정 temperature·추론 OFF를 전달한다. image_priority·기존 동시성 제한을 공유한다.
- 응답 finish_reason=length는 JSON이 파싱되어도 image_prompt_output_truncated로 실패 처리한다. 빈 content, 잘못된 JSON, 스키마 불일치, timeout, upstream 거절을 구분한다. upstream 원문 오류는 노출하지 않는다.
- 검증 성공 시 정규화한 JSON 문자열을 기존 assistant content에 저장한다. DB의 별도 결과 사본은 만들지 않는다. 응답의 assistant_message는 기존 계약을 유지한다.
- 검증 실패는 generation failed, 사용자 질문은 한 번만 저장, 같은 request_id로 재시도 가능하게 한다. 완료 요청 재전송은 재호출 없이 저장 결과를 반환한다.
- gate 응답 직전 취소·대화 삭제에도 assistant 저장이 발생하지 않도록 최종 상태를 확인한다. 클라이언트 연결 종료와 GPU 중단 보장은 구분한다.

완료 조건: mock gate로 payload 검증, 성공 저장, 실패 후 재시도, 완료 재전송, length·빈 응답·스키마 위반·gate 오류·취소·삭제 처리 테스트 통과. reasoning_content는 어떤 응답에도 포함되지 않는다.

## IP04 — 이미지 모드 화면

시작 파일: frontend/src/api.ts, main.tsx, components/MessageList.tsx, style.css. 필요 시 ImagePromptResult.tsx 신설.

작업:
- 이미지 모드는 일반 JSON POST, 다른 모드는 기존 SSE로 분기한다. AbortSignal을 일반 요청에도 연결하고 기존 요청 ID·취소·재시도 구조를 재사용한다.
- 생성 중 '프롬프트 작성 중'과 gate 대기 가능성을 표시한다. 검증된 결과를 한 번에 표시하고 부분 JSON은 표시하지 않는다.
- 이미지 모드에서 출력 언어 선택과 응답 길이 선택을 숨긴다. 영어 출력 고정·프롬프트 작성 전용임을 설명한다. 기존 제외 요소 입력은 유지하되 결과 negative_prompt 필드로 보내지 않음을 구현에 반영한다.
- assistant content가 유효한 계약 JSON이면 영어 프롬프트와 해상도·steps·CFG·대상 모델을 읽기 쉽게 표시한다. 예전 자연어 메시지는 기존 안전 Markdown으로 표시한다.
- '프롬프트 복사'는 prompt만, '설정 JSON 복사'는 전체 객체를 복사한다. HTTP 환경에서 Clipboard API 부재/실패 시 선택 가능한 텍스트와 수동 복사 안내를 제공한다. 성공 전에는 복사 완료를 표시하지 않는다.
- 기존 가상 목록·자동 스크롤·대화 전환 격리를 유지한다. 실제 이미지 생성 버튼은 추가하지 않는다.

완료 조건: lint/build 통과. 정상 JSON/기존 자연어/로딩/실패/재시도 표시와 HTTP 복사 fallback을 확인한다. 브라우저에서 실행하지 못한 확인은 명시한다.

## IP05 — 후속 수정과 모드 간 회귀

시작 파일: tests/test_api.py, tests/test_prompting.py, 기존 frontend 검증 도구.

작업:
- 첫 요청으로 JSON을 저장한 뒤 '조명을 더 어둡게', '세로 구도로 변경' 요청에 이전 완료 결과가 정확히 전달되는지 mock으로 검증한다. 모델의 실제 품질 검증과 구분한다.
- 실패/취소된 시도 제외, 다른 대화 격리, 현재 질문 중복 방지, 입력 예산 유지와 이미지 모드 자동 요약 미실행을 확인한다.
- 생성 중 다른 방으로 이동·취소·동일 요청 재시도·페이지 재조회 시 결과와 요청 ID가 섞이지 않는지 점검한다.
- quick/roleplay SSE·Markdown·페이지 조회의 기존 회귀 테스트를 실행한다. 새 계약에 직접 관련된 검증만 추가한다.

완료 조건: 전체 backend 테스트와 frontend lint/build 통과. 실제 브라우저와 모델에서 미검증인 항목을 목록으로 남긴다.

## IP06 — 사용자 실행용 실서버 점검과 배포 안내

시작 파일: README.md, .env.example, deploy/bc250-llm-client.service. 필요 시 scripts/check_image_prompt.py 신설.

작업:
- 기존 테스트를 gate 경유 점검 스크립트로 정리한다. URL·키는 환경변수에서 읽고 /v1/models로 활성 LLM을 조회한다. 8080이나 Studio를 직접 호출하지 않는다.
- 결과 요약은 status, finish_reason, elapsed, usage, 검증 성공 여부만 출력한다. reasoning 원문·키·헤더·민감 프롬프트는 출력하지 않는다. 화면 품질 검토는 앱에서 수행한다.
- 모델 응답 정상, JSON 규칙, 후속 수정, gate 스키마 지원 여부, 이미지 우선 대기를 사용자 실행 체크로 제공한다. 운영 이미지 작업을 테스트 스크립트가 시작하지 않는다.
- 새 env 설정과 frontend/backend 동시 배포 필요성을 문서화한다. 사용자가 실행할 테스트·패키징·SCP(-P 2222)·백업·재시작·롤백 명령을 제공한다.
- 이전 203/EXEC 재발을 막도록 가상환경은 최종 경로에서 생성하거나, 고정 경로 python -m uvicorn 실행 및 가상환경 경로 전략을 일관되게 설계한다. 임시 경로에서 생성한 venv를 옮긴 뒤 기존 shebang을 그대로 쓰는 절차는 제공하지 않는다.
- Caddy 테스트는 Host:192.0.2.10을 지정한다. 기존 Art Studio 설정은 변경하지 않는다. SQLite 백업은 서비스 정지 후 수행하고 롤백 데이터 손실 범위를 설명한다.

완료 조건: 로컬 검증 결과, 사용자 실행 명령, 실제 서버 미검증 목록이 준비되어 있다. 실제 배포는 수행하지 않았음을 기록한다.

## Terra Medium 실행 프롬프트

```text
docs/plans/image-prompt-tasks.md를 읽고 IP01부터 한 태스크씩 구현해줘.
태스크 시작 전에 현재 코드와 의존 태스크 완료 상태를 확인하고, 기존 사용자 변경을 보존해.
이미지 모드는 z-image-nsfw-v2용 영어 프롬프트와 설정 JSON만 작성하며 실제 이미지는 생성하지 않아.
모든 LLM 호출은 기존 gate를 경유하고 단일 APP_API_KEY 인증과 reasoning 비노출 정책을 유지해.
각 태스크마다 관련 검증을 수행하고 문서에 결과·미검증 항목을 기록한 뒤 보고해.
기존 quick/roleplay 스트리밍 및 대화 저장·재시도·취소·페이지 조회를 유지해.
실제 SCP/SSH 배포나 모델·gate·Studio·Caddy 운영 설정 변경은 하지 마.
```
