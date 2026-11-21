# BC-250 LLM Client

BC-250의 LLM gate를 사용하는 웹 채팅 클라이언트입니다. 일반 대화·롤플레잉, 독립 이미지 프롬프트 작성, Studio 이미지 작업, 공유 Wiki, 서버 공용 LLM 모델 전환을 제공합니다.

브라우저는 동일 출처의 `/llm/api/v1`에 접속하고 FastAPI가 LLM gate와 Studio에 요청합니다. 모델 키와 내부 서비스 주소를 브라우저에 전달하지 않습니다. 대화·요약·Wiki·작업 상태는 SQLite에 저장합니다.

## 문서

| 문서 | 내용 |
|---|---|
| [현재 구성](docs/architecture.md) | 서비스 연결, 코드 구조, 데이터, 인증과 설정 |
| [채팅과 컨텍스트](docs/chat-context.md) | 대화 모드, 입력 예산, 압축, 작업 복구와 편집 충돌 |
| [이미지 프롬프트와 생성](docs/image-generation.md) | 작성 방식, 결과 편집, Studio 작업과 자산 |
| [공유 Wiki와 기억](docs/wiki.md) | 저장소 선택, 문서·별칭·주소, 검색 근거, 자동 기억 |
| [모델 제어](docs/model-control.md) | UI/API/CLI, worker, gate, 잠금과 복구 |
| [개발과 운영](docs/operations.md) | 실행, 검사, 배포 구성, 백업과 복구 |
| [최우선 컨텍스트 구현 Task](docs/tasks/context-preservation.md) | DeepSeek 4.1 Flash용 단계별 구현·UI 요구사항 |
| [TODO](docs/TODO.md) | 남은 구현 범위와 운영·실모델 검증 |

구성 설명은 저장소 코드의 동작과 설정 기본값을 기준으로 합니다. 실행 서버의 활성 모델과 기능 활성화 여부는 API와 서비스의 유효 설정으로 확인합니다.

## 로컬 개발

```sh
cp .env.example .env
# .env에 APP_API_KEY와 LLM_GATE_API_KEY 등 환경에 맞는 값을 설정
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt
PYTHONPATH=. .venv/bin/uvicorn backend.app.main:app --reload --port 8010
```

다른 터미널에서 프런트엔드를 실행합니다.

```sh
cd frontend
npm ci
npm run dev
```

Vite에는 API proxy가 없습니다. 동일 출처 API 접근은 Caddy 설정 또는 별도 개발 구성이 필요합니다. 서비스 환경 변수는 [.env.example](.env.example), systemd와 Caddy 구성은 [deploy](deploy/)를 참고합니다.

## 검사

```sh
PYTHONPATH=. APP_API_KEY=client-key LLM_GATE_API_KEY=gate-key .venv/bin/python -m pytest -q
npm --prefix frontend run lint
npm --prefix frontend test -- --maxWorkers=1 --testTimeout=15000
npm --prefix frontend run build
```

일부 backend 테스트는 Unix/TCP 소켓, Linux/systemd 또는 별도 Studio 소스가 필요합니다. 테스트용 키는 운영 환경에 사용하지 않습니다.
