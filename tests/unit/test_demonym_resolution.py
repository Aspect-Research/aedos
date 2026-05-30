"""Tests for the S1b demonym→country resolution (Wikidata P1549 reverse
lookup) — the general replacement for the hand-curated _DEMONYM_TO_COUNTRY
map. Resolution is sound: a country-typed slot only, unique match only,
fail-closed on ambiguity / non-word input / error."""

from __future__ import annotations

from aedos.layer4_sources.kb_wikidata import WikidataAdapter


class _FakeHttp:
    def __init__(self, bindings):
        self._bindings = bindings
        self.calls = []

    def get(self, url, params=None, ttl_seconds=None):
        self.calls.append({"url": url, "params": params})
        return {"results": {"bindings": self._bindings}}


def _binding(qid: str) -> dict:
    return {"item": {"value": f"http://www.wikidata.org/entity/{qid}"}}


def _adapter(bindings):
    fake = _FakeHttp(bindings)
    return WikidataAdapter(http_cache=fake), fake


class TestDemonymResolution:
    def test_unique_match_returns_qid(self):
        adapter, _ = _adapter([_binding("Q30")])
        assert adapter._resolve_demonym_to_country("American") == "Q30"

    def test_no_match_returns_none(self):
        adapter, _ = _adapter([])
        assert adapter._resolve_demonym_to_country("Klingon") is None

    def test_ambiguous_multiple_matches_returns_none(self):
        # Two distinct countries sharing a demonym — abstain rather than guess.
        adapter, _ = _adapter([_binding("Q30"), _binding("Q183")])
        assert adapter._resolve_demonym_to_country("Foo") is None

    def test_rejects_non_word_input_without_query(self):
        # SPARQL-injection / soundness gate: non-demonym input never reaches
        # the network and resolves to None.
        adapter, fake = _adapter([_binding("Q30")])
        assert adapter._resolve_demonym_to_country('"} UNION { ?x ?y ?z') is None
        assert fake.calls == []

    def test_empty_input_returns_none(self):
        adapter, fake = _adapter([_binding("Q30")])
        assert adapter._resolve_demonym_to_country("") is None
        assert fake.calls == []

    def test_no_http_returns_none(self):
        adapter = WikidataAdapter()  # _http is None
        assert adapter._resolve_demonym_to_country("German") is None
