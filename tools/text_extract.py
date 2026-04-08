"""텍스트 파일 추출 — Slack 파일 업로드에서 텍스트 내용 추출"""
import logging
import requests

log = logging.getLogger(__name__)

_TEXT_MIMES = {
    "text/plain",
    "text/markdown",
    "text/csv",
    "text/html",
    "text/rtf",
    "application/json",
}

# Slack이 plain_text로 변환 가능한 문서 타입
_DOCUMENT_MIMES = {
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",  # .docx
    "application/msword",  # .doc
    "application/vnd.oasis.opendocument.text",  # .odt
    "application/rtf",
}

_MAX_BYTES = 10 * 1024 * 1024  # 10MB


def is_text_document(mime_type: str) -> bool:
    """텍스트 파일 또는 문서 MIME 타입인지 확인"""
    mime_clean = mime_type.split(";")[0].strip()
    return mime_clean in _TEXT_MIMES or mime_clean in _DOCUMENT_MIMES


def extract_text(file_info: dict, slack_token: str) -> str:
    """Slack 파일에서 텍스트 추출.

    Slack API는 문서 파일에 대해 plain_text 버전을 제공하므로 이를 우선 활용.
    plain_text가 없으면 원본 파일을 직접 다운로드하여 텍스트로 읽기 시도.

    Returns: 추출된 텍스트 (빈 문자열이면 추출 실패)
    """
    filename = file_info.get("name", "document")
    mime = file_info.get("mimetype", "").split(";")[0].strip()

    # 1) Slack의 plain_text 변환 활용 (문서 파일 대응)
    plain_text_url = file_info.get("plain_text")
    if plain_text_url:
        try:
            resp = requests.get(
                plain_text_url,
                headers={"Authorization": f"Bearer {slack_token}"},
                timeout=60,
            )
            resp.raise_for_status()
            text = resp.text.strip()
            if text:
                log.info(f"Slack plain_text로 추출 성공: {filename} ({len(text)}자)")
                return text
        except Exception as e:
            log.warning(f"Slack plain_text 추출 실패 ({filename}): {e}")

    # 2) 원본 파일 다운로드하여 텍스트 읽기 (텍스트 MIME 타입만)
    if mime in _TEXT_MIMES:
        file_url = file_info.get("url_private_download") or file_info.get("url_private")
        if file_url:
            try:
                resp = requests.get(
                    file_url,
                    headers={"Authorization": f"Bearer {slack_token}"},
                    timeout=60,
                )
                resp.raise_for_status()
                if len(resp.content) > _MAX_BYTES:
                    log.warning(f"파일 크기 초과 ({filename}): {len(resp.content)} bytes")
                    return ""
                text = resp.content.decode("utf-8", errors="replace").strip()
                if text:
                    log.info(f"직접 다운로드 텍스트 추출: {filename} ({len(text)}자)")
                    return text
            except Exception as e:
                log.warning(f"파일 다운로드 실패 ({filename}): {e}")

    # 3) Slack의 preview 필드 활용 (부분 텍스트라도)
    preview = file_info.get("preview", "")
    if preview:
        log.info(f"Slack preview로 부분 추출: {filename} ({len(preview)}자)")
        return preview

    log.warning(f"텍스트 추출 실패: {filename} (mime={mime})")
    return ""
