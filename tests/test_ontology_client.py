"""tools/ontology.py — 순수 헬퍼 + MCP 클라이언트 테스트"""
import base64, os
os.environ.setdefault("ENCRYPTION_KEY", base64.urlsafe_b64encode(b"0" * 32).decode())
import httpx, json
import pytest
import tools.ontology as ont

_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.sig123"


class TestPureHelpers:
    def test_extract_from_json_config(self):
        cfg = '{"mcpServers":{"lib-mesh":{"url":"https://ont/mcp","headers":{"Authorization":"Bearer %s"}}}}' % _JWT
        assert ont.extract_bearer_token(cfg) == _JWT

    def test_extract_from_raw_text(self):
        assert ont.extract_bearer_token("아무 텍스트 " + _JWT + " 끝") == _JWT

    def test_extract_none(self):
        assert ont.extract_bearer_token("토큰 없음") is None
        assert ont.extract_bearer_token("") is None

    def test_endpoint_adds_trailing_slash(self):
        assert ont._endpoint("https://ont.x/mcp") == "https://ont.x/mcp/"
        assert ont._endpoint("https://ont.x/mcp/") == "https://ont.x/mcp/"

    def test_best_slug_prefers_exact(self):
        find = {"matches": [
            {"slug": "entity/sub", "match_kind": "substring", "confidence": 0.9, "importance": 0.9},
            {"slug": "entity/komsa", "match_kind": "exact", "confidence": 0.95, "importance": 0.9},
        ]}
        assert ont._best_slug(find) == "entity/komsa"

    def test_best_slug_empty(self):
        assert ont._best_slug({"matches": []}) is None
        assert ont._best_slug({}) is None

    def test_normalize_cluster(self):
        cluster = {"seed": "entity/komsa", "entities": [
            {"slug": "entity/komsa", "hop": 0, "title": "KOMSA", "via": None},
            {"slug": "entity/kca", "hop": 1, "title": "KCA", "via": "related-to"},
        ], "documents": [{"id": "doc/1", "title": "KOMSA 마케팅 계획"}]}
        out = ont._normalize_cluster(cluster, "entity/komsa")
        assert out["entity_count"] == 2
        assert {"relation": "related-to", "title": "KCA"} in out["relations"]
        assert out["documents"][0]["title"] == "KOMSA 마케팅 계획"


def _mock_transport():
    """initialize → 200(serverInfo), tools/call → content[].text의 data 봉투, 그 외 405."""
    def handler(request: httpx.Request):
        body = json.loads(request.content.decode())
        method = body.get("method")
        # 트레일링 슬래시로 와야 함
        assert str(request.url).endswith("/mcp/"), f"슬래시 직타 아님: {request.url}"
        assert request.headers.get("authorization", "").startswith("Bearer ")
        if method == "initialize":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1,
                "result": {"serverInfo": {"name": "lib-mesh"}, "protocolVersion": "2025-06-18", "capabilities": {}}})
        if method == "notifications/initialized":
            return httpx.Response(202, json={})
        if method == "tools/call":
            name = body["params"]["name"]
            data = {"matches": [{"slug": "entity/komsa", "match_kind": "exact", "confidence": 0.95}]} \
                if name == "entity_find" else {"seed": "entity/komsa", "entities": [], "documents": []}
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 2,
                "result": {"content": [{"type": "text", "text": json.dumps({"data": data})}]}})
        return httpx.Response(405)
    return httpx.MockTransport(handler)


def _client(token="eyJa.b.c"):
    oc = ont.OntologyClient(token, url="https://ont.x/mcp")
    oc._http = httpx.Client(transport=_mock_transport())
    return oc


class TestOntologyClient:
    def test_call_tool_parses_data_envelope(self):
        with _client() as oc:
            res = oc.call_tool("entity_find", {"name": "KOMSA"})
        assert res["matches"][0]["slug"] == "entity/komsa"

    def test_endpoint_has_trailing_slash(self):
        oc = ont.OntologyClient("eyJa.b.c", url="https://ont.x/mcp")
        assert oc.url == "https://ont.x/mcp/"

    def test_401_raises_auth_error(self):
        def h(request): return httpx.Response(401, json={"error": "unauthorized"})
        oc = ont.OntologyClient("eyJa.b.c", url="https://ont.x/mcp")
        oc._http = httpx.Client(transport=httpx.MockTransport(h))
        with pytest.raises(ont.OntologyAuthError):
            oc.call_tool("entity_find", {"name": "X"})

    def test_validate_ok(self):
        with _client() as oc:
            assert oc.validate() is True
