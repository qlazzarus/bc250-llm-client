# 개발과 운영 구성

## 실행 파일과 경로

| 구성 | 설정·도구 |
|---|---|
| 환경 변수 | [.env.example](../.env.example), [Settings](../backend/app/config.py) |
| Client systemd | [bc250-llm-client.service](../deploy/bc250-llm-client.service) |
| HTTP/SPA | [Caddyfile.snippet](../deploy/Caddyfile.snippet) |
| 자동 배포 | [deploy.yml](../.github/workflows/deploy.yml), [scripts](../scripts/) |
| 모델 제어 | [deploy/model-control](../deploy/model-control/README.md) |

Client 설치 경로는 배포 템플릿에서 `/opt/bc250-llm-client`, API는 loopback 8010, 데이터는 `/var/lib/bc250-llm-client`를 사용한다. systemd 템플릿의 계정·그룹은 대상 환경에 맞게 설정한다. Python venv는 최종 경로에서 생성한다. 이동한 venv는 생성 당시 절대 경로를 참조할 수 있다.

## 네트워크와 환경

Caddy 사이트 안에 `/llm/*` 경로를 추가하고 기존 Studio 경로를 유지한다. `/llm/api/*`는 API, 나머지는 정적 파일과 SPA fallback으로 처리한다. gate 8081, llama 8080, Studio 8090과 외부 Wiki 8020은 공개 브라우저 endpoint가 아니다.

local 인증에서는 허용 Host 목록과 빈 CORS 설정을 사용한다. LAN HTTP는 암호화하지 않으며 허용된 네트워크의 사용자가 공유 앱에 접근한다. 원격 접근을 위한 Caddy 터널 예시는 다음과 같다.

```sh
ssh -N -L 8443:127.0.0.1:80 user@192.0.2.10
# http://127.0.0.1:8443/llm/
```

예시 주소와 계정은 실제 값으로 바꾼다. gate/llama 포트는 터널링 대상에 넣지 않는다. 환경 파일·키·DB·venv·node_modules는 소스 패키지나 Git에 포함하지 않는다.

## 기능 활성화

- **Studio**: `IMAGEGEN_ENABLED=true`, 서버용 일반 client key, 유효한 loopback `IMAGEGEN_URL`을 사용한다. `/api/v1/image-generation/capabilities`의 available과 모델의 execution_enabled를 확인한다. 실제 이미지는 사용자의 제출 버튼으로 생성한다.
- **모델 제어**: 공용 gate·worker·Studio 잠금/journal 계약과 credential을 함께 구성한다. Client와 worker 활성화 설정은 별개다. capability 기록은 모델 파일·설정·runtime에 맞아야 한다.
- **통합 Wiki**: `WIKI_STORE=integrated`로 내부 저장소를 선택한다. 구 외부 문서는 자동 import하지 않는다. 저장소 전환은 단순 UI 토글이 아니며 두 writer의 동시 쓰기를 피해야 한다.
- **요약 복구**: DB 옆 `.summary-locks` 디렉터리를 서비스 사용자가 쓸 수 있어야 한다. 같은 호스트의 worker가 같은 DB와 디렉터리를 공유한다.

## 배포 동작

GitHub Actions는 main push와 main의 수동 실행을 지원한다. 서버 `.deployed-components`에 기록된 성공 배포 구성요소와 현재 backend/frontend 해시를 비교한다. 변경된 구성요소만 검사·교체하고, 수동 실행이나 상태가 없으면 전체를 처리한다.

backend는 pytest 후 교체·재시작하고 frontend는 lint/test/build 후 정적 파일을 교체한다. production environment와 SSH secrets를 사용한다. SSH 공개 호스트 키는 별도로 검증한 값을 등록한다. workflow의 필수 secret 이름은 `BC250_SSH_PRIVATE_KEY`, `BC250_SSH_HOST`, `BC250_SSH_PORT`, `BC250_SSH_USER`, `BC250_SSH_KNOWN_HOSTS`다.

배포 스크립트는 환경 파일과 Caddy 설정을 자동 변경하지 않는다. Client 재시작은 SQLite migration을 실행할 수 있다. 일반 Client 배포가 gate·worker·외부 Studio를 설치하거나 그 설정까지 맞추지는 않는다.

파일 교체 이후 실패하면 이전 파일을 `.deploy.*`에 보존하고 자동으로 예전 앱을 재시작하지 않는다. DB가 이미 바뀌었을 수 있기 때문이다. 별도 경로로 파일을 변경한 서버는 배포 구성요소 상태를 재조정하거나 수동 전체 배포를 사용한다.

## 백업과 복구

1. 모델 전환과 이미지 작업, 배포·재시작 요청을 조율한다. 미해결 전환·hold·journal 상태가 있으면 기록을 보존하고 먼저 조사한다.
2. 앱 파일·환경·서비스 설정과 DB를 백업한다. SQLite backup API 또는 쓰기가 정지된 일관된 snapshot을 사용한다. WAL이 있는 DB의 본 파일만 임의로 복사하지 않는다.
3. 업데이트 시 모든 구 worker를 정지하고 새 코드를 같은 구성으로 실행한다. 요약 revision을 모르는 구 worker와 새 worker를 혼합 실행하지 않는다.
4. 복구는 DB schema·writer 호환성을 먼저 확인한다. 통합 Wiki 저장소 식별자를 모르는 코드로 writer를 자동 복귀시키지 않는다.
5. 전체 DB 백업 복원은 백업 이후 자료를 덮어쓰므로 해당 자료를 별도 보존한다. 즉석 DROP COLUMN으로 migration을 되돌리지 않는다.

모델 제어 복구는 [모델 제어](model-control.md)의 소유 hold·예약·journal 규칙을 따른다. 초기 gate state를 만드는 provision은 기존 state 위에 실행하지 않는다. gate의 uncertainty marker를 자동 삭제하지 않는다.

## 검사 도구

```sh
PYTHONPATH=. APP_API_KEY=client-key LLM_GATE_API_KEY=gate-key .venv/bin/python -m pytest -q
npm --prefix frontend run lint
npm --prefix frontend test -- --maxWorkers=1 --testTimeout=15000
npm --prefix frontend run build
```

소켓 테스트는 Unix/TCP bind 권한이 필요하다. 자식 Python 프로세스에도 `PYTHONPATH=.`를 전달한다. Studio 연동 검사는 `BC250_STUDIO_SOURCE`로 별도 소스를 지정하는 테스트가 있다. 테스트 실행 수치는 현재 기능 설명에 고정하지 않는다.

운영 상태 확인에는 `/api/v1/health`, systemd 상태와 journal, `ai-model status`, gate 상태 API를 사용한다. `scripts/check_image_prompt.py`는 gate 의도/프롬프트 JSON 계약을 검사하며 실제 이미지 작업을 제출하지 않는다. 모델 제어 설치에는 `scripts/coordination_status.py`, `scripts/release_startup_hold.py` 등 별도 도구가 있으며 도움말과 권한·상태 조건을 따른다. startup 해제는 상태 변경 작업이다.
