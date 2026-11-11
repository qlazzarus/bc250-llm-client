# M7-02 배포 준비 판정 — 2026-11-11

## 판정

**후속 운영 진행:** 아래 최초 판정 후 M8에서 protocol 3 공용 gate·Studio·worker를 연결하고 실제 일반/복구 preflight를 확인했다. 네 모델의 slot context와 지정된 요청 형태를 검증해 기록을 설치했고 Client 전환을 활성화했다. 실제 UI 전환·후속 대화와 채팅 drain·동시 요청 거절을 확인했다. 모델은 운영 중 바뀔 수 있으므로 현재 값은 `ai-model status`로 조회한다. 상세 이력과 실제 이미지 경합·운영 장애 주입 등 미시험 범위는 [M8 작업 기록](llm-model-switch-tasks.md#m7-02-준비-판정--2026-11-11)을 따른다.

**최초 11/11 판정 당시 운영 전환 활성화: 불가.** 당시 공용 gate·worker·lock/journal이 설치되지 않았고, Studio와 Client의 신규 역할 키·설정이 적용되지 않았으며 네 모델의 기능 검증 기록이 모두 `unknown`이었다. 현재 상태는 위 후속 진행을 따른다.

**아래는 최초 판정 당시의 기록이다.** 설치 전의 조건부 판정·미완료 항목을 현재 운영 상태로 해석하지 않는다. 실제 운영 백업의 전체 복원 시험은 하지 않았다.

최초 판정 당시 M7-02의 설치 전 준비 판정은 완료였으며 운영 공용 계약과 실제 모델 기능 검증은 미완료였다.

추가 준비 검증: 실제 gate 후보를 사용한 준비 상태 조회→startup hold 해제→rollback 판정 연결이 통과했고, 해제 응답 유실 시 자동 재시도 없이 기존 `legacy` hold를 보존했다. Studio·worker 신원은 대역이다. 서버 임시 user systemd service에서 socket 활성화 후 직접 service restart 시에도 systemd socket FD가 다시 전달됨을 확인했다. 실제 root 운영 서비스 재시작의 증거는 아니다.

## 고정 후보와 시험 근거

| 항목 | 확인 결과 |
| --- | --- |
| 시험 대상 코드 | Client 저장소 `deebf5d6d609cef2135df3f8573e105841f997f6`, tree `988df29d1d432997e27fe86d14cc55f22a4a43db`. 이 판정 문서만 추가하는 후속 commit은 코드 후보 변경이 아니다. 코드·설정 후보가 바뀌면 해시와 시험을 다시 확인한다. |
| 운영 Client | 배포 이력 `bf1d0ba`. 해당 commit과 후보의 Client backend/frontend 차이는 미설치 worker용 `backend/app/model_control/runtime.py`뿐이다. 운영 `main.py`·`config.py` SHA-256은 후보와 일치한다. 이는 gate나 worker가 설치됐다는 뜻이 아니다. |
| 운영 Studio | 확인한 `main.py`, `services/llm.py`, `services/gpu.py` SHA-256은 각각 `3692f5f591b21ee6f66ea7f0fae3a2f6bc4c94383fd0b6254abd4d2d4b43103f`, `fe3871095c5c32fa0b6b0b488557d65e14d876757ba9ed7683c67c670059399e`, `ed7627cc9597bbc05c1371f30b0c53f2fdbc0aabcc7bb293b843653279b11702`. coordination/recovery 코드가 존재하며 공용 설정은 아직 미적용이다. |
| 로컬 최종 회귀 | backend **849 통과·6 건너뜀**; frontend lint·**14 tests**·build 통과. GNU tar Linux 전용 시험과 외부 Studio 등 환경 의존 시험은 로컬 skip에 포함된다. |
| 서버 격리 회귀 | Python 3.14, 운영 Studio 소스 복사본·fake 엔진을 사용한 선택 시험 **110 통과**. 실제 Edge에서 UI→Client→Unix worker→protocol 3 gate와 대화 확인. llama 서비스·Studio preflight는 대역이다. |
| Linux root 경계 | 11/10 root transient service에서 실제 socket activation·재시작, Client UID, Studio 공유 inode·lock, `CAP_SYS_PTRACE`와 SELinux enforcing 조건의 journal 원자 교체 확인. 기존 라벨이 다르면 교체를 거절하고 정책 라벨 일치 시 통과했다. 임시 경로와 가짜 credential 사용. |
| systemd 활성화 설정 | 11/11 임시 user service에서 EnvironmentFile 순서와 worker Environment override를 검증했다. Client·worker opt-in 후보는 초기 설치에서 제외한다. |
| gate 패키지 | Python 3.14 임시 전용 venv에서 고정 의존성 전체, 역할 인증, startup/legacy hold와 snapshot 확인. requirements SHA-256 `60d1bf5dbf54c3f95200ae5387ee435864551ff346ab276fe0561ffc9b98e915`. worker requirements SHA-256 `2da5a9c95684dde622e07a99591726669da7db68039b2c71ecfeb70d2d083e42`. 실제 설치 위치의 패키지와 라벨은 적용 시 재검증한다. |

시험한 후보의 worker socket 경로 보존 수정과 활성화 설정 파일의 서버 복사본 hash는 로컬 후보와 일치했다. 백업 아카이브는 임시 GNU tar 시험에서 파일·mode·symlink·ACL·SELinux 속성을 복원했다. 이는 실제 운영 백업이나 롤백 실행의 증거가 아니다.

## 11/11 운영 읽기 전용 확인

| 항목 | 확인 결과 |
| --- | --- |
| 서비스 | Client·Studio·구형 gate·llama 모두 active/running, systemd 작업 없음. worker socket은 미설치이며 `/opt/bc250-model-control`, `/opt/bc250-llm-gate`, `/run/bc250-gpu`, 두 신규 state 경로는 부재. |
| gate·Studio | root 조회에서 구형 gate `paused=false`, active/waiting 0; Studio `nonterminal_jobs=0`, `quiescent=true`, GPU/복구 clear. 조회 시점에만 유효하고 다른 제어자의 진입을 막지 않는다. |
| Client·키 | Client 전환 비활성, 고정 모델 ID 비어 있음, 운영자 키 미설정. Client와 구형 gate 키 불일치, Studio와 구형 gate 키 일치. 신규 Studio owned hold 비활성, coordination 키 미설정. 비교에 사용한 키 값은 메모리에만 두고 값·해시는 출력하거나 저장하지 않았다. |
| 현재 모델 | `qwen36-35b-a3b-uncensored`, llama health ok, 로드 모델 1개, 슬롯 1개·처리 중 0·실제 context 16384. 다른 세 모델은 이번에 로드하지 않았다. |
| 직접 제어자 | 기존 `/usr/local/bin/ai-model`은 `/opt/ai/scripts/ai-model`을 가리킨다. 두 benchmark 원본 hash는 M7-01 기록과 일치하고 아직 직접 제어 경로다. 독립 Wiki는 사용자 지정 예외다. |

| 관리 모델 | 설정 context | 실제 로드·slot context | reasoning / JSON schema 기록 |
| --- | ---: | --- | --- |
| `gemma4-e4b-hauhaucs` | 16384 | 이번 판정에서 미확인 | unknown / unknown |
| `gemma4-e4b-heretic-rp` | 16384 | 미확인 | unknown / unknown |
| `qwen35-35b-a3b-uncensored` | 4096 | 미확인 | unknown / unknown |
| `qwen36-35b-a3b-uncensored` | 16384 | 16384 | unknown / unknown |

## M8 진행 조건과 중단 기준

1. 모든 적용 직전 gate·Studio·llama·Client의 상태, 기존 pause, 원본 hash와 설정을 다시 조회한다. 신규 요청·이미지·벤치마크·CLI·배포를 관리자가 중단하고, root 전용 백업·credential staging과 후보를 준비한다. 조회만으로 제어 경합을 배제하지 않는다.
2. 신규 gate는 원래 pause를 `legacy`로 보존하고 `startup` hold를 둔다. 신규 worker와 Client는 전환 비활성으로 설치한다. 기존 CLI의 실제 실행 파일과 benchmark 두 경로를 같은 구간에 공용 경로로 전환한다. 작업 중 불명 상태·journal/hold 불일치·응답 실패는 중단하고 보존한다.
3. 실제 파일/키·Studio/gate 역할·namespace inode와 전환 비활성 worker를 확인한다. `startup`을 관리자 확인으로 해제한 뒤에만 전체 worker preflight가 가능하다. 다른 hold는 임의 해제하지 않는다. 기존 `legacy`가 있으면 보존한다.
4. 실제 모델별 확인은 Client 전환 UI를 끈 채 **공용 worker CLI만 제한적으로 활성화**해 수행한다. 각 모델의 실제 ID·slot context·필요한 reasoning/schema를 검증하고 동일 모델 파일·설정·llama runtime에 묶인 기록을 준비한다. 사용자 트래픽과 다른 제어자는 차단한다. 불명 결과 시 다음 모델이나 Client 활성화로 진행하지 않는다.
5. 현재/대상 모델의 필요한 기능 검증과 전체 조율 확인이 끝난 뒤 worker 설정과 Client activation EnvironmentFile의 유효값을 확인하고 Client 모델 전환 UI를 활성화한다. 실제 UI A→B, B 후속 대화와 CLI/API 일치·이미지 경합은 M8-02 완료 조건이다.
6. 롤백 시 [rollback 판정과 복원 절차](llm-model-switch-install-recovery.md#d-불명-상태와-롤백)를 따른다. 미해결 worker 기록, 표현할 수 없는 hold, 진행 중 작업 또는 응답 불명에서는 원본·기록·hold를 보존하고 중단한다. 구형 gate 재시작은 unpaused이므로 기존 pause가 있었다면 외부 유입 차단 상태에서 복원·확인한다.

기존 앱·관리자 인증, 공유 GPU lock·journal, gate admission/drain, 다른 작업의 hold와 불명 상태 복구는 유지한다. 전용 서비스 UID, 전체 origin 자산 검증, 기존 Studio 배포 방식 유지, Wiki 조율 및 과거 DB 차단·유지보수 VM 시험은 이번 선행 조건이 아니다.
