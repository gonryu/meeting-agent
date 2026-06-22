"""tools/ontology.py — _normalize_cluster 보강 + document_fetch"""
import os
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")
import httpx, json
import tools.ontology as ont


class TestNormalizeEnriched:
    def test_docs_carry_uri_space_ym_matched(self):
        cluster = {"seed": "entity/komsa", "entities": [
            {"slug": "entity/kca", "via": "related-to", "title": "KCA", "hop": 1}],
            "documents": [{
                "document_id": "raw://d1", "title": "KOMSA 제안서",
                "source_uri": "https://drive/x", "space_display": "Drive", "ym": "2026-05",
                "matched_via_entities": ["entity/komsa"]}]}
        out = ont._normalize_cluster(cluster, "entity/komsa")
        d = out["documents"][0]
        assert d["uri"] == "https://drive/x" and d["space"] == "Drive"
        assert d["ym"] == "2026-05" and "entity/komsa" in d["matched"]
        assert d["id"] == "raw://d1"


def _fetch_transport():
    def h(req):
        body = json.loads(req.content.decode())
        if body.get("method") == "initialize":
            return httpx.Response(200, json={"jsonrpc":"2.0","id":1,"result":{
                "serverInfo":{"name":"lib-mesh"},"protocolVersion":"2025-06-18","capabilities":{}}})
        if body.get("method") == "notifications/initialized":
            return httpx.Response(202, json={})
        # tools/call document_fetch
        data = {"document_id":"raw://d1","title":"KOMSA 제안서",
                "body_markdown":"총 266억 규모 DID/VC 검증체계","source_uri":"https://drive/x",
                "frontmatter":{"space_display":"Drive"}}
        return httpx.Response(200, json={"jsonrpc":"2.0","id":2,"result":{
            "content":[{"type":"text","text":json.dumps({"data":data})}]}})
    return httpx.MockTransport(h)


class TestDocumentFetch:
    def test_fetch_returns_summary(self, monkeypatch):
        monkeypatch.setattr(ont.user_store, "get_ontology_token", lambda uid: "eyJa.b.c")
        oc = ont.OntologyClient("eyJa.b.c", url="https://ont.x/mcp")
        oc._http = httpx.Client(transport=_fetch_transport())
        monkeypatch.setattr(ont, "OntologyClient", lambda *a, **k: oc)
        out = ont.document_fetch("U1", "raw://d1")
        assert "266억" in out["summary"]
        assert out["title"] == "KOMSA 제안서" and out["uri"] == "https://drive/x"
