# Studio 모델 전환 복구 인터페이스

Studio는 Client와 별도 서비스이며 worker와 공용 GPU lock·전환 journal·protocol 3 gate를 사용한다. Client 일반 배포는 외부 Studio 소스나 서비스를 교체하지 않는다.

## 복구 조회 계약

인증된 `/internal/model-switch/recovery`는 worker가 이미 GPU lock을 보유한 상태에서 호출하는 읽기 전용 endpoint다. Studio가 같은 잠금을 다시 취득하지 않는다. Linux lock table에서 안전하게 연 lock inode에 root 소유 배타 flock이 있는지 검사하고, `/proc/locks` 접근이 제한되거나 상태를 확인할 수 없으면 거절한다.

응답은 유효한 journal의 digest, GPU 진입 차단 상태, Studio 자체 복구 상태와 protocol 3 gate 검증 상태를 구분한다. 빈 journal도 digest 대상이다. worker는 자신의 journal snapshot·잠긴 파일·namespace와 대조한다. 이 조회 하나만으로 hold 해제를 승인하지 않는다.

## 계약 검사

Client 측 계약 회귀는 `tests/test_studio_recovery_contract.py`와 최소 method fixture를 사용한다. 이 검사는 현재 설치된 외부 Studio 소스를 검증하는 것이 아니다. 실제 Studio checkout을 사용하는 검사는 `BC250_STUDIO_SOURCE`를 지정하며 공용 계약은 [모델 제어 구성](../docs/model-control.md)을 따른다.
