"""피드백 트리아지 4차 묶음(F1·F2·I2·I3·I4) 단위 테스트.

4차 묶음 범위:
- F2: cancel_meeting_from_text, handle_meeting_cancel_confirm/abort
- F1: suggest_meeting_slots, _find_free_slots, handle_slot_create_meeting
- I2(a): _post_create_preview, handle_create_confirm/abort
- I2(b): offer_room_booking, handle_room_offer_show/skip
- I3: _post_combined_minutes 링크 포맷
"""
import os
os.environ.setdefault("GOOGLE_API_KEY", "test-key")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")

import json
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

with patch("anthropic.Anthropic"):
    import agents.before as before
    import agents.during as during
    from agents.before import (
        cancel_meeting_from_text,
        handle_meeting_cancel_confirm,
        handle_meeting_cancel_abort,
        suggest_meeting_slots,
        handle_slot_create_meeting,
        _find_free_slots,
        _post_create_preview,
        handle_create_confirm,
        handle_create_abort,
        offer_room_booking,
        handle_room_offer_show,
        handle_room_offer_skip,
        _pending_create_confirm,
        _pending_room_offer,
    )

KST = ZoneInfo("Asia/Seoul")
_TEST_USER = "UTEST"


def _slack():
    client = MagicMock()
    client.chat_postMessage.return_value = {"ts": "1.1", "ok": True}
    return client


def _blocks_action_ids(call) -> set:
    """Slack chat_postMessage/chat_update 호출에서 사용된 action_id 집합 반환"""
    ids = set()
    for b in call[1].get("blocks", []) or []:
        for el in b.get("elements", []) or []:
            aid = el.get("action_id")
            if aid:
                ids.add(aid)
    return ids


# ═══════════════════════════════════════════════════════════════════
# F1: _find_free_slots 알고리즘
# ═══════════════════════════════════════════════════════════════════

class TestFindFreeSlots:
    def test_no_busy_returns_from_first_preferred_hour(self):
        """바쁜 시간 없으면 근무시간 첫 시각부터 슬롯 반환"""
        tmin = datetime(2026, 4, 20, 0, 0, tzinfo=KST)  # 월요일
        tmax = datetime(2026, 4, 20, 23, 59, tzinfo=KST)
        slots = _find_free_slots(tmin, tmax, [], 60, list(range(9, 18)), max_results=3)
        assert len(slots) == 3
        # 첫 슬롯은 9:00~10:00
        assert slots[0][0].hour == 9 and slots[0][0].minute == 0
        assert slots[0][1].hour == 10

    def test_skips_busy_interval(self):
        """바쁜 시간대는 슬롯 후보에서 제외"""
        tmin = datetime(2026, 4, 20, 0, 0, tzinfo=KST)
        tmax = datetime(2026, 4, 20, 23, 59, tzinfo=KST)
        busy = [(datetime(2026, 4, 20, 10, 0, tzinfo=KST),
                 datetime(2026, 4, 20, 11, 0, tzinfo=KST))]
        slots = _find_free_slots(tmin, tmax, busy, 60, list(range(9, 18)),
                                 max_results=5)
        # 9~10 가능, 10~11 불가(busy), 그 뒤 11:00 이후 슬롯
        assert (datetime(2026, 4, 20, 9, 0, tzinfo=KST),
                datetime(2026, 4, 20, 10, 0, tzinfo=KST)) in slots
        # 10:00~11:00 슬롯은 없어야 함
        assert not any(s.hour == 10 and s.minute == 0 for s, _ in slots)

    def test_skips_weekend(self):
        """토/일은 건너뛴다"""
        tmin = datetime(2026, 4, 18, 0, 0, tzinfo=KST)  # 토요일
        tmax = datetime(2026, 4, 20, 23, 59, tzinfo=KST)  # 월요일까지
        slots = _find_free_slots(tmin, tmax, [], 60, list(range(9, 18)),
                                 max_results=5)
        assert all(s.weekday() < 5 for s, _ in slots)
        assert all(s.weekday() < 5 for _, e in slots for s in [e])

    def test_merges_overlapping_busy(self):
        """겹치는 busy 구간은 병합되어 연속 바쁨으로 계산"""
        tmin = datetime(2026, 4, 20, 0, 0, tzinfo=KST)
        tmax = datetime(2026, 4, 20, 23, 59, tzinfo=KST)
        busy = [
            (datetime(2026, 4, 20, 10, 0, tzinfo=KST),
             datetime(2026, 4, 20, 11, 0, tzinfo=KST)),
            (datetime(2026, 4, 20, 10, 30, tzinfo=KST),
             datetime(2026, 4, 20, 12, 0, tzinfo=KST)),
        ]
        slots = _find_free_slots(tmin, tmax, busy, 60, list(range(9, 18)),
                                 max_results=5)
        # 10:00~12:00 전체가 busy여야 함
        assert not any(
            s < datetime(2026, 4, 20, 12, 0, tzinfo=KST) and
            e > datetime(2026, 4, 20, 10, 0, tzinfo=KST)
            for s, e in slots
        )

    def test_duration_respected(self):
        """요구 duration 만큼 비어 있지 않으면 후보에서 제외"""
        tmin = datetime(2026, 4, 20, 0, 0, tzinfo=KST)
        tmax = datetime(2026, 4, 20, 23, 59, tzinfo=KST)
        # 9:30~10:00만 busy → 9:00~10:00 (60분)은 충돌, 10:00~11:00은 OK
        busy = [(datetime(2026, 4, 20, 9, 30, tzinfo=KST),
                 datetime(2026, 4, 20, 10, 0, tzinfo=KST))]
        slots = _find_free_slots(tmin, tmax, busy, 60, list(range(9, 18)),
                                 max_results=3)
        # 9:00~10:00은 (9:30-10:00 busy와 충돌) 후보 아님
        assert (datetime(2026, 4, 20, 9, 0, tzinfo=KST),
                datetime(2026, 4, 20, 10, 0, tzinfo=KST)) not in slots
        # 10:00~11:00은 OK
        assert (datetime(2026, 4, 20, 10, 0, tzinfo=KST),
                datetime(2026, 4, 20, 11, 0, tzinfo=KST)) in slots


# ═══════════════════════════════════════════════════════════════════
# F2: cancel_meeting
# ═══════════════════════════════════════════════════════════════════

class TestCancelMeeting:
    def test_single_candidate_posts_confirm_block(self):
        slack = _slack()
        ev = {"id": "evt_kakao", "summary": "카카오 미팅",
              "start": {"dateTime": "2026-04-20T15:00:00+09:00"}}
        with patch("agents.before._generate",
                   return_value='{"title_hint":"카카오","date":null}'), \
             patch("agents.before.cal.get_upcoming_meetings",
                   return_value=[ev]), \
             patch("agents.before.user_store.get_credentials",
                   return_value=MagicMock()):
            cancel_meeting_from_text(slack, _TEST_USER, "카카오 미팅 취소해줘")

        calls = slack.chat_postMessage.call_args_list
        all_ids = set()
        for c in calls:
            all_ids |= _blocks_action_ids(c)
        assert "meeting_cancel_confirm" in all_ids
        assert "meeting_cancel_abort" in all_ids

    def test_multiple_candidates_posts_selection(self):
        slack = _slack()
        events = [
            {"id": "e1", "summary": "카카오 미팅 A",
             "start": {"dateTime": "2026-04-20T10:00:00+09:00"}},
            {"id": "e2", "summary": "카카오 미팅 B",
             "start": {"dateTime": "2026-04-21T14:00:00+09:00"}},
        ]
        with patch("agents.before._generate",
                   return_value='{"title_hint":"카카오","date":null}'), \
             patch("agents.before.cal.get_upcoming_meetings",
                   return_value=events), \
             patch("agents.before.user_store.get_credentials",
                   return_value=MagicMock()):
            cancel_meeting_from_text(slack, _TEST_USER, "카카오 미팅 취소")

        # 두 이벤트 모두 버튼으로 제시되어야 함 (인덱스 접미사 포함한 action_id)
        calls = slack.chat_postMessage.call_args_list
        all_values = []
        for c in calls:
            for b in c[1].get("blocks", []) or []:
                for el in b.get("elements", []) or []:
                    if el.get("action_id", "").startswith("meeting_cancel_confirm"):
                        all_values.append(el.get("value"))
        assert "e1" in all_values and "e2" in all_values

    def test_no_candidates_sends_not_found(self):
        slack = _slack()
        with patch("agents.before._generate",
                   return_value='{"title_hint":"없음","date":null}'), \
             patch("agents.before.cal.get_upcoming_meetings",
                   return_value=[]), \
             patch("agents.before.user_store.get_credentials",
                   return_value=MagicMock()):
            cancel_meeting_from_text(slack, _TEST_USER, "없음 미팅 취소")
        text = slack.chat_postMessage.call_args[1].get("text", "")
        assert "찾지 못" in text

    def test_confirm_calls_delete_event(self):
        slack = _slack()
        # location에 드림플러스 없음 → 바로 삭제
        ev = {"summary": "KISA 미팅", "location": "",
              "start": {"dateTime": "2026-04-20T15:00:00+09:00"},
              "end": {"dateTime": "2026-04-20T16:00:00+09:00"}}
        with patch("agents.before.user_store.get_credentials",
                   return_value=MagicMock()), \
             patch("agents.before.cal.get_event", return_value=ev), \
             patch("agents.before.cal.delete_event") as mock_delete:
            handle_meeting_cancel_confirm(slack, _TEST_USER, "evt_target")

        mock_delete.assert_called_once()
        args, kwargs = mock_delete.call_args
        event_id = args[1] if len(args) > 1 else kwargs.get("event_id")
        assert event_id == "evt_target"
        text = slack.chat_postMessage.call_args[1].get("text", "")
        assert "KISA 미팅" in text

    def test_confirm_with_reservation_shows_with_room_prompt(self):
        """location에 드림플러스 + 예약 매칭되면 '함께 취소?' 프롬프트 발송"""
        from agents.before import _pending_meeting_cancel_with_room
        _pending_meeting_cancel_with_room.clear()

        slack = _slack()
        ev = {"summary": "팀 회의", "location": "드림플러스 강남 Meeting Room 8A",
              "start": {"dateTime": "2026-04-20T15:00:00+09:00"},
              "end": {"dateTime": "2026-04-20T16:00:00+09:00"}}
        with patch("agents.before.user_store.get_credentials",
                   return_value=MagicMock()), \
             patch("agents.before.cal.get_event", return_value=ev), \
             patch("agents.before.cal.delete_event") as mock_delete, \
             patch("agents.dreamplus.find_reservation_for_meeting",
                   return_value=777):
            handle_meeting_cancel_confirm(slack, _TEST_USER, "evt_with_room",
                                          body={"container": {"channel_id": "C", "message_ts": "1"}})

        # 이 시점엔 삭제 X, 프롬프트만 발송
        mock_delete.assert_not_called()
        assert "evt_with_room" in _pending_meeting_cancel_with_room
        assert _pending_meeting_cancel_with_room["evt_with_room"]["reservation_id"] == 777
        all_ids = set()
        for c in slack.chat_postMessage.call_args_list:
            all_ids |= _blocks_action_ids(c)
        assert "meeting_cancel_with_room" in all_ids
        assert "meeting_cancel_event_only" in all_ids
        assert "meeting_cancel_abort_both" in all_ids

    def test_cancel_with_room_invokes_both_deletions(self):
        """'함께 취소' 버튼 → delete_event + cancel_reservation_by_id 모두 호출"""
        from agents.before import (_pending_meeting_cancel_with_room,
                                    handle_meeting_cancel_with_room)
        _pending_meeting_cancel_with_room["evt_both"] = {
            "user_id": _TEST_USER, "event_id": "evt_both",
            "reservation_id": 777, "summary": "팀 회의", "location": "드림플러스 강남 8A",
        }
        slack = _slack()
        with patch("agents.before.user_store.get_credentials",
                   return_value=MagicMock()), \
             patch("agents.before.cal.delete_event") as mock_del, \
             patch("agents.dreamplus.cancel_reservation_by_id") as mock_cancel:
            handle_meeting_cancel_with_room(slack, _TEST_USER, "evt_both",
                                            body={"container": {"channel_id": "C", "message_ts": "1"}})
        mock_del.assert_called_once()
        mock_cancel.assert_called_once()
        assert mock_cancel.call_args[0][1] == 777
        assert "evt_both" not in _pending_meeting_cancel_with_room

    def test_cancel_event_only_skips_reservation(self):
        """'일정만 취소' → delete_event만 호출, 예약 취소는 안 함"""
        from agents.before import (_pending_meeting_cancel_with_room,
                                    handle_meeting_cancel_event_only)
        _pending_meeting_cancel_with_room["evt_only"] = {
            "user_id": _TEST_USER, "event_id": "evt_only",
            "reservation_id": 888, "summary": "팀 회의", "location": "드림플러스 강남 8A",
        }
        slack = _slack()
        with patch("agents.before.user_store.get_credentials",
                   return_value=MagicMock()), \
             patch("agents.before.cal.delete_event") as mock_del, \
             patch("agents.dreamplus.cancel_reservation_by_id") as mock_cancel:
            handle_meeting_cancel_event_only(slack, _TEST_USER, "evt_only",
                                              body={"container": {"channel_id": "C", "message_ts": "1"}})
        mock_del.assert_called_once()
        mock_cancel.assert_not_called()

    def test_abort_does_not_call_delete(self):
        slack = _slack()
        with patch("agents.before.cal.delete_event") as mock_delete:
            handle_meeting_cancel_abort(
                slack, _TEST_USER, "evt_target",
                body={"container": {"channel_id": "C1", "message_ts": "1.1"}},
            )
        mock_delete.assert_not_called()


# ═══════════════════════════════════════════════════════════════════
# I2(a): 미팅 생성 확인 프리뷰
# ═══════════════════════════════════════════════════════════════════

class TestCreateConfirm:
    def setup_method(self):
        _pending_create_confirm.clear()

    def test_preview_stores_draft_and_posts_buttons(self):
        slack = _slack()
        info = {"title": "테스트 미팅", "date": "2026-04-20",
                "time": "14:00", "duration_minutes": 60}
        _post_create_preview(
            slack, user_id=_TEST_USER, info=info, company=None,
            attendee_emails=["edward@x.com", "kim@x.com"],
            pending_selections=[], missing_names=[],
            channel=None, thread_ts=None, user_msg_ts=None,
        )
        # draft 1건 저장
        assert len(_pending_create_confirm) == 1
        draft_id = next(iter(_pending_create_confirm))
        assert _pending_create_confirm[draft_id]["user_id"] == _TEST_USER
        # create_confirm / create_abort 버튼 있어야 함
        ids = _blocks_action_ids(slack.chat_postMessage.call_args)
        assert "create_confirm" in ids and "create_abort" in ids

    def test_confirm_invokes_create_calendar_event(self):
        slack = _slack()
        _pending_create_confirm["d1"] = {
            "user_id": _TEST_USER,
            "info": {"title": "T", "date": "2026-04-20", "time": "14:00"},
            "company": None,
            "attendee_emails": ["a@x.com"],
            "pending_selections": [],
            "missing_names": [],
            "channel": None, "thread_ts": None, "user_msg_ts": None,
        }
        with patch("agents.before._create_calendar_event",
                   return_value="evt_new") as mock_create:
            handle_create_confirm(slack, _TEST_USER, "d1",
                                  body={"container": {"channel_id": "C", "message_ts": "1"}})
        mock_create.assert_called_once()
        assert "d1" not in _pending_create_confirm

    def test_abort_removes_draft_without_creating(self):
        _pending_create_confirm["d2"] = {"user_id": _TEST_USER}
        slack = _slack()
        with patch("agents.before._create_calendar_event") as mock_create:
            handle_create_abort(slack, _TEST_USER, "d2",
                                body={"container": {"channel_id": "C", "message_ts": "1"}})
        mock_create.assert_not_called()
        assert "d2" not in _pending_create_confirm


# ═══════════════════════════════════════════════════════════════════
# I2(b): 회의실 예약 확인
# ═══════════════════════════════════════════════════════════════════

class TestRoomOffer:
    def setup_method(self):
        _pending_room_offer.clear()

    def test_offer_stores_and_posts_buttons(self):
        slack = _slack()
        start = datetime(2026, 4, 20, 14, 0, tzinfo=KST)
        end = start + timedelta(hours=1)
        offer_room_booking(slack, user_id=_TEST_USER,
                           start_dt=start, end_dt=end,
                           title="팀 회의", attendee_count=3)
        assert len(_pending_room_offer) == 1
        ids = _blocks_action_ids(slack.chat_postMessage.call_args)
        assert "room_offer_show" in ids and "room_offer_skip" in ids

    def test_show_spawns_auto_book_room_thread(self):
        slack = _slack()
        start = datetime(2026, 4, 20, 14, 0, tzinfo=KST)
        end = start + timedelta(hours=1)
        offer_id = "off1"
        _pending_room_offer[offer_id] = {
            "user_id": _TEST_USER,
            "start_dt_iso": start.isoformat(),
            "end_dt_iso": end.isoformat(),
            "title": "팀 회의",
            "attendee_count": 2,
            "channel": None, "thread_ts": None, "event_id": "evt",
        }
        with patch("agents.before.threading.Thread") as mock_thread:
            handle_room_offer_show(slack, _TEST_USER, offer_id,
                                   body={"container": {"channel_id": "C", "message_ts": "1"}})
        mock_thread.assert_called_once()
        assert offer_id not in _pending_room_offer

    def test_skip_removes_pending(self):
        _pending_room_offer["off2"] = {"user_id": _TEST_USER}
        slack = _slack()
        handle_room_offer_skip(slack, _TEST_USER, "off2",
                               body={"container": {"channel_id": "C", "message_ts": "1"}})
        assert "off2" not in _pending_room_offer


# ═══════════════════════════════════════════════════════════════════
# F1: suggest_meeting_slots — 통합 플로우
# ═══════════════════════════════════════════════════════════════════

class TestSuggestSlots:
    def test_no_participants_sends_guide(self):
        slack = _slack()
        with patch("agents.before._generate", return_value='{"participants":[]}'), \
             patch("agents.before.user_store.get_credentials",
                   return_value=MagicMock()):
            suggest_meeting_slots(slack, _TEST_USER, "빈 시간 찾아줘")
        text_hits = [c[1].get("text", "")
                     for c in slack.chat_postMessage.call_args_list]
        assert any("누구" in t or "알려" in t for t in text_hits)

    def test_full_flow_posts_slot_buttons(self):
        """참석자 이메일 해석 + freebusy → 슬롯 버튼 발송"""
        slack = _slack()
        parse_json = json.dumps({
            "participants": ["김민환"],
            "duration_minutes": 60,
            "range_start": "2026-04-20",
            "range_end": "2026-04-20",
        })
        # 2026-04-20 (월) 14:00~15:00 busy
        busy = [(datetime(2026, 4, 20, 14, 0, tzinfo=KST),
                 datetime(2026, 4, 20, 15, 0, tzinfo=KST))]
        with patch("agents.before._generate", return_value=parse_json), \
             patch("agents.before._find_email_candidates",
                   return_value=["kim@x.com"]), \
             patch("agents.before._lookup_slack_email",
                   return_value="edward@x.com"), \
             patch("agents.before.cal.freebusy_query",
                   return_value={"kim@x.com": busy, "edward@x.com": []}), \
             patch("agents.before.user_store.get_credentials",
                   return_value=MagicMock()):
            suggest_meeting_slots(slack, _TEST_USER,
                                  "김민환이랑 내일 1시간 잡을 시간 찾아줘")

        # slot_create_meeting_{i} 액션 버튼 최소 1개 (인덱스 접미사)
        all_ids = set()
        for c in slack.chat_postMessage.call_args_list:
            all_ids |= _blocks_action_ids(c)
        assert any(aid.startswith("slot_create_meeting") for aid in all_ids)

    def test_slot_click_creates_event(self):
        slack = _slack()
        start = datetime(2026, 4, 20, 9, 0, tzinfo=KST)
        end = start + timedelta(minutes=60)
        slot_value = f"{start.isoformat()}|{end.isoformat()}|edward@x.com,kim@x.com"
        with patch("agents.before.user_store.get_credentials",
                   return_value=MagicMock()), \
             patch("agents.before.cal.create_event",
                   return_value={"id": "new", "hangoutLink": "https://meet.example/abc",
                                 "start": {"dateTime": start.isoformat()}}) as mock_create:
            handle_slot_create_meeting(slack, _TEST_USER, slot_value,
                                       body={"container": {"channel_id": "C", "message_ts": "1"}})
        mock_create.assert_called_once()
        # Meet 링크 포함
        text = slack.chat_postMessage.call_args[1].get("text", "")
        assert "Meet" in text or "meet.example" in text


# ═══════════════════════════════════════════════════════════════════
# I3: _post_combined_minutes 링크 포맷
# ═══════════════════════════════════════════════════════════════════

class TestMinutesLinkFormat:
    def test_mrkdwn_link_format_and_folder_line(self):
        """내·외부 파일 링크는 `<url|텍스트>` 포맷, 폴더 링크 포함"""
        slack = _slack()
        during._post_combined_minutes(
            slack, user_id=_TEST_USER, title="T",
            source_label="트랜스크립트",
            internal_body="...", external_body="...",
            internal_file_id="FID_I", external_file_id="FID_E",
            post_channel=None, post_thread_ts=None,
            minutes_folder_id="FOLDER_XYZ",
        )
        text = slack.chat_postMessage.call_args[1].get("text", "")
        assert "<https://drive.google.com/file/d/FID_I/view|Drive에서 열기>" in text
        assert "<https://drive.google.com/file/d/FID_E/view|Drive에서 열기>" in text
        assert "<https://drive.google.com/drive/folders/FOLDER_XYZ|Minutes 폴더>" in text

    def test_no_folder_id_omits_folder_line(self):
        slack = _slack()
        during._post_combined_minutes(
            slack, user_id=_TEST_USER, title="T",
            source_label="노트",
            internal_body="...", external_body="...",
            internal_file_id="FID_I", external_file_id="FID_E",
            minutes_folder_id=None,
        )
        text = slack.chat_postMessage.call_args[1].get("text", "")
        assert "Minutes 폴더" not in text
