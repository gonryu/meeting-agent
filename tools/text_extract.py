"""텍스트 파일 추출 — Slack 파일 업로드에서 텍스트 내용 추출"""
import logging
import requests

log = logging.getLogger(__name__)


def _decode_with_fallback(data: bytes, filename: str) -> str:
    """한글 텍스트 파일 인코딩 폴백 체인.

    Windows 메모장·녹음 솔루션 등이 만든 한글 .txt 는 cp949/euc-kr 인 경우가 많은데,
    UTF-8 strict 디코딩이 실패하면 한글이 통째로 � 로 치환되어 LLM 이 판독 실패한다.
    아래 순서로 시도하여 첫 성공본을 반환한다.
      1) UTF-8 BOM 보존 (utf-8-sig)
      2) UTF-8 strict
      3) charset_normalizer 자동 감지 (한글 우선)
      4) cp949 (= MS949, 한국어 Windows 기본)
      5) euc-kr
      6) UTF-8 + replace (최후 폴백)
    """
    # 1) BOM 우선 — utf-8-sig 는 UTF-8 도 그대로 처리
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        pass

    # 2) charset_normalizer 자동 감지 — requests 가 의존성으로 끌어와 항상 사용 가능
    try:
        from charset_normalizer import from_bytes
        match = from_bytes(data).best()
        if match is not None:
            encoding = match.encoding
            decoded = str(match)
            if decoded:
                log.info(f"인코딩 감지 ({filename}): {encoding}")
                return decoded
    except Exception as e:
        log.debug(f"charset_normalizer 감지 실패 ({filename}): {e}")

    # 3) 한국어 Windows 기본 인코딩 폴백
    for enc in ("cp949", "euc-kr"):
        try:
            decoded = data.decode(enc)
            log.info(f"인코딩 폴백 적용 ({filename}): {enc}")
            return decoded
        except UnicodeDecodeError:
            continue

    # 4) 최후 — UTF-8 replace (� 로 손실 발생)
    log.warning(f"인코딩 식별 실패 — UTF-8 replace 폴백 ({filename})")
    return data.decode("utf-8", errors="replace")

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
                text = _decode_with_fallback(resp.content, filename).strip()
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
