# Imagegen 인계 스냅샷 — 2026-11-07

Studio 분리 작업 직전의 tracked/untracked 소스 455개를 보존했다. source.tar.gz는 원본 참고용이며 Client 실행 경로에 설치한 코드가 아니다. HEAD, status.txt, working-tree.patch, 파일별 SHA-256 manifest.json을 함께 보관한다. ignored 환경 파일·DB·가상환경·node_modules는 포함하지 않았다.

새 작업은 ../../plans/llm-model-switch-plan.md를 따른다. 아카이브 전체를 운영에 설치하지 않는다. 필요한 모델 전환 모듈과 테스트만 선별 이관하며 최신 Studio 공용 barrier 분리는 imagegen의 services/switch_barrier.py를 참고한다.
