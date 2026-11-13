# Wiki RAG·선별 자동 기억·UI 실행 계획

작성일: 2026-11-12. 실행 대상: **Terra / medium**. W00~W11의 실제 진행 상태는 각 Task와 §8을 따른다.

> **2026-11-13 방향 변경:** Wiki를 Client 내부의 가족 공유 지식 공간으로 통합한다. 이 문서 §6의 I00~I10을 순서대로 실행한다. [통합 설계와 전환 계약](wiki-client-consolidation-plan.md)은 상세 참고 자료이며 W12~W19는 §9의 과거 이력이다. W00~W11 결과는 보존한다. 개인 기억과 다중 사용자 식별은 후속 범위다. 이번 변경은 문서만 갱신한다.

이 문서는 한 세션에서 전체 개편을 수행하지 않고, 아래 Task 하나씩 구현·검증·기록하기 위한 실행 기준이다. 모델 설정은 실행자가 Terra / medium으로 선택한다. 이 문서 작성 시 모델 전환이나 구현 실행은 하지 않았다.

## 1. 범위와 작업 제한

- 기존 Client FastAPI, Wiki, Client SQLite, llama.cpp와 gate를 재사용한다.
- 1차 구현: 질문 원문 저장 방지, 혼합 발언 선별, 기억 UI 축소, 입력 영역·스크롤 회귀 해결.
- 후속 구현: 중복·충돌·갱신, Wiki 쓰기 계약, RAG 역할 분리, 출처 메타데이터, 작업 비용 제한.
- 새 서비스·새 DB 파일·상주 모델·벡터 DB·임베딩 서비스를 도입하지 않는다.
- Client의 기존 대화·메시지·자동 기억 이력은 보존한다. 사용자는 별도 Family Wiki DB는 버리고 통합 Wiki를 빈 상태로 시작해도 된다고 했다. 이번 문서 작업에서 DB를 삭제하거나 전환하지 않는다.
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

**방향 변경 우선 규칙:** 이 절의 외부 Wiki/Client 간 분리 저장과 HTTP 결과 조정은 당시 설계 이력이다. 신규 목표는 [통합 계획의 전환 안전 계약](wiki-client-consolidation-plan.md#전환-안전-계약)에 따라 같은 Client SQLite의 단일 트랜잭션을 사용한다. 삭제·출처·개인정보 보류 등 사용자 정책은 그대로 유지한다.

- Wiki는 본문 저장소, 기존 Client SQLite는 기억 분류·출처·fingerprint·활성 관계·작업 조정 저장소다.
- 기존 wiki_memory_jobs는 작업 생명주기로 유지한다. 내용 비교와 활성 기억에 필요한 보조 테이블은 기존 DB 안에 추가한다. 새 DB 파일은 만들지 않는다.
- 후보 스키마: memory_records(scope, subject_key, predicate_key, value, kind, source refs, fingerprint, wiki_document_id, wiki_revision, state, supersedes_id, timestamps). 최종 필드/인덱스는 W08의 계약 산출물로 확정한다.
- fingerprint는 안전한 공백/Unicode 정규화까지만 한다. 부정, 수치, 단위, 날짜를 제거하지 않는다. semantic key와 value 비교를 별도로 한다.
- 정확 중복은 DB 유일성 제약과 작업 claim으로 방지한다. 유사도/LLM 판단만으로 자동 합치지 않는다.
- 갱신은 새 버전 기록 + supersedes 관계를 기본으로 한다. Wiki 성공 확인 후 기존 DB의 단일 트랜잭션에서 활성 관계를 전환한다. 기존 문서 본문은 자동으로 덮어쓰지 않는다.
- 이전 버전이 일반 Wiki 검색 경로로 재주입되지 않게 문서 ID를 기준으로 제외한다. 기존 문서는 legacy 자료로 유지하며 자동 개인 기억으로 승격하지 않는다.
- 외부 Wiki 편집/삭제가 감지되면 revision 불일치를 보류하고 메타데이터를 최신이라고 단정하지 않는다.
- 대화 삭제 시 source 연결 해제/원문 보존/개인정보 처리 정책을 W08에서 명시한다. 조용한 cascade로 활성 기억 인덱스가 사라지거나 원문을 무조건 영구 복제하는 구현을 피한다.

#### 4.2.1 W08 확정 계약 — 소유·표현·출처

아래는 **구현할 제안 계약**이며 현재 운영 동작이 아니다. 기존 Wiki 문서와 W04 이전 작업을 읽거나 재분류해 backfill하지 않는다. 기존 Client SQLite를 재사용하므로 새 DB·상주 서비스는 필요하지 않다. 다만 이 DB와 Wiki는 별도 트랜잭션이므로 Client SQLite의 unique 제약만으로 Wiki 전체의 중복이나 원자성을 보장할 수 없다.

**범위 정정:** 개인 기억 및 여러 기기·선호 지원은 사용자가 요청한 후속 TODO다. 현재 W09 구현 범위에 넣지 않는다. 아래 W08의 `shared` 단일 scope와 단일 활성 값 계약은 현재 W09 초안의 기준으로 유지하며, 기존 W02~W04 writer가 개인·공유 분리를 제공하거나 이를 강제한다고 간주하지 않는다. 향후 확장 시 기존 기억의 소유자를 임의 추정해 일괄 이전하지 않는다.

#### 4.2.1a 후속 TODO — 개인 기억과 가족 확장 (현재 범위 아님)

- [ ] 현재 1인 사용이라는 맥락을 보존하고, 향후 사용자 발언의 `나`에 대한 개인 기억 및 공유 서버·프로젝트 기억을 분리하는 방법을 설계한다. 현재 개인 scope·인증·소유자 식별이 구현됐다고 가정하지 않는다.
- [ ] 가족에게 사용을 열기 전에 사용자별 신원 확인, 개인 기억 접근 범위, 기존 기억의 귀속·이전 및 공유 기억 편집 정책을 별도 설계하고 검증한다. 단순 프로필 선택을 인증으로 간주하지 않는다.
- [ ] 향후 `현재 사용하는 기기`, `명시적인 선호`, `날짜가 분명한 계획`과 공유 서버·프로젝트 정보를 지원할 범위를 확정한다. 자연어 해석·소유자·대상이 모호하면 보류하고 비밀·민감정보 및 저장 거부 우선 원칙을 유지한다.
- [ ] 여러 기기·선호와 계획을 덮어쓰지 않고 구분할 다중값 식별·중복·정정 정책을 설계한다. 서비스별 포트처럼 대상이 다른 값도 섞지 않는다.
- [ ] 후속 구현 시 migration 16의 `scope_key='shared'` 제약과 단일 active index를 검토한다. 기존 데이터 보존, 단일·다중값의 유일성·동시 claim·정정 및 사용자별 접근을 별도로 검증한다. 적용 여부를 추정해 기존 DB를 변경하지 않는다.
- [ ] W12~W15의 Wiki 원자성·멱등성·검색 분리 조건을 충족한 뒤 별도로 활성화 여부를 판단한다. 이 TODO는 현재 W09 완료 범위가 아니다.

#### 4.2.1b 현재 W08 초안 — 개인·다중값은 후속 범위

- `scope_key`는 초기 버전에서 고정 문자열 `shared`뿐이다. 이는 공유 접근 범위이며 개인 인증·개인 소유를 뜻하지 않는다. `subject_key`는 관리 사전에 등록된 비인격 대상의 불투명한 안정 ID만 허용하며 이름·기기 시리얼 등 사용자 원문을 포함하지 않는다. `me`, `나`, 대화 ID, Wiki 제목, 발화자의 암묵적 신원을 개인 식별자로 변환하지 않는다. 현재 인증된 사용자 식별 기능이 없으므로 사람을 주체로 하는 기억은 명시적 기억 요청을 포함해 보류하고 UI에 이유를 표시한다. 개인 scope 도입은 별도 인증·동의 설계 후에만 가능하다.
- 2026-11-13 확정: 공유 서버 `bc250_server`의 `operating_system`, `backup_path`, `backup_frequency`, 공유 Client `bc250_client`의 `service_port` 네 단일 속성만 초기 허용한다. LLM이 임의 키를 만들거나 여러 의미의 속성을 합치지 않는다. 값 정규화는 아래 NFC·공백 규칙만 쓴다. 자연어→키 매핑과 모호성 회귀는 W10에서 구현·검증하기 전 자동 분류 및 새 writer 연결을 차단한다. 키가 없거나 대상·의미가 모호하면 보류하고 제목이나 의미 유사도로 대체하지 않는다. W09의 로컬 저장소 완료는 자연어 매핑이나 운영 안전 쓰기 완료를 뜻하지 않는다.
- 사용자 승인 충돌 정책: 같은 속성의 기존 값과 명확한 새 값이 다르고 명시 정정이 아니면 기존 값을 유지하고 확인 후보로 보류한다. 후속 UI는 기존·새 값과 대상을 보여 주고 변경/유지를 제공한다. 변경 승인 시 확인 대상의 기존 record ID·revision·출처와 현재 상태를 다시 확인하고 변경됐으면 다시 판단한다. 취소·무응답은 쓰지 않는다. 명시 정정은 추가 확인 없이 후보로 진행할 수 있으나 동일한 충돌 및 안전 저장 검증을 거친다. 대상이 모호하면 명확화 또는 보류한다. 확인 후보의 영속성·만료·중복 응답·취소 및 동시성은 W10/W14/W17에서 구체화·검증하며 현재 구현된 기능이 아니다.
- 값은 검증된 사용자 원문 절 그대로 보관하고 비교용 값은 Unicode NFC와 양끝·연속 공백 정규화만 한다. 대소문자, 부정, 숫자, 단위, 날짜를 지우거나 동의어를 합치지 않는다. 날짜 없는 계획이나 기준일·주체가 모호한 표현은 보류한다. fingerprint는 버전 태그와 길이가 지정된 UTF-8 필드 `(scope_key, subject_key, predicate_key, kind, normalized_value)`를 순서대로 SHA-256 처리한다. 비교 전 원 필드도 확인해 해시 충돌을 안전하게 보류한다.
- 원문 메시지 및 대화 참조는 nullable FK `ON DELETE SET NULL`이다. 기억 본문을 원문 복제 저장소로 취급하지 않고, 출처 삭제 시 해당 기록은 활성 검색에서 즉시 제외하고 값·정규화 값·fingerprint와 출처 span을 NULL로 지운다. 문서 ID와 격리 사유만 비식별 tombstone으로 유지한다. 삭제된 대화의 작업 본문은 기존 cascade 정책을 유지한다. Wiki 본문 삭제나 수정은 자동 수행하지 않으며, 사용자가 Wiki에서 별도 처리할 수 있도록 해당 문서 ID를 격리한다. 즉 대화 삭제만으로 Wiki 원문까지 삭제됐다고 주장하지 않는다.
- 여러 출처를 자동으로 합치지 않는다. 중복 발견 시 기존 출처를 바꾸지 않고 새 job에 duplicate 결과만 기록한다. 출처 삭제가 여러 버전에 영향을 미치면 그 출처를 참조하는 각 버전을 위 규칙으로 처리하며 이전 버전을 자동 재활성화하지 않는다.

#### 4.2.2 W08 DDL 초안 — W09의 이행 대상

다음 DDL은 migration 15 이후의 **초안**이다. `created_at`/`updated_at`은 기존 Client의 UTC ISO 문자열, ID는 서버가 발급한 UUID 문자열이다. `subject_key`/`predicate_key`는 사용자 원문 자체가 아닌 검증된 canonical 식별자이며 공백 문자열을 금지한다. SQLite FK를 켜고, 신규 테이블 생성과 migration 기록은 하나의 트랜잭션에서 처리한다. 구현은 기존 데이터·DB 파일을 변경하거나 삭제하지 않는 additive migration만 허용한다.

```sql
CREATE TABLE memory_records (
 id TEXT PRIMARY KEY,
 scope_key TEXT NOT NULL CHECK(scope_key = 'shared'),
 subject_key TEXT NOT NULL CHECK(length(subject_key) > 0),
 predicate_key TEXT NOT NULL CHECK(length(predicate_key) > 0),
 kind TEXT NOT NULL CHECK(kind IN ('fact','preference','plan')),
 value TEXT, normalized_value TEXT, fingerprint TEXT,
 source_conversation_id TEXT REFERENCES conversations(id) ON DELETE SET NULL,
 source_message_id TEXT REFERENCES messages(id) ON DELETE SET NULL,
 source_start INTEGER, source_end INTEGER,
 wiki_document_id INTEGER, wiki_revision TEXT,
 state TEXT NOT NULL CHECK(state IN ('candidate','active','superseded','quarantined','redacted')),
 supersedes_id TEXT REFERENCES memory_records(id) ON DELETE RESTRICT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 CHECK((state = 'redacted' AND value IS NULL AND normalized_value IS NULL AND fingerprint IS NULL
        AND source_start IS NULL AND source_end IS NULL)
    OR (state <> 'redacted' AND value IS NOT NULL AND normalized_value IS NOT NULL AND fingerprint IS NOT NULL
        AND source_conversation_id IS NOT NULL AND source_message_id IS NOT NULL
        AND source_start IS NOT NULL AND source_end > source_start)),
 CHECK(state <> 'active' OR (wiki_document_id IS NOT NULL AND wiki_revision IS NOT NULL))
);
CREATE UNIQUE INDEX memory_one_active_slot ON memory_records(scope_key,subject_key,predicate_key)
 WHERE state = 'active';
CREATE INDEX memory_fingerprint_lookup ON memory_records(scope_key,fingerprint) WHERE fingerprint IS NOT NULL;
CREATE INDEX memory_source_lookup ON memory_records(source_message_id);
CREATE INDEX memory_document_lookup ON memory_records(wiki_document_id);

CREATE TABLE memory_operations (
 id TEXT PRIMARY KEY,
 record_id TEXT NOT NULL REFERENCES memory_records(id) ON DELETE RESTRICT,
 scope_key TEXT NOT NULL, subject_key TEXT NOT NULL, predicate_key TEXT NOT NULL,
 operation_type TEXT NOT NULL CHECK(operation_type IN ('create','supersede')),
 request_hash TEXT NOT NULL, expected_revision TEXT, wiki_document_id INTEGER,
 state TEXT NOT NULL CHECK(state IN ('claimed','prepared','sending','unknown','confirmed','applied','rejected','cancelled')),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE UNIQUE INDEX memory_one_open_operation ON memory_operations(scope_key,subject_key,predicate_key)
 WHERE state IN ('claimed','prepared','sending','unknown','confirmed');
CREATE INDEX memory_operation_record ON memory_operations(record_id);

CREATE TABLE memory_document_quarantine (
 wiki_document_id INTEGER PRIMARY KEY,
 reason TEXT NOT NULL CHECK(reason IN ('owned','source_deleted','external_change','unknown_write')),
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
```

`source_start`/`source_end`는 원본 메시지의 Unicode code point 기준 0 이상·끝 제외 인덱스다. repository는 같은 대화의 user 메시지이며 span 원문이 value와 정확히 같은지 transaction 안에서 확인한다. NULL이 허용되는 SQLite CHECK 동작을 고려해 구현에서 각 NULL을 명시 검증한다. 출처 삭제는 FK 동작만으로 위 CHECK를 만족하지 않으므로 **대화·메시지 삭제 transaction에서 먼저 redaction과 quarantine을 수행한 뒤 삭제**해야 한다. 다른 삭제 경로도 같은 repository를 경유하게 하고 직접 SQL 삭제 시 실패하도록 테스트한다. tombstone의 subject/predicate가 사람을 식별할 수 있는 환경은 이 버전에서 허용하지 않으며, 개인 식별 도입 시 삭제 설계를 다시 승인해야 한다.

`memory_one_active_slot`은 동일 scope/subject/predicate에서 활성 값 하나만 허용한다. 사전에 다중값 속성은 포함하지 않는다. `memory_one_open_operation`은 unknown을 포함해 같은 슬롯의 새 쓰기를 막으며, 명시적 운영 복구 전 timeout으로 claim을 해제하지 않는다. `request_hash`는 정규화된 operation ID·메서드·대상·expected revision·요청 body의 버전 지정 canonical bytes SHA-256이다. 내용이나 URL·credential을 로그에 남기지 않는다. `supersedes_id`는 동일 슬롯의 활성 버전만 가리키고 순환·자기 참조를 repository가 거절한다.

#### 4.2.3 작업 상태·검색·시나리오

- `claimed → prepared → sending → confirmed → applied`: claim/후보 생성과 상태 변경은 짧은 Client DB transaction이다. `sending`과 request hash를 커밋한 뒤에만 Wiki에 전송한다. `confirmed`는 Wiki가 operation ID·body hash·문서 ID·revision을 확인해 준 경우에만 기록한다. 확인된 문서 ID의 `owned` 격리 등록과 confirmed 기록은 같은 transaction에서 처리한다. `confirmed → applied`에서는 Wiki 응답을 재확인한 다음 기존 활성 버전 superseded와 새 active 전환을 하나의 transaction으로 수행한다. HTTP/LLM await 중 DB 연결이나 transaction을 유지하지 않는다.
- 전송 전 확정 거절은 `rejected` 또는 `cancelled`이며 새 claim은 새 operation ID로 가능하다. 전송 후 응답 유실·timeout·불명 오류·재시작 중 sending은 `unknown`이다. unknown은 결과 조회만 허용하고 자동 POST/PUT 재전송이나 새 operation 생성은 금지한다. 같은 ID의 안전한 재전송도 W13의 서버 멱등성 계약이 실제 검증되기 전 금지한다. 확인된 미적용과 실패가 서버에서 입증되면 명시적 복구로 rejected 처리한다.
- 현재 Wiki는 CAS·operation ID·결과 조회가 확인되지 않았다(W00). **W09의 로컬 repository 구현은 가능하지만 새 writer 연결·활성화는 W12~W14 검증 전 차단**한다. W09 자체는 기존 writer를 변경하지 않는다. W04 현행 경로는 이 보장을 제공하지 않으므로 운영에서 현재 쓰기가 계속 가능하다는 사실과 목표 보장을 혼동하지 않는다.
- 새로운 관리 기억은 문서당 하나의 immutable 버전으로 생성하고, 일반 Wiki 문서에 append/replace하지 않는다. 관리 기억의 모든 문서 ID는 `memory_document_quarantine`에 `owned`로 남긴다. RAG에서는 해당 ID를 일반 Wiki 검색 결과에서 제외한 뒤 검증된 active 버전만 별도 기억 채널에 넣는다. superseded·quarantined·redacted·unknown 문서는 주입하지 않는다. 생성 응답 유실로 문서 ID를 모르면 그 ID를 제외할 수 없으므로 operation 결과 조회로 식별할 때까지 일반 Wiki 검색 주입 전체를 보류하고 사용자에게 알린다. 제외 후 결과 부족은 빈 결과로 처리하며 무제한 추가 검색하지 않는다. 검색 결과 수 제한으로 인한 recall 저하는 측정한다. W15에서 이 필터가 구현되기 전 새 writer 활성화는 차단한다.
- legacy 문서는 일반 Wiki 자료로만 검색하며 제목·내용·category로 기억 소유권을 추정하거나 새 메타데이터로 승격하지 않는다. 일반 문서와 legacy 기억이 섞인 경우 자동 분리하지 않는다. Client에서 한정된 Wiki 검색 후보를 조회해 정확 중복 가능성을 보류할 수는 있지만, 검색 누락이 없다는 보장은 없다. 따라서 **legacy 전체에 대한 전역 중복 방지는 미보장**이며 불확실하면 새 쓰기를 보류한다.
- 외부 편집/삭제 감지는 Wiki 서버가 제공하는 검증 가능한 revision 비교를 요구한다. mismatch·404·검증 불가는 active를 `quarantined`로 바꾸고 문서 tombstone을 유지한다. 단순 타임스탬프 비교나 캐시만으로 변경 없음이 증명됐다고 간주하지 않는다. 검증 시점과 이후 외부 편집 사이의 경합은 W12 CAS 및 조회 경로 설계가 해결해야 하며, 실제 계약이 없으면 활성 쓰기/주입은 차단한다.

| 시나리오 | 결정 및 transaction 경계 |
| --- | --- |
| 신규 생성 | 신원·단일 속성·출처 검증 → 슬롯 claim → Wiki 성공 확인 → active 전환 |
| 정확 중복 | 동일 슬롯·정규화 값 및 원 필드 확인 후 Wiki 쓰기 없이 기존 active 유지 |
| 명시 정정 | 동일 슬롯의 기존 active와 명확한 새 값 확인; 새 문서 확인 후 이전 superseded와 새 active를 원자 전환 |
| 상충·모호 | 자동 덮어쓰기 없이 conflict/ambiguous 보류; unknown claim도 해제하지 않음 |
| 대화/출처 삭제 | 같은 DB transaction에서 관련 값 redaction·문서 격리 후 대화 삭제; Wiki 문서는 보존 |
| Wiki 외부 편집/삭제 | revision 불일치/404/검증 실패 시 격리; 자동 재작성·이전 버전 복원 금지 |
| 응답 유실/부분 성공 | operation unknown 유지, Wiki 결과 조회로만 확인; 기존 active 유지, 새 문서 일반 검색 제외 |
| Wiki 성공 후 Client commit 실패 | confirmed 또는 unknown을 유지해 재조회 후 단일 transaction 적용; 임의 재전송 금지 |

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

#### W08 [x] 기억 메타데이터 스키마·소유 범위 계약 확정

- 의존: W00, W03, W04.
- 대상: 이 문서 §4.2, [wiki-integration.md](wiki-integration.md). 이 Task는 설계 문서만 수정.
- 작업: 기존 DB 보조 테이블 DDL 초안, unique/index, scope/subject 판정, source 삭제 정책, operation 상태 전이, legacy 검색 방식을 확정. 일반 문서와 기억 간 경계 명시.
- 완료: 이후 구현자가 필드·삭제 정책·개인 식별 규칙을 새로 결정할 필요 없음. 신규 저장소 불필요 근거 기록.
- 검증: 생성/중복/갱신/충돌/대화 삭제/Wiki 외부 편집/부분 성공 시나리오를 표로 점검. 미확정 정책은 W09 차단 조건.
- 결과: §4.2.1~4.2.3에 소유 범위, 삭제·격리, DDL/index, operation 상태 및 검색 경계를 명세했다. 당시 고정 predicate 사전은 미확정이었으며, 이후 2026-11-13 사용자 승인으로 §4.2.1b의 네 속성이 확정됐다. Wiki CAS/멱등성·결과 조회와 검색 필터가 없으므로 W12~W15 검증 전 새 writer를 활성화하지 않는다. W09는 로컬 repository만 구현한다.
- 검증: 생성·정확 중복·정정·충돌·출처 삭제·외부 편집·부분 성공을 설계 표로 점검했다. 이 작업은 문서만 변경하며 migration·운영 DB·서비스·Wiki를 실행하거나 변경하지 않았다.

#### W09 [x] 기존 DB에 메타데이터와 repository 구현 — 로컬 저장소 완료

- 의존: W08.
- 대상: [db.py](../../backend/app/db.py), 신규 `backend/app/memory_repository.py`, 신규 `tests/test_memory_repository.py`.
- 작업: 추가 migration·조회·claim·유일성 제약·활성 전환 transaction 구현. 기존 문서 import/backfill 금지.
- 완료: 과거 데이터 불변, 새 DB 파일 없음, DB 연결을 잡은 채 HTTP/LLM await하지 않음.
- 검증: old-schema upgrade 반복, 동시 claim/unique 충돌, rollback, source 삭제 정책, 비활성 버전 조회 제외.
- 기반: [db.py](../../backend/app/db.py)에 기존 DB에만 추가되는 migration 16과 W08 테이블·인덱스를 작성했다. 이후 아래와 같이 repository 및 대화 삭제 처리를 추가했다. 새 writer에는 연결하지 않았다.
- 범위 정정: 개인 기억 및 여러 기기·선호 지원은 §4.2.1a의 후속 TODO이며 현재 W09 범위에 넣지 않는다. 공유 scope·단일 active 제약과 사용자 승인 네 속성 사전을 적용한다.
- 추가 진행: [memory_repository.py](../../backend/app/memory_repository.py)에 공유 대상 `bc250_server`의 `operating_system`, `backup_path`, `backup_frequency`와 `bc250_client`의 `service_port`만 허용하는 초기 키 사전 및 짧은 transaction의 claim·상태 전이·활성 전환을 추가했다. 사용자 원문 span을 검증하고 출처 삭제 시 [main.py](../../backend/app/main.py)의 대화 삭제 transaction에서 redaction·Wiki 문서 격리를 수행한다. 현재는 writer/API의 새 기억 경로에 연결하지 않았다. 향후 개인 scope·다중값은 후속 migration과 별도 계약으로 확장하며 기존 공유 slot의 의미를 자동 변경하지 않는다.
- 후속 차단: W10의 제한적인 자연어 매핑은 구현됐지만 실제 writer에 연결되지 않았다. 통합 namespace·검색 격리·원자적 저장·승인 확인은 I02/I06/I07에서 구현한다. 따라서 **W09 로컬 저장소 완료와 무관하게 새 writer 연결·운영 활성화는 차단**한다. `confirmed`/`apply_confirmed`는 서버 증거 확인을 호출자에게 위임하는 내부 부품이며 현재 엔드포인트에서 사용할 수 없다.
- 2026-11-13 로컬 후속: 메시지 단독 삭제를 위한 `redact_message_memory`를 추가했다. 호출자가 해당 메시지 삭제와 같은 transaction에서 실행해야 하며 현재 메시지 단독 삭제 endpoint는 없다. 삭제 실패 rollback과 다른 메시지 불변, 소스 삭제 시 문서 격리를 검증했다. 확인 전이에서는 공백만 있거나 문자열이 아닌 revision을 거절한다. 이는 서버 증거를 검증한다는 뜻이 아니다. 관련 격리 회귀 **57 passed**; 운영 DB·Wiki·서비스는 변경하지 않았다.
- 2026-11-13 완료 검증: 승인된 네 slot의 claim과 migration 중간 실패 시 새 테이블·버전 기록 rollback을 추가 검증했다. 기존 기억·Wiki·모델 전환 회귀 포함 **62 passed**, `git diff --check` 통과. 자연어 매핑 품질이나 서버 증거를 검증한 결과는 아니다.
- 추가 검증: 임시 SQLite·mock만 이용한 repository, 기존 기억 및 모델 전환 회귀 **46 passed**. 동시 claim 1건 허용, 출처가 assistant인 후보 거부, unknown 재전송 전이 거부, 명시 정정의 활성 대상 변경 시 rollback과 대화 삭제 시 redaction·격리를 확인했다. 운영 DB·Wiki·서비스는 변경하지 않았다.
- 후속 안전성 검증: 사용자 메시지 범위를 벗어난 span·boolean index와 이미 격리된 Wiki 문서의 재사용을 거절한다. 전송 중 대화 삭제 시 결과를 unknown으로 남기며 삭제된 출처를 다시 활성화하지 않는다. 이 경우 결과 조회·문서 격리 복구는 Wiki 서버 계약이 준비될 때까지 미구현이다. 관련 임시 DB 회귀 **51 passed**. 자연어→속성 매핑과 모호성 검증은 W10 이후, 서버 결과 증거·복구는 W12~W14 이후에 연결하며 현재 저장소를 운영 writer에 연결하지 않는다.
- 검증: 임시 DB에서 v15 형태로 되돌린 fixture의 반복 upgrade, legacy 내용 보존, active unique 충돌, 비활성 조회 제외, 직접 대화 삭제 실패를 확인했다. `.venv/bin/python -m pytest tests/test_memory_repository.py tests/test_wiki_memory.py tests/test_api.py::test_legacy_database_migrates_without_losing_conversation_data -q` **35 passed**. 운영 migration·서비스 재시작·Wiki 쓰기는 수행하지 않았다. 향후 이 코드 배포 후 startup에서는 migration 16이 실행된다.

#### W10 [x] 정확 중복·충돌 판정 순수 함수 — 제한된 표현 지원

- 의존: W09.
- 대상: [wiki_memory.py](../../backend/app/wiki_memory.py), W09 repository, 신규 `tests/test_memory_dedup.py`.
- 작업: 승인된 네 속성에 대한 자연어 매핑과 모호성 보류, scope/subject/predicate/value/fingerprint 비교. 정확 중복 no-op, 상충값 확인 후보, 명시 정정 또는 유효한 사용자 승인만 supersede 후보. 의미 검색은 후보 제한에만 사용. 확인 응답의 출처·대상 검증은 W14/W17에서 연결한다.
- 완료: R07 충족, 부정/숫자/단위/시간/다른 사용자 정보가 정규화로 합쳐지지 않음.
- 검증: 공백/Unicode 변형, 같은 제목 다른 내용, 다른 문서 같은 사실, 오래된 계획, 다중 주체, 후보 검색 누락. 전역 의미 중복 완전 탐지를 주장하지 않음.
- 결과: [memory_dedup.py](../../backend/app/memory_dedup.py)에 네 속성의 명시적인 전체 문장 템플릿과 정확한 원문 span 추출, 중복·충돌·정정 후보 순수 판정을 추가했다. 질문·부정·계획·복수 대상·모호한 문장 등 인식하지 못하는 입력은 보류한다. 범용 한국어 의미 분석이나 자동 저장으로 연결하지 않았으며 표현 범위가 좁아 실제 발언도 보류될 수 있다.
- 안전 조건: 활성 slot 조회가 불완전하거나 legacy 중복 여부가 확인되지 않으면 신규 생성 후보를 보류한다. 제한된 Wiki 검색의 빈 결과는 legacy 전체 중복 부재의 증거가 아니다. 변경 승인 후보는 대상 record ID·revision·새 값 fingerprint가 모두 일치해야 한다. 승인 인증·만료·일회성 소비, 원문 출처 확인 및 최신 revision 재검증은 W14/W17의 별도 책임이며 이 순수 함수의 결과는 쓰기 권한이 아니다.
- 검증: [test_memory_dedup.py](../../tests/test_memory_dedup.py)의 명시·모호·부정·계획·범위 외 포트·다른 대상, NFC/공백, 대소문자·숫자·부정, fingerprint 오류·충돌, 불완전 조회, 변경된 승인 대상 회귀와 기존 테스트를 실행해 **86 passed**. Wiki 문서 제목은 판단에 쓰지 않는다. 같은 사실의 다른 Wiki 문서에 대한 전역 중복 부재는 검증하거나 보장하지 않는다. 운영 DB·Wiki·서비스는 변경하지 않았다.

#### W11 [x] Wiki HTTP 응답·오류 계약 강화 — Client 측 구현

아래 계약은 레거시 외부 Wiki 어댑터의 이력이다. 통합 전환 전에는 유지하고 전환 이후 제거 여부는 I04/I09에서 검증한다. 이후 W12 조사에서 당시 운영 파일의 POST/PUT ID와 DELETE JSON 형태를 확인했으나 실행 프로세스와 동일 버전인지는 미확인이다.

- 의존: W00, W01.
- 대상: [wiki.py](../../backend/app/wiki.py), [main.py](../../backend/app/main.py)의 Wiki 라우트, [test_wiki.py](../../tests/test_wiki.py).
- 작업: 응답 타입/크기 검증, 빈 검색과 형식 오류 구분, 404/409/422/503/timeout 구분, 성공 응답 호환 유지. 204 여부는 W00 실계약에 따라 처리.
- 완료: 서버 오류가 빈 검색으로 숨겨지지 않고, malformed 응답이 write 완료로 처리되지 않음.
- 검증: MockTransport로 잘못된 JSON/리스트 원소/크기 초과/redirect/상태별 오류. loopback·trust_env=False 유지.
- 결과: [wiki.py](../../backend/app/wiki.py)에서 응답을 스트리밍으로 읽어 4 MiB, 문서 목록 1,000개로 제한하고, 검색·문서 조회 및 쓰기 응답의 필수 형식/ID를 검증한다. 정상 빈 검색과 손상된 응답을 구분하며 404/409/422/503, timeout 504, 잘못된 응답·기타 upstream 오류 502를 구분한다. RAG 오류도 이 코드를 보존한다. 전송 이후 잘못된 쓰기 응답은 자동 기억 job에서 성공이 아닌 `unknown`이다.
- 호환성 제한: 실제 Wiki 서버 소스·실제 응답은 확인되지 않았다. 특히 PUT 성공 응답의 문서 ID, DELETE 성공 JSON 및 204 여부가 실계약과 다를 수 있으므로 운영 적용 전 W00 서버 계약 조사·격리 검증이 필요하다. 현재 204는 성공으로 추정하지 않고 502로 처리한다. 새 한도에 걸리는 기존 문서 목록도 확인해야 한다. 구형 쓰기 경로의 수동 재시도와 원자성 문제를 해결한 것은 아니다.
- 검증: [test_wiki.py](../../tests/test_wiki.py)의 MockTransport 응답/상태/timeout/redirect/크기·개수 제한, [test_wiki_memory_integration.py](../../tests/test_wiki_memory_integration.py)의 잘못된 쓰기 응답 후 `unknown` 회귀 및 기존 기억·저장소 테스트 포함 **110 passed**. 운영 Wiki 호출·DB 마이그레이션·서비스 재시작 없이 수행했다.

### 3차: 가족 공유 Wiki 통합 — 다음 작업부터 순서대로 실행

현재 시작점은 **I09**이다. I00~I10의 상태는 이 문서에서 관리한다. Wiki는 가족용 LLM의 공유 공간이고 개인 기억·사용자별 접근 제어는 후속 범위다. 기존 Family Wiki 문서 없이 시작할 수 있지만 Client 대화·기억·작업 이력은 보존한다. 이력의 외부 Wiki ID와 새 통합 문서 ID를 저장소 식별자 없이 연결하지 않는다.

전환 안전 계약: 단일 writer, 외부·통합 ID namespace 분리, 문서·FTS·기억·작업의 동일 SQLite transaction, revision 조건부 갱신, 작업 ID 기반 중복 방지, 출처 삭제·비활성 기억 격리를 지킨다. LLM/사용자 입력/HTTP 대기 중 DB 트랜잭션을 유지하지 않는다. 상세 전환 계약과 위험은 [통합 설계](wiki-client-consolidation-plan.md#전환-안전-계약)를 참고한다. 운영 DB·서비스·배포는 아래 I09/I10 전 별도 수행하지 않는다.

2026-11-13 결정: 같은 revision을 편집한 여러 요청 중 **먼저 commit된 저장**을 유지한다. 이후 요청은 409로 보류하고 작성 중인 내용을 보존한다. 도착 순서나 버튼을 먼저 누른 순서를 보장하지 않는다. 저장된 revision 간 diff와 충돌 시 사용자의 미저장 초안과 최신본 diff를 지원한다. 현재 revision 숫자만 저장하는 I01/I03 구현은 과거 본문을 보존하지 않으므로 별도의 버전 이력이 필요하다(I04a). 자동 병합·자동 덮어쓰기는 하지 않는다.

#### I00 [x] 전환 계약과 현황 확인

- 의존: W00, W09~W11.
- 대상: 이 문서, [통합 설계](wiki-client-consolidation-plan.md), [wiki-integration.md](wiki-integration.md), [db.py](../../backend/app/db.py), [main.py](../../backend/app/main.py), [wiki.py](../../backend/app/wiki.py), 기존 UI·설정.
- 작업: 외부 API·화면 호출자, Client DB schema v16, 작업 상태와 문서 ID 참조를 파악한다. 빈 Wiki 시작, legacy/integrated namespace, 쓰기 중단·전환·롤백 계약을 확정한다. 운영 내용·비밀은 기록하지 않는다.
- 완료: 각 ID 참조와 기존 pending/unknown/active 처리 경계, 확인 가능한 사실과 미확인 사항, I01/I02 수정 대상이 명시된다.
- 검증: 코드·migration 설계 리뷰. 필요한 운영 조사는 읽기 전용으로 수행하고 파일과 실행 프로세스의 버전 차이 가능성을 기록한다.
- 결과: 다음 I00 조사 기록의 호출 경로·상태·ID 참조와 I01/I02 계약을 확정했다. 작업 공간 코드·기존 SSH 읽기 전용 조사 이력과 소스 스냅샷을 검토했으며 이번 실행에서 운영 서비스·DB는 조회하거나 변경하지 않았다. 미확인 외부 호출자와 실제 운영 schema 버전은 I09 전환 검증에 남긴다.

##### I00 조사 기록 — 현재 코드에서 확인

| 경로·저장 위치 | 현재 동작과 전환 영향 |
| --- | --- |
| [wiki.py](../../backend/app/wiki.py) | `127.0.0.1:8020` HTTP 어댑터, 8초 timeout. 문서 목록·상세·작성 응답을 검증한다. 통합 모드에서는 외부 HTTP를 호출하지 않아야 한다. |
| [main.py](../../backend/app/main.py)의 `/api/v1/wiki/documents` | 인증 API가 목록·상세·생성·수정·삭제를 외부 Wiki에 전달한다. PUT의 기존 GET→시간 비교→PUT은 원자적 CAS가 아니다. 통합 모드에서도 기존 성공 응답은 유지한다. |
| [main.py](../../backend/app/main.py)의 RAG·기억 작업 | quick 대화의 검색은 외부 `/api/search` limit 3, 기억 후보 검색은 limit 5다. 기억 작업은 모델 판단 후 외부 GET/POST/PUT을 수행한다. 현재 legacy writer가 동작하며 W09/W10의 새 repository와는 연결되어 있지 않다. |
| [main.py](../../backend/app/main.py)의 답변 저장 | Wiki 참고자료 JSON이 assistant `messages.content`의 텍스트로 보존된다. 구조화된 문서 참조가 아니므로 ID를 파싱해 새 Wiki 문서에 연결하거나 과거 메시지를 일괄 수정하지 않는다. |
| [WikiMemoryStatus.tsx](../../frontend/src/components/WikiMemoryStatus.tsx), [api.ts](../../frontend/src/api.ts) | 이력은 `document_id`와 제목 등을 표시하지만 현재 문서 이동 링크는 없다. Wiki 검색·자동 기억 토글과 이력 UI는 있고 Client 문서 관리 UI는 없다. |
| 기존 Wiki [소스 스냅샷](../../tests/fixtures/wiki_server_w12/main.py) | 단독 Wiki는 자체 문서 CRUD·검색 및 LLM 8080 직접 호출 `/api/ask`를 제공한다. 확인했던 HTML 화면은 문서 관리·검색·AI 질문을 호출한다. Client 채팅은 `/api/ask`를 사용하지 않는다. |

별도 Wiki HTML·서버 파일은 이전 SSH 조사에서 읽었으며, 그때의 소스 파일과 실제 실행 프로세스가 동일하다는 증거는 없다. 이전 조사 문서의 `/wiki/*`→8020 경로 및 서비스 상태는 **과거 관찰**이다. 현재 운영 설정·직접 API 사용자·다른 기기나 외부 호출자·운영 DB의 migration 버전 및 행 수는 미확인이다. I00의 설계는 운영 레코드의 존재를 가정하지 않으며 I09에서 읽기 전용으로 재확인한다.

##### I00 기존 ID·상태와 namespace 계약

작업 공간 [db.py](../../backend/app/db.py)는 `LATEST_SCHEMA_VERSION = 16`이다. 이는 현재 소스의 목표 버전이며 운영 DB가 16이라는 뜻은 아니다. 현재 `initialize()`는 실제 DB 생성·migration과 일부 UPDATE를 수행하므로 읽기 전용 조사에 호출하지 않는다.

| 저장 위치 | 현재 식별·상태 | I02 계약 |
| --- | --- | --- |
| `wiki_memory_jobs.document_id` | generation별 nullable 정수, 상태 `pending/running/writing/completed/skipped/failed/unknown`; 이력에 반환 | 기존 행은 `legacy_external`로 식별하고 값·상태·본문을 이력으로 보존. 값이 NULL이어도 옛 작업을 새 문서 작업으로 추정하지 않는다. |
| `memory_records.wiki_document_id`, `wiki_revision` | nullable 정수·문자열; candidate/active/superseded/quarantined/redacted. active unique는 scope/subject/predicate 기준 | 기존 행을 `legacy_external`로 식별. 통합 조회에서 active로 자동 승격하지 않으며 ID만으로 열지 않는다. slot 충돌 정책과 인덱스에 저장소 구분을 반영한다. |
| `memory_operations.wiki_document_id`, `record_id`, `expected_revision` | nullable 문서 ID, record FK; claimed/prepared/sending/unknown/confirmed/applied/rejected/cancelled. open unique는 scope/subject/predicate 기준 | record와 동일 namespace를 명시·검증한다. 과거 진행 중 작업을 통합 저장소에서 실행하거나 재전송하지 않는다. 복구되지 않은 작업이 새 slot을 막는지 검사하고 격리 정책에 따라 분리한다. |
| `memory_document_quarantine.wiki_document_id` | 정수 하나가 PRIMARY KEY이고 저장소 구분 없음 | `(store_id, wiki_document_id)` 복합 키로 안전하게 변경·인덱스 재구성한다. 외부 ID 1 격리가 통합 ID 1과 섞이지 않으며 양쪽을 독립 격리할 수 있어야 한다. |
| 사용자·assistant 메시지의 Wiki 참고자료 | assistant 본문 안 텍스트·발췌·ID, 출처 테이블 아님 | 메시지 내용 그대로 보존. 번호는 당시 외부 Wiki의 참고자료이며 통합 문서의 클릭 가능한 참조로 자동 변환하지 않는다. |

전환 시 legacy `pending`은 새 writer로 처리하지 않고 중단 이력으로 남긴다. 이미 외부로 전송됐을 수 있는 `writing/running/unknown`과 `sending/unknown/confirmed`는 성공·실패를 추정하거나 통합 저장소에서 재시도하지 않고 legacy 불명/검토 대상으로 보존한다. 종료된 `completed`, `skipped`, `failed` 이력은 과거 상태 그대로 유지한다. 기존 `active` 기억도 외부 namespace에서만 보존하며 통합 검색·활성 기억에는 포함하지 않는다. 이는 목표 계약이며 현재 startup의 `pending→failed`, `running/writing→unknown` 처리가 namespace를 구분해 구현됐다는 뜻은 아니다.

##### I00 I01/I02 구현 지침·전환 경계

1. **I01:** [db.py](../../backend/app/db.py)의 v16 뒤에 새 migration을 추가해 같은 Client DB에 통합 문서·revision·FTS5·trigger를 만든다. DB 초기화는 기존 패턴을 검토하되 migration의 schema/버전 기록이 실패 시 원자적으로 되돌아가도록 검증한다. 별도 Wiki 파일을 열거나 import하지 않는다. v15에서 v16을 거쳐 올라가는 경우와 v16을 모두 테스트한다.
2. **I02:** `wiki_memory_jobs`, `memory_records`, `memory_operations`, `memory_document_quarantine`의 저장소 식별자를 함께 다룬다. legacy 행과 신규 행의 기본값을 혼동하지 않도록 신규 쓰기는 namespace를 명시한다. quarantine의 기존 단일 PK와 record/operation의 active/open unique 인덱스, document lookup 인덱스를 migration에서 재설계한다. FK/참조·redaction·조회·이력 API 및 UI 표시도 함께 검증한다. 전환 전에 새 namespace를 모르는 구버전은 writer로 실행하지 않는다. 구버전이 schema 버전을 검사한다고 가정하지 않고 I09에서 실행 자체를 통제한다.
3. 같은 SQLite transaction에서 문서·FTS·기억 상태·작업 결과를 함께 commit한다. 별도 Client↔Wiki HTTP 부분 성공 복구 계약은 통합 경로에 그대로 이식하지 않는다. 다만 commit 뒤 클라이언트 응답이 유실될 수 있으므로 작업 ID·payload hash에 기반한 결과 재조회와 idempotency는 I07에서 구현한다.
4. 운영 전환은 구형 자동 기억 작업 생성과 수동 Wiki 쓰기를 막고, 실행 중 작업을 종료 또는 legacy 불명으로 분류한 뒤 단일 통합 writer를 활성화한다. 기존 Wiki를 빈 통합 Wiki로 전환한 뒤에는 구 서비스가 자동 쓰기로 되돌아가지 않는다. 새 통합 데이터가 생긴 후 롤백은 우선 읽기 전용 보존이며 과거 서비스 재개 시 양쪽 변경·ID 충돌을 별도 해결한다. 실제 재시작·기존 Wiki DB 삭제는 I10의 별도 운영 작업이다.
5. I01/I02 테스트는 임시 DB와 fixture로만 수행한다. 같은 숫자 ID를 가진 양 namespace, 이력·메시지 보존, 중단/불명 작업, 구버전 실행 차단, index/FK·rollback·반복 migration을 포함한다. 현재 조사에는 테스트 실행이나 운영 데이터 저장이 없다.

#### I01 [x] 통합 문서·FTS 스키마

- 의존: I00.
- 대상: [db.py](../../backend/app/db.py), 신규 `tests/test_integrated_wiki_db.py`.
- 작업: v16 다음 additive migration에 통합 문서·revision·FTS5·trigger를 추가한다. 기존 Client 데이터는 보존하고 기존 Wiki 자동 import 없이 0건으로 시작한다. FTS 오류를 감추지 않는다.
- 완료: 새 DB 파일 없이 문서 및 인덱스가 추가되고 기존 대화·기억 이력이 유지된다.
- 검증: v15/v16 upgrade·반복·중간 실패 rollback, FTS insert/update/delete, 빈 Wiki, 기존 데이터 보존 및 다른 DB 파일 미생성.
- 결과: [db.py](../../backend/app/db.py)에 migration 17로 `wiki_documents`와 `wiki_documents_fts` 및 insert/update/delete trigger를 추가했다. 문서 revision은 양의 정수로 보관하며 조건부 갱신은 I03에서 구현한다. 별도 Wiki import나 Client API 연결은 없다. migration은 savepoint로 schema·버전 기록을 함께 처리하며 FTS 오류는 숨기지 않는다.
- 검증 결과: [test_integrated_wiki_db.py](../../tests/test_integrated_wiki_db.py)의 v15/v16 형태 업그레이드·반복, 기존 대화/작업/격리 이력 보존, 빈 Wiki, 실패 rollback, FTS 동기화·오류 rollback 및 revision 제약과 기존 회귀 포함 **116 passed**. 운영 DB는 접근하지 않았으며 실제 배포 후 startup 시 migration 17이 실행될 수 있다. 구버전 실행 차단과 namespace는 I02/I09에서 처리한다.

#### I02 [x] 저장소 출처 namespace 분리 — 로컬 저장소 범위 완료

- 의존: I01.
- 대상: [db.py](../../backend/app/db.py), [memory_repository.py](../../backend/app/memory_repository.py), [main.py](../../backend/app/main.py), 관련 테스트.
- 작업: 기존 job·record·quarantine을 `legacy_external`로 보존하고 신규 `integrated`와 분리한다. 참조·유일성·격리는 `(store_id, document_id)` 기준으로 설계한다. 기존 pending/unknown/active를 통합 active로 자동 승격하거나 재전송하지 않는다.
- 완료: 로컬 저장소의 기존 ID 참조와 활성·작업·격리 조회가 namespace로 분리되고 구 Wiki 이력이 보존된다. 아직 없는 통합 CRUD/RAG 경로의 전체 검증과 배포 제어는 각각 I03~I07, I09의 완료 조건으로 이관한다.
- 검증: 양 저장소 ID 1 충돌, 기존 active/unknown 이력, redaction·quarantine 및 반복 upgrade. pending 등 전체 전환 상태와 실제 연결 경로는 I04/I07에서 확인한다.
- 진행: [db.py](../../backend/app/db.py)의 migration 18은 기존 job·record·operation을 `legacy_external`로 보존하고 quarantine의 `(store_id, wiki_document_id)` 복합 키와 저장소별 unique/index를 만든다. record/operation·정정 대상의 namespace 불일치를 거절한다. [memory_repository.py](../../backend/app/memory_repository.py)는 namespace별 조회·claim·격리·redaction을 수행하고 이력 API/UI에 구 Wiki 표시를 추가했다. 기존 호출 호환을 위한 기본 namespace는 `legacy_external`이며 통합 호출은 `integrated`를 명시해야 한다.
- 검증: [test_memory_namespace.py](../../tests/test_memory_namespace.py)의 v17 반복 upgrade, 동일 문서 ID, 기존 active/unknown 및 새로운 claim, 서로 다른 namespace의 격리·redaction, 불일치 거절·migration rollback과 기존 회귀 포함 **119 passed**. 프런트엔드 테스트는 기존 Vitest 실행 파일 권한 문제 및 누락된 jsdom 파일로 실행되지 않았다. 패키지 설치는 하지 않았다.
- 후속 조건: 통합 CRUD/RAG 및 writer의 옛 참조 차단과 작업 상태 전환은 I03~I07에서, 구버전 실행을 차단하는 배포 제어는 I09에서 검증한다. 미실행된 이력 UI 회귀는 I05에서 반드시 실행한다. 현재 기본값은 구버전이 namespace를 이해한다는 보장이 아니며 구버전 writer 자동 복귀는 금지한다. migration 18은 배포 후 startup에서 실행될 수 있으며 운영 DB에는 적용하지 않았다.

#### I03 [x] 내부 Wiki repository CRUD·검색

- 의존: I01, I02.
- 대상: 신규 `backend/app/wiki_repository.py`, [wiki.py](../../backend/app/wiki.py), 신규 repository 테스트.
- 작업: 명시적 connection과 짧은 transaction으로 CRUD·FTS/부분 검색을 구현하고 revision 조건부 갱신을 원자화한다. 기존 Wiki 검색을 참고하되 FTS 오류는 숨기지 않고 요청과 결과를 제한한다.
- 완료: 동일 revision의 동시 수정 중 하나만 성공하고 나머지는 409에 대응할 수 있다. DB 연결을 잡은 채 네트워크·LLM 호출을 하지 않는다.
- 검증: 동시 수정·수동 편집 경합, 404/409, FTS 동기화, 특수문자·빈 검색·검색 오류, 연결 종료.
- I02 이관: 동일 숫자의 legacy 문서 ID로 통합 문서를 조회·수정·삭제하지 않도록 저장소 경계를 검증한다.
- 결과: [wiki_repository.py](../../backend/app/wiki_repository.py)는 호출자가 소유하는 SQLite connection과 transaction에서 통합 저장소 ID를 명시적으로 요구한다. 수정·삭제는 예상 revision을 원자적으로 비교하며 `WikiNotFound`/`WikiConflict`를 구분한다. FTS 최대 30개·부분 문자열 후보 최대 200개·반환 최대 20개로 제한하고 통합 namespace에서 격리된 문서를 검색에서 제외한다. FTS 오류는 전파한다. 동기 함수이므로 I04의 async 경로 연결 시 이벤트 루프 차단과 connection의 스레드 소유를 고려한다.
- 검증 결과: [test_wiki_repository.py](../../tests/test_wiki_repository.py)의 동시 수정, revision 충돌·미존재, 외부 ID 거절, namespace별 격리, FTS 오류·특수문자·경계와 호출자 transaction rollback 및 기존 관련 회귀 **124 passed**. 기존 성공 검색의 성능·정확도 대비, Alias, API 오류 매핑과 활성 기억 별도 주입은 I04/I06에서 검증한다. 운영 DB·서버를 변경하지 않았다.

#### I04 [x] 공용 API와 저장소 모드 전환 — Client 측 완료

- 의존: I03, W11.
- 대상: [main.py](../../backend/app/main.py), [wiki.py](../../backend/app/wiki.py), [config.py](../../backend/app/config.py), [test_wiki.py](../../tests/test_wiki.py), [test_api.py](../../tests/test_api.py).
- 작업: 기존 CRUD 성공 응답을 유지하고 통합 검색 API와 revision 조건부 갱신을 제공한다. 통합 feature flag는 기본 비활성이며 레거시/통합 모드는 각각 하나의 writer만 사용한다.
- 완료: 기존 Client 계약이 유지되고 오류를 빈 검색으로 바꾸지 않는다. 두 writer가 동시에 활성화되지 않는다.
- 검증: API 인증·기존 형식·빈 검색·404/409/422, 양 모드, 외부 HTTP/내부 DB 오류와 동시 writer 차단.
- I02 이관: 이전 pending/running/writing/unknown 작업을 통합 작업으로 실행·재전송하지 않는 전환과 API의 legacy 참조 차단을 검증한다.
- 진행: [config.py](../../backend/app/config.py)에 `wiki_store` 모드를 추가했다. 기본값은 `legacy_external`이며 `integrated`에서 [main.py](../../backend/app/main.py)의 CRUD·검색과 RAG 검색을 내부 repository로 보낸다. SQLite connection 생성·사용·종료는 같은 worker thread에서 처리한다. 통합 모드의 상세/수정/삭제는 `store_id=integrated`가 필요하고 삭제에는 `expected_revision`이 필요하다. PUT은 revision 또는 기존 `expected_updated_at` 조건을 하나의 transaction에서 검증한다. 격리 문서는 일반 목록·상세·검색에서 제외하고 수정·삭제도 차단한다.
- 자동 기억: 통합 모드의 pending 작업은 구 Wiki writer를 호출하지 않고 `integrated_memory_unavailable` 실패 이력으로 남긴다. 통합 기억 writer는 I07 전까지 미구현이며 old 프로세스·별도 Wiki 서버의 쓰기를 이 설정만으로 막지 못한다. 레거시 모드의 기존 동작은 유지한다.
- 검증: [test_wiki_mode_api.py](../../tests/test_wiki_mode_api.py)의 격리 DB 라우트 함수 직접 테스트와 기존 관련 회귀 **130 passed**. 통합/legacy 모드, 422/404/409/503, timestamp/revision, 관리 문서 격리, legacy pending 차단을 확인했다. 이 환경에서 asyncio thread 완료 후 루프가 깨어나지 않아 테스트는 `to_thread`를 동기 대체했다. ASGITransport 실제 요청 테스트도 대기 상태로 중단해 HTTP 인증·직렬화 및 실제 thread 스케줄링은 미검증이다.
- 후속 검증: [test_wiki_mode_http.py](../../tests/test_wiki_mode_http.py)를 정상 실행 환경에서 실제 ASGI 요청 및 worker thread로 실행해 인증, 빈 검색, 성공 응답, namespace·revision 요구, 404/409/422 및 동시 저장을 검증했다. 같은 revision의 두 수정은 하나만 commit되고 다른 하나는 409다. 관련 백엔드 회귀 **131 passed**. 위 130개 테스트의 inline thread 대체는 제거했다. 통합 모드에서 `store_id`와 삭제 revision 필수화는 구형 상세·삭제 요청과 호환되지 않으므로 I05 UI와 배포 전 호출자 확인에 반영한다.
- 남은 운영 조건: 구버전 Client/별도 Wiki 프로세스까지 포함한 단일 writer 제어는 I09 전환 시 검증한다. 구형 프로세스와 새 프로세스가 동시에 동작하지 않는다는 것을 이 설정만으로 보장하지 않는다. 운영 DB·서버·설정은 변경하지 않았다.

#### I04a [x] Wiki revision 이력과 diff API

- 이력 정책(구현 기준): 통합 문서별 최신 100개 revision의 제목·본문·카테고리 snapshot을 같은 Client DB에 보관하고 101번째 저장 시 가장 오래된 snapshot부터 삭제한다. 삭제된 문서의 이력은 문서 삭제 transaction에서 cascade 삭제한다. 출처 삭제 등으로 격리된 기억 문서는 이력·diff API에서도 숨긴다. 모든 이력은 현재 앱 접근자에게 공유되므로 예전 본문에도 정보가 남는다는 점을 I05 UI에 표시한다. 기존 통합 문서는 migration 시 현재 revision만 snapshot으로 만들며 없는 과거 revision은 복원하지 않는다. diff는 최대 총 4,000줄 및 UTF-8 응답 2 MiB를 넘으면 413으로 안내하고 잘린 diff를 완전한 결과로 표시하지 않는다.

- 의존: I03, I04의 API 계약. I05 전에 수행한다.
- 대상: [db.py](../../backend/app/db.py), [wiki_repository.py](../../backend/app/wiki_repository.py), [main.py](../../backend/app/main.py), 신규 `tests/test_wiki_revision_history.py` 및 API 테스트.
- 작업: Client SQLite에 문서별 revision의 제목·본문·카테고리와 시각을 보관한다. 생성·수정·이력 기록·FTS 갱신을 같은 transaction으로 처리하고 실패 시 함께 rollback한다. 이력 조회와 두 저장 revision의 diff를 인증된 opt-in API로 제공한다. 삭제된 문서와 출처 삭제·격리 기억에 대한 이력 접근은 동일한 격리 정책을 적용한다. 기존 문서의 초기 이력 생성은 통합 기능 활성 전 migration에서 검증하며 기존 외부 Wiki는 import하지 않는다.
- 완료: 첫 commit만 성공하고 후속 409에서 기존 저장은 바뀌지 않는다. 저장된 두 revision의 차이를 확인할 수 있으며 현재 revision 숫자만으로 과거 내용을 추정하지 않는다. 사용자 초안은 서버가 자동 저장하지 않는다.
- 검증: 동시 수정 1건 성공·1건 409, 이력과 FTS rollback, 제목/본문/카테고리 diff, 없는 revision·다른 문서 ID·권한 오류, 한글·긴 본문, 삭제/격리·민감한 이전 본문의 노출 방지, 반복 migration. 이력 저장량과 응답 크기를 제한하고 정확한 보존·삭제 정책을 구현 전에 문서화한다.
- 결과: [db.py](../../backend/app/db.py)의 migration 19는 기존 통합 문서의 현재 revision만 snapshot으로 기록하고 이후 문서 생성·revision 갱신을 trigger로 기록한다. 최근 100개만 유지하고 문서 삭제 시 이력은 cascade 삭제된다. [wiki_repository.py](../../backend/app/wiki_repository.py)와 [main.py](../../backend/app/main.py)는 인증된 저장 revision 목록·diff를 제공하고 read snapshot 안에서 격리·삭제 여부를 확인한다. 첫 commit 우선 CAS는 I04의 기존 구현을 유지한다.
- 검증 결과: [test_wiki_revision_history.py](../../tests/test_wiki_revision_history.py)의 migration 반복/실패 rollback, 한글 제목·본문·카테고리 diff, 100개 상한, 삭제·격리 및 인증, diff 상한 HTTP 413과 기존 HTTP 동시성·기억 회귀를 확인했다. 관련 전체 회귀 **136 passed**, 이후 추가 HTTP 413 검증 포함 이력 테스트 **5 passed**. 실제 운영 DB·Wiki는 변경하지 않았고 배포 후 startup에서는 migration 19가 실행될 수 있다. 미저장 초안 대 최신본 비교 UI는 I05에서 구현한다.

#### I05 [x] 공유 Wiki 문서 관리 UI

- 의존: I04, I04a.
- 대상: [main.tsx](../../frontend/src/main.tsx), [api.ts](../../frontend/src/api.ts), 신규 `frontend/src/components/WikiManager.tsx`, 스타일과 frontend 테스트.
- 작업: 공유 안내와 문서 목록·생성·편집·삭제·검색을 제공한다. revision 이력과 저장된 버전 간 diff를 보여주고, 충돌 시 미저장 초안과 최신본의 차이를 보여준다. 기존 Wiki AI 질문은 Client 채팅의 Wiki 검색을 이용하도록 이동 안내한다.
- 완료: 가족 공유 범위가 표시되고 저장 실패·revision 충돌 시 사용자의 편집 내용이 보존된다.
- 검증: React 테스트·접근성·키보드·모바일/데스크톱, 409 draft 보존·최신본/초안 diff·재편집 후 명시적 재저장, 과거 revision diff, 중복 클릭, 인증/오류와 안전한 렌더링.
- I02 이관: 미실행된 Wiki 기억 이력의 legacy 출처 표시 회귀를 실행하고 결과를 기록한다. 기존 환경의 jsdom 누락 문제도 해결하거나 미실행으로 명시한다.
- 진행: [WikiManager.tsx](../../frontend/src/components/WikiManager.tsx)에 공유 안내·문서 목록/검색·생성/편집/삭제, 저장된 revision diff, 충돌 시 최신본과 초안 비교 및 명시적 재저장을 추가했다. [main.tsx](../../frontend/src/main.tsx)에서 공유 Wiki 화면으로 이동하며 화면 전환 시 컴포넌트를 유지해 초안을 보존한다. 기본 legacy 모드에서는 기존 `/wiki/` 링크를 제공하고 통합 문서 쓰기는 하지 않는다. [main.py](../../backend/app/main.py)에 인증된 모드 확인 API를 추가했다.
- 검증 상태: [WikiManager.test.tsx](../../frontend/tests/WikiManager.test.tsx)에 생성·검색·충돌과 초안 유지·이력 diff·삭제·legacy 모드 회귀와 [WikiMemoryStatus.test.tsx](../../frontend/tests/WikiMemoryStatus.test.tsx)에 구 Wiki 표시 회귀를 작성했다. 서버 mode API 관련 테스트 **8 passed**. 기존 frontend 의존성의 `jsdom` 파일과 TypeScript `lib.es2022.d.ts` 등이 누락돼 Vitest와 TypeScript 컴파일이 시작되지 못했다. frontend 테스트·lint·build·네 viewport는 아직 미검증이다. 의존성 복구 없이 I05 완료로 표시하지 않는다.
- 후속 검증: 사용자가 의존성 설치를 허용해 `frontend`에서 lockfile 기준 `npm ci`로 복구했다. 프런트엔드 전체 테스트 **23 passed**, `npm run lint`, `npm run build` 통과. 메모리 내 모의 API와 로컬 정적 서버에서 1280×800·768×1024·390×844·320×568의 문서 입력 UI와 가로 넘침 여부를 확인했다. 실제 운영 Wiki에 대한 저장·삭제나 운영 데이터는 사용하지 않았다. 테스트 서버는 종료했다.

#### I06 [x] Alias 검색·RAG 통합

- 의존: I03, I04.
- 대상: [Alias 계획](wiki-query-alias-plan.md), 신규 `backend/app/wiki_query.py`, 내부 Wiki repository, [wiki.py](../../backend/app/wiki.py)와 테스트.
- 작업: 명시 Alias 정확 일치 → FTS → 제한된 확장 순서로 검색하고 문서 ID 중복을 제거한다. 모든 검색 단계에 격리 필터를 적용한다. 빈 Wiki에 문서나 Alias를 자동 생성하지 않는다.
- 완료: 아래 현재 단계 명세와 격리 테스트를 충족한다. 기존 API 응답 형식과 문서 데이터를 보존한다.
- 검증: 아래 현재 단계 검증 범위에 한정한다. 검색 후보와 실제 답변 근거의 구분은 I08에서 완료한다.
- I02 이관: legacy 문서 ID가 통합 문서와 같아도 검색·RAG 및 격리 필터에서 서로 섞이지 않는지 검증한다.
- 구현: [wiki_query.py](../../backend/app/wiki_query.py)의 제한된 표기 정규화와 독립 질문 처리를 통합 RAG에 연결했다. [db.py](../../backend/app/db.py)의 추가 migration 20은 기존 SQLite에 명시 Alias 테이블을 추가한다. [wiki_repository.py](../../backend/app/wiki_repository.py)는 제목/별칭 정확 일치, 기존 FTS·부분 문자열, 결과가 없을 때 제한 정규화 확장 순서로 검색하고 각 단계에서 통합 격리를 적용한다. 별칭 충돌 후보는 `alias_ambiguous`로 표시하고 [main.py](../../backend/app/main.py)가 모델 답변 대신 대상 확인 질문을 반환·저장한다. [WikiManager.tsx](../../frontend/src/components/WikiManager.tsx)에서 별칭을 명시적으로 조회·등록하며 revision 충돌 시 입력을 보존한다. 문서나 별칭을 자동 생성하지 않는다.
- 검증: 임시 DB에서 표기 네 가지·한국어 질문, 숫자 유사어, 별칭 충돌·삭제·revision 충돌, namespace 격리 및 독립/후속 질문을 검사했다. 기존 Wiki/기억 회귀 **48 passed**, 추가 검색·namespace 회귀 **25 passed**, 별칭 인증·검색 HTTP **1 passed**. 프런트엔드 전체 **24 passed**, lint/build 및 `git diff --check` 통과. 운영 DB와 서비스는 변경하지 않았다.
- 완료 검증: [test_wiki_alias.py](../../tests/test_wiki_alias.py), [test_wiki_mode_api.py](../../tests/test_wiki_mode_api.py), [test_api.py](../../tests/test_api.py), [test_prompting.py](../../tests/test_prompting.py) 및 기존 Wiki·기억 회귀 **97 passed**. 통합/legacy 동일 ID 격리, 201개 초과 문서의 오래된 명시 별칭, 제목 변경 및 FTS, 유사 숫자, 오류·특수문자, 확인 질문의 stream/nonstream 저장과 자동 기억 미생성, 4K/16K 예산을 격리 환경에서 검사했다. 이전 frontend 전체 **24 passed**, lint/build 통과. 운영 DB·서비스·모델 호출은 수행하지 않았다.
- 소규모 비교: 문서 네 개, 대표 기존 성공 질의 네 개에서 각 질의의 관련 문서 Top3 및 무관 결과 집합이 이전 FTS+부분 문자열 순위와 같았다. 같은 로컬 임시 DB에서 질의별 25회 반복한 중앙값은 이전 **0.095 ms**, 현재 **0.226 ms**였다. 매우 작은 warm fixture의 참고 수치이며 BC-250 운영 성능·대규모 Recall이나 실제 답변 품질을 보장하지 않는다.

##### I06 현재 단계 명세 — 가족 공유 Wiki의 소규모 검색

- 우선순위: 이 명세가 I06의 실제 범위와 완료 기준이다. [Alias 계획](wiki-query-alias-plan.md)의 전체 A01~A09 및 수치 성능 기준은 상세 참고와 향후 평가 항목으로 보존하며, 여기서 명시하지 않은 기준을 I06 완료의 추가 차단 조건으로 적용하지 않는다.
- 목표·제한: 기존 SQLite FTS5와 명시 별칭을 이용하는 소규모 공유 Wiki 검색이다. 자연어 의미 검색, 범용 한국어 형태소 분석, 자동 별칭 생성, 모든 질문의 정확한 검색·답변은 보장하지 않는다. 새 검색 서비스·벡터 DB·상주 모델은 도입하지 않는다.
- 검색: 등록된 별칭은 문서 전체를 대상으로 정규화 key의 인덱스에서 찾는다. 제목의 정규화 정확 비교와 부분 문자열 보조 검색은 최근 200개로 제한하고, 그 밖의 문서는 기존 FTS 결과에 의존한다. 200개 밖 문서의 표기 변형을 반드시 찾으려면 별칭을 명시 등록한다. FTS 후보 30개, 반환 최대 20개, RAG 최대 3개와 기존 발췌·context 예산을 유지한다. 현재 제한은 검색 대상 총 문서 수의 상한이 아니며 모든 문서의 제목 정규화 검색을 보장하지 않는다.
- 충돌: 정확 별칭에 둘 이상의 표시 가능한 문서가 일치하고 질문만으로 대상을 구분할 수 없는 경우 단일 문서를 임의 확정하지 않는다. 모델 지시만으로 보장하지 말고 애플리케이션에서 대상 확인 응답을 제공하는 경로를 구현·검증한다. 사용자 확인 이후 문서를 선택하는 완전한 대화형 흐름은 후속으로 두며, 안전하게 구분할 수 없으면 다시 질문한다.
- 안전·호환: 유사 번호 오일치 방지, 모든 단계의 통합 격리, legacy ID와 namespace 분리, FTS 오류 노출, 기존 list 응답 및 수동 문서·별칭의 revision 충돌 보호를 유지한다. 별칭을 자동으로 만들거나 기존 문서를 수정하지 않는다. 기존 legacy 모드를 롤백 경로로 유지하고 별도 검색 단계별 설정 스위치는 필수 범위에서 제외한다. 구버전 복귀와 변경된 데이터 보존의 운영 절차는 I09에서 검증한다.
- 필수 검증: 임시 DB에서 A01·A02 표기와 질문, 유사 숫자, 명시 별칭 충돌 및 대상 확인, 제목 변경·삭제·별칭 갱신 충돌, FTS 오류, 특수문자, 격리와 namespace, 독립/후속 질문, prompt 예산 및 기존 API 회귀를 확인한다. 200개를 넘는 fixture에서 제한 동작과 오래된 문서의 명시 별칭 검색을 검증한다. 고정 소규모 fixture의 대표 기존 성공 질의와 무관 결과를 변경 전후로 비교하고 결과를 기록한다. 측정은 같은 환경의 간단한 지연 비교로 이상 여부를 확인하며 실제 운영 SLA나 p95 성능 보장을 주장하지 않는다.
- 후속: 오렌지파이 클러스터 구축 후 Elasticsearch 등 검색 기술의 필요성, 운영 비용, 문서 동기화·보안·복구, 검색 품질·성능 평가를 별도 설계한다. 특정 제품의 도입은 확정하지 않는다. 의미 검색, 더 큰 문서 집합의 전체 제목 정규화 인덱스, 복잡한 질의·충돌 해소, 단계별 기능 스위치와 실제 하드웨어 부하 평가는 그때 재검토한다. I08의 retrieved/provided/cited 구분과 I09 운영 전환 기준은 그대로 유지한다.

#### I07 [x] 자동 기억 원자 쓰기와 충돌 확인

- 의존: I02~I04, I06, W10.
- 대상: [memory_repository.py](../../backend/app/memory_repository.py), [memory_dedup.py](../../backend/app/memory_dedup.py), [main.py](../../backend/app/main.py), [WikiMemoryStatus.tsx](../../frontend/src/components/WikiMemoryStatus.tsx), backend/frontend 테스트.
- 작업: 후보 검증 뒤 문서·FTS·활성 기억·job/idempotency를 한 transaction으로 처리한다. 충돌 시 기존/새 값을 보여주고 승인에 대상·revision·후보·출처를 결합한다. 취소·무응답은 보류한다.
- 완료: 명시적 정정 또는 유효한 확인만 갱신 후보로 연결하며 저장 결과 확인 전 완료로 표시하지 않는다. 구형 위험 writer로 자동 fallback하지 않는다.
- 검증: 동시 claim·중복/오래된 승인·출처 삭제·rollback·commit 후 응답 유실·재시작·결과 조회.
- I02 이관: 기존 외부 active/unknown 작업이 통합 기억 활성화·쓰기·재전송의 대상이 되지 않는지 검증한다.
- 2026-11-13 진행: [integrated_memory.py](../../backend/app/integrated_memory.py)에 미연결 초기 생성 primitive를 추가했다. 완료된 대화의 통합 저장소 user 원문을 제한된 네 속성으로 재검증하고 한 transaction에서 문서·FTS·격리·기억·operation·job 결과를 저장한다. 동일 operation은 요청 hash와 출처가 일치할 때만 확정 결과를 재조회한다. 기존 활성 슬롯, 미해결 operation, legacy job은 거절한다. 이 함수는 API나 백그라운드 writer에 연결되지 않았으며 운영 저장 동작은 활성화하지 않았다.
- 검증: [test_integrated_memory.py](../../tests/test_integrated_memory.py)에서 임시 DB 초기 저장·동일 요청 동시 실행·응답 유실 이후 재조회·중간 실패 전체 rollback·충돌 차단·출처 삭제 및 legacy unknown 분리를 확인하고 기존 기억·namespace·Wiki 회귀 포함 **67 passed**. 운영 DB와 서비스는 변경하지 않았다.
- 추가 진행: 같은 값은 기존 출처를 유지하며 중복 처리하고 다른 값은 확인 후보로 영속화한다. 승인 시 기존 record·revision·문서와 양쪽 출처·후보 fingerprint를 검증한 뒤 새 문서·FTS·활성 기억 전환·operation·job을 한 transaction으로 처리한다. 확인은 24시간 뒤 변경을 거절하며, 기한이 지났더라도 기존 값 유지는 선택할 수 있다. 인증된 확인 API와 [WikiMemoryStatus.tsx](../../frontend/src/components/WikiMemoryStatus.tsx)의 변경/유지 UI를 추가했다. 반복된 동일 선택은 결과를 재조회하고 반대 선택은 거절한다.
- 추가 검증: 임시 DB에서 중복·승인/유지·서로 다른 동시 결정·원문/revision 변경·기한 만료·승인 저장 실패 rollback과 HTTP 인증/대화 소속 및 재시도를 확인했다. 백엔드 관련 회귀 **85 passed**, 프런트엔드 전체 **25 passed**, lint/build 통과. 실제 운영 DB나 서비스는 변경하지 않았다.
- 최종 구현: [main.py](../../backend/app/main.py)가 신규 job의 저장소를 기록하고 통합 worker를 실행한다. [integrated_memory.py](../../backend/app/integrated_memory.py)는 최초 생성·중복·충돌을 구분하고, `정정:` 또는 `수정:`으로 시작하는 지원된 전체 문장만 명시 정정으로 처리한다. 그 밖의 모호한 정정·개인 기억은 자동 저장하지 않는다. 원문 전체 hash와 대상·revision을 승인에 결합하며 재시작 시 통합 pending 및 명시 정정 중단 작업만 안전하게 처리한다. 기존 외부 Wiki job은 통합 worker가 처리하지 않는다.
- 활성 기억은 통합 RAG에서 명시된 BC250 기기와 지원 속성에 한해 별도 채널로 조회한다. 출처·fingerprint·Wiki revision과 격리를 검증하고 실패한 항목은 주입하지 않는다. 일반 Wiki 문서와 구분하며 검색 결과를 실제 답변 근거로 검증하는 I08 작업은 남는다. 표현 범위가 좁아 지원하지 않는 자연어 질문에는 기억이 조회되지 않을 수 있다.
- 최종 검증: 임시 DB에서 메시지 완료→worker 생성, 중복·충돌·명시 정정, 재시작 복구·기존 외부 active/unknown 분리, 변경된 원문/대상·만료·동시 승인·rollback과 모의 LLM에 대한 기억 주입을 검증했다. 관련 회귀 **97 passed**, 추가 통합 기억 테스트 **26 passed**, frontend 전체 **25 passed**, lint/build 통과. 전체 backend 실행은 **1025 passed, 3 skipped, 5 failed**였으며 5건은 자식 프로세스의 모듈 경로 미설정 때문이었다. `PYTHONPATH=.`로 해당 5건을 재실행해 모두 통과했다. 전체를 경로 설정 후 재실행한 결과는 아니다. 운영 DB·서비스·배포는 변경하지 않았다.

#### I08 [x] 출처·UI·부하 통합 검증

- 의존: I05~I07.
- 대상: [main.py](../../backend/app/main.py), [db.py](../../backend/app/db.py), [prompting.py](../../backend/app/prompting.py), [api.ts](../../frontend/src/api.ts), [main.tsx](../../frontend/src/main.tsx), 관련 backend/frontend 테스트, [wiki-search-evaluation.md](wiki-search-evaluation.md).
- 작업: retrieved/provided/cited 구분, 기억 이력·polling 및 토큰/큐 제한을 구현·검증한다. 기존 W16~W19의 상세 검증은 §9에서 참고한다.
- 완료: 검색 후보를 실제 답변 근거로 오표시하지 않고, UI·검색·기억·비용 평가 결과와 미검증을 기록한다.
- 검증: 미전달 문서 인용·비활성 기억 제외, 4K/16K, 모델 전환·동시 요청, 네 viewport, frontend lint/test/build 및 관련 backend 통합 테스트.
- 진행: [wiki_evidence.py](../../backend/app/wiki_evidence.py)와 migration 21로 명시 opt-in 요청의 검색 후보·실제 payload에 포함된 Wiki 발췌·공유 기억 참조·`[wiki:ID]` 인용을 구분해 답변 메시지에 별도로 저장한다. 전달되지 않은 ID는 유효 인용으로 표시하지 않고, ID 일치만 확인하며 사용 여부나 내용 일치의 증거로 주장하지 않는다. 기존 요청과 기존 답변은 그대로 유지한다. 새 UI의 Wiki 검색 요청은 opt-in하며 상세 출처 카드를 표시한다. 공유 기억 값은 출처 metadata에 복제하지 않는다.
- 진행: 기존 전체 기억 이력 API는 유지하고 `view=summary` 및 대화별 단건 상세 API를 추가했다. UI는 요약만 조회하고 사용자가 선택할 때 상세 내용을 가져온다. 비활성 탭에서는 polling을 중지하고 in-flight 중첩을 막으며, 완료 이벤트·탭 복귀 시 다시 조회한다. 공유 기억만 전달된 경우의 참고 문구도 구분했다.
- 검증: 임시 DB·mock LLM에서 미전달 ID 인용, payload 제외, 응답 생성 시점 문서 revision·발췌 보존, migration 반복·메시지 삭제, stream/nonstream·재조회·다음 턴 본문 분리, 기존 API 호환과 대화별 상세 접근을 확인했다. 관련 회귀 **70 passed**, 추가 출처 테스트 **5 passed**, frontend **26 passed**, lint/build 통과. 운영 DB·서비스·모델은 사용하지 않았다. 배포 후 startup에서는 additive migration 21이 실행될 수 있다.
- 추가 검증: [WikiPolling.test.tsx](../../frontend/tests/WikiPolling.test.tsx)의 fake timer로 비활성 탭·지연된 요청 중복 방지·탭 복귀 및 이전 대화의 늦은 응답 배제를 확인했다. **2 passed**.
- 추가 진행·검증: Wiki 발췌가 입력 예산에 맞지 않으면 뒤쪽 후보부터 제외하며 실제 payload에 남은 발췌만 제공 출처로 기록한다. 모의 4K/16K profile에서 제외된 문서의 인용이 유효 처리되지 않음을 포함해 **5 passed**. 로컬 mock API와 프런트엔드에서 1280×800·768×1024·390×844·320×568 출처 카드와 가로 넘침을 확인했다. 운영 서버·실제 모델은 사용하지 않았다.
- 최종 검증: polling 네트워크 오류·늦은 작업 등록 포함 **3 passed**. 통합 기억은 단일 worker가 최대 32개씩 읽고 재시작 대기 작업 35개를 빠짐없이 처리하며 추가 LLM 추론은 없다. 모델 전환과 token budget 관련 격리 테스트 **30 passed**. 고정 자료 품질·검색 p50/p95 및 이력 전송량은 [평가 기록](wiki-search-evaluation.md)에 수치와 한계를 기록했다. 전체 backend는 `PYTHONPATH=.`로 **1043 passed, 3 skipped**, 최종 worker/인용 보완은 별도 집중 테스트로 확인했다. 프런트엔드 전체 **29 passed**, lint/build 통과.
- 한계·롤백: 실제 BC-250 토크나이저·GPU/foreground 부하, 실제 모델의 인용 정확도와 의미적 근거 일치는 미측정이다. I09 리허설에서 실행 버전·DB 백업·단일 writer와 역전환을 확인한다. 문제가 생기면 통합 writer와 새 출처 opt-in을 비활성화하고 호환 코드를 복구하되, migration 21과 생성된 출처 데이터·대화는 삭제하지 않는다. 운영 전환·서비스 재시작은 수행하지 않았다.

대화의 긴 자료를 공유 Wiki 문서로 저장하는 후속 개선은 별도 [대화 자료 Wiki 저장 실행 계획](wiki-conversation-document-tasks.md)의 D00~D05에서 진행한다. 기존 단일 속성 자동 기억과 운영 전환 단계는 유지한다.

#### I09 [진행 중] 운영 전환 준비와 리허설

- 의존: I08.
- 대상: 이 문서, [통합 설계](wiki-client-consolidation-plan.md), [README](../../README.md), 조사 후 확정한 배포 설정.
- 작업: 백업, 외부 호출자, 단일 writer 전환·서비스 종료·역전환 절차를 작성하고 격리 복제 환경에서 리허설한다. 전환 후 생긴 자료는 구 Wiki 복귀 시 보존·조정한다.
- 완료: 운영에서 실행할 단계와 실패 시 읽기 전용 복구 방안을 검토할 수 있다.
- 검증: 구 writer 차단, 빈 Wiki·ID 충돌·이력 보존, 장애 복구·양쪽 데이터 보호·재가동 점검.
- I02 이관: namespace를 이해하지 못하는 구버전 프로세스와 writer의 실행·자동 복귀를 차단하고 리허설로 증명한다. 구버전이 schema 버전을 검사한다고 가정하지 않는다.

##### 현재 확인된 배포·운영 경계

- 2026-11-13 읽기 전용 SSH 확인: `bc250-llm-client.service`와 `bc250-family-wiki.service`는 모두 `active`, `enabled`; 8010과 8020은 각각 loopback에서 수신 중이었다. 두 서비스 모두 `yeppyshiba`로 실행하고 `Restart=on-failure`다. Client의 working directory는 `/opt/bc250-llm-client/backend`, 환경 파일은 `/etc/bc250-llm-client.env`, `/etc/bc250-model-control/client.env`, `/etc/bc250-model-control/client-activation.env` 순서로 표시됐다. 이 관찰은 당시 상태이며 이후 변경 가능하다.
- 같은 조사에서 Client PID 153606, Wiki PID 1007이었고 디스크의 Client `main.py` SHA-256은 `1a70d464d8dcaf803273cdbc89e616bf2d5f072a48c1b18f4419970f4c76506d`, Wiki `main.py`는 `20889a43f3f749854ef9beace60a31fb18c96afb34b782120f2e9d4a07c19953`이었다. `/etc/caddy/Caddyfile`에는 `/llm/api/*`→8010, `/wiki/*`→8020이 있다. 파일 지문·설정 파일 관찰은 실행 프로세스의 로드 코드나 실효 Caddy 설정과 같다는 증명이 아니다.
- 운영자가 읽기 전용으로 확인한 Client PID 153606의 환경에는 `DATABASE_PATH=/var/lib/bc250-llm-client/bc250-llm-client.db`가 있으며 `WIKI_STORE`는 없다. 작업 디렉터리의 `.env`는 없고, 배포된 설정 파일에서 `wiki_store` 필드는 검색되지 않았다. 이는 기존 외부 Wiki 방식의 배포본이라는 정황이지만 디스크 파일과 실행 프로세스의 코드 일치 및 실제 DB 연결의 증명은 아니다. 현재 작업 공간 코드의 기본값은 `legacy_external`이며 운영에 적용됐다고 단정하지 않는다. Wiki 서버 소스는 DB 경로를 `/opt/bc250-family-wiki/data/wiki.db`로 지정한다. 실행 프로세스와 파일이 같은 버전이라는 보장은 없다.
- 운영자 추가 확인: 설정된 Client DB 파일이 존재하고 PID 153606에서 열려 있었다. 조회 시 WAL 파일은 없었다. 사용자는 현재 배포본이 통합 전 버전임을 확인하고 버전 추가 조사는 불필요하다고 했다. Client와 기존 Wiki 중단 시 Studio·gate 영향 확인이 우선이다. 기존 Wiki DB 폐기 가능성과 Client 대화 DB 삭제는 별도로 취급한다.
- 이전 모델 전환 조사와 Studio 인계 스냅샷에서는 Client가 gate를 호출하고 Studio와 gate는 별도 서비스였으며, 별도 Wiki는 llama 8080을 직접 호출했다. 이는 과거 관찰과 템플릿이고 현행 systemd의 역의존성, 실행 중 이미지·모델 전환 작업과 gate hold 상태를 보장하지 않는다. gate·Studio·llama를 Client 또는 Wiki와 함께 중지하지 않는다.
- 운영자 제공 systemd 조회: Client, Family Wiki, Studio, gate, llama 모두 active/running이고 systemd job은 없었다. 다섯 unit의 `BindsTo`, `PartOf`, `PropagatesStopTo`, `StopPropagatedFrom`은 비어 있고 두 중단 대상의 역의존성 출력에는 `multi-user.target`만 있었다. 확인 시점의 systemd 관계에서 Client/Wiki 중단이 Studio·gate·llama 중단으로 전파되는 관계는 발견되지 않았다. 이는 Studio 이미지·모델 전환 작업, gate hold, 응용 계층 호출자 부재 또는 이후 상태의 증명은 아니다. Client 대화·기억 작업과 기존 Wiki 요청의 중단 영향은 별도로 확인한다. 서비스 중지는 실행하지 않았다.
- 운영자가 기존 Family Wiki를 중지하고 자동 시작을 해제했다. 결과는 `inactive`, `disabled`, Studio·gate·llama는 각각 `active`였다. 이후 운영자는 기존 Wiki 삭제 및 Caddy 정리를 요청했다. Caddy의 실제 구성 범위와 unit 파일·drop-in 경로를 확인한 뒤 다른 경로를 보존하도록 정리한다. Client 대화 DB는 삭제 대상이 아니다.
- 운영자 제공 설정: Wiki unit은 `/etc/systemd/system/bc250-family-wiki.service`, 작업 디렉터리는 `/opt/bc250-family-wiki`, drop-in은 공통 `/usr/lib/systemd/system/service.d/10-timeout-abort.conf`다. Caddy의 독립 `/wiki` redirect 및 `/wiki/*` reverse proxy가 제거 대상이며 `/llm`, `/api` 및 Studio fallback은 유지한다. 공통 drop-in은 삭제하지 않는다. 운영자가 정리 결과를 보고하기 전 실제 삭제·Caddy 반영 여부는 미확인이다.
- 운영자가 Caddyfile을 직접 수정하고 Caddy를 reload했다. `caddy validate`는 유효하다고 보고했으며 Caddy·Client·Studio·gate·llama는 모두 active였다. 수정된 설정의 실제 내용은 아직 재확인하지 않았다. Family Wiki는 inactive/disabled지만 unit은 여전히 로드되어 있고, Wiki 디렉터리와 unit 삭제 실행은 아직 확인되지 않았다.
- 이후 운영자가 `/opt/bc250-family-wiki`와 전용 unit 파일을 삭제하고 daemon-reload했다. `LoadState=not-found`와 디렉터리 부재를 확인했다. 이 디렉터리의 Wiki DB도 삭제된 것으로 취급하며 별도 백업 존재 여부는 미확인이다. 기존 Wiki 복구를 롤백의 전제로 삼지 않는다. Client DB와 구 Client는 유지된다.
- 운영자 보고로 GitHub Actions 배포를 완료했다. 2026-11-13 터널 브라우저 조회에서 Client 화면과 `/api/v1/health` 200, 인증 헤더를 포함한 Wiki capabilities 200 (`store_id=integrated`, `revision_history=true`), 문서 목록 200 빈 배열, `bc250` 검색 200 빈 배열 및 공유 Wiki 화면을 확인했다. 최초 인증 헤더 없는 직접 조회의 403은 local 인증 정책에 따른 것이며 헤더 추가 후 정상 응답했다. 운영 데이터 작성·기억 저장·LLM 답변·revision 충돌·재시작 후 지속성과 기존 대화 보존은 아직 검증하지 않았다. Client 구버전 재배포는 운영자 절차로 방지한다.
- 읽기 전용 `sudo -n -l`에서 SSH 계정의 무암호 권한은 Client 서비스 restart와 별도 Studio 배포 wrapper로 제한됐다. 환경 파일의 root 읽기나 Wiki unit 제어가 무암호로 허용된다는 근거는 없다. 이 조사에서 해당 restart나 wrapper는 호출하지 않았다.
- [deploy.yml](../../.github/workflows/deploy.yml)은 `main` push와 수동 실행에서 checkout된 [deploy-release.sh](../../deploy/deploy-release.sh)를 SSH로 실행하고 서비스를 재시작한다. 따라서 `WIKI_STORE=legacy_external`이어도 새 Client가 시작되면 migration 17~21이 실행될 수 있다. **단순 push도 운영 DB 변경을 유발할 수 있으므로 I09 차단 조건 해소 및 I10 승인 전 이 변경을 운영에 배포하지 않는다.** `/wiki/` 및 기존 8020 API, `/api/ask` 직접 8080 호출과 외부 호출자의 현재 사용 여부는 완전히 확인되지 않았다. 유휴 연결 목록은 호출자 부재의 증거가 아니다.
- 이 작업에서 최신 배포 스크립트의 파일 교체 후 실패 시 **구버전 자동 복원·재시작을 제거**하고 이전 backend/dist와 배포 archive를 `.deploy.*`에 보존하도록 했다. health는 인증이 필요 없는 `/api/v1/health`만 확인한다. 이는 프로세스 기본 응답 확인이지 저장소 모드·기능 정상성이나 단일 writer 보장이 아니다. 파일 교체 전 실패는 기존 프로세스를 재시작하지 않는다. 실패 후 새 프로세스의 실제 실행 여부는 별도 확인해야 한다. 성공 시 이전 임시 파일은 정리되므로 전환 전 별도 release 백업이 필요하다.
- 제한: workflow는 **checkout한 커밋의 스크립트**를 실행한다. 위 수정만으로 구 커밋의 workflow 재실행, 별도 배포 경로, 직접 `systemctl` 또는 서버 재부팅 시 구버전 시작을 차단할 수 없다. 기존 Wiki unit과 디렉터리는 운영자가 삭제했지만 별도 복구·재배포 경로는 미확인이다. Git HEAD의 과거 schema 16 초기화 코드는 상위 schema 버전 거절을 확인하지 않으므로 이를 안전장치로 가정하지 않는다.

##### I10 전환 실행안 — 지금 실행하지 않음

현재 운영 상태: 아래 절차의 별도 Wiki 중단·삭제는 운영자가 이미 수행했다. 삭제된 Wiki DB·release의 백업을 새로 만들거나 복원을 전제할 수 없다. Client DB는 유지하고 구 Client writer를 통제한다. 삭제된 Wiki의 복구는 별도 백업이 확인된 경우에만 검토한다.

운영자 결정: Client DB 백업을 생략한다. 전환 중 손상·실수 시 백업으로 기존 대화·기억을 복구할 수 없다는 제한을 수용한 운영 결정이며 Client DB를 삭제하라는 지시는 아니다. `WIKI_STORE` 설정과 구버전 재배포 방지는 운영자가 직접 관리한다. 구버전 실행 방지는 기술적으로 강제된 상태가 아니므로 구 workflow 재실행·옛 release 복원·재배포를 하지 않는 운영상 통제에 의존한다.

1. 운영자·사용자에게 Client 쓰기 중단을 알린다. `main` push·수동 재실행·예약/별도 배포와 구 release 재기동을 중단하고 진행 중 배포 및 관련 writer가 끝났는지 확인한다. 삭제된 Wiki를 사용하는 호출자가 있다면 종료 사실과 통합 Wiki 전환까지의 영향을 알린다.
2. 권한 있는 운영자가 Client DB 경로와 현재 설정·서비스 실행 파일·release hash·schema 버전을 확인한다. 값에 비밀이 들어갈 수 있는 전체 환경 파일이나 프로세스 환경은 출력하지 않는다. Client DB의 대화·기억·이력과 구 Client의 코드/설정을 보존한다. 기존 Wiki DB 삭제는 Client DB 삭제 승인이 아니다.
3. Client DB 백업은 운영자 결정으로 생략한다. 문제 발생 시 기존 대화·기억의 복구를 보장할 수 없으며 운영 DB를 초기화하거나 삭제하지 않는다.
4. 구 Client/Family Wiki writer와 구 release의 자동 복귀를 운영자가 root 관리 경계에서 차단한다. Caddy `/wiki` 쓰기뿐 아니라 8020 직접 접근, 구 프로세스·systemd 자동 재시작/재부팅·GitHub Actions의 구 커밋 재실행 및 다른 배포자를 모두 포함해야 한다. `systemctl stop` 한 번이나 최신 checkout 스크립트 수정만으로 지속 차단을 증명하지 않는다. 차단 후 실제 unit·PID·listen 경로와 배포 재실행 거절을 확인하고, 차단을 증명하지 못하면 전환하지 않는다.
5. 승인된 새 release·설정과 Client DB 경로를 확정한 뒤에만 통합 모드를 활성화한다. 초기화/migration 성공과 기존 대화·legacy 이력 보존, 빈 통합 Wiki, namespace가 다른 같은 문서 ID, 문서 저장·revision 충돌·검색·기억 확인 및 재시작 후 결과를 순서대로 확인한다. 테스트 작성·삭제는 운영 검증에 별도 승인된 범위에서만 한다. 이전 Wiki 문서의 수동 이관 여부는 별도로 정한다. 전환 후 생성 자료와 작업을 별도 백업한다.
6. 오류·불명 상태는 추가 writer 실행 없이 쓰기를 차단하고, 서비스/DB와 새로 생긴 자료를 보존한 뒤 원인과 마지막 commit 경계를 확인한다. 복구용 복제본은 읽기 전용으로 먼저 검사한다. **전환 전 DB 백업으로 운영 DB를 덮어쓰지 않는다.** 별도 Wiki 복귀가 필요하면 양쪽에서 생긴 자료의 보존·이관과 ID namespace 차이를 검토하고 별도 승인 후 수행한다. 구버전 자동 재시작 또는 `WIKI_STORE`만 바꾸는 것을 롤백으로 간주하지 않는다.

##### 격리 리허설 결과와 잔여 차단 조건

- [test_wiki_cutover_rehearsal.py](../../tests/test_wiki_cutover_rehearsal.py)는 임시 Client DB의 이전 schema→21 반복 초기화, SQLite backup API, 빈 통합 Wiki, 외부/통합 ID 1 충돌, 기존 대화·unknown 이력 보존, 통합 문서·기억 저장과 재개, 전후 백업의 읽기 전용 검사를 확인했다. 가짜 Wiki의 쓰기 차단은 절차 모델이며 **실제 systemd·Caddy·외부 호출자 차단의 증거가 아니다**.
- [test_wiki_deploy_guard.py](../../tests/test_wiki_deploy_guard.py)는 임시 디렉터리·가짜 systemctl/curl에서 정상 배포와 restart/health 실패를 재현했다. 실패 시 구 파일 보존과 구버전 자동 재시작 0회를 확인했다. 격리 DB·namespace·API 및 과거 버전 재기동 재현을 포함한 최종 회귀 **48 passed**. 운영 DB 백업·migration·서비스 제어·설정 변경·배포는 실행하지 않았다.
- 구버전 차단의 필요성을 실제 코드로 재현했다. Git commit `e85c8cf61b9538ce0d4d9a390a8fb87f0fba8674`의 schema 16 초기화 코드 [fixture](../../tests/fixtures/client_schema_v16.py)(SHA-256 `ff50b3b4be2e5a2ad7d9530b8c4a9ccd4bfbaa091e2994f0319e5d611b6b9ac4`)를 임시 schema 21 DB에 실행했을 때 상위 버전이라는 이유로 거절하지 않았다. 기존 코드는 `store_id` 없이 이력에 쓸 수도 있었다. 이는 구버전이 안전하게 동작한다는 뜻이 아니며 schema 숫자만으로 구 writer를 막을 수 없다는 근거다.
- **I09 미완료 / I10 차단:** Client DB 및 구 Client release의 백업·복원 가능성, 외부 호출자, 구 Client release의 재부팅/재배포 포함 지속 차단이 아직 증명되지 않았다. 별도 Wiki unit과 디렉터리는 삭제됐으며 Wiki 복구·재배포 경로는 미확인이다. 특히 과거 workflow 자체가 옛 스크립트를 실행할 수 있어 최신 스크립트 테스트만으로 구 writer 차단 완료를 주장하지 않는다. 실제 차단 장치의 구현·적용 방법과 격리 리허설을 확정한 뒤 운영 접근·실행은 I10 범위에서 수행한다.

##### 운영자 읽기 전용 확인 명령 — I09

서버에서 아래 명령은 실행 중인 Client 프로세스의 작업 디렉터리와 두 설정값만 출력한다. 전체 환경, API 키와 다른 비밀값은 출력하지 않는다. 서비스·설정·DB는 변경하지 않는다. 실행 프로세스에 설정이 없으면 `<프로세스 환경에 없음>`으로 표시하며, 이 경우 별도 환경 파일이나 `.env`의 영향을 확인하기 전 기본값을 유효값으로 단정하지 않는다.

```bash
sudo python3 - <<'PY'
import os
import subprocess
from pathlib import Path

pid = subprocess.check_output(
    ['systemctl', 'show', '--value', '-p', 'MainPID', 'bc250-llm-client.service'],
    text=True,
).strip()
if not pid.isdigit() or int(pid) == 0:
    raise SystemExit('Client 실행 프로세스를 찾지 못했습니다. 여기서 중단하세요.')
process = Path('/proc') / pid
environment = {}
for entry in (process / 'environ').read_bytes().split(b'\0'):
    key, separator, value = entry.partition(b'=')
    if separator and key in (b'DATABASE_PATH', b'WIKI_STORE'):
        environment[key.decode()] = os.fsdecode(value)
print('MainPID:', pid)
print('WorkingDirectory:', os.readlink(process / 'cwd'))
for key in ('DATABASE_PATH', 'WIKI_STORE'):
    print(f'{key}:', environment.get(key, '<프로세스 환경에 없음>'))
PY
```

이 명령은 현재 프로세스 환경만 확인한다. 실행이 거절되거나 값이 없을 때 환경 파일 전체를 공유하지 말고 해당 결과만 보고한다. DB 경로와 모드 확인은 구버전 writer 차단·백업·전환을 승인하거나 실행한 것을 뜻하지 않는다.

#### I10 [ ] 승인된 운영 전환과 기존 Wiki 종료

- 의존: I09 검증과 명시적인 운영 실행 권한.
- 대상: 실제 배포·서비스·데이터 경로는 I09에서 확정.
- 작업: 일시 쓰기 중단 → 통합 경로 활성화 → 기능 검증 후 기존 Wiki를 deprecated 처리한다. 의존 호출자가 없고 롤백 조건이 충족될 때 서비스 종료·기존 DB 삭제 시점을 별도 처리한다.
- 완료: 단일 writer, 기존 대화·이력 보존, 통합 Wiki와 기억 동작이 확인되고 기존 서비스 종료 여부가 기록된다.
- 검증: SSH/브라우저에서 생성·편집·검색·기억·충돌, 재시작 후 유지, 단계별 결과·버전·시각.

## 7. 실행 순서·검증·롤백

현재 권장 순서: 이 문서 §6의 I00 → I01 → I02 → I03 → I04 → I04a → I05 → I06 → I07 → I08 → I09 → I10. I08까지 격리 구현·검증하고 I09 리허설 후 운영 전환 I10을 진행한다. 아래는 방향 변경 전의 실행 이력이다.

이전 순서:

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

별도 Wiki 종료와 데이터 처리의 현재 조건은 [통합 전환 안전 계약](wiki-client-consolidation-plan.md#전환-안전-계약) 및 I09/I10을 따른다.

## 8. 실행 이력과 다음 실행 프롬프트

| 날짜 | Task | 상태 | 수정/검증 | 다음 단계 |
| --- | --- | --- | --- | --- |
| 2026-11-13 | I09 전환 준비 | 진행 중 | 운영 서비스·unit 읽기 전용 확인, 실패 시 구버전 자동 복귀 제거, 임시 전환·백업/실패 리허설. 구 schema 16이 21 DB를 거절하지 않는 사례 확인 | 운영 DB 경로·외부 호출자 및 지속적인 구 writer 차단 검증 |
| 2026-11-13 | I08 출처·UI·부하 | 완료 | 출처 분리·입력 예산·요약 polling·단일 worker, mock 네 viewport·고정 품질/비용 측정. backend 전체 1043 passed/3 skipped 및 집중 회귀, frontend 29 passed. 실제 BC-250 부하는 미측정 | I09 |
| 2026-11-13 | I08 출처·요약 이력 | 진행 중 | opt-in 출처 스냅샷과 카드, 기억 요약/상세 및 polling 개선. 관련 backend 70 passed, 출처 추가 5 passed, frontend 26 passed | polling·viewport·부하 및 품질 평가 |
| 2026-11-13 | I07 통합 worker | 완료 | namespace별 worker·정정·재시작 복구·검증된 공유 기억 주입 연결. 관련 회귀 97 passed, frontend 25 passed; 전체 실행의 하위 프로세스 경로 실패 5건 별도 재실행 통과 | I08 |
| 2026-11-13 | I07 충돌 확인 기반 | 진행 중 | 중복·후보·승인/유지 transaction, 24시간 확인 기한, 인증 API/UI. backend **85 passed**, frontend **25 passed**, lint/build 통과 | 자동 기억 분기 및 명시 정정 연결과 안전한 조회 검증 |
| 2026-11-13 | I07 원자적 초기 생성 기반 | 진행 중 | 미연결 transaction primitive와 임시 DB 회귀 **67 passed**. 자동 기억 writer는 계속 차단 | 충돌 승인·중복 및 복구 구현 후 통합 연결 |
| 2026-11-13 | I06 검색·RAG | 완료 | 제한된 I06 명세에 따라 충돌 확인 응답과 검색/격리 검증 완료. backend **97 passed**, 고정 소규모 fixture 비교 기록. 클러스터 및 고도화는 TODO | I07 |
| 2026-11-13 | I06 현재 단계 범위 | 진행 중 | 가족용 소규모 SQLite 검색의 한계·필수 검증과 클러스터 고도화 TODO를 확정. 기존 구현 검증 결과를 변경하지 않음 | I06 명세에 맞춰 충돌 처리와 나머지 격리 테스트 수행 |
| 2026-11-13 | I05 공유 Wiki UI 검증 | 완료 | 사용자 승인으로 frontend 의존성 복구. 전체 **23 passed**, lint/build 통과, 모의 API 네 viewport 확인. 운영 서버·DB 미변경 | I06 |
| 2026-11-13 | I05 공유 Wiki 화면 | 진행 중 | 문서 관리·충돌 초안 비교·이력 diff UI 및 mode API 작성. API 회귀 **8 passed**. frontend 의존성 불완전으로 테스트·컴파일·화면 미검증 | frontend 의존성 복구 후 I05 검증 |
| 2026-11-13 | I04a Wiki revision 이력·diff | 완료 | migration 19, 최근 100개 snapshot, 인증된 목록·diff 및 삭제/격리 차단. 관련 회귀 **136 passed** 및 추가 HTTP 413 테스트 통과 | I05 |
| 2026-11-13 | I04 HTTP 및 동시성 검증 | Client 측 완료 | 정상 실행 환경에서 실제 ASGI 요청·worker thread, 인증·응답·동시 revision 충돌 확인. 관련 백엔드 **131 passed**, 운영 writer 전환은 I09 | I04a |
| 2026-11-13 | I04 공용 API·모드 전환 | 진행 중 | 통합/legacy 모드 및 CRUD·검색 연결, 자동 기억 legacy writer 차단. 라우트 직접 회귀 포함 **130 passed**. ASGI HTTP·실제 worker thread 검증 미완료 | I04 HTTP/worker 검증 |
| 2026-11-13 | I03 통합 Wiki repository | 완료 | caller 소유 transaction의 CRUD·조건부 revision·bounded FTS/부분 검색·namespace 분리. 관련 backend 회귀 **124 passed**; 운영 경로 미연결 | I04 |
| 2026-11-13 | I02 저장소 namespace | 로컬 완료 | migration 18 및 저장소별 참조·격리, 이력 표시. backend 관련 **119 passed**; frontend jsdom 누락으로 미실행. 통합 경로·UI·구버전 차단 조건은 I03~I09에 명시적으로 이관 | I03 |
| 2026-11-13 | I01 통합 문서·FTS 스키마 | 완료 | Client SQLite migration 17, 빈 문서 테이블·FTS·trigger 추가. 임시 DB 업그레이드·롤백 및 기존 회귀 **116 passed**. 운영 DB 미접근 | I02 |
| 2026-11-13 | I00 전환 계약과 현황 확인 | 완료 | Client 작업 공간 코드와 기존 Wiki 읽기 전용 조사 이력 검토. 외부 호출·문서 ID·상태·메시지 출처, namespace 및 I01/I02 전환 계약 기록. 현재 운영 DB/호출자/실행 프로세스 버전은 미확인 | I01 |
| 2026-11-13 | 통합 방향 확정 | 계획 변경 | Client SQLite/FastAPI/React에 가족 공유 Wiki 통합, 별도 Wiki deprecated, 빈 Wiki 시작 허용, Client 이력 보존 및 namespace·단일 writer 전환 계약 작성. 문서만 변경 | I00 |
| 2026-11-12 | 계획 작성 | 완료(구현 아님) | Client 핵심 경로·스크린샷 분석. 문서만 작성. 운영/테스트 데이터 저장·테스트 실행 없음 | W01 |
| 2026-11-12 | W00 외부 Wiki 계약 조사 | 완료(소스 접근 차단 기록) | 작업 공간·접근 가능한 `/opt`·인접 workspace·Git 참조를 읽기 전용 조사. 예상 경로는 `/opt/bc250-family-wiki/app/main.py`이나 이 환경에는 `/opt`와 Wiki 소스가 없음. Client API 사용 계약과 과거 운영 조사 기록만 §2.3에 기록. 운영 endpoint/DB/서비스는 호출·변경하지 않음 | W01; W12~W14는 Wiki 소스 제공 전 차단 |
| 2026-11-12 | W01 스크린샷·기존 계약 회귀 | 완료 | R01/R02의 현행 비사실 원문 저장 경로를 fake Wiki·mock LLM·임시 SQLite로 재현하는 기준 테스트를 [test_wiki_memory.py](../../tests/test_wiki_memory.py)에 추가. 기존 원문/role/unknown/quick/명시 요청 보호 테스트 유지. 사용자 승인으로 프로젝트 전용 [`.venv/`](../../.venv/)에 [backend/requirements.txt](../../backend/requirements.txt)를 설치했고, 대상 pytest **12 passed** | W02 |
| 2026-11-12 | W02 사전 제외·저장 거부 | 완료 | [wiki_memory.py](../../backend/app/wiki_memory.py)에 좁은 사전 제외를 추가하고, [main.py](../../backend/app/main.py)에서 신규 작업 생성 전·legacy pending 작업 실행 전 적용. [test_wiki_memory.py](../../tests/test_wiki_memory.py)로 질문/거부는 Wiki·LLM 호출 0회, 혼합 발언은 후보 유지 확인. Wiki/기억 pytest **26 passed**, background-switch pytest **5 passed**, `py_compile`·diff 검사 통과 | W03 |
| 2026-11-12 | W03 구조화 분류·최소 원문 절 | 완료 | [wiki_memory.py](../../backend/app/wiki_memory.py)에 kind/reason/source message ID 계약과 요청형 원문·날짜 없는 계획 보류를 추가하고, [main.py](../../backend/app/main.py)에서 모델 입력·검증에 연결. R02 전체 추천 요청 거절과 최소 사실 절 허용, assistant ID 위조, 부정/계획 계약을 [test_wiki_memory.py](../../tests/test_wiki_memory.py)에 추가. 관련 pytest **37 passed**, `py_compile`·diff 검사 통과 | W04 |
| 2026-11-12 | W04 판정 메타데이터·호환 API | 완료 | [db.py](../../backend/app/db.py)에 nullable metadata migration 15, [main.py](../../backend/app/main.py)에 상태별 reason/attention 기록과 이력 API 필드를 추가. legacy row NULL 보존·반복 migration·이력 응답을 테스트. 관련 pytest **40 passed**, `py_compile`·diff 검사 통과. 전체 `test_api.py`는 장기 무출력으로 중단, frontend 의존성 부재로 lint/build 미실행 | W05 |
| 2026-11-12 | W05 기억 결과 UI 축소·상세화 | 완료(실행 검증 일부 미완료) | [WikiMemoryStatus.tsx](../../frontend/src/components/WikiMemoryStatus.tsx)로 기본 상태와 대화상자 상세를 분리하고, [WikiMemoryStatus.test.tsx](../../frontend/tests/WikiMemoryStatus.test.tsx)를 추가. Problems·diff 검사 통과. `node`/`npm` 부재로 Vitest·lint·build 및 브라우저 시각 검증은 미실행 | W06 |
| 2026-11-12 | W06 메시지 viewport·최신 이동·표 스크롤 | 완료(시각 검증 미완료) | [MessageList.tsx](../../frontend/src/components/MessageList.tsx)의 viewport 오버레이와 [MarkdownContent.tsx](../../frontend/src/components/MarkdownContent.tsx)의 표 스크롤 컨테이너를 추가. Problems·diff 검사 통과. `node`/`npm`, Client 서버, 브라우저 실행기 부재로 Vitest·lint·build·네 viewport 시각 검증은 미실행 | W07 |
| 2026-11-12 | W07 1차 통합 회귀·상태 기록 | 완료(Frontend/stream 시각 검증 미완료) | fake Wiki·mock LLM 이력 연결 [test_wiki_memory_integration.py](../../tests/test_wiki_memory_integration.py)를 추가하고 관련 backend 회귀 **41 passed**. snapshot/invalid epoch API 테스트 2건 통과. stale-epoch stream은 20초 무출력 시간 제한, frontend 실행·시각 검증은 환경 부재로 미실행 | W08 |
| 2026-11-12 | W08 기억 메타데이터·소유 범위 설계 | 완료(후속 차단 조건 명시) | §4.2에 SQLite DDL, 삭제·격리, 동시성·unknown·legacy 검색 계약과 시나리오 표를 작성. predicate 사전 미확정으로 W09 claim 완료 판정 차단; Wiki 원자성·검색 필터 전 writer 활성화 차단. 문서 검토만 수행 | predicate 사전 확정 후 W09 |
| 2026-11-12 | W09 repository 기반 | 진행 중 | migration 16 및 비활성 writer의 활성 조회 추가. 임시 DB 관련 회귀 **35 passed**. claim·활성 전환 및 출처 redaction/정상 삭제 미구현, predicate 사전 확정 전 완료 차단 | 사전 확정 및 W09 잔여 구현 |
| 2026-11-12 | 개인 기억 범위 결정 | 후속 TODO로 범위 정정 | 현 시점 1인 사용 맥락과 향후 가족 식별·개인 기억·여러 기기/선호 지원을 §4.2.1a에 보존. 현재 W09 구현 범위에는 포함하지 않음 | 현재 W09 범위 유지; 후속 작업 별도 착수 |
| 2026-11-12 | W09 repository 추가 구현 | 진행 중 | 공유 단일값 사전 초안, claim·상태 전이·활성 전환·대화 삭제 시 redaction/격리. 관련 테스트 **46 passed**. 자동 매핑 모호성·서버 증거 및 단독 메시지 삭제 등 미완료 | W09 차단 항목 해소 후 완료 판단 |
| 2026-11-12 | W09 저장소 안전성 후속 검증 | 진행 중 | span 경계 및 격리된 문서 ID 재사용 거절, 전송 중 출처 삭제 시 unknown 보존 회귀 추가. 관련 테스트 **51 passed**. 자연어 매핑과 서버 결과 증거·unknown 복구는 후속 계약 필요 | W10 매핑 설계 및 W12~W14 서버 계약 확인 |

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
이번에는 I09만 수행한다. 완료 후 이 문서의 상태와 실행 이력을 갱신한다.
기존 사용자 변경을 보존하고, 관련 Wiki/기억 경로만 읽는다.
운영 DB·Wiki 문서·서비스·모델·패키지는 변경하지 않는다.
테스트는 임시 DB, fake Wiki, mock gate에 한정한다.
새로운 저장 정책/모델 서비스/외부 저장소를 임의 도입하지 않는다.
실행하지 못한 검증은 미실행으로 보고한다. commit/push/배포하지 않는다.
```

다음 실행에서는 `I09`을 다음 미완료 Task ID로 바꾸고 의존성·완료 기준을 확인한다. 사용자 요청이 여러 Task 연속 실행이라면 같은 원칙으로 각 Task 완료 기록을 남긴 뒤 순서대로 진행한다.

## 9. 이전 W12~W19 작업 이력 및 세부 검증

이 절은 통합 전 계획과 검증 기준을 보존한 참고 이력이다. 실행 순서와 상태는 §6의 I00~I10만 따른다. W15~W19의 품질 기준은 I06~I08에 반영한다.

#### W12 [대체됨] 별도 Wiki 서버 원자적 revision 지원 — 이력

통합 방향에 따라 별도 서버 패치를 배포하지 않는다. 내부 구현과 검증은 [I03](wiki-client-consolidation-plan.md#단계별-구현-task)에서 진행한다. 아래는 과거 작업 이력이다.

- 의존: W00, W11. 실제 서버 소스 미확보면 실행 금지.
- 대상: W00에서 확정한 Wiki 라우터·저장 계층·격리 테스트. 시작 시 정확한 파일 목록을 §8에 기입.
- 작업: 저장 transaction 안에서 조건 revision 비교와 갱신. 기존 조건 없는 수동 API 호환과 새 조건부 API를 구분. 기존 본문/ID 변경 migration 금지.
- 완료: 두 writer의 같은 revision 요청 중 하나만 성공. 조건 미일치에는 409와 안정된 오류 코드.
- 검증: 격리 DB 동시성·외부 수동 편집 경쟁·legacy 요청. Client GET→PUT 테스트만으로 완료 처리하지 않음.
- 2026-11-13 읽기 전용 SSH 조사: 운영 `/opt/bc250-family-wiki/app/main.py`를 확인했다. `documents.updated_at`과 `unicode61` FTS5/CRUD trigger를 사용하며 기존 PUT은 조건 없는 UPDATE, 정상 POST/PUT은 ID 포함 JSON, DELETE는 상태 JSON을 반환한다. 운영 디렉터리는 Git 저장소가 아니다. 204는 이 소스의 CRUD 정상 경로에서 사용하지 않는다. 읽은 원문의 SHA-256은 `20889a43f3f749854ef9beace60a31fb18c96afb34b782120f2e9d4a07c19953`; 사본은 [격리 테스트 fixture](../../tests/fixtures/wiki_server_w12/main.py)다. 이 관찰은 조회 시점의 파일에 대한 사실이며 현재 실행 프로세스와 파일이 같은 버전인지 증명하지 않는다.
- 검토용 [서버 패치](../patches/wiki-server-w12.patch): 기존 GET에 내용 기반 revision을 추가하고, 별도 조건부 PUT에서 `BEGIN IMMEDIATE` 안에 revision 비교·갱신을 수행한다. 기존 PUT도 같은 잠금과 단조 증가 timestamp를 사용해 동일 내용 재저장과 시계 역행이 기존 revision을 재사용하지 않도록 한다. 스키마 migration은 없다. 기존 PUT 응답과 조건 없는 수동 편집은 유지하며, revision 불일치 시 409 `wiki_revision_conflict`를 반환한다.
- 격리 검증: [test_wiki_server_w12_patch.py](../../tests/test_wiki_server_w12_patch.py)는 읽어 온 소스 사본에 패치를 적용하고 임시 DB에서 서버 라우트 함수를 실행한다. 동시 조건부 갱신 2건 중 1건 성공, legacy 수정에 따른 충돌, 삭제 후 404, FTS 갱신, 같은 본문의 재저장 revision 변경 **3 passed**. 실제 HTTP 전송·운영 서버·다중 프로세스 혼합 배포는 미검증이다.
- **W12 미완료**: 이 패치는 운영 서버에 적용·배포되지 않았으며 운영 파일을 수정하지 않았다. 적용 전에 원본 hash/소유권 확인과 충돌 검토, 패치 및 격리 테스트를 수행하고, 기존 구버전 writer가 동시에 실행되지 않는 배포 방식을 검증해야 한다. 직접 SQLite 변경이나 오래된 서버 프로세스가 timestamp 규칙을 우회하면 revision 보장이 성립하지 않는다. 잘못된 시간 형식의 기존 행은 409로 거절되므로 영향 범위를 배포 전 읽기 전용으로 조사해야 한다. 서버 및 배포 권한은 이 검토용 변경만으로 부여되지 않는다.

#### W13 [대체됨] 별도 Wiki 서버 멱등 쓰기와 결과 조회 — 이력

별도 서버 API 대신 통합 DB의 작업 결과 및 멱등성으로 [I07](wiki-client-consolidation-plan.md#단계별-구현-task)에서 구현한다.

- 의존: W12.
- 대상: W00에서 확정한 Wiki 저장/API 파일과 테스트.
- 작업: 문서 저장과 operation 기록을 같은 transaction으로 처리. 동일 key/payload 재사용, 충돌, 작업 결과 조회 구현. payload hash는 결정적 직렬화 사용.
- 완료: 응답 유실 뒤 결과 조회로 원래 문서 ID/버전을 확인 가능. 같은 key의 다른 내용은 거절.
- 검증: commit 전후 crash 경계, 같은 key 동시 POST, 동일 요청 재조회, operation 없음과 처리 불명 구분. 테스트용 서버만 사용.

#### W14 [대체됨] 별도 Wiki 저장·갱신·결과 조정 — 이력

분리 DB 조정 대신 [I02/I07](wiki-client-consolidation-plan.md#단계별-구현-task)의 namespace·단일 트랜잭션을 적용한다.

- 의존: W10~W13.
- 대상: [main.py](../../backend/app/main.py)의 기억 작업, [wiki.py](../../backend/app/wiki.py), W09 repository, [test_wiki_memory.py](../../tests/test_wiki_memory.py).
- 작업: operation ID를 쓰기 전에 영속화. 새 버전 Wiki 저장 성공 뒤 활성 전환. unknown은 작업 조회로만 조정. 일반 legacy 문서 덮어쓰기 금지.
- 충돌 확인 승인 시 승인된 후보·대상과 기존 record/revision을 재검증하고 변경됐으면 재확인; 반복 승인·취소·지연 응답·출처 삭제 경합은 안전하게 거절하거나 보류한다. 사용자 승인 자체를 Wiki 쓰기 성공으로 간주하지 않는다.
- 완료: DB와 Wiki 부분 성공이 기존 기억 소실/중복 쓰기로 이어지지 않음. 서버 기능 미지원 시 새 안전 쓰기 경로 보류, 구형 위험 경로 자동 fallback 금지.
- 검증: HTTP 응답 유실, Wiki 성공 뒤 Client DB 실패, 앱 재시작, 동시 중복/정정, Wiki 외부 변경/삭제, 대화 삭제. 추정 성공 처리 금지.

### 3차: RAG·출처·조회 비용

#### W15 [대체됨] RAG 역할 분리·발췌·검색 평가 — 이력

통합 이후 실행 대상은 [I06/I08](wiki-client-consolidation-plan.md#단계별-구현-task)이다. 아래 작업 및 품질 기준은 유지한다.

- 의존: W11, W14.
- 대상: [wiki.py](../../backend/app/wiki.py), [main.py](../../backend/app/main.py)의 검색 경로, [prompting.py](../../backend/app/prompting.py), [test_wiki.py](../../tests/test_wiki.py), [wiki-search-evaluation.md](wiki-search-evaluation.md).
- 작업: 일반 자료/활성 기억을 구분, 비활성 기억 문서 제외, bounded query/후보/발췌. 전체 payload 예산 안에서 자료를 선택. 검색 순위 알고리즘 변경은 실제 Wiki 구현 확인·평가 결과가 있을 때만 별도 Task로 분할.
- 완료: legacy 문서 불변, 삭제/만료/비활성 기억 오주입 방지, 불충분 근거 안내, 실제 context 준수.
- 검증: 한국어 조사·후속 질문·긴 문서·무관한 제목·검색 없음·오류·문서 지시 injection·4K/16K profile mock. 기존 대비 Recall@3/무관 근거율 기록.

##### W15-Q0~Q6 [대체됨] 검색어 정규화·Alias 세부 작업 — 이력

통합 방향에서는 아래 외부 서버 수정·차단 조건 대신 [I06](wiki-client-consolidation-plan.md#단계별-구현-task)에서 Client 내부 repository를 대상으로 수행한다. Q0의 서버 코드 사실은 확보한 스냅샷을 기준으로 갱신됐으며 사용자 질의의 운영 재현은 미실시다.

- 사용자 보고: `bc250 ai 서버가 뭐야?`는 성공하지만 `bc250 이 뭐야?`는 실패한다. 운영 재현은 미실시다.
- [검색어 정규화·Alias 개선 계획](wiki-query-alias-plan.md)에 Client 코드 근거, FTS5의 조건부 토큰화 분석, 정확 Alias→FTS5→제한 확장 전략, 신구 응답 호환, 단계별 수정 대상·의존·완료 조건·테스트·롤백을 기록했다.
- 순서: Q0 서버 계약 조사 → Q1 질문/표기 정규화 → Q2 Alias 수명주기 → Q3 단계 검색 → Q4 Client/발췌 → Q5 근거 메타데이터(W16 연계) → Q6 평가. 각 Task는 별도로 실행하며 서버 수정 경로는 Q0 확인 전 확정하지 않는다.
- Q0~Q3의 검색 설계·격리 검증은 W14와 독립적으로 진행 가능하다. W15의 기억 문서 격리와 새 writer 활성화 차단 조건은 유지한다.
- 완료 기준: `bc250`, `bc-250`, `BC 250`, `bc250 ai 서버`가 비충돌 Alias fixture에서 같은 문서 Top1; 기존 성공 질의 Recall@3·무관 결과율·지연 회귀 기준 충족. 새 LLM/벡터 DB 도입 없음. 현재는 계획만 반영했고 코드·DB·운영 설정은 변경하지 않았다.

#### W16 [대체됨] 출처 메타데이터 API·본문 분리 — 이력

- 의존: W15.
- 대상: [main.py](../../backend/app/main.py), [db.py](../../backend/app/db.py), [api.ts](../../frontend/src/api.ts), [test_api.py](../../tests/test_api.py).
- 작업: 새 요청의 명시적 opt-in 계약과 저장 출처 스키마 구현. retrieved/provided/cited 구분. 기존 답변 수정 없음. 새 모드의 다음 턴에는 출처 JSON 본문 재주입 없음.
- W15-Q5 연계: 검색 일치 단계·Alias는 retrieved 진단이며 답변 근거가 아니다. provided는 최종 payload 기준, cited는 전달한 문서 ID와 대조한다. ID 일치만으로 주장과 발췌의 의미 일치까지 검증됐다고 표시하지 않는다. 상세 계약은 [Alias 계획 §5](wiki-query-alias-plan.md#5-기존-문서인덱스api-호환)를 따른다.
- 완료: stream/nonstream·이력 조회 동일 출처, 구형 요청 기본 형식 유지. 문서 버전/발췌는 답변 생성 시점 기준.
- 검증: 전달되지 않은 ID 인용, 최종 프롬프트에서 제거된 자료, 문서 사후 변경/삭제, 구형 클라이언트, 재로딩·request 중복. 기존 legacy 답변의 자동 재작성/문자열 휴리스틱 삭제 금지.

#### W17 [대체됨] 경량 기억 조회·출처 카드·polling 개선 — 이력

- 의존: W04, W05, W16.
- 대상: [main.py](../../backend/app/main.py)의 history/detail API, [main.tsx](../../frontend/src/main.tsx), [api.ts](../../frontend/src/api.ts), [MessageList.tsx](../../frontend/src/components/MessageList.tsx), W05 UI 컴포넌트/테스트.
- 작업: view=summary opt-in, 상세 클릭 조회, source 카드, 비활성 탭 중지/진행 중 polling. 대화 완료 이벤트 이후 새 작업 discovery를 보장하고 미등록→등록 race에도 영구 중단하지 않음.
- 충돌 시 기존·새 값과 대상이 명확한 확인 UI를 제공한다. 변경/유지 및 무응답 동작은 §4.2.1b를 따르며 미구현 서버 기능을 완료로 표시하지 않는다.
- 완료: 30개 전후 본문을 매 5초 전송하지 않음. 기본 API 응답 유지. 요청 중첩 방지, R12 충족.
- 검증: fake timers·visibility·네트워크 실패·대화 전환·완료 직후 등록 지연·상세 권한/대화 소속 확인. frontend lint/test/build와 화면 확인.

#### W18 [대체됨] 기억 작업 대기·토큰·모델 전환 비용 제한 — 이력

- 의존: W14, W15.
- 대상: [main.py](../../backend/app/main.py)의 background scheduler/prepare, [config.py](../../backend/app/config.py), [test_background_switch_deferral.py](../../tests/test_background_switch_deferral.py), [test_model_context.py](../../tests/test_model_context.py).
- 작업: 기존 DB 작업 큐 사용, bounded admission/대기/추론 1개. foreground 대기 중 신규 기억 추론 양보. 매 epoch 재준비 시 불필요한 Wiki 재검색 빈도 제한은 freshness 보존 가능한 경우만 적용.
- 완료: 후보당 전송 1회 원칙, 명확한 admission 거절만 재준비, 타이틀·요약 경로 회귀 없음. 16K 하드코딩/모델 자동 전환 없음.
- 검증: 큐 포화·foreground 경합·모델 hold·409 전송 전 거절/전송 후 불명·schema 미지원·토큰 초과·취소/재시작. fake gate로 검증하고 실제 GPU 성능은 W19에서 구분.

#### W19 [대체됨] 통합 품질·비용 평가와 롤백 문서 확정 — 이력

- 의존: W07, W14~W18.
- 대상: [wiki-search-evaluation.md](wiki-search-evaluation.md), [wiki-integration.md](wiki-integration.md), [README](../../README.md), 이 문서 §8.
- 작업: 기존/개선 동일 fixture로 평가. Qwen3.6/Gemma 실제 모델 검증은 별도 승인된 환경·실행 기회가 있을 때 수행하고 없으면 미검증으로 남김. 새로운 schema 전체를 기존 모델 기능 시험이 보장한다고 간주하지 않음.
- 완료: 질문/assistant 사실 저장 0건, 정확 중복 쓰기 0회, conflict/unknown 덮어쓰기 0회(필수 회귀 세트 기준). Recall@3, 저장 precision, 선별 recall, 추가 추론 비율/토큰, foreground 지연 p50/p95, UI 요청량 기록.
- 검증: 관련 backend 통합, frontend lint/tests/build, 네 viewport 시각 확인. 운영 적용·부하 측정·장애 주입 미실행은 별도 표시. 모두 skip하는 정책으로 recall을 희생해 완료하지 않음.
