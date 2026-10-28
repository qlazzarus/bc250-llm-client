# BC-250 LLM Client

구현·검증 기록: [결과 카드 편집 및 이미지 생성 연동 — GI01~GI08](docs/plans/image-generation-integration-tasks.md). 이미지 프롬프트 결과는 초안으로 편집할 수 있고, Studio 연동을 서버에서 명시적으로 활성화한 경우에만 사용자의 버튼 클릭으로 이미지 작업을 요청합니다.

배포 예시의 `deployuser`, `bc250.example.com`, `192.0.2.10`은 예시 값이다. 실제 환경에 맞게 바꾸고, systemd 서비스 템플릿의 `YOUR_SERVICE_USER`와 `YOUR_SERVICE_GROUP`도 서버 계정으로 수정한 후 설치한다.

BC-250의 llama.cpp를 직접 노출하지 않는 사내용 웹 채팅 클라이언트입니다. 브라우저는 Caddy의 동일 출처 `/llm/api/v1`에만 접속하고, FastAPI만 루프백 LLM gate(`127.0.0.1:8081`)에 요청합니다. `8080`, `8081`, gate API key는 프런트엔드에 포함되지 않습니다.

## 구성

```
Browser ─ HTTPS ─ Caddy (/llm/, /llm/api/*) ─ loopback ─ FastAPI:8010
                                                       │
                                                       └─ loopback ─ LLM gate:8081 ─ llama-server:8080
                                                       │
                                                       └─ loopback ─ Studio:8090 (image_priority, opt-in)
```

기본 제목 `새 대화`로 만든 채팅방은 첫 답변이 완료된 후 첫 메시지를 바탕으로 제목을 자동 생성합니다. 직접 지정하거나 변경한 제목은 유지합니다. 제목 생성이 실패하면 첫 메시지의 일부를 제목으로 사용합니다.

대화와 메시지는 SQLite에 저장합니다. assistant 최종 `content`만 저장·표시하며 `reasoning_content`는 저장하지 않습니다. 일반 대화와 롤플레잉은 최근 완료 대화와 장기 기억 요약을 참고합니다. 기본 생성값은 `temperature=0.7`, `max_tokens=512`, 최근 최대 200개 메시지이며 토큰 예산을 넘는 오래된 턴은 제외합니다. gate가 이미지 작업을 우선하면 클라이언트는 대기 안내를 보입니다.

Chat replies use Server-Sent Events: only final answer token deltas are rendered and saved. Reasoning deltas are discarded. If a model consumes its entire budget on reasoning and produces no final content, the UI explains this rather than exposing reasoning.

For the deployed Qwen3.5 model, normal chat sends `chat_template_kwargs.enable_thinking=false` by default so a short request cannot exhaust its entire response budget on hidden reasoning. Set server-only `LLM_DISABLE_REASONING=false` only when deliberate reasoning-mode workloads are required.

## 로컬 개발

```bash
cp .env.example .env
# .env의 LLM_GATE_API_KEY를 실제 값으로 변경
python3 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt
PYTHONPATH=. .venv/bin/uvicorn backend.app.main:app --reload --port 8010
cd frontend && npm install && npm run dev
```

Vite 개발 서버에는 proxy를 의도적으로 두지 않았습니다. 외부 노출 없이 테스트하려면 API도 SSH로 터널링하거나 Caddy `/llm/`을 사용합니다.

## 로그인 없는 개인용 설정

서버 환경 파일에서 `APP_AUTH_MODE=local`을 설정하고 `APP_ALLOWED_HOSTS`에 브라우저가 사용하는 서버의 LAN IP 또는 신뢰할 수 있는 호스트 이름을 쉼표로 추가합니다. SSH 터널의 `localhost`와 `127.0.0.1`도 유지합니다. `CORS_ORIGINS`는 비워 둡니다. 기존 설치는 새 프런트엔드를 배포하기 전에 반드시 이 설정으로 전환해야 합니다. `LLM_GATE_API_KEY`는 계속 필요합니다.

`deploy/Caddyfile.snippet`에는 접속 IP 제한이 없습니다. 공유기에서 웹 포트를 외부에 포워딩하지 않고 서버 방화벽과 IPv6 노출 여부를 확인합니다. 접근 가능한 기기는 모두 앱을 사용할 수 있으며, LAN HTTP는 암호화되지 않습니다.

API는 허용된 Host, 동일 출처 및 전용 요청 헤더를 검사합니다. 이 검사는 브라우저를 통한 외부 사이트의 요청을 막기 위한 것이며 사용자 인증이 아닙니다. 허용된 LAN의 모든 기기는 대화 열람·삭제와 게이트 제어를 할 수 있습니다. 계정별 격리는 제공하지 않습니다.

## SSH 터널 테스트

관리 PC에서 BC-250의 Caddy만 로컬에 연결합니다.

```bash
ssh -N -L 8443:127.0.0.1:80 user@192.0.2.10
# 브라우저에서 http://127.0.0.1:8443/llm/
```

8080/8081을 포워딩하지 마십시오. 로그인 없이 접속합니다. 이 모드에서는 허용된 네트워크의 모든 사용자가 대화와 관리 기능에 접근할 수 있습니다.

## 목업 SCP 배포 (`/llm/` 우선)

기존 `/opt/bc250-ai-studio`는 변경하지 않습니다.

### 1. 개발 PC에서 검증·패키징

`.env`와 가상환경, `node_modules`는 절대 전송하지 않습니다. 아래는 프런트엔드를 빌드한 뒤, 필요한 소스와 배포 파일만 하나의 아카이브로 전송합니다.

```bash
PYTHONPATH=. .venv/bin/pytest -q
(cd frontend && npm run lint && npm run build)
tar -czf /tmp/bc250-llm-client-mock.tgz \
  --exclude=.env --exclude=.venv --exclude=.pytest_cache \
  --exclude=frontend/node_modules --exclude=tests/__pycache__ \
  backend frontend/dist frontend/package.json frontend/package-lock.json \
  deploy scripts docs .env.example README.md
scp -P 2222 /tmp/bc250-llm-client-mock.tgz deployuser@bc250.example.com:/tmp/
```

### 2. BC-250에서 설치

아래는 서버에서 `deployuser`로 로그인한 뒤 실행합니다. `/opt`, `/etc`, Caddy와 systemd 변경은 `sudo` 권한이 필요합니다. 처음 배포 전 현재 Caddyfile을 별도 백업합니다.

가상환경은 반드시 최종 `/opt/bc250-llm-client` 경로에서 만든다. Python 가상환경의 실행 파일은 생성 당시의 절대 경로를 참조하므로, 임시 디렉터리에서 만든 `.venv`를 이동하면 systemd가 `203/EXEC`로 실패할 수 있다.

```bash
sudo install -d -o deployuser -g deployuser /opt/bc250-llm-client /var/lib/bc250-llm-client
sudo cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.before-bc250-llm-client
sudo -u deployuser tar -xzf /tmp/bc250-llm-client-mock.tgz -C /opt/bc250-llm-client
sudo -u deployuser python3 -m venv /opt/bc250-llm-client/.venv
sudo -u deployuser /opt/bc250-llm-client/.venv/bin/pip install -r /opt/bc250-llm-client/backend/requirements.txt
sudo chown -R deployuser:deployuser /opt/bc250-llm-client /var/lib/bc250-llm-client
```

Create the server-only secret file without echoing values to terminal history. Configure local mode and keep the gate key secret.

```bash
sudo install -m 600 -o root -g deployuser /dev/null /etc/bc250-llm-client.env
sudoedit /etc/bc250-llm-client.env
```

Use this content in that file (replace every placeholder):

```dotenv
APP_HOST=127.0.0.1
APP_PORT=8010
DATABASE_PATH=/var/lib/bc250-llm-client/bc250-llm-client.db
APP_AUTH_MODE=local
APP_ALLOWED_HOSTS=localhost,127.0.0.1,192.0.2.10
LLM_GATE_URL=http://127.0.0.1:8081
LLM_GATE_API_KEY=replace-with-existing-gate-key
LLM_TIMEOUT_SECONDS=180
MAX_REQUEST_BYTES=65536
MAX_HISTORY_MESSAGES=200
MAX_COMPLETION_TOKENS=2048
DEFAULT_MAX_TOKENS=512
MAX_CONCURRENT_GENERATIONS=2
STORE_REASONING_CONTENT=false
LLM_DISABLE_REASONING=true
LLM_CONTEXT_TOKENS=16384
LLM_CONTEXT_SAFETY_TOKENS=512
IMAGE_PROMPT_MAX_TOKENS=512
IMAGEGEN_ENABLED=false
IMAGEGEN_ALLOW_FAKE=false
IMAGEGEN_URL=http://127.0.0.1:8090
IMAGEGEN_API_KEY=
IMAGEGEN_TIMEOUT_SECONDS=20
MAX_IMAGE_JOBS=10
MAX_IMAGE_ASSET_BYTES=20971520
IMAGE_JOB_POLL_MIN_INTERVAL_SECONDS=2
ROLEPLAY_SUMMARY_TRIGGER_TOKENS=5000
ROLEPLAY_SUMMARY_KEEP_RECENT_TURNS=6
ROLEPLAY_SUMMARY_MAX_TOKENS=512
```

서버의 실제 컨텍스트 한도를 확인해 `LLM_CONTEXT_TOKENS`를 설정하세요. 기존 서버 환경 파일은 자동으로 바뀌지 않으므로 16k 서버에서는 `LLM_CONTEXT_TOKENS=16384`, `MAX_HISTORY_MESSAGES=200`을 직접 반영해야 합니다. 토큰 수는 현재 UTF-8 바이트 기반 추정치이며 모델의 정확한 토큰 수가 아닙니다.

일반 대화와 롤플레잉의 장기 기억 요약은 위 임계값을 넘은 **완료된** 턴만 대상으로 백그라운드에서 실행됩니다. 요약은 기존 gate를 통해서만 실행되며, image_priority 대기 정책과 생성 동시성 제한을 공유합니다. 실패하면 기존 요약은 유지됩니다.

Install and start only the new service:

```bash
sudo cp /opt/bc250-llm-client/deploy/bc250-llm-client.service /etc/systemd/system/
sudoedit /etc/systemd/system/bc250-llm-client.service
sudo systemctl daemon-reload
sudo systemctl enable --now bc250-llm-client
sudo systemctl status bc250-llm-client --no-pager
curl -fsS http://127.0.0.1:8010/api/v1/health
```

### 3. Caddy route

Append the contents of `deploy/Caddyfile.snippet` **inside the existing HTTP site block**, preserving every existing Art Studio rule. The snippet reserves only `/llm/*`; it does not take ownership of `/api/*`.

```bash
sudoedit /etc/caddy/Caddyfile
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
curl -I -H 'Host: 192.0.2.10' http://127.0.0.1/llm/
```

### 4. Smoke test

After checking the router and firewall, open `http://192.0.2.10/llm/`, make a short chat request, and open “시스템 상태” to review the gate and exposed model list. Never forward or expose ports 8080, 8081, 8090, or 8010.

## 이미지 프롬프트 gate 점검

이 점검은 한국어 예제로 구조화된 의도 분석과 모델별 프롬프트 작성의 두 JSON 계약을 확인한다. 지정된 영문 문구가 분석과 프롬프트에 보존되는지도 검사한다. Studio에 이미지를 생성하거나 작업을 제출하지 않는다. `LLM_GATE_URL`은 정확히 `http://127.0.0.1:8081`만 허용하고 redirect를 따라가지 않으므로, gate key가 다른 주소로 전달되지 않는다. 키·Authorization 헤더·reasoning 원문·테스트 프롬프트는 출력하지 않는다.

`/etc/bc250-llm-client.env`는 root 소유 600 파일이므로 `deployuser`로 직접 source하면 안 된다. 아래 명령은 root가 환경 파일을 읽은 뒤 필요한 gate 변수만 보존하여 `deployuser` 권한으로 스크립트를 실행한다. 키를 터미널에 출력하거나 파일 권한을 넓히지 않는다.

```bash
sudo bash -c '
  set -a
  . /etc/bc250-llm-client.env
  set +a
  cd /opt/bc250-llm-client
  exec sudo --preserve-env=LLM_GATE_URL,LLM_GATE_API_KEY,LLM_MODEL_ID,IMAGE_PROMPT_MAX_TOKENS \
    -u deployuser /opt/bc250-llm-client/.venv/bin/python -m scripts.check_image_prompt
'
```

성공하면 활성 LLM ID와 각 단계의 시도 횟수, 종료 사유, 경과 시간, 숫자 usage, JSON 계약 통과 여부만 표시한다. 각 단계는 유효하지 않은 응답을 한 번 재시도한다. 이 점검은 프롬프트 품질이나 실제 이미지 생성 성공을 보장하지 않는다. 실패 시 gate가 `response_format: json_schema`를 지원·전달하는지 확인하고, 8080으로 우회하지 않는다.

브라우저에서는 이미지 프롬프트 대화를 만들고 아래를 확인한다. `IMAGEGEN_ENABLED=false` 상태에서는 결과 카드의 이미지 생성 요청 버튼이 비활성화되는 것이 정상이다.

- 실제 이미지를 생성하기 전 영어 프롬프트·지원 해상도를 수정하고 `초안 저장`할 수 있는지
- `원문으로 되돌리기`, `프롬프트 복사`, `설정 JSON 복사`가 수정본을 사용하는지. HTTP에서 자동 복사가 제한되면 표시되는 텍스트 영역을 선택해 수동 복사할 수 있는지
- 이전 결과를 자동으로 참조하지 않는지. 두 번째 요청은 “더 어둡게”처럼 축약하지 말고 원하는 전체 장면을 다시 입력해야 하는지
- Studio에서 이미지 작업이 진행 중일 때 대기 안내가 보이고, 작업 종료 뒤 요청이 재개되는지

## Studio 연동 사전점검과 활성화

기본값은 `IMAGEGEN_ENABLED=false`다. 배포 직후에는 이 값을 유지해 LLM Client가 Studio에 어떤 작업도 제출하지 않게 한다. 아래 사전점검은 Studio 상태·모델 정보만 읽고 이미지 생성, 취소, 자산 다운로드를 수행하지 않는다. 명령은 BC-250에서 실행하며 키를 출력하지 않는다.

```bash
sudo systemctl status bc250-llm-client --no-pager -l
sudo bash -c '
  set -a
  . /etc/bc250-llm-client.env
  set +a
  curl -fsS -H "X-BC250-Client: 1" \
    http://127.0.0.1:8010/api/v1/image-generation/capabilities
'
```

`disabled`가 반환되면 배포 직후의 정상 상태다. Studio 일반 client key가 준비되고 Studio 자체가 정상인지 확인한 뒤에만 환경 파일을 편집한다. 관리 키가 아니라 Studio jobs/models/status에 허용된 일반 client key를 사용한다.

```bash
sudoedit /etc/bc250-llm-client.env
# 다음 두 값만 실제 환경에 맞게 변경한다. URL은 변경하지 않는다.
# IMAGEGEN_ENABLED=true
# IMAGEGEN_API_KEY=<Studio 일반 client key>

sudo systemctl restart bc250-llm-client
sudo bash -c '
  set -a
  . /etc/bc250-llm-client.env
  set +a
  curl -fsS -H "X-BC250-Client: 1" \
    http://127.0.0.1:8010/api/v1/image-generation/capabilities
'
```

응답이 `"available": true`이고 `models`에서 사용할 모델의 `execution_enabled`가 true일 때만 브라우저에서 이미지 프롬프트를 하나 만든다. 결과 카드를 편집·저장한 뒤 사용자가 `이미지 생성 요청`을 한 번 누르는 것이 첫 실제 GPU 실행이다. `submission_unknown`이면 재클릭하지 말고 Studio 작업 목록에서 `client_reference`를 먼저 확인한다. 앱은 자동 재제출하지 않는다.

## Full BC-250 deployment

For a direct checked-out deployment instead of SCP packaging:

```bash
cd /opt/bc250-llm-client
sudo -u deployuser python3 -m venv .venv
sudo -u deployuser .venv/bin/pip install -r backend/requirements.txt
(cd frontend && sudo -u deployuser npm ci && sudo -u deployuser npm run build)
sudo install -m 600 -o root -g deployuser .env /etc/bc250-llm-client.env
sudo sed -i 's|DATABASE_PATH=.*|DATABASE_PATH=/var/lib/bc250-llm-client/bc250-llm-client.db|' /etc/bc250-llm-client.env
sudo cp deploy/bc250-llm-client.service /etc/systemd/system/
sudoedit /etc/systemd/system/bc250-llm-client.service
sudo systemctl daemon-reload && sudo systemctl enable --now bc250-llm-client
```

Add the contents of `deploy/Caddyfile.snippet` to the existing Caddy site block, then validate before reload:

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
curl -fsS http://127.0.0.1:8010/api/v1/health
curl -I -H 'Host: 192.0.2.10' http://127.0.0.1/llm/
sudo ss -ltnp | rg ':8010|:8080|:8081' # all three must be loopback-only
```

서비스를 재시작하면 image_prompt 대화의 독립 요청 마이그레이션이 자동 적용된다. 배포 전에 서비스를 중지하고 SQLite를 백업하는 업데이트 절차는 아래 릴리스 업데이트 섹션을 따른다. 기존 이미지 기록·설정은 삭제하지 않지만 이후 LLM 입력에는 사용하지 않는다.

Keep `CORS_ORIGINS` empty in local mode. Control external access with the router and server firewall.

## Tests and build

```bash
PYTHONPATH=. .venv/bin/pytest -q
cd frontend && npm run lint && npm run build
```

## 릴리스 업데이트와 롤백

업데이트는 새 소스를 별도 릴리스 디렉터리에 풀고, 기존 서비스를 멈춘 뒤 디렉터리를 교체한다. `.venv`는 반드시 최종 `/opt/bc250-llm-client` 경로에서 새로 만든다. SQLite는 교체 전 백업하지만, 백업 시점 이후에 생긴 대화는 롤백 시 사라진다.

개발 PC에서 위 패키징·전송을 마친 뒤 서버에서 실행한다.

```bash
RELEASE_ID="$(date +%Y%m%d-%H%M%S)"
RELEASE_DIR="/opt/bc250-llm-client.release-${RELEASE_ID}"

sudo install -d -o deployuser -g deployuser "$RELEASE_DIR"
sudo -u deployuser tar -xzf /tmp/bc250-llm-client-mock.tgz -C "$RELEASE_DIR"

sudo systemctl stop bc250-llm-client
sudo install -d -m 700 /var/backups/bc250-llm-client
sudo cp -a /var/lib/bc250-llm-client/bc250-llm-client.db \
  "/var/backups/bc250-llm-client/bc250-llm-client.db.before-${RELEASE_ID}"

sudo mv /opt/bc250-llm-client "/opt/bc250-llm-client.previous-${RELEASE_ID}"
sudo mv "$RELEASE_DIR" /opt/bc250-llm-client
sudo -u deployuser python3 -m venv /opt/bc250-llm-client/.venv
sudo -u deployuser /opt/bc250-llm-client/.venv/bin/pip install -r /opt/bc250-llm-client/backend/requirements.txt
# 기존 운영 서비스 파일은 유지한다. 변경이 필요하면 별도 검토 후 수정한다.
sudo systemctl daemon-reload
sudo systemctl start bc250-llm-client
sudo systemctl status bc250-llm-client --no-pager -l
```

서비스가 시작하지 않으면 아래 롤백을 실행한다. `RELEASE_ID`에는 위에서 사용한 같은 값을 넣는다.

```bash
sudo systemctl stop bc250-llm-client
sudo mv /opt/bc250-llm-client "/opt/bc250-llm-client.failed-${RELEASE_ID}"
sudo mv "/opt/bc250-llm-client.previous-${RELEASE_ID}" /opt/bc250-llm-client
sudo cp \
  "/var/backups/bc250-llm-client/bc250-llm-client.db.before-${RELEASE_ID}" \
  /var/lib/bc250-llm-client/bc250-llm-client.db
sudo systemctl daemon-reload
sudo systemctl start bc250-llm-client
```

배포 뒤에는 `curl -fsS http://127.0.0.1:8010/api/v1/health`, `sudo journalctl -u bc250-llm-client -n 80 --no-pager`, 그리고 위 이미지 프롬프트 gate 점검을 실행한다. 비밀값을 git·아카이브·로그에 넣지 않는다.

## System-status API

`GET /api/v1/admin/gate-status` and `PUT /api/v1/admin/gate-control` are available to every client allowed to use the local API. The “시스템 상태” screen presents the gate health data and the model IDs returned by `/v1/models`; gate `/health` and `/control` response fields are passed through, preserving gate-specific queue/pause/image state.


## 다중 이미지 모델과 옵션

이미지 프롬프트 모드는 `klein-4b-q4`, `klein-4b-q8`, `pornmaster-klein-9b`, `z-image-turbo`, `z-image-nsfw-v2`, `realistic-vision-v6`, `moss-image-nsfw`를 지원한다. LLM에 사용자가 지정한 모델을 따르도록 지시하고, 지정이 없으면 요청을 바탕으로 선택하도록 한다. Studio가 온라인이면 `/api/v1/models`로 확인한 실행 가능한 모델만 LLM에 전달한다. Studio가 꺼져 있어도 저장된 카탈로그로 프롬프트를 작성할 수 있다.

LLM은 gate의 `/v1/models`에서 조회한다. 서버 `.env`의 `LLM_MODEL_ID=qwen36-35b-a3b`로 고정할 수 있으며, 이 값은 gate가 반환하는 ID와 일치해야 한다. 이미지 프롬프트 요청은 `enable_thinking=false`와 텍스트 전용 JSON Schema를 사용한다. 추천 모델과 한국어 추천 이유는 결과 카드에 표시하며, 이는 품질 비교가 검증된 결과가 아니다. LLM이 선택한 모델에 서버가 balanced 프리셋을 적용하며 LLM이 반환한 수치 설정은 사용하지 않는다. 실제 qwen36 gate에서 새 스키마 수용 여부는 별도로 확인해야 한다.

결과 카드에서 모델, fast/balanced/quality 프리셋, 해상도, steps(1~100), CFG, seed(-1은 무작위), sampler, scheduler, 지원 모델의 negative prompt를 편집할 수 있다. 모델 변경 시 balanced 설정으로 초기화한다. 프리셋의 experimental 표시는 그대로 보여 주며, 지원되는 옵션도 검증된 이미지 품질을 보장하는 것은 아니다.

이미지 프롬프트 입력 위에서 `AI 최적화` 또는 `직접 번역`을 선택한다. 직접 번역은 원문에 없는 장면 요소나 모델별 스타일을 추가하지 않도록 LLM에 지시한다. 선택한 방식은 결과 카드와 이미지 작업 기록에 남으며, 이후 사용자가 프롬프트를 편집해도 최초 작성 방식으로 표시된다. 이전 기록은 방식이 기록되지 않아 빈 값으로 유지된다. 실제 번역 충실도는 생성된 문구를 검토해야 한다.

`원본 그대로`를 선택하면 LLM 분석과 번역을 거치지 않고 입력한 문구를 프롬프트로 사용한다. 앞뒤 공백은 제거하며 이미지 모델을 직접 선택해야 한다. LLM이 오프라인이어도 결과 카드를 만들 수 있지만 이미지 제출에는 Studio가 필요하다. 원문이 한국어라면 이미지 모델의 한국어 이해도에 따라 결과가 달라질 수 있다.

입력 위의 이미지 모델을 `추천받기`로 두거나 직접 선택할 수 있다. 직접 선택하면 해당 모델의 전략만 LLM에 제공하고 다른 모델 응답을 거부한다. Studio가 실행 가능한 모델을 보고하는 동안에는 사용할 수 없는 모델 선택을 제한한다. Studio가 오프라인이면 프롬프트 작성은 계속 가능하지만 이미지 제출은 Studio 확인이 필요하다. 결과 카드에서 모델만 바꿀 때는 프롬프트를 자동으로 다시 작성하지 않는다.

비교할 때는 같은 한국어 요청을 두 방식으로 각각 작성하고, 결과 카드에서 같은 모델·프리셋·해상도와 고정 seed를 선택한 뒤 각각 확인하여 생성한다. 각 작업의 `생성 기록`에서 `비교 기록 저장 (JSON)`을 누르면 원본 요청, 최초 프롬프트와 실제 제출한 프롬프트, 설정, 확인된 실행 정보와 사용자 평가를 파일로 저장할 수 있다. 이미지 파일은 기존 다운로드 버튼으로 별도 저장한다. 확인할 수 없는 값은 `null`이며 모델 버전은 현재 제공하지 않는다. 이미지 품질은 사용자가 직접 평가해야 한다. 자동 평가나 두 기록의 자동 비교는 아직 제공하지 않는다.

작업의 `생성 기록`에는 선택한 프리셋, Studio가 반환한 실제 seed, 해상도, steps, CFG, 프롬프트, 양자화 및 LoRA 상태를 표시한다. 해상도·steps·CFG를 직접 바꾸면 `사용자 설정`으로 기록한다. 입력값이 선택한 프리셋과 다르거나 서버 프리셋이 바뀌면 다시 적용해야 한다. 실행 시간은 Studio의 시작부터 완료까지이며 이미지 처리 외에 GPU 반환·서비스 복구가 포함될 수 있다. 이전 작업의 확인되지 않은 프리셋은 `기록 없음`으로 표시한다. 완료된 이미지의 `사용자 평가`에서 1~5점과 메모를 저장하거나 지울 수 있다. 평가는 LLM의 점수가 아닌 사용자 기록이며 로컬 작업 DB에 저장된다.

- 제공된 설정에서 MOSS balanced는 512×512, 20 steps, CFG 3, Euler, discrete이고 fast와 quality는 experimental이다. 실제 설정은 Studio API에서 확인한다. Studio가 작업별 LoRA 제어를 지원한다고 알리면 결과 카드에 ON/OFF가 표시된다. ON은 Studio에 설정된 강도와 trigger를 사용하고, OFF는 기본 MOSS를 사용한다. 강도는 클라이언트에서 임의로 변경하지 않는다. LoRA OFF 이미지 품질은 실제 BC-250에서 아직 검증하지 않았다.
- PornMaster의 negative prompt는 CFG 2에서만 허용한다.
- Realistic Vision은 CFG 0~30 및 negative prompt를 지원한다. 나머지 모델의 고정값·금지 옵션은 각 모델 계약에 따라 검증한다.

LLM에는 사용자가 명시한 금지 요소만 Negative Prompt로 작성하도록 지시한다. 서버는 선택 모델의 현재 balanced 프리셋과 옵션 조건을 검증하고, 허용되지 않는 결과는 제외하며 카드에 안내한다. 지원 모델의 Negative Prompt는 카드에서 편집할 수 있다. 프리셋 변경으로 사용 조건이 맞지 않으면 기존 내용을 유지하고 오류를 표시하므로, 설정이나 내용을 사용자가 조정해야 한다. 제외 조건이 실제 이미지에 반영될지는 모델과 생성 엔진에 따라 달라질 수 있다.

Studio가 활성화되어 응답하면 `/api/v1/models`에서 `models.yaml` 기반 프리셋과 옵션을 읽고 검증해 사용한다. 경로 등 비공개 필드는 클라이언트에 전달하지 않는다. 계약이 빠졌거나 유효하지 않은 모델은 실행할 수 없다. `backend/app/image_models.json`은 알려진 모델 ID와 Studio 오프라인 시 프롬프트·초안 작성을 위한 대체 계약이며 배포 시 포함해야 한다. 새 모델은 자동 등록하지 않으므로 별도 프로파일과 카탈로그 추가가 필요하다. Studio를 사용할 수 없을 때 저장한 초안도 실제 제출 직전 최신 계약으로 다시 검증한다. 이미 접수된 동일 요청의 재조회는 설정 변경 후에도 기존 작업을 재제출하지 않는다.

CPU 테스트 용도로만 `IMAGEGEN_ALLOW_FAKE=true`를 지정하고 Studio 자체가 `fake` 엔진으로 실행 중이면, 실제 Studio 모델 API에서 `fake` 계약을 읽어 테스트 모델을 표시한다. 기본값은 `false`이며 Fake 모델은 추천 대상이 아니다. 테스트 모델은 프롬프트를 작성하기 전에 직접 선택해야 하고, 카드·작업 기록·비교 기록에 테스트 결과로 표시된다. 일반 GPU 운영에서는 이 값을 `false`로 유지한다.

Studio 소스와 Python 의존성이 준비된 개발 환경에서는 아래 선택적 통합 테스트가 임시 디렉터리의 Studio Fake Engine과 실제 큐를 사용해 제출·상태 조회·PNG 반환을 확인한다. 한국어 의도 분석과 프롬프트 작성은 모의 LLM 응답으로 검증한다. 운영 서비스를 실행하거나 기존 데이터를 사용하지 않는다.

```bash
BC250_STUDIO_SOURCE=/path/to/bc250-ai-studio PYTHONPATH=. \
  /path/to/bc250-ai-studio/.venv/bin/python -m pytest -q tests/test_studio_fake_integration.py
```

### Studio 작업별 MOSS LoRA 지원 적용

클라이언트 토글은 Studio가 `lora_toggle_supported`를 명시적으로 제공할 때만 나타난다. 기존 Studio는 이 기능 없이 계속 동작한다. 새 Studio 구현은 작업 요청의 `lora_enabled`를 검증하고, 선택한 값을 작업별 모델 스냅샷에 고정해 기존 큐와 엔진을 사용한다. 전역 `models.yaml`이나 기존 작업은 변경하지 않는다.

서버 Studio 버전이 이 패치의 기준 소스와 일치하는지 먼저 검사한다. [Studio 패치](deploy/ai-studio-lora-toggle.patch)를 확인하고, 운영 적용 전에 Studio 소스와 데이터베이스를 백업한다. 아래 명령은 실제 적용 없이 충돌 여부만 확인한다.

```bash
cd /opt/bc250-ai-studio
patch --dry-run -p1 -i /opt/bc250-llm-client/deploy/ai-studio-lora-toggle.patch
```

불일치가 있으면 패치를 강제로 적용하지 않는다. 일치하고 운영 적용을 결정한 경우 패치를 적용하고 Studio 테스트를 통과한 뒤, 운영자의 기존 서비스 절차로 Studio와 LLM Client를 순서대로 재시작한다. 이후 모델 API에서 지원 여부를 확인하고, 사용자가 선택한 ON과 OFF 작업을 각각 실행해 작업 스냅샷·결과·LLM 복구를 확인한다. 이 저장소에서 실제 서버 업데이트나 GPU 실행은 수행하지 않았다.

root에서 `umask 077`로 `git apply`를 실행하면 교체된 Python 파일이 서비스 계정에서 읽을 수 없는 권한으로 생성될 수 있다. 패치 후 Studio 서비스 계정의 읽기 권한을 확인하고, 필요한 경우 패치 대상인 `backend/app/models/schemas.py`, `backend/app/services/registry.py`, `backend/tests/test_moss.py`에 각각 소스 파일 읽기 권한을 복구한다. 비밀 설정이나 데이터 디렉터리의 권한은 변경하지 않는다.

초안과 작업에는 모델 및 옵션 전체가 저장된다. SQLite 마이그레이션 8이 기존 기록을 보존하며 옵션 저장 열을 추가한다. 재시도 검증에도 옵션을 포함하므로 같은 요청 ID로 다른 옵션을 제출할 수 없다.
마이그레이션 9는 프롬프트 작성 방식 저장 열을 추가한다. 기존 기록을 임의로 분류하지 않는다. 배포 전 아래 업데이트 절차에 따라 SQLite를 백업한다.
마이그레이션 10은 요청별 이미지 모델 선택을 저장해 동일 요청 ID 재시도에서 선택이 달라지는 것을 방지한다.
마이그레이션 11은 Studio 실행 기록을 저장할 열을 추가한다. 경로를 포함한 모델 스냅샷 전체를 저장하지 않는다.
마이그레이션 12는 작업별 사용자 점수와 평가 메모를 위한 열을 추가한다.
마이그레이션 13은 초안과 작업의 프리셋 이름을 저장하며, 과거 기록의 프리셋을 추측해 채우지 않는다.

### 이미지 요청 분석 API

`POST /api/v1/image-assistant/analyze`에 일반 클라이언트 인증을 사용하여 `{"content":"..."}`를 보내면 이미지 요청 분류와 구조화된 장면 필드를 반환한다. gate의 활성 LLM과 기존 생성 동시성 제한을 공유하며 유효하지 않은 출력은 한 번 재시도한다. `exact_text`에는 원문에 실제로 포함된 문구만 허용한다. API 자체는 이미지를 생성하거나 결과를 저장하지 않는다.

AI 최적화 요청은 먼저 같은 분석을 수행하고 원문과 분석 결과를 함께 사용해 프롬프트를 작성한다. 결과 카드의 `요청 분석 확인`에서 추출값을 볼 수 있다. 추출된 `exact_text`가 최종 프롬프트에 없으면 결과를 거부한다. 분석은 추가 LLM 호출이므로 시간이 더 걸릴 수 있다. 직접 번역은 분석 단계를 거치지 않는다. 추출된 필드의 의미와 조건 보존 여부는 사용자가 원문과 비교해야 한다. 실제 Qwen 및 gate의 두 JSON 스키마 지원은 서버에서 확인해야 한다.

이미지 프롬프트 작성 단계에서 응답 JSON이 유효하지 않거나 비어 있거나 잘렸을 때, 또는 지정 문구가 누락되었을 때 한 번 재시도한다. 두 번 실패하면 요청을 실패로 기록하며 이미지 큐에 제출하지 않는다. 취소가 감지되면 다음 시도를 중단한다. 반환되는 LLM usage는 마지막 프롬프트 작성 호출의 값이며 분석이나 실패한 시도의 토큰을 합산하지 않는다.

## GitHub Actions를 통한 배포

`.github/workflows/deploy.yml`은 `main`에 푸시하거나 `main`에서 수동 실행할 때 GitHub 호스팅 runner에서 백엔드 테스트와 프런트엔드 lint/build를 수행한 후 SSH로 배포한다. `production` environment의 배포 브랜치를 `main`으로 제한한다. 외부 PR에서는 배포하지 않는다. 이 워크플로는 서버의 `/etc/bc250-llm-client.env`, Caddy 설정, SQLite DB를 수정하지 않는다.

최초 배포 전에 서버에서 환경 파일에 `APP_AUTH_MODE=local`, `APP_ALLOWED_HOSTS=localhost,127.0.0.1,192.168.0.57`, 빈 `CORS_ORIGINS`를 설정한다. 공유기와 방화벽이 외부 웹 접근을 차단하는지 확인한다. 서버 주소가 바뀌면 허용 Host도 수정해야 한다. 기존 환경 파일과 Caddyfile을 백업하고 기존 설정을 보존한다.

서버의 sudoers에 `visudo`를 통해 다음 한 줄을 추가한다. 경로는 서버에서 `command -v systemctl`로 확인한다.

```text
yeppyshiba ALL=(root) NOPASSWD: /usr/bin/systemctl restart bc250-llm-client.service
```

배포 전용 SSH 키를 별도로 생성하여 공개키만 서버의 `authorized_keys`에 추가한다. GitHub `production` environment secrets에 `BC250_SSH_PRIVATE_KEY`, `BC250_SSH_HOST`, `BC250_SSH_PORT`, `BC250_SSH_USER`, `BC250_SSH_KNOWN_HOSTS`를 등록한다. `BC250_SSH_KNOWN_HOSTS`는 `[hostname]:port` 형식의 실제 서버 공개 호스트 키 한 줄이다. `ssh-keyscan -p 2222 hostname`의 출력은 반드시 서버에서 직접 확인한 ED25519 fingerprint와 비교한 후 등록한다. `ssh-keyscan` 결과를 검증 없이 신뢰하지 않는다.

수동 실행은 GitHub Actions에서 **Deploy BC-250 client → Run workflow → main**을 선택한다. 실패 시 이전 앱 파일을 복원하고 재시작을 시도하지만 pip 의존성 변경과 DB 마이그레이션은 되돌리지 않는다. 운영 DB 백업을 유지하고, 최초 배포는 사용자가 실행 결과와 웹 접근을 확인한다. 서버 Python 및 실행 중인 가상환경에 `pip`와 인터넷 연결이 필요하다.
