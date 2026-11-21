# 이미지 프롬프트와 Studio 연동

## 독립 프롬프트 작성

이미지 프롬프트 대화는 이전 사용자·assistant 메시지, 장기 기억, 예전 모드 설정을 모델 입력에 넣지 않는다. 각 요청에 원하는 전체 장면을 작성한다. 기록은 화면에서 조회할 수 있다.

| 작성 방식 | 동작 |
|---|---|
| AI 최적화 | LLM 의도 분석 후 모델별 프롬프트 작성 |
| 직접 번역 | 별도 의도 분석 없이 원문 번역. 추가 장면을 만들지 않도록 지시 |
| 원본 그대로 | LLM을 호출하지 않고 앞뒤 공백을 제거한 입력 사용. 이미지 모델 직접 선택 필수 |

`POST /api/v1/image-assistant/analyze`는 분류와 장면 필드를 반환하며 이미지 작업을 만들지 않는다. exact_text는 원문에 실제 존재하는 문자열이어야 하고, 최종 프롬프트에도 보존돼야 한다. 잘린·비어 있는·잘못된 JSON 또는 지정 문구 누락은 한 번 재시도한다. 두 번 실패하면 생성 요청을 실패 처리한다.

LLM은 영어 텍스트용 JSON schema를 사용하며 모델 수치 옵션은 서버 프리셋에서 적용한다. 지원 카탈로그는 [image_models.json](../backend/app/image_models.json), 프리셋은 [prompt_profiles.json](../backend/app/prompt_profiles.json)에 있다. Studio가 온라인이면 실행 가능한 모델 목록을 사용하고, 오프라인이면 저장된 카탈로그로 프롬프트를 작성할 수 있다. `fake` 엔진은 별도 허용 설정이 필요한 테스트용이다.

## 초안과 제출

결과 카드에서 프롬프트, 모델, 프리셋, 해상도, steps, CFG, seed, sampler/scheduler와 지원 모델의 negative prompt를 편집한다. 모델 변경 시 balanced 설정으로 초기화한다. 지원하지 않는 옵션은 제출하지 않는다. MOSS LoRA 토글은 Studio capability가 지원을 명시한 경우만 표시한다.

초안 저장과 이미지 작업 제출은 별개다. 사용자가 이미지 생성 버튼을 눌렀을 때만 Studio에 작업을 요청한다. `IMAGEGEN_ENABLED=false`이면 제출할 수 없다. 활성화에는 Studio의 일반 client key가 필요하며 관리 키를 사용하지 않는다.

동일 요청 ID에 다른 payload를 제출하면 충돌한다. 초안·작업에는 선택한 모델·옵션·프리셋·작성 방식을 저장한다. 제출 결과가 불명인 `submission_unknown`은 자동 재제출하지 않으며 Studio의 client_reference로 결과를 확인한다.

## 작업과 자산

작업 목록·진행 상태·취소·결과 복원을 제공한다. 브라우저는 Studio에 직접 접속하지 않고 Client의 자산 API를 사용한다. 서버가 작업과 자산 ID를 검증하고 다운로드 크기를 제한한다.

작업 기록에는 Studio가 제공하는 실행 정보, 모델 스냅샷의 허용 필드, 사용자 점수와 평가 메모를 보관한다. 비교 기록 API는 작업별 자료를 반환한다. 출력 usage는 마지막 프롬프트 작성 호출 기준이며 의도 분석과 실패 시도 전체 비용을 합산하지 않는다.

## 설정과 API

| 설정 | 기본값 |
|---|---|
| `IMAGEGEN_ENABLED` | false |
| `IMAGEGEN_URL` | `http://127.0.0.1:8090` |
| `IMAGEGEN_TIMEOUT_SECONDS` | 20초 |
| `MAX_IMAGE_JOBS` | 10 |
| `MAX_IMAGE_ASSET_BYTES` | 20 MiB |
| `IMAGE_JOB_POLL_MIN_INTERVAL_SECONDS` | 2초 |
| `IMAGE_PROMPT_MAX_TOKENS` | 512 |

API 그룹은 `/api/v1/image-generation/capabilities`, `/conversations/{id}/messages/{message_id}/image-draft`, `/conversations/{id}/image-jobs`, `/image-jobs/{id}`다. 세부 라우트는 [main.py](../backend/app/main.py)에 있다.

Studio는 별도 서비스다. LoRA 옵션은 실제 Studio capability와 작업별 snapshot 계약에 따라 제공하며 클라이언트만 배포해 외부 기능을 추가하지 않는다. 프리셋과 모델 추천은 생성 품질 보증이 아니다.
