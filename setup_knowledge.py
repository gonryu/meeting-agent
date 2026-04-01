"""company_knowledge.md를 Google Drive에 업로드"""
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from dotenv import load_dotenv
import os

load_dotenv()

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/gmail.readonly",
]

creds = Credentials.from_authorized_user_file("token.json", SCOPES)
service = build("drive", "v3", credentials=creds)

root_folder_id = os.getenv("DRIVE_ROOT_FOLDER_ID")

metadata = {
    "name": "company_knowledge.md",
    "parents": [root_folder_id],
    "mimeType": "text/plain",
}

media = MediaFileUpload("company_knowledge.md", mimetype="text/plain")
file = service.files().create(body=metadata, media_body=media, fields="id").execute()

print(f"✅ company_knowledge.md 업로드 완료 — ID: {file['id']}")
