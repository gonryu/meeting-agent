#!/bin/bash
cd "$(dirname "$0")"

# 기존 main.py 프로세스 모두 종료
EXISTING=$(pgrep -f "main.py")
if [ -n "$EXISTING" ]; then
    echo "기존 프로세스 종료: $EXISTING"
    echo "$EXISTING" | xargs kill
    sleep 2
fi
rm -f server.pid

# 서버 시작
source .venv/bin/activate
nohup python3 -u main.py > server.log 2>&1 &
echo $! > server.pid
echo "서버 시작 (PID $(cat server.pid))"
