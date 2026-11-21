# 현재 클라이언트 구성

## 서비스 연결

```text
Browser ─ Caddy /llm/ ─ React 정적 파일
          └ /llm/api/* ─ FastAPI 127.0.0.1:8010
                         ├ LLM gate 127.0.0.1:8081 ─ llama-server 8080
                         ├ Studio 127.0.0.1:8090
                         ├ SQLite
                         └ Unix socket ─ model-control worker
```

브라우저의 API 기본 경로는 `/llm/api/v1`이다. Caddy는 `/llm/api/*`를 FastAPI로 전달하고 나머지 `/llm/*`에는 SPA fallback을 제공한다. 외부 모델·이미지 요청은 서버 어댑터를 통하며 API 키는 서버에만 둔다. `WIKI_STORE=legacy_external`일 때는 외부 Wiki HTTP 어댑터도 사용한다.

## 기능과 코드 위치

| 구성 | 역할 | 소스 |
|---|---|---|
| API | 대화, 생성, 요약, Wiki, 이미지, 모델 제어 라우트 | `backend/app/main.py` |
| 설정·저장소 | 환경 변수, SQLite 초기화와 migration | `backend/app/config.py`, `db.py` |
| 채팅 입력 | 모드별 지시, 예산과 완료 턴 처리 | `backend/app/prompting.py` |
| 이미지 | 의도 분석, 프롬프트 검증, Studio 어댑터, 작업 저장 | `image_intent.py`, `image_prompt.py`, `imagegen.py`, `image_jobs.py` |
| Wiki | 문서 CRUD·revision·검색·별칭·주소와 근거 | `wiki_repository.py`, `wiki_query.py`, `wiki_paths.py`, `wiki_evidence.py` |
| 기억 | 공유 속성 매핑, 작업·출처·충돌·활성 기억 | `integrated_memory.py`, `memory_repository.py`, `wiki_memory.py` |
| 모델 제어 | 공용 worker·CLI·gate 조율·복구 | `backend/app/model_control/`, `gate_candidate/` |
| 화면 | 대화·설정·라우팅과 기능별 컴포넌트 | `frontend/src/main.tsx`, `frontend/src/components/` |

## 데이터

SQLite 경로는 `DATABASE_PATH`로 지정하며 기본값은 `./data/bc250-llm-client.db`다. 초기화 시 schema migration을 실행한다. 현재 스키마 버전은 24다.

- 대화와 사용자/assistant 원문, 생성 요청 상태·usage·종료 사유를 저장한다.
- 대화 요약 본문, 요약 범위 커서, 수정 버전, 요약 작업과 실행 시도 횟수를 저장한다.
- 이미지 초안·작업·실행 정보·평가를 저장하고 이미지 자산은 Studio를 통해 제공한다.
- 통합 Wiki 문서·revision·FTS·별칭·주소, 기억의 출처·상태·작업 결과를 저장한다.
- `legacy_external`과 `integrated`의 문서 참조는 저장소 식별자로 구분한다. 숫자 문서 ID만으로 두 저장소를 연결하지 않는다.

assistant의 최종 content를 저장·표시하며 reasoning 내용은 노출하지 않는다. 대화·Wiki 데이터에는 계정별 접근 격리가 없다.

## 인증

`APP_AUTH_MODE=key`는 앱 API 키를 사용한다. `local` 모드는 `APP_ALLOWED_HOSTS`, 동일 출처와 전용 요청 헤더를 검사하며 `CORS_ORIGINS`는 비워야 한다. local 모드는 사용자 인증이 아니므로 앱에 접근 가능한 기기는 공유 대화·문서와 일반 앱 권한의 기능을 사용할 수 있다.

게이트 pause/resume는 앱 인증 외에 `OPERATOR_KEY`와 `X-BC250-Operator-Key`가 필요하다. 빈 운영자 키는 해당 변경 기능을 비활성화한다. 모델 전환 API는 일반 앱 인증을 사용한다. 전환 대상 모델은 서버 공용이며 대화별로 별도 모델을 고정하지 않는다.

## 주요 기본 설정

| 설정 | 기본값 | 의미 |
|---|---|---|
| `WIKI_STORE` | `legacy_external` | 내부 Wiki는 `integrated`로 선택 |
| `MODEL_CONTROL_ENABLED` | false | 모델 제어 worker 사용 여부 |
| `IMAGEGEN_ENABLED` | false | Studio 이미지 작업 제출 여부 |
| `LLM_DISABLE_REASONING` | true | 일반 채팅의 thinking 비활성 요청 |
| `MAX_CONCURRENT_GENERATIONS` | 2 | 프로세스 내부 생성 세마포어 |
| `LLM_CONTEXT_TOKENS` | 16384 | 요청 컨텍스트 설정 상한 |
| `MAX_COMPLETION_TOKENS` | 2048 | 일반 대화 출력 상한 |
| `MAX_REQUEST_BYTES` | 65536 | 요청 크기 제한 |

모델 제어 활성 시 `LLM_MODEL_ID`를 비워야 한다. 비활성 경로에서는 gate 모델 목록과 일치하는 고정 ID를 설정할 수 있다. 실제 컨텍스트와 기능 지원은 [모델 제어](model-control.md), 상세 설정은 [환경 변수 예시](../.env.example)를 따른다.
