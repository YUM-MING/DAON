#!/usr/bin/env bash
# DAON 통합 서버 한 번에 기동
#   ./run.sh            실제 센서 (config.yaml)
#   ./run.sh --mock     센서 없이 가짜 이벤트로 UI/대화 테스트
set -e
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  echo "[run] .venv 가 없습니다. 먼저:  python3.12 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt"
  exit 1
fi
source .venv/bin/activate
exec python -m server.main "$@"
