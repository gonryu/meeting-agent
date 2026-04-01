"""STT — 로컬 Whisper 모델을 이용한 음성→텍스트 변환 (무료, API 키 불필요)"""
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
_MAX_BYTES = 500 * 1024 * 1024  # 500MB (로컬이므로 제한 완화)

_EXT_MAP = {
    "audio/mpeg": ".mp3", "audio/mp3": ".mp3",
    "audio/mp4": ".mp4", "audio/m4a": ".m4a", "audio/x-m4a": ".m4a",
    "audio/wav": ".wav", "audio/x-wav": ".wav",
    "audio/ogg": ".ogg", "audio/webm": ".webm",
    "audio/aac": ".aac",
    "video/mp4": ".mp4", "video/quicktime": ".mov", "video/webm": ".webm",
}

# 모델은 최초 사용 시 1회 다운로드 후 캐시됨 (~244MB for small)
# 한국어 품질: small < medium < large / 속도: small > medium > large
_WHISPER_MODEL_NAME = os.getenv("WHISPER_MODEL", "small")
_whisper_model = None


def _get_model():
    global _whisper_model
    if _whisper_model is None:
        import whisper
        log.info(f"Whisper 모델 로딩: {_WHISPER_MODEL_NAME}")
        _whisper_model = whisper.load_model(_WHISPER_MODEL_NAME)
        log.info("Whisper 모델 로딩 완료")
    return _whisper_model


def is_audio(mime_type: str) -> bool:
    return mime_type.split(";")[0].strip() in _SUPPORTED_MIMES


def transcribe(file_url: str, slack_token: str, mime_type: str = "audio/mpeg",
               filename: str = "") -> str:
    """Slack 파일 URL에서 음성을 다운로드하여 로컬 Whisper로 텍스트 변환.
    API 키 불필요. 최초 실행 시 모델 다운로드(~244MB).
    Returns: 변환된 텍스트
    """
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
    if filename:
        ext = os.path.splitext(filename)[1] or _EXT_MAP.get(mime_clean, ".mp3")
    else:
        ext = _EXT_MAP.get(mime_clean, ".mp3")

    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        tmp.write(resp.content)
        tmp_path = tmp.name

    try:
        model = _get_model()
        result = model.transcribe(tmp_path, language="ko")
        text = result["text"].strip()
        log.info(f"Whisper STT 완료: {len(text)}자")
        return text
    finally:
        os.unlink(tmp_path)
