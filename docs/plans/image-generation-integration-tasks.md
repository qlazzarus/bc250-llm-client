# 결과 카드 편집 및 Studio 이미지 생성 연동 — Terra 5.6 medium

작성: 2026-10-26. 현재 단계는 코드 리뷰와 실행 계획 수립이다. 이 문서 작성으로 실제 생성·배포 또는 Studio 수정이 승인된 것은 아니다. IS01~IS04 이후 확장 계획이며 기존 IG01~IG03의 개략 계획을 이 문서로 대체한다.

## 리뷰 결론

사용자의 방향은 적절하다. LLM이 만든 영어 프롬프트를 결과 카드에서 직접 수정하고, 원하는 시점에 `이미지 생성 요청`을 누르면 수정본으로 Studio 작업을 만든다. 문구 수정에 LLM을 다시 호출할 필요는 없다. 원래 assistant 메시지는 보존하고 편집 초안과 실제 제출 스냅샷을 별도로 관리한다. 이미지 프롬프트 모드의 독립 요청 정책은 유지한다.

카드에는 편집 가능한 영어 프롬프트, 원문으로 되돌리기, 프롬프트 복사, 설정 JSON 복사, 이미지 생성 요청을 둔다. 복사와 생성은 모두 현재 편집값을 사용한다. 버튼 가까이에 대상 모델·해상도·steps·CFG를 표시한다. 첫 버전에서는 prompt만 자유 편집하고, 기존 직사각형 결과를 바로잡을 수 있도록 지원되는 정사각형 크기 선택만 제공한다. steps/CFG/model 자유 편집, 자동 생성, 일괄 생성은 제외한다.

## 확인된 로컬 Studio 계약

참조 루트: 로컬 AI Studio 저장소. 로컬 소스 확인 결과이며 BC-250에 같은 버전이 배포되어 있는지는 별도 확인해야 한다.

| 근거 파일 | 확인 사항 |
|---|---|
| `backend/app/api/routes.py` | Bearer 일반 client key로 jobs/model/status/assets 접근 가능. 관리자 키 불필요 |
| `backend/app/models/schemas.py` | JobRequest는 extra 금지, prompt 1~8000자·공백/NUL 거절, batch_count 기본 1, seed 기본 -1 |
| `backend/app/api/routes.py` | `POST /api/v1/jobs` → 202 `{job_id,status}`; `GET /api/v1/jobs/{id}`; `POST /api/v1/jobs/{id}/cancel` |
| `backend/app/api/routes.py` | `GET /api/v1/jobs?limit=&offset=`와 `GET /api/v1/jobs/{id}/events` 제공. client_reference 전용 검색 없음 |
| `backend/app/models/schemas.py` | 상태 queued/waiting_for_gpu/running/completed/failed/cancelled/interrupted; output_asset_ids/items 제공 |
| `backend/app/api/routes.py` | `GET /api/v1/assets/{id}` 및 `/file`도 Bearer 인증 필요 |
| `config/models.yaml` | z-image-nsfw-v2는 512×512와 768×768만 지원. CFG 1.0 고정, negative_prompt 미지원, sampler euler 고정 |
| `backend/app/services/registry.py` | 모델 실행 가능 여부·해상도 조합 검증, seed=-1이면 실제 seed 결정, model_snapshot 저장 |
| `backend/app/api/routes.py`의 enqueue | client_reference를 받지만 unique 제약·기존 작업 반환 로직 없음. 같은 POST 반복은 새 작업 생성 |
| `backend/app/services/gpu.py`, `services/llm.py`, `scheduler/runner.py` | Studio가 gate pause/drain·GPU 확보·복구와 image_priority를 담당 |

현재 LLM 출력 스키마는 width/height 각각 512 또는 768을 허용하므로 512×768도 만들어질 수 있다. 직접 생성 전에 이 불일치를 해결해야 한다. 기존 결과는 보존하되 지원 크기를 사용자가 선택할 때까지 제출을 막는다. 크기를 조용히 변경하지 않는다.

## 연동 설계

브라우저 `/llm/api/v1` → LLM Client FastAPI → 루프백 Studio `127.0.0.1:8090/api/v1`. Studio key와 URL은 서버 환경변수에만 둔다. 기존 `/api/*` Caddy 라우트·Studio 프로젝트는 변경하지 않는다. 이 앱이 gate pause/resume을 직접 추가하지 않는다.

제안 설정: `IMAGEGEN_ENABLED=false`, `IMAGEGEN_URL=http://127.0.0.1:8090`, `IMAGEGEN_API_KEY`(Studio 일반 client key), `IMAGEGEN_TIMEOUT_SECONDS`, `MAX_IMAGE_JOBS`. 실제 키 값을 문서·로그·응답에 기록하지 않는다. 내부 주소 allowlist와 redirect 거부, 환경 proxy 우회 방지를 adapter에 적용한다.

제출 입력은 source assistant message ID, 편집된 prompt, 선택 해상도, request UUID다. 서버가 image_prompt 방에 속한 완료 assistant 메시지인지 확인하고 원본 6필드 JSON을 검증한다. model/steps/CFG는 원본과 서버 allowlist에서 결정한다. prompt는 1~8000자, 공백/NUL 거절. 임의 URL·파일 경로·priority·model override는 받지 않는다.

Studio 요청은 6필드에 `job_type=image.generate`, `batch_count=1`, `source=nsfw`(이 프로필의 보관 namespace), `priority=0`, `preset=balanced`, `seed=-1`, `client_reference=llm-client:<local-job-uuid>`를 더한다. negative_prompt/sampling_method/scheduler는 생략하여 Studio 프로필을 사용한다. 반환된 실제 seed는 조회 결과에 보존한다. source는 채팅 모드가 아니라 Studio 자산 분류이며 서버에서 고정한다.

제안 앱 API(모두 APP_API_KEY 인증):

- `GET /api/v1/image-generation/capabilities`: 활성 여부, 허용 크기, 모델 실행 가능 여부만 정제해서 반환.
- `PUT /api/v1/conversations/{cid}/messages/{mid}/image-draft`: 편집 초안 저장, revision 기반 충돌 방지.
- `POST /api/v1/conversations/{cid}/image-jobs`: 서버 검증 후 제출. 로컬 ID와 상태 반환.
- `GET /api/v1/conversations/{cid}/image-jobs`: 해당 방 작업 복원. 페이지 단위 조회.
- `GET /api/v1/image-jobs/{id}` 및 `POST /api/v1/image-jobs/{id}/cancel`.
- `GET /api/v1/image-jobs/{id}/assets/{asset_id}/file`: 해당 job 소유 asset만 인증 프록시.

DB 초안은 원본 메시지와 별개다. 가상 스크롤로 카드가 unmount되어도 수정값이 사라지지 않도록 대화 상위 상태와 서버 저장을 사용한다. 입력 debounce 저장 상태를 표시하고, 복사·제출은 저장 완료 여부와 관계없이 현재 화면의 동일 snapshot을 사용한다. 제출 후 편집은 이미 접수된 작업을 변경하지 않는다.

로컬 image_jobs에는 UUID, source message/conversation, 편집 snapshot, payload fingerprint, upstream job ID, 상태, client_reference, 시각, 정제된 오류, asset IDs를 저장한다. 동일 request UUID+동일 payload는 기존 작업 반환, 다르면 409. DB unique와 원자적 상태 전이로 중복 클릭·동시 제출을 막는다.

upstream에 멱등성이 없으므로 timeout/응답 손실/제출 중 재시작은 `submission_unknown`이다. 자동 재POST 금지. client_reference로 제한된 페이지 조회를 수행하여 정확히 한 작업이 확인될 때만 연결한다. 미발견은 미접수의 증거가 아니므로 unknown 유지, 복수 발견 시 수동 점검. 사용자가 Studio에서 확인 후 명시적으로 새 작업을 시작할 수 있게 하되 중복 가능성을 표시한다. 동일 이미지가 보장되는 retry로 표현하지 않는다.

완료 결과는 2~5초 polling부터 구현하고 terminal에서 중지한다. 앱 재시작·새로고침 이후 local job ID로 조회를 재개한다. 제출 대기/unknown과 upstream queued/waiting_for_gpu/running/completed/failed/cancelled/interrupted를 구분한다. 취소 요청 성공은 즉시 GPU 중단을 의미하지 않는다. 진행 중/unknown 작업이 있는 방 삭제는 409로 막고, 완료 후 방 삭제는 로컬 연결만 삭제하며 Studio 자산은 유지한다.

이미지는 Authorization을 포함한 fetch로 blob을 받아 표시·다운로드한다. img src에 키를 넣지 않고, upstream file_url은 그대로 fetch하지 않는다. 서버가 검증한 asset ID로 고정 경로를 구성하고 크기·MIME 제한을 적용한다. Object URL은 교체/unmount 때 해제한다.

## 실행 태스크

각 태스크는 별도 실행 지시 후 한 개씩 진행한다. 범위·완료 조건·실행 결과를 이 문서에 기록한다. 실제 서버 명령은 사용자에게 제공한다.

| ID | 작업 | 의존성 | 상태 |
|---|---|---|---|
| GI01 | 프로필 해상도 계약 정합화 | 없음 | 완료 |
| GI02 | Studio adapter와 capabilities | GI01 | 완료 |
| GI03 | 편집 초안·작업 DB 및 API 스키마 | GI01 | 완료 |
| GI04 | 제출·멱등성·불확실 상태 복구 | GI02, GI03 | 완료 |
| GI05 | 상태·취소·이미지 프록시 | GI04 | 완료 |
| GI06 | 결과 카드 편집·복사·명시적 제출 | GI03, GI04 | 완료 |
| GI07 | 작업 상태·결과 표시·대화 전환 복원 | GI05, GI06 | 완료 |
| GI08 | 회귀·배포·사용자 실서버 점검 문서 | GI07 | 완료 |

### GI01 — 해상도 계약

파일: image_prompt.py, prompting.py, ImagePromptResult.tsx, 관련 테스트.
새 LLM 출력의 해상도 조합을 정사각형 두 개로 제한하고 시스템 지시를 일치시킨다. 기존 직사각형 결과 파싱·조회는 호환 경로로 유지하되 직접 제출에는 사용 불가로 표시한다. 구조화 출력 지원 여부는 기존 gate 계약 안에서 검증하며 schema 제거 fallback은 금지한다.
완료: 신규 직사각형 출력 거절, 기존 카드 유지, 정사각형 선택 후 제출용 snapshot 검증, quick/roleplay 회귀.

구현 기록: `IMAGE_PROMPT_PROFILE`에 Studio가 확인한 `supported_resolutions=((512,512),(768,768))`를 추가했다. gate JSON schema의 `allOf/anyOf`, 서버 검증기, 시스템 지시가 이 계약을 함께 사용한다. 새 512×768/768×512 응답은 완료 메시지로 저장되지 않고 `image_prompt_invalid_schema`로 실패한다. 프런트엔드 파서는 기존 직사각형 JSON을 계속 카드로 표시하며, 이후 생성 요청 전 정사각형 선택이 필요하다는 안내를 출력한다. `PYTHONPATH=. .venv/bin/pytest`는 49 passed, `frontend`의 `npm run lint`와 `npm run build`는 통과했다. 실제 gate/Studio 요청은 하지 않았다.

### GI02 — Studio adapter

파일: 새 backend/app/imagegen.py, config.py, .env.example, adapter 테스트.
고정 loopback URL·일반 client key·timeout·redirect/proxy 정책을 설정한다. models/status/capabilities 조회 및 jobs/assets 호출 adapter를 만든다. 외부 응답의 model_path/model_snapshot/원문 오류는 브라우저로 전달하지 않는다. 기능 OFF이면 네트워크 호출 없이 비활성 반환. 운영 engine=fake는 모의 실행으로 표시하고 실제 생성 가능으로 오인하지 않게 한다.
완료: 인증/404/429/5xx/timeout/잘못된 응답을 모킹 검증, 운영 모델 미실행/복구 필요 상태를 UI용 이유로 반환. live 호출 없음.

구현 기록: `backend/app/imagegen.py`에 loopback Studio adapter를 추가했다. `IMAGEGEN_ENABLED=false`일 때는 네트워크를 전혀 호출하지 않고 정제된 disabled capabilities를 반환한다. 활성화 시 URL은 정확히 `http://127.0.0.1:8090`만 허용하며 `trust_env=false`, redirect 거부, Studio 일반 client key의 Authorization header를 사용한다. `/api/v1/image-generation/capabilities`는 APP_API_KEY로 보호되고 engine=fake, recovery_required, model 실행 불가, 프로필 해상도 불일치를 UI용 상태로 정제한다. model path·snapshot·upstream body·키는 응답에 넣지 않는다. `.env.example`에 server-only IMAGEGEN 설정을 추가했다. 모킹으로 401/404/429/5xx/timeout, fake/recovery, URL 검증을 확인했다. `PYTHONPATH=. .venv/bin/pytest`는 59 passed, `frontend`의 `npm run lint`와 `npm run build`는 통과했다. live Studio 호출은 하지 않았다.

### GI03 — 저장 계약

파일: db.py, 새 image job 스키마/저장 모듈, main.py 또는 별도 router, migration 테스트.
초안과 image_jobs를 추가한다. 초안 revision·원본 메시지 검증, APP_API_KEY 인증, snapshot/fingerprint·unique request ID, 방별 조회를 구현한다. 원본 assistant content 불변. active/unknown 작업 삭제 차단 정책 구현.
완료: 마이그레이션 반복 안전, 기존 대화 보존, 다른 방 message 참조·user 메시지·잘못된 JSON·공백/과대 prompt 거절, 초안 동시 수정 409.

구현 기록: SQLite v6 마이그레이션에 `image_drafts`와 `image_jobs`를 추가했다. 원본 assistant content는 수정하지 않으며, 초안은 `PUT/GET /api/v1/conversations/{cid}/messages/{mid}/image-draft`로 저장한다. source는 같은 image_prompt 대화의 완료된 assistant JSON이어야 하고, prompt 1~8,000자·NUL/공백·해상도 조합·revision을 서버에서 검증한다. revision 불일치는 409, 다른 방/message·user message·잘못된 JSON은 안전한 오류로 거절한다. 방별 `GET /api/v1/conversations/{cid}/image-jobs`도 추가했지만 Studio 제출 endpoint는 GI04까지 없다. `submission_pending`, `submission_unknown`, queued/GPU waiting/running 상태가 남아 있으면 대화 삭제는 409다. `PYTHONPATH=. .venv/bin/pytest`는 61 passed, `frontend`의 `npm run lint`와 `npm run build`는 통과했다. live Studio 호출·이미지 생성·배포는 하지 않았다.

### GI04 — 제출과 중복 방지

파일: image job service/router, adapter, API 테스트.
DB 저장 후 단일 제출 수행. 원자적 claim으로 여러 프로세스의 중복 POST 방지. HTTP 성공 시 upstream ID 저장, 명확한 거절과 결과 불명확 오류 구분. 동일 UUID replay/충돌, restart 시 제출 중 상태를 unknown으로 복구, client_reference 제한 조회를 구현한다. 자동 재제출·Studio retry endpoint 호출 금지.
완료: 빠른 중복 클릭/동시 요청은 upstream POST 1회, 접수 후 응답 손실에도 자동 POST 0회 추가, 재시작 후 복구, no-match/복수-match unknown 유지. LLM 호출 없이 편집 prompt 제출 확인.

구현 기록: `POST /api/v1/conversations/{cid}/image-jobs`를 추가했다. 이 endpoint는 저장된 초안 revision과 동일한 prompt/해상도 snapshot만 받아, server-side로 고정한 모델·steps·CFG·Studio 옵션을 조합한다. request UUID는 SQLite의 unique 제약과 payload fingerprint로 보호한다. 같은 UUID와 같은 snapshot은 기존 local job을 반환하고 Studio에 다시 POST하지 않으며, 다른 snapshot이면 409으로 거절한다. Studio가 명확히 4xx로 거절하면 local job은 `failed`가 되고 422를 반환한다. timeout·5xx·응답 파싱 실패·응답 손실처럼 접수 여부를 알 수 없는 경우는 `submission_unknown`으로 남기며 자동 재제출하지 않는다. 재시작 시 남아 있던 `submission_pending`도 unknown으로 전환한다. unknown job을 다시 조회하면 `client_reference`가 정확히 하나만 일치할 때에만 upstream job과 연결하고, 그 외에는 unknown을 유지한다. 전체 활성 image job은 `MAX_IMAGE_JOBS`로 상한을 둔다. `PYTHONPATH=. .venv/bin/pytest`는 65 passed, `frontend`의 `npm run lint`와 `npm run build`는 통과했다. Studio/GPU/운영 서버에는 실제 요청하지 않았다.

### GI05 — 상태·취소·자산

파일: image job service/router, adapter, API 테스트.
상태 정제, polling rate 제한, upstream cancel 요청과 최종 cancelled 구분, interrupted/복구 필요 안내를 구현한다. job별 asset 소유 확인 및 인증 이미지 프록시·용량 제한·허용 MIME을 구현한다. unknown은 upstream ID 확인 전 취소 불가로 표시.
완료: 완료/실패/취소/중단/404/일시 통신 장애 회귀, 다른 job asset과 임의 URL 거절, 키·내부 경로 미노출, 대화 삭제 정책 확인.

구현 기록: `GET /api/v1/image-jobs/{id}`가 local job을 기준으로 Studio 상태를 정제해 동기화한다. 완료 결과의 `output_asset_ids`/`output_items.asset_id`만 보존하며, polling 간격(`IMAGE_JOB_POLL_MIN_INTERVAL_SECONDS`, 기본 2초) 안의 반복 조회는 Studio 호출 없이 local 상태를 반환한다. Studio 404는 `upstream_job_not_found` 실패 상태로 기록하고, 일시 통신 장애는 기존 local 상태를 보존한 채 503으로 반환한다. `POST /api/v1/image-jobs/{id}/cancel`은 upstream ID가 없는 `submission_unknown` 작업을 409으로 거절하고, 정상 요청도 곧바로 cancelled로 단정하지 않고 `cancel_requested=true`만 기록한 뒤 Studio의 반환 상태를 따른다. `GET /api/v1/image-jobs/{id}/assets/{asset_id}/file`은 해당 local job에 이미 연결된 asset ID만 허용해 server-side Bearer로 Studio 파일을 읽고, PNG/JPEG/WebP 및 `MAX_IMAGE_ASSET_BYTES`(기본 20 MiB)만 프록시한다. upstream URL·키·경로는 브라우저 응답에 포함하지 않는다. 상태 완료·404·일시 장애·unknown 취소 차단·asset 소유 확인을 모킹 검증했다. `PYTHONPATH=. .venv/bin/pytest`는 68 passed, `frontend`의 `npm run lint`와 `npm run build`는 통과했다. Studio/GPU/운영 서버에는 실제 요청하지 않았다.

### GI06 — 편집 카드

파일: ImagePromptResult.tsx, MessageList.tsx, main.tsx 또는 분리한 상태 hook, api.ts, CSS.
prompt 편집·원문 복원·지원 크기 선택·초안 저장 상태·복사·이미지 생성 요청 버튼 구현. 수정본 복사와 제출이 동일 값을 사용하도록 단일 draft 객체를 참조한다. submit 직전 snapshot/request UUID를 고정하고 미응답 재시도는 같은 UUID 유지. 비활성/unsupported 상태에 이유 표시. 가상화 unmount 및 방 전환으로 초안이 섞이거나 사라지지 않게 한다.
완료: 긴 목록 스크롤·방 왕복·새로고침·초안 충돌·HTTP 수동 복사 확인. 원문 불변, 빈 prompt 차단, 클릭 전 생성 없음, lint/build 통과.

구현 기록: image_prompt assistant 결과 카드를 편집 가능한 영어 prompt·정사각형 해상도 선택 UI로 바꿨다. 원본 assistant JSON은 읽기 전용으로 유지하며, `원문으로 되돌리기`는 별도 draft만 바꾼다. 프롬프트/설정 JSON 복사는 현재 수정본을 사용하고, Clipboard API를 쓸 수 없으면 선택 가능한 textarea를 제공한다. draft는 650ms debounce로 `PUT image-draft`에 저장하며 저장 중·성공·오류를 표시한다. 가상화로 카드가 unmount되어도 대화 상위의 message-id별 draft 캐시와 서버 저장본을 사용해 방 전환·스크롤 뒤 값이 섞이지 않게 했다. `이미지 생성 요청`은 사용자가 명시적으로 눌렀을 때만 current snapshot을 저장·제출하고, 전송 오류 재시도에는 같은 UUID를 유지한다. Studio capabilities가 disabled/unavailable이거나 지원 해상도가 아니면 버튼을 비활성화하고 안전한 이유만 표시한다. 실제 작업의 상세 상태/결과 표시는 GI07에서 추가한다. `frontend`의 `npm run lint`와 `npm run build`는 통과했고, 백엔드 회귀는 이전 GI05 시점 `68 passed`를 유지한다. 실제 Studio/GPU/운영 서버에는 요청하지 않았다.

### GI07 — 결과와 작업 복원

파일: 새 ImageJobStatus/Result 컴포넌트, api.ts, 대화 상태 hook.
카드별 여러 작업을 local ID로 표시하고 queued/GPU 대기/running/완료/실패/unknown을 설명한다. 완료 썸네일·다운로드, 취소 요청, 명시적 새 생성 제공. 방 전환 시 polling 정리, 복귀/새로고침 시 서버 작업 복원. UI 취소/컴포넌트 unmount로 이미지 작업을 임의 취소하지 않는다.
완료: A방 생성 중 B방 이동 후 상태·이미지가 B에 들어가지 않음, terminal polling 중지, blob URL 정리, unknown 중 자동 제출 없음.

구현 기록: image_prompt 방을 열 때 방별 local image job 목록을 복원하고, `queued`·`waiting_for_gpu`·`running` 작업만 2.5초 간격으로 local job endpoint에 polling한다. terminal 상태는 polling하지 않으며, 방 전환 시 기존 timer를 정리한다. 카드별로 source assistant message ID가 같은 작업만 표시하므로 A방 작업/이미지가 B방에 섞이지 않는다. `submission_unknown`에는 접수 확인 필요와 자동 재요청 금지 문구를 표시하고 polling/자동 submit하지 않는다. upstream ID가 있는 활성 작업은 취소 요청 버튼을 제공하되, 요청 직후 완료로 표시하지 않고 server의 `cancel_requested`/최종 상태를 따른다. 완료 asset은 브라우저가 LLM Client의 인증 API에서 blob으로만 받아 썸네일·다운로드 링크로 표시한다. upstream URL/Studio key는 사용하지 않으며 Object URL은 카드 update/unmount 때 해제한다. `PYTHONPATH=. .venv/bin/pytest`는 68 passed, `frontend`의 `npm run lint`와 `npm run build`는 통과했다. 실제 Studio/GPU/운영 서버에는 요청하지 않았다.

### GI08 — 통합 검증·배포

파일: tests, README, 이 문서, 필요 시 읽기 전용 preflight 스크립트.
fake Studio로 전체 편집→제출→조회→결과, 중복·timeout·재시작·권한·용량 제한 검증. pytest/lint/build와 브라우저 모의 검증 수행. 실행하지 못한 항목을 완료라고 기록하지 않는다. 배포 스크립트는 중지 후 SQLite 일관 백업, 기존 unit 백업·복원, 최종 위치 venv, 환경 파일 유지, 코드+DB 롤백 절차를 포함한다.
사용자가 운영 8090의 실제 API/model/engine/policy를 읽기 전용 preflight로 확인한 뒤 기능을 활성화한다. 실제 이미지 한 건은 사용자의 명시적 클릭으로 수행한다. image_priority로 LLM 대기·복귀와 결과 다운로드를 사용자가 확인한다. 기존 Studio/Caddy 수정이 필요해지면 별도 변경안으로 보고한다.

구현 기록: fake Studio adapter로 초안 편집→server-side job 제출→완료 상태 동기화→소유 asset의 인증 프록시 다운로드까지 한 흐름을 추가 회귀 테스트했다. 원본 assistant JSON 불변과 `client_reference`도 검증한다. README는 기본 `IMAGEGEN_ENABLED=false` 배포, secret 미출력 capabilities preflight, Studio 일반 client key를 사용하는 명시적 활성화, 첫 실제 GPU 작업 시 주의점, 기존 SQLite 백업/릴리스 교체/롤백 절차를 정리했다. `PYTHONPATH=. .venv/bin/pytest`는 69 passed, `frontend`의 `npm run lint`와 `npm run build`는 통과했다. 실제 Studio/GPU/운영 서버·Caddy에는 요청이나 변경을 수행하지 않았다.

## Terra 5.6 medium 실행 지시

```text
docs/plans/image-generation-integration-tasks.md를 읽고 GI01부터 요청된 태스크 한 개씩 구현해줘.
참조 프로젝트 ~/Works/bc250-imagegen은 읽기 전용이고, 실제 API 계약을 코드로 확인해.
사용자가 영어 prompt를 수정한 후 버튼을 눌렀을 때만 생성해. 원본 답변과 제출 snapshot을 보존해.
Studio client_reference는 멱등키가 아니야. timeout/재시작 이후 자동 재POST하지 마.
브라우저는 LLM Client API만 호출하고 Studio key와 주소는 서버에만 둬.
태스크 종료마다 변경 파일·검증·미검증을 문서에 기록해. 실제 배포/GPU 실행은 사용자가 해.
```
