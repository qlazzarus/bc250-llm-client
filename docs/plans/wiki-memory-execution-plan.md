# Wiki RAG·선별 자동 기억·UI 실행 계획

작성일: 2026-11-12. 실행 대상: **Terra / medium**. 상태: **계획만 작성, 모든 구현 Task 미착수**.

이 문서는 한 세션에서 전체 개편을 수행하지 않고, 아래 Task 하나씩 구현·검증·기록하기 위한 실행 기준이다. 모델 설정은 실행자가 Terra / medium으로 선택한다. 이 문서 작성 시 모델 전환이나 구현 실행은 하지 않았다.

## 1. 범위와 작업 제한

- 기존 Client FastAPI, Wiki, Client SQLite, llama.cpp와 gate를 재사용한다.
- 1차 구현: 질문 원문 저장 방지, 혼합 발언 선별, 기억 UI 축소, 입력 영역·스크롤 회귀 해결.
- 후속 구현: 중복·충돌·갱신, Wiki 쓰기 계약, RAG 역할 분리, 출처 메타데이터, 작업 비용 제한.
- 새 서비스·새 DB 파일·상주 모델·벡터 DB·임베딩 서비스를 도입하지 않는다.
- 기존 문서·메시지·자동 기억을 삭제·재분류·수정하는 정리 작업은 포함하지 않는다. 스크린샷의 문서 2도 수정하지 않는다.
- 운영 DB 마이그레이션, 테스트 데이터의 운영 저장, 서비스 재시작, 패키지 설치, 배포, GPU 모델 전환은 이 계획 실행의 기본 권한에 포함하지 않는다. 단, 사용자가 2026-11-12에 승인한 테스트 의존성은 프로젝트 전용 [`.venv/`](../../.venv/)에만 설치했다.
- 현재 요청은 **문서 작성만**이다. 아래 구현·격리 테스트는 사용자가 후속 실행을 요청할 때 진행한다.
- 후속 구현 테스트는 임시 SQLite·fake Wiki·mock gate를 사용한다. 앱 import/lifespan이 실제 DB 초기화나 자동 마이그레이션을 유발하지 않도록 설정을 먼저 격리한다.
- 패키지/브라우저 실행 환경이 없으면 설치하지 말고 해당 검증을 미실행으로 기록한다.
- 자동 배포 workflow가 있으므로 문서/코드 작성이 push·배포 권한을 의미하지 않는다.
- sub-agent 실행을 요구하지 않는다. Task를 순차 실행하며 관련 없는 모델 제어·Studio 코드 전체를 탐색하지 않는다.

## 2. 근거와 미확인 사항

### 2.1 확인된 Client 경로

| 기능 | 근거 | 현재 동작 |
| --- | --- | --- |
| Wiki HTTP | [wiki.py](../../backend/app/wiki.py) | loopback 8020, 8초 제한, 404 외 오류 대부분 503으로 변환 |
| RAG | [main.py](../../backend/app/main.py), [prompting.py](../../backend/app/prompting.py) | 일반 대화에서 문서 3개·각 1,000자 발췌를 사용자 질문에 결합 |
| 기억 작업 | [main.py](../../backend/app/main.py) | 완료 답변 뒤 자동 ON 또는 명시 요청이면 작업 생성, 검색 5개 후 추가 LLM 판단 |
| 원문 검증 | [wiki_memory.py](../../backend/app/wiki_memory.py) | 사용자 발언 부분 문자열인지 검증. 사실/질문 판별은 모델 지시에 의존 |
| 중복/갱신 | [main.py](../../backend/app/main.py), [wiki_memory.py](../../backend/app/wiki_memory.py) | append 대상 본문의 원문 포함 검사, old_text 1회 일치 교체. create의 전역 내용 중복 검사는 없음 |
| 저장 이력 | [db.py](../../backend/app/db.py) | 기존 SQLite에 wiki_memory_jobs. generation_id 기본 키, 변경 전후 본문 보관, 대화 삭제에 따른 cascade |
| UI | [main.tsx](../../frontend/src/main.tsx) | 자동 기억 기본 ON, 5초 polling, 최신 상태에 원문 포함, 입력 패널 안 상세 기록 |
| 레이아웃 | [scroll.css](../../frontend/src/scroll.css), [MessageList.tsx](../../frontend/src/components/MessageList.tsx) | composer-panel flex-shrink:0, 높이 제한 없는 기억 상세, sticky 최신 이동 버튼 |
| gate | [main.py](../../backend/app/main.py), [전환 회귀](../../tests/test_background_switch_deferral.py) | 모델별 context/capability/epoch 확인, 확실한 admission 거절만 재준비 |

현재 흐름: UI → Client → Wiki 검색 → 제한 발췌 → gate/현재 모델 → 답변 저장 → 기억 작업 → Wiki 검색 → gate 추가 판단 → 원문 검증 → Wiki 쓰기 → 작업 이력 polling.

보존할 계약:

- 일반 대화만 대상. roleplay/image_prompt에 자동 확장하지 않는다.
- Wiki 검색과 자동 기억은 독립 토글이며 localStorage 값을 유지한다.
- 자동 OFF에서도 명시적인 기억 요청은 처리한다. 단, 명시 요청은 질문/모호함/저장 거부를 무조건 저장하라는 뜻이 아니다.
- assistant 텍스트는 저장 근거가 아니다. 현재 또는 허용된 이전 사용자 발언의 출처를 검증한다.
- 쓰기 결과 unknown은 자동 재전송하지 않는다.
- 기존 CRUD·draft·wiki-memory 라우트와 기본 응답 형태를 유지한다.

### 2.2 문서 근거와 한계

- [기존 Wiki 연동 계획](wiki-integration.md), [검색 평가](wiki-search-evaluation.md), [README](../../README.md)는 별도 Wiki의 SQLite·FTS·단어/부분 문자열 검색을 설명한다. 실제 Wiki 스키마·tokenizer·SQL·트랜잭션은 미확인이다.
- 별도 Wiki·Studio 소스는 현재 분석 환경에서 확인하지 못했다. W00 조사 결과 Wiki의 예상 운영 경로는 확인했지만 소스는 이 환경에 없다. Wiki 수정 파일명은 실제 소스가 제공될 때까지 확정하지 않는다.
- 사용자가 전달한 두 첨부 작업 문서 경로는 이 환경에 없어 읽지 못했다. 대신 저장소의 [모델 전환 작업 문서](llm-model-switch-tasks.md)를 참고했다. 첨부본과 동일하다고 간주하지 않는다.
- 모델 전환 현재 단계는 [통합 계획](llm-model-switch-plan.md)이 기준이다. 과거 날짜의 중간 상태를 현재 운영 상태로 해석하지 않는다.
- 독립 Wiki의 직접 LLM 호출은 기존 계획에서 예외로 기록됐다. 이번 작업으로 해당 경로를 임의 변경하지 않는다.
- 운영 DB, 실제 기억 저장 내용, 현재 모델·서비스 상태는 이번에 조회하지 않았다.

### 2.3 W00 외부 Wiki 계약 조사 결과 — 2026-11-12

조사는 읽기 전용으로만 수행했다. 이 작업 공간, 접근 가능한 `/opt`, 인접 workspace와 Git의 모든 참조를 조사했으며, Family Wiki의 실제 소스·DB 파일·배포 설정은 발견하지 못했다. 현재 컨테이너에는 `/opt` 자체가 없다. 따라서 아래의 `확인됨`은 Client 코드 또는 과거의 **운영 읽기 전용 조사 기록**에서 확인한 사실이고, Wiki 서버 소스의 구현 사실은 아니다.

| 항목 | 확인 결과 | 근거·한계 |
| --- | --- | --- |
| 예상 소스 경로 | `/opt/bc250-family-wiki/app/main.py` | [모델 전환 작업 문서](llm-model-switch-tasks.md)의 과거 운영 조사. 이 환경에는 해당 경로가 없어 재확인·수정 불가 |
| Wiki LLM 경로 | `main.py`의 `ask`가 `127.0.0.1:8080`을 사용한 것으로 기록됨 | 위 과거 조사 기록. 현재 파일을 읽지 못했으므로 함수의 전체 계약·호출 조건은 미확인 |
| Client가 사용하는 API | `GET /api/search?q=&limit=`, `GET/POST /api/documents`, `GET/PUT/DELETE /api/documents/{id}` | [Client Wiki 어댑터](../../backend/app/wiki.py)와 [Client 라우트](../../backend/app/main.py). 응답은 최소 `id`, `content`, `title`, `updated_at`로 사용됨 |
| 조건부 갱신 | 지원하지 않는다고 문서화됨 | [README](../../README.md)와 [기존 Wiki 계획](wiki-integration.md). 실제 PUT 트랜잭션은 미확인 |
| SQLite·FTS·검색 방식 | SQLite/FTS, 단어·부분 문자열 검색이라는 문서 설명만 존재 | [기존 Wiki 계획](wiki-integration.md), [README](../../README.md). DB 경로, DDL, FTS tokenizer, 검색 SQL, ranking은 미확인 |
| 실제 수정 파일 | 미확정 | Wiki 라우터·repository·schema/migration·테스트의 실제 경로가 없음 |

W12~W14를 시작하려면 다음 중 하나가 필요하다.

1. Wiki 저장소의 읽기 가능한 작업 경로 또는 별도 workspace 연결
2. 해당 저장소의 revision이 고정된 읽기 전용 소스 스냅샷
3. Wiki 서비스의 라우터, SQLite schema/FTS 설정, 검색/CRUD transaction, 테스트 위치를 포함한 코드 묶음

운영 Wiki API에 추측성 요청을 보내 계약을 역추론하지 않는다. 특히 POST·PUT·DELETE와 `/api/ask`는 호출하지 않는다. 운영 DB 파일을 직접 열거나 Client의 `initialize`/연결 헬퍼를 실행하지 않았다.

## 3. 사용자 스크린샷: 회귀 시나리오

### 3.1 직접 관찰한 현상

1. `iphone 12 pro max 가 어떤 기종이야?`가 문서 2의 저장 본문으로 표시된다.
2. `지금 쓰는 iphone 12 pro max 에서 galaxy 로 바꾸려고 하는데 추천해줘` 전체가 같은 문서의 저장할 발언으로 표시된다. 변경 전 본문에는 1번 질문이 보인다.
3. 입력 패널에 최신 원문, 여러 작업, 변경 전·저장 시도 본문이 반복돼 대화 외 영역이 크게 늘어난다.
4. `최신 메시지로` 버튼이 답변 문장 위에 겹쳐 보인다.
5. 추천 표는 좁은 열에서 여러 줄로 줄바꿈된다. 스크린샷만으로 실제 가로 overflow나 모든 뷰포트의 입력창 가림을 확정하지 않는다.

1~2는 UI가 표시한 저장 결과의 증거이며 운영 DB 직접 검증은 아니다. 3~4의 레이아웃 원인은 위 CSS와 연관될 가능성이 있으나 브라우저 재현으로 확정한다. 스크린샷 속 제품 추천의 최신성은 별도 문제이며 이 작업에 웹 검색 서비스 추가나 추천 모델 목록 수정은 포함하지 않는다.

### 3.2 기대 결과

| ID | 입력/상황 | 기대 결과 |
| --- | --- | --- |
| R01 | `iphone 12 pro max 가 어떤 기종이야?` | 질문으로 skip. 명확한 사전 제외라면 기억용 추론·Wiki 쓰기 0회 |
| R02 | `지금 쓰는 iphone 12 pro max 에서 galaxy 로 바꾸려고 하는데 추천해줘` | 전체 문장 저장 금지. 사용자 주체가 확인되면 현재 기기 사실만 후보. 교체 의향은 확정 계획으로 저장하지 않음. 분리/주체 확인이 어려우면 보류 |
| R03 | `나는 iPhone 12 Pro Max를 사용해. 이걸 기억해 줘` | 사실 원문 절과 출처를 검증한 뒤 후보 처리. 명령어 부분은 저장 본문에서 제외 |
| R04 | `기억하지 마. 비밀번호는 ...` | 명시 요청 정규식보다 저장 거부 우선, 저장하지 않음 |
| R05 | `나는 그 기기를 안 써` | 부정 제거/반전 금지. 기존 사실과의 관계 불명확 시 보류 |
| R06 | `그거 기억해 줘` / assistant에만 존재하는 사실 | 이전 사용자 근거가 하나로 확정되지 않으면 보류 |
| R07 | 같은 사실 반복 / 표현만 변경 | 정확 중복 무쓰기, 의미 중복 검증 실패 시 보류. 별도 주체/시점은 병합 금지 |
| R08 | `11월 20일에 교체할 계획이야` | 명시된 계획으로 분류. 연도/기준일이 모호하면 임의 날짜 확정 금지 |
| R09 | 원문·전후 본문이 긴 작업 30개 | 기본 입력 패널에는 짧은 상태와 상세 진입만 표시. 입력/전송 접근 가능 |
| R10 | 긴 표, 스트리밍, 과거 메시지 추가 로딩 | 최신 이동 버튼이 마지막 본문을 가리지 않음. 사용자가 위로 읽는 동안 강제 이동 금지 |
| R11 | 자동 OFF + 명시 요청, 중복, 실패, unknown | 각각 명확한 결과 표시. unknown을 완료/재시도 가능으로 표시하지 않음 |
| R12 | 대화 A 요청 지연 중 B 전환, 조회 실패 | A 데이터가 B에 표시되지 않음. 실패 시 이전 성공 목록 보존+갱신 실패 안내 |

## 4. 목표 계약: 구현 중 임의 확장 금지

### 4.1 기억 판정

저비용 제외 → 후보 분류/추출 1회 → 서버 검증 → 중복/충돌 검사 → 저장 또는 보류.

- 분류: fact, preference, plan, question, uncertain.
- 저장 근거: source_message_id와 exact source span. 저장 표현과 원문 근거는 별개 필드로 취급한다.
- 첫 단계의 저장 표현은 **검증된 최소 원문 절**을 우선한다. 자유 재서술을 허용해 환각 검증 문제를 키우지 않는다.
- 서버는 `kind=fact`라는 모델 출력만 신뢰하지 않는다. 질문/추천 지시 포함, 부정 손실, 출처 불일치 등 금지 조건을 재검사한다. 규칙만으로 자연어 의미를 완벽하게 보장한다고 주장하지 않는다.
- 물음표 하나로 메시지 전체를 폐기하지 않는다. 한국어 어미·의문형·혼합 발언과 영문 질문을 평가한다.
- 한 메시지에 여러 후보가 있으면 초기에는 명확한 후보 최대 1개만 처리한다. 복수 후보 batch는 별도 요구가 생길 때 설계한다.
- 이유 코드: question, not_durable, duplicate, ambiguous, conflict, unsupported_model, rejected_sensitive, write_unknown 등. 기존 status와 분리한다.
- UI 호환을 위해 기존 pending/running/writing/completed/skipped/failed/unknown 상태를 유지한다. 보류는 skipped+reason_code+needs_attention으로 표현할 수 있다. 신규 상태 추가가 필요하면 계약 변경을 먼저 기록한다.
- 공유 앱 접근을 개인 신원으로 해석하지 않는다. 인증된 개인 식별이 없으면 공유 scope를 명시하고, `나`의 주체를 확정할 수 없는 자동 개인 기억은 보류한다. category/title만으로 소유자를 추정하지 않는다.

### 4.2 저장과 갱신

- Wiki는 본문 저장소, 기존 Client SQLite는 기억 분류·출처·fingerprint·활성 관계·작업 조정 저장소다.
- 기존 wiki_memory_jobs는 작업 생명주기로 유지한다. 내용 비교와 활성 기억에 필요한 보조 테이블은 기존 DB 안에 추가한다. 새 DB 파일은 만들지 않는다.
- 후보 스키마: memory_records(scope, subject_key, predicate_key, value, kind, source refs, fingerprint, wiki_document_id, wiki_revision, state, supersedes_id, timestamps). 최종 필드/인덱스는 W08의 계약 산출물로 확정한다.
- fingerprint는 안전한 공백/Unicode 정규화까지만 한다. 부정, 수치, 단위, 날짜를 제거하지 않는다. semantic key와 value 비교를 별도로 한다.
- 정확 중복은 DB 유일성 제약과 작업 claim으로 방지한다. 유사도/LLM 판단만으로 자동 합치지 않는다.
- 갱신은 새 버전 기록 + supersedes 관계를 기본으로 한다. Wiki 성공 확인 후 기존 DB의 단일 트랜잭션에서 활성 관계를 전환한다. 기존 문서 본문은 자동으로 덮어쓰지 않는다.
- 이전 버전이 일반 Wiki 검색 경로로 재주입되지 않게 문서 ID를 기준으로 제외한다. 기존 문서는 legacy 자료로 유지하며 자동 개인 기억으로 승격하지 않는다.
- 외부 Wiki 편집/삭제가 감지되면 revision 불일치를 보류하고 메타데이터를 최신이라고 단정하지 않는다.
- 대화 삭제 시 source 연결 해제/원문 보존/개인정보 처리 정책을 W08에서 명시한다. 조용한 cascade로 활성 기억 인덱스가 사라지거나 원문을 무조건 영구 복제하는 구현을 피한다.

### 4.3 API와 출처

- 기존 문서 CRUD, draft, wiki-memory 기본 요청/응답 유지.
- 경량 이력: 기존 history에 선택적 `view=summary` 도입, 상세 조회는 conversation+generation ID로 제한. 기본값은 기존 응답 유지.
- Wiki 쓰기: 서버 트랜잭션의 revision/CAS + operation ID 멱등성 + operation 결과 조회. Client GET→PUT만으로 원자성 달성을 주장하지 않는다.
- 같은 operation ID/같은 body는 같은 결과, 같은 ID/다른 body는 409. 실패 코드와 결과 불명은 구별한다.
- 기존 클라이언트의 조건 없는 수동 갱신 호환은 유지하되, 그것까지 lost update 방지가 보장된다고 하지 않는다. 새 자동 경로에는 검증된 계약이 필수다.
- Wiki 검색의 빈 결과와 응답 형식 오류를 구분하고, HTTP 404/409/422/503을 의미에 맞게 전달한다.
- 출처 메타데이터: retrieved / provided / cited, 문서 ID·버전·발췌/hash·인용 검증. cited는 근거 진실성 검증 완료를 의미하지 않는다.
- 새 답변부터 본문과 출처를 분리한다. 구형 클라이언트의 본문 부착 계약은 명시적인 opt-in 응답 모드로 유지하고, 기존 저장 메시지는 일괄 재작성하지 않는다.

### 4.4 UI·자원

- 기본 상태는 짧게: `기억 저장됨`, `기억 갱신됨`, `확인 필요`. 원문은 상세에서만 노출.
- 자동 question/duplicate skip은 조용히 처리. 명시 요청의 duplicate는 `이미 기억하고 있습니다`로 표시하고, conflict/unknown은 상세 진입을 제공한다.
- 기억 상세는 입력 패널 안에 무한 확장하지 않는다. 높이가 제한된 접근 가능한 dialog/drawer로 분리하는 방식을 기본안으로 한다.
- 모바일 360×640·390×844, 스크린샷과 유사한 738px 너비, desktop 1280×800에서 확인한다. 가상 키보드는 실기기 검증 여부를 별도로 기록한다.
- 최신 이동 버튼은 본문 가림 없는 영역/여백을 확보하고 키보드 포커스·스크롤 앵커·virtualizer 측정을 보존한다. 표 가로 스크롤은 메시지 영역 안에서만 허용한다.
- 신규 모델 로드 금지. 현재 모델로 후보당 추론 기본 1회, 비후보 0회, 기억 실행 1개, 큐/대기 상한.
- 기존 gate epoch/capability/실측 토큰 검증 유지. 전체 context를 무조건 16K로 가정하지 않는다.
- Wiki+기억의 합산 초기 발췌 예산 후보는 유효 입력 예산의 15~20%, 최대 약 2K tokens. 측정 전 확정 성능값으로 쓰지 않는다.
- foreground 우선은 시작 전 대기로 구현하고 이미 전달한 추론은 취소 후 재생하지 않는다. 큐 초과 시 작업을 조용히 유실하지 않고 이유를 기록한다.

## 5. Task 실행 규칙

1. 시작 시 git diff와 대상 파일만 읽고 기존 사용자 변경을 보존한다.
2. 해당 Task의 의존 Task가 완료인지 확인한다. 미완료 외부 의존은 추정으로 건너뛰지 않는다.
3. 한 Task에서 아래 수정 범위를 지킨다. 새 모듈은 책임 분리를 위해 필요한 경우만 추가하고 근거를 기록한다.
4. 먼저 명시된 회귀를 작성/확인한 뒤 구현한다. 단순 텍스트/CSS 변경에 구현을 그대로 복제한 테스트를 추가하지 않는다.
5. 관련 테스트 → 필요 시 통합 회귀 순서. 운영 endpoint/DB는 호출하지 않는다.
6. 완료 후 §8 실행 이력에 수정 파일, 정확한 검증 명령·결과, 미검증, 다음 Task를 기록한다. 테스트 미실행을 통과로 적지 않는다.
7. 새로운 계약 판단이 필요하면 그 Task의 초안을 남기고 영향 범위를 보고한다. 의존 없는 다음 Task는 진행할 수 있다.
8. 구현 완료는 배포 완료가 아니다. commit/push는 별도 요청이 없으면 하지 않는다.

## 6. 작은 실행 Task

상태 표기: `[ ] 미착수`, `[-] 진행/차단`, `[x] 완료`. 아래 모두 미착수다.

### 1차: 질문 저장 방지와 화면 문제

#### W00 [x] 외부 Wiki 계약 읽기 전용 조사

- 의존: 없음. 다른 1차 Task를 막지 않는다.
- 대상: [wiki-integration.md](wiki-integration.md), 이 문서 §2; 실제 Wiki 소스 위치 확보 후 해당 라우터·저장·검색 파일만 조사.
- 작업: routes, DB 경로/DDL, FTS/tokenizer/검색 SQL, CRUD transaction, revision/멱등성 가능 여부를 근거와 함께 기록. DB를 열어야 한다면 승인된 읽기 전용 절차만 사용하고 initialize/connect 헬퍼를 실행하지 않는다.
- 결과: Client가 기대하는 API와 과거 운영 조사의 예상 경로만 확인했다. 실제 Wiki 소스/DB/배포 설정은 이 환경에 없고 `/opt`도 존재하지 않아 수정 파일·schema·transaction은 확정할 수 없다. §2.3에 근거와 필요한 입력을 기록했다.
- 차단 영향: W12~W14는 계속 미착수다. W01~W07은 Wiki 서버 소스 없이 진행할 수 있다.
- 검증: 작업 공간·접근 가능한 `/opt`·인접 workspace·Git 참조를 읽기 전용으로 조사했다. 실서비스 endpoint·DB·상태 변경 경로는 호출하지 않았다.

#### W01 [x] 스크린샷과 기존 계약 회귀 고정

- 의존: 없음.
- 대상: [test_wiki_memory.py](../../tests/test_wiki_memory.py), [test_wiki.py](../../tests/test_wiki.py), [test_api.py](../../tests/test_api.py); 필요 시 신규 `tests/test_wiki_memory_policy.py`.
- 작업: R01~R08을 테스트 케이스로 분류. 현행 질문 원문 통과를 재현하는 증거와 목표 동작을 분리. fake 응답과 임시 DB만 사용.
- 완료: 현행 보호 조건(원문/role/unknown/일반 모드/명시 요청)과 개선 목표를 기록. 미구현 목표를 무조건 통과시키거나 blanket xfail로 숨기지 않음.
- 검증: 기존 테스트 통과; 실패해야 하는 현행 재현은 별도 명령/결과로 명시하고 W02~W04에서 해결.
- 결과: [test_wiki_memory.py](../../tests/test_wiki_memory.py)에 R01/R02의 현행 경로를 fake Wiki·mock LLM으로 재현하는 `test_current_policy_reproduces_screenshot_nonfact_write`를 추가했다. 모델이 두 질문/추천 요청의 전체 원문을 `create`로 선택하면 현행 서버는 Wiki POST까지 진행함을 고정한다. 이 기준 테스트는 결함을 보존하는 회귀가 아니며 W02/W03에서 거절 또는 최소 사실 절 추출 테스트로 교체해야 한다. 기존 원문/role/unknown/quick/명시 요청 보호 테스트는 그대로 유지한다.
- 검증 결과: `python3 -m py_compile tests/test_wiki_memory.py` 통과. 사용자 승인 뒤 프로젝트 전용 [`.venv/`](../../.venv/)에 [backend/requirements.txt](../../backend/requirements.txt)를 설치했고, `.venv/bin/python -m pytest tests/test_wiki.py tests/test_wiki_memory.py -q`는 **12 passed**로 통과했다. 운영 endpoint/DB/서비스는 호출·변경하지 않았다.

#### W02 [x] 저비용 사전 제외와 저장 거부 우선 처리

- 의존: W01.
- 대상: [wiki_memory.py](../../backend/app/wiki_memory.py), [main.py](../../backend/app/main.py)의 기억 작업 진입, W01 테스트.
- 작업: 질문/인사/단순 요청의 명확한 비후보, 명시 저장 거부를 분리. 문장 전체 물음표 필터 금지. 혼합 발언은 후보 처리로 통과.
- 완료: R01·R04에서 기억 추론·Wiki 쓰기 0회. 일반 채팅 답변은 그대로 생성. 기존 자동 OFF+명시 요청 계약 보존.
- 검증: `기억하지 마`, `기억해 줘라는 말이 뭐야?`, 영문 질문, 물음표 없는 질문, 혼합 사실/질문. 명시 요청 정규식 변경이 다른 모드에 영향 없음.
- 결과: [wiki_memory.py](../../backend/app/wiki_memory.py)에 좁은 `automatic_memory_skip_reason` 사전 검사를 추가했다. 명백한 질문·인사·단순 요청·저장 거부는 Wiki 검색과 LLM 판단 전에 제외한다. `complete_generation`은 해당 작업을 만들지 않으며, 기존 pending 작업은 [main.py](../../backend/app/main.py)에서 외부 호출 없이 `skipped`로 끝낸다. 명시적 저장 요청과 first-person/current-state 혼합 발언은 후보로 남긴다.
- 검증 결과: [test_wiki_memory.py](../../tests/test_wiki_memory.py)에 R01/R04, 영문·물음표 없는 질문, 인사·단순 요청, 인용된 기억 표현, legacy pending 작업, 혼합 발언을 추가했다. `.venv/bin/python -m pytest tests/test_wiki.py tests/test_wiki_memory.py -q` **26 passed**, `.venv/bin/python -m pytest tests/test_background_switch_deferral.py -q` **5 passed**, `py_compile`과 diff 검사 통과. 운영 endpoint/DB/서비스는 호출·변경하지 않았다.

#### W03 [x] 후보 분류와 최소 원문 절 검증

- 의존: W02.
- 대상: [wiki_memory.py](../../backend/app/wiki_memory.py), [main.py](../../backend/app/main.py)의 memory payload/parse, W01 테스트.
- 작업: kind·source ref·reason 구조를 정하고 단일 후보만 추출. 저장 명령·추천 요청 전체를 제외. JSON schema 실패나 의미 검증 불확실 시 보류. current/previous user 출처 확인을 유지.
- 완료: R02의 추천 요청 전체가 저장되지 않음. R03 사실 절만 후보. R05~R08에서 부정·시점·주체 왜곡 없음. 주체 미확정 개인 기억은 보류.
- 검증: 악의적/오분류 모델 응답이 `kind=fact`로 질문을 제출해도 저장 거절. 긴 입력 절단, 원문 불일치, assistant만 존재, plan을 fact로 승격하는 사례. 실제 모델 품질 검증은 W19에 남김.
- 결과: [wiki_memory.py](../../backend/app/wiki_memory.py)의 `MemoryDecision`에 `kind`, `reason`, `source_message_id`를 추가했다. 저장 행동은 durable fact/preference/plan과 선택된 사용자 메시지 ID·정확한 원문 절을 모두 요구하며, `none`은 question/uncertain과 null source만 허용한다. [main.py](../../backend/app/main.py)는 현재 메시지 ID와 허용된 이전 사용자 메시지 ID를 모델에 제공하고, 선택 ID가 assistant/알 수 없는 메시지이거나 원문 절이 불일치하면 거절한다.
- 결과: 요청형 원문과 날짜 없는 계획을 서버에서 보류한다. 추천 요청 전체를 `fact`로 제출해도 저장하지 않지만, `지금 쓰는 iphone 12 pro max`처럼 정확히 인용된 최소 사실 절은 저장할 수 있다. 부정·수치·시제는 원문을 재서술하지 않고 그대로 저장하므로 이 Task가 의미를 정규화하지 않는다. kind/reason/source ID의 DB 영속·API 노출은 W04에 남긴다.
- 검증 결과: [test_wiki_memory.py](../../tests/test_wiki_memory.py)에 구조화 계약, assistant ID 위조, 부정 보존, 시간 명시 계획, 시간 없는 계획, R02 전체 요청 거절·최소 사실 절 허용을 추가했다. `.venv/bin/python -m pytest tests/test_wiki.py tests/test_wiki_memory.py tests/test_background_switch_deferral.py -q` **37 passed**, `py_compile`과 diff 검사 통과. 실제 모델의 JSON-schema 출력 품질과 운영 저장 결과는 W19 전까지 미검증이다.

#### W04 [x] 판정 이유 저장·호환 API 노출

- 의존: W03.
- 대상: [db.py](../../backend/app/db.py), [main.py](../../backend/app/main.py)의 상태/history, [api.ts](../../frontend/src/api.ts), 기억 테스트.
- 작업: 기존 DB의 작업에 필요한 최소 reason_code/needs_attention/정책 버전 정보를 추가하는 migration 작성. 기존 상태/필드 유지, legacy 행의 NULL 처리. 명시 none을 중복/보류/실패로 구분.
- 완료: 구형 응답 필드와 상태 유지. 과거 행 의미를 추정해 backfill하지 않음. 질문/중복은 저장 실패로 오인되지 않음.
- 검증: 임시 old-schema DB의 반복 migration, 기존 기록 보존, null reason fallback, explicit/automatic 구분. 운영 migration 금지.
- 결과: [db.py](../../backend/app/db.py)에 schema migration 15을 추가했다. 기존 `wiki_memory_jobs`에 nullable `memory_kind`, `reason_code`, `needs_attention`, `policy_version`만 additive로 추가하며, 기존 행은 모두 NULL로 유지한다. 신규 작업은 정책 버전 1을 기록하고, 질문/불확실성/중복/비내구 후보/쓰기 불명/처리 실패를 상태와 분리해 기록한다. explicit 거절과 unknown·실패만 attention으로 표시한다.
- 결과: [main.py](../../backend/app/main.py)의 이력 API는 기존 필드를 유지한 채 새 메타데이터를 추가로 반환한다. [api.ts](../../frontend/src/api.ts)와 [main.tsx](../../frontend/src/main.tsx)는 이를 optional nullable 메타데이터로 타입화했다. W05 전까지 기존 UI의 상태 표시 방식은 바꾸지 않는다.
- 검증 결과: legacy SQLite에서 migration 15을 두 번 실행해 기존 job의 새 열이 NULL인 채 보존되고 migration 기록이 한 번만 생기는 것을 확인했다. API 이력의 신규/구형 필드도 확인했다. `.venv/bin/python -m pytest tests/test_wiki.py tests/test_wiki_memory.py tests/test_background_switch_deferral.py tests/test_api.py::test_legacy_database_migrates_without_losing_conversation_data -q` **40 passed**. `py_compile`과 diff 검사는 통과. 전체 `tests/test_api.py`는 2분 이상 무출력으로 중단했으므로 미완료로 기록한다. frontend `node_modules`가 없어 lint/build는 실행하지 않았고 패키지 설치도 하지 않았다.

#### W05 [x] 기억 결과 UI를 짧은 상태와 상세 패널로 분리

- 의존: W01. W04 전에는 legacy 상태만으로도 동작; reason 표시 연결은 W04 이후 완료.
- 대상: [main.tsx](../../frontend/src/main.tsx), [api.ts](../../frontend/src/api.ts), [scroll.css](../../frontend/src/scroll.css), 신규 `frontend/src/components/WikiMemoryStatus.tsx`, 신규 `frontend/tests/WikiMemoryStatus.test.tsx`.
- 작업: 원문·전후 본문을 기본 입력 패널에서 제거하고 접힌 상세 진입 제공. 상세는 높이 제한 dialog/drawer. 기본 알림은 의미 있는 최신 결과를 선택하되 이전 완료를 현재 요청 결과처럼 표시하지 않음.
- 완료: R09·R11 충족. 명시 요청 결과/unknown은 접근 가능. 원문은 React text로 렌더링. 최근 실패가 skip에 묻히지 않음.
- 검증: 긴 30개 작업, pending→completed, duplicate, explicit skipped, unknown, 대화 전환. dialog 포커스 이동/복귀·Escape·키보드 접근. 기본 화면에는 원문 반복 없음.
- 결과: [WikiMemoryStatus.tsx](../../frontend/src/components/WikiMemoryStatus.tsx)를 추가해 기본 입력 영역에는 처리 중 또는 확인 필요 상태만 짧게 표시하고, 완료·skip 기록은 현재 요청 결과처럼 표시하지 않는다. 원문과 Wiki 변경 전후 본문은 스크롤 제한 대화상자에서만 React 텍스트로 렌더링한다. unknown/failed는 최신 skip보다 우선 표시하며, 명시 요청 여부·분류·판정 이유·대상 문서를 상세에서 확인할 수 있다.
- 검증 결과: [WikiMemoryStatus.test.tsx](../../frontend/tests/WikiMemoryStatus.test.tsx)에 기본 화면 원문 비노출, 상세 열기/닫기와 포커스 복귀, unknown 우선 표시, 30개 기록 상세 렌더링을 추가했다. TypeScript Problems 진단과 `git diff --check`는 통과했다. 이 환경에는 `node`/`npm` 실행기가 없어 Vitest·lint·build와 실제 브라우저에서 Escape·viewport 시각 검증은 미실행이다. 패키지 설치, 서비스 변경, 운영 데이터 변경은 하지 않았다.

#### W06 [x] 입력 영역·최신 이동 버튼·표 레이아웃 재현 및 수정

- 의존: W05.
- 대상: [scroll.css](../../frontend/src/scroll.css), [style.css](../../frontend/src/style.css), [MessageList.tsx](../../frontend/src/components/MessageList.tsx); 필요 시 신규 `frontend/tests/MessageList.test.tsx`.
- 작업: R09/R10을 브라우저에서 재현하고 원인을 기록한 뒤 최소 CSS/배치 수정. 메시지 viewport·입력 패널·상세 패널 스크롤 소유권 분리. 버튼이 본문을 가리지 않도록 공간 확보.
- 완료: §4.4의 네 크기에서 입력/전송 접근 가능, 문서 전체 가로 overflow 없음, 표만 내부 스크롤 가능, 마지막 답변 가림 없음.
- 검증: 긴 표/코드/한국어·스트리밍·이전 메시지 로딩·사용자 위쪽 스크롤·최신 이동·상세 열기/닫기. DOM 테스트만으로 실제 레이아웃 통과를 주장하지 않음. 브라우저 불가면 시각 검증 미완료로 기록.
- 결과: [MessageList.tsx](../../frontend/src/components/MessageList.tsx)에 스크롤 가능한 `.messages`와 별도 `.message-viewport`를 두고 최신 이동 버튼을 viewport 오버레이로 이동했다. 목록 하단에 버튼 높이만큼 여백을 확보해 마지막 답변이 가려지지 않도록 했다. [MarkdownContent.tsx](../../frontend/src/components/MarkdownContent.tsx)는 표를 전용 가로 스크롤 컨테이너로 감싸며, [scroll.css](../../frontend/src/scroll.css)와 [style.css](../../frontend/src/style.css)는 메시지·입력·상세 패널의 스크롤 소유권 및 긴 표·코드의 가로 넘침을 제한한다.
- 검증 결과: TypeScript Problems 진단과 `git diff --check`는 통과했다. 이 환경에는 `node`/`npm`, 실행 중인 Client 서버, 브라우저 실행기가 없어 Vitest·lint·build 및 §4.4의 네 viewport 시각 검증, 스트리밍/이전 기록 로딩 실제 동작은 미실행이다. 패키지 설치, 서비스·운영 데이터 변경은 하지 않았다.

#### W07 [x] 1차 통합 회귀와 상태 기록

- 의존: W02~W06.
- 대상: 위 테스트, [wiki-search-evaluation.md](wiki-search-evaluation.md), 이 문서 §8.
- 작업: fake Wiki·mock LLM의 API→작업→UI 시나리오 연결. 기존 문서 2와 같은 질문 본문은 테스트 fixture에서만 구성하고 운영 문서 수정 없음.
- 완료: 질문 저장 차단, 혼합 발언 보류/절 추출, 기존 문서 불변, UI 입력 접근 검증 결과 기록. 1차 완료와 후속 미완료를 분리.
- 검증: backend 관련 회귀, frontend 관련 tests/lint/build, W06 시각 확인. 기존 결과 unknown 재시도 금지와 모델 epoch 회귀 포함.
- 결과: [test_wiki_memory_integration.py](../../tests/test_wiki_memory_integration.py)에 fake Wiki·mock LLM의 저장 작업과 UI 이력 입력(`wiki_memory_history`) 연결을 추가했다. 사실 선호는 저장 결과와 메타데이터가 이력에 노출되고, 질문은 완료 처리 후 이력에 작업을 만들지 않는 것을 확인한다. [wiki-search-evaluation.md](wiki-search-evaluation.md)에 1차 회귀 표와 미검증 범위를 기록했다.
- 검증 결과: `.venv/bin/python -m pytest tests/test_wiki.py tests/test_wiki_memory.py tests/test_wiki_memory_integration.py tests/test_background_switch_deferral.py -q` **41 passed**. API의 snapshot 충돌·잘못된 epoch 테스트 2개는 통과했다. `test_stale_epoch_fails_stream_without_retry`는 20초 무출력으로 시간 제한 종료했고 전체 `test_api.py`는 실행하지 않았다. `node`/`npm` 및 실행 중인 Client 서버 부재로 frontend Vitest/lint/build와 W06 네 viewport 시각 검증은 미실행이다. 운영 Wiki/DB/모델에는 접근하지 않았다.

### 2차: 중복·갱신과 Wiki 계약

#### W08 [ ] 기억 메타데이터 스키마·소유 범위 계약 확정

- 의존: W00, W03, W04.
- 대상: 이 문서 §4.2, [wiki-integration.md](wiki-integration.md). 이 Task는 설계 문서만 수정.
- 작업: 기존 DB 보조 테이블 DDL 초안, unique/index, scope/subject 판정, source 삭제 정책, operation 상태 전이, legacy 검색 방식을 확정. 일반 문서와 기억 간 경계 명시.
- 완료: 이후 구현자가 필드·삭제 정책·개인 식별 규칙을 새로 결정할 필요 없음. 신규 저장소 불필요 근거 기록.
- 검증: 생성/중복/갱신/충돌/대화 삭제/Wiki 외부 편집/부분 성공 시나리오를 표로 점검. 미확정 정책은 W09 차단 조건.

#### W09 [ ] 기존 DB에 메타데이터와 repository 구현

- 의존: W08.
- 대상: [db.py](../../backend/app/db.py), 신규 `backend/app/memory_repository.py`, 신규 `tests/test_memory_repository.py`.
- 작업: 추가 migration·조회·claim·유일성 제약·활성 전환 transaction 구현. 기존 문서 import/backfill 금지.
- 완료: 과거 데이터 불변, 새 DB 파일 없음, DB 연결을 잡은 채 HTTP/LLM await하지 않음.
- 검증: old-schema upgrade 반복, 동시 claim/unique 충돌, rollback, source 삭제 정책, 비활성 버전 조회 제외.

#### W10 [ ] 정확 중복·충돌 판정 순수 함수

- 의존: W09.
- 대상: [wiki_memory.py](../../backend/app/wiki_memory.py), W09 repository, 신규 `tests/test_memory_dedup.py`.
- 작업: scope/subject/predicate/value/fingerprint 비교. 정확 중복 no-op, 상충값 보류, 명시 정정만 supersede 후보. 의미 검색은 후보 제한에만 사용.
- 완료: R07 충족, 부정/숫자/단위/시간/다른 사용자 정보가 정규화로 합쳐지지 않음.
- 검증: 공백/Unicode 변형, 같은 제목 다른 내용, 다른 문서 같은 사실, 오래된 계획, 다중 주체, 후보 검색 누락. 전역 의미 중복 완전 탐지를 주장하지 않음.

#### W11 [ ] Wiki HTTP 응답·오류 계약 강화

- 의존: W00, W01.
- 대상: [wiki.py](../../backend/app/wiki.py), [main.py](../../backend/app/main.py)의 Wiki 라우트, [test_wiki.py](../../tests/test_wiki.py).
- 작업: 응답 타입/크기 검증, 빈 검색과 형식 오류 구분, 404/409/422/503/timeout 구분, 성공 응답 호환 유지. 204 여부는 W00 실계약에 따라 처리.
- 완료: 서버 오류가 빈 검색으로 숨겨지지 않고, malformed 응답이 write 완료로 처리되지 않음.
- 검증: MockTransport로 잘못된 JSON/리스트 원소/크기 초과/redirect/상태별 오류. loopback·trust_env=False 유지.

#### W12 [ ] Wiki 서버 원자적 revision 지원

- 의존: W00, W11. 실제 서버 소스 미확보면 실행 금지.
- 대상: W00에서 확정한 Wiki 라우터·저장 계층·격리 테스트. 시작 시 정확한 파일 목록을 §8에 기입.
- 작업: 저장 transaction 안에서 조건 revision 비교와 갱신. 기존 조건 없는 수동 API 호환과 새 조건부 API를 구분. 기존 본문/ID 변경 migration 금지.
- 완료: 두 writer의 같은 revision 요청 중 하나만 성공. 조건 미일치에는 409와 안정된 오류 코드.
- 검증: 격리 DB 동시성·외부 수동 편집 경쟁·legacy 요청. Client GET→PUT 테스트만으로 완료 처리하지 않음.

#### W13 [ ] Wiki 서버 멱등 쓰기와 결과 조회

- 의존: W12.
- 대상: W00에서 확정한 Wiki 저장/API 파일과 테스트.
- 작업: 문서 저장과 operation 기록을 같은 transaction으로 처리. 동일 key/payload 재사용, 충돌, 작업 결과 조회 구현. payload hash는 결정적 직렬화 사용.
- 완료: 응답 유실 뒤 결과 조회로 원래 문서 ID/버전을 확인 가능. 같은 key의 다른 내용은 거절.
- 검증: commit 전후 crash 경계, 같은 key 동시 POST, 동일 요청 재조회, operation 없음과 처리 불명 구분. 테스트용 서버만 사용.

#### W14 [ ] Client 저장·갱신·결과 조정 연결

- 의존: W10~W13.
- 대상: [main.py](../../backend/app/main.py)의 기억 작업, [wiki.py](../../backend/app/wiki.py), W09 repository, [test_wiki_memory.py](../../tests/test_wiki_memory.py).
- 작업: operation ID를 쓰기 전에 영속화. 새 버전 Wiki 저장 성공 뒤 활성 전환. unknown은 작업 조회로만 조정. 일반 legacy 문서 덮어쓰기 금지.
- 완료: DB와 Wiki 부분 성공이 기존 기억 소실/중복 쓰기로 이어지지 않음. 서버 기능 미지원 시 새 안전 쓰기 경로 보류, 구형 위험 경로 자동 fallback 금지.
- 검증: HTTP 응답 유실, Wiki 성공 뒤 Client DB 실패, 앱 재시작, 동시 중복/정정, Wiki 외부 변경/삭제, 대화 삭제. 추정 성공 처리 금지.

### 3차: RAG·출처·조회 비용

#### W15 [ ] RAG 역할 분리·발췌·검색 평가

- 의존: W11, W14.
- 대상: [wiki.py](../../backend/app/wiki.py), [main.py](../../backend/app/main.py)의 검색 경로, [prompting.py](../../backend/app/prompting.py), [test_wiki.py](../../tests/test_wiki.py), [wiki-search-evaluation.md](wiki-search-evaluation.md).
- 작업: 일반 자료/활성 기억을 구분, 비활성 기억 문서 제외, bounded query/후보/발췌. 전체 payload 예산 안에서 자료를 선택. 검색 순위 알고리즘 변경은 실제 Wiki 구현 확인·평가 결과가 있을 때만 별도 Task로 분할.
- 완료: legacy 문서 불변, 삭제/만료/비활성 기억 오주입 방지, 불충분 근거 안내, 실제 context 준수.
- 검증: 한국어 조사·후속 질문·긴 문서·무관한 제목·검색 없음·오류·문서 지시 injection·4K/16K profile mock. 기존 대비 Recall@3/무관 근거율 기록.

#### W16 [ ] 출처 메타데이터 API·본문 분리

- 의존: W15.
- 대상: [main.py](../../backend/app/main.py), [db.py](../../backend/app/db.py), [api.ts](../../frontend/src/api.ts), [test_api.py](../../tests/test_api.py).
- 작업: 새 요청의 명시적 opt-in 계약과 저장 출처 스키마 구현. retrieved/provided/cited 구분. 기존 답변 수정 없음. 새 모드의 다음 턴에는 출처 JSON 본문 재주입 없음.
- 완료: stream/nonstream·이력 조회 동일 출처, 구형 요청 기본 형식 유지. 문서 버전/발췌는 답변 생성 시점 기준.
- 검증: 전달되지 않은 ID 인용, 최종 프롬프트에서 제거된 자료, 문서 사후 변경/삭제, 구형 클라이언트, 재로딩·request 중복. 기존 legacy 답변의 자동 재작성/문자열 휴리스틱 삭제 금지.

#### W17 [ ] 경량 기억 조회·출처 카드·polling 개선

- 의존: W04, W05, W16.
- 대상: [main.py](../../backend/app/main.py)의 history/detail API, [main.tsx](../../frontend/src/main.tsx), [api.ts](../../frontend/src/api.ts), [MessageList.tsx](../../frontend/src/components/MessageList.tsx), W05 UI 컴포넌트/테스트.
- 작업: view=summary opt-in, 상세 클릭 조회, source 카드, 비활성 탭 중지/진행 중 polling. 대화 완료 이벤트 이후 새 작업 discovery를 보장하고 미등록→등록 race에도 영구 중단하지 않음.
- 완료: 30개 전후 본문을 매 5초 전송하지 않음. 기본 API 응답 유지. 요청 중첩 방지, R12 충족.
- 검증: fake timers·visibility·네트워크 실패·대화 전환·완료 직후 등록 지연·상세 권한/대화 소속 확인. frontend lint/test/build와 화면 확인.

#### W18 [ ] 기억 작업 대기·토큰·모델 전환 비용 제한

- 의존: W14, W15.
- 대상: [main.py](../../backend/app/main.py)의 background scheduler/prepare, [config.py](../../backend/app/config.py), [test_background_switch_deferral.py](../../tests/test_background_switch_deferral.py), [test_model_context.py](../../tests/test_model_context.py).
- 작업: 기존 DB 작업 큐 사용, bounded admission/대기/추론 1개. foreground 대기 중 신규 기억 추론 양보. 매 epoch 재준비 시 불필요한 Wiki 재검색 빈도 제한은 freshness 보존 가능한 경우만 적용.
- 완료: 후보당 전송 1회 원칙, 명확한 admission 거절만 재준비, 타이틀·요약 경로 회귀 없음. 16K 하드코딩/모델 자동 전환 없음.
- 검증: 큐 포화·foreground 경합·모델 hold·409 전송 전 거절/전송 후 불명·schema 미지원·토큰 초과·취소/재시작. fake gate로 검증하고 실제 GPU 성능은 W19에서 구분.

#### W19 [ ] 통합 품질·비용 평가와 롤백 문서 확정

- 의존: W07, W14~W18.
- 대상: [wiki-search-evaluation.md](wiki-search-evaluation.md), [wiki-integration.md](wiki-integration.md), [README](../../README.md), 이 문서 §8.
- 작업: 기존/개선 동일 fixture로 평가. Qwen3.6/Gemma 실제 모델 검증은 별도 승인된 환경·실행 기회가 있을 때 수행하고 없으면 미검증으로 남김. 새로운 schema 전체를 기존 모델 기능 시험이 보장한다고 간주하지 않음.
- 완료: 질문/assistant 사실 저장 0건, 정확 중복 쓰기 0회, conflict/unknown 덮어쓰기 0회(필수 회귀 세트 기준). Recall@3, 저장 precision, 선별 recall, 추가 추론 비율/토큰, foreground 지연 p50/p95, UI 요청량 기록.
- 검증: 관련 backend 통합, frontend lint/tests/build, 네 viewport 시각 확인. 운영 적용·부하 측정·장애 주입 미실행은 별도 표시. 모두 skip하는 정책으로 recall을 희생해 완료하지 않음.

## 7. 실행 순서·검증·롤백

권장 순서:

1. **첫 실행:** W01 → W02 → W03 → W04. 저장 오류부터 차단한다.
2. **다음 실행:** W05 → W06 → W07. 사용자 스크린샷의 UI 문제를 해결한다. 이 묶음은 Wiki 서버 소스 없이 진행 가능하다.
3. W00 → W08 → W09 → W10 → W11 → W12 → W13 → W14.
4. W15 → W16 → W17 → W18 → W19.

W00은 앞서 수행해도 되며, 외부 소스 부재로 1차 작업을 멈출 필요는 없다. W12~W14가 차단되면 안전한 읽기/화면 개선만 진행하고 쓰기 보장을 축소하지 않는다.

후속 구현 시 검증 명령 예시(기존 환경이 준비된 경우만):

```bash
python -m pytest tests/test_wiki.py tests/test_wiki_memory.py -q
python -m pytest tests/test_background_switch_deferral.py tests/test_model_context.py -q
```

Python 실행기는 저장소에 이미 준비된 테스트 환경에 맞춘다. 신규 테스트는 해당 Task에서 경로를 추가한다. frontend 디렉터리에서 기존 의존성으로 `npm test -- <대상 테스트>`, `npm run lint`, `npm run build`를 실행한다. 준비된 build task가 있으면 해당 task를 사용한다. 테스트 설정에 운영 URL/키/DB가 들어가지 않았는지 확인하고 실행한다. 이 문서 작성 중 위 명령은 실행하지 않았다.

| 위험 | 예방 | 롤백 |
| --- | --- | --- |
| 필터가 유용한 사실까지 제외 | 질문/혼합/명시 사실 세트를 함께 측정 | 새 자동 저장을 중지하고 정책 조정. 질문 저장 가능한 구형 writer를 무조건 재활성화하지 않음 |
| UI 수정으로 입력/스크롤 회귀 | 별도 W06 및 viewport 확인 | UI 변경만 복귀, 서버 선별 보호는 유지 |
| 추가 DB schema | additive·legacy NULL·반복 migration 검증 | 먼저 기능/코드만 복귀, 추가 열/행 삭제 금지. 구버전 startup 호환 확인 |
| Wiki/Client 부분 성공 | operation 영속화·조회·확정 뒤 활성 전환 | unknown 유지, 무조건 POST 재전송 금지 |
| 기억 잘못 갱신 | 원문·이전 버전 보존 | 새 버전 비활성·이전 활성 관계 복원. 운영 데이터 조정은 별도 승인 작업 |
| 저장소/소유 범위 불명 | W00/W08 차단 기준 | 해당 저장만 보류, 일반 대화/검색 유지 |
| 모델 전환 경합/부하 | 기존 epoch/기능/예산 검증 재사용 | 자동 기억 중지, gate/Studio/모델 서비스 임의 변경 없음 |
| 운영 DB 전체 복원 손실 | 정상 롤백은 기능 비활성+호환 코드 복귀 | 전체 복원은 기본 절차에서 제외. 별도 사고 복구 판단 필요 |

과거 [Wiki 연동 계획](wiki-integration.md)의 서비스 통합/별도 Wiki 종료 TODO는 이 계획의 선행 조건이 아니다. 이번 범위에서 실행하지 않는다.

## 8. 실행 이력과 다음 실행 프롬프트

| 날짜 | Task | 상태 | 수정/검증 | 다음 단계 |
| --- | --- | --- | --- | --- |
| 2026-11-12 | 계획 작성 | 완료(구현 아님) | Client 핵심 경로·스크린샷 분석. 문서만 작성. 운영/테스트 데이터 저장·테스트 실행 없음 | W01 |
| 2026-11-12 | W00 외부 Wiki 계약 조사 | 완료(소스 접근 차단 기록) | 작업 공간·접근 가능한 `/opt`·인접 workspace·Git 참조를 읽기 전용 조사. 예상 경로는 `/opt/bc250-family-wiki/app/main.py`이나 이 환경에는 `/opt`와 Wiki 소스가 없음. Client API 사용 계약과 과거 운영 조사 기록만 §2.3에 기록. 운영 endpoint/DB/서비스는 호출·변경하지 않음 | W01; W12~W14는 Wiki 소스 제공 전 차단 |
| 2026-11-12 | W01 스크린샷·기존 계약 회귀 | 완료 | R01/R02의 현행 비사실 원문 저장 경로를 fake Wiki·mock LLM·임시 SQLite로 재현하는 기준 테스트를 [test_wiki_memory.py](../../tests/test_wiki_memory.py)에 추가. 기존 원문/role/unknown/quick/명시 요청 보호 테스트 유지. 사용자 승인으로 프로젝트 전용 [`.venv/`](../../.venv/)에 [backend/requirements.txt](../../backend/requirements.txt)를 설치했고, 대상 pytest **12 passed** | W02 |
| 2026-11-12 | W02 사전 제외·저장 거부 | 완료 | [wiki_memory.py](../../backend/app/wiki_memory.py)에 좁은 사전 제외를 추가하고, [main.py](../../backend/app/main.py)에서 신규 작업 생성 전·legacy pending 작업 실행 전 적용. [test_wiki_memory.py](../../tests/test_wiki_memory.py)로 질문/거부는 Wiki·LLM 호출 0회, 혼합 발언은 후보 유지 확인. Wiki/기억 pytest **26 passed**, background-switch pytest **5 passed**, `py_compile`·diff 검사 통과 | W03 |
| 2026-11-12 | W03 구조화 분류·최소 원문 절 | 완료 | [wiki_memory.py](../../backend/app/wiki_memory.py)에 kind/reason/source message ID 계약과 요청형 원문·날짜 없는 계획 보류를 추가하고, [main.py](../../backend/app/main.py)에서 모델 입력·검증에 연결. R02 전체 추천 요청 거절과 최소 사실 절 허용, assistant ID 위조, 부정/계획 계약을 [test_wiki_memory.py](../../tests/test_wiki_memory.py)에 추가. 관련 pytest **37 passed**, `py_compile`·diff 검사 통과 | W04 |
| 2026-11-12 | W04 판정 메타데이터·호환 API | 완료 | [db.py](../../backend/app/db.py)에 nullable metadata migration 15, [main.py](../../backend/app/main.py)에 상태별 reason/attention 기록과 이력 API 필드를 추가. legacy row NULL 보존·반복 migration·이력 응답을 테스트. 관련 pytest **40 passed**, `py_compile`·diff 검사 통과. 전체 `test_api.py`는 장기 무출력으로 중단, frontend 의존성 부재로 lint/build 미실행 | W05 |
| 2026-11-12 | W05 기억 결과 UI 축소·상세화 | 완료(실행 검증 일부 미완료) | [WikiMemoryStatus.tsx](../../frontend/src/components/WikiMemoryStatus.tsx)로 기본 상태와 대화상자 상세를 분리하고, [WikiMemoryStatus.test.tsx](../../frontend/tests/WikiMemoryStatus.test.tsx)를 추가. Problems·diff 검사 통과. `node`/`npm` 부재로 Vitest·lint·build 및 브라우저 시각 검증은 미실행 | W06 |
| 2026-11-12 | W06 메시지 viewport·최신 이동·표 스크롤 | 완료(시각 검증 미완료) | [MessageList.tsx](../../frontend/src/components/MessageList.tsx)의 viewport 오버레이와 [MarkdownContent.tsx](../../frontend/src/components/MarkdownContent.tsx)의 표 스크롤 컨테이너를 추가. Problems·diff 검사 통과. `node`/`npm`, Client 서버, 브라우저 실행기 부재로 Vitest·lint·build·네 viewport 시각 검증은 미실행 | W07 |
| 2026-11-12 | W07 1차 통합 회귀·상태 기록 | 완료(Frontend/stream 시각 검증 미완료) | fake Wiki·mock LLM 이력 연결 [test_wiki_memory_integration.py](../../tests/test_wiki_memory_integration.py)를 추가하고 관련 backend 회귀 **41 passed**. snapshot/invalid epoch API 테스트 2건 통과. stale-epoch stream은 20초 무출력 시간 제한, frontend 실행·시각 검증은 환경 부재로 미실행 | W08 |

Task 완료 시 아래 형식으로 덧붙인다:

```text
Task:
상태: 완료 / 진행 / 차단
수정 파일:
계약 변경: 없음 또는 명시
검증 명령과 결과:
시각/운영/실모델 미검증:
남은 위험:
다음 Task:
```

Terra / medium에서 시작할 때 붙여 넣을 프롬프트:

```text
docs/plans/wiki-memory-execution-plan.md를 실행 기준으로 사용한다.
이번에는 W01만 수행한다. 완료 후 이 문서의 상태와 실행 이력을 갱신한다.
기존 사용자 변경을 보존하고, 관련 Wiki/기억 경로만 읽는다.
운영 DB·Wiki 문서·서비스·모델·패키지는 변경하지 않는다.
테스트는 임시 DB, fake Wiki, mock gate에 한정한다.
새로운 저장 정책/모델 서비스/외부 저장소를 임의 도입하지 않는다.
실행하지 못한 검증은 미실행으로 보고한다. commit/push/배포하지 않는다.
```

다음 실행에서는 `W01`만 다음 Task ID로 바꾸고 의존성·완료 기준을 확인한다. 사용자 요청이 여러 Task 연속 실행이라면 같은 원칙으로 각 Task 완료 기록을 남긴 뒤 순서대로 진행한다.
