"""담당자 표시명/검색키 분리 헬퍼"""
import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
import agents.before as before


def test_external_persons_display_and_search(monkeypatch):
    # displayName 없는 외부 참석자 → 표시명은 전체 이메일(localpart 아님), 검색키는 localpart
    monkeypatch.setattr(before, "_resolve_attendee_names",
                        lambda atts, uid, sc: [a.get("name") or a.get("email", "") for a in atts])
    attendees = [
        {"email": "min@icon.foundation"},                 # 외부, 이름 없음
        {"email": "kim@parametacorp.com", "name": "김파"},  # 사내 → 제외
        {"email": "park@kakao.com", "name": "박카카오"},     # 외부, 이름 있음
    ]
    persons = before._build_person_targets(attendees, "U1", None)
    names = [p["name"] for p in persons]
    searches = [p["search"] for p in persons]
    assert "min@icon.foundation" in names           # 표시명 = 전체 이메일
    assert "min" not in names                        # localpart 아님
    assert "박카카오" in names
    assert "김파" not in names                        # 사내 제외
    assert "min" in searches                          # 검색키 = localpart
