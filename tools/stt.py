"""STT — Deepgram API를 이용한 음성→텍스트 변환"""
import os
import logging
import warnings
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

log = logging.getLogger(__name__)

_SUPPORTED_MIMES = {
    "audio/mpeg", "audio/mp3", "audio/mp4", "audio/m4a", "audio/x-m4a",
    "audio/wav", "audio/x-wav", "audio/ogg", "audio/webm", "audio/aac",
    "video/mp4", "video/quicktime", "video/webm",
}
_MAX_BYTES = 500 * 1024 * 1024  # 500MB

_DEEPGRAM_API_KEY = os.getenv("DEEPGRAM_API_KEY", "")
_DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"


def is_audio(mime_type: str) -> bool:
    return mime_type.split(";")[0].strip() in _SUPPORTED_MIMES


def transcribe(file_url: str, slack_token: str, mime_type: str = "audio/mpeg",
               filename: str = "") -> str:
    """Slack 파일 URL에서 음성을 다운로드하여 Deepgram API로 텍스트 변환.
    Returns: 변환된 텍스트
    """
    if not _DEEPGRAM_API_KEY:
        raise ValueError("DEEPGRAM_API_KEY 환경변수가 설정되지 않았습니다.")

    # Slack에서 파일 다운로드
    resp = requests.get(
        file_url,
        headers={"Authorization": f"Bearer {slack_token}"},
        timeout=120,
    )
    resp.raise_for_status()

    size = len(resp.content)
    if size > _MAX_BYTES:
        raise ValueError(f"파일이 너무 큽니다 ({size // 1024 // 1024}MB). 최대 500MB까지 지원합니다.")

    mime_clean = mime_type.split(";")[0].strip()

    # Deepgram API 호출
    params = {
        "model": "nova-2",
        "language": "ko",
        "smart_format": "true",
        "punctuate": "true",
    }
    headers = {
        "Authorization": f"Token {_DEEPGRAM_API_KEY}",
        "Content-Type": mime_clean,
    }
    dg_resp = requests.post(
        _DEEPGRAM_URL,
        params=params,
        headers=headers,
        data=resp.content,
        timeout=300,
        verify=False,
    )
    dg_resp.raise_for_status()

    result = dg_resp.json()
    try:
        text = result["results"]["channels"][0]["alternatives"][0]["transcript"]
    except (KeyError, IndexError) as e:
        raise ValueError(f"Deepgram 응답 파싱 실패: {e} / 응답: {result}") from e

    text = text.strip()
    log.info(f"Deepgram STT 완료: {len(text)}자")
    return text
