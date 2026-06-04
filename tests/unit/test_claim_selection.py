"""v0.16.2 Phase D: central-claim selection — parsing + the fail-open-to-all
safety rails (a selector failure must NEVER skip verification)."""

from __future__ import annotations

from types import SimpleNamespace

from aedos.deployment.claim_selection import (
    _SYSTEM,
    _parse_selected_numbers,
    select_central_claims,
)


def _claims(n):
    return [
        SimpleNamespace(claim_id=f"c{i}", subject=f"S{i}", predicate="p",
                        object=f"O{i}", polarity=1)
        for i in range(n)
    ]


class FakeLLM:
    def __init__(self, reply="", raises=False):
        self.reply = reply
        self.raises = raises
        self.calls = 0

    def chat(self, system, messages, max_tokens=256, purpose=None):
        self.calls += 1
        if self.raises:
            raise RuntimeError("boom")
        return self.reply


class TestSystemPromptContract:
    """E4/v0.16.2: the selector MUST always pull in the claim establishing the
    answer's core identity/role/title (the wrong-pope fix relies on the role claim
    being selected and therefore verified). Pin the instruction so it can't be
    silently dropped — knowledge lives in the prompt, not a hardcoded allowlist."""

    def test_prompt_demands_identity_role_inclusion(self):
        low = _SYSTEM.lower()
        assert "always include" in low
        # names the identity dimensions it must never drop
        assert "identity" in low
        assert "role" in low and "title" in low


class TestParse:
    def test_plain_array(self):
        assert _parse_selected_numbers("[1,3,4]") == [1, 3, 4]

    def test_array_embedded_in_text(self):
        assert _parse_selected_numbers("The central ones are [2, 5].") == [2, 5]

    def test_string_ints_coerced(self):
        assert _parse_selected_numbers('["1", "2"]') == [1, 2]

    def test_no_array_returns_none(self):
        assert _parse_selected_numbers("claims one and two are central") is None

    def test_malformed_returns_none(self):
        assert _parse_selected_numbers("[1, 2,") is None

    def test_empty_array_returns_none(self):
        assert _parse_selected_numbers("[]") is None


class TestSelect:
    def test_narrows_to_central(self):
        claims = _claims(8)
        llm = FakeLLM("[1,3,5]")
        sel = select_central_claims(llm, "q", "draft", claims, min_claims=4)
        assert sel.applied is True
        assert sel.central_ids == {"c0", "c2", "c4"}  # 1->c0, 3->c2, 5->c4
        assert llm.calls == 1

    def test_out_of_range_numbers_ignored(self):
        claims = _claims(5)
        sel = select_central_claims(FakeLLM("[1, 99, 3]"), "q", "d", claims, min_claims=4)
        assert sel.applied is True and sel.central_ids == {"c0", "c2"}

    def test_few_claims_skips_selection_and_verifies_all(self):
        claims = _claims(3)
        llm = FakeLLM("[1]")
        sel = select_central_claims(llm, "q", "d", claims, min_claims=4)
        assert sel.applied is False
        assert sel.central_ids == {"c0", "c1", "c2"}
        assert llm.calls == 0  # no LLM call when below threshold

    def test_disabled_verifies_all_without_calling_llm(self):
        claims = _claims(8)
        llm = FakeLLM("[1]")
        sel = select_central_claims(llm, "q", "d", claims, enabled=False)
        assert sel.applied is False
        assert sel.central_ids == {c.claim_id for c in claims} and llm.calls == 0

    def test_selector_error_fails_open_to_all(self):
        claims = _claims(8)
        sel = select_central_claims(FakeLLM(raises=True), "q", "d", claims, min_claims=4)
        assert sel.applied is False
        assert sel.central_ids == {c.claim_id for c in claims}

    def test_unparseable_reply_fails_open_to_all(self):
        claims = _claims(8)
        sel = select_central_claims(FakeLLM("one and three"), "q", "d", claims, min_claims=4)
        assert sel.applied is False
        assert sel.central_ids == {c.claim_id for c in claims}

    def test_empty_selection_fails_open_to_all(self):
        claims = _claims(8)
        sel = select_central_claims(FakeLLM("[]"), "q", "d", claims, min_claims=4)
        assert sel.applied is False
        assert sel.central_ids == {c.claim_id for c in claims}


# --------------------------------------------------------------------------- #
# ChatWrapper.respond integration: verify ONLY central claims, record the rest
# --------------------------------------------------------------------------- #

class _SelLLM:
    """Returns a draft for the chat purpose and a selection array for the
    selection purpose."""

    def __init__(self, selection="[1,3]"):
        self.selection = selection
        self.purposes = []

    def chat(self, system, messages, max_tokens=4096, purpose="chat"):
        self.purposes.append(purpose)
        if purpose == "deployment:claim_selection":
            return self.selection
        return "A draft answer about the topic."


class _Extractor:
    def __init__(self, claims):
        self._claims = claims

    def extract(self, text, ctx):
        return list(self._claims)


class _Walker:
    def __init__(self):
        self.walked = []

    def walk(self, claim, ctx):
        self.walked.append(claim.claim_id)
        return SimpleNamespace(verdict="verified", trace=None, abstention_reason=None)


class _Aggregator:
    def __init__(self):
        self.aggregated = None

    def aggregate(self, claims, per_claim_results, text_input=None):
        self.aggregated = [c.claim_id for c in claims]
        return SimpleNamespace(claim_verdicts=[], per_claim_verdicts={},
                               aggregate_metadata={})


def _draft_claims(n):
    return [
        SimpleNamespace(claim_id=f"c{i}", subject=f"S{i}", predicate="p",
                        object=f"O{i}", polarity=1, abstention_reason=None)
        for i in range(n)
    ]


class TestRespondSelection:
    def _wrapper(self, llm, walker, claims):
        from aedos.deployment.chat_wrapper import ChatWrapper
        return ChatWrapper(extractor=_Extractor(claims), walker=walker,
                           aggregator=_Aggregator(), llm_client=llm, tier_u=None)

    def test_verifies_only_central_and_records_not_assessed(self):
        claims = _draft_claims(6)
        llm = _SelLLM("[1,3]")  # central = c0, c2
        walker = _Walker()
        resp = self._wrapper(llm, walker, claims).respond("the question?", select_min_claims=4)
        assert sorted(walker.walked) == ["c0", "c2"]            # only central walked
        assert {c["claim_id"] for c in resp.not_assessed_claims} == {"c1", "c3", "c4", "c5"}
        assert "deployment:claim_selection" in llm.purposes

    def test_fails_open_verifies_all_on_unparseable_selection(self):
        claims = _draft_claims(6)
        walker = _Walker()
        resp = self._wrapper(_SelLLM("I think one and three"), walker, claims).respond(
            "q", select_min_claims=4)
        assert sorted(walker.walked) == [f"c{i}" for i in range(6)]  # all verified
        assert resp.not_assessed_claims == []

    def test_disabled_verifies_all(self):
        claims = _draft_claims(6)
        walker = _Walker()
        resp = self._wrapper(_SelLLM("[1]"), walker, claims).respond(
            "q", select_central=False)
        assert sorted(walker.walked) == [f"c{i}" for i in range(6)]
        assert resp.not_assessed_claims == []
