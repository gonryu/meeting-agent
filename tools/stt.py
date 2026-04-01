"""STT — OpenAI Whisper API를 이용한 음성→텍스트 변환"""
import os
import tempfile
import logging
import requests

log = logging.getLogger(__name__)

_SUPPORTED_MIMES = {
    "audio/mpeg", "audio/mp3", "audio/mp4", "audio/m4a", "audio/x-m4a",
    "audio/wav", "audio/x-wav", "audio/ogg", "audio/webm", "audio/aac",
    "video/mp4", "video/quicktime", "video/webm",
}
_MAX_BYTES = 25 * 1024 * 1024  # 25MB (Whisper API 제한)

_EXT_MAP = {
    "audio/mpeg": ".mp3", "audio/mp3": ".mp3",
    "audio/mp4": ".mp4", "audio/m4a": ".m4a", "audio/x-m4a": ".m4a",
    "audio/wav": ".wav", "audio/x-wav": ".wav",
    "audio/ogg": ".ogg", "audio/webm": ".webm",
    "audio/aac": ".aac",
    "video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm",
}


def is_audio(mime_type: str) -> bool:
    return mime_type.split(";")[0].strip() in _SUPPORTED_MIMES


def transcribe(file_url: str, slack_token: str, mime_type: str = "audio/mpeg",
               filename: str = "") -> str:
    """Slack 파일 URL에서 음성을 다운로드하여 Whisper API로 한국어 텍스트 변환.
    Returns: 변환된 텍스트
    Raises: ValueError on size limit, Exception on API failure
    """
    try:
        import openai
    except ImportError:
        raise RuntimeError("openai 패키지가 설치되지 않았습니다. pip install openai 를 실행해주세요.")

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY 환경변수가 설정되지 않았습니다.")

    # Slack에서 파일 다운로드
    resp = requests.get(
        file_url,
        headers={"Authorization": f"Bearer {slack_token}"},
        timeout=120,
    )
    resp.raise_for_status()

    size = len(resp.content)
    if size > _MAX_BYTES:
        raise ValueError(f"파일이 너무 큽니다 ({size // 1024 // 1024}MB). 최대 25MB까지 지원합니다.")

    # 확장자 결정 (Whisper는 확장자로 포맷 판단)
    mime_clean = mime_type.split(";")[0].strip()
    if filename:
        ext = os.path.splitext(filename)[1] or _EXT_MAP.get(mime_clean, ".mp3")
    else:
        ext = _EXT_MAP.get(mime_clean, ".mp3")

    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        tmp.write(resp.content)
        tmp_path = tmp.name

    try:
        client = openai.OpenAI(api_key=api_key)
        with open(tmp_path, "rb") as f:
            transcript = client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                language="ko",
            )
        log.info(f"Whisper STT 완료: {len(transcript.text)}자")
        return transcript.text.strip()
    finally:
        os.unlink(tmp_path)
