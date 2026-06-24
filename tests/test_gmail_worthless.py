"""B: 이메일 노이즈 필터 (#5) — _is_worthless_email"""
import os
os.environ.setdefault("GOOGLE_API_KEY", "test")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
from unittest.mock import patch

with patch("googleapiclient.discovery.build"):
    import tools.gmail as gmail


def _w(frm="", subject="", snippet="", extra=None):
    h = {"From": frm, "Subject": subject}
    if extra:
        h.update(extra)
    return gmail._is_worthless_email(h, snippet)


class TestWorthlessEmail:
    def test_google_alerts(self):
        assert _w(frm="Google 알리미 <googlealerts-noreply@google.com>", subject="Google 알리미 - 일일 알림") is True
        assert _w(frm="x@y.com", subject="Google Alert - blockchain") is True

    def test_noreply_senders(self):
        assert _w(frm="noreply@service.com") is True
        assert _w(frm="No-Reply <no-reply@bank.com>") is True
        assert _w(frm="donotreply@corp.com") is True

    def test_calendar_still_filtered(self):
        assert _w(frm="calendar-notification@google.com", subject="Invitation: 미팅") is True
        assert _w(subject="초대: 회의") is True

    def test_marketing_newsletter(self):
        assert _w(subject="[광고] 신제품 출시", snippet="구독 해지하려면") is True
        assert _w(frm="a@b.com", extra={"List-Unsubscribe": "<mailto:unsub@b.com>"}) is True

    def test_real_person_email_passes(self):
        assert _w(frm="박종도 <jd.park@komsa.or.kr>", subject="KOMSA 마케팅 협의 건", snippet="안녕하세요, 다음 미팅 일정 관련") is False
        assert _w(frm="이정훈 <lee@edaily.co.kr>", subject="인터뷰 일정 조율") is False
