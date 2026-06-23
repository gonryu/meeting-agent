"""tools/slack_tools.py — 출력 포맷 유틸"""
import tools.slack_tools as st


class TestRelationLabel:
    def test_known(self):
        assert st._relation_label("part-of") == "소속"
        assert st._relation_label("related-to") == "관련"
        assert st._relation_label("uses") == "활용"
        assert st._relation_label("instance-of") == "유형"
    def test_unknown_passthrough(self):
        assert st._relation_label("custom-rel") == "custom-rel"


class TestNoiseRelation:
    def test_numbered_section_is_noise(self):
        assert st._is_noise_relation("01. Cluster 구성하기") is True
        assert st._is_noise_relation("0102. PrivateKey 관리") is True
        assert st._is_noise_relation("  02 백엔드") is True
    def test_normal_not_noise(self):
        assert st._is_noise_relation("KISA 공공과제") is False
        assert st._is_noise_relation("InfraTeam") is False


class TestDocLabel:
    def test_strips_ext(self):
        assert st._doc_label("발표자료_KOMSA.pdf") == "발표자료_KOMSA"
        assert st._doc_label("점검.xlsx") == "점검"
        assert st._doc_label("2026-04-02 인프라기술실") == "2026-04-02 인프라기술실"


class TestToSlackMrkdwn:
    def test_double_to_single(self):
        assert st.to_slack_mrkdwn("**굵게**") == "*굵게*"
        assert st.to_slack_mrkdwn("a **b** c **d**") == "a *b* c *d*"
    def test_single_preserved(self):
        assert st.to_slack_mrkdwn("*이미*") == "*이미*"
    def test_none_safe(self):
        assert st.to_slack_mrkdwn(None) is None
        assert st.to_slack_mrkdwn("") == ""
