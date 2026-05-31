"""v0.16 WS1 — fixture tests for WikidataAdapter.fetch_label.

fetch_label(qid) returns an entity's English label, used by the discovery flow
and the WS5 correction surface (reverse-labeling a contradicting Q-id). It is
OPTIONAL on KBProtocol (called via getattr) and FAILS OPEN: a non-Q-id, a
missing label, a missing fixture, or any error returns None — never raises.

These tests exercise the deterministic fixture path
(`tests/fixtures/wikidata/label_<Q>.json`), so they run without RUN_LIVE_KB.
"""

from __future__ import annotations

from aedos.layer4_sources.kb_wikidata import WikidataAdapter


def _adapter():
    return WikidataAdapter()  # fixture mode (RUN_LIVE_KB unset)


class TestFetchLabelFixture:
    def test_label_from_entities_shape(self):
        # label_Q5.json uses the full wbgetentities shape.
        assert _adapter().fetch_label("Q5") == "human"

    def test_label_from_terse_shape(self):
        # label_Q4164871.json uses the compact {"label": ...} shape.
        assert _adapter().fetch_label("Q4164871") == "position"

    def test_missing_fixture_returns_none(self):
        # No fixture for this Q-id → fail open to None.
        assert _adapter().fetch_label("Q9999999") is None

    def test_non_qid_returns_none(self):
        # A non-Q-id input never reaches the fixture loader.
        assert _adapter().fetch_label("not-a-qid") is None
        assert _adapter().fetch_label("P39") is None
        assert _adapter().fetch_label("") is None

    def test_fetch_label_satisfies_protocol_getattr_contract(self):
        # Consumers call fetch_label via getattr; confirm the method is present
        # and callable on the adapter.
        adapter = _adapter()
        fn = getattr(adapter, "fetch_label", None)
        assert callable(fn)
        assert fn("Q5") == "human"
