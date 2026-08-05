"""실험 executor Pod의 브랜치 bootstrap 패키지.

[파이프라인]
실험 이슈와 기준 SHA가 봉인된 뒤 launcher가 Pod를 기동하고, 실제 실험 코드가 실행되기
전 exp branch를 준비하는 구간을 담당한다.

[기능]
launcher 입력 검증, GitHub App installation token의 파일 전달, 봉인 SHA 기반 ref 생성
기능을 모듈별로 제공한다.

[비책임]
Pod/volume 배포와 App private key mount(Autoresearch-infra), Job 생성과 상태 전이
(#546 launcher), Git checkout·실험 코드 실행은 담당하지 않는다.
"""
