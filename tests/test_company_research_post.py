"""On-demand company research Slack posting behavior."""
import os
from unittest.mock import MagicMock, patch

os.environ.setdefault("GOOGLE_API_KEY", "test-key")
os.environ.setdefault("ANTHROPIC_API_KEY", "test-key")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1lbmNyeXB0aW9uLWtleS0xMjM0NTY3ODk=")
os.environ.setdefault("SLACK_BOT_TOKEN", "xoxb-test")
os.environ.setdefault("SLACK_APP_TOKEN", "xapp-test")
os.environ.setdefault("TRELLO_API_KEY", "test-trello-key")
os.environ.setdefault("TRELLO_BOARD_ID", "test-board-id")


def test_on_demand_company_research_disables_slack_unfurls(monkeypatch):
    with patch("anthropic.Anthropic"), \
         patch("slack_bolt.App"), \
         patch("slack_bolt.adapter.socket_mode.SocketModeHandler"):
        import main

    client = MagicMock()
    trello = MagicMock()
    trello.get_card_context.return_value = None
    trello.get_lookup_diagnostic.return_value = {"message": "카드 미발견"}

    monkeypatch.setattr(main.before_agent, "trello", trello)
    monkeypatch.setattr(
        main.before_agent,
        "_extract_company_content_sections",
        lambda content: ([], [], [], [], []),
    )
    monkeypatch.setattr(main.before_agent, "_drive_parse_frontmatter", lambda content: ({}, content))
    monkeypatch.setattr(main.before_agent, "deep_company_ontology", lambda *a, **k: None)
    monkeypatch.setattr(main.before_agent, "_company_ontology", lambda *a, **k: None)
    monkeypatch.setattr(main.before_agent, "_structured_news_items", lambda *a, **k: [])
    monkeypatch.setattr(
        main.before_agent,
        "build_company_research_block",
        lambda *a, **k: [{"type": "section", "text": {"type": "mrkdwn", "text": "x"}}],
    )

    main._post_company_research_result(
        client,
        user_id="U_TEST",
        company="두나무",
        content="# 두나무\n\n## 최근 동향\n",
    )

    kwargs = client.chat_postMessage.call_args.kwargs
    assert kwargs["unfurl_links"] is False
    assert kwargs["unfurl_media"] is False
