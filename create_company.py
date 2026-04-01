"""특정 업체 정보를 Gemini로 검색해서 Drive에 저장하는 스크립트"""
import os
import sys
from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

from store import user_store
user_store.init_db()

users = user_store.all_users()
if not users:
    print("등록된 사용자가 없습니다.")
    sys.exit(1)

user_id = users[0]["slack_user_id"]
company_name = sys.argv[1] if len(sys.argv) > 1 else "LG전자"

print(f"'{company_name}' 정보를 검색합니다...")
from agents.before import research_company
content, file_id = research_company(user_id, company_name)
print(f"\n✅ 저장 완료 (file_id: {file_id})")
print(f"\n--- 내용 미리보기 ---")
print(content[:500])
