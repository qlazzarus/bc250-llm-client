# 모델 전환 구현 작업과 차단 조건

2026-11-07 기준. 현재 단계·작업 상태의 단일 기준은 [통합 계획 §7](llm-model-switch-plan.md#7-실행-체크리스트와-완료-조건)이며, 이 문서는 구현·조사 근거를 보존한다. M3와 M4 핵심은 로컬에 구현했으나 실제 전환은 사용할 수 없다.

## 최신 진행 — 2026-11-10

이번 시작점은 깨끗한 `main` (`db96e85`, origin/main과 일치)이었다. 과거 기록의 672/681 등 시험 수와 “Studio 미적용” 설명은 최신 상태가 아니다. 아래 날짜별 기록은 이력으로 보존하고 현재 단계는 통합 계획 §7을 따른다.

- **읽기 전용 서버 확인:** SSH 외부 주소와 기존 키로 접속. Client(PID 1411), Studio(111463), 구형 gate(1014), llama(113928) 모두 active/running. worker socket과 `/run/bc250-gpu`, `/var/lib/bc250-model-control`, `/opt/bc250-model-control`, `/opt/bc250-llm-gate`는 미설치/부재. PID는 조사 순간값이다.
- **실행 설정/모델:** Client 실행 환경의 전환 값은 비활성, 고정 모델 ID는 비어 있다. llama health ok, Qwen3.6 IQ2_M 실제 모델 ID, models meta의 `n_ctx=16384`와 slot 0의 `n_ctx=16384`, `is_processing=false`를 확인했다. 타 모델 context·옵션 호환 검증은 남는다.
- **Studio:** 운영 `main.py`의 coordination/recovery endpoint와 GPU·switch barrier의 protocol 3 검사 코드 존재 확인. 앞선 PR #1 병합 기록과 일치한다. 패치를 다시 적용하지 않는다. 실제 credential·owned hold·공유 lock/journal 설정 확인은 root 결과 대기이며 GPU 조율 성공 증거는 아니다.
- **M5-02 불일치 수정:** 문서의 Client 소유 hold 계약과 달리 구현이 legacy pause API만 호출하던 문제를 수정했다. gate에 고유 Client 역할 credential, Client 서버 설정 `LLM_GATE_HOLD_ID`, protocol identity 검증과 자기 hold PUT/DELETE를 추가했다. 구형 identity 404 + 전환 비활성 + hold ID 미설정일 때만 legacy 호환을 유지한다. admin 키·인증 오류·미지원 protocol 뒤 fallback은 금지한다. Client pause가 있으면 worker는 전환을 시작하지 않으며 배타 예약 중 새 Client hold도 거절한다.
- **M6 로컬 검증:** 전체 백엔드 712 통과·4 건너뜀과 샌드박스 socket bind 오류 3개. 해당 Unix socket 통합 3개는 제한 밖에서 별도 재실행해 전부 통과했다(합산 715 통과). 새 Client↔실제 gate ASGI 시험은 타 hold 보존, 재시작·반복 resume, 응답 유실, 잘못된 역할/키·legacy fallback 제한과 worker admission 차단을 포함한다. 프런트 lint·13개 시험·build 통과. Studio 외부 의존/OS 조건의 skip은 운영 성공으로 간주하지 않는다.
- **문서:** 조율 계약에서 폐기된 origin/UID 강화 요구를 현재 계약과 분리하고, 후보에 이미 구현된 예약/조건부 해제 및 운영 Studio 코드 상태를 정정했다. gate runtime credential 파일은 Client 키 포함 5개이며 적용 절차에 반영했다.
- **남은 M5~M8:** 모델별 context/최종 payload 예산·옵션, 벤치마크 제어 경로 정리, 실제 Linux root runtime와 Studio·gate 계약 연결, 비밀 비노출 설정 확인·상태 이전·복구/롤백 절차, 실제 UI A→B와 B 후속 대화. 전환은 활성화하지 않는다. Wiki 예외 유지, DB 차단/유지보수 VM 시험은 재개하지 않는다.

Client main 배포는 backend와 frontend/dist만 교체하고 Client만 재시작한다. gate_candidate·worker unit·root runtime·Studio 패치는 패키지에 포함되지 않는다. 따라서 Client 배포 확인과 M8 모델 전환 성공을 구분한다.

## M1 운영 읽기 전용 조사

- `/usr/local/bin/ai-model`은 root 소유 `/opt/ai/scripts/ai-model`을 가리킨다. llama unit은 root로 `/opt/ai/scripts/start-llama.sh`를 실행하며 8080 loopback을 사용한다. 설정의 `ACTIVE_MODEL`과 개별 모델 conf를 source하고 `MODEL_PATH`를 `-m`으로 전달한다. 재시작 on-failure, stop timeout 60초다.
- 설치된 모델 설정은 4개. 조사 당시 설정은 `gemma4-e4b-hauhaucs`, health는 `ok`, `/v1/models` ID는 해당 `MODEL_PATH`와 일치했다. 예시 설정은 context 16384, parallel 1, reasoning off다. 모델별 설정을 공통으로 가정하지 않는다.
- Client는 UID 1000 (`yeppyshiba`), Studio와 gate는 `ai-studio`, 각각 NoNewPrivileges 적용. 모두 active. model-worker service/socket은 설치되지 않았다. Wiki 서비스도 active다.
- 당시 SSH 계정은 보호된 Client/Studio env를 읽을 수 없었으며 sudo 비대화형 허용은 Client restart와 Studio deploy 명령뿐이었다. 후속 조사에서 확인한 Client 프로세스와 Wiki 구성은 아래에 기록한다. 공유 잠금/journal 설정은 여전히 미확인이다. 소스 기본값이나 유휴 순간의 연결 부재를 운영 설정의 증거로 사용하지 않는다. 운영 재시작·전환·배포는 하지 않았다.

### M1 추가 조사 — 2026-11-07

- 설치된 네 모델 `gemma4-e4b-hauhaucs`, `gemma4-e4b-heretic-rp`, `qwen35-35b-a3b-uncensored`, `qwen36-35b-a3b-uncensored`의 설정은 모두 현재 `ModelRegistry.model_id`의 literal 구문 및 이름·절대 GGUF 경로 규칙과 일치하고 파일도 비어 있지 않았다. 활성 설정 역시 현재 literal 규칙과 일치했다. 모델별 추론 옵션의 실제 호환성은 별개다.
- `/opt/ai/config`의 관련 디렉터리 및 파일은 root 소유이며 group/other 쓰기가 없고, 모델·활성 설정은 일반 파일과 단일 링크로 확인했다. 이 확인은 ACL/SELinux나 실행 시점의 무결성 검증을 대신하지 않는다.
- 초기 조사에서는 읽을 수 있는 `/opt/ai/scripts`, systemd unit 및 시스템 cron 파일 범위에서 기존 `ai-model`을 호출하는 다른 파일은 발견하지 못했고 root cron spool은 읽을 수 없었다. 이후 사용자 제공 검색 결과와 호출 분석은 아래에 기록한다.
- 이후 사용자가 root 권한으로 cron spool을 포함한 서버 파일을 검색한 결과 벤치마크 두 파일과 `/usr/local/bin/bc250-model-switch`가 호출 후보로 나왔다. bash history와 테스트·문서는 실행 자동화의 증거로 취급하지 않는다. 오류는 컨테이너 overlay의 끊어진 링크에서 발생했으며 그 내부 호출 여부는 확인하지 못했다. 사용자는 이 서버 밖에서 SSH로 두 명령을 호출하는 자동화는 없는 것으로 알고 있다고 확인했다. 이는 사용자 확인 범위이며 외부 전체를 기술적으로 검색했다는 뜻은 아니다.
- 읽기 전용 코드 분석에서 `benchmark.py`는 `ai-model list`의 각 줄을 활성 모델 이름과 정확히 비교하고, `run-comparison.py`는 `switch`의 0이 아닌 모든 종료 코드를 실패로 처리한다. `bc250-model-switch`는 `exec`로 종료 코드를 그대로 전달한다. 읽을 수 있는 주요 로컬 호출 범위에서 이 래퍼의 추가 참조는 발견하지 못했다. 특정 실패 코드 1만 비교하는 의존성은 확인되지 않았지만, 래퍼나 외부 미확인 호출자의 정확한 코드 의존은 배제할 수 없다.
- 운영 `ai-model list`는 한 줄에 이름만 출력한다. 기존 벤치마크 호환을 위해 로컬 후보 CLI도 표시 접두사 없이 출력하도록 수정하고 `tests/test_worker_socket_integration.py`의 소켓 연결 경로에서 확인했다. 해당 파일의 테스트 3개가 통과했다. 이 확인은 운영 명령 교체나 운영 벤치마크 실행이 아니다.
- `run-comparison.py`는 같은 모델일 때 llama 서비스가 꺼져 있으면 직접 `systemctl start`를 수행하고, 전환 subprocess에 360초 timeout을 둔다. timeout이 작업자 측 작업 취소를 뜻하지 않으므로 M5-03/M7에서 벤치마크 실행 경로의 조율과 불명 결과 처리를 확인한다.
- 실행 중 Client 프로세스의 `LLM_MODEL_ID`는 빈 값이었다. 이는 당시 프로세스 환경의 관찰이며 이후 설정이나 다른 호출자의 정책을 보장하지 않는다. 보호된 환경파일의 원문·키 값은 출력하지 않았다.
- 같은 Client 프로세스의 gate 주소는 loopback HTTP 8081이다. Studio와 gate의 프로세스 환경 및 세 서비스의 `/etc/bc250-*.env`는 현재 SSH 계정으로 읽을 수 없었다. 이 계정의 접근 가능 경로 외 설정을 추측하거나 우회하지 않았다.
- 운영 Wiki `/opt/bc250-family-wiki/app/main.py`의 `LLM_URL`은 `127.0.0.1:8080`을 가리키고 `ask` 함수가 사용한다. Wiki는 gate를 우회하는 실제 구성이다. 사용자 결정으로 독립 Wiki API/UI 통합은 후속 작업이며 이번 M1~M8에서 예외로 둔다. 전환 중 Wiki 요청은 중단·실패할 수 있고 gate drain의 보호를 받지 못한다. Wiki 외 LLM/GPU 경로의 조사는 아직 완료되지 않았다.
- 읽을 수 있는 `/opt/bc250-*` 소스에서 추가로 Studio 설정 기본값의 `llm_url`/`upstream_url`과 모델 제어·health 검사 코드의 8080 참조를 발견했다. 이 문자열만으로 현재 Studio의 실제 요청 경로를 판단하지 않는다. 가상환경·의존성·테스트·숨김 경로와 외부 호스트는 조사 범위에서 제외했다.
- 사용자가 비밀값을 가린 `/etc/bc250-*.env` 내용을 제공했다. Studio의 GPU lock 설정은 `/run/bc250-ai-studio/gpu.lock`, LLM과 admission은 각각 loopback 8080과 8081이다. Client는 `LLM_MODEL_ID`가 비어 있고 context 상한은 16384, gate는 upstream 8080이다. 설정에 Studio owned hold·secure precreated lock·switch journal과 gate owned control/state가 보이지 않는다. 키의 실값이나 일치 여부는 제공되지 않았으며 저장하지 않는다.
- 별도 읽기 전용 systemd 조회에서 Studio·gate는 각각 하나의 지정된 환경파일을 사용하고 해당 전환 변수의 inline 설정은 없었다. Studio는 `PrivateMounts=no`, 별도 `RootDirectory`/`RootImage` 없음이다. 조회 당시 호스트에서 `/run/bc250-ai-studio` 디렉터리는 있었지만 그 안의 `gpu.lock`은 없었고 `/run/bc250-gpu`도 없었다. Studio의 일반 잠금은 실제 획득 시 파일을 만들 수 있으므로 부재만으로 이미지 실행 실패로 판정하지 않는다. 잠금 생성 시점·권한·서비스가 보는 inode는 아직 확인하지 않았다.
- 운영 gate 원본은 단일 paused bool이며 owned hold를 제공하지 않는다. 현재 Studio 설정의 잠금 경로는 Client 후보 작업자의 `/run/bc250-gpu/gpu.lock`과 달라 같은 파일 잠금을 공유하지 않는다. 후보 Studio 계약의 journal 및 secure precreated lock 설정도 제공된 env에는 없다. 전환을 활성화하기 전 이 계약을 명시적으로 통일·검증해야 한다. 이 정보는 현재 Studio 설정 변경 지시가 아니다.

### M1-02 운영 계약표 — 2026-11-07

| 항목 | 현재 운영에서 확인한 값 | 후보와의 차이 / 남은 확인 |
| --- | --- | --- |
| Client 계정·권한 | `yeppyshiba:yeppyshiba`, UID 1000, `NoNewPrivileges=yes` | 작업자 socket 허용 UID는 일치. 실제 Linux peer credential 검증은 M6-02 |
| Studio·gate 계정 | 둘 다 `ai-studio:ai-studio`, `NoNewPrivileges=yes` | 후보 preflight는 Studio `ai-studio`, gate `ai-gate`를 요구하므로 현재 gate는 불일치. 배포 계정 설계는 M2-02 |
| Studio GPU 잠금 | env의 `/run/bc250-ai-studio/gpu.lock`; 부모 디렉터리 UID 970/GID 967, mode 0755 | 후보는 `/run/bc250-gpu/gpu.lock` 및 root 소유 secure-precreated 파일을 요구한다. root 조회 시 양쪽 파일 및 후보 디렉터리 부재; 실제 작업 중 생성 파일의 inode·소유권·권한은 M4/M6에서 검증 |
| 전환 journal | 제공된 Studio env에 `STUDIO_SWITCH_JOURNAL_PATH` 없음 | 후보는 `/var/lib/bc250-model-control/switch.json`을 요구한다. 해당 디렉터리와 파일은 조사 시 부재 |
| gate 조율 | 운영 gate는 단일 pause bool. env에 owned control 및 durable state 항목 없음 | 후보는 역할별 credential과 영속 protocol 2 owned hold가 필요하다. legacy bool을 대체물로 사용하지 않음 |
| 현재 키 역할 | Client의 `LLM_GATE_API_KEY`와 Studio의 `STUDIO_ADMISSION_KEY`는 각각 운영 gate 접근에 사용, gate의 `GATE_API_KEY`가 서버 쪽 키 | 사용자가 값 비노출 방식으로 확인: 세 서비스 모두 실행값↔각 환경파일 일치, Client 실행값↔gate 실행값 불일치, Studio 실행값↔gate 실행값 일치. Client 실행 키의 읽기 전용 `/control` 조회는 HTTP 401. 후보의 switch 전용 gate 키와 Studio coordination 키는 제공 env에 없음 |
| namespace·서비스 | 세 서비스 모두 `PrivateTmp=yes`, `PrivateMounts=no`, 별도 `RootDirectory`/`RootImage` 없음, `ProtectSystem=strict`. 사용자 root 조회 시 host·Studio·gate·Client는 서로 다른 mount namespace | 존재하는 `/run/bc250-ai-studio` 디렉터리는 host와 Studio 관점 모두 dev 27, inode 13512. 잠금·journal 파일은 두 관점에서 없어 파일 자체의 inode 비교는 불가. 후보 설치 후 양쪽 파일과 보유 FD의 inode를 M4/M6에서 비교해야 함 |

이는 현재 운영 설정을 기록한 것으로, 잠금 파일 생성·권한 변경이나 Studio/gate 재설정을 지시하지 않는다. 실제 전환은 M2·M4·M6/M7의 계약 및 검증 완료 전 비활성 상태로 유지한다.

운영 gate의 `/v1/models` 프록시는 별도의 gate 제어 인증을 검사하지 않아 Client 키로 HTTP 200이 나와도 키 일치 증거가 아니다. 인증이 필요한 읽기 전용 `GET /control`에서는 Client 실행 키가 401로 거절됐다. 따라서 Client의 gate 제어·상태 기능에 영향이 있으며, 키 불일치를 자동으로 수정하거나 운영 서비스를 재시작하지 않는다.

사용자가 2026-11-07에 확인한 실행 프로세스↔환경파일 비교는 gate·Client·Studio 모두 일치하며, 실행 중인 Client↔gate는 불일치, Studio↔gate는 일치다. 따라서 확인 시점의 Client `/control` 401은 Client가 gate의 제어 키와 다른 값을 보내는 것으로 설명된다. 서비스가 오래된 환경파일을 읽고 있다는 증거는 없다. 이는 Studio의 키 일치 결과이지 이미지 생성 전체 성공 검증은 아니다. 키의 평문이나 해시는 기록하지 않는다.

**남은 작업 구분:**

- 현재 운영 설정: Client의 기존 gate 상태·제어 기능을 사용하려면 Client가 gate에 허용되는 제어 자격 증명을 사용하도록 별도 변경 계획·적용과 인증된 읽기 전용 확인이 필요하다. 정확한 credential 배포 방식·서비스 반영·롤백은 기존 운영 구성과 영향에 맞게 검토한다. 이번 조사에서는 변경·재시작하지 않는다.
- M2-02: legacy 단일 gate 키와 후보의 switch 전용 gate 키, Studio coordination 키 및 Client 운영자 자격의 역할·배포·전환 경로를 정의하고, 운영 gate/Studio 계정 차이와 owned hold를 결정한다. 현재 키 하나를 그대로 모든 역할에 재사용한다고 가정하지 않는다.
- M4-02: 확정된 계정·credential·공유 lock/journal 계약을 preflight 및 설치 템플릿에 반영하고 불일치 시 차단한다. M4-03은 별도 root 전용 복구 진입을 구현한다.
- M6-02/M7: Linux에서 실제 socket 권한, namespace·lock/journal inode, ACL/SELinux 및 credential 전달을 검증하고, 운영 적용·백업·롤백 절차와 키 비노출 인증 확인을 준비한다.

사용자 root 읽기 전용 조회 결과 host `mnt:[4026531832]`, Studio `mnt:[4026532633]`, gate `mnt:[4026532707]`, Client `mnt:[4026532703]`로 각 mount namespace가 달랐다. host와 Studio에서 현재 Studio 런타임 디렉터리는 같은 dev/inode로 보였고, Studio lock·후보 lock 및 journal 경로는 모두 없었다. PID와 namespace 번호·inode는 조사 당시의 값이며 서비스 재시작 후 고정값으로 가정하지 않는다.

M1-02의 **현 운영 계약 조사와 미확인 사유 기록은 완료**다. lock/journal 파일이 없어 해당 파일의 생성 시점·inode·실제 파일 권한을 검증할 수 없다는 한계는 유지한다. 파일을 임의 생성하거나 GPU 작업을 시작하지 않았고, 운영 준비 완료를 뜻하지 않는다. 후보 설치 후 M4/M6에서 실제 공유 파일과 Studio가 보는 inode 및 권한을 확인해야 한다.

### M1-03 호출 경로 조사 — 2026-11-07

| 호출자 / 경로 | 현재 확인한 구성 | 전환 시 처리 및 남은 점 |
| --- | --- | --- |
| Client | `LLM_MODEL_ID` 빈 값. loopback 8081 gate 사용, 서비스는 8010 loopback. 활성 Caddy 설정에 `/llm`→8010 경로 존재 | M5-01/02에서 실제 로드 모델 추종·요청 진입 경합과 context를 처리. gate 제어 키 불일치는 M1-02 참고 |
| Studio 이미지 | `sdcpp` 이미지 엔진이 `sd-cli` subprocess 실행. GPUManager는 설정된 `/run/bc250-ai-studio/gpu.lock`과 현재 gate pause를 이용 | 후보 worker의 잠금 경로 및 owned hold/journal과 다름. M2-02/M4/M5-03에서 공용 계약을 적용하고 M6에서 검증 |
| llama-server·gate | llama는 8080 loopback, gate는 8081 loopback에서 실행 | gate는 유일한 범위 내 추론 admission 지점이어야 한다. 운영 gate의 bool pause는 owned hold가 아님 |
| Wiki | `ask`가 8080 직접 호출 | 사용자가 독립 Wiki를 이번 범위의 명시적 예외로 지정. 전환 시 요청 실패·중단 가능; API/UI 통합은 후속 계획 |
| `/opt/ai-benchmark/benchmark.py` | `/v1` 관련 요청을 8080에 직접 전송하고 `ai-model list` 사용 | 전환 중 gate를 우회한다. M5-03/M7에서 gate 경유 및 실행 제한·검증 전에는 전환과 동시 사용을 허용하는 구성으로 취급하지 않는다 |
| `/opt/ai-benchmark/run-comparison.py` | 전환은 `ai-model switch`; 직접 8080 health 및 subprocess timeout 360초, 경우에 따라 `systemctl start` | M5-03/M7에서 systemd 직접 제어와 timeout 뒤 지속되는 작업의 상태 확인을 조율. 작업자 완료를 추정하거나 중복 restart하지 않음 |
| `benchmark-memory.py`, `monitor-memory.sh` | `systemctl`을 통한 서비스 상태·메모리 관찰 참조 | 확인된 코드는 관찰 목적. 자동 호출 여부와 변경성은 운영 실행 경로 기준으로 재검토 |
| Studio `scripts/probe_server.py`, `bc250-ttm.service` | probe는 선택 시 `sd-cli --help` 등 정보를 조회. TTM은 memory limit 설정용 oneshot이며 조사 시 active/exited | 이 확인을 GPU 추론 자동 실행 증거로 간주하지 않음 |

사용자가 root로 `/etc/crontab`, cron spool·시스템 cron 디렉터리와 시스템 unit 파일을 검색한 결과 관련 문자열 일치 항목이 없고 읽기 오류는 0건이었다. 해당 시점 `systemctl list-timers --all`에서 관련 이름의 timer도 없었다. 이는 검색한 문자열·위치에 한정되며 동적 생성 명령, 외부 호출이나 다른 scheduler가 없다는 증명은 아니다. 조사 시 `yeppyshiba`의 rootless Podman 실행 컨테이너는 없고 Docker 명령은 없었다. 사용자가 root `podman ps` 결과도 실행 중 컨테이너가 없음을 확인했고, 서버에서 Docker/Podman은 테스트 외에 사용하지 않는다고 밝혔다. 유휴 시점의 `ps`에서는 `llama-server`만 해당 이름으로 보였으며, 이를 다른 호출자가 영구적으로 없다는 증거로 사용하지 않는다. 어떠한 서비스 재시작·모델 전환·GPU 작업도 실행하지 않았다.

M1-03은 **현재 알려진 호출 경로와 후속 조율·제한 방안의 조사 완료**다. M5-03의 벤치마크 gate 경유 또는 명시적 실행 제한, 직접 systemd 제어 정리와 Studio 공용 계약은 아직 구현·검증되지 않았다. 알려지지 않은 외부·수동 호출의 부재까지 증명한 것은 아니며 운영 전환 준비 완료를 뜻하지 않는다.
- 모델 설정의 context/추론 옵션은 다음과 같다. 이는 파일 설정만의 조사이며 JSON schema·실제 요청 호환성 시험은 아니다.

  | 관리 이름 | context | parallel | reasoning mode / budget |
  | --- | ---: | ---: | --- |
  | `gemma4-e4b-hauhaucs` | 16384 | 1 | off / 0 |
  | `gemma4-e4b-heretic-rp` | 16384 | 1 | off / 0 |
  | `qwen35-35b-a3b-uncensored` | 4096 | 1 | 설정 없음 |
  | `qwen36-35b-a3b-uncensored` | 16384 | 1 | auto / -1 |

Client 기본 `llm_context_tokens`는 16384이며 여러 추론 경로가 이를 공통 사용한다. 따라서 Qwen 3.5를 선택했을 때 설정상 4096 한도를 초과하는 요청을 허용할 수 있다. M2-03/M5-01에서 실제 모델에 따른 한도를 적용하고 각 요청 경로를 검증하기 전 기능 완료로 보지 않는다.

M2-03의 구현 방향: 신뢰된 모델 설정에서 관리 이름·GGUF ID·per-sequence context를 읽고 실제 로드 ID 및 준비 상태와 일치시킨다. 요청 유효 context는 Client 상한과 검증된 모델 한도 중 작은 값이다. 알 수 없는 모델에 16384를 적용하지 않는다. 채팅·제목·요약·Wiki 메모리·이미지 의도/프롬프트가 같은 요청별 모델 프로필을 사용하고, 최종 payload의 전체 메시지와 출력 예산을 함께 검사한다. 요약은 기존 요약 및 지시문도 예산에 포함하고 오래된 대기 작업의 메시지 선택·cursor를 보존해야 한다. 모델별 reasoning/JSON schema 허용 여부는 실제 모델별 시험 근거가 필요하다. 전환 경합의 마지막 방어는 M5-02 gate admission으로 처리하며 이 설계만으로 운영 준비 완료가 아니다.

M2-03의 [모델별 추론 정책](llm-model-switch-model-policy.md)은 모델별 설정, 실제 로드 확인 범위, 신뢰된 프로필·epoch·context 및 미검증 기능 처리와 M5/M6 수용 기준을 확정했다. 추가 읽기 전용 조회 당시 현재 `gemma4-e4b-hauhaucs`는 `/props` model path와 `/v1/models` ID가 일치하고 `/props`와 유일 슬롯의 `n_ctx`가 16384였다. 세 모델의 실제 로드 상태 및 네 모델 모두의 reasoning/strict schema 동작은 아직 검증하지 않았다. 이 단계에서 모델 전환이나 추론 요청은 실행하지 않았다.

## M2 결정과 프로토콜

M2-02의 운영 gate·Studio·credential·잠금·journal 및 요청 epoch 설계는 [공용 조율 계약](llm-model-switch-coordination.md)에 확정했다. 이 문서의 나머지는 기존 구현과 조사 근거이며 실제 변경·Linux/운영 검증을 대신하지 않는다.

CLI `ai-model list/status/switch`와 Client는 동일한 root 소유 별도 설치본의 작업자를 호출한다. Client UID 1000과 root peer만 허용하고 웹 서비스에 sudo를 주지 않는다. CLI 종료·HTTP 연결 유실은 작업을 취소하지 않는다. Unix socket은 줄 단위 JSON, 요청 최대 4096바이트, 응답 최대 65536바이트다. 작업자는 `models`, `current`, `get`, `submit`을 제공하고 `submit`의 중복 키와 활성 작업 충돌을 원자적으로 처리한다. models는 `models[{name,current}]`, `configured_model`, `loaded_model`, `ready`, `switch_enabled`, `active_operation_id`를 반환한다. current는 `{operation: 작업 또는 null}`이다. 후보 bridge의 관리자 전용 `pending`을 그대로 사용하지 않는다. `get`/`submit`은 작업 객체를 반환한다. 오류는 `error` 코드로 반환한다. 이 계약은 로컬 작업자에 구현됐으며 실제 운영 작업자 설치·통합은 검증되지 않았다.

### M2-01 로컬 계약 확인 — 2026-11-07

| 경계 | 확인된 계약 |
| --- | --- |
| Client 인증 | 기존 앱 인증과 별도 운영자 키가 모두 필요하다. `local` 모드나 socket 접근 가능 여부만으로 브라우저 운영자 권한을 주지 않는다. 기본 설정은 비활성이다. |
| 모델·작업 API | 모델 목록, 진행 중 작업, ID별 작업 조회와 switch 접수를 제공한다. 작업 ID는 32자리 소문자 hex이며 상태와 검증된 이전·현재 모델을 구분한다. |
| 오류 | 설치되지 않은 모델은 `invalid_model`/422, 동일 키의 다른 모델은 `idempotency_conflict`/409, 다른 미완료 작업은 409, 없는 작업은 404, 작업자 비활성·연결 실패는 503이다. 잘못된 모델은 접수하지 않는다. |
| 중복 방지 키 | CLI·API·socket 모두 ASCII 영문·숫자와 `_ . : -`로 구성된 1~128자를 허용한다. 같은 키와 모델의 반복 접수는 기존 작업을 반환한다. |
| CLI | `list`는 모델 이름을 한 줄씩 출력한다. `switch`는 키·작업 ID를 출력하고 완료를 기다린다. 검증 성공 0, 요청 또는 전환 실패 1, 복구 필요 또는 통신 결과 불명 2다. `status --operation`으로 다시 조회한다. |
| 권한·수명 | API와 CLI는 공용 Unix socket 작업자에 연결되며 peer UID를 검사한다. 접수 후 HTTP/CLI 연결 종료는 작업을 취소하지 않는다. 작업 DB가 중복 키와 단일 미완료 작업을 원자적으로 관리한다. |

계약 대조 중 API만 `.`·`:`가 든 중복 방지 키를 거절하던 문제와 미설치 모델이 503으로 나타나던 문제를 수정했다. 관련 API·CLI·작업 저장소·작업자 및 실제 로컬 Unix socket 시험 38개가 통과했다. socket 시험의 peer UID와 GPU/gate/systemd는 대역이며 Linux `SO_PEERCRED`, 실제 gate/Studio 및 GPU 검증은 M6에서 수행한다. 이전에 발견한 운영 Client↔gate 키 불일치와 후보 작업자의 role별 credential 구성은 별개의 M2-02/M4 작업이다.

관리 API는 기존 앱 인증에 더해 별도 `OPERATOR_KEY`를 요구한다. local 모드는 관리자가 아니다. 브라우저 자격은 메모리에만 두고 HTTPS 또는 SSH 터널에서 사용한다. 기존 gate-control에도 같은 권한을 요구한다. `MODEL_CONTROL_ENABLED` 기본값은 false이며 테스트 fake는 의존성 오버라이드로만 주입한다. 실제 배포에서 fake를 고를 수 없다.

운영 gate의 단일 paused bool은 소유자 hold가 아니다. **영속 owned hold와 기존 관리자 pause 보존을 gate에 추가하는 것이 필수**다. 대기하던 오래된 모델 ID의 재전송을 방지해야 한다. Studio `switch_barrier`의 8192바이트 제한 및 journal stage `pausing`, `draining`, `switching`, `verifying`만 허용하는 계약을 준수한다. DB의 `rolling_back` 상태를 그대로 journal stage로 기록하지 않는다. imagegen은 이번 변경에서 수정하지 않는다. 복구 불명 시 hold와 journal을 유지하고 새 작업을 차단한다.

## 후속 작업 개요

세부 상태·선행관계·완료 조건은 [통합 계획 §7](llm-model-switch-plan.md#7-실행-체크리스트와-완료-조건)의 M번호 표를 따른다.

- M3: API/UI는 기본 비활성으로 추가했다. fake 작업자 API 흐름은 실제 전환 증거가 아니다. 브라우저 상호작용 자동 검증, Unix 소켓 오류 및 보안 경계 검증을 추가하고 실제 작업자 계약과 일치시킨다.
- M4: 상태 저장·조정·잠금·systemd 제어 및 CLI/socket을 선별 이관했다. root 전용 복구 진입과 실제 환경 권한·조율 검증을 완료한다. gate owned hold와 Studio journal/공유 잠금이 실제 설치와 맞기 전 활성화 금지. 기존 코드의 `studio_files`, `switch_barrier`, `llm`, `model_control`, `systemd_model`, bridge 및 SQLite 의존을 분리한다.
- M5: 고정 `LLM_MODEL_ID`, 모델별 context/옵션, 채팅·요약·제목·Client 내부 Wiki 메모리·이미지 프롬프트 진입 및 전환 경쟁을 해결한다. 독립 Wiki 외 직접 호출은 gate로 조율하거나 명확히 차단한다. 독립 Wiki API와 UI 통합은 후속 계획이다.
- M6: 동일 최종 후보로 CLI/API 동시성, idempotency, drain, 이미지 경쟁, restart/health 실패, 롤백, 작업자 종료와 불명 상태 복구를 검증한다. 가짜 시험과 실제 GPU 시험을 구별한다.
- M7: root 설치본·gate/Studio 최소 변경의 배포, 백업, SELinux/권한, 복구와 롤백 절차를 작성한다. 환경 미확인 항목을 확인하기 전 운영 활성화하지 않는다.
- M8: 별도 운영 절차에서 실제 UI A→B 전환 및 B 후속 대화, CLI/API 일치, 복구와 최종 정리를 확인한다. 이번 턴에서는 운영 적용하지 않는다.

### M3-01 API 재검증 — 2026-11-07

운영자 키가 있어도 기존 local 요청의 host·origin·클라이언트 헤더 검사를 우회할 수 없고, 운영자 키 미설정일 때 관리 API가 거절되는지 검증했다. 작업자의 응답에서 모델 이름·작업 ID와 허용된 오류 코드만 통과시키고 유효하지 않은 응답의 내부 상세는 503에 노출하지 않는다. 기본 비활성, idempotency·요청 오류·Unix socket 연결 유실과 권한 검사는 기존 관련 시험과 함께 확인했다.

CI가 실행하는 기본 `pytest -q`에서도 비동기 테스트가 실행되도록 `pytest.ini`를 추가했다. 전체 백엔드 회귀는 226 통과·1 건너뜀이며, 이후 응답 필드 검증을 보강한 뒤 관련 API·CLI·socket 13개를 다시 통과했다. 프런트 lint/build도 통과했다. 건너뛴 시험은 외부 Studio 소스와 Python 환경이 필요한 통합 시험이다. 이 결과는 macOS 로컬에서 수행되었고, socket peer UID·운영 GPU/gate/systemd는 대역을 사용했다.

M3-01은 로컬 인증 API·transport 계약 검증으로 완료 가능하며 별도 운영 배포를 요구하지 않는다. 현재 `main` push는 Client 전체를 배포·재시작하고 기존 gate-control에 운영자 인증을 요구하는 동작을 적용한다. 운영에는 `OPERATOR_KEY` 설정과 gate 키 불일치 등의 준비가 필요하고 protocol 3·실제 worker 연결은 미완성이므로 이 단계에서는 푸시나 배포를 수행하지 않았다. 실제 활성화·운영 검증은 M4~M8을 따른다.

### M3-02 추가 진행 — 2026-11-07

접수 결과 불명 시 요청 키와 선택한 모델을 세션에 보관해 관리자 확인 정보로 표시하고, 알려진 작업 ID를 보관해 새로고침 후에도 결과를 조회했다. 조회가 실패한 동안 새 전환을 차단하고, 이전 조회 응답이 새 작업을 덮어쓰지 않도록 요청 세대를 검사했다. 인증 정보는 세션에 저장하지 않는다. 당시 프런트 lint/build와 관련 API·Unix socket 시험 6개는 통과했으며 브라우저 자동화 호스트 연결 실패로 UI 상호작용은 미검증이었다. 이후 브라우저와 추가 검증 결과는 아래를 따른다. root 전용 복구는 M4-03에 남아 있다.

### M3-02 UI 검증 — 2026-11-08

느린 조회가 서로를 무효화하지 않도록 한 번에 하나만 실행하고, timeout과 완료 후 모델 재조회를 추가했다. 확정적인 인증·입력·충돌 거절과 응답 유실을 구별한다. 결과 불명 시 저장된 같은 모델·같은 중복 방지 키로만 사용자가 접수를 확인할 수 있으며, 원 요청이 접수되지 않았다면 전환이 시작될 수 있음을 화면에 알린다. 화면 종료 시 요청을 중단하되 결과 불명 기록은 유지하고, 이전 화면의 늦은 응답이 새 요청의 기록을 지우지 않도록 저장된 요청 키와 모델을 확인한다.

로컬 Edge에서 운영 서버와 분리된 가짜 HTTP 작업자로 인증 실패, 선택·접수, 새로고침 후 결과 복원, 확정 거절, 응답 유실과 같은 키 재접수, 지연된 조회 및 완료 모델 갱신을 확인했다. 프런트 자동 시험 `npm test` 10개와 lint/build가 통과했다. 자동 시험은 재접수·timeout·recovery_required 차단·느린 polling·이중 클릭 및 언마운트된 요청의 늦은 응답 경합을 포함한다. `npm test`는 배포 workflow의 검증 단계에도 연결했다.

M3-02는 가짜 작업자 기준의 **로컬 UI 흐름 검증 완료**다. 실제 작업자·gate·GPU 및 Linux 권한을 연결한 최종 후보 회귀와 root 전용 불명 상태 복구는 M4-03/M6에서 별도로 검증한다. 브라우저 시험은 실제 모델 전환이나 운영 서비스 변경이 아니다.

## M4 로컬 구현 기록 — 2026-11-07

Client의 `backend/app/model_control/`에 imagegen 후보의 작업 DB, coordinator, journal, systemd/registry, GPU 잠금, gate adapter를 선별해 분리했다. API와 `ai-model list/status/switch` CLI는 같은 Unix socket worker를 사용한다. 접수된 작업은 연결 종료와 독립적으로 실행된다. CLI는 접수 키와 작업 ID를 출력하며 `status --operation ID`로 결과를 다시 확인한다. CLI `switch` 종료 코드는 성공 0, 실패 1, 결과 불명/복구 필요 2다. 운영 원본 명령은 변경하지 않았다.

작업 DB는 단일 미완료 작업과 idempotency를 원자적으로 보장하고, 재시작 시 journal/DB 불일치를 복구 필요로 보수적으로 처리한다. 검증된 이전·현재 모델만 기록하며 전환 중 불명인 현재 모델은 비운다. systemd restart 결과 불명이면 겹치는 restart를 하지 않는다. 복구 성공은 이전 모델의 health와 ID 검증 후에만 기록한다. journal stage는 Studio가 읽는 네 가지로 제한한다.

운영 adapter는 공유 GPU 잠금을 획득한 후 Studio protocol 1 및 gate switch-role protocol 2를 인증해 확인하고, Studio/gate의 안정된 프로세스 식별자, Studio가 보는 lock·journal inode, 보유한 lock FD의 inode를 확인한다. gate는 durable owned hold를 요구하며 기존 관리자 hold는 유지한다. 예시 systemd service/socket과 root 설치본 부트스트랩은 `deploy/model-control/`에 있다. worker는 기본적으로 전환 비활성이며 별도 활성화 시에도 이 모든 검사를 통과해야 한다. 부트스트랩은 격리된 시스템 Python으로 root 소유이며 일반 사용자가 쓸 수 없는 설치 트리만 import하고 링크와 `.pth`/`.pyc`를 거부한다. 배포 시 이 부트스트랩 및 시스템 Python 자체가 실행 전에 신뢰 가능한 root 소유 경로에 설치돼야 한다.

### 수용 근거와 한계

- `tests/test_worker_switch_operations.py`, `test_worker_switch_worker.py`: 중복 키, 동시 작업, lifetime 잠금, 재시작 불명확 상태와 복구 차단.
- `tests/test_worker_model_switch.py`, `test_worker_systemd_model.py`: restart/health 실패 시 복구, 불명 systemd 작업·journal 유지, 같은 모델 health, 실제 model ID 판정. macOS에서는 Linux 파일 xattr 검사를 테스트 대역으로 치환했다.
- `tests/test_worker_gate.py`, `test_worker_preflight.py`, `test_worker_bootstrap.py`: owned hold/관리자 hold, 인증 endpoint, 프로세스 변경 및 lock inode 불일치, 설치 트리 검증. 프로세스·endpoint는 테스트 대역이다.
- `tests/test_worker_socket_integration.py`: 로컬 Unix socket에서 API·CLI와 동일 작업자 연결, 중복·충돌, CLI 연결 종료 후 작업 지속, 허용되지 않은 peer와 잘못된 프레임 거절. macOS는 Linux `SO_PEERCRED`를 제공하지 않아 peer UID 추출을 테스트 대역으로 주입했다. Linux에서 실제 peer credential, systemd socket activation 및 root/SELinux 환경 시험은 남았다. 템플릿의 CAP_SYS_PTRACE는 다른 UID인 Studio의 /proc/PID/root inode 확인에 필요하며 실제 커널·SELinux에서 접근 가능성과 권한 범위를 검증해야 한다.
- 전체 로컬 회귀: `PYTHONPATH=. .venv/bin/pytest -o asyncio_mode=auto -q` 219 통과, 1 건너뜀. 프런트 `npm run lint && npm run build` 통과. 실제 gate/Studio, GPU 전환 및 브라우저 상호작용 시험은 수행하지 않았다.

M4 핵심은 **로컬 구현 및 대역 검증 완료**이나 단계 전체는 부분 완료다. root 전용 복구 진입과 runtime 검증 연결, 운영 통합은 미완료다. 운영 gate는 현재 owned hold를 지원하지 않으며 gate 계정도 이 후보의 검증 계정과 다르다. Studio lock 경로는 후보와 다르고, 제공된 env에 공유 journal 설정은 없다. Wiki는 8080을 직접 호출하며 Client의 현재 고정 모델 ID는 비어 있다. Linux의 권한/SELinux 및 재부팅·실제 GPU 시험, 구체적인 안전 설치/백업/복구 절차가 끝나기 전 운영 활성화하지 않는다. M5의 전환 중 요청·고정 모델 처리도 구현되지 않았다.

### M4-01 핵심 재검증 — 2026-11-08

공용 worker·영속 DB·CLI/API socket의 단일 작업 및 중복 방지 경로를 다시 검토했다. 신규 전환을 비활성화한 상태에서도 이미 접수된 같은 키·모델의 기록은 조회할 수 있도록 했으며, 새로운 접수는 계속 거절한다. 이는 작업을 새로 실행하지 않는 조회다.

성공한 모델 검증 후 journal을 원자 교체해 비우고 디렉터리 fsync가 실패하면 파일이 이미 빈 상태일 수 있다. 이때 원래 설정으로 재시작하면 변경 근거가 없는 상태에서 또 다른 변경을 하게 되므로, 성공과 롤백 모두 GPU 잠금과 자기 gate hold를 유지한 채 journal을 먼저 정리한다. journal 정리가 실패하거나 이후 hold 해제 결과가 불명이면 추가 재시작 없이 `recovery_required`로 남긴다. 해제 응답이 불명일 때 자기 hold를 다시 얻으려 시도하지만, gate 자체가 실패하면 재획득까지 보장할 수 없으므로 이를 성공으로 판단하지 않는다.

실제 파일 교체 뒤 디렉터리 fsync 실패, 롤백 정리 실패, 비활성 상태의 기존 키 조회 및 신규 요청 차단을 관련 시험에 추가했다. 관련 시험 62개가 통과했고 이어서 전체 백엔드 회귀 229개 통과·1개 건너뜀이었다. 가짜 gate/systemd와 macOS peer UID 대역을 사용했다. 빈 journal과 남은 hold를 작업과 연결할 영속 근거·root 복구 진입은 M4-03, 실제 Studio/gate protocol 3·공유 잠금 및 Linux 권한 검증은 M4-02/M6의 차단 조건이다.

운영 Client는 SSH 계정 `yeppyshiba`와 UID 1000을 공유한다. 현재 socket의 peer UID 허용만으로 이 계정의 직접 호출을 Client 프로세스의 호출과 구분할 수 없으므로 M4-02에서 비대화형 전용 서비스 UID와 socket·DB 권한 이관을 준비한다. 이번 검증은 운영 서비스나 계정을 변경하지 않았다.

### M4-02 사전 검사·설치 경계 진행 — 2026-11-08

- worker 후보가 gate identity의 protocol 3과 durable 상태·32자리 hex epoch를 요구하도록 변경했다. protocol 2와 epoch 누락·오류, pause/hold 불일치, startup 또는 다른 GPU 작업의 미해결 hold는 거절한다. 운영 gate 및 보존된 protocol 2 후보는 아직 통과할 수 없다.
- socket 허용 계정과 그룹을 `yeppyshiba`가 아닌 비대화형 전용 `ai-client`로 정했다. root·대화형 shell·다른 계정과 중복 UID·그룹 불일치와 socket 소유자·그룹·mode 불일치를 거절한다. Client·Studio·gate는 systemd 설정값뿐 아니라 실제 프로세스의 UID/GID 네 값, `NoNewPrivs`, PID·시작 시각도 확인한다.
- Client의 `/usr/bin/python3 -I -S`용 root 관리 실행 트리와 서비스 drop-in 후보를 추가했다. preflight는 Client 실행 unit·인자·interpreter·환경파일·코드/의존성의 소유·쓰기 권한과 Client mount namespace에서의 파일 일치를 확인한다. 실제 Linux `/proc`·systemd·SELinux 환경에서의 검증은 M6-02에 남는다.
- 2026-11-08 읽기 전용 조회에서 Caddy의 실제 적용 구성은 `/opt/bc250-llm-client/frontend/dist`를 `/llm` 자산으로 제공했고, 이 경로와 `index.html`은 `yeppyshiba`가 쓸 수 있었다. root backend/전용 UID만으로 운영자 UI에 입력한 키가 보호되는 것은 아니다. Caddy admin 구성의 GET은 localhost에서 응답했지만 변경 권한은 시험하지 않았다.
- 사용자가 기존 `/llm` 주소·origin을 유지하고 같은 origin의 관련 자산·배포 권한을 함께 보호하는 방식을 선택했다. 실제 Caddy HTTP 서버에는 Client·Studio 정적 root 및 Client 8010·Studio 8090·Wiki 8020 loopback upstream 경로가 있다. Studio 정적 root의 조사 당시 소유 UID는 1001이었고 SSH 계정으로 쓸 수 없었다. Wiki `main.py`는 root 소유였지만 상위 `/opt/bc250-family-wiki/app`은 `yeppyshiba`가 쓸 수 있었다. 파일 하나의 소유권만으로 전체 경로 보호를 판정하지 않는다. Wiki 기능·모델 조율은 범위 예외이나 동일 origin의 UI 보안 영향은 따로 검토해야 한다.
- root 관리 정적 자산의 모든 예상 파일·해시·소유권·권한과 예기치 않은 파일/디렉터리를 대조하는 내부 부품과 음성 시험을 추가했다. 이는 아직 신뢰된 manifest 발급, 실제 Caddy·same-origin 동적 응답 및 자산의 namespace 검사를 포함하는 완성된 검증기가 아니므로 기존 전환 차단을 해제하지 않는다. root 검토 또는 별도 서명 릴리스만을 승격하는 배포 계약은 M7에서 준비한다.
- Caddy의 실제 설정을 제한된 Unix socket에서 읽는 부품을 추가했다. socket 경로·소유권·권한과 전용 그룹, 연결 상대의 PID/UID/GID, 응답 크기·형식 및 중복 JSON 키를 검사하며 일반 TCP admin으로 대체하지 않는다. 이는 전송 계층 검사일 뿐이며 아직 preflight에 연결된 전체 origin 검증 결과가 아니다. 이 부품과 기존 Client 신뢰·preflight 관련 대역 시험은 51개 통과했다.
- 별도로 검토된 Caddy 설정 digest와 검사 전후 프로세스·설정 일치 및 제한된 다운로드 응답 헤더를 확인하는 정책 부품을 추가했다. digest의 신뢰된 발급과 전체 경로·동적 응답 검증이 없으므로 이것도 origin 안전 판정이나 차단 해제 근거가 아니다.
- 독립 digest로 고정한 root 관리 manifest를 읽고 제한된 형태의 route 목록을 대조하는 부품을 추가했다. 허용하지 않은 matcher·handler·upstream은 거절하지만 route 목록의 일치만으로 backend 코드·동적 응답·자산 namespace의 안전성을 증명하지는 못한다. 이 부품을 포함한 관련 대역 시험은 75개 통과했다. manifest의 독립된 신뢰 근거와 모든 응답 및 실행 프로세스의 provenance 검증을 구현해 preflight에 연결해야 한다.
- Caddy unit과 실행 프로세스·바이너리 및 Caddy namespace의 정적 자산을 확인하고, 검토 manifest·인증된 admin 설정·route 목록과 검사 전후 신원을 대조하는 제한된 조사 함수를 추가했다. 동적 route는 미검증으로 반환한다. 조회 전체 시간 제한과 경로 치환·escape 거절도 추가했다. manifest digest의 독립적인 신뢰 공급, 동적 backend·응답과 OS runtime 보호 및 전체 preflight 연결은 미완성이다. 전체 회귀는 Unix socket 생성이 가능한 환경에서 318개 통과·1개 건너뜀이며 실제 Linux 경계의 증거는 아니다.
- 추가로 동적 route를 manifest v3의 backend·upstream에 연결하고 unit·프로세스·코드 트리와 namespace를 대조하는 조사 부품, 검토된 OS runtime 트리 비교 부품, root 관리 고정 digest와 공용 잠금 부품을 추가했다. 잠금 아래 조사하는 진단 래퍼는 미검증 route를 거절한다. Linux listener 소유자를 조사하는 부품과 마지막 backend 신원 재확인도 추가했으나, PID namespace·hidepid·별도 task FD 및 FD 전달 경쟁은 확인하지 못하므로 단독 소유의 증거가 아니다. 로더·공유 라이브러리·모든 응답의 정책 강제, 실제 Caddy 중첩 route 처리, 모든 배포자의 잠금 참여도 미완성이다. 다운로드 헤더의 현재 허용 범위는 정상 HTTP 헤더 일부도 거절하므로 운영용 응답 정책 완료가 아니다. preflight에는 연결하지 않았고 기존 전환 차단을 유지한다. Unix socket 통합 시험을 포함한 전체 로컬 회귀는 341개 통과·1개 건너뜀이며 실제 Linux 또는 운영 검증은 아니다.
- 실제 적용된 Caddy route와 UI 자산 및 같은 origin의 다른 자산까지 신뢰 경계를 검증하는 완성된 구현은 아직 없다. 따라서 후보의 `verify_operator_frontend()`는 **무조건 실패**하며, 설정 플래그로 우회할 수 없다. 이관된 M4-04에서 실제 유효 Caddy 라우팅·admin 접근 권한·namespace와 root 관리 자산의 검증기 및 로컬 대역 시험을 구현하고, M7에서 그 계약을 충족하는 배포 구성·자산을 준비해야 한다. OS 표준 라이브러리와 동적 로더가 Client namespace에서 대체되지 않는지도 검증해야 한다. 검증기와 배포 준비 전 전환을 활성화하지 않는다.
- 기존 Client 배포는 `yeppyshiba`가 쓰는 checkout과 venv를 갱신하므로 새 root 관리 실행 트리를 갱신하지 못한다. M7에서 Studio 독립 배포를 보존하면서 Client 코드·정적 자산·의존성의 root 관리 배포, DB/WAL 권한 이관, credential, 백업과 롤백을 준비해야 한다. 현재 운영에 파일을 설치하거나 권한을 바꾸지 않았다.

정적 자산 검증 부품 추가 후 전체 백엔드 회귀는 263 통과·1 건너뜀이었다. protocol·계정·파일 대역 시험의 일부는 미구현 프런트 검사만 대역으로 대체했으며, 실제 프런트 검사의 거절도 별도로 시험했다. 이 결과는 production preflight가 성공한다는 증거가 아니다. 프로세스·namespace·socket 검사는 macOS에서 주입한 대역이므로 Linux 실제 경계 검증이 아니다. 이후 범위 조정으로 M4-02의 미완성 프런트 provenance 검증기는 M4-04에 이관했다. 설치 후보와 남은 보안 경계는 [템플릿 안내](../../deploy/model-control/README.md)에 정리했다.

### M4-03 root 복구·조사 진행

| 세부 작업 | 완료 조건 | 상태 |
| --- | --- | --- |
| 복구 전용 Studio 계약 | GPU 잠금·journal·서비스 신원과 인증을 유지하며 정상 미해결 journal을 구분하고 미지원 응답은 차단 | worker 소비자·로컬 대역 완료; 실제 Studio endpoint는 M5-03 |
| orphan·불일치 처리 | 증거가 부족한 기록과 모든 hold를 보존하고 root 조사 후 안전한 종료 조건과 처리 경로를 명시·검증 | 조사 분류 구현; 안전한 후속 처리·종결 미완료 |
| 빈 journal 조건부 복구 | 상관관계·독립 hold·epoch를 확인하고 원자 해제 미지원이나 불명 결과를 미해결로 유지 | 예약 유무 양쪽 worker 후보·로컬 대역 구현; 실제 gate 연산은 M5-02 |
| 권한·중단 회귀 | Client UID 거부, 연결 종료·재시작·동시 복구 및 hold 보존을 로컬 대역으로 검증 | 로컬 대역 완료; 실제 Linux 및 최종 후보는 M6 |
| 설치·운영 복구 | 독립 hold 보존, 기록 백업과 불명 상태 대응을 포함한 검토용 절차 | M7-01 |

- 전환 hold ID를 journal 기록과 gate hold 취득 전에 SQLite에 영속 기록하고, 재시작 시 DB·journal·hold ID가 맞지 않으면 신규 전환을 차단한다. 기존 DB의 hold ID 없는 작업을 임의로 연결하지 않는다.
- root peer UID만 사용할 수 있는 조사·복구 CLI/bridge와 직렬화를 추가했다. 소켓 연결이 끊겨도 접수된 복구 작업이 계속 실행되고 worker 종료 시 완료를 기다린다. 손상·누락된 journal은 내용을 노출하지 않는 조사 모드로 기동하며 원본 보존·전환 차단을 유지한다.
- 빈 journal에 남은 상관 switch hold 복구 후보는 검증 후 gate의 원자 조건부 해제를 요구한다. 미지원·불명 응답은 미해결로 유지하고 자기 hold 재획득을 시도한다. gate 구현과 조건부 해제의 실제 원자성은 M5-02, 최종 후보 통합·Linux 경계는 M6, 운영 복구 절차는 M7에 남는다.
- root 조사 응답에 orphan·불일치 분류를 추가했다. 이는 gate 상태를 포함한 원자 스냅샷이 아니며 복구 승인에 쓸 수 없다. 증거가 불충분한 orphan·불일치의 자동 처리는 금지하고 기록과 hold를 보존한다. 조사 이후의 안전한 처리·종결 구현은 M4-04로 이관했다.
- 영속 조사·종결 설계 조건: root peer 인증과 단일 worker 및 복구 직렬화 아래 조사 시작·증거 관찰·검토·각 조치의 의도와 결과를 순서 있는 이벤트로 내구성 있게 기록한다. 이벤트는 작업·journal 식별자와 digest, 관찰 시각·서비스 세대·gate epoch/hold, 증거 출처 및 검토 결과를 연결하되 비밀값과 손상된 journal 원문을 넣지 않는다. root 소유 보호 경로와 DB/WAL 파일의 신뢰·권한을 검증하고 일관된 백업 및 이벤트 누락·손상·쓰기/fsync 실패를 감지해야 한다. 기록 실패, 불명 응답, 재시작 중 미완료 조치 또는 증거 변경 시 모든 원본과 hold를 보존하고 새 전환과 종결을 차단한다. 실제 이벤트 저장은 아직 구현되지 않았다.
- 종결은 기록의 출처와 특정 작업·hold 소유권을 독립 증거로 입증하고, Studio/gate 신원·공유 잠금과 journal, systemd 작업 종료 및 현재 모델 health/ID를 확인하며 독립 hold를 유지해야 한다. GPU 잠금만으로 외부 DB·gate 변경을 배제한다고 가정하지 않는다. 관찰한 DB 행·journal 세대와 gate epoch/hold 집합을 조치 직전에 재검증하고, 각 외부 변경은 해당 시스템이 원자적 조건부 연산으로 보장해야 한다. DB·journal·gate의 단일 트랜잭션은 없으므로 각 단계의 재시작·응답 유실을 내구성 있는 상태 기계로 처리해야 하며 어떤 불확실성도 성공으로 간주하지 않는다. 현재는 이 상관 증거, 조건부 정리 및 감사 상태 기계가 없어 불일치 상태를 종결하지 못한다. 실제 gate/Studio 지원 및 Linux 장애 주입 검증은 M5/M6, 운영 증거 보존과 절차는 M7에 남는다.
- 범위 구분: 기존 M4-03에 남았던 root 전용 영속 이벤트와 증거 결속·검토, 실패 시 차단, 중단 후 재진입 및 안전한 조건부 처리 구현과 로컬 장애 시험은 M4-04에 이관한다. M5/M6의 외부 계약과 Linux 검증은 이를 실제로 보장하기 위한 필수 의존성이다. M7에서 운영자 역할, 증거 보관 기간·접근, 승인 및 구체적 복구 절차를 정할 수 있지만 그 결정 전에는 종결 기능을 활성화하지 않는다. 단순 필드 일치나 정상 health는 상관관계·소유권의 증거가 아니고, 서로 다른 저장소를 한 번에 커밋할 수 없어 불명 결과가 생길 수 있으므로 현재 자동 종결을 허용하지 않는다.
- 복구 전용 Studio 검증을 일반 전환 preflight에서 분리했다. GPU lock·Studio/gate 신원과 인증·journal digest 및 Studio 독립 복구 상태를 요구하며 미지원 응답은 거절한다. 정상 전환과 journal 복구는 gate의 영속 배타 예약을 요구하며, 빈 journal에서 자기 예약이 남은 경우 독립 hold를 보존하는 원자적 해제 계약을 사용한다. 실제 Studio endpoint·gate 예약/해제 연산은 M5-03/02에서 구현해야 한다. 해제 응답 유실 후 재획득도 실패하면 hold 보존을 보장하지 못하므로 미해결 상태를 유지한다.
- 전체 로컬 회귀는 419개 통과·1개 건너뜀이며 peer UID와 gate는 일부 대역을 사용했다. 운영 서비스 변경, 실제 gate 조건부 연산 및 Linux 권한 검증은 수행하지 않았다.

### M4-04 이관된 미완료 작업

**2026-11-09 가정용 단일 관리자 결정:** 아래 과거의 origin·root 자산·별도 UID·엄격한 감사 출처 검증은 이번 전환 기능의 완료 게이트에서 제외한다. 사용자는 동일 관리자 계정을 사용하고 Studio 배포 방법도 변경 가능하다고 확인했다. 앱·관리자 인증, 공용 worker, GPU lock/journal, gate admission/drain·hold 보존과 불명 상태의 안전한 복구는 계속 필수다. 해당 결정이 아래 표의 기존 보안 강화 항목과 구 체크리스트에 우선한다. M4-02/03은 축소된 로컬 범위만 완료했고 실제 gate/Studio 제공자와 Linux/운영 검증은 M5~M8에 남는다.

로컬 변경: 전환 preflight는 더 이상 UI/Caddy 자산 출처나 Client 전용 UID를 필수로 요구하지 않는다. 현재 서버의 `yeppyshiba` Client와 `ai-studio` gate/Studio 계정, socket 그룹을 반영했고 기능 검사와 공유 GPU lock·journal 대조 및 복구 preflight는 유지한다. 별도의 앱·운영자 인증도 유지한다. 실제 운영의 gate는 protocol 3이 아니므로 후보를 배포·연결했다고 주장하지 않는다.

gate protocol 3의 owned hold·epoch·예약·streaming 및 Studio recovery endpoint 패치를 handoff 기반 로컬 후보로 추가했다. gate는 재시작 때 startup hold를 영속 추가하고, 응답 streaming이 끝날 때까지 active 요청을 유지한다. Studio 후보는 공유 GPU 잠금을 다시 취득하지 않고 journal digest와 root 소유 잠금·자체 복구 상태를 확인한다. 당시 Client snapshot 연동은 미구현이었고, 기존 gate 상태 이전·실제 Studio 패치 적용·Linux 서비스 간 검증은 여전히 필요하다. 당시 전체 로컬 회귀 **651개 통과·1개 건너뜀**; Studio 전체 원본 테스트는 `sqlalchemy` 미설치로 실행하지 못했다. 서버 설정과 서비스는 변경하지 않았다.

후속 후보 검증: Client의 추론 경로는 같은 모델 snapshot의 epoch를 전달하고 전환 활성 상태에서 구형 gate fallback을 거절한다. gate/Client ASGI 통합에서 정상 streaming·일반 응답 및 전환 사이의 stale 409를 확인했다. UI는 409 시 초안을 보존하며 모델 목록을 재조회하고, 전환 성공 시 활성 모델도 갱신한다. 현재 전체 백엔드 로컬 회귀 **669개 통과·1개 건너뜀**, 프런트 lint·13개 테스트·build 통과다. 이 결과는 실제 gate 상태 이전이나 Studio/Linux 종단 검증을 대신하지 않는다.

M5-01 부분 구현: `MODEL_CONTROL_ENABLED=true`와 고정 `LLM_MODEL_ID`의 조합을 설정 단계에서 거절하며 전환 비활성 시 기존 고정 모델 설정은 유지한다. 현재 로드된 운영 모델의 읽기 전용 `/props` 및 `/slots`는 context 16384를 보고했다. 다른 모델의 실제 slot context·기능과 모델별 요청 예산은 아직 확인·구현하지 않았으며 추정하지 않는다. 이 변경까지 포함한 백엔드 전체 회귀는 **672개 통과·1개 건너뜀**이다.

2026-11-09 후속 읽기 전용 확인: 운영 `bc250-llm-gate`, `bc250-ai-studio`, `bc250-llm-client`, `llama-server`는 active이고 `bc250-model-worker.socket`은 설치되지 않았다. gate와 Studio는 `/opt/bc250-ai-studio`의 `ai-studio` 계정 서비스이며 Client는 `yeppyshiba` 계정으로 실행된다. `/usr/local/bin/ai-model`은 기존 `/opt/ai/scripts/ai-model`을 가리킨다. Client 자동 배포 스크립트는 backend와 frontend 정적 산출물만 교체하고 Client를 재시작하며 gate·Studio·worker를 설치하지 않는다. 이 조회는 서버 상태를 변경하지 않았으며 protocol 3 이전이나 실제 전환 준비를 증명하지 않는다.

M4-04 후속 로컬 검증: worker의 gate 사전 검사가 protocol 3 응답의 미지·중복 hold 및 누락되거나 활성인 복구 예약을 거절하도록 보완했다. 실제 로컬 gate ASGI 후보와 worker adapter를 연결하여 예약 중 Studio hold 거절, gate 재시작 후 예약·관리자 hold 보존, 이전 epoch 거절 및 조건부 해제를 확인했다. 관련 기존 시험 129개, 변경 후 gate·예약 통합 시험 33개 및 preflight·복구 관련 시험 104개가 각각 통과했다(일부 중복). 서비스 신원과 Studio endpoint는 이 통합 시험에 포함되지 않으며 실제 Linux·운영 검증은 남는다. 전체 테스트 재실행은 완료되지 않아 통과로 기록하지 않는다.

| ID | 출처 | 구현·검증 작업 | 완료 조건 | 상태 |
| --- | --- | --- | --- | --- |
| M4-04-01 | M4-02 | 전환·배포의 운영 조율 | 전환 중 배포/restart가 겹치지 않도록 운영 절차를 정하고 미해결 전환은 보존 | 로컬 절차 완료: 단일 관리자의 push·배포·restart와 전환 간 수동 조율을 설치 후보 README에 명시. 자동 강제는 없으며 실제 확인 명령·설치·복구는 M7 |
| M4-04-02 | M4-02 | Client·Studio 접속과 모델 API 경로 | 기존 `/llm` 및 Studio 연동 동작, 관리 API 인증과 오류 처리 확인 | 로컬 완료: API 인증·오류 회귀 및 실제 Studio 후보의 fake 엔진과 Client 이미지 경로 통과. 실제 `/llm` 라우팅과 운영 UI/API는 M6/M8 |
| M4-04-03 | M4-02 | 실제 서비스·공유 GPU 파일 상태 확인 | Studio·worker가 같은 GPU lock/journal을 사용하고 실패·부재 시 전환 거절 | 로컬 완료: Linux 별도 프로세스의 `/proc/PID/root`와 실제 flock·inode 비교, 잠금 교체·journal 부재 거절 검증. 실제 Studio 서비스 namespace와 설치 파일 검증은 M6/M7 |
| M4-04-04 | M4-02 | 기능 중심 전환 preflight | 인증된 gate/Studio 기능 계약과 공용 GPU lock을 확인; 미지원·불일치 시 변경 전에 거절 | 로컬 완료: 패치된 Studio 앱과 protocol 3 gate 앱, 실제 flock·procfs를 worker preflight에 연결해 성공·Studio 사용 중·gate 인증 실패를 검증. 서비스 신원·Studio 파일 경로 활성 여부는 대역이며 실제 프로세스와 전체 전환은 M6/M7 |
| M4-04-05 | M4-03 | 작업 상태 기록과 root 조사 | 기존 작업 DB·journal·hold 상관관계를 보존하고 불명 결과를 표시 | 로컬 완료: 작업 DB에 hold ID를 저장하고 root 조사에서 불일치를 분류. worker·실제 gate 후보 결합 시험에서 결과 불명과 상관 journal/hold를 재시작까지 보존. 별도 서명·검토자 인증·불변 archive는 제외; 실제 Linux·운영 검증은 M6/M8 |
| M4-04-06 | M4-03 | 중단·불일치 복구 | 일치 작업은 기능 검사를 거쳐 복구하고 소유권 불명·타 hold는 보존; 실패·응답 유실을 시험 | 로컬 완료: gate 후보 재시작·worker 재시작 후 복구, 해제 응답 유실과 독립 hold 보존 검증. 실제 Studio와 worker의 종단 복구·중단점 검증은 M6/M7. 자동 orphan 종결은 요구하지 않음 |

M4-04 검증기·복구 구현이 완료돼도 M5의 실제 gate·Studio 지원, M6의 최종 후보/Linux 시험, M7의 배포·복구 준비와 M8 운영 확인 전에는 전환을 활성화하지 않는다. Studio DB 차단이나 과거 유지보수 VM 시험은 다시 시작하지 않는다.

후속 계약 시험: 패치된 Studio recovery endpoint의 실제 응답과 gate 후보의 인증 응답을 worker recovery preflight에 연결해 정상 journal digest, 잠금 미확인 및 Studio 인증 실패를 확인했다. 관련 gate·Studio·preflight·예약 시험 53개 통과. Studio endpoint는 handoff 코드에서 추출했고 서비스 신원·잠금 소유·Studio의 gate identity 확인은 대역이므로 실제 프로세스 종단 검증은 아니다. 따라서 위 세부 항목을 완료로 변경하지 않는다.

추가 worker 통합: 실제 작업 DB·journal·worker 및 gate ASGI 후보를 연결하여 재시작 결과 불명 후 `recovery_required`와 상관 hold를 보존하고, worker 재시작 후 이전 모델 복구 및 독립 `legacy` hold 보존을 확인했다. 모델 서비스·Studio 검증·drain은 대역이며 이후 실제 thread로 재실행해 통과했다. 관련 시험 109개 통과. M4-04-05의 로컬 완료는 실제 서비스나 운영 성공을 의미하지 않는다.

Studio 원본 검증: handoff 아카이브를 임시 디렉터리에 풀고 recovery 패치를 적용했으며 의존성을 설치했다. 샌드박스에서 TestClient의 스레드 간 호출이 대기했으나 제한 밖의 동일 후보에서 GPU·journal 관련 36개 시험이 통과했다. Client·Studio fake 엔진 종단 시험은 오래된 gate 대역을 snapshot 계약으로 수정한 뒤 통과했다. Client 전체 회귀는 678개 통과·1개 건너뜀. 실제 GPU·운영 프로세스 검증이 아니며 운영 Studio는 변경하지 않았다.

추가 Linux·실패 통합: 별도 프로세스의 실제 procfs 경로와 flock을 이용해 공유 inode 확인, 충돌, lock 교체 및 journal 부재 거절을 확인했다. 실제 worker·gate 후보에서 restart 결과 불명과 gate 해제 응답 유실 두 경로를 재시작 후 복구하고 다른 관리자 hold가 보존되는 것을 확인했다. 관련 시험 15개 통과. 프로세스는 실제 Studio 서비스가 아니며 Studio preflight·모델 서비스·drain은 대역이다.

정상 preflight도 handoff Studio coordination endpoint의 실제 함수를 사용해 정상 응답과 Studio 작업 중 거절을 확인했다. gate identity·서비스 신원·파일 검사는 해당 시험에서 대역이다. 관련 Studio·preflight·worker/gate 시험 22개 통과.

후속 로컬 앱 통합: 패치된 Studio 앱과 protocol 3 gate 앱의 실제 endpoint, worker 정상 preflight 및 실제 flock·procfs 파일 비교를 연결해 정상·Studio 사용 중·gate 인증 실패를 시험했다. Studio의 canonical 경로 활성 여부와 systemd 서비스 신원은 대역이고 모델 변경은 수행하지 않았다. Client·Studio 후보 통합 2개 통과. M4-04 완료는 로컬 구현과 계약 검증의 완료이며 M5~M8 실제 통합·배포·GPU 확인을 대신하지 않는다.

최종 로컬 백엔드 회귀 681개 통과·2개 건너뜀(Studio 후보 연동 2개는 별도 실행해 통과). 프런트 lint는 통과했지만 기존 node_modules에 Vitest가 없어 테스트·빌드를 완료하지 못했고 `npm ci` 복원도 완료되지 않았다. 운영 Client 설정 파일은 일반 계정에서 읽을 수 없어 전환 활성 여부를 직접 확인하지 못했다. 따라서 현재 Client 자동 배포의 운영 준비가 검증됐다고 판단하지 않는다. 자동 배포는 worker·gate·Studio를 설치하지 않으며 전환 기능 시험을 대신하지 않는다.

사용자 root 읽기 전용 확인: Client env의 `MODEL_CONTROL_ENABLED`는 `unset-or-other`, `LLM_MODEL_ID`는 `empty-or-unset`으로 보고됐다. 전자는 미설정과 다른 표기를 구별하지 않으므로 실제 유효값을 단정하지 않는다. 공유 GPU lock·switch journal·신규 switch gate/Studio 조율 credential 경로는 모두 부재했다. 운영 gate는 Studio venv의 구형 factory로 실행 중이다. protocol 3용 systemd credential 기반 factory와 unit 후보를 추가했고 관련 gate·worker 시험 22개가 통과했다. 로컬 `systemd-analyze verify`는 대상 서버에만 있는 Studio venv 경로가 없어 실행 파일을 확인하지 못했다. 이 결과는 대상 설치나 서비스 동작 검증이 아니다. 서버에서 비밀값을 생성·보관하는 방향이며 GitHub에 서비스 키를 등록할 필요는 아직 확인되지 않았다.

M7-01 준비: [설치·복구 절차 초안](llm-model-switch-install-recovery.md)에 구성·credential, 백업, gate 상태 이전, 비활성 worker 설치, root 조사·복구 및 롤백 순서를 정리했다. 기존 gate 상태 이전 명령, worker 의존성 패키징, 실제 Studio 배포 설정 및 비밀 비노출 상태 확인 등은 미완료이므로 실행 승인용 최종 runbook이나 M7 완료가 아니다.

후속 작업: 운영 gate 원문은 영속 hold가 없는 메모리 `paused`·active·waiting 계약임을 확인했다. 로컬 이전 후보는 인증된 구형 상태의 active/waiting이 모두 0일 때만 신규 상태를 독점 생성하고, pause는 `legacy` hold로 보존하며 `startup` hold를 추가한다. 기존 파일 덮어쓰기 및 잘못된 snapshot은 거절한다. 후보는 서비스 정지나 동시 제어 배제를 스스로 증명하지 못하므로 실제 이전 절차·역방향 복구는 미완료다. 관련 gate·worker 시험 30개 통과. gate는 Studio 배포와 분리된 Client 관리 코드·venv를 사용하도록 unit 후보를 수정했다. Studio의 이미지 기능과 공용 GPU 조율은 유지하며 실제 설치는 하지 않았다.

사용자 읽기 전용 조회: gate PID 1014, `paused=false`, active/waiting 모두 0, 조회 전후 PID 동일; Client·Studio·gate·llama 모두 active였다. 이 값은 당시 순간 상태이며 적용 시 재확인이 필요하다. 비밀을 출력하지 않는 재조회 후보 `scripts/legacy_gate_status.py`는 systemd 상태와 PID/시작 시각을 전후로 확인한다. 이전·pause·정지는 실행하지 않으며 해당 status·이전·runtime 시험 14개 통과.

설치 후보 후속: 운영 Studio 세 파일은 handoff와 hash가 다르지만 현재 패치는 운영 원문 복사본에 dry-run 적용 성공(GPU hunk fuzz 1); 실제 배포 원본에 대한 정식 적용·전체 테스트는 미완료다. 서버 Python은 3.14다. 별도 worker 의존성 staging 빌더와 고정 버전 목록을 추가하고 로컬 및 서버 임시 경로에서 `-I -S` import를 확인했다. 기존 gate venv가 worker bootstrap 검사 트리 안에 있으면 symlink/bytecode 검사로 실패하는 충돌을 발견해 gate를 `/opt/bc250-llm-gate`로 분리했다. 서비스 설치·동작이나 root 권한·SELinux 검증을 뜻하지 않는다.

후속 Studio 기준 정정: 운영 세 파일의 hash는 `imagegen-closeout-20261107` 보존본과 일치한다. 이 기준으로 fuzz 없이 적용되는 별도 patch를 생성하고 운영 원문 임시 복사본에 dry-run했다. 패치된 closeout 후보의 Studio GPU/journal 시험 36개와 Client·Studio/worker socket 시험 5개 통과. 운영 서버 적용이나 실제 GPU 검증은 아니다. Linux 실제 socketpair에서 SO_PEERCRED도 확인했다.

Studio CI 대상 후속: 실제 저장소 `qlazzarus/bc250-ai-studio`의 main `47f7dc5`에서 protocol 3 변경과 복구 endpoint 회귀를 [초안 PR #1](https://github.com/qlazzarus/bc250-ai-studio/pull/1)로 준비했다. main push는 자동 배포되므로 미병합 상태다. 기존 Studio 릴리스 백엔드 129개, 새 endpoint 1개 및 Ruff가 로컬에서 통과했다. 현재 workflow는 PR에서 실행되지 않아 GitHub CI 통과 증거는 없다. 운영 서버에서는 사용자 소유 일회성 systemd service에서 worker 격리 import, 임시 socket/service에서 실제 socket activation을 확인했다. root 소유 unit·credential·실제 worker/gate 설치, 운영 상태 이전 및 롤백 종단 시험은 수행하지 않았다.

Studio PR #1은 사용자 보고 및 GitHub에서 merge commit `448bcd3375b8` 병합을 확인했고, 운영 Studio 코드 해시가 이전 버전에서 변경됐다. Client·Studio·기존 gate·llama는 조회 시 active였으며 worker socket은 미설치였다. 실제 배포 설정과 GPU 동작은 별도 확인이 필요하다.

gate 이전/롤백 로컬 보완: 구형 pause 양쪽을 protocol 3로 이전해 gate 재시작과 startup hold 해제 후 원래 pause가 보존되는지 확인했다. protocol 3 상태를 구형 bool로 표현할 수 있는지 검사하는 순수 판정도 추가했다. 다른 hold·예약·진행 중 요청·불명 상태는 롤백 판정을 거절한다. 관련 gate·worker 시험 52개 통과. 판정만으로 worker journal/DB가 안전하거나 구형 gate 재기동의 pause 공백이 해결된 것은 아니며, 실제 Linux 서비스 설치·상태 이전 및 롤백은 미완료다.

2026-11-09 사용자 판단: 단일 사용자·관리자 환경에서 UI로 모델을 쉽게 바꾸는 것이 목표다. 기존 conf 형식이나 서비스 구성을 유지하는 일 자체는 필수 조건이 아니며 필요하면 변경할 수 있다. 다만 전환과 실제 로드 모델의 일치, GPU 공유 잠금, journal, gate drain·다른 hold 보존, 불명 결과의 복구 및 기존 인증은 유지한다. 설정 변경 방식은 실제 통합에서 기능과 복구를 검증해 결정한다.

#### 과거 M4-04 보안 강화 체크리스트 — 이번 필수 범위에서 제외

아래 항목은 이전 설계와 조사 이력이다. 사용자 결정에 따라 동일 origin 보안·별도 UID·불변 감사 체계는 이번 기능의 완료 조건이 아니며, 현행 실행 기준은 위 표와 통합 계획이다. 개별 기능의 작동 여부를 주장하는 표가 아니다.

| 세부 ID | 선행 | 실행 작업 및 완료 기준 | 현재 상태 / 후속 이관 |
| --- | --- | --- | --- |
| 01-a | M4-02/03 | root 검토 anchor·manifest 및 배포 잠금의 취득·실패·취소 계약을 구현하고 경합 대역 시험 | 부분 구현; 모든 변경자 참여와 root 승격은 M7, Linux 권한은 M6 |
| 01-b | 01-a | journal·작업 DB·gate hold/예약 상태를 대조하는 배포 차단을 구현하고 timeout·WAL 손상·불명 상태를 거절 | journal/DB 및 gate 전후 상태의 진단 추가; gate 인증·동시성은 미입증이어서 실제 가드는 무조건 차단. 실제 gate 계약은 M5-02, 배포자 설치와 WAL 운영 절차는 M7 |
| 02-a | 01-a | 전체 Caddy 실효 설정의 listener·중첩 route·matcher·rewrite·순서를 독립 검토 정책과 비교하고 알려지지 않은 입력 거절 | 전체 설정 digest·Unix admin·서버/listener·중첩 트리 전후 대조의 진단 연결. peer 인증과 독립 정책의 신뢰 공급은 호출자 책임이며 동적 경로는 미검증 |
| 02-b | 02-a | 모든 정적 자산·backend 실행본·listener·namespace를 검토 정책에 결속하고 미등록·변경·권한 오류 거절 | 조사 부품만 있음; Linux proc·ACL/SELinux 경계와 실제 경합은 M6 |
| 03-a | 02-a | 동적 응답·업로드·service worker 범위를 분류하고 모든 관련 route에 정책을 강제하며 정상·악성 응답 회귀 확인 | route별 응답 생산자와 profile·표본 결속 진단 추가. 표본 통과는 전체 응답 강제의 증거가 아님; Studio 실제 변경은 M5-03, 실 HTTP 검증은 M6 |
| 03-b | 02-b | loader·라이브러리·실행 코드 및 동적 로딩 신뢰 모델을 완성하고 불명 런타임 거절 | maps 진단에 공유 쓰기 매핑 거절 추가; 실행 메모리 무결성은 디스크 hash만으로 증명 불가, Linux 검증은 M6 |
| 04-a | 01-a/01-b, 02-a/02-b, 03-a/03-b | origin 검증을 preflight에 연결하고 안전한 지원 구성의 성공과 변조·경합·미지원 차단을 시험 | 선행 미완료; 현재 무조건 차단 유지 |
| 05-a | M4-03 | root 조사 증거를 출처·서비스 세대·gate 상태에 결속하고 검토자 인증 및 보호 저장/백업을 구현 | root-private 증거 archive와 digest·typed 기록·pinned review 진단 결합 후보 추가. archive는 전달된 bytes를 그대로 저장하므로 비밀 제외를 보장하지 않으며 허용 스키마·redaction 수집기 미구현; digest는 검토자 인증이 아니고 출처·신선도·독립 백업 미검증. root 권한 및 배포/백업은 M6/M7 |
| 05-b | 05-a | 이벤트·checkpoint 중단, fsync 실패, 기록 손상·누락·동시 rollback을 재시작 후 감지하고 실패 시 복구 차단 | 단일 파일 실패 대역만 일부 있음; 독립 신뢰 근거와 실제 Linux는 M6/M7 |
| 06-a | 05-a/05-b | 충분한 독립 소유권 증거가 있는 제한된 불일치만 조건부 처리하는 영속 상태 기계 구현 | 판정 부품만 있음; gate/Studio 원자 계약은 M5-02/03 |
| 06-b | 06-a | 응답 유실·각 중단 지점·재진입 및 증거 부족 시 기록과 다른 hold 보존을 검증 | 미완료; 최종 후보 통합은 M6, 운영자 조사·복구 절차는 M7 |

위 과거 체크리스트의 01~03→04 및 엄격한 05 선행 관계는 현재 필수 조건이 아니다. 다만 증거 부족 orphan은 여전히 임의 연결이나 삭제 대신 미해결로 보존한다.

2026-11-09 중간 검증: 전환·복구 origin 잠금 경합과 취소, proc 경계, 제한적 응답 검사, 감사 기록·체크포인트·재진입 판정 부품을 추가한 뒤 전체 로컬 회귀 482개 통과·1개 건너뜀. 소유권 및 Linux 경계 일부는 대역이며 실제 운영 검증이 아니다. 서버 Caddy handler 읽기 전용 확인은 SSH 연결 시간 초과로 수행하지 못했다.

후속 읽기 전용 조사: 외부 주소 `yeppyshiba.iptime.org:2222`에서 기존 BC-250 SSH 키로 접속해 `fedora`를 확인했다. Caddy `srv0`의 실효 HTTP 설정에는 `encode`, `subroute`, `rewrite`, `vars`, `file_server`, `reverse_proxy`, `static_response`가 있으며, `/llm/api/*`→8010, `/wiki/*`→8020, `/api/*`→8090, `/llm/*` 및 기본 Studio 정적 자산 순서의 중첩 경로가 존재한다. file matcher는 요청 경로 다음 `/index.html`을 시도한다. 이 정보는 설정의 일부만 요약한 것이며 헤더나 전체 보안 정책의 검증 증거가 아니다. Caddy 서비스는 `NoNewPrivileges=no`, Client와 Wiki는 `yeppyshiba`로 실행되며 Client dist와 Wiki app 디렉터리는 그 계정에서 쓰기 가능하다. 현재 운영 구성은 후보 보안 계약을 만족한다고 판단하지 않는다. 설정·키·서비스는 변경하지 않았다.

사용자 root 읽기 전용 조회: Caddy·Client·Studio·Wiki의 PID namespace는 모두 `pid:[4026531836]`, 네트워크 namespace는 `net:[4026531833]`이나 mount namespace는 각각 `mnt:[4026532706]`, `mnt:[4026532703]`, `mnt:[4026532633]`, `mnt:[4026532705]`다. Caddy PID 1413이 `127.0.0.1:2019` TCP admin을 수신한다. Client dist와 Wiki app은 UID/GID 1000, Studio dist는 1001, 모두 mode 0755의 기본 ACL이며 각 소유자 쓰기가 허용된다. Caddyfile은 root 소유 0644의 기본 ACL이다. 각 디렉터리는 SELinux `usr_t`, Caddyfile은 `httpd_config_t`로 표시됐다. 서로 다른 mount namespace가 같은 inode를 본다는 근거는 아니고 TCP admin의 변경 권한은 시험하지 않았다. 이 결과는 M4-04-01~03과 M6/M7의 보안·권한 이관 근거로 사용하며 운영 변경은 하지 않았다.

추가 root 조회에서 두 정적 자산 디렉터리 자체는 host와 Caddy PID 1413의 namespace가 같은 device·inode를 보았다. Client dist는 `(35, 329753)`, Studio dist는 `(35, 351608)`이고 소유권과 mode도 양쪽 관점에서 각각 UID/GID 1000·0755, UID/GID 1001·0755로 일치했다. 이는 해당 시점의 디렉터리 inode 확인이며 내부 전체 파일·상위 경로·향후 교체까지 검증한 것은 아니다.

추가 로컬 회귀: 중첩 route 트리, runtime mapping, 조사 기록과 journal/DB 배포 가드 후보 및 손상 WAL 차단, typed 로그의 스냅샷 연속성 보완을 포함해 **564개 통과·1개 건너뜀**. 실제 운영 Caddy의 302/Location 형태를 제한적으로 비교할 뿐 실제 리다이렉트 의미와 동적 응답 보안성은 미검증이다. 가드는 내용이 있는 WAL을 무조건 거절하므로 실제 운영에 적용하기 전 M7에서 안전한 checkpoint·점검 절차가 필요하다. 각 부품은 여전히 preflight/복구 승인에 연결되지 않았으며 실제 Linux·gate·Studio 통합 증거가 아니다.

실행 체크리스트 추가 후 진단 변경을 포함한 전체 로컬 회귀는 **605개 통과·1개 건너뜀**이다. gate 상태 진단과 pinned review 대조가 성공해도 인증·동시성 및 검토자의 실제 승인 증거가 없으므로 각각 배포나 복구를 승인하지 않는다. 서버 설정·파일·서비스는 변경하지 않았다.

외부 SSH 재확인: Caddy·llama-server·Client·gate·Studio·Wiki는 모두 active/running이다. Client·Wiki는 `yeppyshiba`, gate·Studio는 `ai-studio`, Caddy는 `caddy`로 실행되며 Caddy의 `NoNewPrivileges=no`다. 같은 origin의 `HEAD /`와 `HEAD /llm/`는 HTML 200, `HEAD /wiki/`는 JSON 405였고 세 응답에서 CSP·`X-Content-Type-Options`·`Service-Worker-Allowed` 헤더가 관찰되지 않았다. 이는 세 HEAD 응답만의 관찰이며 GET·업로드·다른 오류 경로 전체의 정책 증거는 아니다. 운영 변경은 하지 않았다.

사용자 root 읽기 전용 배포 권한 확인: `sudo -l -U yeppyshiba`에는 일반 `(ALL) ALL` 권한과 Client 서비스 재시작 및 `/usr/local/sbin/bc250-ai-studio-deploy`에 대한 별도 NOPASSWD 허용이 표시됐다. 일반 `(ALL) ALL`의 인증 여부를 NOPASSWD라고 추정하지 않는다. Client 루트·backend·frontend 및 Wiki 루트는 UID/GID 1000·0755, Studio 루트와 `/etc/caddy`는 root·0755이며 조사한 상위 경로의 ACL은 기본 owner/group/other 항목이다. Studio NOPASSWD 배포 명령의 구현과 신뢰 경계, sudo 인증 정책, 모든 관리자 경로의 잠금 참여는 아직 확인하지 않았다. M4-04-01의 신뢰 모델 및 M7 root 승격·배포 절차에 반영하며, 조회 과정에서 설정·서비스는 변경하지 않았다.

Studio 배포 wrapper는 현재 root 소유 0755의 일반 파일이며 `yeppyshiba`가 쓸 수 없다. 일반 계정의 읽기 전용 문자열 분류상 staging·lock·systemctl 관련 코드가 있으나, 이 사실만으로 입력 검증·자산 승격·공용 잠금 참여를 증명할 수 없다. 신뢰된 root 운영자를 전제로 일반 서비스 계정과 운영자 권한을 분리하고, Client restart와 Studio NOPASSWD 경로의 실행 코드·의존성·입력 승격 경계를 검증해야 한다. 실제 배포 구성과 독립 Studio 승격은 M7에서 처리한다.

후속 로컬 검사: route별 응답 profile 진단, runtime 공유 쓰기 매핑 거절, 조사 증거 archive·review 결속 및 FIFO 거절을 포함한 전체 회귀는 **637개 통과·1개 건너뜀**이다. archive는 원본 bytes를 보관하며 비밀 redaction이나 출처 인증을 제공하지 않고 운영 복구에 연결하지 않았다. 기존 `main` push 배포는 Client 서비스 재시작과 일반 계정 쓰기 경로 갱신을 수행하고 model worker/root runtime을 설치하지 않으므로, 이번 진단 부품을 검증하기 위한 운영 배포는 수행하지 않았다.
