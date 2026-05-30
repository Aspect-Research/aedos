"""Phase H D16 diagnostic.

Reproduces der_revision_001 and der_revision_002 from the derivation_corpus
in the same case order the calibration runner uses, surfaces the walker
verdict, the Tier U state, and the audit log for each, and prints whether
Stage 1 / Stage 3 / object-conflict fired.

Run: py scripts/diag_d16.py

No API spend: uses a MockTransport for the LLM (matches the integration
test pattern in tests/integration/test_walker_with_substrate.py). The
substrate predicates the walker consults (`prefers`, `employed_by`,
`works at`) are routed through the mock, which returns the seed-pack
values verbatim (single_valued=1 for prefers, 0 for employed_by, etc.).
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from aedos.audit.log import query_events  # noqa: E402
from aedos.database import open_memory_db  # noqa: E402
from aedos.layer1_extraction.extractor import Claim  # noqa: E402
from aedos.layer1_extraction.triage import TriageDecision  # noqa: E402
from aedos.layer3_substrate import Substrate  # noqa: E402
from aedos.layer3_substrate.predicate_distribution import PredicateDistributionOracle  # noqa: E402
from aedos.layer3_substrate.predicate_translation import PredicateTranslation  # noqa: E402
from aedos.layer3_substrate.resolver import EntityResolver  # noqa: E402
from aedos.layer3_substrate.subsumption import SubsumptionOracle  # noqa: E402
from aedos.layer4_sources.kb_protocol import (  # noqa: E402
    ResolutionCandidate,
    SubsumptionResult,
)
from aedos.layer4_sources.kb_verifier import KBVerifier  # noqa: E402
from aedos.layer4_sources.python_verifier import PythonVerifier  # noqa: E402
from aedos.layer4_sources.tier_u import TierU  # noqa: E402
from aedos.layer4_sources.walker import VerificationContext, Walker  # noqa: E402
from aedos.llm.client import LLMClient  # noqa: E402


# Per-predicate metadata the LLM "would" generate. Values copied from
# seeds/predicate_translation.json so the mock LLM matches the deployed
# seed pack.
_SEED_PREDICATES = {
    "prefers": {
        "object_type": "entity",
        "user_subject_required": 1,
        "routing_hint": "user_authoritative",
        "kb_namespace": None,
        "kb_property": None,
        "slot_to_qualifier": None,
        "single_valued": 1,
        "reason": "diagnostic seed (prefers)",
    },
    "employed_by": {
        "object_type": "entity",
        "user_subject_required": 0,
        "routing_hint": "kb_resolvable",
        "kb_namespace": "wikidata",
        "kb_property": "P108",
        "slot_to_qualifier": {"subject": "statement_subject", "object": "statement_value",
                              "start": "qualifier:P580", "end": "qualifier:P582"},
        "single_valued": 0,
        "reason": "diagnostic seed (employed_by)",
    },
    "works at": {
        # The Phase E5 transcript shows the extractor produced predicate
        # "works at" with a space rather than the canonical "employed_by".
        # The mock returns the same shape an LLM probably would for this
        # non-normalized predicate.
        "object_type": "entity",
        "user_subject_required": 0,
        "routing_hint": "kb_resolvable",
        "kb_namespace": "wikidata",
        "kb_property": "P108",
        "slot_to_qualifier": {"subject": "statement_subject", "object": "statement_value"},
        "single_valued": 0,
        "reason": "diagnostic seed (works at)",
    },
}


class MockTransport:
    def __init__(self):
        self.call_count = 0
        self.calls: list[dict] = []

    def extract_with_tool(self, *a, purpose=None, **kw):
        self.call_count += 1
        # LLMClient passes (system, user_message, tool) positionally; allow
        # kw fallback for direct callers.
        user_msg = a[1] if len(a) >= 2 else kw.get("user_message", "")
        self.calls.append({"purpose": purpose, "user_message": user_msg[:80]})

        if purpose == "substrate:predicate_distribution":
            return {"verdict": "neither", "reason": "diag"}
        if purpose == "substrate:subsumption":
            return {"verdict": "unrelated", "reason": "diag"}
        if purpose and purpose.startswith("substrate:predicate_translation"):
            # Pull the predicate out of the user message.
            for predicate, meta in _SEED_PREDICATES.items():
                if f'"{predicate}"' in user_msg:
                    return meta
            return {
                "object_type": "entity",
                "user_subject_required": 0,
                "routing_hint": "abstain",
                "kb_namespace": None,
                "kb_property": None,
                "slot_to_qualifier": None,
                "single_valued": 0,
                "reason": "diag default",
            }
        # Extractor calls — the diagnostic supplies claims directly, but
        # leave a stub in case anything triggers it.
        return {"claims": []}

    def chat(self, *a, **kw):
        return ""


class StubKB:
    def resolve_entity(self, r, lc):
        # Only "Google" / "Microsoft" / "Asa" stand in for entities; "Asa"
        # is a personal name (not in KB) so resolution returns []. Google /
        # Microsoft return a stub Q-id so the KB lookup path can fire.
        if r == "Google":
            return [ResolutionCandidate("Q95", score=0.9)]
        if r == "Microsoft":
            return [ResolutionCandidate("Q2283", score=0.9)]
        return []

    def lookup_statements(self, e, p):
        return []

    def subsumption(self, a, b, rt):
        return SubsumptionResult(verdict="unrelated")


def build_pipeline():
    db = open_memory_db()
    transport = MockTransport()
    client = LLMClient(_transport=transport)
    pt = PredicateTranslation(db=db, llm_client=client)
    kb = StubKB()
    resolver = EntityResolver(kb_protocol=kb, db=db, llm_client=client)
    sub = SubsumptionOracle(db=db, llm_client=client, kb_protocol=kb)
    pd = PredicateDistributionOracle(db=db, llm_client=client)
    substrate = Substrate(
        resolver=resolver,
        predicate_translation=pt,
        subsumption=sub,
        predicate_distribution=pd,
    )
    tier_u = TierU(db=db, predicate_translation=pt)
    kb_verifier = KBVerifier(kb_protocol=kb, entity_resolver=resolver, predicate_translation=pt)
    py_verifier = PythonVerifier(llm_client=client)
    walker = Walker(
        tier_u=tier_u, kb_verifier=kb_verifier,
        python_verifier=py_verifier, substrate=substrate,
    )
    return db, transport, walker, tier_u


def write_seed(tier_u, asserting_party, subject, predicate, object_val, polarity=1, valid_from=None):
    claim = Claim(
        claim_id="seed",
        subject=subject,
        predicate=predicate,
        object=object_val,
        polarity=polarity,
        source_text="seed",
        asserting_party=asserting_party,
        triage_decision=TriageDecision.VERIFY,
        valid_from=valid_from,
    )
    return tier_u.write(claim)


def walk_claim(walker, asserting_party, subject, predicate, object_val, polarity=1, source_text=""):
    claim = Claim(
        claim_id="walk",
        subject=subject,
        predicate=predicate,
        object=object_val,
        polarity=polarity,
        source_text=source_text,
        asserting_party=asserting_party,
        triage_decision=TriageDecision.VERIFY,
    )
    ctx = VerificationContext(
        current_time=datetime.now(timezone.utc).isoformat(),
        asserting_party=asserting_party,
        source_text=source_text,
    )
    return walker.walk(claim, ctx)


def dump_tier_u(db, header):
    rows = db.execute(
        "SELECT id, asserting_party, subject, predicate, object, polarity, "
        "valid_from, valid_until, retracted_at FROM tier_u ORDER BY id"
    ).fetchall()
    print(f"\n  Tier U state {header}:")
    if not rows:
        print("    (empty)")
        return
    for r in rows:
        d = dict(r)
        closed = "CLOSED" if d["valid_until"] else "open"
        retracted = "RETRACTED" if d["retracted_at"] else ""
        print(
            f"    id={d['id']:2}  "
            f"{d['asserting_party']:<12} {d['subject']:<10} {d['predicate']:<14} "
            f"{d['object']:<10} p={d['polarity']}  "
            f"{closed} {retracted}".rstrip()
        )


def dump_trace(result, header):
    print(f"\n  Trace {header}:")
    print(f"    verdict       = {result.verdict}")
    if result.abstention_reason:
        print(f"    abstain_reason= {result.abstention_reason}")
    print(f"    edges ({len(result.trace.edges)}):")
    for e in result.trace.edges:
        meta_str = ", ".join(f"{k}={v}" for k, v in (e.metadata or {}).items() if v is not None)
        print(f"      - {e.edge_type:<25} {meta_str}")


def run_diagnostic():
    print("=" * 72)
    print("Phase H D16 diagnostic — cross-case Tier U state leakage check")
    print("=" * 72)

    db, transport, walker, tier_u = build_pipeline()
    ASSERTER = "calibration"

    # Replay the corpus cases that run BEFORE der_revision_001/002 and that
    # write Tier U rows touching the same (asserting_party, subject, predicate)
    # the revision cases use.
    print("\n--- Step 1: replay prior cases' Tier U writes ---")

    print("\n  der_cross_002 writes (Asa, employed_by, Google, +1)")
    r = write_seed(tier_u, ASSERTER, "Asa", "employed_by", "Google", polarity=1)
    print(f"    write_result: row_id={r.row_id} idempotent={r.was_idempotent} "
          f"contradiction_closed={r.contradiction_closed} closed_ids={r.closed_row_ids}")

    print("\n  der_cross_005 writes (Asa, lives_in, Williamstown, +1)")
    r = write_seed(tier_u, ASSERTER, "Asa", "lives_in", "Williamstown", polarity=1)
    print(f"    write_result: row_id={r.row_id}")

    print("\n  der_cross_007 writes (Asa, born_in_year, 1973, +1)")
    r = write_seed(tier_u, ASSERTER, "Asa", "born_in_year", "1973", polarity=1)
    print(f"    write_result: row_id={r.row_id}")

    print("\n  der_cross_009 writes (Asa, prefers, coffee, +1)")
    r = write_seed(tier_u, ASSERTER, "Asa", "prefers", "coffee", polarity=1)
    print(f"    write_result: row_id={r.row_id} idempotent={r.was_idempotent} "
          f"closed_ids={r.closed_row_ids}")

    print("\n  der_cross_009 writes (Asa, prefers, tea, -1)")
    r = write_seed(tier_u, ASSERTER, "Asa", "prefers", "tea", polarity=0)
    print(f"    write_result: row_id={r.row_id}")

    dump_tier_u(db, "after prior cases")

    # --- der_revision_001 ---
    print("\n\n--- Step 2: der_revision_001 ---")
    print("  Corpus: text='Asa prefers coffee', tier_u_prior=(Asa, prefers, tea, +1)")
    print("  Expected verdict: contradicted")

    print("\n  der_revision_001 writes its tier_u_prior (Asa, prefers, tea, +1)")
    r = write_seed(tier_u, ASSERTER, "Asa", "prefers", "tea", polarity=1)
    print(f"    write_result: row_id={r.row_id} idempotent={r.was_idempotent} "
          f"contradiction_closed={r.contradiction_closed} closed_ids={r.closed_row_ids}")

    dump_tier_u(db, "after der_revision_001 seeding")

    # The corpus runner extracts the claim from text; we supply the claim
    # the v5 extractor produced (per the Phase E5 transcript: predicate "prefers"
    # for case 001).
    print("\n  Walker walks extracted claim (Asa, prefers, coffee, +1)")
    result = walk_claim(walker, ASSERTER, "Asa", "prefers", "coffee", source_text="Asa prefers coffee")
    dump_trace(result, "der_revision_001")

    print(f"\n  Mock LLM calls during walk: {transport.call_count}")
    for c in transport.calls:
        print(f"    {c['purpose']}: {c['user_message']}")
    transport.calls.clear()
    transport.call_count = 0

    # --- der_revision_002 ---
    print("\n\n--- Step 3: der_revision_002 ---")
    print("  Corpus: text='Asa works at Google', tier_u_prior=(Asa, employed_by, Microsoft, +1)")
    print("  Expected verdict: contradicted")

    print("\n  der_revision_002 writes its tier_u_prior (Asa, employed_by, Microsoft, +1)")
    r = write_seed(tier_u, ASSERTER, "Asa", "employed_by", "Microsoft", polarity=1)
    print(f"    write_result: row_id={r.row_id} idempotent={r.was_idempotent} "
          f"contradiction_closed={r.contradiction_closed} closed_ids={r.closed_row_ids}")

    dump_tier_u(db, "after der_revision_002 seeding")

    print("\n  Walker walks extracted claim (Asa, 'works at', Google, +1)")
    print("    (per Phase E5 transcript: extractor emits predicate='works at' literally)")
    result = walk_claim(walker, ASSERTER, "Asa", "works at", "Google", source_text="Asa works at Google")
    dump_trace(result, "der_revision_002 (predicate='works at')")

    print(f"\n  Mock LLM calls during walk: {transport.call_count}")
    for c in transport.calls:
        print(f"    {c['purpose']}: {c['user_message']}")
    transport.calls.clear()
    transport.call_count = 0

    # Also re-run der_revision_002 assuming a HYPOTHETICAL extractor that
    # normalizes to "employed_by" — to show the walker behavior under the
    # canonical-predicate variant.
    print("\n\n--- Step 4: der_revision_002 with normalized predicate 'employed_by' ---")
    print("  (Hypothetical: shows what the walker would do if the extractor")
    print("   normalized 'works at' -> 'employed_by'.)")
    result = walk_claim(walker, ASSERTER, "Asa", "employed_by", "Google", source_text="Asa works at Google")
    dump_trace(result, "der_revision_002 (predicate='employed_by')")

    # Variant: clear cross-case state and re-run der_revision_002 in isolation.
    # Exercises whether the failure is the leakage vs the multi-valued seed.
    print("\n\n--- Step 5: der_revision_002 in ISOLATION (no prior-case state) ---")
    print("  Fresh harness, seed only the case's own tier_u_prior, walk the claim.")
    db2, transport2, walker2, tier_u2 = build_pipeline()
    write_seed(tier_u2, ASSERTER, "Asa", "employed_by", "Microsoft")
    result = walk_claim(walker2, ASSERTER, "Asa", "employed_by", "Google", source_text="Asa works at Google")
    dump_trace(result, "der_revision_002 (isolated, employed_by single_valued=0)")

    # And the variant where employed_by is single_valued=1 (functional). This
    # is the "what if D23 reclassifies employed_by" thought experiment.
    print("\n\n--- Step 6: der_revision_002 in ISOLATION, employed_by single_valued=1 ---")
    _SEED_PREDICATES["employed_by"]["single_valued"] = 1
    db3, transport3, walker3, tier_u3 = build_pipeline()
    write_seed(tier_u3, ASSERTER, "Asa", "employed_by", "Microsoft")
    result = walk_claim(walker3, ASSERTER, "Asa", "employed_by", "Google", source_text="Asa works at Google")
    dump_trace(result, "der_revision_002 (isolated, employed_by single_valued=1)")
    _SEED_PREDICATES["employed_by"]["single_valued"] = 0  # restore

    # --- audit log ---
    print("\n\n--- Step 7: audit log events written during this run ---")
    events = query_events(db, limit=100)
    print(f"  Total audit events: {len(events)}")
    # Show belief-revision and tier-u closure events
    relevant = [
        e for e in events
        if e["event_type"] in {
            "tier_u_row_closed", "tier_u_parallel_assertion",
            "row_created", "row_retracted",
        }
    ]
    print(f"  Belief-revision / row-state events: {len(relevant)}")
    for e in relevant[:30]:
        data = e.get("event_data", {})
        if isinstance(data, dict):
            pred = data.get("predicate", "")
            closed = data.get("contradiction_closed", "")
            extra = f"predicate={pred} closed={closed}" if pred else ""
        else:
            extra = ""
        print(f"    {e['event_type']:<28} {e['event_subject']:<26} {extra}")


if __name__ == "__main__":
    run_diagnostic()
