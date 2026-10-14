"""DAON 통합 서버 — FastAPI + WebSocket 브로커.

구조
    core/        내부 이벤트 버스, WebSocket 브로커, 상태 조정자(coordinator), 설정
    adapters/    각 팀 모듈 ↔ 내부 버스 변환 (모듈 코드는 건드리지 않는다)
    workers/     비전·음성·레이더를 별도 프로세스로 띄우는 스크립트 (JSON lines 통신)
    mock/        센서 없이 UI/대화를 테스트하기 위한 가짜 이벤트 발생기
    main.py      FastAPI 앱 + 진입점
"""
