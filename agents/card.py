"""명함 OCR 에이전트 — Slack DM 이미지 업로드 → Claude Vision → 인물정보 저장"""
import base64
import json
import logging
import os
import threading

import anthropic
import requests

log = logging.getLogger(__name__)

_claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
_OCR_MODEL = "claude-haiku-4-5"  # Vision 지원 모델 (명함 OCR에 충분)

# 사용자별 OCR 결과 임시 저장 {user_id: card_data}
_pending_cards: dict[str, dict] = {}


# ── 헬퍼 ────────────────────────────────────────────────────


def _post(slack_client, *, user_id: str, channel=None, thread_ts=None,
          text=None, blocks=None) -> dict:
    kwargs = {"channel": channel or user_id}
    if thread_ts:
        kwargs["thread_ts"] = thread_ts
    if blocks is not None:
        kwargs["blocks"] = blocks
    if text is not None:
        kwargs["text"] = text
    return slack_client.chat_postMessage(**kwargs)


def _detect_media_type(image_bytes: bytes) -> str:
    if image_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if image_bytes[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    if image_bytes[:4] in (b"GIF8", b"GIF9"):
        return "image/gif"
    return "image/jpeg"


# ── OCR ─────────────────────────────────────────────────────


def download_slack_image(url: str, bot_token: str) -> bytes:
    """Slack private URL에서 이미지 다운로드"""
    resp = requests.get(
        url,
        headers={"Authorization": f"Bearer {bot_token}"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.content


def ocr_business_card(image_bytes: bytes) -> dict:
    """Claude Vision으로 명함 OCR. Returns: 구조화된 명함 데이터 dict."""
    media_type = _detect_media_type(image_bytes)
    image_b64 = base64.standard_b64encode(image_bytes).decode("utf-8")

    prompt = """이 명함 이미지에서 모든 텍스트를 읽고 아래 JSON 형식으로만 반환해줘.
없는 필드는 빈 문자열("")로 표시하고, JSON 외 다른 텍스트는 절대 포함하지 마.

{
  "name": "이름 (한국어 또는 영문)",
  "company": "회사명",
  "title": "직책",
  "department": "부서",
  "email": "이메일",
  "phone": "전화번호",
  "mobile": "휴대폰",
  "fax": "팩스",
  "address": "주소",
  "website": "웹사이트",
  "sns": "SNS 계정"
}"""

    resp = _claude.messages.create(
        model=_OCR_MODEL,
        max_tokens=1024,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": image_b64,
                    },
                },
                {"type": "text", "text": prompt},
            ],
        }],
    )

    raw = resp.content[0].text.strip()
    # 마크다운 코드블록 제거
    raw = raw.lstrip("```json").lstrip("```").rstrip("```").strip()
    return json.loads(raw)


# ── Block Kit UI ─────────────────────────────────────────────


def _build_confirm_blocks(card_data: dict) -> list[dict]:
    """OCR 결과 확인 Block Kit 메시지"""
    field_map = [
        ("name",       "이름"),
        ("company",    "회사"),
        ("title",      "직책"),
        ("department", "부서"),
        ("email",      "이메일"),
        ("phone",      "전화"),
        ("mobile",     "휴대폰"),
        ("address",    "주소"),
        ("website",    "웹사이트"),
        ("sns",        "SNS"),
    ]
    lines = []
    for key, label in field_map:
        val = card_data.get(key, "").strip()
        if val:
            lines.append(f"*{label}:*  {val}")

    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "📇 *명함을 인식했습니다. 내용을 확인해주세요.*\n\n"
                        + "\n".join(lines),
            },
        },
        {"type": "divider"},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✅ 저장"},
                    "style": "primary",
                    "action_id": "card_confirm_save",
                    "value": "save",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✏️ 수정"},
                    "action_id": "card_open_edit",
                    "value": "edit",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "❌ 취소"},
                    "style": "danger",
                    "action_id": "card_cancel",
                    "value": "cancel",
                },
            ],
        },
    ]


def _input_block(block_id: str, action_id: str, label: str,
                 initial_value: str = "", required: bool = True) -> dict:
    # Slack은 initial_value가 빈 문자열이면 무시하므로 값 있을 때만 설정
    element: dict = {
        "type": "plain_text_input",
        "action_id": action_id,
    }
    val = (initial_value or "").strip()
    if val:
        element["initial_value"] = val

    block = {
        "type": "input",
        "block_id": block_id,
        "label": {"type": "plain_text", "text": label},
        "element": element,
    }
    if not required:
        block["optional"] = True
    return block


def open_edit_modal(slack_client, trigger_id: str, user_id: str):
    """수정 Modal 열기 — _pending_cards[user_id] 데이터로 초기값 채움"""
    card_data = _pending_cards.get(user_id, {})
    slack_client.views_open(
        trigger_id=trigger_id,
        view={
            "type": "modal",
            "callback_id": "card_edit_modal",
            "title": {"type": "plain_text", "text": "명함 정보 수정"},
            "submit": {"type": "plain_text", "text": "저장"},
            "close": {"type": "plain_text", "text": "취소"},
            "blocks": [
                _input_block("name_block",       "name_input",       "이름",     card_data.get("name", "")),
                _input_block("company_block",    "company_input",    "회사",     card_data.get("company", "")),
                _input_block("title_block",      "title_input",      "직책",     card_data.get("title", ""),      required=False),
                _input_block("department_block", "department_input", "부서",     card_data.get("department", ""), required=False),
                _input_block("email_block",      "email_input",      "이메일",   card_data.get("email", ""),      required=False),
                _input_block("phone_block",      "phone_input",      "전화",     card_data.get("phone", ""),      required=False),
                _input_block("mobile_block",     "mobile_input",     "휴대폰",   card_data.get("mobile", ""),     required=False),
                _input_block("website_block",    "website_input",    "웹사이트", card_data.get("website", ""),    required=False),
            ],
        },
    )


# ── 이벤트 핸들러 ────────────────────────────────────────────


def handle_image_upload(slack_client, user_id: str, file_info: dict):
    """DM에 이미지 업로드 시 명함 OCR 처리 (백그라운드)"""
    def _process():
        try:
            url = file_info.get("url_private") or file_info.get("url_private_download", "")
            if not url:
                return

            bot_token = os.getenv("SLACK_BOT_TOKEN", "")
            _post(slack_client, user_id=user_id, text="📇 명함을 분석 중입니다...")

            image_bytes = download_slack_image(url, bot_token)
            card_data = ocr_business_card(image_bytes)

            if not card_data.get("name") and not card_data.get("company"):
                _post(slack_client, user_id=user_id,
                      text="⚠️ 명함에서 정보를 읽지 못했습니다. 더 선명한 이미지로 다시 올려주세요.")
                return

            # 임시 저장 (저장/수정 버튼 클릭 시 사용)
            _pending_cards[user_id] = card_data

            blocks = _build_confirm_blocks(card_data)
            _post(slack_client, user_id=user_id,
                  blocks=blocks, text="📇 명함을 인식했습니다. 내용을 확인해주세요.")

        except Exception as e:
            log.error(f"명함 OCR 실패 ({user_id}): {e}", exc_info=True)
            _post(slack_client, user_id=user_id, text=f"⚠️ 명함 분석 실패: {e}")

    threading.Thread(target=_process, daemon=True).start()


def handle_confirm_save(slack_client, user_id: str):
    """✅ 저장 버튼 — _pending_cards[user_id] 데이터로 인물정보 저장"""
    def _save():
        from agents.before import research_person  # 순환 import 방지

        card_data = _pending_cards.pop(user_id, None)
        if not card_data:
            _post(slack_client, user_id=user_id,
                  text="⚠️ 저장할 명함 데이터가 없습니다. 명함 이미지를 다시 올려주세요.")
            return

        person_name = card_data.get("name", "").strip()
        company_name = card_data.get("company", "").strip()

        if not person_name:
            _post(slack_client, user_id=user_id, text="⚠️ 이름 정보가 없어 저장할 수 없습니다.")
            return

        _post(slack_client, user_id=user_id,
              text=f"💾 *{person_name}* ({company_name}) 인물정보 저장 중...\n웹 검색도 함께 진행합니다.")
        try:
            research_person(user_id, person_name, company_name,
                            force=True, card_data=card_data)
            msg = f"✅ *{person_name}* 인물정보가 저장되었습니다."
            if company_name:
                msg += f"\n명함 정보 + 웹 검색 결과가 함께 저장됐어요."
            _post(slack_client, user_id=user_id, text=msg)
        except Exception as e:
            log.error(f"인물정보 저장 실패 ({user_id}): {e}", exc_info=True)
            _post(slack_client, user_id=user_id, text=f"⚠️ 저장 실패: {e}")

    threading.Thread(target=_save, daemon=True).start()


def handle_edit_modal_submit(slack_client, user_id: str, view: dict):
    """✏️ 수정 Modal 제출 — 수정된 데이터로 저장"""
    values = view["state"]["values"]

    def _v(block_id, action_id):
        return (values.get(block_id, {}).get(action_id, {}).get("value") or "").strip()

    card_data = {
        "name":       _v("name_block",       "name_input"),
        "company":    _v("company_block",    "company_input"),
        "title":      _v("title_block",      "title_input"),
        "department": _v("department_block", "department_input"),
        "email":      _v("email_block",      "email_input"),
        "phone":      _v("phone_block",      "phone_input"),
        "mobile":     _v("mobile_block",     "mobile_input"),
        "website":    _v("website_block",    "website_input"),
    }
    # 수정된 데이터로 덮어쓰기 후 저장
    _pending_cards[user_id] = card_data
    handle_confirm_save(slack_client, user_id)
