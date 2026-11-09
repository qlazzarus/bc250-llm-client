# M4-02 설치 경계 템플릿

이 디렉터리는 검토용 후보이며 운영에 그대로 설치하지 않는다. 구체적 포장·마이그레이션·백업·복구와 실제 Linux 검증은 M7/M6에서 완성한다.

- 현재 서버의 Client UID와 socket 그룹은 `yeppyshiba`를 사용한다. socket은 root 소유·`yeppyshiba` 그룹 `0660`, 상위 디렉터리는 root 소유 `0755`다. Client UID는 socket peer 검사에도 사용되며 같은 UID를 쓰는 프로세스는 동일하게 신뢰한다. 다른 서버에 맞게 변경하려면 runtime의 `CLIENT_USER`, socket unit과 필요 시 Client unit의 계정·그룹을 함께 변경해야 한다. 추후 설정 가능하게 일반화할 수 있다.
- 가정용 단일 관리자 전제에서 기존 `/opt/bc250-llm-client`와 `yeppyshiba` 배포를 사용할 수 있다. `bc250-llm-client.service.d/identity.conf` 및 별도 root 관리 Client runtime은 과거 강화안의 **선택적 예시**이며 필수 설치 대상이 아니다. 기존 배포는 worker·공유 lock·gate/Studio 계약을 설치하지 않으므로 M7에서 필요한 설치와 롤백을 별도로 준비한다.
- 기존 `/llm` 주소와 origin을 사용한다. 동일 origin 전체의 asset provenance·CSP·서명 manifest는 이번 모델 전환의 필수 선행 조건이 아니다. 앱과 운영자 인증은 유지하며, Wiki의 모델 조율 및 Client 통합은 사용자 지정 후속 예외다.
- 현재 운영 gate와 보존된 protocol 2 후보는 epoch admission을 제공하지 않는다. worker는 protocol 3·durable 상태·32자리 hex epoch를 요구한다. M5-02에서 gate·Studio·Client를 함께 구현하기 전 활성화할 수 없다.
- `/run/bc250-gpu/gpu.lock`과 `/var/lib/bc250-model-control/switch.json`은 root가 사전 준비한 공유 파일이어야 한다. ACL·SELinux·group 접근과 양쪽 namespace의 inode를 M6에서 실제 확인한다. 운영 Studio의 기존 lock 경로를 지금 바꾸거나 파일을 생성하지 않는다.

- [origin-review-contract.md](origin-review-contract.md)는 과거 강화안의 참고 자료다. 해당 승격·잠금 체계가 활성화 전 필수라는 뜻은 아니다. 전환 중 배포/restart 경합을 피하는 운영 절차와 미해결 전환 보존은 M7에서 준비한다.
