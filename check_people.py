"""People 폴더 내 파일 목록 확인"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__))
from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), ".env"))

from store import user_store
from googleapiclient.discovery import build

user_store.init_db()
creds = user_store.get_credentials(user_store.all_users()[0]["slack_user_id"])
user = user_store.all_users()[0]
print(f"contacts_folder_id: {user['contacts_folder_id']}")

svc = build("drive", "v3", credentials=creds)

# People 서브폴더 찾기
contacts_id = user["contacts_folder_id"]
q = f"name='People' and '{contacts_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false"
result = svc.files().list(q=q, fields="files(id,name)").execute()
people_folders = result.get("files", [])
print(f"People 폴더: {people_folders}")

if people_folders:
    folder_id = people_folders[0]["id"]
    result2 = svc.files().list(
        q=f"'{folder_id}' in parents and trashed=false",
        fields="files(id,name)"
    ).execute()
    print("People 폴더 내 파일 목록:")
    for f in result2.get("files", []):
        name = f["name"]
        print(f"  - '{name}' ({f['id']})")
        print(f"    bytes: {name.encode('utf-8').hex()}")

    # name 필터 직접 테스트
    import unicodedata
    search_name = "김민환.md"
    for form in ["NFC", "NFD", "NFKC", "NFKD"]:
        normalized = unicodedata.normalize(form, search_name)
        r = svc.files().list(
            q=f"name='{normalized}' and '{folder_id}' in parents and trashed=false",
            fields="files(id,name)"
        ).execute()
        found = len(r.get("files", [])) > 0
        print(f"  name 필터 ({form}): {'✅ 찾음' if found else '❌ 못 찾음'}")
