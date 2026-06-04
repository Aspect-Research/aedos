# v0.16.2 — Live verification of the born_in enumeration-skip fix (Change 2-FINAL)

Confirms, against **live Wikidata**, that the `functional_entity_predicate` metadata
signal (commit `f1138e8`) eliminates the "Obama born_in Kenya" P17 fanout without any
verdict regression. Repro harness: `docs/v0_16_2/live_smoke_born_in.py`.

## Methodology (fully live, ZERO LLM)
- `build_pipeline` under `RUN_LIVE_KB=1` with a **keyless tripwire LLM** (every method
  raises) → no case raised, so the walk was **pure-KB**: resolution via live
  `wbsearchentities` + type filter; verify/enumerate via live SPARQL.
- `predicate_distribution` seeded **`distributes_down`** (NON-`neither`) for
  born_in / capital_of / located_in. This *disables the `neither` skip term*, so any
  discovery skip can only be the `functional_entity_predicate` signal — making the test
  **harder**, not easier (a `neither` seed would have masked the fix's own term).
- born_in seeded P19 single_valued=1 object_type=entity (the live deployment value,
  used 162×). Resolution caveat: the live `wbsearchentities` top hit for bare "Obama" is
  **Q41773 ("Obama", city in Fukui, Japan)**, not Barack Obama (Q76) — the live
  confirmation of the diagnosed subject-ambiguity that produces the no-statements path.

## A/B causation (identical claim + data + DB: "Obama born_in Kenya")
| | verdict | wall time | kb_neighbor_enumeration edges |
|---|---|---|---|
| signal ON (fix) | no_grounding_found | **1.86 s** | **0** |
| signal FORCED OFF (pre-fix) | no_grounding_found | **62.4 s** | **42** (39 part_of + 3 is_a), `wall_clock` |

The OFF arm reproduces the reported live bug (P17 part_of fanout → 30 s+ timeout). The
**edge-count contrast (0 vs 42) is confound-free** (immune to DB/cache state) and is the
proof of causation; the timing corroborates. Both arms share the identical verify-time
verdict (`no_grounding_found`) — the signal governs only post-abstain discovery.

## Broader live cases (signal ON; all 0 enum edges)
| claim | verdict | depth | note |
|---|---|---|---|
| Barack Obama born_in Honolulu | verified | 0 | exact P19 |
| Barack Obama born_in Hawaii | verified | 0 | Honolulu ⊆ Hawaii (directed) |
| Barack Obama born_in United States | verified | 0 | Honolulu ⊆ USA (directed) |
| Albert Einstein born_in Germany | verified | 0 | Ulm ⊆ Germany (directed) |
| Albert Einstein born_in France | **contradicted** | 0 | single-value, wrong — sound contradiction preserved |
| Paris capital_of France | verified | 0 | |
| Berlin capital_of France | **contradicted** | 0 | sound contradiction preserved |
| Obama born_in Kenya | no_grounding_found | 1 | subject → Q41773 city / no P19 |
| Barack Obama born_in Kenya | no_grounding_found | 1 | P19 = {hospital, Honolulu}: multi-value guard abstains (sound, conservative) |
| Williams College located_in US | verified | 0 | non-functional control: verifies directly |

**Headline:** every TRUE container claim verifies at **depth 0** via the directed
subsumption upgrade — enumeration is never needed — which is the live confirmation that
the skip costs **zero coverage**. Sound contradictions are preserved.

## Adversarial verification (4 lenses + prior unit review) — all CONFIRMED
- **Causation:** only the `functional_entity_predicate` OR-term is live in the A/B
  (`neither` excluded by the seed; `value_known_entity`/`functional_value_known` require
  `bool(statements)`, False for a no-P19 subject). Edge-count proof is confound-free.
- **§3.2 soundness:** the signal is stamped only on NO_MATCH traces (kb_verifier.py
  `last_no_match` + defensive returns), absent on VERIFIED/CONTRADICTED; `_discover_chains`
  is structurally unreachable after a non-None verdict (the walk loop `continue`/`break`s
  before it); enumeration emits only candidate Claims, never verdicts. **Abstain-only.**
- **Over-abstention:** object-substitution enumeration shares the *exact* `(P131,P30,P17)+`
  closure (the single `_build_transitive_ask_query`) the directed all-values upgrade
  already exhausts at depth 0 → strictly dominated; subject-substitution is semantically
  void for functional-entity facts. No constructible TRUE-but-now-abstains case.
- **Methodology:** the signal is **metadata-only**, independent of the subject QID, so the
  Q41773-vs-Q76 resolution divergence changes neither *whether* the fix fires nor its
  correctness. The "Barack Obama born_in Kenya" case empirically covers the Q76 +
  statements-found branch.

## What the live evidence does and does NOT establish
- **Establishes:** the fix eliminates the born_in fanout (0 vs 42 enum edges on identical
  live data); true claims still verify (at depth 0); sound contradictions are preserved;
  the walk is pure-KB on these inputs.
- **Does NOT establish (honest caveats — none threaten soundness):**
  1. *Production LLM resolution path.* The smoke disables the Wikipedia normalizer
     (production uses it + an LLM for subject disambiguation); no LLM key is available
     here. The fix is *provably* resolution-independent (metadata-only signal) and the Q76
     branch is covered by "Barack Obama born_in Kenya", but the end-to-end production
     resolution path was not *run*. Most valuable follow-up: "Obama born_in Kenya" with the
     normalizer enabled + a real/recorded LLM.
  2. *Timing airtightness.* Run order / resolution-cache clearing between the A/B arms was
     not controlled, so the *timing* half carries a minor cache confound; the **edge-count**
     half (the actual proof) is confound-free.
  3. *Oracle-trust watch item.* A predicate WRONGLY marked `single_valued=1` would skip an
     enumeration that could ground a true claim — a coverage cost only, and **strictly
     safer** than the false-CONTRADICT the same flag already governs. Pre-existing trust
     dependency, not a new §3.2 risk.
