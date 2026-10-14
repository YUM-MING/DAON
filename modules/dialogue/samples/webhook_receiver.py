"""로컬 웹훅 수신 테스트용 서버 (표준 라이브러리 http.server만 사용).

보호자 웹앱이 알림을 받는 쪽을 흉내 낸다. 받은 알림 JSON을 화면에 출력하고 200으로 응답만 한다.

  1) 수신 서버 실행:   python samples/webhook_receiver.py            (기본 포트 8765)
  2) 다른 터미널에서:  $env:ALERT_WEBHOOK_URL = "http://127.0.0.1:8765/alerts"
                       python main.py --live
     (또는 .env에 ALERT_WEBHOOK_URL=http://127.0.0.1:8765/alerts)
"""
from __future__ import annotations

import argparse
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Callable, Optional


def _print_alert(payload: dict) -> None:
    print(f"[수신] {payload.get('time', '-')}  kind={payload.get('kind')}  status={payload.get('status')}  "
          f"seq={payload.get('seq')}  cause={payload.get('cause')}")
    print(f"       {payload.get('title', '')}")
    for line in str(payload.get("body", "")).splitlines():
        print(f"       {line}")
    sys.stdout.flush()


def make_server(port: int = 8765, on_alert: Optional[Callable[[dict], None]] = None,
                host: str = "127.0.0.1") -> HTTPServer:
    """port=0이면 비어 있는 포트를 자동으로 고른다 (server.server_address[1]로 확인)."""
    handle_alert = on_alert or _print_alert

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                self.send_response(400); self.end_headers()
                return
            handle_alert(payload)
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, fmt, *args):     # 기본 접속 로그는 숨긴다
            pass

    return HTTPServer((host, port), Handler)


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="로컬 웹훅 수신 테스트 서버")
    ap.add_argument("--port", type=int, default=8765)
    port = ap.parse_args().port
    server = make_server(port)
    print(f"웹훅 수신 대기: http://127.0.0.1:{port}/alerts  (Ctrl+C로 종료)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("종료합니다.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
