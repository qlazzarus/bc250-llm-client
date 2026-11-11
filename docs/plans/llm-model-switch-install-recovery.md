# 모델 전환 설치·복구 절차 초안

상태: **검토용, 운영 적용 불가**. Client·Studio·gate·worker를 하나의 호환 후보로 준비하고 M6 검증과 아래 미결 항목을 해결한 뒤 M7에서 실제 파일·명령을 확정한다. 이 문서는 운영 변경을 실행하지 않는다. 기존 Wiki의 직접 llama 호출은 이번 조율의 예외이며 전환 중 실패할 수 있다.

## 구성과 비밀값

| 구성 | 설치·설정 대상 | 현재 상태 |
| --- | --- | --- |
| Client | 기존 Client CI의 backend와 frontend; 전환은 기본 비활성 | `bf1d0ba` 자동 배포 성공(11/10); 전환 비활성 |
| gate | `gate_candidate`와 protocol 3 unit 후보, 전용 상태 및 역할 키 | 기존 gate는 Studio venv의 구형 factory; protocol 3 미설치 |
| Studio | handoff 원본에 적용한 recovery 패치와 protocol 3 설정 | PR 병합 기록 및 11/10 운영 복구 endpoint 코드 확인; 설정·실제 조율 미검증 |
| worker/CLI | root 관리 실행 트리·소켓·unit·기존 CLI 백업 및 교체 | 미설치; 기존 Client 배포가 설치하지 않음 |
| GPU 공유 | `/run/bc250-gpu/gpu.lock`, worker lifetime lock, `/var/lib/bc250-model-control/switch.json` | 11/10 공유 lock/runtime 부재 및 Studio journal 설정 미설정 확인 |

gate는 [unit 후보](../../deploy/model-control/bc250-llm-gate-protocol3.service)의 systemd credential 다섯 개를 읽는다. admin·Client·Studio·switch 키는 서로 달라야 하며 Studio 조율 키도 별도다. `llm-key`는 upstream 인증 미사용 시 빈 파일을 포함해 실제 서버 설정에 맞게 준비한다. 키는 서버 root가 생성·보관하고 내용은 채팅·로그·Git에 넣지 않는다. Client의 기존 앱·운영자 인증은 유지한다. 공개 GitHub에는 배포용 SSH credential만 기존 Actions Secret으로 전달하며 서비스 키를 등록하는 것은 필수가 아니다.

Studio는 `STUDIO_ADMISSION_URL`, `STUDIO_ADMISSION_KEY`, `STUDIO_ADMISSION_OWNED_HOLDS`, `STUDIO_COORDINATION_KEY`, `STUDIO_GPU_LOCK_PATH`, `STUDIO_GPU_LOCK_SECURE_PRECREATED`, `STUDIO_SWITCH_JOURNAL_PATH` 설정을 사용한다. 역할별 키는 gate credential과 값이 일치해야 한다. 실제 배포 대상의 설정 이름·키 전달 방식과 기존 값을 적용 전에 확인한다. Client 전환 활성화와 고정 `LLM_MODEL_ID`는 함께 사용할 수 없다.

## 1. 설치 전 준비와 중단 조건

1. 최종 Client commit, Studio 배포 대상과 patch 적용 결과, gate·worker 코드 및 의존성 버전을 고정한다. Studio 기존 배포 workflow가 gate 코드 또는 venv를 덮어쓰는지 확인하고, protocol 3 gate가 사용하는 코드·venv가 이후 배포에도 유지되도록 배치한다.
2. gate factory의 상태 디렉터리는 `ai-studio`가 쓸 수 있어야 한다. gate 코드와 별도 venv는 Client 측 `/opt/bc250-llm-gate`에서 관리해 Studio 배포 및 worker bootstrap 검사 트리와 분리한다. worker 실행 트리는 `/opt/bc250-model-control/lib` 아래에 `backend.app.model_control`과 `/usr/bin/python3 -I -S`에서 실제 import 가능한 의존성이 포함되어야 한다. symlink·bytecode·`.pth`에 대한 bootstrap 검사와 대상 서버의 Python/SELinux를 검증한다. gate unit 후보의 실행 파일과 working directory도 실제 대상에서 검증한다.
3. GPU lock은 root 소유, Studio가 잠글 수 있는 공유 그룹 `0660`; journal은 root 소유, Studio가 읽을 수 있는 그룹 `0640`, 초기 유효 내용은 `{}`다. 상태 디렉터리·그룹·ACL·SELinux와 `/run` 재부팅 후 생성 방식을 준비한다. 운영 서비스가 시작한 뒤 lock inode를 교체하지 않는다. **이미 존재하는 journal·gate 상태·DB를 초기화하거나 덮어쓰지 않는다.**

   SELinux enforcing 시험에서 관리자 셸이 만든 journal과 systemd service가 만든 임시 파일의 보안 속성이 다르면 원자 교체가 fail-closed됐다. 새 격리 디렉터리에 정책 라벨을 적용한 뒤에는 속성 보존과 원자 교체가 통과했다. 운영에서는 실제 state 경로의 정책 라벨과 worker가 생성하는 임시 파일 라벨이 일치하는지 설치 중 확인하고, 불일치하면 전환을 활성화하지 않는다. 기존 journal을 무조건 relabel하거나 초기화하는 명령으로 취급하지 않는다.
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
- Client frontend lint/test/build는 11/10 통과; 11/10 실행 환경에서 `MODEL_CONTROL_ENABLED`는 비활성, 고정 ID는 비어 있음.

## 2026-11-10 Client hold 적용 조건

`LLM_GATE_API_KEY`에는 신규 gate의 `client-key` 값을 전달하고, `LLM_GATE_HOLD_ID=client-<32 lowercase hex>`를 한 번 생성해 이후 배포에도 보존한다. gate unit에 `LoadCredential=client-key:/etc/bc250-model-control/gate-client-key`가 추가됐다. Client 재시작 후 resume가 다른 hold나 legacy pause를 해제하지 않는지 확인한다. Client pause가 남아 있으면 worker 전환은 차단된다. 기존 운영 키 정정과 신규 역할 키 발급은 구분한다. 이 설정 준비만으로 전환을 활성화하지 않는다.

11/10 root 읽기 전용 확인: 구형 gate paused=false, active/waiting=0; Client↔gate 키 불일치, Studio↔gate 키 일치. Studio coordination 키, owned hold, secure-precreated lock 및 journal 설정은 미설정이며 lock 경로는 기존 `/run/bc250-ai-studio/gpu.lock`이다. 실제 이전 시 다시 확인하고 역할 키·공유 파일 설정을 한 호환 세트로 적용해야 한다.

## 모델별 기능 기록 준비 — M5-01-03 / M6

기능 검증 기록은 선택적 `/opt/ai/config/capabilities/<관리이름>.json`이며 directory와 파일은 worker가 검사할 수 있도록 root 관리 경로여야 한다. [draft 생성기](../../scripts/draft_model_capabilities.py)는 현재 모델·설정·파일·llama runtime의 관찰값만 수집하고 두 기능을 unknown으로 작성한다. 전환과 서비스 재시작이 없는 구간에 생성하고, 실제 기능 시험 결과와 관찰된 동일 모델·설정·빌드가 맞는지 확인한 뒤 관리자만 검토·작성한다. 단순 초안이나 한 차례 응답을 근거로 전체 reasoning/schema 지원을 자동 승격하지 않는다. 설치·소유권·백업 및 교체 절차는 아직 M7에서 대상 서버에 맞춰 확정해야 한다.

기록이 없거나 stale이면 기능은 unknown이며 `enable_thinking`/JSON schema가 필요한 요청은 차단된다. 운영의 현재 기본 채팅도 `enable_thinking=false`를 사용하므로 각 대상 모델의 검증과 기록을 준비하기 전 활성화하면 대화할 수 없다. 모델별 기능시험 및 worker/gate 실제 설치 전에는 전환을 비활성으로 유지한다.

M5-02 candidate: protocol 3 gate는 입장 전 거절 409에 `X-BC250-Admission: rejected`를 제공하고 upstream의 동일 header를 제거한다. 이 후보 gate와 해당 Client를 함께 설치·검증해야 background의 안전한 대기/재시도가 완성된다. 현재 Client 자동 배포는 gate를 설치하지 않는다.

M5-03: 운영 benchmark.py/run-comparison.py는 아직 직접 8080과 서비스 제어를 사용한다. [검토용 후보](../../deploy/benchmark-candidate/README.md)의 원본 hash를 적용 직전 다시 대조하고 기존 파일을 백업한다. protocol 3 gate·Studio 공용 lock/journal·worker와 함께 두 후보를 설치하고 오래된 직접 경로가 남지 않는지 확인하기 전에는 전환을 활성화하지 않는다. 비교 중 다른 UI/CLI 전환을 실행하지 않는 단일 관리자 절차를 적용한다.

## M7-01 대상 서버 실행 순서와 점검 도구 — 2026-11-10

아래는 검토용 순서다. 이 문서 작성은 운영 설치나 전환 활성화가 아니며, M7-02에서 최종 commit·변경 세트·복구 가능성을 확인한 뒤 M8에서 실행한다. root 명령에서 `SRC`는 **해당 commit을 고정해 root가 검토한 후보 디렉터리**, `BACKUP`은 새 root 전용 백업 디렉터리다. 기존 `/etc` 환경 파일과 credential을 터미널·채팅·로그에 출력하지 않는다. 명령을 실행하는 동안 관리자는 Client 요청, Studio 이미지 작업, 벤치마크, 기존 CLI, 배포 및 다른 제어자의 접근을 중단한다. 아래 읽기는 그 차단을 대신하지 않는다.

### A. 현재 상태와 백업

1. `systemctl is-active`로 Client·Studio·gate·llama가 active인지, `systemctl show ... -p Job -p MainPID`로 진행 중 systemd 작업이 없는지 확인한다. `sudo /usr/bin/python3 "$SRC/scripts/studio_quiescence_status.py"`는 키·작업 내용을 출력하지 않고 전체 Studio 작업 페이지를 두 번 비교한다. `quiescent=true`가 아니거나 조회 오류면 중단한다. 현재 조회는 `nonterminal_jobs=0`, `quiescent=true`였으나 실행 직전 다시 조회해야 한다.
2. `sudo /usr/bin/python3 "$SRC/scripts/legacy_gate_status.py"`에서 active·waiting이 모두 0인지 확인한다. waiting이 있으면 기존 pause를 풀어 요청을 재전송하지 말고 배포를 중단해 연결 종료를 확인한다. 이미지 작업과 새 Client 요청의 차단은 관리자 절차로 별도 보장한다.
3. 다음 명령은 **새** root 전용 `BACKUP`을 만들고 unit·환경 파일, 실제 CLI와 symlink, benchmark, Client·Studio 코드/정적 파일, Studio 모델 설정과 현재 active-model 설정을 ACL·SELinux·소유권 정보와 함께 아카이브한다. 환경 파일이 포함되므로 archive 내용을 공개하지 않는다. 필요한 파일 누락이나 기존 `BACKUP`이 있으면 중단한다.

   ```bash
   sudo /usr/bin/python3 "$SRC/scripts/create_rollout_backup.py" "$BACKUP"
   ```

   unit enable 상태, 고정한 commit 및 gate/Studio/Client venv의 전체 의존성 버전은 별도 root 전용 기록으로 보관한다. 이 백업은 대화·Studio DB 또는 모델 파일의 백업이 아니며, 기존 gate/Studio venv는 교체하지 않고 보존한다. `active-model.conf`는 조사 기록으로 포함하지만 실행 중 임의 복원하지 않는다. 실제 파일이 없으면 임의로 생성하지 말고 미존재 사실을 기록한다.
4. 기존 CLI의 실제 실행 파일은 `/opt/ai/scripts/ai-model`, `/usr/local/bin/ai-model`은 그 symlink다. 양쪽 호출이 새 worker를 통하도록 실제 파일 경로를 후보 launcher로 교체하고 symlink를 유지해야 한다. `/opt/ai-benchmark` 두 원본 hash는 각각 `d44a3030911195d8a950542eb588cbe8e7955227b52abe8e1213170db28534d6`, `f7db2d20b5c2b16de38547202c510498dd23c004415c0b7a47a59b708dc208f1`였다. 적용 직전 다시 대조하고 차이가 있으면 후보를 재검토한다. Wiki는 직접 llama를 호출하는 명시적 예외다.

### B. 비밀·패키지·파일 준비

1. 비밀을 출력하지 않는 다음 명령은 **존재하지 않는** root 전용 staging 디렉터리를 만들 뿐 실제 설정을 교체하지 않는다. 기존 Studio API/admin 및 Client 운영자 키가 있으면 보존한다. 운영자 키가 없으면 새 키를 만들며, 관리자는 root 전용 staging의 `client.env`에서 이를 안전한 경로로 확인한다. 서버의 `/health`, `/v1/models`, `/slots`는 11/10 인증 없는 읽기가 가능했으나, 실제 upstream 인증 설정이 다르면 `--upstream-key-file`에 root 전용 기존 키 파일을 지정한다.

   ```bash
   sudo env PYTHONPATH="$SRC" /opt/bc250-llm-client/.venv/bin/python \
     "$SRC/scripts/prepare_coordination_credentials.py" /root/bc250-model-credentials-stage
   ```

   실패한 부분 staging은 원인을 조사하고 새 경로를 사용한다. 기존 staging 또는 credential을 덮어쓰지 않는다. `client.env`는 전환 비활성, 고유 Client hold, Client gate 키 및 운영자 키를 포함하고 `studio.env`는 Studio gate 키·조율 키와 공용 경로를 포함한다. 내용을 출력하지 않고 root 소유 `0600`으로 `/etc/bc250-model-control/`에 설치한다. gate와 worker는 같은 `switch-gate-key`, Studio와 worker는 같은 `studio-coordination-key`, gate와 각 소비자는 동일한 역할 키를 사용한다.
2. `python scripts/build_model_worker.py <새-lib-경로>`로 고정 의존성과 후보 worker를 별도 staging한 뒤 검증한다. `/opt/bc250-model-control`에 root 소유 bootstrap·CLI bootstrap·lib를 설치하며 실행 트리에는 symlink·`.pth`·`.pyc`가 없어야 한다. gate는 별도 `/opt/bc250-llm-gate`에서 `gate_candidate`와 Python 3.14 가상환경을 사용한다. Python venv를 다른 경로에서 복사해 이동하지 않는다. 서버의 별도 임시 venv에서 [고정 의존성](../../deploy/model-control/gate-requirements.txt) 전체와 [역할·hold·snapshot 시험](../../scripts/check_gate_candidate.py)을 통과했다. 최종 경로에 venv를 만들 때 같은 고정 의존성과 후보를 설치한 다음 import·시험 결과와 hash를 다시 확인한다.
3. [tmpfiles 후보](../../deploy/model-control/bc250-model-control.tmpfiles.conf)를 `/etc/tmpfiles.d/`에 설치하고 최초 실행 시 `/run/bc250-gpu`의 root 소유 lock `0660 root:ai-studio`, worker lock `0600 root:root`, socket 상위 디렉터리 `0755`를 만든다. 부팅 후에도 같은 tmpfiles 규칙을 사용한다. 기존 lock은 교체하거나 truncate하지 않는다. 별도의 임시 사용자 파일에서 tmpfiles를 두 번 적용해 inode·내용 유지 확인을 마쳤다. 실제 설치 후 worker·Studio 시작 전에 inode 및 그룹 접근을 확인한다.
4. `/var/lib/bc250-model-control`은 root 소유·Studio 읽기 그룹이 접근 가능한 `0750`, 새 journal은 `0640 root:ai-studio`, 새 DB는 worker가 root 전용으로 만든다. journal `{}`는 경로가 **정말 신규이고 기존 상태가 전혀 없을 때만** 배타적으로 만든다. 이미 존재하는 파일, 불명 journal, gate state/marker 또는 작업 DB를 초기화하지 않는다. 실제 state 경로의 SELinux 정책 라벨과 서비스 생성 임시 파일의 라벨·ACL 일치를 확인한다.
5. [Studio drop-in](../../deploy/model-control/bc250-ai-studio.service.d/model-control.conf)은 기존 서비스 설정을 유지하면서 root 전용 `studio.env`를 추가하고 `ProtectSystem=strict` 안에서 공용 lock 경로 쓰기를 허용한다. [Client drop-in](../../deploy/model-control/bc250-llm-client.service.d/model-control.conf)은 기존 인증 설정을 유지하면서 `client.env`를 추가한다. 두 파일을 같은 호환 세트로 적용하고 기존 unit·drop-in 백업을 유지한다. 대상 `model-control.conf`가 이미 있으면 덮어쓰지 않고 내용을 검토해 충돌을 해결한다. 아래 롤백의 신규 drop-in 삭제는 기존에 없음을 확인한 경우에만 적용한다.

M8에서 실제 신규 경로를 준비할 때 `WORKER_LIB`는 검증된 새 worker lib staging, `BUNDLE`은 위에서 만든 credential staging이다. **모든 대상이 미존재인 초기 설치**에서만 다음을 순서대로 실행한다. 하나라도 이미 있거나 이전 설치 상태가 불명확하면 아래 블록을 실행하지 않고 조사한다.

```bash
( set -eu
sudo test ! -e /opt/bc250-model-control
sudo test ! -e /opt/bc250-llm-gate
sudo test ! -e /etc/bc250-model-control
sudo test ! -e /var/lib/bc250-model-control
sudo test ! -e /var/lib/bc250-llm-gate
sudo test ! -e /run/bc250-gpu
sudo test ! -e /run/bc250-model-control
sudo test ! -e /etc/tmpfiles.d/bc250-model-control.conf
for path in /opt/bc250-model-control /opt/bc250-llm-gate /etc/bc250-model-control /var/lib/bc250-model-control /var/lib/bc250-llm-gate /run/bc250-gpu /run/bc250-model-control /etc/tmpfiles.d/bc250-model-control.conf; do
  sudo test ! -L "$path"
done
sudo install -d -m 0755 -o root -g root /opt/bc250-model-control /opt/bc250-llm-gate
sudo cp -a "$WORKER_LIB" /opt/bc250-model-control/lib
sudo chown -R root:root /opt/bc250-model-control/lib
sudo chmod -R go-w /opt/bc250-model-control/lib
sudo install -m 0644 -o root -g root "$SRC/deploy/model-control/worker-bootstrap.py" /opt/bc250-model-control/worker-bootstrap.py
sudo install -m 0644 -o root -g root "$SRC/deploy/model-control/cli-bootstrap.py" /opt/bc250-model-control/cli-bootstrap.py
sudo cp -a "$SRC/gate_candidate" /opt/bc250-llm-gate/gate_candidate
sudo chown -R root:root /opt/bc250-llm-gate/gate_candidate
sudo /usr/bin/python3 -m venv /opt/bc250-llm-gate/.venv
sudo /opt/bc250-llm-gate/.venv/bin/python -m pip install -r "$SRC/deploy/model-control/gate-requirements.txt"
sudo env PYTHONPATH=/opt/bc250-llm-gate /opt/bc250-llm-gate/.venv/bin/python "$SRC/scripts/check_gate_candidate.py"
sudo install -d -m 0700 -o root -g root /etc/bc250-model-control
for name in gate-admin-key gate-client-key gate-studio-key switch-gate-key studio-coordination-key llm-key client.env studio.env; do
  sudo install -m 0600 -o root -g root "$BUNDLE/$name" "/etc/bc250-model-control/$name"
done
sudo install -d -m 0750 -o root -g ai-studio /var/lib/bc250-model-control
sudo install -d -m 0700 -o ai-studio -g ai-studio /var/lib/bc250-llm-gate
sudo install -m 0644 -o root -g root "$SRC/deploy/model-control/bc250-model-control.tmpfiles.conf" /etc/tmpfiles.d/bc250-model-control.conf
sudo systemd-tmpfiles --create /etc/tmpfiles.d/bc250-model-control.conf
)
```

위 블록은 journal·gate state·DB를 생성하지 않는다. journal은 다음 배타 생성 명령으로 한 번만 준비하며, 실패하면 기존 파일을 지우거나 덮어쓰지 않는다. `restorecon`은 **방금 새로 만든** 디렉터리에만 적용한다.

```bash
sudo /usr/bin/python3 - <<'PY'
import grp, os
p = '/var/lib/bc250-model-control/switch.json'
fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o640)
try:
    os.fchown(fd, 0, grp.getgrnam('ai-studio').gr_gid)
    os.fchmod(fd, 0o640)
    os.write(fd, b'{}')
    os.fsync(fd)
finally:
    os.close(fd)
directory = os.open('/var/lib/bc250-model-control', os.O_RDONLY | os.O_DIRECTORY)
try: os.fsync(directory)
finally: os.close(directory)
PY
sudo restorecon -RF /var/lib/bc250-model-control /var/lib/bc250-llm-gate /run/bc250-gpu
```

이어서 worker service/socket 후보와 두 drop-in을 해당 `/etc/systemd/system/` 경로에 설치한다. 기존 gate unit과 실제 CLI·benchmark는 백업 후 §C의 제어된 중단 구간에서만 교체한다. 새 drop-in·tmpfiles·unit 대상이 이미 있으면 임의로 덮어쓰지 않는다. `systemctl daemon-reload`, socket enable 및 서비스 시작은 §C 순서를 따른다. 부분 실패 시 임의 재실행하지 않고 생성된 파일과 기록을 보존해 조사한다.

### C. gate 이전과 비활성 설치

아래 명령은 M8에서 ingress와 다른 제어자를 중단한 관리자가 실행한다. snapshot 이후 구형 gate stop까지 다른 제어를 허용하지 않는다. snapshot은 순간 관찰이며 이것만으로 경합을 방지하지 않는다.

```bash
( set -eu
# BACKUP은 새 root 전용 디렉터리, SRC는 고정된 후보 소스다.
sudo env PYTHONPATH="$SRC" /opt/bc250-llm-client/.venv/bin/python \
  "$SRC/scripts/migrate_legacy_gate.py" capture "$BACKUP/legacy-gate.json"
# capture가 성공한 직후에만 관리자가 기존 gate를 정지한다.
sudo systemctl stop bc250-llm-gate.service
# /var/lib/bc250-llm-gate는 미리 준비한 ai-studio 소유의 신규 0700 디렉터리다.
sudo env PYTHONPATH="$SRC" /opt/bc250-llm-client/.venv/bin/python \
  "$SRC/scripts/migrate_legacy_gate.py" initialize "$BACKUP/legacy-gate.json" /var/lib/bc250-llm-gate/state.json
)
```

capture는 인증된 구형 gate의 active/waiting이 모두 0일 때만 root 전용 snapshot을 배타적으로 생성한다. initialize는 gate가 정지·정착했는지 전후로 확인하고 기존 state·lock·uncertainty marker가 없을 때만 초기 `startup`과 원래 pause에 해당하는 `legacy`를 생성한다. 중단 또는 불명 오류 시 생성된 기록을 삭제하거나 재초기화하지 말고 조사한다.

초기 state 생성이 성공한 뒤 **신규 state 파일에만** 정책 라벨을 적용하고, 백업한 기존 gate unit 대신 protocol 3 unit 후보를 설치해 시작한다. 새 worker unit/socket과 두 신규 drop-in의 대상이 미존재인지 확인한다. 아래는 전체 블록이 한 항목이라도 실패하면 중단하는 예시다. Client·Studio 요청과 다른 제어자는 계속 차단하고, 각 재시작 전에 이 문서의 유휴 조건을 다시 확인한다.

```bash
( set -eu
sudo restorecon -F /var/lib/bc250-llm-gate/state.json
for path in /etc/systemd/system/bc250-model-worker.service /etc/systemd/system/bc250-model-worker.socket /etc/systemd/system/bc250-ai-studio.service.d/model-control.conf /etc/systemd/system/bc250-llm-client.service.d/model-control.conf; do
  sudo test ! -e "$path"
  sudo test ! -L "$path"
done
sudo install -m 0644 -o root -g root "$SRC/deploy/model-control/bc250-llm-gate-protocol3.service" /etc/systemd/system/bc250-llm-gate.service
sudo install -m 0644 -o root -g root "$SRC/deploy/model-control/bc250-model-worker.service" /etc/systemd/system/bc250-model-worker.service
sudo install -m 0644 -o root -g root "$SRC/deploy/model-control/bc250-model-worker.socket" /etc/systemd/system/bc250-model-worker.socket
sudo mkdir -p /etc/systemd/system/bc250-ai-studio.service.d /etc/systemd/system/bc250-llm-client.service.d
sudo install -m 0644 -o root -g root "$SRC/deploy/model-control/bc250-ai-studio.service.d/model-control.conf" /etc/systemd/system/bc250-ai-studio.service.d/model-control.conf
sudo install -m 0644 -o root -g root "$SRC/deploy/model-control/bc250-llm-client.service.d/model-control.conf" /etc/systemd/system/bc250-llm-client.service.d/model-control.conf
sudo systemctl daemon-reload
sudo systemctl start bc250-llm-gate.service
sudo systemctl is-active --quiet bc250-llm-gate.service
sudo systemctl restart bc250-ai-studio.service
sudo systemctl is-active --quiet bc250-ai-studio.service
sudo systemctl restart bc250-llm-client.service
sudo systemctl is-active --quiet bc250-llm-client.service
sudo systemctl enable --now bc250-model-worker.socket
)
```

`startup` hold가 유지되고 worker socket/service는 `MODEL_CONTROL_SWITCH_ENABLED=false`, Client는 `MODEL_CONTROL_ENABLED=false`인 상태를 확인한다. 백업과 원본 hash를 다시 대조한 뒤 기존 CLI 실제 파일과 두 benchmark 후보를 함께 전환한다.

```bash
( set -eu
sudo install -m 0755 -o root -g root "$SRC/deploy/model-control/ai-model" /opt/ai/scripts/ai-model
sudo install -m 0755 -o root -g root "$SRC/deploy/benchmark-candidate/benchmark.py" /opt/ai-benchmark/benchmark.py
sudo install -m 0755 -o root -g root "$SRC/deploy/benchmark-candidate/run-comparison.py" /opt/ai-benchmark/run-comparison.py
test "$(readlink /usr/local/bin/ai-model)" = /opt/ai/scripts/ai-model
)
```

부분 실패 시 다른 직접 제어 경로를 열지 않고 기록과 hold를 보존한다. launcher 교체와 benchmark 교체 사이는 다른 사용자가 실행하지 않는 단일 관리자 중단 구간이어야 한다.

`sudo env PYTHONPATH="$SRC" /opt/bc250-llm-client/.venv/bin/python "$SRC/scripts/coordination_status.py"`는 gate 네 역할 인증, Client 실행 환경의 역할 키·hold·운영자 키, Studio coordination 및 Studio 유휴 상태를 값 노출 없이 확인한다. `holds`에 `startup`과 원래 `legacy`가 예상대로 있는지 확인하고 파일 소유권·SELinux, 두 프로세스 namespace의 inode 및 worker `inspect-recovery` 결과를 별도로 확인한다. 이 정적 검사는 실제 GPU 전환 증거가 아니다.

M7-02는 운영 설치 전 후보·절차와 남은 운영 확인 조건을 판정한다. M8에서 비활성 설치와 정적 확인을 끝내고 관리자가 ingress 통제를 유지한 상태에서만 `sudo env PYTHONPATH="$SRC" /opt/bc250-llm-client/.venv/bin/python "$SRC/scripts/release_startup_hold.py"`를 실행한다. 이 도구는 사전 상태를 재확인하고 startup만 해제한 뒤 다른 hold가 그대로인지 확인한다. 결과 불명 시 자동 재시도하지 않고 재조회한다. `legacy`가 남으면 별도 관리자의 원래 pause로 보존한다. 실제 worker 전체 preflight는 startup 해제 후 M8에서 확인한다. 기능별 실제 모델 검증 전에는 Client 모델 전환 UI를 활성화하지 않는다.

### C-1. 실제 모델 기능 확인 후 활성화

이 단계는 비활성 설치 후 gate·Studio·worker의 실제 계약과 복구 검사를 통과한 뒤에만 실행한다. 현재 로드하지 않은 모델의 실제 context와 기능을 확인하려면 공용 worker로 해당 모델을 먼저 로드해야 한다. 따라서 M8에서 먼저 worker만 활성화해 관리자가 공용 CLI로 각 모델을 제한적으로 검증하고, Client의 모델 전환 UI는 그동안 비활성으로 유지한다. 각 모델의 실제 context 및 필요한 기능 기록을 확인한 후에만 Client를 활성화한다. 기본 대화의 `enable_thinking=false`도 모델별 reasoning 제어 기록이 필요하다. `LLM_MODEL_ID`가 비어 있는지, 작업·journal·예약이 미해결이 아닌지, 새 요청·다른 CLI·배포가 중단됐는지 다시 확인한다.

Client는 기존 `client.env`의 `MODEL_CONTROL_ENABLED=false`를 나중에 읽는 별도 EnvironmentFile로 재정의한다. systemd의 EnvironmentFile은 Environment보다 우선하며 drop-in 이름순으로 읽히므로 `zz-model-control-enable.conf`와 `client-activation.env`를 함께 사용한다. worker는 별도 drop-in의 `MODEL_CONTROL_SWITCH_ENABLED=true`로 활성화한다. 아래는 각 대상이 이전에 없음을 확인한 초기 설치 예시다.

```bash
( set -eu
for path in /etc/bc250-model-control/client-activation.env /etc/systemd/system/bc250-model-worker.service.d/zz-switch-enable.conf /etc/systemd/system/bc250-llm-client.service.d/zz-model-control-enable.conf; do
  sudo test ! -e "$path"
  sudo test ! -L "$path"
done
sudo mkdir -p /etc/systemd/system/bc250-model-worker.service.d
sudo install -m 0644 -o root -g root "$SRC/deploy/model-control/bc250-model-worker.service.d/zz-switch-enable.conf" /etc/systemd/system/bc250-model-worker.service.d/zz-switch-enable.conf
sudo systemctl daemon-reload
sudo systemctl restart bc250-model-worker.service
sudo systemctl is-active --quiet bc250-model-worker.service
)
```

이제 관리자만 공용 CLI로 각 대상 모델의 로드·slot context·필요한 reasoning/JSON schema 기능을 확인하고, 결과와 정확한 모델·설정·runtime에 결부된 기록을 준비한다. 이 기간 Client 모델 전환 UI와 다른 제어자는 비활성 상태를 유지한다. 실패나 결과 불명 시 조사·복구하고 다음 모델로 넘어가지 않는다. 모델별 검증이 완료되고 현재 모델도 준비된 뒤에만 다음 Client 활성화 단계를 진행한다.

```bash
( set -eu
for path in /etc/bc250-model-control/client-activation.env /etc/systemd/system/bc250-llm-client.service.d/zz-model-control-enable.conf; do
  sudo test ! -e "$path"
  sudo test ! -L "$path"
done
sudo install -m 0600 -o root -g root "$SRC/deploy/model-control/client-activation.env" /etc/bc250-model-control/client-activation.env
sudo install -m 0644 -o root -g root "$SRC/deploy/model-control/bc250-llm-client.service.d/zz-model-control-enable.conf" /etc/systemd/system/bc250-llm-client.service.d/zz-model-control-enable.conf
sudo systemctl daemon-reload
sudo systemctl restart bc250-llm-client.service
sudo systemctl is-active --quiet bc250-llm-client.service
)
```

적용 후 두 프로세스의 유효 설정과 worker·Client의 모델/작업 조회를 비밀값 없이 확인하고 M8의 실제 UI 전환과 후속 대화를 검증한다. 중간 실패나 전환 결과 불명 시 새 전환을 중단하고 기록·hold를 보존한다. 비활성화나 롤백은 실행 중·불명 작업이 없음을 확인한 후 Client 활성화 drop-in부터 해제하고, 그다음 worker 활성화 drop-in을 해제한다. 미해결 작업이 있으면 설정 변경·서비스 재시작 대신 복구 경로를 따른다.

### D. 불명 상태와 롤백

`sudo ai-model inspect-recovery`의 operation·journal·hold 정보를 조사한다. journal과 상관관계가 일치하는 경우에만 `recover`, journal이 비었고 독립 hold 등 검증 조건이 충족된 경우에만 `resolve-verified`를 사용한다. 응답 유실 시 새 전환이나 반복 restart 대신 같은 작업 ID를 조회한다.

롤백 직전 `sudo env PYTHONPATH="$SRC" /opt/bc250-llm-client/.venv/bin/python "$SRC/scripts/rollback_readiness.py"`는 gate가 legacy pause로 표현 가능한 상태인지, worker에 미해결 작업·journal이 없는지, Studio가 유휴이고 llama가 정착했는지 읽기 전용으로 확인한다. 이 결과도 다른 제어자를 막지 않으므로 관리자 차단을 유지한다. 하나라도 불명·실패이거나 `admin-*`/Studio/Client/switch 등 구형 gate로 옮길 수 없는 hold가 있으면 중단하고 해제하지 않는다.

통과 시 ingress와 다른 제어자를 계속 차단하고 worker socket과 Client·Studio·gate 서비스를 정지한다. 기존에 없음을 확인하고 이번에 새로 만든 활성화 drop-in과 두 `model-control.conf` drop-in만 제거한다. 나머지 기존 drop-in은 건드리지 않는다. 아래는 root 전용 archive에서 **M7에서 직접 바꾼 기존 파일만** 복원하는 예시다. archive의 전체 풀기나 active-model 설정의 무조건 복원은 하지 않는다. 실행 전 대상 파일·백업을 대조한다.

```bash
sudo tar --same-owner --same-permissions --acls --xattrs --selinux -C / \
  -xf "$BACKUP/prechange.tar" -- \
  etc/systemd/system/bc250-llm-gate.service \
  etc/bc250-llm-gate.env etc/bc250-ai-studio.env etc/bc250-llm-client.env \
  opt/ai/scripts/ai-model usr/local/bin/ai-model \
  opt/ai-benchmark/benchmark.py opt/ai-benchmark/run-comparison.py
sudo systemctl daemon-reload
```

기존 unit enable 상태와 각 파일의 소유권·mode·ACL·SELinux 및 CLI symlink를 백업 기록과 대조한다. 새 gate·worker 상태와 journal은 조사 기록으로 보존하고 자동 초기화하거나 삭제하지 않는다. 구형 gate는 시작 시 unpaused이므로 `legacy_pause_to_restore=true`였으면 **ingress를 계속 차단한 상태**에서 구형 gate를 시작하고 `sudo /usr/bin/python3 "$SRC/scripts/restore_legacy_pause.py"`로 기존 관리자 pause를 복원·재조회한다. 결과 불명이면 ingress를 다시 열지 않는다. `false`인 경우에도 구형 gate의 인증된 상태와 활성 모델·Studio·Client를 확인한 후에만 ingress를 연다. 미해결 기록을 지우거나 타 작업 hold를 임의 해제하지 않는다.
