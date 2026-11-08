# 모델 전환 공용 조율 계약 — M2-02

기준: 2026-11-07 M1 운영 조사와 로컬 후보 코드. 이 문서는 구현·배포해야 할 계약이며 현재 운영에서 이미 제공된다는 뜻이 아니다. [통합 계획 §7](llm-model-switch-plan.md#7-실행-체크리스트와-완료-조건)이 단계 상태의 기준이다. 실제 활성화는 M4~M8의 검증과 운영 절차가 선행된다.

## 1. 현재 차이와 소유 경계

사용자는 운영자 화면의 기존 `/llm` 주소·origin을 유지하고, 같은 origin의 관련 자산과 배포 권한을 함께 보호하는 방식을 선택했다. 독립 Wiki의 **모델 전환 조율·API/UI 기능 통합**은 여전히 후속 작업이지만, 같은 origin에서 운영자 인증을 보호하는 데 필요한 Wiki 경로의 자산·응답·배포 신뢰 경계는 예외가 아니다. Studio의 독립 배포를 유지하면서 해당 자산의 신뢰성을 보장해야 한다.

| 항목 | 운영 확인값 | 목표 계약 |
| --- | --- | --- |
| gate | `ai-studio` 계정, 단일 paused bool, owned hold 미지원 | 별도 `ai-gate` 계정과 Studio 배포에서 독립된 root 소유 gate 설치본. durable owned hold와 epoch admission을 명시한 protocol 3 |
| Studio GPU 잠금 | `/run/bc250-ai-studio/gpu.lock`; 런타임 디렉터리 `ai-studio` 소유 | Studio와 worker가 `/run/bc250-gpu/gpu.lock`의 동일 inode를 잠금. root 소유의 신뢰 가능한 경로와 사전 생성 파일 사용 |
| 전환 journal | 운영 Studio 설정에 없음 | `/var/lib/bc250-model-control/switch.json`, worker가 root 소유로 관리하고 Studio는 필요한 읽기 권한만 가짐 |
| gate 키 | 운영 Client 키는 gate와 불일치해 `/control` 401, Studio와 gate 키는 일치 | 현재 운영 정정은 별도 절차. 새 구조는 gate admin·Studio·switch 키 및 Studio coordination 키를 분리 |
| 직접 호출 | Wiki와 벤치마크가 8080 직접 사용 | Wiki는 사용자 지정 예외. 벤치마크 추론은 gate로 옮기고 직접 service 제어를 제거·검증 |

Worker의 기존 preflight가 gate 계정 `ai-gate`를 요구한다는 사실을 근거로 운영 서비스가 이미 그 계정이라고 가정하지 않는다. 계정·설치·권한 전환은 M4/M7에서 템플릿과 운영 절차로 구현하고 Linux에서 검증한다. 별도 gate 설치본 및 배포 절차는 Studio 코드·venv를 실행 경로로 공유하지 않아 Studio의 독립 배포를 유지해야 한다. 기존 Studio의 안전한 작업 방식은 변경 전까지 유지한다.

## 2. 역할·인증과 제어 인터페이스

- 브라우저→Client는 기존 앱 인증과 별도 운영자 키를 사용한다. local 모드는 운영자 인증이 아니다. Client→worker는 Unix socket의 실제 peer UID를 확인한다. root와 지정 Client 서비스 UID만 허용하며 Client에 sudo를 주지 않는다. 현재 운영 Client는 대화형 SSH 계정 `yeppyshiba`와 UID 1000을 공유하므로 UID 확인만으로 이 계정의 직접 socket 요청을 웹 운영자 인증과 구분할 수 없다. 전환 활성화 전 Client 서비스를 별도의 비대화형 `ai-client` UID로 분리하고 socket·기존 DB/파일 권한 이관과 CLI root 접근을 M4-02/M7에서 검증한다. Client의 실행 코드·의존성뿐 아니라 운영자 UI 자산과 유효 Caddy route도 일반 계정이 수정할 수 없어야 한다. 이를 검증하지 못하는 현재 preflight는 무조건 전환을 거절한다. 이 계정 변경을 단순히 가정하거나 운영에 먼저 적용하지 않는다.
- gate의 admin, Client, Studio, switch credential은 서로 다른 고유 비밀이다. 운영자의 legacy pause는 admin 역할만 제어하고, Client·Studio·worker는 각각 `client-<32hex>`, `studio-<32hex>`, `switch-<32hex>` 자신의 hold만 획득·해제한다. Client와 worker는 legacy·startup 또는 다른 주체의 hold를 해제하지 않는다. 기존 Client gate-control은 Client 소유 hold 범위로 바꾸며 운영자의 독립 pause는 root 관리 절차에서 다룬다. Studio coordination endpoint는 gate의 모든 키와 다른 별도 자격을 쓴다.
- Client gate-control의 hold ID는 신뢰된 Client 서비스 설정 또는 영속 상태에서 관리한다. 프로세스 재시작과 요청 응답 유실 뒤에도 같은 ID를 추적해야 하며 브라우저가 임의 ID를 지정해 다른 hold를 해제하도록 하지 않는다. 활성화 전에 기존 control 호출자·응답 계약과 회복 흐름을 M5/M6에서 검증한다.
- 기존 origin을 유지하므로 운영자 UI가 읽는 Client 자산뿐 아니라 같은 origin의 Studio·Wiki 등에서 실행될 수 있는 자산·응답과 이를 배포하는 경로도 검토한다. 실효 Caddy 설정·관리 인터페이스와 프런트 자산의 provenance, 실행 서비스·출력 경계가 신뢰할 수 없는 상태에서는 전환을 활성화하지 않는다. 이는 독립 Wiki의 모델 호출을 gate로 옮기는 작업과 구분한다.
- M4-04에서 완성할 검증기는 root 관리 manifest에 고정한 origin/listener, 전체 실효 HTTP 설정, 프로젝트별 정적 자산·backend 실행 경로 및 revision을 실제 Caddy 프로세스·namespace·파일과 대조해야 한다. 미등록 route·upstream·파일, 소유권/권한 불일치, 해시 불일치, 불명 설정 또는 관리 인터페이스 접근 권한 미확인은 모두 차단한다. Caddy 설정은 실제 적용 상태를 읽고, 단순 Caddyfile 문자열만으로 판정하지 않는다. 검사 전후 Caddy 프로세스·설정·manifest가 바뀌지 않았는지도 확인한다.
- manifest는 untrusted SSH 사용자가 임의 내용을 root-owned 파일로 승격하는 통로가 되어서는 안 된다. M7에서 root가 검토한 기준 또는 별도 서명 권한의 릴리스만 허용하는 제한 승격 절차를 준비한다. Studio는 자기 산출물만 독립 승격할 수 있어야 하며 Client·gate 전체 재배포나 DB fence를 요구하지 않는다. Caddy 관리 인터페이스는 검증 외 임의 변경을 허용하는 일반 사용자 경로가 없도록 제한하고 실제 접근 경계를 검증한다.
- 정적 파일 해시만으로 동적 HTML·업로드·저장된 스크립트·service worker의 same-origin 영향을 증명할 수 없다. Wiki 기능 통합 예외를 유지하되 같은 origin의 각 동적 응답·업로드를 분류하고, 신뢰하지 않는 활성 콘텐츠의 inline 제공·script 실행·상위 service-worker scope 등을 막는 정책과 검증 근거를 별도로 마련한다. 알 수 없는 동적 경로를 안전하다고 가정하지 않는다.
- 검증기 지원 범위는 root 관리 정적 자산, 신뢰된 backend의 `application/json` API, attachment 다운로드, MIME이 제한된 이미지 응답으로 좁히고 각 경로의 `nosniff` 등 응답 정책을 강제·시험한다. HTML/SVG/JavaScript 업로드의 inline 제공과 검증되지 않은 동적 HTML 렌더러·알 수 없는 handler/upstream은 거절한다. 정적 파일 해시와 backend 코드의 신뢰성은 XSS 부재 자체를 증명하지 않으므로 렌더·업로드·service-worker 범위에 대한 별도 보안 검토와 실제 응답 회귀를 요구한다.
- 읽기 전용 실효 설정 조회도 신뢰할 수 있는 관리 채널을 사용한다. localhost TCP로 열린 Caddy 관리 endpoint를 안전하다고 가정하지 않으며, root/전용 서비스에만 허용된 Unix socket 등으로 접근 권한·실제 Caddy peer PID/UID와 설정을 검증한다. 현재 운영의 TCP 2019는 GET 응답만 확인했으며 쓰기 권한을 시험하거나 차단하지 않았다. 검증기와 M7 배포 절차는 확인 전후 설정·프로세스 변경과 다른 관리 endpoint의 우회를 거절해야 한다.
- worker는 switch credential로 `/control/identity`의 `{protocol: 3, role: switch}`와 `/control`의 durable hold 상태 및 32자리 소문자 hex epoch를 확인한다. protocol 3은 소유 hold뿐 아니라 §3의 epoch admission도 필수로 제공함을 뜻한다. 기존 protocol 2 후보는 epoch를 제공하지 않으므로 그 번호만으로 활성화를 허용하지 않는다. Studio coordination endpoint는 protocol 1의 `enabled=true`, `transition_clear=true`를 반환해야 하며 Studio의 gate identity 검증도 protocol 3에 맞춘다. 미설치·구형 protocol·인증 오류·불명 응답은 변경 전에 거절한다. 단일 bool로 되돌아가는 fallback은 없다.
- gate는 영속 상태를 원자적으로 기록하고 성공 응답보다 먼저 보류를 보존한다. `legacy`, `startup`, 관리자 hold와 독립 Studio·switch hold를 분리하며 기존 관리자 pause를 타 작업 종료 시 해제하지 않는다. gate 재시작은 startup hold를 더하고 기존 hold를 보존한다. 조사·복구 확인 후 권한 있는 관리 절차만 startup hold를 해제한다.
- credential은 root가 관리하는 제한된 파일 또는 systemd credential로 각 서비스에 전달한다. UI, 명령줄 인자, 로그, journal, 작업 DB에 평문을 기록하지 않는다. 현재 Client↔gate 키 불일치의 운영 수정은 새 역할 credential 발급·전환과 구분해 계획하고 변경 전후 인증 확인 및 롤백 절차를 M7에 둔다.

## 3. 추론 admission과 오래된 요청

gate는 요청 admission과 hold 획득을 같은 동기화 경계에서 직렬화한다. 이미 입장한 추론은 active로 집계하며 worker는 hold 획득 뒤 이 요청과 llama 슬롯이 비워질 때까지 기다린다. hold 중 새 추론은 대기시켰다 재전송하지 않고 `409 model_switch_in_progress`로 즉시 거절한다. Client는 입력 초안을 보존하고 전환 후 실제 모델을 다시 조회해 사용자가 다시 제출할 수 있게 한다. 응답을 일부 전달한 요청은 자동 재실행하지 않는다.

hold 기간에만 거절하면 **hold 이전에 모델을 조회하고 hold 해제 후 오래된 ID로 진입하는 경합**을 막지 못한다. 따라서 모델 snapshot에는 전환 세대(epoch)를 포함하고, 범위 내 추론 요청에는 조회한 epoch가 필수다. gate는 같은 동기화 경계에서 hold 부재·요청 epoch 일치 검사와 active 증가를 원자적으로 수행한다. switch hold 획득 및 gate 재시작 때 epoch를 변경해 이전 snapshot의 요청을 거절하며 프로세스 재시작으로 세대가 우연히 재사용되지 않게 한다. epoch 부재·불일치는 재조회가 필요한 명시적 오류이며 자동 재전송하지 않는다. 기존 deployment hold의 generation과 모델 epoch는 별개다. 범위 내 호출자는 전환 활성화 전 이 계약으로 옮긴다.

gate 프로세스 교체 시 먼저 유입·제어 쓰기를 제한하고 기존 active 요청을 0으로 배출한다. waiting도 0이면 정상적으로 교체한다. 기존 관리자 pause 때문에 waiting이 남았다면 pause를 풀어 원래 요청을 재전송하지 않는다. pause snapshot을 영속 보존한 다음 구형 gate를 종료해 그 대기 연결을 실패로 종료하고 새 gate의 startup/legacy hold로 이어간다. 이 분기에서 active 요청이 이미 끝났고 남은 연결이 실패로 끝나는지 확인할 수 없다면 교체하지 않는다. 모델 조회는 추론 대기열과 분리해 hold 중 `ready=false`와 현재 epoch를 제공한다. 모델 ID와 epoch는 같은 세대의 snapshot으로 확인해야 하며, 조회 중 전환이 끼면 결과를 폐기한다. 작업자의 모델 검증은 gate를 우회한 loopback llama health·model ID를 사용한다. 기존 `/v1/models`와 호환되는 snapshot 전달 경로·오류 형식은 M5-02에서 Client와 gate를 함께 구현하고 통합 시험한다.

현재 `gate_candidate.py`의 `Admission.enter()`는 pause 중 대기 후 원래 요청을 전달하므로 위 계약을 **아직 충족하지 않는다**. worker 후보 preflight는 protocol 3을 요구하지만 운영 gate와 기존 후보는 이를 제공하지 않는다. M4-02/M5-02에서 gate·Studio·worker·Client를 함께 변경·검증하기 전 운영 전환 기능을 활성화하지 않는다.

## 4. 공유 잠금·journal과 복구

Studio와 worker는 동일한 root 관리 GPU lock의 열린 FD에 flock을 취득한다. lock은 root 소유·공유 그룹 `0660`, journal은 root 소유·Studio 읽기 그룹 `0640`, 작업 DB는 root 전용을 기본으로 한다. root 소유 디렉터리, symlink/hardlink·쓰기 권한, ACL·SELinux 속성과 Studio 읽기·잠금 권한을 검증한다. worker는 잠금을 보유한 상태에서 Studio가 `/proc/PID/root`를 통해 보는 lock·journal의 dev/inode를 자기 관점 및 lock FD와 비교한다. Studio/gate의 계정, 활성·정착 상태, PID와 시작 시각을 전후로 확인하고 불일치·권한 부족·서비스 재시작 시 fail-closed한다. namespace 번호가 달라도 같은 inode를 보는지는 실제 비교가 필요하다. lock inode는 실행 중 교체하지 않으며 journal은 원자 교체되므로 inode를 영구 고정값으로 취급하지 않는다.

Studio는 공유 잠금 안에서 journal을 읽고 미해결·손상·부재·읽기 실패 시 이미지 GPU 작업을 거절한다. journal은 최대 8192바이트, root 소유 regular file 및 제한된 권한을 요구한다. 빈 정상 상태는 `{}`이며 활성 기록은 `hold_id`, `old`, `target`, `stage`, `mutation_started`와 선택적 32자리 `operation_id`만 포함한다. `stage`는 `pausing`, `draining`, `switching`, `verifying`만 허용한다. 작업 DB의 `rolling_back`이나 `recovery_required`를 journal stage로 기록하지 않는다. 원자 교체 시 소유권·mode·ACL·SELinux를 보존하고 fsync한다.

worker는 단일 영속 작업 접수→GPU lock→실제 계약 preflight→이전 모델 확인→journal→자기 hold→drain→설정 원자 교체·고정 서비스 재시작→health와 모델 ID 확인 순서로 진행한다. restart 결과가 불명이면 겹치는 restart와 보류 해제를 금지한다. 실패 시 이전 모델 설정을 복원하고 이전 모델의 health·ID까지 검증한다. 검증 불능이면 journal/hold 및 `recovery_required`를 유지한다.

성공·복구 정리 중 어느 지점에서 중단돼도 작업 DB·journal·gate hold가 모순되면 성공이나 안전한 해제로 추정하지 않는다. worker 재시작은 서로 대조하고 미해결이면 새 전환을 차단한다. Studio도 journal과 gate의 독립 hold를 확인해 GPU 작업을 차단해야 한다. 현재 후보는 모델 검증 후 GPU 잠금 안에서 journal을 먼저 비우고 자기 hold를 해제한다. journal 교체 후 fsync가 실패하면 hold를 유지하고 재시작 없이 복구 필요로 둔다. hold 해제 응답이 불명이면 다시 획득을 시도하되 재획득 실패 가능성도 불명으로 취급한다. 후보 DB에 journal 기록 및 hold 획득 전 상관 hold ID를 영속 기록하지만, 빈 journal과 잔존 hold를 안전하게 복구하는 경로는 아직 완성되지 않았다. root 전용 조사·복구 진입에서 파일 삭제나 임의 hold 해제로 해결하지 않는다. 정리의 모든 중단 지점을 M6에서 동일 최종 후보로 시험한다.

## 5. 범위 내 다른 호출자

`benchmark.py`는 직접 8080 대신 gate의 모델 snapshot·epoch를 사용하고 전환 중 거절에 맞춰 모델을 다시 조회해야 한다. `run-comparison.py`의 직접 `systemctl start`는 제거하고 전환·복구를 공용 CLI로 보낸다. 360초 timeout이나 CLI 연결 종료는 서버 작업 취소를 의미하지 않으므로 출력된 중복 방지 키·작업 ID로 결과를 확인하고 불명 상태에서 새 요청이나 restart를 하지 않는다. 실제 도구 변경과 테스트는 M5-03/M6에 속한다. 전환과 벤치마크의 안전한 조율이 준비되기 전 동시 실행을 허용된 운영 흐름으로 취급하지 않는다.

독립 Wiki는 사용자가 명시한 이번 범위의 예외다. Wiki의 8080 요청은 gate drain으로 차단·보호되지 않으며 전환 시 중단·실패할 수 있다. Wiki API/UI의 Client 통합은 후속 계획이고 M2-02의 완료 조건이 아니다. Client 내부 Wiki 메모리 생성은 다른 Client 추론 경로와 함께 M5에 포함한다.

## 6. 설치 전환과 복구 설계

M7은 실제 파일·unit·credential·접근 권한, 백업 및 롤백을 포함한 실행 가능한 절차를 작성한다. 기존 운영을 자동 변경하지 않는다. 우선 격리 환경에서 새 gate·Studio 계약과 worker를 시험하고, 운영 준비 단계에서 모든 범위 내 제어자 및 새 요청 유입을 제한해 기존 gate의 active가 0이고 Studio GPU 작업·복구가 없음을 확인한다. waiting이 0이 아니면 §3의 구형 대기 연결 실패 종료 분기를 사용한다. 기존 관리자 pause의 상태를 영속 기록하고 동일한 `legacy` hold로 옮기며, 이 전환 구간에 다른 관리자 제어가 개입하지 못하도록 한다. 확인할 수 없으면 중단한다.

새 gate는 startup hold가 있는 상태로 설치·시작한다. Studio의 기존 이미지 기능 및 독립 배포를 유지하면서 canonical lock·journal·역할별 credential을 적용하고 양쪽 inode·인증·복구 상태를 확인한다. worker는 전환 비활성으로 설치하고 CLI 백업 및 단일 경로 전환을 검토한다. startup hold가 있을 때는 인증·파일 등 정적 준비만 확인하며, 현 worker의 admission 검사는 startup hold를 거절하므로 이를 통과했다고 주장하지 않는다. 권한 있는 관리자가 준비를 확인한 뒤에만 startup hold를 해제하고 전체 preflight를 다시 통과한 후 전환 활성화를 판단한다.

롤백은 새 worker/CLI를 더 이상 호출하지 못하게 하고, 미해결 전환 DB/journal/switch hold와 systemd 작업이 없는지 확인한 뒤 gate·Studio·CLI의 백업된 호환 구성을 되돌리는 절차로 제한한다. 기존의 독립 `legacy`·관리자 hold는 미해결 전환 hold와 구별하며 제거 대상이 아니다. 불명·복구 필요 상태에서는 원본으로 단순 복원하거나 hold/journal을 삭제하지 않고 복구 절차로 분기한다. 기존 운영의 관리자 pause 및 Studio 복구 상태는 롤백 전후 보존한다.

## 7. 후속 작업과 검증 경계

- **M4-02/03:** gate 계정·경로·credential 검사와 root 전용 복구 진입의 기본 후보. **M4-04:** 동일 origin의 완전한 preflight 및 orphan·불일치의 영속 조사·안전한 종결. M2-02 문서 완료가 구현 완료는 아니다.
- **M5-02/03:** hold와 원자적 admission 및 오래된 요청 거절, Client 재조회·초안, 벤치마크와 Studio 조율. Wiki 예외는 유지한다.
- **M6-01/02:** 다른 주체의 hold 보존, pause 중 새 요청 거절, 대기 요청·hold 경합, 모든 정리 중단 지점, rollback 검증 및 실제 Linux peer UID·namespace·ACL·SELinux·systemd 경계 시험.
- **M7/M8:** 동일 후보의 설치·백업·credential·CLI 전환·복구·롤백 절차와 운영 준비를 검토한 뒤 실제 UI A→B 및 B 대화를 확인한다. 과거 DB fence·유지보수 VM 시험은 재개하지 않는다.

복구 후보 제한: 빈 journal과 영속 DB의 switch hold가 함께 남은 경우에는 root 조사에서 두 식별자를 확인할 수 있지만, 현재 gate API에는 독립 hold를 조건으로 자기 hold를 원자적으로 해제하는 연산이 없다. worker 후보는 아래 조건부 API를 요구하며 미지원·오류 시 `recovery_required`를 유지한다. 조건부 해제의 실제 gate 구현과 검증은 M5-02/M6 작업이다. 손상되거나 읽을 수 없는 journal은 내용을 노출하지 않는 조사 전용 상태로 기동하며 복구 실행과 새 전환은 차단한다. 복구 전용 Studio 검증 계약은 아래 후보이며 Studio 실제 구현과 통합 검증은 남은 과제다.

M4-03 후보는 빈 journal 복구에서 상관 hold와 독립 hold를 확인한 뒤 gate의 원자 조건부 해제를 요구한다. switch credential로 `POST /control/holds/{switch-hold}/release-if-independent`에 `expected_epoch`, `required_independent_hold`를 전달한다. gate는 같은 직렬화·영속 경계에서 역할과 자기 hold, epoch 일치, 지정 독립 hold 존재를 검증하고 자기 hold만 해제해야 한다. 성공 응답은 `protocol=3`, `durable=true`, 동일 `epoch`, `held=false`, `paused=true`, 독립 hold가 포함되고 자기 hold가 빠진 `holds`를 반환한다. 미지원·오류·응답 유실은 성공으로 보지 않으며 자기 hold 재획득을 시도하고 작업을 복구 필요로 유지한다. 응답 유실과 재획득이 모두 실패하면 hold 보존을 보장할 수 없다. 이 API는 현재 gate에 없으며 M5-02에서 구현·통합 검증 전 활성화할 수 없다.


복구 전용 Studio 검증 후보: worker는 GPU lock을 보유한 채 별도 credential로 `GET /internal/model-switch/recovery`를 호출한다. 이 endpoint는 GPU lock을 다시 취득하지 않는 비변경 조회여야 한다. Studio는 자신의 인증된 protocol 3 gate와 journal을 독립 확인하고, GPU 신규 작업이 차단되어 있고 Studio 자체의 미해결 GPU 작업·복구가 없을 때만 정확히 `protocol: 1`, `enabled: true`, `gpu_blocked: true`, `studio_recovery_clear: true`, `gate_verified: true`, `journal_sha256`을 반환한다. digest는 검증한 journal 객체를 ASCII JSON (`sort_keys=true`, `ensure_ascii=true`, 구분자 `,`와 `:`)으로 직렬화한 바이트의 SHA-256 소문자 hex다. 빈 객체도 같은 방식으로 계산한다. Studio는 임의 요청값을 반영하지 않고 직접 읽은 유효한 journal의 digest를 반환한다. worker는 전후 Studio·gate의 PID/시작시각, 양측 lock·journal inode, 잠긴 FD, journal 내용 및 양쪽 endpoint 인증을 확인한다. 이 조회의 성공은 hold 해제 권한이 아니며 별도 gate 조건부 해제 및 상태 검증이 필요하다. Studio endpoint와 조건부 gate API가 아직 없으므로 오류·미지원 시 복구를 허용하지 않는다. 실제 Studio의 잠금·admission 및 독립 복구 상태 검증과 endpoint 구현은 M5-03, 최종 통합 검증은 M6에 속한다.

활성 journal 복구와 전환 실패 후 자동 롤백은 gate의 영속 배타 복구 예약도 요구한다. switch credential로 `PUT /control/holds/{own-hold}/recovery-reservation`을 호출하며, gate는 자기 hold 존재와 다른 `switch-*`·`studio-*` 또는 미확인 hold가 없음을 같은 직렬화·영속 경계에서 확인하고 예약한다. `startup`, `legacy`, 유효한 `admin-*`는 보존한다. 응답과 `/control` 상태는 `protocol=3`, `durable=true`, `paused=true`, `recovery_exclusive_hold=own-hold`, 32자리 hex `epoch`, 자기 hold를 포함한 허용 hold 목록을 제공해야 한다. worker는 예약 후와 모델 변경 직전에 epoch와 상태를 다시 검증한다. 상태 조회만으로는 조회 직후 다른 hold가 생기는 경합을 막을 수 없다. 따라서 gate는 예약이 존재하는 동안 다른 Studio·switch hold 진입을 원자적으로 거절하고, 예약과 자기 hold를 성공 응답 전에 영속화하며, 응답 유실·worker 종료·gate 재시작에도 예약을 보존해야 한다. 예약은 timeout으로 만료되지 않고 검증된 자기 hold 해제와 같은 원자적 경계에서만 해제되어야 한다. 실패 또는 결과 불명에서는 예약과 hold를 유지해 재조사한다. 이 예약 API는 아직 실제 gate에 구현되지 않았으므로 지원 여부 확인 실패 시 모델 변경 없이 복구 필요 상태로 남는다. 정상 전환의 초기 admission 및 전체 경합 방지는 gate 구현과 함께 M5-02/M6에서 검증해야 한다.

정상 전환도 모델 변경 전에 같은 배타 예약을 취득하고 drain 후 다시 확인한다. 미지원 시 모델을 변경하지 않으며 불명인 journal/hold는 복구 대상으로 보존한다. 예약을 가진 작업의 정리에서는 해제 직전 검증한 epoch와 hold 목록을 `POST /control/holds/{own-hold}/release-recovery-reservation`에 `expected_epoch`, `expected_holds`로 전달한다. gate는 현재 목록과 epoch 및 예약 소유자를 같은 영속 경계에서 비교하고 자기 hold와 예약을 함께 제거해야 한다. 성공 응답은 `held=false`, `recovery_exclusive_hold=null`, protocol 3, durable, 같은 epoch, 다른 hold가 정확히 보존된 목록 및 목록과 일치하는 paused 상태를 포함한다. worker는 응답뿐 아니라 후속 `/control` 재조회도 같은 조건으로 검증한다. 어느 단계든 오류·응답 유실·불일치면 성공 처리하지 않고 자기 hold 재획득을 시도한다. 재획득이 성공해도 원래 예약의 복원은 보장되지 않으며 실패 시 hold 자체의 보존도 보장되지 않으므로 작업은 미해결이다. journal이 이미 비워진 뒤 상관 자기 예약이 남은 경우 독립 hold가 있을 때만 같은 예약 해제 API에 `required_independent_hold`를 함께 전달한다. gate는 이 hold의 존재까지 원자적으로 확인해야 한다. 타인의 예약 또는 예약 여부가 불명인 경우 차단한다. gate는 기존 조건부 해제에서도 예약이 없는 경우에만 실행되도록 원자적으로 보장해야 한다. 관련 API는 아직 gate에 구현되지 않았다.
