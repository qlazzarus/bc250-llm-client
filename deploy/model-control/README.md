# M4-02 설치 경계 템플릿

이 디렉터리는 검토용 후보이며 운영에 그대로 설치하지 않는다. 구체적 포장·마이그레이션·백업·복구와 실제 Linux 검증은 M7/M6에서 완성한다.

- `ai-client`는 대화형 로그인이 불가능한 전용 UID·기본 그룹이어야 하며 다른 계정과 UID를 공유할 수 없다. `yeppyshiba`를 그대로 쓰지 않는다. socket은 root 소유·`ai-client` 그룹 `0660`, 상위 디렉터리는 root 관리여야 한다.
- Client는 root 소유이며 일반 계정·서비스 계정이 수정할 수 없는 `/opt/bc250-llm-client-runtime`에서 `client-bootstrap.py`와 검토된 `lib`를 로드한다. 템플릿 drop-in은 `/usr/bin/python3 -I -S`로 고정한다. 응용 코드·의존성·systemd unit/drop-in·환경파일의 권한과 Client가 보는 mount namespace를 worker preflight가 검사한다.
- 기존 `/opt/bc250-llm-client`와 기존 배포 스크립트는 `yeppyshiba` 쓰기 권한을 전제로 하며 새 트리를 구성·갱신하지 않는다. 이 템플릿만 설치하면 자동 배포가 새 실행본을 갱신하지 못한다. M7에서 root 제어 설치·업데이트·무결성·롤백 경로와 기존 DB/WAL·백업의 접근 권한 이관을 명시하고 검증해야 한다. 기존 데이터를 임의로 `chown`하거나 덮어쓰지 않는다.
- Client의 operator UI 자산과 실제 Caddy serving 경로도 일반 사용자 쓰기 불가능한 별도 신뢰 경계가 필요하다. 현재 effective Caddy 설정·자산 provenance를 검증하는 구현은 없으므로 worker preflight는 **무조건 거절**한다. M4-02에서 단일 안전 경로만 허용하는 검증기와 로컬 대역 시험을 구현하고 M7에서 해당 배포 구성·자산을 준비한다. 실제 실행 설정·namespace·모든 제공 자산을 확인하기 전 이 차단을 제거하거나 설정 플래그로 우회하지 않는다. 같은 origin의 다른 사용자 수정 가능 자산도 영향을 검토한다.
- 사용자는 기존 `/llm` 주소와 origin을 유지하도록 선택했다. 운영 Caddy의 실효 구성에서 Client·Studio 정적 root와 Client·Studio·Wiki upstream이 같은 HTTP 서버에 있다. 동일 origin의 자산·동적 응답·관리 인터페이스와 배포 권한을 모두 고려해야 한다. Wiki 기능의 통합이나 모델 조율은 후속 예외지만, 동일 origin 보안 경계에서 제외한다는 뜻은 아니다.
- `/usr/bin/python3`의 시스템 표준 라이브러리와 동적 로더는 운영 OS의 root 관리 구성이라는 가정이 있으며, Client namespace에서 대체되지 않았는지 Linux/M7에서 입증해야 한다. 무결성을 입증하지 못하면 활성화하지 않는다.
- 현재 운영 gate와 보존된 protocol 2 후보는 epoch admission을 제공하지 않는다. worker는 protocol 3·durable 상태·32자리 hex epoch를 요구한다. M5-02에서 gate·Studio·Client를 함께 구현하기 전 활성화할 수 없다.
- `/run/bc250-gpu/gpu.lock`과 `/var/lib/bc250-model-control/switch.json`은 root가 사전 준비한 공유 파일이어야 한다. ACL·SELinux·group 접근과 양쪽 namespace의 inode를 M6에서 실제 확인한다. 운영 Studio의 기존 lock 경로를 지금 바꾸거나 파일을 생성하지 않는다.
