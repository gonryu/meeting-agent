"""During 에이전트 — 미팅 중 노트 수집 및 사후 회의록 생성

동작 방식:
  - 트랜스크립트 폴링은 항상 실행 (수동 세션 여부와 무관)
  - /미팅종료 시 노트를 저장하고 즉시 1회 트랜스크립트 탐색 (백그라운드 스레드)
  - 폴러가 트랜스크립트 발견 시 노트와 결합하여 회의록 생성
  - 90분 경과 후 트랜스크립트 미수집 시 노트만으로 생성 (fallback)
  - 회의록은 내부용 / 외부용 2종 생성
  - 세션/노트는 .sessions/ 폴더에 JSON으로 백업 → 서버 재시작 시 자동 복구
"""
import json
import logging
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import os
import anthropic

from store import user_store
from tools import drive, docs, calendar as cal
from tools.slack_tools import format_time
from prompts.briefing import minutes_internal_prompt, minutes_external_prompt
from agents import after
from agents import minutes_orchestrator

log = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")

_claude = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
_CLAUDE_MODEL = "claude-haiku-4-5"
_CLAUDE_MINUTES_MODEL = "claude-sonnet-4-5"  # 회의록 생성 전용

# ── 세션 파일 저장 경로 ──────────────────────────────────────
_SESSIONS_DIR = Path(__file__).parent.parent / ".sessions"

# ── 상태 저장소 ───────────────────────────────────────────────

# 동시성 보호용 Lock (INF-07)
_sessions_lock = threading.Lock()   # _active_sessions, _completed_notes, _processed_events
_minutes_lock = threading.Lock()    # _pending_minutes
_inputs_lock = threading.Lock()     # _pending_inputs

# 진행 중인 수동 노트 세션
# { user_id: { title, started_at, notes, event_id } }
_active_sessions: dict[str, dict] = {}

# /미팅종료 후 폴러 대기 중인 노트
# { event_id: { user_id, title, notes, started_at, ended_at, stored_at } }
_completed_notes: dict[str, dict] = {}

# 트랜스크립트 처리 완료 이벤트 (중복 방지)
# { user_id: set(event_id) }
_processed_events: dict[str, set] = {}

# 노트만으로 회의록 생성 후, 트랜스크립트 도착 시 보강 대기
# { event_id: { user_id, title, date_str, time_range, attendees,
#               notes_text, minutes_folder_id, attendees_raw, created_at } }
_awaiting_transcript: dict[str, dict] = {}

# 회의록 검토 대기 중인 초안 (FR-D14: event_id 키 사용)
# { event_id: { user_id, title, date_str, time_range, attendees, source_label,
#               transcript_text, notes_text, internal_body, external_body,
#               minutes_folder_id, creds, event_id, attendees_raw,
#               draft_ts, channel } }
_pending_minutes: dict[str, dict] = {}

# 이벤트 선택 대기 중인 입력 (캘린더 이벤트가 0개 또는 여러 개일 때)
# { user_id: { inputs: [{ type: "note"|"audio"|"document", content: str }],
#              events: [parsed_event, ...], prompt_ts: str } }
_pending_inputs: dict[str, dict] = {}

# I1: /미팅종료 직후 "회의록 생성 방식 선택" 대기 payload. key = event_id
_pending_source_select: dict[str, dict] = {}

# I1+: 사용자가 "📎 트랜스크립트 첨부"를 선택한 후 파일 업로드 대기 상태. key = user_id
# { user_id: { event_id, title, notes, started_at, ended_at, post_channel,
#              post_thread_ts, created_at(KST datetime) } }
# 30분 경과 시 자동 만료(_handle_text_upload·신규 /미팅종료 시 정리).
_pending_uploaded_transcript: dict[str, dict] = {}

# 사후 회의록 복구 — /미팅종료 시 활성 세션이 없을 때 최근 종료된 캘린더 이벤트 후보
# { user_id: { events: [parsed_event, ...], session_channel, session_thread_ts } }
_pending_recovery: dict[str, dict] = {}


# ── 세션 파일 저장/복구 헬퍼 ─────────────────────────────────


def _ensure_sessions_dir():
    _SESSIONS_DIR.mkdir(exist_ok=True)


def _save_active_session(user_id: str):
    """진행 중인 세션을 JSON 파일로 저장"""
    data = _active_sessions.get(user_id)
    if data is None:
        return
    try:
        _ensure_sessions_dir()
        path = _SESSIONS_DIR / f"active_{user_id}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning(f"세션 파일 저장 실패 ({user_id}): {e}")


def _delete_active_session_file(user_id: str):
    """진행 중인 세션 파일 삭제"""
    try:
        (_SESSIONS_DIR / f"active_{user_id}.json").unlink(missing_ok=True)
    except Exception as e:
        log.warning(f"세션 파일 삭제 실패 ({user_id}): {e}")


def _save_completed_note(event_id: str):
    """완료된 노트를 JSON 파일로 저장 (stored_at datetime → ISO string)"""
    data = _completed_notes.get(event_id)
    if data is None:
        return
    try:
        _ensure_sessions_dir()
        path = _SESSIONS_DIR / f"completed_{event_id}.json"
        serializable = {**data, "stored_at": data["stored_at"].isoformat()}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(serializable, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning(f"완료 노트 파일 저장 실패 ({event_id}): {e}")


def _delete_completed_note_file(event_id: str):
    """완료된 노트 파일 삭제"""
    try:
        (_SESSIONS_DIR / f"completed_{event_id}.json").unlink(missing_ok=True)
    except Exception as e:
        log.warning(f"완료 노트 파일 삭제 실패 ({event_id}): {e}")


def _save_processed_events(user_id: str):
    """처리 완료 이벤트 목록을 JSON 파일로 저장"""
    events = _processed_events.get(user_id)
    if not events:
        return
    try:
        _ensure_sessions_dir()
        path = _SESSIONS_DIR / f"processed_{user_id}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(list(events), f, ensure_ascii=False)
    except Exception as e:
        log.warning(f"처리 완료 이벤트 저장 실패 ({user_id}): {e}")


def _load_sessions():
    """서버 시작 시 .sessions/ 폴더에서 세션·노트 복구"""
    if not _SESSIONS_DIR.exists():
        return

    # 진행 중인 세션 복구
    for path in _SESSIONS_DIR.glob("active_*.json"):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            user_id = path.stem[len("active_"):]
            _active_sessions[user_id] = data
            log.info(f"세션 복구: {user_id} ({data.get('title')}, "
                     f"노트 {len(data.get('notes', []))}개)")
        except Exception as e:
            log.warning(f"세션 파일 로드 실패 ({path}): {e}")

    # 완료된 노트 복구
    for path in _SESSIONS_DIR.glob("completed_*.json"):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            event_id = path.stem[len("completed_"):]
            data["stored_at"] = datetime.fromisoformat(data["stored_at"])
            _completed_notes[event_id] = data
            log.info(f"완료 노트 복구: {event_id} ({data.get('title')}, "
                     f"노트 {len(data.get('notes', []))}개)")
        except Exception as e:
            log.warning(f"완료 노트 파일 로드 실패 ({path}): {e}")

    # 처리 완료 이벤트 복구
    for path in _SESSIONS_DIR.glob("processed_*.json"):
        try:
            with open(path, encoding="utf-8") as f:
                event_ids = json.load(f)
            user_id = path.stem[len("processed_"):]
            _processed_events[user_id] = set(event_ids)
            log.info(f"처리 완료 이벤트 복구: {user_id} ({len(event_ids)}개)")
        except Exception as e:
            log.warning(f"처리 완료 이벤트 파일 로드 실패 ({path}): {e}")


# ── _pending_minutes 파일 영속화 (INF-09) ────────────────────


def _save_pending_minutes():
    """_pending_minutes를 JSON 파일로 저장 (creds 등 직렬화 불가 객체 제외)"""
    try:
        _ensure_sessions_dir()
        serializable = {}
        for key, val in _pending_minutes.items():
            serializable[key] = {
                k: v for k, v in val.items()
                if k != "creds"  # Credentials 객체 제외
            }
        with open(_SESSIONS_DIR / "pending_minutes.json", "w", encoding="utf-8") as f:
            json.dump(serializable, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning(f"pending_minutes 저장 실패: {e}")


def _load_pending_minutes() -> dict:
    """서버 시작 시 pending_minutes.json에서 복구"""
    path = _SESSIONS_DIR / "pending_minutes.json"
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, Exception) as e:
        log.warning(f"pending_minutes 로드 실패: {e}")
        return {}


# 모듈 로드 시 자동 복구
_load_sessions()
_pending_minutes.update(_load_pending_minutes())


# ── LLM 헬퍼 ─────────────────────────────────────────────────


def _generate(prompt: str) -> str:
    """텍스트 생성 — Claude"""
    msg = _claude.messages.create(
        model=_CLAUDE_MODEL,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


# ── 회의록 품질 검증 (FR-D09, FR-D10) ────────────────────────

# 내부용 필수 섹션 — docs/requirements.md L400 표준 (5섹션):
#   회의 요약 / 주요 결정 사항 / 액션 아이템 / 주요 논의 내용 / 내부 메모
# Orchestrator 의 옛 7섹션 양식("회의 개요/결론/액션아이템/...")도 폴백으로 인정하여
# 기존에 저장된 회의록 검증 호환성을 유지한다.
_INTERNAL_REQUIRED_SECTIONS = ["회의 요약", "주요 결정 사항", "액션 아이템", "주요 논의 내용", "내부 메모"]
_INTERNAL_LEGACY_ALIASES = {
    "회의 요약": ["회의 요약", "회의 개요"],
    "주요 결정 사항": ["주요 결정 사항", "결론"],
    "액션 아이템": ["액션 아이템", "액션아이템"],
    "주요 논의 내용": ["주요 논의 내용"],
    "내부 메모": ["내부 메모", "신뢰도 및 검토 메모", "검토 메모"],
}

# 외부용 필수 섹션 (Obsidian 10섹션 양식 + 레거시 양식 모두 허용)
# 신규: ## 1. 회의 개요 / ## 2. 결론 / ## 5. Action Items / ## 10. 신뢰도 및 검토 메모
# 레거시: ## 회의 개요 / ## 주요 합의 사항 / ## 공동 액션 아이템
# "신뢰도" 는 권장 섹션 — 누락 시 hard fail 대신 warnings 로 처리하여 기존 회의록과 호환.
_EXTERNAL_REQUIRED_SECTIONS = ["회의 개요", "결론", "Action Items"]
_EXTERNAL_RECOMMENDED_SECTIONS = ["신뢰도"]
_EXTERNAL_LEGACY_ALIASES = {
    "회의 개요": ["1. 회의 개요", "회의 개요"],
    "결론": ["2. 결론", "결론", "주요 합의 사항"],
    "Action Items": ["5. Action Items", "Action Items", "액션 아이템", "공동 액션 아이템"],
    "신뢰도": ["10. 신뢰도", "신뢰도"],
}
# 외부용 금지 키워드
_EXTERNAL_FORBIDDEN_KEYWORDS = ["내부 메모", "협상", "전략"]


def _section_present(body_lower: str, candidates: list[str]) -> bool:
    """후보 섹션 헤더 중 하나라도 본문에 존재하면 True. (## 헤더 기준)"""
    for c in candidates:
        c = c.lower()
        if f"## {c}" in body_lower or f"##{c}" in body_lower:
            return True
    return False


def validate_minutes(body: str, minute_type: str) -> dict:
    """회의록 필수항목 검증 (Obsidian 신규/레거시 양쪽 양식 지원).

    Args:
        body: 회의록 본문 (마크다운)
        minute_type: "internal" 또는 "external"

    Returns:
        {"valid": bool, "missing": [...], "forbidden": [...], "warnings": [...]}
    """
    result = {"valid": True, "missing": [], "forbidden": [], "warnings": []}

    body_lower = body.lower() if body else ""

    if minute_type == "internal":
        for canonical in _INTERNAL_REQUIRED_SECTIONS:
            cands = _INTERNAL_LEGACY_ALIASES.get(canonical, [canonical])
            if not _section_present(body_lower, cands):
                result["missing"].append(canonical)
    else:
        for canonical in _EXTERNAL_REQUIRED_SECTIONS:
            cands = _EXTERNAL_LEGACY_ALIASES.get(canonical, [canonical])
            if not _section_present(body_lower, cands):
                result["missing"].append(canonical)
        # 권장 섹션은 누락 시 warnings 로만 기록 (hard fail 아님)
        for canonical in _EXTERNAL_RECOMMENDED_SECTIONS:
            cands = _EXTERNAL_LEGACY_ALIASES.get(canonical, [canonical])
            if not _section_present(body_lower, cands):
                result["warnings"].append(f"권장 섹션 누락: {canonical}")

    # 외부용 금지 키워드 검출
    if minute_type == "external":
        for keyword in _EXTERNAL_FORBIDDEN_KEYWORDS:
            if keyword in body:
                result["forbidden"].append(keyword)

    # 권장 사항 검증
    if body and len(body) < 500 and minute_type == "internal":
        result["warnings"].append("본문이 500자 미만입니다")

    if result["missing"] or result["forbidden"]:
        result["valid"] = False

    return result


_MINUTES_SYSTEM_PROMPT = """\
당신은 전문 회의록 작성자입니다. 다음 원칙을 반드시 준수하세요:

1. **사실 기반 작성**: 제공된 트랜스크립트·노트에 실제로 언급된 내용만 작성합니다. 유추·추론·창작은 절대 금지합니다.
2. **발언자 구분**: 트랜스크립트에 발언자 정보가 있으면 "누가 무엇을 말했는지"를 명확히 반영합니다.
3. **구조화**: 논의 주제별로 정리하되, 시간순 흐름도 반영합니다. 단순 나열이 아닌, 맥락이 이어지도록 작성합니다.
4. **액션아이템 정확성**: 담당자·기한이 명시된 경우 반드시 포함합니다. 명시되지 않은 것은 추측하지 않습니다.
5. **불명확한 내용 처리**: 들리지 않거나 불분명한 부분은 "(불명확)" 또는 "[음성 불명확]"으로 표시합니다.
6. **분량 조절**: 회의 길이와 내용 밀도에 비례하여 작성합니다. 30분 미팅은 간결하게, 2시간 미팅은 상세하게.
"""


def _generate_minutes(prompt: str) -> str:
    """회의록 생성 전용 — Claude Sonnet + 시스템 프롬프트 사용"""
    msg = _claude.messages.create(
        model=_CLAUDE_MINUTES_MODEL,
        max_tokens=8192,
        system=_MINUTES_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


def _summarize_transcript_chunk(chunk: str, chunk_idx: int, total_chunks: int,
                                 meeting_title: str) -> str:
    """긴 트랜스크립트의 개별 청크를 요약"""
    prompt = (
        f"다음은 '{meeting_title}' 회의 트랜스크립트의 {total_chunks}개 파트 중 "
        f"{chunk_idx + 1}번째입니다.\n\n"
        f"핵심 내용을 발언자 구분하여 요약해주세요. "
        f"결정사항, 액션아이템, 주요 논의 내용 위주로 정리하되, "
        f"원문의 구체적 수치·이름·날짜는 그대로 보존하세요.\n\n"
        f"[트랜스크립트 파트 {chunk_idx + 1}/{total_chunks}]\n{chunk}"
    )
    msg = _claude.messages.create(
        model=_CLAUDE_MODEL,
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text.strip()


def _preprocess_transcript(transcript: str, meeting_title: str) -> str:
    """긴 트랜스크립트를 청크별 요약 후 통합. 30,000자 이하면 그대로 반환."""
    if len(transcript) <= 30000:
        return transcript

    log.info(f"긴 트랜스크립트 감지 ({len(transcript):,}자), 청크별 요약 진행: {meeting_title}")

    # 약 20,000자씩 분할 (문단 경계 기준)
    chunk_size = 20000
    chunks = []
    current = 0
    while current < len(transcript):
        end = min(current + chunk_size, len(transcript))
        if end < len(transcript):
            # 줄바꿈 경계에서 자르기
            newline_pos = transcript.rfind("\n", current, end)
            if newline_pos > current + chunk_size // 2:
                end = newline_pos + 1
        chunks.append(transcript[current:end])
        current = end

    # 각 청크 요약
    summaries = []
    for i, chunk in enumerate(chunks):
        summary = _summarize_transcript_chunk(chunk, i, len(chunks), meeting_title)
        summaries.append(f"### 파트 {i + 1}/{len(chunks)}\n{summary}")

    combined = "\n\n".join(summaries)
    log.info(f"트랜스크립트 요약 완료: {len(transcript):,}자 → {len(combined):,}자 ({len(chunks)}파트)")
    return combined


def _get_creds_and_config(user_id: str):
    creds = user_store.get_credentials(user_id)
    user = user_store.get_user(user_id)
    return creds, user.get("minutes_folder_id")


def _post(slack_client, *, user_id: str, channel: str = None,
          thread_ts: str = None, text: str = None, blocks=None):
    slack_client.chat_postMessage(
        channel=channel or user_id,
        thread_ts=thread_ts,
        text=text or "",
        blocks=blocks,
    )


def _hint(text: str) -> str:
    """공통 디스커버리 힌트 푸터 — 메시지 하단에 한 줄 이탤릭으로 부가."""
    return f"\n_💡 {text}_"


def get_session_thread(user_id: str) -> tuple[str, str] | None:
    """사용자의 활성 세션이 채널 쓰레드에서 시작된 경우 (channel, thread_ts) 반환."""
    session = _active_sessions.get(user_id)
    if not session:
        return None
    ch = session.get("session_channel")
    ts = session.get("session_thread_ts")
    if ch and ts:
        return (ch, ts)
    return None


def _find_draft_for_user(user_id: str) -> tuple[str, dict] | None:
    """FR-D14: user_id로 _pending_minutes에서 초안 역방향 조회.
    Returns (event_id_key, draft_dict) 또는 None."""
    for eid, draft in _pending_minutes.items():
        if draft.get("user_id") == user_id:
            return (eid, draft)
    return None


def find_draft_by_thread_ts(user_id: str, thread_ts: str) -> tuple[str, dict] | None:
    """(B3) 스레드 ts와 정확히 일치하는 초안을 찾아서 반환.
    draft_ts 또는 edit_prompt_ts 중 하나라도 일치하면 그 초안.
    복수 초안이 있을 때 엉뚱한 회의록에 수정이 반영되는 문제 방지.
    Returns (event_id_key, draft_dict) 또는 None."""
    if not thread_ts:
        return None
    for eid, draft in _pending_minutes.items():
        if draft.get("user_id") != user_id:
            continue
        if draft.get("draft_ts") == thread_ts or draft.get("edit_prompt_ts") == thread_ts:
            return (eid, draft)
    return None


# ── 캘린더 이벤트 자동 감지 ─────────────────────────────────────


def _find_candidate_events(creds) -> dict:
    """캘린더 이벤트 자동 감지.

    Returns:
        {
          "ongoing": [진행 중 이벤트],              # start <= now <= end
          "upcoming": [30분 내 시작 예정 이벤트],    # now < start <= now+30min
          "nearby": [가장 가까운 이벤트들],           # 진행중·예정 없을 때, 비슷한 거리의 이벤트들
          "nearby_distance_min": int,              # nearby 이벤트까지 분 단위 거리
        }
    """
    now = datetime.now(KST)
    try:
        events = cal.get_upcoming_meetings(creds, days=1)
    except Exception as e:
        log.warning(f"캘린더 이벤트 조회 실패: {e}")
        return {"ongoing": [], "upcoming": [], "nearby": [], "nearby_distance_min": 0}

    ongoing = []
    upcoming = []
    all_parsed = []
    for ev in events:
        parsed = cal.parse_event(ev)
        start_str = parsed.get("start_time", "")
        end_str = ev.get("end", {}).get("dateTime", "")
        if not start_str or not end_str:
            continue
        try:
            start_dt = datetime.fromisoformat(start_str)
            end_dt = datetime.fromisoformat(end_str)
        except Exception:
            continue

        parsed["_start_dt"] = start_dt
        parsed["_end_dt"] = end_dt
        parsed["_end_time"] = end_str
        parsed["_raw_event"] = ev
        all_parsed.append(parsed)

        if start_dt <= now <= end_dt:
            ongoing.append(parsed)
        elif now < start_dt <= now + timedelta(minutes=30):
            upcoming.append(parsed)

    if ongoing:
        return {"ongoing": ongoing, "upcoming": [], "nearby": [], "nearby_distance_min": 0}

    if upcoming:
        return {"ongoing": [], "upcoming": upcoming, "nearby": [], "nearby_distance_min": 0}

    # 진행 중·예정 없음 → 앞뒤로 가장 가까운 이벤트(들) 찾기
    if not all_parsed:
        return {"ongoing": [], "upcoming": [], "nearby": [], "nearby_distance_min": 0}

    def _event_distance(parsed):
        start_dt = parsed["_start_dt"]
        end_dt = parsed["_end_dt"]
        if now < start_dt:
            return (start_dt - now).total_seconds() / 60
        elif now > end_dt:
            return (now - end_dt).total_seconds() / 60
        return 0

    # 거리순 정렬
    sorted_events = sorted(all_parsed, key=_event_distance)
    min_dist = _event_distance(sorted_events[0])

    # 가장 가까운 이벤트와 ±15분 이내의 이벤트들을 모두 포함 (같은 시간대 이벤트 묶기)
    nearby = []
    for ev in sorted_events:
        dist = _event_distance(ev)
        if dist <= min_dist + 15:
            nearby.append(ev)
        else:
            break

    return {
        "ongoing": [],
        "upcoming": [],
        "nearby": nearby,
        "nearby_distance_min": int(min_dist),
    }


def _start_session_with_event(slack_client, user_id: str, event: dict):
    """파싱된 캘린더 이벤트로 세션 자동 시작. 이미 세션이 있으면 무시."""
    if user_id in _active_sessions:
        return

    event_id = event["id"]
    event_summary = event["summary"]
    start_str = event.get("start_time", "")
    end_str = event.get("_end_time", "")
    event_time_str = ""
    try:
        event_time_str = f"{format_time(start_str)} ~ {format_time(end_str)}"
    except Exception:
        pass

    _active_sessions[user_id] = {
        "title": event_summary,
        "started_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
        "notes": [],
        "event_id": event_id,
        "event_summary": event_summary,
        "event_time_str": event_time_str,
    }
    _save_active_session(user_id)

    event_line = f"\n📅 연동된 일정: *{event_summary}*" + (f" ({event_time_str})" if event_time_str else "")
    _post(slack_client, user_id=user_id,
          text=f"✅ 자동으로 세션을 시작했습니다: *{event_summary}*{event_line}\n"
               f"미팅이 끝나면 `/미팅종료` 를 입력해주세요.")


def _prompt_event_confirm(slack_client, user_id: str, event: dict, distance_min: int):
    """가장 가까운 이벤트를 보여주고 맞는지 확인 요청."""
    summary = event["summary"]
    start_str = event.get("start_time", "")
    time_display = ""
    try:
        time_display = format_time(start_str)
    except Exception:
        pass

    if distance_min <= 0:
        distance_text = "현재 진행 중"
    else:
        distance_text = f"약 {distance_min}분 {'후 시작' if event.get('_start_dt', datetime.now(KST)) > datetime.now(KST) else '전 종료'}"

    buttons = [
        {
            "type": "button",
            "text": {"type": "plain_text", "text": f"✅ 맞습니다"},
            "action_id": "select_meeting_event_0",
            "value": event["id"],
            "style": "primary",
        },
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "📝 아닙니다 (새 미팅)"},
            "action_id": "select_meeting_event_new",
        },
    ]
    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn",
                     "text": f"📅 가장 가까운 일정: *{summary}* ({time_display}, {distance_text})\n이 미팅에 대한 기록이 맞나요?"},
        },
        {"type": "actions", "elements": buttons},
    ]
    resp = slack_client.chat_postMessage(
        channel=user_id,
        text=f"가장 가까운 일정 '{summary}'에 대한 기록이 맞나요?",
        blocks=blocks,
    )
    pending = _pending_inputs.get(user_id, {})
    pending["prompt_ts"] = resp["ts"] if resp and resp.get("ok") else None


def _prompt_event_selection(slack_client, user_id: str, events: list[dict],
                             custom_title: str | None = None):
    """여러 캘린더 이벤트 중 선택하도록 Slack 버튼 발송.

    custom_title: 사용자가 /미팅시작 시 입력한 제목. "새 미팅 추가" 버튼 레이블에
    반영됨. 있으면 클릭 즉시 ad-hoc 세션으로 진입 (스레드 답글 요청 생략).
    """
    buttons = []
    for i, ev in enumerate(events[:5]):  # 최대 5개
        summary = ev["summary"]
        start_str = ev.get("start_time", "")
        time_display = ""
        try:
            time_display = f" ({format_time(start_str)})"
        except Exception:
            pass
        buttons.append({
            "type": "button",
            "text": {"type": "plain_text", "text": f"{summary}{time_display}"[:75]},
            "action_id": f"select_meeting_event_{i}",
            "value": ev["id"],
        })

    # "새 미팅 추가" 옵션 — custom_title이 있으면 그 제목을 버튼에 표시
    new_label = (
        f'📝 새 미팅 추가 ("{custom_title}")'[:75]
        if custom_title else "📝 새 미팅으로 기록"
    )
    buttons.append({
        "type": "button",
        "text": {"type": "plain_text", "text": new_label},
        "action_id": "select_meeting_event_new",
    })

    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn",
                     "text": "📅 어떤 미팅에 대한 기록인가요?"},
        },
        {"type": "actions", "elements": buttons},
    ]
    resp = slack_client.chat_postMessage(
        channel=user_id,
        text="어떤 미팅에 대한 기록인지 선택해주세요.",
        blocks=blocks,
    )
    pending = _pending_inputs.get(user_id, {})
    pending["prompt_ts"] = resp["ts"] if resp and resp.get("ok") else None


def _auto_start_or_enqueue(slack_client, user_id: str, input_item: dict):
    """세션 없이 입력이 들어왔을 때: 캘린더 이벤트 자동 감지 → 세션 시작 또는 확인 요청.

    input_item: { "type": "note"|"audio"|"document", "content": str }
    Returns: True면 세션 시작됨, False면 대기 큐에 저장됨.
    """
    # 이미 이벤트 선택 대기 중이면 큐에 추가만
    if user_id in _pending_inputs:
        _pending_inputs[user_id]["inputs"].append(input_item)
        _post(slack_client, user_id=user_id,
              text=f"📝 메모가 대기열에 추가되었습니다. 위의 미팅을 먼저 선택해주세요.")
        return False

    try:
        creds, _ = _get_creds_and_config(user_id)
    except Exception as e:
        _post(slack_client, user_id=user_id, text=f"⚠️ 인증 오류: {e}")
        return False

    result = _find_candidate_events(creds)
    ongoing = result["ongoing"]
    upcoming = result["upcoming"]
    nearby = result["nearby"]
    nearby_dist = result["nearby_distance_min"]

    # 1) 진행 중 이벤트
    if len(ongoing) == 1:
        _start_session_with_event(slack_client, user_id, ongoing[0])
        return True
    elif len(ongoing) > 1:
        _pending_inputs[user_id] = {"inputs": [input_item], "events": ongoing}
        _prompt_event_selection(slack_client, user_id, ongoing)
        return False

    # 2) 30분 내 시작 예정 이벤트
    if len(upcoming) == 1:
        _pending_inputs[user_id] = {"inputs": [input_item], "events": upcoming}
        _prompt_event_confirm(slack_client, user_id, upcoming[0], 0)
        return False
    elif len(upcoming) > 1:
        _pending_inputs[user_id] = {"inputs": [input_item], "events": upcoming}
        _prompt_event_selection(slack_client, user_id, upcoming)
        return False

    # 3) 진행 중·예정 없음, 가장 가까운 이벤트(들)
    if len(nearby) == 1:
        _pending_inputs[user_id] = {"inputs": [input_item], "events": nearby}
        _prompt_event_confirm(slack_client, user_id, nearby[0], nearby_dist)
        return False
    elif len(nearby) > 1:
        _pending_inputs[user_id] = {"inputs": [input_item], "events": nearby}
        _prompt_event_selection(slack_client, user_id, nearby)
        return False

    # 4) 오늘 일정 없음 → 제목 입력 요청
    _pending_inputs[user_id] = {
        "inputs": [input_item],
        "events": [],
    }
    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn",
                     "text": "📅 오늘 캘린더에 일정이 없습니다.\n미팅 제목을 이 스레드에 답글로 입력하거나, 아래 버튼을 눌러주세요."},
        },
        {
            "type": "actions",
            "elements": [{
                "type": "button",
                "text": {"type": "plain_text", "text": "📝 제목 없이 기록 시작"},
                "action_id": "select_meeting_event_new",
            }],
        },
    ]
    resp = slack_client.chat_postMessage(
        channel=user_id,
        text="오늘 일정이 없습니다. 미팅 제목을 입력해주세요.",
        blocks=blocks,
    )
    if resp and resp.get("ok"):
        _pending_inputs[user_id]["prompt_ts"] = resp["ts"]
    return False


def handle_event_selection(slack_client, user_id: str, selected_event_id: str | None,
                           custom_title: str | None = None):
    """이벤트 선택 버튼 콜백 또는 제목 직접 입력 처리.

    selected_event_id: 선택한 이벤트 ID (None이면 새 미팅)
    custom_title: 새 미팅일 때 사용자 지정 제목
    """
    pending = _pending_inputs.pop(user_id, None)
    if not pending:
        return

    inputs = pending.get("inputs", [])
    events = pending.get("events", [])
    pending_channel = pending.get("session_channel")
    pending_thread_ts = pending.get("session_thread_ts")

    if selected_event_id:
        # 이벤트 목록에서 해당 ID 찾기
        matched = next((ev for ev in events if ev["id"] == selected_event_id), None)
        if matched:
            _start_session_with_event(slack_client, user_id, matched)
        else:
            # fallback: 제목으로 시작 (force_ad_hoc으로 재귀 방지)
            start_session(slack_client, user_id,
                          custom_title or pending.get("custom_title") or "미팅",
                          force_ad_hoc=True)
    else:
        # 새 미팅 — custom_title 우선 (버튼 콜백 직접 인자), 없으면 pending에 저장된 값
        title = custom_title or pending.get("custom_title") or "미팅"
        # F3: force_ad_hoc=True로 재귀적 선택 UI 방지 (사용자가 방금 "새 미팅"을 고름)
        start_session(slack_client, user_id, title, force_ad_hoc=True)

    # B2: 선택 프롬프트 이전에 보존한 채널/스레드를 세션에 주입
    if user_id in _active_sessions and (pending_channel or pending_thread_ts):
        _active_sessions[user_id]["session_channel"] = pending_channel
        _active_sessions[user_id]["session_thread_ts"] = pending_thread_ts
        _save_active_session(user_id)

    # 대기 중이던 입력들을 세션에 추가
    if user_id in _active_sessions:
        for item in inputs:
            content = item.get("content", "")
            if content:
                timestamp = datetime.now(KST).strftime("%H:%M")
                _active_sessions[user_id]["notes"].append({"time": timestamp, "text": content})
        _save_active_session(user_id)
        count = len(_active_sessions[user_id]["notes"])
        if inputs:
            _post(slack_client, user_id=user_id,
                  text=f"📝 대기 중이던 메모 {len(inputs)}개가 세션에 추가되었습니다. (총 {count}개)")


def handle_event_title_reply(slack_client, user_id: str, title_text: str):
    """이벤트 없음 상태에서 사용자가 제목을 직접 입력한 경우."""
    handle_event_selection(slack_client, user_id, selected_event_id=None,
                           custom_title=title_text)


# ── 수동 노트 세션 ─────────────────────────────────────────────


def start_session(slack_client, user_id: str, title: str,
                   channel: str = None, thread_ts: str = None,
                   force_ad_hoc: bool = False):
    """/미팅시작 {제목} — 수동 노트 세션 시작.

    F3 정책(2026-04): 후보 이벤트가 1건이라도 있으면 **항상** 선택 UI를 띄움.
    사용자가 원하는 미팅을 명시적으로 선택하거나 "새 미팅 추가"로 ad-hoc 세션을
    만들 수 있게 함. 캘린더 이벤트 의존성을 완화.

    force_ad_hoc=True: 이벤트 탐색을 건너뛰고 즉시 ad-hoc 세션 생성.
    "새 미팅 추가" 버튼 클릭 흐름(handle_event_selection)에서 재귀 방지용으로 사용.
    """
    try:
        creds, _ = _get_creds_and_config(user_id)
    except Exception as e:
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text=f"⚠️ 인증 오류: {e}")
        return

    if user_id in _active_sessions:
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text=f"⚠️ 이미 진행 중인 세션이 있습니다: *{_active_sessions[user_id]['title']}*\n"
                   f"`/미팅종료` 후 다시 시작해주세요.")
        return

    title_to_use = title or "미팅"

    # force_ad_hoc=True면 이벤트 탐색 없이 바로 ad-hoc 세션 생성
    if force_ad_hoc:
        _create_ad_hoc_session(slack_client, user_id, title_to_use, channel, thread_ts)
        return

    # 후보 이벤트 수집: 진행 중·30분 내 시작·제목 일치 — 중복 제거 (우선순위 순)
    candidates: list[dict] = []
    try:
        now = datetime.now(KST)
        events = cal.get_upcoming_meetings(creds, days=1)

        ongoing: list[dict] = []
        upcoming: list[dict] = []
        by_title: list[dict] = []

        for ev in events:
            parsed = cal.parse_event(ev)
            start_str = parsed.get("start_time", "")
            end_str = ev.get("end", {}).get("dateTime", "")
            if not start_str or not end_str:
                continue
            try:
                start_dt = datetime.fromisoformat(start_str)
                end_dt = datetime.fromisoformat(end_str)
            except Exception:
                continue

            if start_dt <= now <= end_dt:
                ongoing.append(parsed)
            elif now < start_dt <= now + timedelta(minutes=30):
                upcoming.append(parsed)
            if title and title.lower() in parsed["summary"].lower():
                by_title.append(parsed)

        seen = set()
        for bucket in (ongoing, upcoming, by_title):
            for ev in bucket:
                ev_id = ev.get("id")
                if ev_id and ev_id not in seen:
                    candidates.append(ev)
                    seen.add(ev_id)
    except Exception as e:
        log.warning(f"캘린더 이벤트 매칭 실패: {e}")

    if not candidates:
        # 후보 0건 — 사용자가 명시적 제목을 줬으면 즉시 ad-hoc, 아니면 모달 트리거 버튼 게시
        explicit_title = (title or "").strip() and title.strip().lower() not in ("미팅", "회의", "meeting")
        if explicit_title:
            _create_ad_hoc_session(slack_client, user_id, title_to_use, channel, thread_ts)
            return
        # 모달 트리거 버튼 게시 — 클릭 시 main.py에서 trigger_id로 모달 오픈
        _post_meeting_start_modal_trigger(slack_client, user_id, channel, thread_ts,
                                           custom_title=title or "")
        return

    # 후보 1건 이상 → 항상 선택 UI 표시 (F3)
    _pending_inputs[user_id] = {
        "inputs": [],
        "events": candidates,
        # B2: 원래 호출 컨텍스트(채널/스레드)를 보존해 선택 후 세션에 주입
        "session_channel": channel,
        "session_thread_ts": thread_ts,
        # F3: "새 미팅 추가" 클릭 시 사용자가 입력한 제목을 그대로 사용 (스레드 답글 생략)
        "custom_title": title if title else None,
    }
    _prompt_event_selection(slack_client, user_id, candidates,
                            custom_title=title if title else None)


def _create_ad_hoc_session(slack_client, user_id: str, title: str,
                           channel: str | None, thread_ts: str | None,
                           company: str = "", attendees_manual: list | None = None):
    """캘린더 이벤트 없이 ad-hoc 세션 생성 + 확인 메시지.

    company / attendees_manual 은 모달에서 사용자가 직접 입력한 값(선택). 회의록 생성 시
    참석자 컨텍스트로 활용된다.
    """
    _active_sessions[user_id] = {
        "title": title,
        "started_at": datetime.now(KST).strftime("%Y-%m-%d %H:%M"),
        "notes": [],
        "event_id": None,
        "event_summary": None,
        "event_time_str": None,
        "session_channel": channel,
        "session_thread_ts": thread_ts,
        "company": (company or "").strip(),
        "attendees_manual": list(attendees_manual or []),
    }
    _save_active_session(user_id)
    info_lines = [f"✅ *{title}* 노트 세션 시작 _(캘린더 일정 미연동)_"]
    if company:
        info_lines.append(f"🏢 업체: *{company}*")
    if attendees_manual:
        info_lines.append(f"👥 참석자: {', '.join(attendees_manual)}")
    info_lines.append("`/메모 내용` 으로 실시간 메모를 기록하세요.")
    info_lines.append("미팅이 끝나면 `/미팅종료` 를 입력해주세요.")
    info_lines.append(_hint("음성 파일·텍스트 문서 업로드도 가능 / `/메모 [내용]` 으로 즉시 추가").lstrip("\n"))
    _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
          text="\n".join(info_lines))


# ── 새 미팅 시작 모달 (옵션 A — Fix 3) ─────────────────────────


_MEETING_START_MODAL_CALLBACK = "meeting_start_modal"


def open_meeting_start_modal(slack_client, *, trigger_id: str, user_id: str,
                              custom_title: str = "",
                              channel: str | None = None,
                              thread_ts: str | None = None) -> None:
    """ad-hoc 세션 시작용 모달 — 제목·업체·참석자 입력.

    호출 컨텍스트(채널/스레드/대기 입력)는 view.private_metadata에 직렬화하여
    제출 핸들러로 전달.
    """
    private_meta = json.dumps({
        "user_id": user_id,
        "channel": channel,
        "thread_ts": thread_ts,
    }, ensure_ascii=False)

    title_input = {
        "type": "plain_text_input",
        "action_id": "title_input",
        "placeholder": {"type": "plain_text", "text": "예: Allobank 사전논의"},
    }
    if custom_title:
        title_input["initial_value"] = custom_title

    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn",
                     "text": "📝 *새 미팅 노트 세션*\n캘린더에 없는 회의를 시작합니다. 회의 정보를 입력해주세요."},
        },
        {
            "type": "input",
            "block_id": "title_block",
            "label": {"type": "plain_text", "text": "회의 제목"},
            "element": title_input,
        },
        {
            "type": "input",
            "block_id": "company_block",
            "optional": True,
            "label": {"type": "plain_text", "text": "관련 업체 (선택)"},
            "element": {
                "type": "plain_text_input",
                "action_id": "company_input",
                "placeholder": {"type": "plain_text", "text": "예: Allobank, 카카오, KISA"},
            },
        },
        {
            "type": "input",
            "block_id": "attendees_block",
            "optional": True,
            "label": {"type": "plain_text", "text": "참석자 (선택, 쉼표로 구분)"},
            "element": {
                "type": "plain_text_input",
                "action_id": "attendees_input",
                "placeholder": {"type": "plain_text", "text": "예: 김민환, 김종협, 김은서"},
            },
        },
    ]
    slack_client.views_open(
        trigger_id=trigger_id,
        view={
            "type": "modal",
            "callback_id": _MEETING_START_MODAL_CALLBACK,
            "title": {"type": "plain_text", "text": "미팅 노트 시작"},
            "submit": {"type": "plain_text", "text": "시작"},
            "close": {"type": "plain_text", "text": "취소"},
            "private_metadata": private_meta,
            "blocks": blocks,
        },
    )


def _post_meeting_start_modal_trigger(slack_client, user_id: str,
                                       channel: str | None, thread_ts: str | None,
                                       custom_title: str = "") -> None:
    """no-candidates 경로 — 사용자가 클릭하면 trigger_id로 모달이 열리도록 버튼 게시.

    pending_inputs에 컨텍스트(채널/스레드/custom_title)를 저장해 main.py의
    select_meeting_event_new 핸들러가 그대로 모달을 열 수 있게 한다.
    """
    _pending_inputs.setdefault(user_id, {})
    _pending_inputs[user_id].update({
        "inputs": _pending_inputs[user_id].get("inputs", []),
        "events": [],
        "session_channel": channel,
        "session_thread_ts": thread_ts,
        "custom_title": custom_title or None,
    })
    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn",
                     "text": "📅 캘린더에 매칭되는 일정이 없습니다.\n버튼을 눌러 미팅 정보(제목·업체·참석자)를 입력해주세요."},
        },
        {
            "type": "actions",
            "elements": [{
                "type": "button",
                "text": {"type": "plain_text", "text": "📝 새 미팅 정보 입력"},
                "action_id": "select_meeting_event_new",
                "style": "primary",
            }],
        },
    ]
    resp = slack_client.chat_postMessage(
        channel=channel or user_id,
        thread_ts=thread_ts,
        text="새 미팅 정보를 입력해주세요.",
        blocks=blocks,
    )
    if resp and resp.get("ok"):
        _pending_inputs[user_id]["prompt_ts"] = resp["ts"]


def handle_meeting_start_modal(slack_client, user_id: str, view: dict) -> None:
    """미팅 시작 모달 제출 처리 — ad-hoc 세션 생성 + 대기 입력 합치기."""
    values = view.get("state", {}).get("values", {})
    title = ((values.get("title_block", {}).get("title_input", {}) or {}).get("value") or "").strip() or "미팅"
    company = ((values.get("company_block", {}).get("company_input", {}) or {}).get("value") or "").strip()
    attendees_raw = ((values.get("attendees_block", {}).get("attendees_input", {}) or {}).get("value") or "").strip()
    attendees_list = [a.strip() for a in re.split(r"[,，、\n]", attendees_raw) if a.strip()] if attendees_raw else []

    # private_metadata 복원
    try:
        meta = json.loads(view.get("private_metadata") or "{}")
    except Exception:
        meta = {}
    pending = _pending_inputs.pop(user_id, None) or {}
    inputs = pending.get("inputs", [])
    channel = pending.get("session_channel") or meta.get("channel")
    thread_ts = pending.get("session_thread_ts") or meta.get("thread_ts")

    # 이미 진행 중 세션이 있으면 무시
    if user_id in _active_sessions:
        log.info(f"meeting_start_modal: 이미 진행 중 세션 — 무시 ({user_id})")
        return

    _create_ad_hoc_session(slack_client, user_id, title, channel, thread_ts,
                           company=company, attendees_manual=attendees_list)

    # 대기 메모 합치기 (handle_event_selection 패턴 동일)
    if inputs and user_id in _active_sessions:
        for item in inputs:
            content = item.get("content", "")
            if content:
                ts = datetime.now(KST).strftime("%H:%M")
                _active_sessions[user_id]["notes"].append({"time": ts, "text": content})
        _save_active_session(user_id)
        count = len(_active_sessions[user_id]["notes"])
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text=f"📝 대기 중이던 메모 {len(inputs)}개가 세션에 추가되었습니다. (총 {count}개)")


def start_document_based_minutes(slack_client, user_id: str,
                                  filename: str, text: str) -> None:
    """F4: 세션 없이 업로드된 문서(트랜스크립트·회의록)로부터 회의록 생성.

    캘린더 이벤트나 기존 세션 없이 바로 회의록 초안 생성 경로로 진입한다.
    저장 시 After Agent가 기업·인물 Wiki를 자동으로 갱신함 (기존 흐름 재사용).

    - 제목: 파일명에서 확장자 제거. 사용자는 초안 스레드에서 '✏️ 수정' 버튼으로 변경 가능
    - 날짜: 오늘
    - 참석자: '정보 없음' (문서 내용에서 After Agent가 추론)
    - transcript_text: 문서 본문 전체
    """
    import os as _os
    try:
        creds, minutes_folder_id = _get_creds_and_config(user_id)
    except Exception as e:
        _post(slack_client, user_id=user_id, text=f"⚠️ 인증 오류: {e}")
        return

    # 파일명에서 확장자 제거 + 기본 정제
    title = _os.path.splitext(filename or "업로드 문서")[0].strip() or "업로드 문서"
    date_str = datetime.now(KST).strftime("%Y-%m-%d")
    time_range = ""  # 문서 기반이라 시간대 없음

    _post(slack_client, user_id=user_id,
          text=f"📄 *{filename}* 문서를 트랜스크립트로 회의록을 생성합니다...\n"
               f"_(캘린더 미연동 — 제목·날짜 수정은 초안 스레드에서 가능)_")

    _generate_and_post_minutes(
        slack_client, user_id=user_id,
        title=title, date_str=date_str, time_range=time_range,
        attendees="정보 없음",
        transcript_text=text, notes_text="",
        minutes_folder_id=minutes_folder_id, creds=creds,
        event_id=None, attendees_raw=[],
    )


def add_note(slack_client, user_id: str, note_text: str, session_title: str = "메모 세션",
             input_type: str = "note", channel: str = None, thread_ts: str = None):
    """/메모 {내용} — 진행 중 세션에 노트 추가. 세션이 없으면 캘린더 이벤트 자동 감지."""
    if not note_text.strip():
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text="⚠️ 노트 내용을 입력해주세요. 예: `/메모 DID 연동 방안 논의`")
        return

    if user_id not in _active_sessions:
        # 캘린더 이벤트 자동 감지 → 세션 시작 또는 선택 요청
        input_item = {"type": input_type, "content": note_text.strip()}
        session_started = _auto_start_or_enqueue(slack_client, user_id, input_item)
        if not session_started:
            # 이벤트 선택 대기 중 — 입력은 큐에 저장됨
            return

    timestamp = datetime.now(KST).strftime("%H:%M")
    _active_sessions[user_id]["notes"].append({"time": timestamp, "text": note_text.strip()})
    _save_active_session(user_id)
    count = len(_active_sessions[user_id]["notes"])
    _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
          text=f"📝 노트 #{count} 저장: _{note_text.strip()}_")


def _generate_from_session_end(slack_client, *, user_id: str, event_id: str,
                                title: str, notes: list, started_at: str, ended_at: str,
                                source: str = "transcript",
                                post_channel: str | None = None,
                                post_thread_ts: str | None = None):
    """/미팅종료 후 사용자 선택(source)에 따라 회의록 생성.
    source:
      - 'transcript' — 트랜스크립트 탐색 후 있으면 사용, 없으면 노트+대기 등록
      - 'notes' — 트랜스크립트 탐색 skip, 노트만으로 즉시 생성
      - 'wait' — 즉시 생성 안 함, 트랜스크립트 도착까지 대기만 등록 (path D)
    """
    # /미팅종료 명시 호출이므로 중복 처리 방지 플래그를 제거 후 재생성 허용
    _processed_events.setdefault(user_id, set()).discard(event_id)

    try:
        creds, minutes_folder_id = _get_creds_and_config(user_id)
    except Exception as e:
        log.error(f"인증 오류 ({user_id}): {e}")
        _post(slack_client, user_id=user_id, channel=post_channel, thread_ts=post_thread_ts,
              text=f"⚠️ 인증 오류: {e}")
        return

    # Calendar 이벤트에서 날짜·참석자 조회 시도
    date_str = datetime.now(KST).strftime("%Y-%m-%d")
    time_range = f"{started_at.split(' ')[-1]} ~ {ended_at}"
    attendees_str = "정보 없음"
    attendees_raw = []
    try:
        recently_ended = cal.get_recently_ended_meetings(creds, min_minutes_ago=0, max_minutes_ago=90)
        for m in recently_ended:
            if m.get("id") == event_id:
                date_str, time_range, attendees_str = _parse_meeting_meta(m)
                attendees_raw = m.get("attendees", [])
                break
    except Exception as e:
        log.warning(f"Calendar 이벤트 조회 실패: {e}")

    notes_text = _format_notes(notes)

    # I1: 'wait' → 즉시 생성하지 않고 트랜스크립트 도착 대기만 등록
    if source == "wait":
        if event_id:
            _awaiting_transcript[event_id] = {
                "user_id": user_id,
                "title": title,
                "date_str": date_str,
                "time_range": time_range,
                "attendees": attendees_str,
                "notes_text": notes_text,
                "minutes_folder_id": minutes_folder_id,
                "attendees_raw": attendees_raw,
                "created_at": datetime.now(KST),
                "post_channel": post_channel,
                "post_thread_ts": post_thread_ts,
            }
            log.info(f"트랜스크립트 대기만 등록 (즉시 생성 안 함): {title} ({event_id})")
            _post(slack_client, user_id=user_id, channel=post_channel, thread_ts=post_thread_ts,
                  text=f"🕐 *{title}* — 트랜스크립트 도착까지 최대 90분 대기합니다. 도착 시 회의록을 자동 생성합니다.")
        return

    # 트랜스크립트 탐색 (source=='transcript'일 때만)
    transcript_text = ""
    if source == "transcript":
        _post(slack_client, user_id=user_id, channel=post_channel, thread_ts=post_thread_ts,
              text=f"🔍 *{title}* 트랜스크립트 탐색 중...")
        try:
            transcript_file = drive.find_meet_transcript(creds, title, None)
            if transcript_file:
                log.info(f"트랜스크립트 발견 (즉시): {transcript_file['name']}")
                transcript_text = docs.read_document(creds, transcript_file["id"])
            else:
                log.info(f"트랜스크립트 없음, 노트만으로 생성: {title}")
        except Exception as e:
            log.warning(f"트랜스크립트 탐색 실패: {e}")
    else:
        # source == 'notes'
        log.info(f"노트만으로 회의록 생성 (사용자 선택): {title}")

    _processed_events.setdefault(user_id, set()).add(event_id)
    _save_processed_events(user_id)

    # 트랜스크립트가 없는 상태로 노트만으로 생성하는 경우, 도착 시 자동 보강을 위한 대기 등록
    # (source=='notes' 는 사용자가 '노트만'을 명시 선택했으므로 보강하지 않음)
    if source == "transcript" and not transcript_text and notes_text and event_id:
        _awaiting_transcript[event_id] = {
            "user_id": user_id,
            "title": title,
            "date_str": date_str,
            "time_range": time_range,
            "attendees": attendees_str,
            "notes_text": notes_text,
            "minutes_folder_id": minutes_folder_id,
            "attendees_raw": attendees_raw,
            "created_at": datetime.now(KST),
            "post_channel": post_channel,
            "post_thread_ts": post_thread_ts,
        }
        log.info(f"트랜스크립트 대기 등록: {title} ({event_id})")
        _post(slack_client, user_id=user_id, channel=post_channel, thread_ts=post_thread_ts,
              text=f"ℹ️ 트랜스크립트가 나중에 도착하면 회의록을 자동 보강합니다. (최대 90분 대기)")

    _generate_and_post_minutes(
        slack_client, user_id=user_id,
        title=title, date_str=date_str, time_range=time_range,
        attendees=attendees_str,
        transcript_text=transcript_text, notes_text=notes_text,
        minutes_folder_id=minutes_folder_id, creds=creds,
        event_id=event_id, attendees_raw=attendees_raw,
        post_channel=post_channel, post_thread_ts=post_thread_ts,
    )


def generate_minutes_now(slack_client, user_id: str, channel: str = None, thread_ts: str = None):
    """/회의록작성 — 세션 종료 + 회의록 생성. /미팅종료와 동일 동작."""
    end_session(slack_client, user_id, channel=channel, thread_ts=thread_ts)


# ── 사후 회의록 복구 (post-hoc recovery) ─────────────────────


def _format_recovery_button_label(event: dict) -> str:
    """복구 버튼 라벨 — '제목 (HH:MM 종료)' 형식. 75자 제한."""
    summary = (event.get("summary") or "(제목 없음)").strip()
    end_str = event.get("end_time") or ""
    end_label = ""
    try:
        if end_str:
            end_dt = datetime.fromisoformat(end_str)
            end_label = f" ({end_dt.strftime('%H:%M')} 종료)"
    except Exception:
        pass
    return f"{summary}{end_label}"[:75]


def _try_post_recovery_selection(slack_client, *, user_id: str,
                                  channel: str | None,
                                  thread_ts: str | None) -> None:
    """활성 세션이 없을 때 호출 — 최근 종료된 캘린더 이벤트 후보를 찾아 복구 UI 발송.

    후보 0건 또는 인증 오류 시에는 기존 경고 메시지로 폴백.
    """
    warning_text = (
        "⚠️ 진행 중인 미팅 세션이 없습니다.\n"
        "먼저 `/미팅시작` 또는 `/메모`로 메모를 기록해주세요."
        + _hint("새 미팅이라면 `/미팅시작 [제목]` 부터 시작하세요")
    )

    try:
        creds, _ = _get_creds_and_config(user_id)
    except Exception as e:
        # 토큰 만료 등은 친화적 안내, 그 외는 기존 경고 폴백
        if user_store.is_token_expired_error(e):
            _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
                  text="🔐 Google 인증이 만료되었어요.\n`/재등록` 명령으로 다시 인증해주세요.")
            return
        log.warning(f"복구용 자격증명 조회 실패 ({user_id}): {e}")
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text=warning_text)
        return

    # 최근 3시간 이내 종료된 미팅 탐색 — 0분 ago 부터 (방금 끝난 미팅 포함)
    try:
        recent = cal.get_recently_ended_meetings(
            creds, min_minutes_ago=0, max_minutes_ago=180,
        )
    except Exception as e:
        if user_store.is_token_expired_error(e):
            _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
                  text="🔐 Google 인증이 만료되었어요.\n`/재등록` 명령으로 다시 인증해주세요.")
            return
        log.warning(f"최근 종료 미팅 조회 실패 ({user_id}): {e}")
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text=warning_text)
        return

    if not recent:
        # 후보 없음 → 기존 경고 유지
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text=warning_text)
        return

    # 후보 1건 이상 → 복구 선택 UI 발송
    _pending_recovery[user_id] = {
        "events": recent,
        "session_channel": channel,
        "session_thread_ts": thread_ts,
    }
    _post_recovery_selection(slack_client, user_id=user_id, events=recent,
                             channel=channel, thread_ts=thread_ts)


def _post_recovery_selection(slack_client, *, user_id: str, events: list[dict],
                              channel: str | None,
                              thread_ts: str | None) -> None:
    """방금 끝난 미팅 후보를 버튼으로 표시 (사후 회의록 복구)."""
    buttons = []
    for ev in events[:5]:  # 최대 5개
        ev_id = ev.get("id")
        if not ev_id:
            continue
        buttons.append({
            "type": "button",
            "text": {"type": "plain_text",
                     "text": _format_recovery_button_label(ev)},
            "action_id": "recover_meeting_minutes",
            "value": ev_id,
        })

    if not buttons:
        # 안전장치 — 후보는 있는데 id가 없는 비정상 케이스
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text="⚠️ 진행 중인 미팅 세션이 없습니다."
                   + _hint("새 미팅이라면 `/미팅시작 [제목]` 부터 시작하세요"))
        return

    blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": ("📋 진행 중인 세션이 없네요. *방금 끝난 미팅이 있나요?*\n"
                         "회의록을 만들 미팅을 선택해주세요."),
            },
        },
        {"type": "actions", "elements": buttons},
        {
            "type": "context",
            "elements": [{
                "type": "mrkdwn",
                "text": "_트랜스크립트가 아직 도착하지 않았다면 최대 90분 대기 후 자동 생성됩니다._",
            }],
        },
    ]
    try:
        slack_client.chat_postMessage(
            channel=channel or user_id,
            thread_ts=thread_ts,
            text="방금 끝난 미팅을 선택해주세요.",
            blocks=blocks,
        )
    except Exception as e:
        log.warning(f"복구 선택 블록 발송 실패: {e}")


def handle_recover_meeting_minutes_button(slack_client, body: dict) -> None:
    """`recover_meeting_minutes` 버튼 콜백 — 사후 회의록 생성 시도.

    - 선택된 event_id로 캘린더 이벤트를 조회해 제목·시간·참석자 메타 추출
    - `_generate_from_session_end(source='transcript', notes=[])` 호출 →
      트랜스크립트가 있으면 즉시, 없으면 `_awaiting_transcript`에 등록되어
      10분 폴링으로 최대 90분간 대기.
    """
    user_id = body.get("user", {}).get("id")
    action = (body.get("actions") or [{}])[0]
    event_id = action.get("value", "")
    container = body.get("container", {}) or {}
    msg_ch = container.get("channel_id")
    msg_ts = container.get("message_ts")

    pending = _pending_recovery.pop(user_id, None) if user_id else None
    post_channel = (pending or {}).get("session_channel") or msg_ch
    post_thread_ts = (pending or {}).get("session_thread_ts")

    if not user_id or not event_id:
        log.warning(f"recover_meeting_minutes: user_id/event_id 없음 (body={list(body.keys())})")
        return

    # 중복 클릭 방지 — 원본 메시지 텍스트로 교체
    if msg_ch and msg_ts:
        try:
            slack_client.chat_update(
                channel=msg_ch, ts=msg_ts,
                text="🔍 사후 회의록 복구 진행 중...",
                blocks=[{
                    "type": "section",
                    "text": {"type": "mrkdwn",
                             "text": "🔍 사후 회의록 복구 진행 중..."},
                }],
            )
        except Exception:
            pass

    try:
        creds, _ = _get_creds_and_config(user_id)
    except Exception as e:
        if user_store.is_token_expired_error(e):
            _post(slack_client, user_id=user_id, channel=post_channel, thread_ts=post_thread_ts,
                  text="🔐 Google 인증이 만료되었어요.\n`/재등록` 명령으로 다시 인증해주세요.")
        else:
            _post(slack_client, user_id=user_id, channel=post_channel, thread_ts=post_thread_ts,
                  text=f"⚠️ 인증 오류: {e}")
        return

    # 캘린더 이벤트 메타 조회
    try:
        event_raw = cal.get_event(creds, event_id)
    except Exception as e:
        if user_store.is_token_expired_error(e):
            _post(slack_client, user_id=user_id, channel=post_channel, thread_ts=post_thread_ts,
                  text="🔐 Google 인증이 만료되었어요.\n`/재등록` 명령으로 다시 인증해주세요.")
            return
        log.warning(f"recover_meeting_minutes: 이벤트 조회 실패 ({event_id}): {e}")
        _post(slack_client, user_id=user_id, channel=post_channel, thread_ts=post_thread_ts,
              text=f"⚠️ 캘린더 이벤트 조회 실패: {e}")
        return

    parsed = cal.parse_event(event_raw)
    title = parsed.get("summary") or "(제목 없음)"
    end_raw = (event_raw.get("end") or {}).get("dateTime") \
        or (event_raw.get("end") or {}).get("date") or ""
    start_raw = parsed.get("start_time") or ""
    started_at = ""
    ended_at = datetime.now(KST).strftime("%H:%M")
    try:
        if start_raw:
            sdt = datetime.fromisoformat(start_raw)
            started_at = sdt.strftime("%Y-%m-%d %H:%M")
        if end_raw:
            edt = datetime.fromisoformat(end_raw)
            ended_at = edt.strftime("%H:%M")
    except Exception:
        pass
    if not started_at:
        started_at = datetime.now(KST).strftime("%Y-%m-%d %H:%M")

    _post(slack_client, user_id=user_id, channel=post_channel, thread_ts=post_thread_ts,
          text=(f"🔍 *{title}* — 회의록 생성 시도 중. "
                f"트랜스크립트가 있으면 즉시 생성, 없으면 도착 대기 (최대 90분)."))

    # _generate_from_session_end 는 트랜스크립트 탐색 + 없으면 _awaiting_transcript 등록
    threading.Thread(
        target=_generate_from_session_end,
        kwargs=dict(
            slack_client=slack_client,
            user_id=user_id,
            event_id=event_id,
            title=title,
            notes=[],  # 사후 복구 — 사용자가 입력한 노트는 없음
            started_at=started_at,
            ended_at=ended_at,
            source="transcript",
            post_channel=post_channel,
            post_thread_ts=post_thread_ts,
        ),
        daemon=True,
    ).start()


def end_session(slack_client, user_id: str, channel: str = None, thread_ts: str = None):
    """/미팅종료 — 세션을 종료하고 회의록 생성 방식(I1)을 사용자에게 선택받은 뒤 생성.

    활성 세션이 없으면 최근 종료된 캘린더 이벤트(최대 3시간 이내)를 찾아 사후 복구
    선택 UI를 표시한다. 후보가 0건이면 기존 경고 메시지를 유지.
    """
    if user_id not in _active_sessions:
        _try_post_recovery_selection(slack_client, user_id=user_id,
                                     channel=channel, thread_ts=thread_ts)
        return

    session = _active_sessions.pop(user_id)
    title = session["title"]
    notes = session["notes"]
    event_id = session["event_id"]
    event_summary = session.get("event_summary")
    event_time_str = session.get("event_time_str")
    started_at = session["started_at"]
    ended_at = datetime.now(KST).strftime("%H:%M")

    _delete_active_session_file(user_id)

    # B2: 세션이 채널에서 시작되었으면 해당 채널(스레드)에 응답을 유지
    post_channel = session.get("session_channel") or channel
    post_thread_ts = session.get("session_thread_ts") or thread_ts

    note_count = len(notes)
    if event_id and event_summary:
        event_line = f"\n📅 일정: *{event_summary}*" + (f" ({event_time_str})" if event_time_str else "")
    else:
        event_line = "\n_(캘린더 미연동)_"
    _post(slack_client, user_id=user_id, channel=post_channel, thread_ts=post_thread_ts,
          text=f"✅ 세션 종료. 노트 {note_count}개 저장됨.{event_line}")

    if event_id:
        # I1: 트랜스크립트/노트/대기 선택 UI를 보여주고 사용자가 선택한 뒤에 생성
        _pending_source_select[event_id] = {
            "user_id": user_id,
            "title": title,
            "notes": notes,
            "started_at": started_at,
            "ended_at": ended_at,
            "post_channel": post_channel,
            "post_thread_ts": post_thread_ts,
        }
        _post_source_selection(
            slack_client, user_id=user_id, event_id=event_id, title=title,
            has_notes=bool(notes),
            post_channel=post_channel, post_thread_ts=post_thread_ts,
        )
    else:
        # 캘린더 연동 없음 — 노트만으로 즉시 생성
        try:
            creds, minutes_folder_id = _get_creds_and_config(user_id)
        except Exception as e:
            _post(slack_client, user_id=user_id, channel=post_channel, thread_ts=post_thread_ts,
                  text=f"⚠️ 인증 오류: {e}")
            return

        date_str = datetime.now(KST).strftime("%Y-%m-%d")
        time_range = f"{started_at.split(' ')[-1]} ~ {ended_at}"
        notes_text = _format_notes(notes)
        _generate_and_post_minutes(
            slack_client, user_id=user_id,
            title=title, date_str=date_str, time_range=time_range,
            attendees="정보 없음",
            transcript_text="", notes_text=notes_text,
            minutes_folder_id=minutes_folder_id, creds=creds,
            event_id=None, attendees_raw=[],
            post_channel=post_channel, post_thread_ts=post_thread_ts,
        )


def _post_source_selection(slack_client, *, user_id: str, event_id: str,
                           title: str, has_notes: bool,
                           post_channel: str | None = None,
                           post_thread_ts: str | None = None):
    """I1: 회의록 생성 방식 선택 블록 발송."""
    note_hint = "노트 있음" if has_notes else "노트 없음"
    blocks = [
        {
            "type": "section",
            "text": {"type": "mrkdwn",
                     "text": (f"📋 *{title}* — 회의록을 어떤 방식으로 만들까요? "
                              f"_({note_hint})_")},
        },
        {
            "type": "actions",
            "elements": [
                {"type": "button", "style": "primary",
                 "text": {"type": "plain_text", "text": "🎙️ 트랜스크립트 탐색"},
                 "action_id": "minutes_src_transcript", "value": event_id},
                {"type": "button",
                 "text": {"type": "plain_text", "text": "📎 트랜스크립트 첨부"},
                 "action_id": "minutes_src_upload", "value": event_id},
                {"type": "button",
                 "text": {"type": "plain_text", "text": "📝 노트만"},
                 "action_id": "minutes_src_notes", "value": event_id},
                {"type": "button",
                 "text": {"type": "plain_text", "text": "🕐 트랜스크립트 대기"},
                 "action_id": "minutes_src_wait", "value": event_id},
                {"type": "button", "style": "danger",
                 "text": {"type": "plain_text", "text": "❌ 취소"},
                 "action_id": "minutes_src_cancel", "value": event_id},
            ],
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn",
                          "text": "_🎙️ 트랜스크립트가 없으면 90분간 기다렸다 자동 보강합니다. "
                                  "📎 첨부는 직접 업로드한 텍스트(.txt/.md/.pdf 등)를 트랜스크립트로 사용. "
                                  "📝 노트만은 즉시 생성. 🕐 대기는 바로 생성하지 않고 도착 시 생성._"}],
        },
    ]
    try:
        slack_client.chat_postMessage(
            channel=post_channel or user_id,
            thread_ts=post_thread_ts,
            text=f"회의록 생성 방식을 선택해주세요: {title}",
            blocks=blocks,
        )
    except Exception as e:
        log.warning(f"회의록 소스 선택 블록 발송 실패: {e}")


def handle_minutes_source_select(slack_client, user_id: str, event_id: str,
                                  source: str, body: dict | None = None):
    """I1: 회의록 소스 선택 버튼 콜백.
    source: 'transcript' | 'upload' | 'notes' | 'wait' | 'cancel'"""
    payload = _pending_source_select.pop(event_id, None)
    if not payload:
        _post(slack_client, user_id=user_id,
              text="⚠️ 이미 처리되었거나 만료된 선택입니다.")
        return

    title = payload["title"]
    notes = payload["notes"]
    started_at = payload["started_at"]
    ended_at = payload["ended_at"]
    post_channel = payload["post_channel"]
    post_thread_ts = payload["post_thread_ts"]

    # 원본 버튼 메시지를 상태 텍스트로 교체 (중복 클릭 방지)
    if body:
        container = body.get("container", {}) or {}
        msg_ch = container.get("channel_id")
        msg_ts = container.get("message_ts")
        label_map = {
            "transcript": "🎙️ 트랜스크립트 탐색",
            "upload": "📎 트랜스크립트 첨부",
            "notes": "📝 노트만 사용",
            "wait": "🕐 트랜스크립트 대기",
            "cancel": "❌ 회의록 생성 취소",
        }
        label = label_map.get(source, source)
        if msg_ch and msg_ts:
            try:
                slack_client.chat_update(
                    channel=msg_ch, ts=msg_ts,
                    text=f"{label} 선택됨: *{title}*",
                    blocks=[{"type": "section",
                             "text": {"type": "mrkdwn",
                                      "text": f"{label} 선택됨: *{title}*"}}],
                )
            except Exception:
                pass

    if source == "cancel":
        _post(slack_client, user_id=user_id, channel=post_channel, thread_ts=post_thread_ts,
              text=f"❌ *{title}* 회의록 생성을 취소했습니다.")
        return

    if source == "upload":
        # 파일 업로드 대기 상태 등록 — 파일 도착 시 _handle_text_upload 가 처리
        _pending_uploaded_transcript[user_id] = {
            "event_id": event_id,
            "title": title,
            "notes": notes,
            "started_at": started_at,
            "ended_at": ended_at,
            "post_channel": post_channel,
            "post_thread_ts": post_thread_ts,
            "created_at": datetime.now(KST),
        }
        _post(slack_client, user_id=user_id, channel=post_channel, thread_ts=post_thread_ts,
              text=(f"📎 *{title}* — 트랜스크립트 파일을 DM에 업로드해주세요.\n"
                    f"_지원: .txt / .md / .pdf / .docx / .doc 등 (10MB 이내). "
                    f"30분 내 도착하지 않으면 만료됩니다._"))
        return

    threading.Thread(
        target=_generate_from_session_end,
        kwargs=dict(
            slack_client=slack_client,
            user_id=user_id,
            event_id=event_id,
            title=title,
            notes=notes,
            started_at=started_at,
            ended_at=ended_at,
            source=source,
            post_channel=post_channel,
            post_thread_ts=post_thread_ts,
        ),
        daemon=True,
    ).start()


def consume_pending_uploaded_transcript(user_id: str) -> dict | None:
    """I1+: 첨부 대기 payload 반환 + 만료(30분) 정리.

    main.py 의 텍스트 업로드 핸들러가 호출. 활성 payload 가 있으면 pop 하여 반환,
    만료된 payload 는 자동 삭제 후 None 반환."""
    data = _pending_uploaded_transcript.get(user_id)
    if not data:
        return None
    age = (datetime.now(KST) - data["created_at"]).total_seconds()
    if age > 30 * 60:
        _pending_uploaded_transcript.pop(user_id, None)
        return None
    return _pending_uploaded_transcript.pop(user_id, None)


def apply_uploaded_transcript(slack_client, user_id: str, payload: dict,
                               filename: str, transcript_text: str) -> None:
    """I1+: 사용자가 업로드한 텍스트를 트랜스크립트로 사용해 회의록 생성.

    payload 는 consume_pending_uploaded_transcript() 가 반환한 _pending_source_select
    페이로드 + event_id. transcript_text 는 이미 추출·인코딩 정상화된 본문."""
    event_id = payload["event_id"]
    title = payload["title"]
    notes = payload["notes"] or []
    started_at = payload["started_at"]
    ended_at = payload["ended_at"]
    post_channel = payload.get("post_channel")
    post_thread_ts = payload.get("post_thread_ts")

    # /미팅종료 명시 호출 흐름이므로 중복 처리 플래그 해제
    _processed_events.setdefault(user_id, set()).discard(event_id)

    try:
        creds, minutes_folder_id = _get_creds_and_config(user_id)
    except Exception as e:
        log.error(f"인증 오류 ({user_id}): {e}")
        _post(slack_client, user_id=user_id, channel=post_channel, thread_ts=post_thread_ts,
              text=f"⚠️ 인증 오류: {e}")
        return

    # Calendar 이벤트에서 날짜·참석자 조회 시도 (없으면 폴백)
    date_str = datetime.now(KST).strftime("%Y-%m-%d")
    time_range = f"{started_at.split(' ')[-1]} ~ {ended_at}" if started_at else ""
    attendees_str = "정보 없음"
    attendees_raw: list[dict] = []
    if event_id:
        try:
            recently_ended = cal.get_recently_ended_meetings(
                creds, min_minutes_ago=0, max_minutes_ago=180
            )
            for m in recently_ended:
                if m.get("id") == event_id:
                    date_str, time_range, attendees_str = _parse_meeting_meta(m)
                    attendees_raw = m.get("attendees", [])
                    break
        except Exception as e:
            log.warning(f"Calendar 이벤트 조회 실패: {e}")

    notes_text = _format_notes(notes)

    _post(slack_client, user_id=user_id, channel=post_channel, thread_ts=post_thread_ts,
          text=(f"📎 *{title}* — 첨부 파일 *{filename}* "
                f"({len(transcript_text):,}자)을 트랜스크립트로 회의록을 생성합니다..."))

    _processed_events.setdefault(user_id, set()).add(event_id)
    _save_processed_events(user_id)

    _generate_and_post_minutes(
        slack_client, user_id=user_id,
        title=title, date_str=date_str, time_range=time_range,
        attendees=attendees_str,
        transcript_text=transcript_text, notes_text=notes_text,
        minutes_folder_id=minutes_folder_id, creds=creds,
        event_id=event_id, attendees_raw=attendees_raw,
        post_channel=post_channel, post_thread_ts=post_thread_ts,
    )


# ── 트랜스크립트 폴링 ──────────────────────────────────────────


def check_transcripts(slack_client):
    """APScheduler 10분 주기 호출.
    트랜스크립트 폴링 + 90분 초과 노트 fallback 처리.
    """
    for user in user_store.all_users():
        user_id = user["slack_user_id"]
        try:
            _check_transcripts_for_user(slack_client, user_id)
        except Exception as e:
            log.error(f"트랜스크립트 체크 실패 ({user_id}): {e}")

    # 90분 경과 후에도 처리 못한 노트 → 노트만으로 fallback 생성
    _flush_expired_notes(slack_client)

    # 트랜스크립트 대기 중인 이벤트 체크 (노트만으로 생성 후 보강 대기)
    _check_awaiting_transcripts(slack_client)


def _check_transcripts_for_user(slack_client, user_id: str, min_minutes_ago: int = 10):
    """사용자별 트랜스크립트 탐색 및 회의록 생성.
    min_minutes_ago=0 으로 호출하면 방금 종료된 미팅도 즉시 탐색.
    """
    creds, minutes_folder_id = _get_creds_and_config(user_id)
    recently_ended = cal.get_recently_ended_meetings(
        creds, min_minutes_ago=min_minutes_ago, max_minutes_ago=90
    )
    if not recently_ended:
        return

    processed = _processed_events.setdefault(user_id, set())

    for meeting in recently_ended:
        event_id = meeting.get("id")
        if not event_id or event_id in processed:
            continue

        title = meeting.get("summary", "미팅")
        end_str = meeting.get("end_time", "")
        ended_after = None
        if end_str:
            try:
                ended_after = datetime.fromisoformat(end_str) - timedelta(minutes=5)
            except Exception:
                pass

        # 트랜스크립트 탐색 (항상 시도 — 수동 세션 여부 무관)
        try:
            transcript_file = drive.find_meet_transcript(creds, title, ended_after)
        except Exception as e:
            log.warning(f"트랜스크립트 탐색 실패 ({title}): {e}")
            continue

        if not transcript_file:
            log.info(f"트랜스크립트 없음: {title}")
            continue

        log.info(f"트랜스크립트 발견: {transcript_file['name']}")

        # 수동 노트 수집 (파일도 함께 삭제)
        notes_data = _completed_notes.pop(event_id, None)
        if notes_data is not None:
            _delete_completed_note_file(event_id)
        # 진행 중인 세션에서도 노트 수집 + 세션 자동 종료
        if notes_data is None:
            sess = _active_sessions.get(user_id)
            if sess and sess.get("event_id") == event_id:
                notes_data = {
                    "user_id": user_id,
                    "title": sess["title"],
                    "notes": list(sess["notes"]),
                    "started_at": sess["started_at"],
                    "ended_at": datetime.now(KST).strftime("%H:%M"),
                }
                # 세션 자동 종료 (트랜스크립트 발견 → 회의록 생성으로 전환)
                _active_sessions.pop(user_id, None)
                _delete_active_session_file(user_id)
                log.info(f"트랜스크립트 발견으로 세션 자동 종료: {user_id} / {title}")
                _post(slack_client, user_id=user_id,
                      text=f"📡 *{title}* 트랜스크립트가 발견되어 세션을 자동 종료하고 회의록을 생성합니다.")

        processed.add(event_id)
        _save_processed_events(user_id)

        try:
            transcript_text = docs.read_document(creds, transcript_file["id"])
        except Exception as e:
            log.error(f"트랜스크립트 읽기 실패: {e}")
            _post(slack_client, user_id=user_id,
                  text=f"⚠️ *{title}* 트랜스크립트 읽기 실패: {e}")
            continue

        # 날짜/시간/참석자 파싱
        date_str, time_range, attendees_str = _parse_meeting_meta(meeting)
        notes_text = _format_notes(notes_data["notes"] if notes_data else [])

        _post(slack_client, user_id=user_id,
              text=f"📝 *{title}* 트랜스크립트 수집 완료. 회의록을 생성 중입니다...")

        _generate_and_post_minutes(
            slack_client, user_id=user_id,
            title=title, date_str=date_str, time_range=time_range,
            attendees=attendees_str,
            transcript_text=transcript_text,
            notes_text=notes_text,
            minutes_folder_id=minutes_folder_id, creds=creds,
            event_id=event_id,
            attendees_raw=meeting.get("attendees", []),
        )


def _flush_expired_notes(slack_client):
    """90분 이상 경과한 노트 → 트랜스크립트 없이 노트만으로 fallback 생성"""
    now = datetime.now(KST)
    expired = [
        eid for eid, data in _completed_notes.items()
        if (now - data["stored_at"]).total_seconds() > 90 * 60
    ]
    for event_id in expired:
        data = _completed_notes.pop(event_id)
        _delete_completed_note_file(event_id)
        user_id = data["user_id"]
        title = data["title"]
        processed = _processed_events.setdefault(user_id, set())
        if event_id in processed:
            continue
        processed.add(event_id)
        _save_processed_events(user_id)

        log.info(f"노트 fallback 처리 (트랜스크립트 없음): {title}")
        _post(slack_client, user_id=user_id,
              text=f"⏰ *{title}* 트랜스크립트를 수집하지 못했습니다. 노트만으로 회의록을 생성합니다.")

        try:
            creds, minutes_folder_id = _get_creds_and_config(user_id)
        except Exception as e:
            log.error(f"fallback 인증 오류 ({user_id}): {e}")
            continue

        date_str = data["stored_at"].strftime("%Y-%m-%d")
        time_range = f"{data['started_at'].split(' ')[-1]} ~ {data['ended_at']}"
        notes_text = _format_notes(data["notes"])

        _generate_and_post_minutes(
            slack_client, user_id=user_id,
            title=title, date_str=date_str, time_range=time_range,
            attendees="정보 없음",
            transcript_text="", notes_text=notes_text,
            minutes_folder_id=minutes_folder_id, creds=creds,
            event_id=event_id, attendees_raw=[],
        )


def _check_awaiting_transcripts(slack_client):
    """트랜스크립트 대기 중인 이벤트에 트랜스크립트가 도착했는지 확인.
    도착 시 회의록 보강 초안을 생성하여 사용자에게 승인 요청.
    90분 경과 시 대기 해제.
    """
    now = datetime.now(KST)
    expired = []
    for event_id, data in list(_awaiting_transcript.items()):
        elapsed = (now - data["created_at"]).total_seconds()
        if elapsed > 90 * 60:
            expired.append(event_id)
            continue

        user_id = data["user_id"]
        title = data["title"]
        try:
            creds, _ = _get_creds_and_config(user_id)
            transcript_file = drive.find_meet_transcript(creds, title, None)
            if not transcript_file:
                continue

            log.info(f"지연 트랜스크립트 발견: {title} ({event_id})")
            transcript_text = docs.read_document(creds, transcript_file["id"])
            if not transcript_text:
                continue

            # 대기 목록에서 제거
            _awaiting_transcript.pop(event_id, None)

            _post(slack_client, user_id=user_id,
                  text=f"📡 *{title}* 트랜스크립트가 도착했습니다! 회의록을 보강 중...")

            # 트랜스크립트 + 노트로 보강된 회의록 생성 (B2: 세션 채널 유지)
            _generate_and_post_minutes(
                slack_client, user_id=user_id,
                title=f"{title} (트랜스크립트 보강)",
                date_str=data["date_str"],
                time_range=data["time_range"],
                attendees=data["attendees"],
                transcript_text=transcript_text,
                notes_text=data["notes_text"],
                minutes_folder_id=data["minutes_folder_id"],
                creds=creds,
                event_id=event_id,
                attendees_raw=data["attendees_raw"],
                post_channel=data.get("post_channel"),
                post_thread_ts=data.get("post_thread_ts"),
            )

        except Exception as e:
            log.warning(f"트랜스크립트 대기 체크 실패 ({title}): {e}")

    # 90분 초과 항목 정리
    for event_id in expired:
        data = _awaiting_transcript.pop(event_id, None)
        if data:
            log.info(f"트랜스크립트 대기 만료 (90분): {data['title']}")


# ── 회의록 생성 공통 ──────────────────────────────────────────


def _classify_meeting_type(attendees_raw: list[dict]) -> str:
    """이메일 도메인 기반으로 internal / vendor / mixed 분류.

    INTERNAL_DOMAINS 환경변수 (기본 parametacorp.com,iconloop.com).
    - 외부 도메인 0명: internal
    - 내부 도메인 0명, 외부만: vendor
    - 둘 다 있음: mixed
    - 참석자 정보 없음 또는 모두 도메인 미상: internal (보수적)
    """
    raw = os.getenv("INTERNAL_DOMAINS", "parametacorp.com,iconloop.com")
    internal_domains = {d.strip().lower() for d in raw.split(",") if d.strip()}

    has_internal = False
    has_external = False
    for a in attendees_raw or []:
        email = (a.get("email") or "").strip()
        if "@" not in email:
            continue
        domain = email.split("@")[-1].lower()
        if domain in internal_domains:
            has_internal = True
        else:
            has_external = True

    if has_external and has_internal:
        return "mixed"
    if has_external and not has_internal:
        return "vendor"
    return "internal"


def _generate_and_post_minutes(slack_client, *, user_id: str, title: str,
                                date_str: str, time_range: str, attendees: str,
                                transcript_text: str, notes_text: str,
                                minutes_folder_id, creds,
                                event_id: str | None = None,
                                attendees_raw: list | None = None,
                                post_channel: str | None = None,
                                post_thread_ts: str | None = None):
    """내부용·외부용 회의록 생성 → Drive 저장 → Slack 발송 → After Agent 트리거.

    daemon thread에서 호출되는 경우가 많아, 외곽 try/except로 사용자-facing
    오류를 보장한다. silent crash 차단.
    """
    try:
        return _generate_and_post_minutes_impl(
            slack_client, user_id=user_id, title=title,
            date_str=date_str, time_range=time_range, attendees=attendees,
            transcript_text=transcript_text, notes_text=notes_text,
            minutes_folder_id=minutes_folder_id, creds=creds,
            event_id=event_id, attendees_raw=attendees_raw,
            post_channel=post_channel, post_thread_ts=post_thread_ts,
        )
    except Exception as e:
        log.exception(f"회의록 생성 흐름 전체 실패: user={user_id} title={title!r}")
        try:
            from store.user_store import is_token_expired_error
            is_token = is_token_expired_error(e)
        except Exception:
            is_token = False
        try:
            if is_token:
                _post(slack_client, user_id=user_id,
                      channel=post_channel, thread_ts=post_thread_ts,
                      text="🔐 Google 인증이 만료되었어요. `/재등록` 명령으로 다시 인증해주세요.")
            else:
                _post(slack_client, user_id=user_id,
                      channel=post_channel, thread_ts=post_thread_ts,
                      text=f"⚠️ *{title}* 회의록 생성에 실패했어요.\n_에러: {e}_\n\n"
                           f"잠시 후 `/대기회의록` 으로 상태를 확인해보시거나, "
                           f"문제가 계속되면 `/재등록` 시도 또는 관리자에게 알려주세요.")
        except Exception:
            log.exception("회의록 생성 실패 안내 메시지 발송 실패")
        return None


def _generate_and_post_minutes_impl(slack_client, *, user_id: str, title: str,
                                     date_str: str, time_range: str, attendees: str,
                                     transcript_text: str, notes_text: str,
                                     minutes_folder_id, creds,
                                     event_id: str | None = None,
                                     attendees_raw: list | None = None,
                                     post_channel: str | None = None,
                                     post_thread_ts: str | None = None):
    """실제 회의록 생성 본체 — _generate_and_post_minutes의 try/except 래퍼 안쪽."""

    # 입력 비어있는 경우 사전 차단 (recovery flow에서 transcript/notes 모두 빈 경우)
    if not (transcript_text or "").strip() and not (notes_text or "").strip():
        log.warning(f"회의록 생성 입력 모두 비어있음: user={user_id} title={title!r}")
        _post(slack_client, user_id=user_id,
              channel=post_channel, thread_ts=post_thread_ts,
              text=f"⚠️ *{title}* — 트랜스크립트도 노트도 없어 회의록을 만들 자료가 없어요.\n"
                   f"_(트랜스크립트는 Google Meet 종료 후 5~10분 내 도착합니다. "
                   f"수동 노트는 `/메모 [내용]` 으로 추가 가능)_\n"
                   f"이 미팅에 트랜스크립트가 도착하면 자동 생성 시도합니다.")
        return None

    # FR-D15: 복수 미팅 대기열 — 기존 미처리 초안이 있으면 사용자에게 알림
    # 기존 초안 목록 + [📋 자세히] [🗑️ 모두 정리] 버튼 발송
    existing_drafts = [
        (eid, d) for eid, d in _pending_minutes.items()
        if d.get("user_id") == user_id
    ]
    if existing_drafts:
        try:
            blocks = _build_pending_notice_blocks(
                existing=existing_drafts, new_title=title,
            )
            _post(
                slack_client,
                user_id=user_id,
                channel=post_channel or user_id,
                thread_ts=post_thread_ts,
                text=(
                    f"ℹ️ 검토 대기 중인 회의록 {len(existing_drafts)}건이 있어요. "
                    f"*{title}* 회의록을 추가로 생성합니다."
                ),
                blocks=blocks,
            )
        except Exception as e:
            log.warning(f"검토 대기 안내 블록 발송 실패, 텍스트로 폴백: {e}")
            existing_titles = ", ".join(f"*{d['title']}*" for _, d in existing_drafts)
            _post(slack_client, user_id=user_id,
                  text=f"ℹ️ 검토 대기 중인 회의록이 있습니다: {existing_titles}\n"
                       f"*{title}* 회의록을 추가로 생성합니다.")

    meeting_date = f"{date_str} {time_range}".strip()

    # 입력 소스 표기
    sources = []
    if transcript_text:
        sources.append("트랜스크립트")
    if notes_text:
        sources.append("수동 노트")
    source_label = " + ".join(sources) if sources else "없음"

    # ── 긴 트랜스크립트 전처리 ──
    processed_transcript = transcript_text
    if transcript_text and len(transcript_text) > 30000:
        _post(slack_client, user_id=user_id,
              text=f"📊 *{title}* 트랜스크립트가 길어 ({len(transcript_text):,}자) 사전 요약 중...")
        try:
            processed_transcript = _preprocess_transcript(transcript_text, title)
        except Exception as e:
            log.warning(f"트랜스크립트 전처리 실패, 원본 사용: {e}")
            processed_transcript = transcript_text[:40000]

    # ── 회의 유형 분류 (자사/상대 분리용) ──
    # 도메인 휴리스틱 우선 적용. 외부 도메인이 한 명이라도 있으면 vendor/mixed.
    meeting_type = _classify_meeting_type(attendees_raw or [])

    # ── 알려진 엔티티(Companies/People) 로딩 — 위키링크 자동 적용용 ──
    known_entities: list[str] = []
    try:
        from store import user_store as _us
        user_info = _us.get_user(user_id) if user_id else None
        contacts_folder_id = user_info.get("contacts_folder_id") if user_info else None
        if creds and contacts_folder_id:
            from tools.wiki_linker import load_known_entities
            known_entities = load_known_entities(creds, contacts_folder_id)
    except Exception as e:
        log.warning(f"알려진 엔티티 로드 실패 (위키링크 적용 안 됨): {e}")

    # 트랜스크립트 원본 frontmatter source_refs 기본 이름
    source_basename = f"{date_str}_{title}_원문" if transcript_text else None

    # ── 내부용 생성 + 품질 검증 루프 (FR-D09, 최대 2회 재생성) ──
    # Phase 1: Minutes Orchestrator (6단계 파이프라인) 우선 시도, 실패 시 단일 호출 폴백
    _post(slack_client, user_id=user_id,
          text=f"✍️ *{title}* 회의록 생성 중... (유형: {meeting_type})")
    internal_body = None
    used_orchestrator = False

    if minutes_orchestrator.is_enabled():
        try:
            internal_body = minutes_orchestrator.generate_internal_minutes(
                title=title, date=meeting_date, attendees=attendees,
                transcript_text=processed_transcript, notes_text=notes_text,
                attendees_raw=attendees_raw or [],
                meeting_type=meeting_type,
                known_entities=known_entities,
                source_basename=source_basename,
            )
            used_orchestrator = True
            validation = validate_minutes(internal_body, "internal")
            if not validation["valid"]:
                log.warning(f"오케스트레이터 산출 검증 실패, 단일 호출 폴백: 누락={validation.get('missing')}, 금지={validation.get('forbidden')}")
                internal_body = None
                used_orchestrator = False
        except Exception as e:
            log.warning(f"Minutes Orchestrator 실패, 단일 호출 폴백: {e}")
            internal_body = None

    if not used_orchestrator:
        for attempt in range(3):  # 최초 1회 + 재생성 최대 2회
            try:
                internal_body = _generate_minutes(
                    minutes_internal_prompt(title, meeting_date, attendees,
                                            processed_transcript, notes_text)
                )
            except Exception as e:
                log.error(f"내부용 회의록 생성 실패 (시도 {attempt+1}): {e}")
                internal_body = f"## 회의 요약\n(생성 실패: {e})\n\n## 원본\n{notes_text or transcript_text[:2000]}"
                break

            validation = validate_minutes(internal_body, "internal")
            if validation["valid"]:
                break
            if attempt < 2 and validation["missing"]:
                log.info(f"회의록 검증 실패 (시도 {attempt+1}), 재생성: 누락={validation['missing']}")
                processed_transcript = processed_transcript  # 동일 입력으로 재시도
            else:
                log.warning(f"회의록 검증 실패 (최종): 누락={validation.get('missing')}")
                break

    # ── 초안 저장 + 검토 요청 (FR-D14: event_id 키 사용) ──
    draft_key = event_id or f"manual_{user_id}_{int(datetime.now(KST).timestamp())}"
    _pending_minutes[draft_key] = {
        "user_id": user_id,
        "title": title,
        "date_str": date_str,
        "time_range": time_range,
        "attendees": attendees,
        "source_label": source_label,
        "transcript_text": transcript_text,
        "notes_text": notes_text,
        "internal_body": internal_body,
        "minutes_folder_id": minutes_folder_id,
        "creds": creds,
        "event_id": event_id,
        "attendees_raw": attendees_raw or [],
        "meeting_type": meeting_type,
        "known_entities": known_entities,
        "source_basename": source_basename,
        "draft_ts": None,
        # B2: 세션이 채널에서 시작된 경우 초안도 해당 채널(+스레드)로 응답
        "channel": post_channel or user_id,
        "thread_ts": post_thread_ts,
        # 검토 대기 회의록 안내 — 생성 시각 (한국시간 ISO)
        "created_at": datetime.now(KST).isoformat(),
    }
    _save_pending_minutes()
    _post_minutes_draft(slack_client, user_id=user_id, draft_key=draft_key)


def _build_minutes_content(title: str, date_str: str, time_range: str,
                            attendees: str, source: str, body: str,
                            transcript_text: str, notes_text: str,
                            kind: str, *,
                            company_names: list[str] = None,
                            attendee_names: list[str] = None,
                            transcript_source_name: str = None) -> str:
    """Drive 저장용 마크다운 파일 내용 구성 (CM-07: 역링크 포함).

    Obsidian 호환: body가 이미 YAML frontmatter + H1 으로 시작하면 그대로 사용하고,
    옛 양식(frontmatter 없음)이면 기존처럼 # 제목 + ## 기본 정보 헤더를 prepend.
    """
    has_frontmatter = bool(body) and body.lstrip().startswith("---")

    if has_frontmatter:
        # 신규 Obsidian 양식 — body 안에 frontmatter, H1, 섹션이 모두 들어 있음
        lines = [body.rstrip()]
    else:
        lines = [
            f"# {title} ({kind})",
            "",
            "## 기본 정보",
            f"- 날짜: {date_str}",
            f"- 시간: {time_range}",
            f"- 참석자: {attendees}",
            f"- 입력 소스: {source}",
            f"- 구분: {kind}",
            "",
            body or "",
        ]
    if kind == "내부용":
        if transcript_text:
            preview = transcript_text[:3000]
            suffix = "..." if len(transcript_text) > 3000 else ""
            lines += ["", "---", "## 원본 트랜스크립트", preview + suffix]
        if notes_text:
            lines += ["", "---", "## 원본 수동 노트", notes_text]

    content = "\n".join(lines)

    # CM-07: 관련 자료 역링크 추가
    content = drive.add_minutes_backlinks(
        content,
        company_names=company_names,
        attendee_names=attendee_names,
        transcript_source=transcript_source_name,
    )

    return content


def _post_combined_minutes(slack_client, *, user_id: str, title: str,
                            source_label: str, internal_body: str, external_body: str,
                            internal_file_id: str | None, external_file_id: str | None,
                            post_channel: str | None = None,
                            post_thread_ts: str | None = None,
                            minutes_folder_id: str | None = None):
    """내부용·외부용 회의록 Drive 링크를 Slack으로 발송 (B2: 채널/스레드 유지, I3: 클릭 링크 + 폴더)"""
    def drive_link(file_id: str) -> str:
        return f"https://drive.google.com/file/d/{file_id}/view"

    # I3: mrkdwn `<url|text>` 형식으로 클릭 가능한 링크
    if internal_file_id:
        internal_line = f"📄 *내부용*: <{drive_link(internal_file_id)}|Drive에서 열기>"
    else:
        internal_line = "📄 *내부용*: Drive 저장 실패"

    if external_file_id:
        external_line = f"📤 *외부용* (상대방 공유 가능): <{drive_link(external_file_id)}|Drive에서 열기>"
    else:
        external_line = "📤 *외부용*: Drive 저장 실패"

    # I3: 저장 폴더 링크 — Minutes 폴더로 바로 이동 가능
    folder_line = ""
    if minutes_folder_id:
        folder_url = f"https://drive.google.com/drive/folders/{minutes_folder_id}"
        folder_line = f"\n📁 *저장 위치*: <{folder_url}|Minutes 폴더>"

    slack_client.chat_postMessage(
        channel=post_channel or user_id,
        thread_ts=post_thread_ts,
        text=(
            f"*📋 회의록이 생성되었습니다: {title}*  |  _소스: {source_label}_\n"
            f"{internal_line}\n"
            f"{external_line}"
            f"{folder_line}"
            + _hint("양식이 깨지면 `/회의록정리` / 같은 미팅 일정 수정은 `/미팅편집`")
        ),
    )


# ── 회의록 검토 단계 ──────────────────────────────────────────


def _post_minutes_draft(slack_client, *, user_id: str, draft_key: str = None):
    """내부용 회의록 미리보기 + 확인/수정/취소 버튼 발송.
    minutes_folder_id가 있으면 Google Docs 초안을 생성하여 직접 편집 링크도 제공.
    """
    # FR-D14: draft_key(event_id)로 조회, 없으면 user_id로 역방향 조회
    if draft_key:
        draft = _pending_minutes.get(draft_key)
    else:
        found = _find_draft_for_user(user_id)
        if found:
            draft_key, draft = found
        else:
            draft = None
    if not draft:
        return

    title = draft["title"]
    date_str = draft["date_str"]
    internal_body = draft["internal_body"]
    minutes_folder_id = draft.get("minutes_folder_id")
    creds = draft.get("creds")

    # ── Google Docs 초안 생성 (직접 편집용) ──
    doc_id = draft.get("draft_doc_id")
    if not doc_id and minutes_folder_id and creds:
        try:
            doc_id = drive.create_draft_doc(
                creds,
                f"{date_str}_{title}_초안(편집용).gdoc",
                internal_body,
                minutes_folder_id,
            )
            draft["draft_doc_id"] = doc_id
            log.info(f"회의록 초안 Google Doc 생성: {doc_id}")
        except Exception as e:
            log.warning(f"초안 Google Doc 생성 실패 (무시): {e}")

    # ── 미리보기: 최대 2500자 ──
    preview = internal_body[:2500]
    if len(internal_body) > 2500:
        preview += "\n\n_(이하 생략)_"

    # ── Block Kit 구성 (FR-D14: 버튼에 draft_key 포함) ──
    action_elements = [
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "✅ 저장 및 완료"},
            "action_id": "minutes_confirm",
            "style": "primary",
            "value": draft_key or "",
        },
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "✏️ 수정 요청"},
            "action_id": "minutes_edit_request",
            "value": draft_key or "",
        },
        {
            "type": "button",
            "text": {"type": "plain_text", "text": "❌ 취소"},
            "action_id": "minutes_cancel",
            "style": "danger",
            "value": draft_key or "",
        },
    ]
    if doc_id:
        action_elements.insert(1, {
            "type": "button",
            "text": {"type": "plain_text", "text": "📝 직접 편집"},
            "url": f"https://docs.google.com/document/d/{doc_id}/edit",
            "action_id": "minutes_open_doc",
        })

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"📋 회의록 초안 검토: {title}"},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*내부용 회의록 미리보기*\n\n{preview}"},
        },
        {"type": "divider"},
        {"type": "actions", "elements": action_elements},
    ]
    # 사용 팁 — 사용자가 흐름을 직관적으로 파악하기 어려우므로 카드 하단에 항상 노출
    tip_lines = []
    if doc_id:
        tip_lines.append("📝 _*직접 편집* 후 *✅ 저장 및 완료* 를 누르면 편집된 내용으로 최종 저장됩니다._")
    tip_lines.append(
        "💡 _*✏️ 수정 요청* 클릭 후 스레드에 답글을 달면 LLM이 새 초안을 다시 발송합니다 — "
        "이전 초안 카드는 무시하고 *새로 발송된 카드*에서 저장/편집해주세요._"
    )
    tip_lines.append(
        "🔄 _처음부터 다시 만들고 싶으면: *❌ 취소* → `/미팅종료` 재실행 "
        "(또는 `📎 트랜스크립트 첨부`로 직접 업로드한 텍스트로 재생성)._"
    )
    blocks.append({
        "type": "context",
        "elements": [{"type": "mrkdwn", "text": "\n".join(tip_lines)}],
    })

    # B2: 세션이 채널에서 시작된 경우 draft["channel"]에 채널 ID 저장되어 있음
    post_channel = draft.get("channel") or user_id
    post_thread_ts = draft.get("thread_ts")
    resp = slack_client.chat_postMessage(
        channel=post_channel,
        thread_ts=post_thread_ts,
        text=f"📋 회의록 초안이 작성되었습니다: *{title}*\n내용을 확인하고 저장하거나 수정 요청해주세요.",
        blocks=blocks,
    )
    if resp and resp.get("ok"):
        draft["draft_ts"] = resp["ts"]


def finalize_minutes(slack_client, user_id: str, draft_key: str = None):
    """회의록 저장 및 완료 — Drive 저장 + Slack 발송 + After Agent"""
    # FR-D14: draft_key(event_id)로 조회, 없으면 user_id로 역방향 조회
    if draft_key:
        draft = _pending_minutes.pop(draft_key, None)
    else:
        found = _find_draft_for_user(user_id)
        if found:
            draft_key, draft = found
            _pending_minutes.pop(draft_key, None)
        else:
            draft = None
    if draft:
        _save_pending_minutes()
    if not draft:
        slack_client.chat_postMessage(
            channel=user_id,
            text="⚠️ 저장할 회의록 초안이 없습니다.",
        )
        return

    title = draft["title"]
    date_str = draft["date_str"]
    time_range = draft["time_range"]
    attendees = draft["attendees"]
    source_label = draft["source_label"]
    internal_body = draft["internal_body"]
    transcript_text = draft["transcript_text"]
    notes_text = draft["notes_text"]
    minutes_folder_id = draft["minutes_folder_id"]
    creds = draft["creds"]
    event_id = draft["event_id"]
    attendees_raw = draft["attendees_raw"]
    draft_doc_id = draft.get("draft_doc_id")
    meeting_date = f"{date_str} {time_range}".strip()

    # ── Google Doc 직접 편집 내용 반영 ──
    if draft_doc_id and creds:
        try:
            edited = docs.read_document(creds, draft_doc_id)
            if edited and edited.strip() != internal_body.strip():
                log.info(f"Google Doc 편집 내용 반영: {title}")
                internal_body = edited.strip()
        except Exception as e:
            log.warning(f"Google Doc 읽기 실패, 원본 사용: {e}")
        # 편집용 초안 Doc 삭제 (정리)
        drive.delete_file(creds, draft_doc_id)

    # ── 외부용 생성 + 품질 검증 (FR-D10: 금지 키워드 자동 제거) ──
    _post(slack_client, user_id=user_id, text=f"✍️ *{title}* 외부용 회의록 생성 중...")
    external_body = None
    for attempt in range(3):
        try:
            extra_instruction = ""
            if attempt > 0:
                extra_instruction = "\n\n⚠️ 주의: 다음 키워드는 절대 포함하지 마세요: 내부 메모, 협상, 전략"
            external_body = _generate_minutes(
                minutes_external_prompt(title, meeting_date, attendees, internal_body)
                + extra_instruction
            )
        except Exception as e:
            log.error(f"외부용 회의록 생성 실패 (시도 {attempt+1}): {e}")
            external_body = f"## 회의 개요\n(생성 실패: {e})\n"
            break

        validation = validate_minutes(external_body, "external")
        if validation["valid"]:
            break
        if attempt < 2 and (validation["missing"] or validation["forbidden"]):
            log.info(f"외부용 검증 실패 (시도 {attempt+1}): 누락={validation.get('missing')} 금지={validation.get('forbidden')}")
        else:
            # 최종 시도에서도 금지 키워드가 있으면 해당 문장 제거
            if validation.get("forbidden"):
                for kw in validation["forbidden"]:
                    external_body = "\n".join(
                        line for line in external_body.split("\n")
                        if kw not in line
                    )
                log.warning(f"외부용 금지 키워드 문장 제거: {validation['forbidden']}")
            break

    _post(slack_client, user_id=user_id, text=f"💾 *{title}* Drive에 회의록 저장 중...")

    # CM-07/08/10: Wiki 관련 메타데이터 추출
    company_names = []
    attendee_names = []
    contacts_folder_id = None
    try:
        user_info = user_store.get_user(user_id)
        contacts_folder_id = user_info.get("contacts_folder_id") if user_info else None
    except Exception:
        pass

    # 이벤트에서 업체명 추출
    if event_id and creds:
        try:
            ev = cal.get_event(creds, event_id)
            company_raw = (ev.get("extendedProperties", {})
                           .get("private", {}).get("company", ""))
            company_names = [c.strip() for c in company_raw.split(",") if c.strip()]
        except Exception:
            pass

    # 참석자 이름 추출
    for a in (attendees_raw or []):
        name = a.get("displayName") or a.get("name", "")
        if name:
            attendee_names.append(name)

    # CM-10: Sources/Transcripts/ 에 트랜스크립트 원본 저장
    transcript_source_name = None
    if transcript_text and contacts_folder_id and creds:
        try:
            transcript_filename = f"{date_str}_{title}_transcript.md"
            transcript_source_name = f"Sources/Transcripts/{transcript_filename}"
            source_content = (
                f"# {title} 트랜스크립트 원본\n"
                f"- 날짜: {date_str}\n"
                f"- 참석자: {attendees}\n\n"
                f"{transcript_text}"
            )
            drive.save_source_file(
                creds, contacts_folder_id, "Transcripts",
                transcript_filename, source_content,
            )
            log.info(f"Sources/Transcripts 저장: {transcript_filename}")
        except Exception as e:
            log.warning(f"Sources/Transcripts 저장 실패: {e}")
            transcript_source_name = None

    internal_file_id = external_file_id = None
    minutes_filename = f"{date_str}_{title}_내부용"

    if minutes_folder_id:
        internal_content = _build_minutes_content(
            title, date_str, time_range, attendees, source_label,
            internal_body, transcript_text, notes_text, kind="내부용",
            company_names=company_names,
            attendee_names=attendee_names,
            transcript_source_name=transcript_source_name,
        )
        external_content = _build_minutes_content(
            title, date_str, time_range, attendees, source_label,
            external_body, "", "", kind="외부용",
            company_names=company_names,
            attendee_names=attendee_names,
        )
        try:
            internal_file_id = drive.save_minutes(
                creds, minutes_folder_id,
                f"{date_str}_{title}_내부용.md", internal_content
            )
            external_file_id = drive.save_minutes(
                creds, minutes_folder_id,
                f"{date_str}_{title}_외부용.md", external_content
            )
            log.info(f"회의록 저장: {title} 내부용={internal_file_id} 외부용={external_file_id}")

            # CM-08: 기업·인물 파일 미팅 히스토리 갱신
            if contacts_folder_id and creds:
                for cn in company_names:
                    try:
                        drive.append_meeting_history_company(
                            creds, contacts_folder_id, cn,
                            date_str, title, minutes_filename, attendee_names,
                        )
                    except Exception as e:
                        log.warning(f"기업 미팅 히스토리 갱신 실패 ({cn}): {e}")
                for an in attendee_names:
                    try:
                        drive.append_meeting_history_person(
                            creds, contacts_folder_id, an,
                            date_str, title, minutes_filename,
                        )
                    except Exception as e:
                        log.warning(f"인물 미팅 히스토리 갱신 실패 ({an}): {e}")

                # CM-07: 상호 참조 링크 삽입
                if company_names and attendee_names:
                    try:
                        for cn in company_names:
                            drive.add_wiki_cross_references(
                                creds, contacts_folder_id, cn, attendee_names,
                            )
                    except Exception as e:
                        log.warning(f"Wiki 상호 참조 삽입 실패: {e}")

            # INF-10: meeting_index에 자동 등록
            try:
                import json as _json
                user_store.save_meeting_index(
                    event_id=event_id or f"manual_{date_str}_{title}",
                    user_id=user_id,
                    date=date_str,
                    title=title,
                    company_name=", ".join(company_names) if company_names else None,
                    attendees=_json.dumps(attendees_raw, ensure_ascii=False) if attendees_raw else None,
                    drive_file_id=internal_file_id,
                    drive_link=f"https://drive.google.com/file/d/{internal_file_id}/view" if internal_file_id else None,
                )
            except Exception as idx_err:
                log.warning(f"meeting_index 등록 실패: {idx_err}")
        except Exception as e:
            log.error(f"회의록 Drive 저장 실패: {e}")

    _post_combined_minutes(
        slack_client, user_id=user_id,
        title=title, source_label=source_label,
        internal_body=internal_body, external_body=external_body,
        internal_file_id=internal_file_id,
        external_file_id=external_file_id,
        post_channel=draft.get("channel"),
        post_thread_ts=draft.get("thread_ts"),
        minutes_folder_id=minutes_folder_id,
    )

    threading.Thread(
        target=after.trigger_after_meeting,
        kwargs=dict(
            slack_client=slack_client,
            user_id=user_id,
            event_id=event_id,
            title=title,
            date_str=date_str,
            attendees_raw=attendees_raw,
            internal_body=internal_body,
            external_body=external_body,
            creds=creds,
        ),
        daemon=True,
    ).start()


def cancel_minutes(slack_client, user_id: str, draft_key: str = None):
    """회의록 초안 취소"""
    # FR-D14: draft_key(event_id)로 조회, 없으면 user_id로 역방향 조회
    if draft_key:
        draft = _pending_minutes.pop(draft_key, None)
    else:
        found = _find_draft_for_user(user_id)
        if found:
            draft_key, draft = found
            _pending_minutes.pop(draft_key, None)
        else:
            draft = None
    if draft:
        _save_pending_minutes()
    title = draft["title"] if draft else "회의록"
    slack_client.chat_postMessage(
        channel=user_id,
        text=f"❌ *{title}* 회의록 초안을 삭제했습니다.",
    )


def request_minutes_edit(slack_client, user_id: str, draft_key: str = None):
    """수정 요청 — 초안 스레드에 안내 메시지 발송"""
    # FR-D14: draft_key로 조회, 없으면 user_id로 역방향 조회
    if draft_key:
        draft = _pending_minutes.get(draft_key)
    else:
        found = _find_draft_for_user(user_id)
        draft = found[1] if found else None
    if not draft:
        slack_client.chat_postMessage(
            channel=user_id,
            text="⚠️ 수정할 회의록 초안이 없습니다.",
        )
        return

    resp = slack_client.chat_postMessage(
        channel=draft.get("channel") or user_id,
        thread_ts=draft["draft_ts"],
        text=(
            "✏️ 수정할 내용을 이 스레드에 답글로 작성해주세요.\n"
            "예: _'액션아이템의 기한을 다음 주 금요일로 수정해줘'_, _'담당자 이름을 홍길동으로 변경해줘'_\n\n"
            "💡 답글을 받으면 LLM이 *새 초안 카드*를 다시 발송합니다. "
            "*이전 초안 카드는 무시*하고 새 카드에서 저장/편집/취소해주세요. "
            "아무 카드든 ✅ 저장은 한 번만 처리됩니다 (이중 저장 방지)."
        ),
    )
    if resp and resp.get("ok"):
        draft["edit_prompt_ts"] = resp["ts"]


def handle_minutes_edit_reply(slack_client, user_id: str, edit_text: str,
                               thread_ts: str | None = None):
    """수정 요청 텍스트로 회의록 재생성 후 새 초안 발송.
    thread_ts가 있으면 그 스레드에 연결된 정확한 초안을 타겟 (B3)."""
    # 우선순위: (1) thread_ts로 정확히 일치하는 초안 (2) fallback: user_id의 첫 초안
    found = find_draft_by_thread_ts(user_id, thread_ts) if thread_ts else None
    if not found:
        found = _find_draft_for_user(user_id)
    if found:
        draft_key, draft = found
    else:
        draft_key, draft = None, None
    if not draft:
        return

    title = draft["title"]
    _post(slack_client, user_id=user_id, text=f"🔄 *{title}* 회의록 수정 중...")

    # 기존 내부용에 수정 지시를 더해 재생성 (외부용은 '저장 및 완료' 후 생성)
    edit_prompt = (
        f"다음 회의록을 아래 수정 요청에 따라 수정해줘. 반드시 한국어로.\n\n"
        f"[기존 회의록]\n{draft['internal_body']}\n\n"
        f"[수정 요청]\n{edit_text}\n\n"
        f"수정 규칙:\n"
        f"1. 요청된 부분만 정확히 수정하고, 나머지 내용과 구조는 그대로 유지\n"
        f"2. 섹션 헤더(##)와 마크다운 형식을 동일하게 유지\n"
        f"3. 요청에 없는 내용을 임의로 추가·삭제하지 말 것\n"
        f"4. 수정된 전체 회의록을 동일한 마크다운 형식으로 반환해줘"
    )
    try:
        new_internal = _generate_minutes(edit_prompt)
    except Exception as e:
        log.error(f"회의록 수정 실패: {e}")
        _post(slack_client, user_id=user_id, text=f"⚠️ 회의록 수정 실패: {e}")
        return

    draft["internal_body"] = new_internal
    draft["draft_ts"] = None  # 새 메시지로 재발송
    _save_pending_minutes()
    _post_minutes_draft(slack_client, user_id=user_id, draft_key=draft_key)


# ── 검토 대기 회의록 관리 (대기 목록 / 일괄 정리) ─────────────


def _format_pending_age(created_at_iso: str | None) -> str:
    """검토 대기 회의록의 경과 시간 표시.

    "방금 전" / "N분 전" / "N시간 전" / "N일 전" / "N주 전" 형태.
    `created_at_iso`가 없거나 파싱 실패면 "이전" 반환.
    """
    if not created_at_iso:
        return "이전"
    try:
        created = datetime.fromisoformat(created_at_iso)
        if created.tzinfo is None:
            created = created.replace(tzinfo=KST)
    except Exception:
        return "이전"

    now = datetime.now(KST)
    delta = now - created
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "방금 전"
    if seconds < 60:
        return "방금 전"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}분 전"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}시간 전"
    days = hours // 24
    if days < 7:
        return f"{days}일 전"
    weeks = days // 7
    return f"{weeks}주 전"


def _pending_drafts_for_user(user_id: str) -> list[tuple[str, dict]]:
    """user_id의 검토 대기 회의록을 오래된 순으로 정렬하여 반환.

    정렬 키: created_at(있으면) → 없는 항목은 가장 오래된 것으로 간주(맨 앞).
    """
    items = [
        (key, draft) for key, draft in _pending_minutes.items()
        if draft.get("user_id") == user_id
    ]

    def _sort_key(it):
        created = (it[1] or {}).get("created_at") or ""
        # created_at 없는 항목은 빈 문자열 → 가장 앞으로
        return created

    items.sort(key=_sort_key)
    return items


def _source_label_short(draft: dict) -> str:
    """초안의 입력 소스를 짧게 표시 (트랜스크립트 / 노트만 / 트랜스크립트+노트 / 없음)."""
    label = (draft or {}).get("source_label")
    if label:
        return label
    if draft.get("transcript_text") and draft.get("notes_text"):
        return "트랜스크립트 + 수동 노트"
    if draft.get("transcript_text"):
        return "트랜스크립트"
    if draft.get("notes_text"):
        return "노트만"
    return "없음"


def _build_pending_notice_blocks(*, existing: list[tuple[str, dict]],
                                  new_title: str) -> list[dict]:
    """새 회의록 생성 시 표시되는 "검토 대기 안내" 메시지 블록 (FR-D15 개선판).

    상단 안내 + 상위 2건 미리보기 + [📋 대기 목록 자세히] [🗑️ 모두 정리] 버튼.
    """
    count = len(existing)
    sorted_items = sorted(
        existing,
        key=lambda it: (it[1] or {}).get("created_at") or "",
    )

    # 미리보기는 상위 2건만 (오래된 순)
    preview_lines = []
    for _, draft in sorted_items[:2]:
        title = draft.get("title") or "(제목 없음)"
        age = _format_pending_age(draft.get("created_at"))
        preview_lines.append(f"• {title} ({age})")
    if count > 2:
        preview_lines.append(f"_…외 {count - 2}건_")

    header_text = (
        f"ℹ️ 검토 대기 중인 회의록 *{count}건*이 있어요.\n"
        f"이번엔 *{new_title}* 회의록을 추가로 생성합니다."
    )

    blocks: list[dict] = [
        {"type": "section", "text": {"type": "mrkdwn", "text": header_text}},
    ]
    if preview_lines:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "*대기 목록*\n" + "\n".join(preview_lines),
            },
        })
    blocks.append({
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "📋 대기 목록 자세히"},
                "action_id": "pending_drafts_view",
                "value": "view",
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "🗑️ 모두 정리"},
                "action_id": "pending_drafts_cleanup_all",
                "style": "danger",
                "value": "cleanup_all",
            },
        ],
    })
    return blocks


def build_pending_drafts_blocks(user_id: str,
                                 slack_client=None) -> list[dict]:
    """검토 대기 회의록 전체 목록 메시지 블록 생성.

    각 항목에 [📝 검토] [🗑️ 버리기] 버튼, 하단에 [🗑️ 모두 정리] 버튼.
    `slack_client` 인자는 호출 호환성을 위해 받지만 현재는 사용하지 않음.
    """
    items = _pending_drafts_for_user(user_id)
    count = len(items)

    if count == 0:
        return [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "✅ 검토 대기 중인 회의록이 없습니다.",
                },
            }
        ]

    blocks: list[dict] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"📋 검토 대기 회의록 {count}건",
            },
        }
    ]

    for idx, (key, draft) in enumerate(items, start=1):
        title = draft.get("title") or "(제목 없음)"
        created_at = draft.get("created_at")
        age = _format_pending_age(created_at)
        # 생성 시각 표시 — ISO 파싱 가능하면 'YYYY-MM-DD HH:MM'
        created_disp = "-"
        if created_at:
            try:
                dt = datetime.fromisoformat(created_at)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=KST)
                created_disp = dt.astimezone(KST).strftime("%Y-%m-%d %H:%M")
            except Exception:
                created_disp = created_at
        source = _source_label_short(draft)
        body = (
            f"*{idx}. {title}*\n"
            f"_생성_: {created_disp} ({age}) · _입력_: {source}"
        )
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": body},
        })
        blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "📝 검토"},
                    "action_id": "pending_draft_review",
                    "value": key,
                    "style": "primary",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "🗑️ 버리기"},
                    "action_id": "pending_draft_discard",
                    "value": key,
                    "style": "danger",
                },
            ],
        })

    # 하단 — 모두 정리 버튼
    blocks.append({"type": "divider"})
    blocks.append({
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "🗑️ 모두 정리"},
                "action_id": "pending_drafts_cleanup_all",
                "style": "danger",
                "value": "cleanup_all",
            }
        ],
    })
    return blocks


def post_pending_drafts(slack_client, *, user_id: str,
                         channel: str | None = None,
                         thread_ts: str | None = None) -> None:
    """검토 대기 회의록 목록 메시지 발송 (슬래시·인텐트·버튼 진입점)."""
    blocks = build_pending_drafts_blocks(user_id, slack_client)
    items = _pending_drafts_for_user(user_id)
    fallback = (
        f"📋 검토 대기 회의록 {len(items)}건"
        if items else "검토 대기 회의록이 없습니다."
    )
    try:
        slack_client.chat_postMessage(
            channel=channel or user_id,
            thread_ts=thread_ts,
            text=fallback,
            blocks=blocks,
        )
    except Exception as e:
        log.warning(f"검토 대기 회의록 목록 발송 실패: {e}")


def _replace_message_blocks(slack_client, body: dict,
                             *, blocks: list[dict], text: str) -> None:
    """버튼 클릭 후 원본 메시지를 새 블록으로 치환 (chat_update)."""
    channel = (
        (body.get("channel") or {}).get("id")
        or (body.get("container") or {}).get("channel_id")
    )
    msg_ts = (
        (body.get("message") or {}).get("ts")
        or (body.get("container") or {}).get("message_ts")
    )
    if not (channel and msg_ts):
        return
    try:
        slack_client.chat_update(
            channel=channel,
            ts=msg_ts,
            text=text,
            blocks=blocks,
        )
    except Exception as e:
        log.warning(f"chat_update 실패 (무시): {e}")


def _replace_clicked_actions_with_context(slack_client, body: dict,
                                            status_label: str) -> None:
    """클릭된 actions 블록을 상태 텍스트로 교체 (좀비 버튼 방지).
    todo._disable_clicked_action_block 와 동일 패턴 — 단일 회의록 항목 정리용.
    """
    channel = (
        (body.get("channel") or {}).get("id")
        or (body.get("container") or {}).get("channel_id")
    )
    msg_ts = (
        (body.get("message") or {}).get("ts")
        or (body.get("container") or {}).get("message_ts")
    )
    if not (channel and msg_ts):
        return
    clicked_value = (body.get("actions") or [{}])[0].get("value", "")
    if not clicked_value:
        return
    original_blocks = (body.get("message") or {}).get("blocks") or []
    if not original_blocks:
        return

    new_blocks: list[dict] = []
    for blk in original_blocks:
        if blk.get("type") == "actions":
            elements = blk.get("elements", []) or []
            if any((el.get("value") or "") == clicked_value for el in elements):
                new_blocks.append({
                    "type": "context",
                    "elements": [{"type": "mrkdwn",
                                  "text": f"_✓ {status_label}_"}],
                })
                continue
        new_blocks.append(blk)

    try:
        slack_client.chat_update(
            channel=channel,
            ts=msg_ts,
            text=(body.get("message") or {}).get("text") or "검토 대기 회의록",
            blocks=new_blocks,
        )
    except Exception as e:
        log.warning(f"chat_update 실패 (무시): {e}")


def handle_pending_view_button(slack_client, body: dict) -> None:
    """[📋 대기 목록 자세히] 버튼 → 검토 대기 회의록 전체 목록 발송."""
    user_id = (body.get("user") or {}).get("id")
    if not user_id:
        return
    channel = (
        (body.get("channel") or {}).get("id")
        or (body.get("container") or {}).get("channel_id")
        or user_id
    )
    thread_ts = (body.get("message") or {}).get("thread_ts")
    post_pending_drafts(slack_client, user_id=user_id,
                         channel=channel, thread_ts=thread_ts)


def handle_pending_review_button(slack_client, body: dict) -> None:
    """[📝 검토] — 해당 초안을 다시 발송 (`_post_minutes_draft`)."""
    user_id = (body.get("user") or {}).get("id")
    draft_key = (body.get("actions") or [{}])[0].get("value", "")
    if not (user_id and draft_key):
        return
    draft = _pending_minutes.get(draft_key)
    if not draft or draft.get("user_id") != user_id:
        try:
            slack_client.chat_postMessage(
                channel=user_id,
                text="⚠️ 해당 회의록 초안을 찾을 수 없습니다 (이미 처리되었을 수 있어요).",
            )
        except Exception:
            pass
        return
    # 새 검토 메시지 발송 — 기존 draft_ts 무효화
    draft["draft_ts"] = None
    _save_pending_minutes()
    _post_minutes_draft(slack_client, user_id=user_id, draft_key=draft_key)


def handle_pending_discard_button(slack_client, body: dict) -> None:
    """[🗑️ 버리기] — 단일 초안 삭제 (확인 없이 즉시 처리)."""
    user_id = (body.get("user") or {}).get("id")
    draft_key = (body.get("actions") or [{}])[0].get("value", "")
    if not (user_id and draft_key):
        return
    draft = _pending_minutes.get(draft_key)
    if not draft or draft.get("user_id") != user_id:
        # 이미 정리된 경우에도 UX 일관성을 위해 안내만
        _replace_clicked_actions_with_context(slack_client, body, "이미 처리됨")
        return
    title = draft.get("title") or "(제목 없음)"
    # 단일 항목 삭제 — _save_pending_minutes로 영속화
    _pending_minutes.pop(draft_key, None)
    _save_pending_minutes()
    log.info(f"검토 대기 회의록 단일 버리기: user={user_id} key={draft_key} title={title}")
    _replace_clicked_actions_with_context(
        slack_client, body, f"버림 — {title}",
    )


def _build_cleanup_confirm_blocks(count: int) -> list[dict]:
    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"⚠️ 검토 대기 회의록 *{count}건*을 모두 버립니다. "
                    f"되돌릴 수 없어요."
                ),
            },
        },
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "✅ 모두 버림"},
                    "action_id": "pending_drafts_cleanup_confirm",
                    "style": "danger",
                    "value": "confirm",
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "❌ 취소"},
                    "action_id": "pending_drafts_cleanup_cancel",
                    "value": "cancel",
                },
            ],
        },
    ]


def handle_pending_cleanup_all_button(slack_client, body: dict) -> None:
    """[🗑️ 모두 정리] — 일괄 삭제 확인 프롬프트 발송."""
    user_id = (body.get("user") or {}).get("id")
    if not user_id:
        return
    channel = (
        (body.get("channel") or {}).get("id")
        or (body.get("container") or {}).get("channel_id")
        or user_id
    )
    thread_ts = (body.get("message") or {}).get("thread_ts")
    items = _pending_drafts_for_user(user_id)
    count = len(items)
    if count == 0:
        try:
            slack_client.chat_postMessage(
                channel=channel,
                thread_ts=thread_ts,
                text="✅ 검토 대기 회의록이 없습니다.",
            )
        except Exception:
            pass
        return
    blocks = _build_cleanup_confirm_blocks(count)
    try:
        slack_client.chat_postMessage(
            channel=channel,
            thread_ts=thread_ts,
            text=f"⚠️ 검토 대기 회의록 {count}건 모두 버리시겠어요?",
            blocks=blocks,
        )
    except Exception as e:
        log.warning(f"일괄 정리 확인 메시지 발송 실패: {e}")


def handle_pending_cleanup_confirm_button(slack_client, body: dict) -> None:
    """[✅ 모두 버림] — 검토 대기 회의록 전체 삭제 (실제 정리)."""
    user_id = (body.get("user") or {}).get("id")
    if not user_id:
        return
    items = _pending_drafts_for_user(user_id)
    count = len(items)
    for key, _draft in items:
        _pending_minutes.pop(key, None)
    if items:
        _save_pending_minutes()
    log.info(f"검토 대기 회의록 일괄 정리: user={user_id} count={count}")

    # 원본 확인 메시지를 결과 안내로 치환
    new_blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"🗑️ 검토 대기 회의록 *{count}건*을 모두 버렸습니다."
                    if count else "✅ 정리할 회의록이 없습니다."
                ),
            },
        }
    ]
    _replace_message_blocks(
        slack_client, body, blocks=new_blocks,
        text=f"🗑️ 검토 대기 회의록 {count}건 정리 완료",
    )


def handle_pending_cleanup_cancel_button(slack_client, body: dict) -> None:
    """[❌ 취소] — 일괄 정리 확인 취소 (메시지에서 버튼 제거)."""
    new_blocks = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": "↩️ 일괄 정리를 취소했습니다.",
            },
        }
    ]
    _replace_message_blocks(
        slack_client, body, blocks=new_blocks,
        text="일괄 정리 취소됨",
    )


# ── 유틸리티 ──────────────────────────────────────────────────


def _format_notes(notes: list) -> str:
    """노트 리스트를 텍스트로 변환"""
    if not notes:
        return ""
    return "\n".join(f"[{n['time']}] {n['text']}" for n in notes)


def _parse_meeting_meta(meeting: dict) -> tuple[str, str, str]:
    """meeting dict에서 date_str, time_range, attendees_str 추출"""
    start_str = meeting.get("start_time", "")
    end_str = meeting.get("end_time", "")
    attendees_list = meeting.get("attendees", [])

    date_str = datetime.now(KST).strftime("%Y-%m-%d")
    time_range = ""
    try:
        start_dt = datetime.fromisoformat(start_str)
        end_dt = datetime.fromisoformat(end_str)
        date_str = start_dt.strftime("%Y-%m-%d")
        time_range = f"{start_dt.strftime('%H:%M')} ~ {end_dt.strftime('%H:%M')}"
    except Exception:
        pass

    attendees_str = ", ".join(
        a.get("name") or a.get("email", "") for a in attendees_list
    ) or "정보 없음"

    return date_str, time_range, attendees_str


# ── 회의록 목록 조회 ──────────────────────────────────────────


def get_minutes_list(slack_client, user_id: str, channel: str = None, thread_ts: str = None):
    """/회의록 — 저장된 회의록 목록 조회.

    상위 10건 한정으로 *경량* 양식 진단을 수행하여 ⚠️ 마커 + `[🔧 양식 보정]` 버튼을 표시.
    경량 진단은 본문 전체를 읽지 않고 frontmatter 유무만 확인 (성능 보호).
    """
    try:
        creds, minutes_folder_id = _get_creds_and_config(user_id)
    except Exception as e:
        # 토큰 만료는 친화적 안내, 그 외는 raw 에러
        if user_store.is_token_expired_error(e):
            _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
                  text="🔐 Google 인증이 만료되었어요.\n`/재등록` 명령으로 다시 인증해주세요.")
        else:
            _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
                  text=f"⚠️ 인증 오류: {e}")
        return

    if not minutes_folder_id:
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text="⚠️ Minutes 폴더가 설정되지 않았습니다. `/재등록` 으로 재인증해주세요.")
        return

    try:
        files = drive.list_minutes(creds, minutes_folder_id)
    except Exception as e:
        if user_store.is_token_expired_error(e):
            _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
                  text="🔐 Google 인증이 만료되었어요.\n`/재등록` 명령으로 다시 인증해주세요.")
        else:
            _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
                  text=f"⚠️ 회의록 조회 실패: {e}")
        return

    if not files:
        _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
              text="📁 저장된 회의록이 없습니다.")
        return

    # 양식 깨짐 lazy 진단 — 상위 10건만 본문 읽어 경량 진단
    try:
        from agents import minutes_normalizer
    except Exception:
        minutes_normalizer = None  # 안전 폴백

    blocks: list[dict] = [
        {"type": "section",
         "text": {"type": "mrkdwn", "text": f"*📋 저장된 회의록 ({len(files)}개)*"}},
        {"type": "divider"},
    ]

    for f in files[:10]:
        name = f.get("name", "").replace(".md", "")
        modified = f.get("modifiedTime", "")[:10]
        file_id = f.get("id", "")
        link = f"https://drive.google.com/file/d/{file_id}/view" if file_id else ""

        # 경량 진단 — 본문 head 만 읽음 (실패 시 진단 생략)
        broken = False
        if minutes_normalizer and file_id:
            try:
                content_head = drive._read_file(creds, file_id)
                # 너무 긴 문서 보호 — 처음 4KB 만 본다
                diag = minutes_normalizer.diagnose_minutes_light(content_head[:4096])
                broken = bool(diag.get("needs_normalization"))
            except Exception as e:
                log.warning(f"경량 진단 실패 ({name}): {e}")

        warning_tag = " ⚠️ 양식 깨짐" if broken else ""
        link_text = f"  <{link}|열기>" if link else ""
        block: dict = {
            "type": "section",
            "text": {"type": "mrkdwn",
                     "text": f"• *{name}*  _{modified}_{link_text}{warning_tag}"},
        }
        if broken and file_id:
            block["accessory"] = {
                "type": "button",
                "text": {"type": "plain_text", "text": "🔧 양식 보정"},
                "action_id": "summon_minutes_for_normalize",
                "value": file_id,
            }
        blocks.append(block)

    if len(files) > 10:
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn",
                          "text": f"_...외 {len(files) - 10}개 (상위 10건만 진단)_"}],
        })

    _post(slack_client, user_id=user_id, channel=channel, thread_ts=thread_ts,
          text=f"📋 저장된 회의록 ({len(files)}개)", blocks=blocks)
