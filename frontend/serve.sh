#!/bin/bash
# 관리자 프론트엔드 로컬 개발 서버
# 기본 포트 3000 (백엔드 CORS 허용 목록과 일치)

cd "$(dirname "$0")"
PORT="${PORT:-3030}"

echo "🌐 프론트엔드 실행: http://localhost:${PORT}"
echo "🔗 백엔드: $(grep -E 'BACKEND_URL' config.js | head -1)"
echo ""
python3 -m http.server "${PORT}"
