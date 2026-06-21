"""tools/ontology.py — 순수 헬퍼 + MCP 클라이언트 테스트"""
import base64, os
os.environ.setdefault("ENCRYPTION_KEY", base64.urlsafe_b64encode(b"0" * 32).decode())
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
