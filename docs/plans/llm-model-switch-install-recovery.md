# 모델 전환 설치·복구 절차 초안

상태: **검토용, 운영 적용 불가**. Client·Studio·gate·worker를 하나의 호환 후보로 준비하고 M6 검증과 아래 미결 항목을 해결한 뒤 M7에서 실제 파일·명령을 확정한다. 이 문서는 운영 변경을 실행하지 않는다. 기존 Wiki의 직접 llama 호출은 이번 조율의 예외이며 전환 중 실패할 수 있다.

## 구성과 비밀값

| 구성 | 설치·설정 대상 | 현재 상태 |
| --- | --- | --- |
| Client | 기존 Client CI의 backend와 frontend; 전환은 기본 비활성 | 운영에는 이전 버전, 현재 후보 미배포 |
| gate | `gate_candidate`와 protocol 3 unit 후보, 전용 상태 및 역할 키 | 기존 gate는 Studio venv의 구형 factory; protocol 3 미설치 |
| Studio | handoff 원본에 적용한 recovery 패치와 protocol 3 설정 | 패치는 로컬 후보; 실제 배포 대상과 차이 확인 필요 |
| worker/CLI | root 관리 실행 트리·소켓·unit·기존 CLI 백업 및 교체 | 미설치; 기존 Client 배포가 설치하지 않음 |
| GPU 공유 | `/run/bc250-gpu/gpu.lock`, worker lifetime lock, `/var/lib/bc250-model-control/switch.json` | 사용자 확인 시 공유 lock과 journal 부재 |

gate는 [unit 후보](../../deploy/model-control/bc250-llm-gate-protocol3.service)의 systemd credential 네 개를 읽는다. admin·Studio·switch 키는 서로 달라야 하며 Studio 조율 키도 별도다. `llm-key`는 upstream 인증 미사용 시 빈 파일을 포함해 실제 서버 설정에 맞게 준비한다. 키는 서버 root가 생성·보관하고 내용은 채팅·로그·Git에 넣지 않는다. Client의 기존 앱·운영자 인증은 유지한다. 공개 GitHub에는 배포용 SSH credential만 기존 Actions Secret으로 전달하며 서비스 키를 등록하는 것은 필수가 아니다.

Studio는 `STUDIO_ADMISSION_URL`, `STUDIO_ADMISSION_KEY`, `STUDIO_ADMISSION_OWNED_HOLDS`, `STUDIO_COORDINATION_KEY`, `STUDIO_GPU_LOCK_PATH`, `STUDIO_GPU_LOCK_SECURE_PRECREATED`, `STUDIO_SWITCH_JOURNAL_PATH` 설정을 사용한다. 역할별 키는 gate credential과 값이 일치해야 한다. 실제 배포 대상의 설정 이름·키 전달 방식과 기존 값을 적용 전에 확인한다. Client 전환 활성화와 고정 `LLM_MODEL_ID`는 함께 사용할 수 없다.

## 1. 설치 전 준비와 중단 조건

1. 최종 Client commit, Studio 배포 대상과 patch 적용 결과, gate·worker 코드 및 의존성 버전을 고정한다. Studio 기존 배포 workflow가 gate 코드 또는 venv를 덮어쓰는지 확인하고, protocol 3 gate가 사용하는 코드·venv가 이후 배포에도 유지되도록 배치한다.
2. gate factory의 상태 디렉터리는 `ai-studio`가 쓸 수 있어야 한다. gate 코드와 별도 venv는 Client 측 `/opt/bc250-llm-gate`에서 관리해 Studio 배포 및 worker bootstrap 검사 트리와 분리한다. worker 실행 트리는 `/opt/bc250-model-control/lib` 아래에 `backend.app.model_control`과 `/usr/bin/python3 -I -S`에서 실제 import 가능한 의존성이 포함되어야 한다. symlink·bytecode·`.pth`에 대한 bootstrap 검사와 대상 서버의 Python/SELinux를 검증한다. gate unit 후보의 실행 파일과 working directory도 실제 대상에서 검증한다.
3. GPU lock은 root 소유, Studio가 잠글 수 있는 공유 그룹 `0660`; journal은 root 소유, Studio가 읽을 수 있는 그룹 `0640`, 초기 유효 내용은 `{}`다. 상태 디렉터리·그룹·ACL·SELinux와 `/run` 재부팅 후 생성 방식을 준비한다. 운영 서비스가 시작한 뒤 lock inode를 교체하지 않는다. **이미 존재하는 journal·gate 상태·DB를 초기화하거나 덮어쓰지 않는다.**
4. 운영 적용 전 실제 Studio 이미지 작업·복구, gate의 active/waiting 및 기존 pause, llama systemd 작업을 확인한다. 인증 실패·상태 불명·작업 진행 중·미해결 전환이면 중단한다. 기존 gate에서 durable hold/epoch 지원 여부를 추정하지 않는다. 독립 hold 및 기존 pause를 보존할 이전 절차를 준비한다.
5. gate 교체 시 active와 waiting이 모두 0인지 확인한다. 대기 요청이 있으면 기존 pause를 해제해 재전송하지 않고 중단한다. Client/Studio 배포와 수동 CLI 실행도 이 구간에 중단한다. `migration.provision_from_legacy_snapshot`은 인증된 snapshot의 세 필드를 검증해 pause를 보존하지만 서비스 중단·다른 제어자의 배제를 증명하지는 않는다.

읽기 전용 서비스 상태 확인 예시:

```bash
systemctl is-active bc250-llm-client.service bc250-ai-studio.service bc250-llm-gate.service llama-server.service
systemctl is-enabled bc250-model-worker.socket 2>/dev/null || true
sudo systemctl show bc250-llm-gate.service bc250-ai-studio.service llama-server.service -p ActiveState -p SubState -p MainPID -p Job
```

이 명령은 gate active/waiting이나 Studio 내부 이미지 작업의 부재를 증명하지 않는다. 인증된 상태 조회와 해당 응답의 안전한 검증 방법을 M7에서 별도 확정한다. credential 값은 명령행 인자나 출력에 넣지 않는다.

인증된 구형 gate의 읽기 전용 조회 후보는 [legacy_gate_status.py](../../scripts/legacy_gate_status.py)다. root로 실행하면 키를 출력하지 않고 gate PID와 pause·active·waiting만 출력하며 조회 전후 systemd/PID 시작 시각을 비교한다. 후보를 서버에 배치·검증하기 전에는 이 명령이 서버에 이미 설치되었다고 가정하지 않는다. 조회는 이후의 제어 경합을 방지하지 않으므로 실제 교체 시 다시 조회하고 다른 작업을 중단해야 한다.

worker 의존성 staging 후보는 `python scripts/build_model_worker.py <새-디렉터리>`다. 기존 디렉터리는 덮어쓰지 않고 고정된 의존성을 설치해 `-I -S` import를 확인하며 운영 설치는 하지 않는다. 빌드 결과를 서버 임시 디렉터리에서 Python 3.14로 import하는 시험은 통과했다. root bootstrap의 전체 소유권 검증·서비스 실행·서버 패키징 및 설치는 별도 검증이 필요하다. gate venv는 worker bootstrap이 검사하는 트리 밖에 둔다.

## 2. 백업과 적용 순서

1. Client·Studio·gate의 실제 설치 버전과 unit/drop-in, 설정, CLI 원본 및 link, 기존 gate 상태를 root만 읽는 백업 위치에 보존한다. credential이 포함된 백업은 공개 아티팩트나 GitHub에 올리지 않는다. 백업·복원 경로와 각 버전의 호환성을 기록한다.
2. 완성된 후보를 stage하고 검사한다. Studio 패치는 정확한 배포 원본에 적용 및 테스트하고, gate/worker 패키지·unit·credential·state 및 공유 lock/journal의 권한과 inode를 확인한다. **기존 서비스 교체나 CLI 전환 전에** 실패 시 원본을 그대로 유지할 수 있어야 한다.
3. 관리자 단독으로 새 요청·제어 및 배포를 중단하고 §1의 drain 조건을 재확인한다. 구형 gate 상태와 기존 pause를 기록한다. 이전 방법이 확정되지 않았으면 여기서 중단한다.
4. 호환 Client·Studio·gate를 조율해 교체한다. protocol 3 gate는 보존된 hold를 포함한 상태로 시작하며 `startup` hold를 추가한다. 초기화는 신규 상태임이 확인된 경우에만 한 번 수행하고, 미해결 기존 상태는 초기화로 덮어쓰지 않는다. Studio에는 같은 공유 파일 및 protocol 3 역할 credential을 연결한다.
5. worker와 socket을 **전환 비활성**으로 설치한다. root 관리 worker 트리와 CLI 교체를 확인하고 원본 CLI를 보존한다. 기존 CLI가 잠금을 우회해 동시 restart하지 않도록 전환 창에서 실행을 막고, 단일 공용 경로로의 전환은 운영자 확인 후 수행한다.
6. startup hold 중 정적 상태·인증·동일 inode·Studio 차단과 기존 hold 보존을 확인한다. 현 worker 정상 admission 검사는 startup hold를 거절하므로 이 단계에서 전환 가능 판정을 하지 않는다. 준비를 확인한 관리자만 startup hold를 해제하고, 전체 preflight를 다시 실행한다. 키·상태 확인 명령은 비밀을 노출하지 않는 방식으로 M7에서 확정한다.
7. M5 모델별 context 및 요청 호환, M6 통합 검증, M7 readiness가 끝나면 Client/worker를 활성화하고 M8의 UI A→B 및 B 대화를 검증한다. Wiki 예외를 기록한다.

## 3. 불명 상태 조사와 복구

작업 ID와 CLI 출력의 idempotency key를 보관한다. 타임아웃이나 연결 종료가 작업 취소를 뜻하지 않는다. 같은 작업을 확인하기 전 새 switch나 수동 restart를 하지 않는다. worker 설치 및 CLI 전환 이후 root 조사 경로는 다음과 같다:

```bash
sudo ai-model status
sudo ai-model inspect-recovery
# 작업 ID가 확인된 경우:
sudo ai-model status --operation <operation-id>
```

`inspect-recovery`는 조사 정보이며 자동 해제 승인이 아니다. 유효한 journal·작업 ID·상관 hold가 일치하고 Studio/gate 및 이전 모델 확인이 가능한 경우에만 worker의 `sudo ai-model recover <operation-id>`를 사용한다. 빈 journal과 상관 hold의 조건부 정리는 독립 hold 등 worker가 요구하는 검사가 모두 통과할 때만 `sudo ai-model resolve-verified <operation-id>`로 요청한다. 이 명령들은 root 전용이며 결과 불명 또는 실패 시 원본·hold를 보존한다. journal 손상·누락, 불일치, 소유권 불명 hold, systemd 작업 불명 또는 응답 유실은 수동 조사 대상으로 남긴다. journal 삭제, 임의 hold 해제, 반복 restart로 해결하지 않는다.

## 4. 롤백

새 요청과 배포를 중단하고 실행 중 systemd 작업, worker DB의 미해결 작업, journal과 switch hold/예약을 확인한다. 하나라도 불명이면 롤백 대신 §3으로 분기한다. 안전한 상태가 확인된 경우에만 새 worker/CLI 진입을 중단하고, 기존 CLI·Studio·gate·Client의 **서로 호환되는 백업 세트**를 복원한다. 기존 관리자 pause와 독립 hold는 보존한다. 구형 gate는 protocol 3 상태를 이해하지 못하므로 미해결 예약이나 switch hold가 있는 상태에서 구형 gate로 단순 교체하지 않는다. 이후 기존 모델 health·ID, Studio 이미지 경로, Client 추론 및 원래 pause를 확인한다.

로컬 `legacy_pause_for_rollback`은 active/waiting 0, 예약·deployment generation 부재 및 `startup`/`legacy` 외 hold가 없는 상태만 구형 pause 값으로 변환한다. 이는 다른 저장소의 미해결 기록이나 동시 작업을 검사하지 않는다. 특히 구형 gate는 재기동 시 unpaused이므로 원래 pause가 있었다면 외부 유입을 차단한 상태에서 기존 pause를 복원할 방법을 검증하기 전에는 롤백하지 않는다.

## 5. 적용 전에 해결할 항목

- gate 기존 pause/대기 연결과 protocol 3 durable state를 안전하게 이전하는 실제 명령 및 역방향 롤백.
- Studio 실제 CI 배포 버전에 패치 적용, protocol 3 credential·공유 파일 설정과 gate 코드/venv 배포 소유권 확인.
- worker root 실행 트리의 의존성 패키징, socket activation, tmpfiles, 그룹·SELinux와 부팅 후 경로 재생성 검증.
- gate/Studio/worker 인증 조회와 실행 중 작업 확인을 **비밀값 출력 없이** 수행하는 대상 서버용 명령.
- M5 모델별 실제 context·요청 예산 및 범위 내 직접 호출자 정리, M6 Linux 종단·중단 시험, M7 readiness 판정.
- Client frontend 의존성 복원 및 lint/test/build; 실제 `MODEL_CONTROL_ENABLED` 유효값은 사용자 조회가 `unset-or-other`여서 미확정.
