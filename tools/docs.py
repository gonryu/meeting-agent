"""Google Docs API 래퍼 — 문서 읽기"""
import logging
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

log = logging.getLogger(__name__)


def _service(creds: Credentials):
    return build("docs", "v1", credentials=creds)


def read_document(creds: Credentials, doc_id: str) -> str:
    """Google Docs 문서 전문을 텍스트로 반환"""
    try:
        document = _service(creds).documents().get(documentId=doc_id).execute()
        return _extract_text(document)
    except Exception as e:
        log.error(f"Docs 읽기 실패 (doc_id={doc_id}): {e}")
        raise


def _extract_text(document: dict) -> str:
    """Docs 구조체에서 순수 텍스트 추출"""
    texts = []
    body = document.get("body", {})
    for element in body.get("content", []):
        paragraph = element.get("paragraph")
        if not paragraph:
            continue
        for pe in paragraph.get("elements", []):
            text_run = pe.get("textRun")
            if text_run:
                content = text_run.get("content", "")
                texts.append(content)
    return "".join(texts)
