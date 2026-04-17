"""Trello 연동 스모크 테스트 — DRY_RUN 모드에서 전체 흐름 검증

실행:
  .venv/bin/python tests/smoke_trello.py
"""
import os
import sys

# DRY_RUN 강제 활성화
os.environ["DRY_RUN_TRELLO"] = "true"
os.environ.setdefault("GOOGLE_API_KEY", "test-key")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
os.environ.setdefault("TRELLO_API_KEY", "")
os.environ.setdefault("TRELLO_BOARD_ID", "69731ce5")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")

from unittest.mock import patch, MagicMock

# 외부 서비스 차단
with patch("anthropic.Anthropic"), \
     patch("tools.calendar._service"), \
     patch("tools.drive._service"), \
     patch("tools.gmail._service"):

    from tools import trello
    import agents.after as after

log = logging.getLogger("smoke_trello")

PASS = "✅"
FAIL = "❌"
results = []
UID = "UTEST"


def check(name: str, condition: bool, detail: str = ""):
    status = PASS if condition else FAIL
    results.append((name, condition))
    print(f"  {status} {name}" + (f"  ({detail})" if detail else ""))


print("\n" + "=" * 60)
print("🔍 Trello 연동 스모크 테스트 (DRY_RUN 모드)")
print("=" * 60)

# ── 1. tools/trello.py 기본 함수 ──
print("\n📦 1. tools/trello.py — 기본 함수 (사용자별 인증)")

check("_is_dry_run() == True", trello._is_dry_run() is True)

result = trello.find_card_by_name(UID, "테스트업체")
check("find_card_by_name → None (DRY_RUN)", result is None)

result = trello.get_card_context(UID, "테스트업체")
check("get_card_context → 빈 dict (DRY_RUN)", result == {})

result = trello.create_card(UID, "테스트업체")
check("create_card → dummy dict (DRY_RUN)",
      result is not None and "dry-run" in result.get("card_id", ""),
      f"card_id={result.get('card_id')}")

items = [
    {"assignee": "김민환", "content": "기술 검토", "due_date": "2026-04-15"},
    {"assignee": "이수연", "content": "자료 공유", "due_date": None},
]
count = trello.add_checklist_items(UID, "테스트업체", items)
check("add_checklist_items → 항목 수 반환 (DRY_RUN)",
      count == 2, f"count={count}")

result = trello.add_comment(UID, "테스트업체", "테스트 코멘트")
check("add_comment → True (DRY_RUN)", result is True)

# ── 2. 체크리스트 항목 포맷 ──
print("\n📝 2. 체크리스트 항목 포맷")

fmt = trello._format_checklist_item(
    {"assignee": "홍길동", "content": "계약서 작성", "due_date": "2026-05-01"}
)
check("포맷 정상",
      fmt == "[홍길동] 계약서 작성 (기한: 2026-05-01)", f"결과: {fmt}")

# ── 3. After Agent — 업체명 추론 ──
print("\n🏢 3. After Agent — 업체명 추론")

with patch.object(after, "_generate", return_value="카카오"):
    name = after._infer_company_name("카카오 기술 협력 미팅")
    check("업체명 추론 성공", name == "카카오", f"결과: {name}")

# ── 4. After Agent — Trello 등록 제안 ──
print("\n💬 4. After Agent — Trello 등록 제안 Slack 메시지")

slack = MagicMock()
slack.chat_postMessage.return_value = {"ts": "123.456"}

with patch("agents.after.user_store") as mock_store, \
     patch.object(after, "_infer_company_name", return_value="카카오"), \
     patch("agents.after.trello") as mock_trello:
    mock_store.get_action_items.return_value = [
        {"id": 1, "assignee": "김민환", "content": "검토", "due_date": "2026-04-15", "status": "open"},
        {"id": 2, "assignee": "이수연", "content": "공유", "due_date": None, "status": "open"},
    ]
    mock_trello.find_card_by_name.return_value = {
        "card_id": "c1", "card_name": "카카오",
        "list_name": "Contact/Meeting", "url": "https://trello.com/c/c1",
    }
    after._propose_trello_registration(
        slack, user_id=UID, event_id="evt1", title="카카오 미팅"
    )
    called = slack.chat_postMessage.called
    check("Slack 메시지 발송됨", called)

    if called:
        kwargs = slack.chat_postMessage.call_args[1]
        blocks = kwargs.get("blocks", [])
        has_actions = any(b["type"] == "actions" for b in blocks)
        check("등록/건너뜀 버튼 포함", has_actions)

# ── 5. 캐시 관리 ──
print("\n🔄 5. 사용자 캐시 관리")

trello._client_cache["TEST_USER"] = MagicMock()
trello._board_cache["TEST_USER"] = MagicMock()
trello.clear_user_cache("TEST_USER")
check("캐시 초기화", "TEST_USER" not in trello._client_cache)

# ── 결과 요약 ──
print("\n" + "=" * 60)
passed = sum(1 for _, ok in results if ok)
total = len(results)
emoji = "🎉" if passed == total else "⚠️"
print(f"{emoji} 결과: {passed}/{total} 통과")
print("=" * 60 + "\n")

sys.exit(0 if passed == total else 1)
