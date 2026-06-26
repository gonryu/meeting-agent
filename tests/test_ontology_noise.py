"""온톨로지 클러스터 노이즈 필터 — instance-of(유형) 타입형제 제외"""
import os
os.environ.setdefault("ANTHROPIC_API_KEY", "test")
os.environ.setdefault("ENCRYPTION_KEY", "dGVzdC1rZXktMzItYnl0ZXMtZm9yLWZlcm5ldC0h")

from tools.ontology import _normalize_cluster


class TestInstanceOfFilter:
    def _cluster(self):
        return {"entities": [
            {"slug": "cizion", "title": "시지온"},                              # seed 자신
            {"slug": "kim", "title": "김미균", "via": "part-of", "hop": 1},
            {"slug": "iconloop", "title": "아이콘루프", "via": "related-to", "hop": 1},
            {"slug": "perme", "title": "퍼미(Perme)", "via": "related-to", "hop": 1},
            {"slug": "00bank", "title": "00은행", "via": "instance-of", "hop": 2},
            {"slug": "1inch", "title": "1inch", "via": "instance-of", "hop": 2},
            {"slug": "1inchdao", "title": "1inch DAO", "via": "instance_of", "hop": 2},
        ]}

    def test_drops_instance_of_keeps_meaningful(self):
        out = _normalize_cluster(self._cluster(), "cizion")
        titles = [r["title"] for r in out["relations"]]
        # 의미 있는 관계는 유지
        assert "김미균" in titles and "아이콘루프" in titles and "퍼미(Perme)" in titles
        # 유형(instance-of/instance_of) 타입형제는 전부 제외
        assert "00은행" not in titles
        assert "1inch" not in titles
        assert "1inch DAO" not in titles

    def test_no_instance_of_unchanged(self):
        cluster = {"entities": [
            {"slug": "seed", "title": "X"},
            {"slug": "a", "title": "에이", "via": "part-of", "hop": 1},
        ]}
        out = _normalize_cluster(cluster, "seed")
        assert [r["title"] for r in out["relations"]] == ["에이"]
