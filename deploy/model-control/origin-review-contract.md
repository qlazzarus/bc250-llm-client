# 선택적 Origin 진단 모듈

`backend/app/model_control/origin_*`는 같은 origin의 설정·경로·응답·실행 파일과 검토 기준을 검사하는 모듈이다. 기본 runtime은 `CoordinationPreflight`와 `RecoveryPreflight`를 사용하며, 이 문서의 존재나 진단 성공이 일반 모델 전환·배포 허가를 의미하지 않는다.

## 진단과 잠금

`inspect_locked_origin()`은 origin 변경 잠금을 직접 취득하고 기준 anchor와 경로 검사 결과를 검증한다. 이미 같은 잠금을 잡은 구간에서 중복 호출하면 경합할 수 있다. 잠금은 협력적이며 참여하지 않는 배포자나 root 변경까지 차단하지 않는다.

`inspect_clear_deployment_records()`는 gate 조회 전후의 epoch, journal과 작업 DB의 상태를 비교하는 읽기 전용 진단이다. 호출자가 잠금을 보유해야 하며 진단 자체는 조회자의 신원이나 전체 쓰기 경로의 직렬화를 보장하지 않는다.

`origin_deployment_guard()`는 고정된 journal·작업 DB와 파일 신뢰 상태를 검사하는 보호 부품이다. 내용이 있는 WAL은 보수적으로 거절하며 검사 결과만으로 배포를 허용하지 않는다. DB 원본에 쓰지 않는다.

## 적용 범위

이 모듈은 현재 단일 관리자 모델 제어의 필수 운영 조건이 아니다. Client 자동 배포와 공용 worker 사이에 강제 배포 잠금이 연결돼 있다고 해석하지 않는다. 배포·전환 자동 상호 배제의 추가 구현 범위는 [TODO](../../docs/TODO.md)에서 관리한다.

기본 운영에서는 [모델 제어 구성](../../docs/model-control.md)의 GPU lock·journal·owned hold·admission/drain과 복구 규칙을 사용한다.
