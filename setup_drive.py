"""Drive 셋업 수동 재실행 스크립트"""
import os
from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

from store import user_store
from server.oauth import _setup_drive_for_user

user_store.init_db()

users = user_store.all_users()
for u in users:
    print(f"user: {u['slack_user_id']}, contacts_folder_id: {u['contacts_folder_id']}")

if not users:
    print("등록된 사용자가 없습니다.")
else:
    user_id = users[0]["slack_user_id"]
    print(f"\n{user_id} 의 Drive 셋업을 재실행합니다...")
    creds = user_store.get_credentials(user_id)
    _setup_drive_for_user(user_id, creds)
    print("완료!")
    u = user_store.get_user(user_id)
    print(f"contacts_folder_id: {u['contacts_folder_id']}")
    print(f"knowledge_file_id:  {u['knowledge_file_id']}")
