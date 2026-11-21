# 모델 제어 배포 구성

이 디렉터리는 공용 worker·protocol 3 gate·Client·Studio를 연결하는 systemd 템플릿, credential 설정 예시, CLI launcher와 bootstrap을 제공한다. 기능 설명은 [모델 제어](../../docs/model-control.md), 운영 절차는 [개발과 운영](../../docs/operations.md), 남은 작업은 [TODO](../../docs/TODO.md)를 따른다.

## 실행 경계

- worker는 root 실행과 Unix peer 검사를 사용한다. runtime의 Client 계정은 `yeppyshiba`이며 socket은 root 소유, Client 그룹 0660을 사용한다. 다른 계정에서는 runtime·socket·서비스 설정을 함께 맞춘다.
- 공유 GPU lock은 `/run/bc250-gpu/gpu.lock`, journal은 `/var/lib/bc250-model-control/switch.json`이다. Studio와 worker가 같은 파일을 사용하는지 namespace·inode·권한·SELinux를 확인한다.
- gate runtime은 별도 `/opt/bc250-llm-gate`와 `/var/lib/bc250-llm-gate`를 사용하며 service가 state를 자동 provision하지 않는다. systemd credential 파일로 역할 키와 upstream 키를 전달한다.
- worker bootstrap은 `/opt/bc250-model-control`의 실행 트리를 검사한다. Client 일반 배포 스크립트는 이 runtime과 외부 Studio를 설치하지 않는다.
- Client `MODEL_CONTROL_ENABLED`와 worker `MODEL_CONTROL_SWITCH_ENABLED`는 별도 활성화 값이다. Client의 activation EnvironmentFile은 기본 환경 파일 이후에 읽혀야 한다.

## 적용과 상태 관리

템플릿의 계정·경로·credential·서비스 이름을 대상 환경에 맞춰 사용한다. gate state가 이미 있으면 초기화 도구를 다시 실행하지 않는다. 기존 pause와 다른 소유자의 hold를 보존하며 불명 상태에서는 새 전환과 임의 해제를 중단한다.

전환 중에는 Client·gate·Studio·worker·llama를 배포하거나 재시작하지 않는다. 자동 main 배포도 같은 단일 관리자 조율 대상이다. `ai-model status`와 작업 ID 조회를 통해 완료를 확인한다. 결과 불명이나 recovery_required는 복구 검사 대상으로 남긴다.

[origin-review-contract.md](origin-review-contract.md)의 별도 검토 모듈과 `identity.conf` 등 강화 템플릿은 기본 구성의 필수 설치 대상이 아니다.
