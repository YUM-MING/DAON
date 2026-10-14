#!/usr/bin/env bash
# DAON 통합 서버 한 번에 기동
#   ./run.sh                 폰 카메라·마이크 (config.yaml)
#   ./run.sh --tunnel        + Cloudflare 터널 → https 공개 주소 (폰·보호자가 밖에서 접속, 폰 카메라는 https 필수)
#   ./run.sh --mock          센서 워커 없이 가짜 이벤트
#   그 외 인자는 그대로 server.main 에 전달 (--port 8000 등)
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  echo "[run] .venv 가 없습니다. 먼저:  python3.12 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
  exit 1
fi
source .venv/bin/activate

TUNNEL=0; ARGS=(); PORT=8000
while [ $# -gt 0 ]; do
  case "$1" in
    --tunnel) TUNNEL=1 ;;
    --port) ARGS+=("$1" "$2"); PORT="$2"; shift ;;
    *) ARGS+=("$1") ;;
  esac
  shift
done

cleanup(){ [ -n "$TPID" ] && kill "$TPID" 2>/dev/null; [ -n "$SPID" ] && kill "$SPID" 2>/dev/null; }
trap cleanup EXIT INT TERM

if [ "$TUNNEL" = "1" ]; then
  if ! command -v cloudflared >/dev/null; then
    echo "[run] cloudflared 가 없습니다:  brew install cloudflared   (Windows: winget install Cloudflare.cloudflared)"
    exit 1
  fi
  python -m server.main "${ARGS[@]}" & SPID=$!
  echo "[run] 터널 여는 중…"
  cloudflared tunnel --url "http://localhost:$PORT" --no-autoupdate > logs/tunnel.log 2>&1 & TPID=$!
  for i in $(seq 1 30); do
    URL=$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' logs/tunnel.log | head -1)
    [ -n "$URL" ] && break
    sleep 1
  done
  if [ -n "$URL" ]; then
    echo "=================================================================="
    echo "  공개 주소:  $URL"
    echo "  로봇 폰   :  $URL/            → 사용자용 → 집 코드 입력"
    echo "  보호자    :  $URL/            → 보호자용 → 로그인 (데모: demo@daon.local / daon1234)"
    echo "=================================================================="
  else
    echo "[run] 터널 주소를 못 받았습니다. logs/tunnel.log 확인"
  fi
  wait $SPID
else
  exec python -m server.main "${ARGS[@]}"
fi
