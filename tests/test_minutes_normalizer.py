"""agents/minutes_normalizer.py 단위 테스트"""
import os

os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
os.environ.setdefault("INTERNAL_DOMAINS", "parametacorp.com,iconloop.com")

from unittest.mock import patch, MagicMock

with patch("anthropic.Anthropic"), patch("tools.drive._service"):
    from agents import minutes_normalizer as mn


# ── diagnose_minutes ───────────────────────────────────────────


class TestDiagnoseMinutes:
    def test_empty_content_is_broken(self):
        diag = mn.diagnose_minutes("", "internal")
        assert diag["severity"] == "broken"
        assert diag["needs_normalization"] is True
        assert diag["has_frontmatter"] is False

    def test_well_formed_internal_minutes_is_ok(self):
        body = (
            "---\n"
            "title: 2026-04-28_test\n"
            "date: 2026-04-28\n"
            "type: meeting\n"
            "meeting_type: internal\n"
            "tags: [meeting]\n"
            "---\n\n"
            "# 테스트\n\n"
            "## 회의 개요\n- 일시: 2026-04-28\n\n"
            "## 결론\n해당 없음\n\n"
            "## 액션아이템\n해당 없음\n\n"
            "## 주요 논의 내용\n없음\n\n"
            "## 출처 로그\n<!-- auto:start -->\n<!-- auto:end -->\n\n"
            "## 관련 문서\n<!-- auto:start -->\n<!-- auto:end -->\n"
        )
        diag = mn.diagnose_minutes(body, "internal")
        assert diag["has_frontmatter"] is True
        assert diag["missing_required_sections"] == []
        assert diag["missing_auto_sections"] == []
        assert diag["severity"] == "ok"
        assert diag["needs_normalization"] is False

    def test_missing_frontmatter_marks_warning_or_broken(self):
        body = (
            "# 회의\n\n"
            "## 회의 개요\n- 일시: 2026-04-28\n\n"
            "## 결론\n없음\n\n"
            "## 액션아이템\n없음\n\n"
            "## 주요 논의 내용\n없음\n"
        )
        diag = mn.diagnose_minutes(body, "internal")
        assert diag["has_frontmatter"] is False
        assert diag["severity"] in ("warning", "broken")
        assert diag["needs_normalization"] is True
        # 자동 섹션 누락도 잡혀야 한다
        assert "출처 로그" in diag["missing_auto_sections"]

    def test_missing_required_sections(self):
        body = (
            "---\n"
            "title: x\n"
            "---\n"
            "# 회의\n\n"
            "## 임의 섹션\n사용자가 적은 본문\n"
        )
        diag = mn.diagnose_minutes(body, "internal")
        assert "회의 개요" in diag["missing_required_sections"]
        assert "결론" in diag["missing_required_sections"]
        assert diag["needs_normalization"] is True
        assert diag["severity"] == "broken"

    def test_external_required_set(self):
        body = (
            "---\n"
            "title: x\n"
            "---\n\n"
            "# 회의\n\n"
            "## 회의 개요\n- 일시: 2026-04-28\n"
        )
        diag = mn.diagnose_minutes(body, "vendor")
        # 외부용 회의는 external 필수 섹션 셋 사용
        assert "회의 목적" in diag["missing_required_sections"]
        assert "주요 논의" in diag["missing_required_sections"]

    def test_broken_wiki_link_detected(self):
        body = (
            "---\n"
            "title: x\n"
            "---\n\n"
            "# 회의\n\n"
            "## 회의 개요\n[[]] [[ ]] 정상은 [[홍길동]]\n\n"
            "## 결론\n없음\n\n"
            "## 액션아이템\n없음\n\n"
            "## 주요 논의 내용\n없음\n"
        )
        diag = mn.diagnose_minutes(body, "internal")
        assert len(diag["broken_links"]) >= 1


# ── parse_filename_metadata ───────────────────────────────────


class TestParseFilenameMetadata:
    def test_internal_pattern(self):
        meta = mn.parse_filename_metadata("2026-04-28_카카오 사전논의_내부용.md")
        assert meta == {
            "date": "2026-04-28",
            "title": "카카오 사전논의",
            "meeting_type": "internal",
        }

    def test_external_pattern(self):
        meta = mn.parse_filename_metadata("2026-04-28_카카오 사전논의_외부용.md")
        assert meta["meeting_type"] == "vendor"
        assert meta["title"] == "카카오 사전논의"

    def test_missing_pattern_falls_back(self):
        meta = mn.parse_filename_metadata("그냥 메모.md")
        assert meta["date"] == ""
        assert meta["title"] == "그냥 메모"
        assert meta["meeting_type"] == "internal"


# ── normalize_light ───────────────────────────────────────────


class TestNormalizeLight:
    def test_adds_frontmatter_without_modifying_body(self):
        body = (
            "# 회의\n\n"
            "## 회의 개요\n- 일시: 2026-04-28\n\n"
            "## 결론\n없음\n\n"
            "## 액션아이템\n없음\n\n"
            "## 주요 논의 내용\n사용자가 직접 적은 본문 ABCD\n"
        )
        out = mn.normalize_light(
            body,
            {
                "title": "테스트",
                "date": "2026-04-28",
                "meeting_type": "internal",
                "attendees_raw": [],
            },
            known_entities=[],
        )
        assert out.startswith("---\n")
        # 본문 콘텐츠는 그대로 유지
        assert "사용자가 직접 적은 본문 ABCD" in out
        # 자동 섹션 추가
        assert "## 출처 로그" in out
        assert "## 관련 문서" in out
        assert "<!-- auto:start -->" in out

    def test_preserves_user_added_unknown_section(self):
        body = (
            "---\n"
            "title: x\n"
            "---\n\n"
            "# 회의\n\n"
            "## 회의 개요\n- 일시: 2026-04-28\n\n"
            "## 후속 메모\n사용자가 추가한 섹션이며 절대 삭제되면 안 됨\n"
        )
        out = mn.normalize_light(
            body,
            {"title": "x", "date": "2026-04-28", "meeting_type": "internal"},
            known_entities=[],
        )
        # 사용자 정의 섹션 보존
        assert "## 후속 메모" in out
        assert "사용자가 추가한 섹션이며 절대 삭제되면 안 됨" in out

    def test_known_entity_wraps(self):
        body = (
            "---\n"
            "title: x\n"
            "---\n\n"
            "# 회의\n\n"
            "## 회의 개요\n홍길동 님이 참석했습니다.\n"
        )
        out = mn.normalize_light(
            body,
            {"title": "x", "date": "2026-04-28", "meeting_type": "internal"},
            known_entities=["홍길동"],
        )
        assert "[[홍길동]]" in out

    def test_idempotent_does_not_double_wrap(self):
        body = (
            "---\n"
            "title: x\n"
            "---\n\n"
            "# 회의\n\n"
            "## 회의 개요\n[[홍길동]] 참석\n"
        )
        out = mn.normalize_light(
            body,
            {"title": "x", "date": "2026-04-28", "meeting_type": "internal"},
            known_entities=["홍길동"],
        )
        # 이미 [[]] 로 감싸진 곳은 다시 감싸지 않음
        assert "[[[[홍길동]]]]" not in out


# ── list → diagnosis severity counts ──────────────────────────


class TestListMinutesForNormalize:
    def test_severity_counts_in_listing(self):
        """list_minutes_for_normalize 가 ⚠️ 카운트를 정확히 발송하는지 확인."""
        ok_body = (
            "---\ntitle: ok\n---\n\n# x\n\n"
            "## 회의 개요\n- 1\n\n## 결론\n-\n\n## 액션아이템\n-\n\n"
            "## 주요 논의 내용\n-\n\n## 출처 로그\n<!-- auto:start -->\n<!-- auto:end -->\n\n"
            "## 관련 문서\n<!-- auto:start -->\n<!-- auto:end -->\n"
        )
        broken_body = "# 깨진 회의록\n본문만 있고 frontmatter 없음\n"

        files = [
            {"id": "f1", "name": "2026-04-28_OK_내부용.md", "modifiedTime": "2026-04-28T10:00:00Z"},
            {"id": "f2", "name": "2026-04-27_Broken_내부용.md", "modifiedTime": "2026-04-27T10:00:00Z"},
        ]

        slack = MagicMock()
        slack.chat_postMessage.return_value = {"ts": "1.2"}

        with patch("agents.minutes_normalizer.user_store") as us, \
             patch("agents.minutes_normalizer.drive") as dv:
            us.get_credentials.return_value = MagicMock()
            us.get_user.return_value = {
                "minutes_folder_id": "minutes_folder",
                "contacts_folder_id": "contacts_folder",
            }
            dv.list_minutes.return_value = files
            dv._read_file.side_effect = lambda creds, fid: ok_body if fid == "f1" else broken_body

            mn.list_minutes_for_normalize(slack, "U1")

        # 메시지가 발송되어야 한다
        slack.chat_postMessage.assert_called_once()
        call_kwargs = slack.chat_postMessage.call_args.kwargs
        text = call_kwargs.get("text", "")
        blocks = call_kwargs.get("blocks") or []
        # 2건 진단, 1건 보정 필요 — 헤더 텍스트에 반영
        header_text = blocks[0]["text"]["text"]
        assert "진단 2건" in header_text
        assert "보정 필요 1건" in header_text
