"""실시간 추론 테스트 진입점 (제출 규격: inference.py).

웹캠(또는 영상 파일)에서 실시간으로 포즈/낙상/표정을 인식하고
이벤트를 콘솔에 출력한다. 내부 구현은 src/main.py와 동일.

사용법:
    python inference.py                      # 기본 웹캠(0)
    python inference.py --source 1           # 다른 카메라
    python inference.py --source video.mp4   # 영상 파일 테스트
    python inference.py --no-show            # 화면 없이 (서버 모드)
    python inference.py --log events.jsonl   # 이벤트 로그 저장
종료: q
"""
from src.main import main

if __name__ == "__main__":
    main()
