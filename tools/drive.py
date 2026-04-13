"""Google Drive API 래퍼 — Contacts 읽기/쓰기 + Wiki 구조 + Sources 원본 보관"""
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaInMemoryUpload
from datetime import datetime, timezone
import logging
import os

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/gmail.readonly",
]


def _service(creds: Credentials):
    return build("drive", "v3", credentials=creds)


def _read_file(creds: Credentials, file_id: str) -> str:
    content = _service(creds).files().get_media(fileId=file_id).execute()
    return content.decode("utf-8")


def _find_file(creds: Credentials, name: str, parent_id: str) -> dict | None:
    """파일명으로 Drive 파일 조회 (NFD → NFC 순으로 시도 — 인코딩 혼재 대응)"""
    import unicodedata
    svc = _service(creds)
    for form in ("NFD", "NFC"):
        normalized = unicodedata.normalize(form, name)
        q = f"name='{normalized}' and '{parent_id}' in parents and trashed=false"
        result = svc.files().list(q=q, fields="files(id,name,modifiedTime)").execute()
        files = result.get("files", [])
        if files:
            return files[0]
    return None


def _write_file(creds: Credentials, name: str, content: str,
                parent_id: str, file_id: str = None) -> str:
    """Drive 파일 생성 또는 업데이트.
    file_id 없어도 이름으로 기존 파일 검색 후 존재하면 업데이트, 없으면 신규 생성.
    파일명은 NFD 정규화(_find_file 검색 기준과 통일).
    """
    import unicodedata
    name_nfd = unicodedata.normalize("NFD", name)
    media = MediaInMemoryUpload(content.encode("utf-8"), mimetype="text/plain")
    svc = _service(creds)

    if not file_id:
        existing = _find_file(creds, name, parent_id)
        if existing:
            file_id = existing["id"]

    if file_id:
        svc.files().update(fileId=file_id, media_body=media).execute()
        return file_id
    else:
        metadata = {"name": name_nfd, "parents": [parent_id], "mimeType": "text/plain"}
        file = svc.files().create(body=metadata, media_body=media, fields="id").execute()
        return file["id"]


def create_folder(creds: Credentials, name: str, parent_id: str = None) -> str:
    """Drive 폴더 생성 (이미 존재하면 기존 폴더 ID 반환). Returns: folder_id"""
    svc = _service(creds)
    q = f"name='{name}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
    if parent_id:
        q += f" and '{parent_id}' in parents"
    result = svc.files().list(q=q, fields="files(id)").execute()
    files = result.get("files", [])
    if files:
        return files[0]["id"]
    metadata = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
    }
    if parent_id:
        metadata["parents"] = [parent_id]
    folder = svc.files().create(body=metadata, fields="id").execute()
    return folder["id"]


def _get_subfolder_id(creds: Credentials, contacts_folder_id: str, folder_name: str) -> str | None:
    """Contacts 하위 폴더 ID 조회 (Companies 또는 People)"""
    q = (f"name='{folder_name}' and '{contacts_folder_id}' in parents "
         f"and mimeType='application/vnd.google-apps.folder' and trashed=false")
    result = _service(creds).files().list(q=q, fields="files(id)").execute()
    files = result.get("files", [])
    return files[0]["id"] if files else None


def get_company_names(creds: Credentials, contacts_folder_id: str) -> list[str]:
    """Contacts/Companies 폴더의 업체명 목록 반환"""
    svc = _service(creds)
    q = (f"'{contacts_folder_id}' in parents "
         f"and mimeType='application/vnd.google-apps.folder' and trashed=false")
    result = svc.files().list(q=q, fields="files(id,name)").execute()
    companies = []
    for folder in result.get("files", []):
        if folder["name"] == "Companies":
            q2 = f"'{folder['id']}' in parents and trashed=false"
            r2 = svc.files().list(q=q2, fields="files(name)").execute()
            import unicodedata
            companies = [unicodedata.normalize("NFC", f["name"].replace(".md", "")) for f in r2.get("files", [])]
    return companies


def get_company_info(creds: Credentials, contacts_folder_id: str,
                     company_name: str) -> tuple[str | None, str | None, bool]:
    """
    업체 정보 조회
    Returns: (content, file_id, is_fresh)
    is_fresh: last_searched가 7일 이내면 True
    """
    folder_id = _get_subfolder_id(creds, contacts_folder_id, "Companies")
    if not folder_id:
        return None, None, False
    file = _find_file(creds, f"{company_name}.md", folder_id)
    if not file:
        return None, None, False

    content = _read_file(creds, file["id"])

    is_fresh = False
    for line in content.splitlines():
        if line.startswith("- last_searched:"):
            try:
                date_str = line.split(":", 1)[1].strip()
                last = datetime.strptime(date_str, "%Y-%m-%d")
                is_fresh = (datetime.now() - last).days < 7
            except Exception:
                pass
    return content, file["id"], is_fresh


def save_company_info(creds: Credentials, contacts_folder_id: str,
                      company_name: str, content: str, file_id: str = None) -> str:
    """업체 정보 저장"""
    folder_id = _get_subfolder_id(creds, contacts_folder_id, "Companies")
    return _write_file(creds, f"{company_name}.md", content, folder_id, file_id)


def get_person_info(creds: Credentials, contacts_folder_id: str,
                    person_name: str) -> tuple[str | None, str | None]:
    """인물 정보 조회. Returns: (content, file_id)"""
    import logging
    log = logging.getLogger(__name__)
    folder_id = _get_subfolder_id(creds, contacts_folder_id, "People")
    log.info(f"get_person_info: People folder_id={folder_id}")
    if not folder_id:
        return None, None
    file = _find_file(creds, f"{person_name}.md", folder_id)
    log.info(f"get_person_info: file={file}")
    if not file:
        return None, None
    return _read_file(creds, file["id"]), file["id"]


def save_person_info(creds: Credentials, contacts_folder_id: str,
                     person_name: str, content: str, file_id: str = None) -> str:
    """인물 정보 저장"""
    folder_id = _get_subfolder_id(creds, contacts_folder_id, "People")
    return _write_file(creds, f"{person_name}.md", content, folder_id, file_id)


def get_company_knowledge(creds: Credentials, knowledge_file_id: str) -> str:
    """company_knowledge.md 읽기"""
    return _read_file(creds, knowledge_file_id)


def update_company_knowledge(creds: Credentials, knowledge_file_id: str, content: str):
    """company_knowledge.md 업데이트"""
    media = MediaInMemoryUpload(content.encode("utf-8"), mimetype="text/plain")
    _service(creds).files().update(fileId=knowledge_file_id, media_body=media).execute()


# ── During Agent: 트랜스크립트 / 회의록 ─────────────────────────


def find_meet_transcript(creds: Credentials, meeting_title: str,
                         ended_after: "datetime" = None) -> dict | None:
    """
    Google Meet 트랜스크립트/회의록 파일 검색.
    Drive의 'Meet Recordings' 폴더에서 아래 두 유형을 탐색:
      - 구형 Meet: '{meeting_title} - Transcript' (영문)
      - Gemini 회의록: '{meeting_title} - ... - Gemini가 작성한 회의록' (한국어)
    서브폴더 없이 루트에 바로 저장된 경우도 지원.
    Returns: {id, name, mimeType} 또는 None
    """
    import unicodedata
    svc = _service(creds)

    # Meet Recordings 폴더 탐색 (루트 레벨)
    q = ("name='Meet Recordings' "
         "and mimeType='application/vnd.google-apps.folder' "
         "and trashed=false")
    result = svc.files().list(q=q, fields="files(id,name)").execute()
    recordings_folders = result.get("files", [])
    if not recordings_folders:
        return None

    recordings_id = recordings_folders[0]["id"]

    # 회의명 서브폴더 탐색 (NFD/NFC 대응)
    meeting_folder_id = None
    for form in ("NFD", "NFC"):
        normalized = unicodedata.normalize(form, meeting_title)
        q2 = (f"name='{normalized}' "
              f"and '{recordings_id}' in parents "
              f"and mimeType='application/vnd.google-apps.folder' "
              f"and trashed=false")
        r2 = svc.files().list(q=q2, fields="files(id,name)").execute()
        folders = r2.get("files", [])
        if folders:
            meeting_folder_id = folders[0]["id"]
            break

    transcript_mime = "application/vnd.google-apps.document"

    if meeting_folder_id:
        # 서브폴더 내 탐색: 'Transcript' 또는 'Gemini가 작성한 회의록' 포함 파일
        q3 = (f"'{meeting_folder_id}' in parents "
              f"and mimeType='{transcript_mime}' "
              f"and (name contains 'Transcript' or name contains 'Gemini가 작성한 회의록') "
              f"and trashed=false")
    else:
        # 서브폴더 없음 → 루트 Meet Recordings에서 회의명 포함 파일 탐색
        # Gemini 회의록은 미팅 시작 직후 생성되므로 ended_after 필터 미적용
        # (제목 포함 + modifiedTime 내림차순으로 가장 최근 파일 반환)
        for form in ("NFD", "NFC"):
            normalized_title = unicodedata.normalize(form, meeting_title)
            q3 = (f"'{recordings_id}' in parents "
                  f"and mimeType='{transcript_mime}' "
                  f"and name contains '{normalized_title}' "
                  f"and trashed=false")
            r3 = svc.files().list(q=q3, fields="files(id,name,mimeType,modifiedTime)",
                                  orderBy="modifiedTime desc").execute()
            files = r3.get("files", [])
            if files:
                return files[0]
        return None

    if ended_after:
        ended_after_utc = ended_after.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        q3 += f" and modifiedTime > '{ended_after_utc}'"

    r3 = svc.files().list(q=q3, fields="files(id,name,mimeType,modifiedTime)",
                          orderBy="modifiedTime desc").execute()
    files = r3.get("files", [])
    return files[0] if files else None


def create_draft_doc(creds: Credentials, name: str, content: str, parent_id: str) -> str:
    """마크다운 텍스트로 편집 가능한 Google Docs 초안 생성. Returns: doc_id"""
    media = MediaInMemoryUpload(content.encode("utf-8"), mimetype="text/plain")
    svc = _service(creds)
    metadata = {
        "name": name,
        "parents": [parent_id],
        "mimeType": "application/vnd.google-apps.document",
    }
    file = svc.files().create(body=metadata, media_body=media, fields="id").execute()
    return file["id"]


def delete_file(creds: Credentials, file_id: str) -> None:
    """Drive 파일 삭제 (휴지통으로 이동)"""
    try:
        _service(creds).files().trash(fileId=file_id).execute()
    except Exception as e:
        import logging
        logging.getLogger(__name__).warning(f"파일 삭제 실패 (file_id={file_id}): {e}")


def save_minutes(creds: Credentials, minutes_folder_id: str,
                 filename: str, content: str) -> str:
    """회의록 저장. Returns: file_id"""
    return _write_file(creds, filename, content, minutes_folder_id)


def list_minutes(creds: Credentials, minutes_folder_id: str) -> list[dict]:
    """회의록 목록 조회. Returns: [{id, name, modifiedTime}]"""
    svc = _service(creds)
    q = f"'{minutes_folder_id}' in parents and trashed=false"
    result = svc.files().list(
        q=q, fields="files(id,name,modifiedTime)", orderBy="modifiedTime desc"
    ).execute()
    return result.get("files", [])


# ── Wiki 구조 헬퍼 (Phase 2.3) ──────────────────────────────────


def _ensure_sources_folder(creds: Credentials, contacts_folder_id: str,
                           subfolder: str) -> str:
    """Sources/{subfolder} 폴더 ID 반환 (없으면 생성).
    subfolder: 'Transcripts', 'Emails', 'Research'
    """
    # Sources 폴더 (contacts_folder_id와 같은 수준 — 루트에 생성)
    # contacts_folder_id의 부모를 조회하여 같은 위치에 생성
    svc = _service(creds)
    try:
        parent_resp = svc.files().get(fileId=contacts_folder_id, fields="parents").execute()
        root_id = parent_resp.get("parents", [None])[0]
    except Exception:
        root_id = None

    sources_id = create_folder(creds, "Sources", root_id)
    return create_folder(creds, subfolder, sources_id)


def save_source_file(creds: Credentials, contacts_folder_id: str,
                     subfolder: str, filename: str, content: str) -> str:
    """CM-10: Sources/{subfolder}/{filename} 에 원본 자료 저장. Returns: file_id"""
    folder_id = _ensure_sources_folder(creds, contacts_folder_id, subfolder)
    return _write_file(creds, filename, content, folder_id)


def append_meeting_history_company(
    creds: Credentials, contacts_folder_id: str,
    company_name: str, date_str: str, title: str,
    minutes_filename: str, attendee_names: list[str] = None,
) -> None:
    """CM-08: 기업 파일의 미팅 히스토리 테이블에 행 추가"""
    content, file_id, _ = get_company_info(creds, contacts_folder_id, company_name)
    if not content:
        return

    minutes_link = f"[[{minutes_filename}]]"
    attendee_links = ", ".join(f"[[{n}]]" for n in (attendee_names or []))
    new_row = f"| {date_str} | {title} | {minutes_link} | {attendee_links} |"

    if "## 미팅 히스토리" in content:
        # 테이블 헤더가 이미 있으면 바로 아래에 행 추가
        # 중복 체크: 같은 날짜+제목이면 건너뜀
        if f"| {date_str} | {title} |" in content:
            return
        # 테이블 헤더 행(|---|---| 등) 뒤에 삽입
        lines = content.split("\n")
        insert_idx = None
        in_history = False
        for i, line in enumerate(lines):
            if "## 미팅 히스토리" in line:
                in_history = True
            elif in_history and line.startswith("|---"):
                insert_idx = i + 1
                break
            elif in_history and line.startswith("| "):
                # 테이블 헤더 구분선 없이 바로 데이터 행인 경우
                insert_idx = i
                break
        if insert_idx is not None:
            lines.insert(insert_idx, new_row)
        else:
            # 테이블 구조가 없으면 섹션 바로 아래에 테이블 생성
            for i, line in enumerate(lines):
                if "## 미팅 히스토리" in line:
                    table_header = (
                        "| 날짜 | 주제 | 회의록 | 참석자 |\n"
                        "|------|------|--------|--------|\n"
                        f"{new_row}"
                    )
                    lines.insert(i + 1, table_header)
                    break
        content = "\n".join(lines)
    else:
        # 미팅 히스토리 섹션 새로 추가
        table = (
            "\n\n## 미팅 히스토리\n"
            "| 날짜 | 주제 | 회의록 | 참석자 |\n"
            "|------|------|--------|--------|\n"
            f"{new_row}\n"
        )
        content = content.rstrip() + table

    try:
        save_company_info(creds, contacts_folder_id, company_name, content, file_id)
        log.info(f"Wiki 미팅 히스토리 갱신 (기업): {company_name}")
    except Exception as e:
        log.warning(f"Wiki 미팅 히스토리 갱신 실패 ({company_name}): {e}")


def append_meeting_history_person(
    creds: Credentials, contacts_folder_id: str,
    person_name: str, date_str: str, title: str,
    minutes_filename: str,
) -> None:
    """CM-08: 인물 파일의 미팅 히스토리 테이블에 행 추가"""
    content, file_id = get_person_info(creds, contacts_folder_id, person_name)
    if not content:
        return

    minutes_link = f"[[{minutes_filename}]]"
    new_row = f"| {date_str} | {title} | {minutes_link} |"

    if "## 미팅 히스토리" in content:
        if f"| {date_str} | {title} |" in content:
            return
        lines = content.split("\n")
        insert_idx = None
        in_history = False
        for i, line in enumerate(lines):
            if "## 미팅 히스토리" in line:
                in_history = True
            elif in_history and line.startswith("|---"):
                insert_idx = i + 1
                break
            elif in_history and line.startswith("| "):
                insert_idx = i
                break
        if insert_idx is not None:
            lines.insert(insert_idx, new_row)
        else:
            for i, line in enumerate(lines):
                if "## 미팅 히스토리" in line:
                    table_header = (
                        "| 날짜 | 주제 | 회의록 |\n"
                        "|------|------|--------|\n"
                        f"{new_row}"
                    )
                    lines.insert(i + 1, table_header)
                    break
        content = "\n".join(lines)
    else:
        # 기존 after.py의 "## 미팅 이력" 형식도 지원
        if "## 미팅 이력" in content:
            history_line = f"- {date_str} {title} → {minutes_link}"
            content = content.replace(
                "## 미팅 이력",
                f"## 미팅 이력\n{history_line}",
                1,
            )
        else:
            table = (
                "\n\n## 미팅 히스토리\n"
                "| 날짜 | 주제 | 회의록 |\n"
                "|------|------|--------|\n"
                f"{new_row}\n"
            )
            content = content.rstrip() + table

    try:
        save_person_info(creds, contacts_folder_id, person_name, content, file_id)
        log.info(f"Wiki 미팅 히스토리 갱신 (인물): {person_name}")
    except Exception as e:
        log.warning(f"Wiki 미팅 히스토리 갱신 실패 ({person_name}): {e}")


def add_wiki_cross_references(
    creds: Credentials, contacts_folder_id: str,
    company_name: str, person_names: list[str],
) -> None:
    """CM-07: 기업 파일에 인물 [[링크]], 인물 파일에 기업 [[링크]] 상호 참조 삽입"""
    # 기업 파일에 주요 담당자 [[링크]] 추가
    if company_name and person_names:
        content, file_id, _ = get_company_info(creds, contacts_folder_id, company_name)
        if content:
            updated = False
            for name in person_names:
                wiki_link = f"[[{name}]]"
                if wiki_link not in content:
                    # "## 기본 정보" 섹션에 담당자 추가
                    if "주요 담당자:" in content:
                        # 기존 담당자 목록에 추가
                        import re
                        content = re.sub(
                            r"(주요 담당자:.+)",
                            rf"\1, {wiki_link}",
                            content,
                            count=1,
                        )
                    elif "## 기본 정보" in content:
                        content = content.replace(
                            "## 기본 정보",
                            f"## 기본 정보\n- 주요 담당자: {wiki_link}",
                            1,
                        )
                    updated = True
            if updated:
                try:
                    save_company_info(creds, contacts_folder_id, company_name, content, file_id)
                    log.info(f"Wiki 상호 참조 갱신 (기업→인물): {company_name}")
                except Exception as e:
                    log.warning(f"Wiki 상호 참조 실패 ({company_name}): {e}")

    # 인물 파일에 소속 기업 [[링크]] 추가
    if company_name:
        for name in (person_names or []):
            try:
                content, file_id = get_person_info(creds, contacts_folder_id, name)
                if not content:
                    continue
                company_link = f"[[{company_name}]]"
                if company_link not in content:
                    if "소속:" in content:
                        import re
                        # 기존 소속에 추가 (이미 다른 기업명이 있을 수 있음)
                        if company_name not in content.split("소속:")[1].split("\n")[0]:
                            content = re.sub(
                                r"(소속:.+)",
                                rf"\1, {company_link}",
                                content,
                                count=1,
                            )
                    elif "## 기본 정보" in content:
                        content = content.replace(
                            "## 기본 정보",
                            f"## 기본 정보\n- 소속: {company_link}",
                            1,
                        )
                    else:
                        content = f"- 소속: {company_link}\n" + content
                    save_person_info(creds, contacts_folder_id, name, content, file_id)
                    log.info(f"Wiki 상호 참조 갱신 (인물→기업): {name} → {company_name}")
            except Exception as e:
                log.warning(f"Wiki 상호 참조 실패 ({name}→{company_name}): {e}")


def add_minutes_backlinks(
    content: str, *,
    company_names: list[str] = None,
    attendee_names: list[str] = None,
    transcript_source: str = None,
    previous_minutes: list[str] = None,
    research_source: str = None,
) -> str:
    """CM-07: 회의록 하단에 관련 자료 역링크 섹션 추가. Returns: 수정된 content"""
    links = []
    for c in (company_names or []):
        links.append(f"- 업체: [[{c}]]")
    for a in (attendee_names or []):
        links.append(f"- 참석자: [[{a}]]")
    if transcript_source:
        links.append(f"- 원본 트랜스크립트: [[{transcript_source}]]")
    for m in (previous_minutes or []):
        links.append(f"- 이전 회의록: [[{m}]]")
    if research_source:
        links.append(f"- 브리핑 리서치: [[{research_source}]]")

    if not links:
        return content

    backlink_section = "\n\n## 관련 자료\n" + "\n".join(links) + "\n"
    return content.rstrip() + backlink_section
