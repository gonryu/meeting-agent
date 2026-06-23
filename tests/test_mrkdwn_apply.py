"""mrkdwn 정규화 적용 — 대표 경로에 ** 미잔존"""
import tools.slack_tools as st


def test_util_contract():
    # 적용 함수가 ** 를 남기지 않음(유틸 계약 — 호출부는 이 유틸로 정규화)
    samples = ["**카드**: 내용", "정상 *볼드*", "**A** 그리고 **B**"]
    for s in samples:
        out = st.to_slack_mrkdwn(s)
        assert "**" not in out
