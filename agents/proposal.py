"""Proposal Agent — 제안서 워크플로우 (Phase 2.4)

동작 방식:
  1. 회의록 확정 후 키워드 감지 → Slack에 제안서 작성 제안 (FR-A11)
  2. 사용자 수락 시 intake 자동 추출 → 개요 제시 (FR-A12)
  3. 개요 확인 → 생성 → 수정 요청 루프 (FR-A13)
  4. Google Docs 공유 + 직접 편집 (FR-A14)
"""
import json
import logging
import os
import threading

import anthropic

from store import user_store
from tools import drive
from prompts.briefing import proposal_intake_prompt, proposal_generate_prompt

log = logging.getLogger(__name__)

_claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
_CLAUDE_MODEL = "claude-sonnet-4-5"  # 제안서는 고품질 모델 사용

# FR-A11: 제안서 트리거 키워드
_PROPOSAL_TRIGGERS = [
    "협업", "제안", "MOU", "PoC", "파일럿", "공동개발",
    "제휴", "투자", "계약", "도입", "검토", "다음 단계",
]

# 제안서 작성 대기 중인 상태
# { user_id: { event_id, title, date_str, internal_body, company_names,
#              attendees_raw, creds, contacts_folder_id, knowledge_file_id,
#              intake, outline_ts, draft_doc_id, proposal_body } }
_pending_proposals: dict[str, dict] = {}
_proposals_lock = threading.Lock()


# ── LLM 헬퍼 ───────────────────────────────────────────────────

def _generate(prompt: str) -> str:
    """Claude LLM 호출"""
    resp = _claude.messages.create(
        model=_CLAUDE_MODEL,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text.strip()


def _generate_proposal(prompt: str) -> str:
    """제안서 생성 전용 — Claude Sonnet 직접 사용 (고품질)"""
    resp = _claude.messages.create(
        model=_CLAUDE_MODEL,
        max_tokens=8192,
        messages=[{"role": "user", "content": prompt}],
    )
    return resp.content[0].text.strip()


# ── FR-A11: 트리거 감지 + 제안 ────────────────────────────────

def detect_and_suggest_proposal(
    slack_client, *,
    user_id: str,
    event_id: str | None,
    title: str,
    date_str: str,
    internal_body: str,
    company_names: list[str],
    attendees_raw: list[dict],
    creds,
) -> None:
    """회의록 본문에서 제안서 키워드 감지 시 Slack 제안 메시지 발송."""
    if not any(kw in internal_body for kw in _PROPOSAL_TRIGGERS):
        return

    # 감지된 키워드 목록
    detected = [kw for kw in _PROPOSAL_TRIGGERS if kw in internal_body]
    keywords_text = ", ".join(detected[:5])
    company_text = ", ".join(company_names) if company_names else title

    payload = json.dumps({
        "event_id": event_id,
        "title": title,
        "date_str": date_str,
    })

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"📝 *{title}* 회의록에서 제안서 관련 키워드가 감지되었습니다.\n"
                    f"_감지 키워드: {keywords_text}_\n\n"
                    f"*{company_text}*에 대한 제안서 초안을 만들어드릴까요?"
                ),
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "📝 제안서 작성"},
                    "style": "primary",
                    "action_id": "proposal_start",
                    "value": payload,
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "건너뛰기"},
                    "action_id": "proposal_skip",
                    "value": payload,
                },
            ],
        },
    ]

    slack_client.chat_postMessage(
        channel=user_id,
        text=f"📝 {title} — 제안서 작성을 제안합니다.",
        blocks=blocks,
    )

    # 상태 저장 (intake 추출 전 기본 정보)
    with _proposals_lock:
        _pending_proposals[user_id] = {
            "event_id": event_id,
            "title": title,
            "date_str": date_str,
            "internal_body": internal_body,
            "company_names": company_names,
            "attendees_raw": attendees_raw,
            "creds": creds,
            "contacts_folder_id": None,
            "knowledge_file_id": None,
            "intake": None,
            "outline_ts": None,
            "draft_doc_id": None,
            "proposal_body": None,
        }
        # 사용자 설정 조회
        try:
            user_info = user_store.get_user(user_id)
            if user_info:
                _pending_proposals[user_id]["contacts_folder_id"] = user_info.get("contacts_folder_id")
                _pending_proposals[user_id]["knowledge_file_id"] = user_info.get("knowledge_file_id")
        except Exception:
            pass


# ── FR-A12: Intake 추출 + 개요 제시 ───────────────────────────

def handle_proposal_start(slack_client, body: dict) -> None:
    """'제안서 작성' 버튼 핸들러 — intake 추출 후 개요 제시"""
    user_id = body["user"]["id"]

    with _proposals_lock:
        state = _pending_proposals.get(user_id)
    if not state:
        slack_client.chat_postMessage(
            channel=user_id, text="⚠️ 제안서 작성 상태를 찾을 수 없습니다.")
        return

    slack_client.chat_postMessage(
        channel=user_id, text="🔍 회의록에서 제안서 개요를 추출하고 있습니다...")

    # 맥락 자료 수집
    company_info = ""
    knowledge = ""
    creds = state["creds"]
    contacts_folder_id = state.get("contacts_folder_id")
    knowledge_file_id = state.get("knowledge_file_id")

    if creds and contacts_folder_id:
        for cn in (state.get("company_names") or []):
            try:
                info, _, _ = drive.get_company_info(creds, contacts_folder_id, cn)
                if info:
                    company_info += f"\n### {cn}\n{info}\n"
            except Exception:
                pass

    if creds and knowledge_file_id:
        try:
            knowledge = drive.get_company_knowledge(creds, knowledge_file_id)
        except Exception:
            pass

    # LLM으로 intake 추출
    try:
        prompt = proposal_intake_prompt(
            minutes_body=state["internal_body"],
            company_info=company_info,
            knowledge=knowledge,
        )
        result = _generate(prompt)
        cleaned = result.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        intake = json.loads(cleaned)
    except Exception as e:
        log.error(f"제안서 intake 추출 실패: {e}")
        slack_client.chat_postMessage(
            channel=user_id,
            text="⚠️ 제안서 개요 추출에 실패했습니다. 다시 시도해주세요.",
        )
        return

    # 상태 업데이트
    with _proposals_lock:
        if user_id in _pending_proposals:
            _pending_proposals[user_id]["intake"] = intake
            _pending_proposals[user_id]["company_info"] = company_info
            _pending_proposals[user_id]["knowledge"] = knowledge

    # 개요 제시
    _post_proposal_outline(slack_client, user_id=user_id, intake=intake)


def _post_proposal_outline(slack_client, *, user_id: str, intake: dict):
    """제안서 개요 제시 + 확인/수정/취소 버튼"""
    title = intake.get("title", "제안서")
    purpose = intake.get("purpose", "")
    target = intake.get("target", "")
    scope = intake.get("scope", "")
    key_points = intake.get("key_points", [])
    background = intake.get("background", "")

    key_points_text = "\n".join(f"  - {p}" for p in key_points) if key_points else "  - (없음)"

    outline_text = (
        f"■ *제목:* {title}\n"
        f"■ *목적:* {purpose}\n"
        f"■ *대상:* {target}\n"
        f"■ *범위:* {scope}\n"
        f"■ *주요 내용:*\n{key_points_text}\n"
        f"■ *배경:* {background}"
    )

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"📋 *아래 개요로 제안서를 작성하겠습니다:*\n\n{outline_text}",
            },
        },
        {"type": "divider"},
        {
            "type": "context",
            "elements": [{"type": "mrkdwn",
                          "text": "_수정할 부분이 있으면 '개요 수정' 후 스레드에서 수정 내용을 입력해주세요._"}],
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✅ 진행"},
                    "style": "primary",
                    "action_id": "proposal_confirm_outline",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✏️ 개요 수정"},
                    "action_id": "proposal_edit_outline",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "❌ 취소"},
                    "style": "danger",
                    "action_id": "proposal_cancel",
                },
            ],
        },
    ]

    resp = slack_client.chat_postMessage(
        channel=user_id,
        text=f"📋 제안서 개요가 준비되었습니다: {title}",
        blocks=blocks,
    )
    if resp and resp.get("ok"):
        with _proposals_lock:
            if user_id in _pending_proposals:
                _pending_proposals[user_id]["outline_ts"] = resp["ts"]


# ── FR-A13: 개요 확인 → 생성 → 수정 루프 ──────────────────────

def handle_proposal_confirm_outline(slack_client, body: dict) -> None:
    """개요 확인 → 제안서 생성"""
    user_id = body["user"]["id"]

    with _proposals_lock:
        state = _pending_proposals.get(user_id)
    if not state or not state.get("intake"):
        slack_client.chat_postMessage(
            channel=user_id, text="⚠️ 제안서 개요를 찾을 수 없습니다.")
        return

    slack_client.chat_postMessage(
        channel=user_id, text="📝 제안서를 생성하고 있습니다... (1~2분 소요)")

    intake = state["intake"]
    key_points_text = "\n".join(f"- {p}" for p in intake.get("key_points", []))

    try:
        prompt = proposal_generate_prompt(
            title=intake.get("title", "제안서"),
            purpose=intake.get("purpose", ""),
            target=intake.get("target", ""),
            scope=intake.get("scope", ""),
            key_points=key_points_text,
            background=intake.get("background", ""),
            minutes_body=state["internal_body"],
            company_info=state.get("company_info", ""),
            knowledge=state.get("knowledge", ""),
        )
        proposal_body = _generate_proposal(prompt)
    except Exception as e:
        log.error(f"제안서 생성 실패: {e}")
        slack_client.chat_postMessage(
            channel=user_id, text=f"⚠️ 제안서 생성 실패: {e}")
        return

    # 역링크 추가
    company_names = state.get("company_names", [])
    minutes_filename = f"{state['date_str']}_{state['title']}_내부용"
    backlinks = (
        f"\n\n---\n## 관련 자료\n"
        f"- 회의록: [[{minutes_filename}]]\n"
    )
    for cn in company_names:
        backlinks += f"- 업체 정보: [[{cn}]]\n"
    backlinks += f"- 생성일: {state['date_str']}\n"
    proposal_body += backlinks

    with _proposals_lock:
        if user_id in _pending_proposals:
            _pending_proposals[user_id]["proposal_body"] = proposal_body

    # FR-A14: Google Docs에 저장
    _save_and_post_proposal(slack_client, user_id=user_id)


def _save_and_post_proposal(slack_client, *, user_id: str):
    """Google Docs에 제안서 저장 후 Slack 발송"""
    with _proposals_lock:
        state = _pending_proposals.get(user_id)
    if not state or not state.get("proposal_body"):
        return

    creds = state.get("creds")
    proposal_body = state["proposal_body"]
    title = state.get("intake", {}).get("title", state.get("title", "제안서"))
    date_str = state["date_str"]
    company_names = state.get("company_names", [])
    company_label = "_".join(company_names) if company_names else "제안서"

    doc_id = None
    doc_link = None

    if creds:
        try:
            # Proposals 폴더 생성/조회
            contacts_folder_id = state.get("contacts_folder_id")
            if contacts_folder_id:
                # contacts_folder의 부모(루트)에 Proposals 폴더 생성
                svc = drive._service(creds)
                try:
                    parent_resp = svc.files().get(
                        fileId=contacts_folder_id, fields="parents"
                    ).execute()
                    root_id = parent_resp.get("parents", [None])[0]
                except Exception:
                    root_id = None
                proposals_folder_id = drive.create_folder(creds, "Proposals", root_id)
            else:
                proposals_folder_id = drive.create_folder(creds, "Proposals")

            filename = f"{date_str}_{company_label}_{title}"
            doc_id = drive.create_draft_doc(
                creds, filename, proposal_body, proposals_folder_id
            )
            doc_link = f"https://docs.google.com/document/d/{doc_id}/edit"
            log.info(f"제안서 Google Docs 생성: {doc_id}")

            with _proposals_lock:
                if user_id in _pending_proposals:
                    _pending_proposals[user_id]["draft_doc_id"] = doc_id

            # meeting_index has_proposal 플래그 갱신
            event_id = state.get("event_id")
            if event_id:
                try:
                    user_store.update_meeting_proposal_flag(event_id, user_id)
                except Exception as e:
                    log.warning(f"has_proposal 플래그 갱신 실패: {e}")

        except Exception as e:
            log.error(f"제안서 Google Docs 생성 실패: {e}")

    # 미리보기
    preview = proposal_body[:2500]
    if len(proposal_body) > 2500:
        preview += "\n\n_(이하 생략)_"

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"📄 제안서 초안: {title}"},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": preview},
        },
        {"type": "divider"},
    ]

    action_elements = [
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "✅ 완료"},
            "style": "primary",
            "action_id": "proposal_done",
        },
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "✏️ 수정 요청"},
            "action_id": "proposal_edit",
        },
    ]
    if doc_link:
        action_elements.insert(1, {
            "type": "button",
            "text": {"type": "plain_text", "text": "📝 직접 편집"},
            "url": doc_link,
            "action_id": "proposal_open_doc",
        })

    blocks.append({"type": "actions", "elements": action_elements})

    if doc_link:
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn",
                          "text": f"📄 <{doc_link}|Google Docs에서 직접 편집> 후 *완료*를 누르면 됩니다."}],
        })

    resp = slack_client.chat_postMessage(
        channel=user_id,
        text=f"📄 제안서 초안이 생성되었습니다: {title}",
        blocks=blocks,
    )
    if resp and resp.get("ok"):
        with _proposals_lock:
            if user_id in _pending_proposals:
                _pending_proposals[user_id]["draft_ts"] = resp["ts"]


def handle_proposal_edit_outline(slack_client, body: dict) -> None:
    """개요 수정 요청 — 스레드에서 수정 내용 입력 안내"""
    user_id = body["user"]["id"]

    with _proposals_lock:
        state = _pending_proposals.get(user_id)
    if not state:
        return

    outline_ts = state.get("outline_ts")
    resp = slack_client.chat_postMessage(
        channel=user_id,
        thread_ts=outline_ts,
        text="✏️ 수정할 내용을 이 스레드에 답글로 작성해주세요.\n"
             "예: '범위를 6개월로 변경', '대상에 마케팅팀도 추가해줘'",
    )
    if resp and resp.get("ok"):
        with _proposals_lock:
            if user_id in _pending_proposals:
                _pending_proposals[user_id]["edit_outline_ts"] = resp["ts"]


def handle_proposal_outline_edit_reply(slack_client, user_id: str, edit_text: str) -> None:
    """개요 수정 텍스트 반영 후 개요 재제시"""
    with _proposals_lock:
        state = _pending_proposals.get(user_id)
    if not state or not state.get("intake"):
        return

    intake = state["intake"]
    slack_client.chat_postMessage(
        channel=user_id, text="🔄 개요를 수정하고 있습니다...")

    # LLM으로 수정 반영
    edit_prompt = (
        f"다음 제안서 개요를 수정 요청에 따라 수정해줘.\n\n"
        f"[기존 개요]\n{json.dumps(intake, ensure_ascii=False, indent=2)}\n\n"
        f"[수정 요청]\n{edit_text}\n\n"
        f"수정된 결과를 동일한 JSON 형식으로만 반환 (설명 없이):\n"
        f'{{"title": "...", "purpose": "...", "target": "...", "scope": "...", '
        f'"key_points": [...], "background": "..."}}'
    )
    try:
        result = _generate(edit_prompt)
        cleaned = result.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        new_intake = json.loads(cleaned)
    except Exception as e:
        log.error(f"개요 수정 실패: {e}")
        slack_client.chat_postMessage(
            channel=user_id, text=f"⚠️ 개요 수정 실패: {e}")
        return

    with _proposals_lock:
        if user_id in _pending_proposals:
            _pending_proposals[user_id]["intake"] = new_intake

    _post_proposal_outline(slack_client, user_id=user_id, intake=new_intake)


def handle_proposal_edit(slack_client, body: dict) -> None:
    """생성된 제안서 수정 요청 — 스레드에서 수정 내용 입력 안내"""
    user_id = body["user"]["id"]

    with _proposals_lock:
        state = _pending_proposals.get(user_id)
    if not state:
        return

    draft_ts = state.get("draft_ts")
    resp = slack_client.chat_postMessage(
        channel=user_id,
        thread_ts=draft_ts,
        text="✏️ 수정할 내용을 이 스레드에 답글로 작성해주세요.\n"
             "예: '3단계 추진 방안을 더 구체적으로', '기대 성과에 ROI 추가'",
    )
    if resp and resp.get("ok"):
        with _proposals_lock:
            if user_id in _pending_proposals:
                _pending_proposals[user_id]["edit_draft_ts"] = resp["ts"]


def handle_proposal_edit_reply(slack_client, user_id: str, edit_text: str) -> None:
    """제안서 수정 텍스트로 재생성 후 새 초안 발송"""
    with _proposals_lock:
        state = _pending_proposals.get(user_id)
    if not state or not state.get("proposal_body"):
        return

    slack_client.chat_postMessage(
        channel=user_id, text="🔄 제안서를 수정하고 있습니다...")

    edit_prompt = (
        f"다음 제안서를 아래 수정 요청에 따라 수정해줘. 반드시 한국어로.\n\n"
        f"[기존 제안서]\n{state['proposal_body']}\n\n"
        f"[수정 요청]\n{edit_text}\n\n"
        f"수정 규칙:\n"
        f"1. 요청된 부분만 정확히 수정하고, 나머지 내용과 구조는 그대로 유지\n"
        f"2. 섹션 헤더(##)와 마크다운 형식을 동일하게 유지\n"
        f"3. 수정된 전체 제안서를 반환"
    )
    try:
        new_body = _generate_proposal(edit_prompt)
    except Exception as e:
        log.error(f"제안서 수정 실패: {e}")
        slack_client.chat_postMessage(
            channel=user_id, text=f"⚠️ 제안서 수정 실패: {e}")
        return

    with _proposals_lock:
        if user_id in _pending_proposals:
            _pending_proposals[user_id]["proposal_body"] = new_body

    # Google Docs 업데이트
    creds = state.get("creds")
    doc_id = state.get("draft_doc_id")
    if creds and doc_id:
        try:
            from googleapiclient.http import MediaInMemoryUpload
            media = MediaInMemoryUpload(new_body.encode("utf-8"), mimetype="text/plain")
            drive._service(creds).files().update(
                fileId=doc_id, media_body=media
            ).execute()
        except Exception as e:
            log.warning(f"Google Docs 업데이트 실패: {e}")

    _save_and_post_proposal(slack_client, user_id=user_id)


def handle_proposal_done(slack_client, body: dict) -> None:
    """제안서 완료 — 상태 정리"""
    user_id = body["user"]["id"]

    with _proposals_lock:
        state = _pending_proposals.pop(user_id, None)

    title = "제안서"
    if state:
        title = state.get("intake", {}).get("title", state.get("title", "제안서"))
        doc_id = state.get("draft_doc_id")
        if doc_id:
            doc_link = f"https://docs.google.com/document/d/{doc_id}/edit"
            slack_client.chat_postMessage(
                channel=user_id,
                text=f"✅ *{title}* 제안서가 완료되었습니다.\n📄 {doc_link}",
            )
            return

    slack_client.chat_postMessage(
        channel=user_id,
        text=f"✅ *{title}* 제안서가 완료되었습니다.",
    )


def handle_proposal_cancel(slack_client, body: dict) -> None:
    """제안서 취소"""
    user_id = body["user"]["id"]

    with _proposals_lock:
        state = _pending_proposals.pop(user_id, None)

    slack_client.chat_postMessage(
        channel=user_id,
        text="❌ 제안서 작성을 취소했습니다.",
    )


def handle_proposal_skip(slack_client, body: dict) -> None:
    """제안서 건너뛰기"""
    user_id = body["user"]["id"]

    with _proposals_lock:
        _pending_proposals.pop(user_id, None)

    # 별도 메시지 불필요 (건너뛰기)


def get_pending_proposal(user_id: str) -> dict | None:
    """사용자의 대기 중인 제안서 상태 조회 (main.py 스레드 감지용)"""
    return _pending_proposals.get(user_id)
