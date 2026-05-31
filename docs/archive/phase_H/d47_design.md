# Phase H D47 — Contextual entity normalization (design)

Closes v0.16 D47 work item 1 + parts of items 2 and 3. Adds a normalization
step that resolves bare ambiguous entity references to canonical Wikipedia
article titles before the substrate sees them. Two-stage design: deterministic
Wikipedia-redirect resolution, with an LLM-mediated selection fallback that
biases to **explicit abstention** when context does not disambiguate.

Surfaced as the underlying-cause finding from Phase G D33's live validation
(`docs/phase_G/d33_validation.md`). The Wikidata-side fixes D33 delivered are
necessary but not sufficient: when the canonical entity's label set in Wikidata
doesn't include the asserted reference, no amount of post-hoc filtering
rescues it. The fix has to happen upstream of the KB queries.

## Goal

Lift the architectural ceiling on entity-resolution and walker corpora caused
by bare ambiguous references that cannot be resolved through Wikidata's data
model alone. Use Wikipedia's editorial redirect system for the cases it
handles cleanly (the common, deterministic case) and fall back to a bounded
LLM judgment over a closed candidate set (the genuinely-ambiguous case),
honoring Aedos's soundness commitment via the abstention path when no
candidate clearly matches.

## Architecture

A new pipeline step between extraction (Layer 1) and substrate oracles
(Layer 3). The step processes each entity in each extracted claim and produces
a normalized form. The structured claim is updated with the canonical labels;
the audit log preserves both the original surface form and the normalized
form for traceability.

```
text ─► Extractor.extract ─► [normalization step] ─► substrate / KB queries
                                      │
                                      └─► audit_log: per-entity events
```

Module location: `src/aedos/layer1_extraction/wikipedia_normalizer.py`.
Conceptually post-extraction / pre-substrate; physically lives in
`layer1_extraction` because it consumes extraction output directly and its
input data shape (surface forms + source text + claim slots) is Layer-1
domain. The substrate stays agnostic to where its references came from.

## Two-stage normalization

### Stage 1 — Wikipedia redirect resolution (deterministic)

For each entity reference, query the MediaWiki API at
`https://en.wikipedia.org/w/api.php` with

```
action=query
titles={surface_form}      (pipe-separated for batched calls, up to 50)
redirects=1
prop=pageprops
format=json
```

The response distinguishes four outcomes:

- **`canonical_no_redirect`** — the title is itself a valid Wikipedia
  article. No `redirects` block appears for the title and `pageprops` does
  not flag disambiguation. Normalized form = surface form unchanged.

- **`clean_redirect`** — the response's `redirects` array includes an
  entry whose `from` matches the surface form (case-insensitive) and whose
  `to` is a normal article (pageprops not a disambiguation page).
  Normalized form = the canonical article title (the `to` value, also
  echoed in the response's `pages[…].title`).

- **`disambiguation_page`** — the resolved page's `pageprops` contains
  `disambiguation`. Stage 1 returns this outcome and stores the
  disambiguation page's title; Stage 2 fetches its candidate list and
  invokes the LLM.

- **`not_found`** — the response's `pages` block contains a `missing: ""`
  entry for the title. Normalized form = surface form unchanged.
  Downstream behavior unchanged from today (likely a low-recall resolution
  or abstention).

A fifth bucket — `api_error` — covers HTTP failures, JSON-parse failures,
and malformed responses. Treated as `not_found` for normalization output
(surface form unchanged) but tagged distinctly in the audit log so
operational issues are visible.

### Stage 2 — LLM-mediated selection from disambiguation candidates

When Stage 1 returns `disambiguation_page`, fetch the disambiguation page's
candidate links via

```
action=parse
page={disambiguation_title}
prop=links
format=json
```

The `links` array contains every internal link on the disambiguation page.
Filter to the namespace-0 (article) links and drop the obvious noise
(meta-pages, the disambiguation page's own self-links, navigation
sub-headers). Truncate to the first ~20 candidates if more remain — Stage 2's
LLM call doesn't benefit from a longer list and the prompt budget is finite.

Pass to Haiku 4.5 via a tool-call schema:

```
inputs:
  surface_form    : the bare reference being disambiguated
  claim           : {subject, predicate, object, polarity}
  source_text     : the full input text the extractor saw
  candidates      : list of Wikipedia article titles
tool output:
  selection       : one of the candidate strings, OR the literal "ABSTAIN"
  reasoning       : 1-2 sentence justification (for audit log)
```

The prompt:

```
You disambiguate ambiguous entity references using surrounding context.

The user wrote a claim whose entity reference matches multiple Wikipedia
articles. Pick the article whose subject the user most plausibly meant,
based on the surrounding text. If the surrounding text does not provide
clear evidence for one candidate, output ABSTAIN.

Abstention is the correct response when context does not determine the
answer. Do NOT guess based on prior probability or what seems most likely
in general — guess only when the source text actively supports the guess.
A wrong selection is worse than an abstention; abstention lets the system
honestly report it could not verify, which is the intended behaviour.

Inputs:
  surface form : {surface_form}
  claim        : {claim_subject} → {claim_predicate} → {claim_object}
  source text  :
  ---
  {source_text}
  ---
  candidates   :
    - {c0}
    - {c1}
    ...

Output the candidate string that best matches the source text, OR
ABSTAIN if no candidate clearly matches.
```

`purpose="layer1:entity_normalization"` is added to
`DEFAULT_MODEL_BY_PURPOSE` mapped to `claude-haiku-4-5` (Anthropic native).
Tool-call output handling reuses `LLMClient.extract_with_tool`; the same
fallback pattern for content-not-tool-calls applies (Haiku 4.5 is reliable
here, but the harness should not crash if a future model emits content
text).

### Why Stage 2 biases to abstention

The framing the operator articulated: "this is a problem of the user being
more or not enough precise with their words." If a user wrote "Smith proved
the theorem" with no further context, no LLM should confidently guess which
Smith was meant. The correct behavior is to leave the surface form
unchanged, let downstream resolution abstain, and surface a "could not
verify; please be more specific" response to the user. The prompt makes
this explicit and unstigmatized.

## Pipeline integration

### Where the normalization fires (operator answer: inside `EntityResolver.resolve`)

Per the design check-in, the normalization step is invoked from inside
`EntityResolver.resolve` itself. Two reasons:

1. **The corpus runner question.** Phase D / Phase E inherited a calibration
   runner pattern where many runners invoke substrate components directly,
   bypassing extraction. Specifically, `_run_entity_resolution` calls
   `h.resolver.resolve(reference, ctx)` on bare references — extraction is
   never involved. A strict pipeline-only D47 (only firing between
   `Extractor.extract` and substrate) would leave `entity_resolution_corpus`
   measuring 82% post-D47 because the runner doesn't traverse the new path.
   Embedding the normalization inside the resolver gives every caller —
   pipeline, corpus runner, future ad-hoc paths — the lift without needing
   each call site to opt in.

2. **Resolver-internal makes the resolver's contract honest.** The resolver
   already accepts a `LocalContext` and is the natural boundary at which the
   "what entity does this reference name?" question is asked. The
   normalization step is just an additional disambiguation pass alongside the
   existing wbsearchentities + D33 type filter + LLM-disambiguation pipeline
   the resolver already runs. The architectural cost is small; the substrate
   gains an additional input dependency (the MediaWiki client) and the
   `LocalContext` gains an optional source-text field.

The architectural trade-off recorded: substrate now embeds an upstream
concern (input-text disambiguation), which is a layer-purity weakening. The
operator accepted this because the alternative (a strict pipeline-only
integration plus runner updates) creates the same kind of runner-vs-corpus
divergence Phase D's D24 was meant to prevent. **Captured as a new v0.16
delta (D48-class)**: "audit each calibration runner's pipeline-traversal
shape vs. the corpus's intent" — a structural pattern observation, not a
fix-needed-now finding.

### Where the source text comes from (operator answer: thread alongside)

The full input text is **not** added to the `Claim` dataclass (which would
bloat Claim for every consumer). It is threaded request-scoped through the
verification pipeline:

- `VerificationContext` gains an optional `source_text: Optional[str]`
  field. Populated by pipeline-level code (`ChatWrapper.respond`,
  `benchmark.AedosRunner.run_case`, the calibration runner where applicable)
  from the text the extractor was originally called with.
- `Walker.walk` passes `context.source_text` down to `KBVerifier.verify`.
- `KBVerifier.verify` populates the `LocalContext` it builds with
  `source_text`.
- `EntityResolver.resolve` reads `local_context.source_text` and passes it
  to the normalizer.

For call sites that don't have a meaningful source text (the direct-resolver
corpus runner, ad-hoc tests), `source_text=None` is the legitimate value.
Stage 2 then sees a None text and the prompt's abstention bias fires hard —
which is the correct behavior for "bare reference, no context."

### Where the normalized form is consumed (operator answer: KB + Tier U)

Both KBVerifier and Tier U key on the normalized form. The motivating
scenario: yesterday the user wrote "Obama signed the bill" (normalized to
"Barack Obama"), today they write "Barack Obama signed the bill". Tier U
should treat these as the same row, not two parallel assertions. Keeping
both forms unified inside the substrate is the only way to make
cross-utterance entity identity work.

The change to Tier U:

- `TierU.lookup` keys on the normalized form when it is present, surface
  form otherwise. (Existing rows without a normalized form keep their
  surface-form keying — no migration needed.)
- `TierU.write` stores the normalized form alongside surface form
  (`resolved_subject_id` / `resolved_object_id` columns are the existing
  place for KB Q-ids; we reuse the existing `subject` / `object` columns
  for the normalized canonical text and add `subject_surface` /
  `object_surface` for the original).

This is a Tier U semantic change. It is **bounded**: deduplication of
cross-utterance references with the same canonical entity. It does **not**
change polarity-conflict detection, object-conflict (D16) detection, or
the write-path closure rule — those all key on the same (party, subject,
predicate, object) tuple, now compared on the normalized form.

DB migration: idempotent `ALTER TABLE tier_u ADD COLUMN subject_surface
TEXT` + `object_surface TEXT`, same pattern as D33's
`subject_entity_types` / `object_entity_types` migration. Both default
NULL — pre-D47 rows keep working.

### Where normalization does NOT fire

- **First-person subjects.** `Extractor._canonicalize` replaces "I" / "we"
  with the asserting party identifier (the user id, the deployment id).
  These are post-canonicalization; they are not Wikipedia article titles
  and normalizing them would silently invent a wrong canonical. The
  normalizer skips any reference matching the asserting party.

- **Reified event subjects.** Decomposed event claims use synthetic
  `event_xxx` ids as subjects. Skipped.

- **Walker-synthesized claims.** When the walker expands via substrate
  subsumption traversal, it substitutes the slot entity with a taxonomy
  neighbor from `Substrate.subsumption.find_neighbors`. Those entities
  are already canonical substrate-side ids; running them through Wikipedia
  redirect is wasteful and risks normalization disagreement. Skipped via
  a flag on the synthesized claim (or via the existing `EntityRef` namespace
  check).

- **Stage 1 cache + Stage 2 cache.** Stage 1 is HTTP-cached at the
  `CachingHTTPClient` level (entity TTL). Stage 2 is not cached — the
  selection is context-dependent (source text varies per claim) and
  re-invoking the LLM on each call is the correct behavior. The Stage 2
  audit event records the selection so post-hoc analysis sees the
  decisions; an in-memory hash of (surface_form, source_text_hash,
  candidates_hash) could memoize within one run if needed, but is not
  included in v0.15.

## Schema changes

### `Claim` (no fields added)

Per the operator's source-text-threading answer. Claim stays as-is.

### `VerificationContext`

```python
@dataclass
class VerificationContext:
    current_time: str
    asserting_party: str
    source_text: Optional[str] = None  # D47: full input text for Stage 2 context
```

Backward-compatible: existing call sites that don't populate `source_text`
get None, and Stage 2's abstention bias handles that path.

### `LocalContext`

```python
@dataclass
class LocalContext:
    predicate: str
    slot_position: str
    asserting_party: Optional[str] = None
    prior_resolutions: list["ResolutionCandidate"] = field(default_factory=list)
    expected_entity_types: list["KBEntityID"] = field(default_factory=list)
    # D47:
    source_text: Optional[str] = None  # Stage 2 disambiguation context
    claim_subject: Optional[str] = None  # Stage 2 claim context
    claim_predicate: Optional[str] = None
    claim_object: Optional[str] = None
```

The four D47 fields default to None; pre-D47 callers continue to work, and
Stage 2 falls through to "no context → bias to abstention" when they are
absent.

### Tier U schema

Idempotent `ALTER TABLE` migration adding `subject_surface TEXT` and
`object_surface TEXT` columns. Pre-D47 rows have NULL surface columns and
key on the existing `subject` / `object` columns directly.

### Audit event shape

Per-entity normalization events with `event_type="entity_normalization"`:

```json
{
  "event_type": "entity_normalization",
  "event_subject": "Obama",
  "event_data": {
    "claim_id": "...",
    "slot_position": "subject",
    "surface_form": "Obama",
    "stage_1_outcome": "disambiguation_page",
    "stage_1_redirect_target": "Obama (disambiguation)",
    "normalized_form": "Barack Obama",
    "stage_2_invoked": true,
    "stage_2_candidates": ["Barack Obama", "Michelle Obama", "Obama, Fukui", ...],
    "stage_2_selection": "Barack Obama",
    "stage_2_reasoning": "The source text mentions the President signing a bill, which matches Barack Obama's role.",
    "duration_ms": 142.7
  }
}
```

Distinct events per entity per claim — verbose for Phase 10.5 post-hoc
analysis but the audit log is the right home for normalization decisions
operators may need to second-guess. The `event_subject` is the surface form
so audit queries by reference work cleanly.

When normalization is a no-op (`canonical_no_redirect`), the event still
fires (for observability), but Stage 2 fields are null and the duration is
small.

## MediaWiki API client

New module `src/aedos/layer1_extraction/wikipedia_normalizer.py`. Mirrors
the patterns from `kb_wikidata.py`:

```python
class WikipediaNormalizer:
    def __init__(
        self,
        http_cache: CachingHTTPClient,
        llm_client: LLMClient,
        db: sqlite3.Connection,
        config: Config,
    ) -> None: ...

    def normalize(
        self,
        surface_form: str,
        claim_subject: str,
        claim_predicate: str,
        claim_object: str,
        source_text: Optional[str],
        slot_position: str,
        claim_id: str,
    ) -> NormalizationResult: ...

    def normalize_batch(
        self,
        references: list[str],
    ) -> dict[str, Stage1Outcome]: ...
```

- **HTTP cache + rate limit + User-Agent**: reuses the `CachingHTTPClient`
  already wired by `build_pipeline` (which threads `Config.user_agent`).
  Adds a `WikipediaNormalizer._wikipedia_limiter` rate limiter at
  10 req/s default (MediaWiki is generous; this is well below the
  per-IP fairness limit).
- **Batching**: `normalize_batch` is for the Stage 1 path only. When a
  call site has multiple references it accumulates them and issues one
  `titles=A|B|C` query (up to 50 per batch, batched if more). The result
  is a per-reference dict of Stage 1 outcomes; Stage 2 is then called
  individually for each disambiguation-page result that has a context to
  use.
- **Error handling**: HTTP failures → `api_error` (logged in audit,
  treated as `not_found` for normalization output). Never raises into the
  pipeline; fail-open is the right default for an additive normalization
  step (architecture §3.2: false-abstain is cheaper than letting a
  normalizer outage break verification).

### `NormalizationResult` dataclass

```python
@dataclass
class NormalizationResult:
    surface_form: str
    normalized_form: str
    stage_1_outcome: str  # canonical_no_redirect | clean_redirect | disambiguation_page | not_found | api_error
    stage_2_invoked: bool = False
    stage_2_candidates: list[str] = field(default_factory=list)
    stage_2_selection: Optional[str] = None  # None on abstain
    stage_2_reasoning: Optional[str] = None
    stage_1_redirect_target: Optional[str] = None
    duration_ms: float = 0.0
```

The audit-log event is produced from this object inside `normalize()` so
all logging happens at one place.

## Configuration

`src/aedos/config.py` additions (following the F3 pattern):

```python
wikipedia_api_url: str = "https://en.wikipedia.org/w/api.php"
wikipedia_request_rate_per_second: float = 10.0
wikipedia_normalizer_enabled: bool = True  # diagnostic kill switch
wikipedia_stage_2_max_candidates: int = 20  # truncate disambiguation links
```

`user_agent` is reused (same Wikimedia policy applies).

Validation in `__post_init__`:
- `wikipedia_api_url` must be http(s) URL.
- `wikipedia_request_rate_per_second` > 0.
- `wikipedia_stage_2_max_candidates` > 0.

## Wikidata sitelink mapping

The design note in the operator's prompt mentioned canonical Wikipedia
article titles "map deterministically to Wikidata Q-ids via sitelinks."
This mapping happens **downstream** in the existing Wikidata resolution
path — D47 produces a normalized string ("Barack Obama"), the resolver
passes it to `wbsearchentities`, which returns Q76 at rank 0 because the
canonical full label IS the Wikidata label. No separate Wikidata sitelink
call is added in D47.

Confirmed by the Phase G D33 validation:
`test_barack_obama_full_name_reaches_q76` passes against live Wikidata
with the type filter — feeding the canonical full label into the existing
resolution pipeline reaches Q76 cleanly.

## Validation gates

After implementation:

1. **The two D47-pinning xfails flip to passing** (or are reframed as
   passing-with-normalization). `test_obama_short_query_does_not_yield_canonical_q76`
   and `test_williams_college_short_query_does_not_yield_canonical_q49112`
   in `tests/integration/live/test_wikidata_live.py`. With D47, bare
   "Obama" → Stage 1 (probably disambiguation) → Stage 2 (without source
   text, may abstain depending on Wikipedia's current structure — see
   "caveats" below). Bare "Williams College" → Stage 1: needs live check
   on what Wikipedia returns for that title.

2. **End-to-end through derivation**: a derivation case whose claim has
   a bare entity reference + surrounding text disambiguation succeeds
   end-to-end through extraction → normalization → resolution →
   verification.

3. **Audit log captures normalization events** with all four required
   fields (surface form, Stage 1 outcome, normalized form, Stage 2 details
   when applicable).

4. **`entity_resolution_corpus` accuracy lifts** measurably above the 82%
   Phase E baseline. The expected lift is bounded by how many of the
   bare-reference cases have clean Wikipedia redirects (Stage 1) vs.
   disambiguation pages without source text (Stage 2 → likely abstain →
   no improvement).

5. **`derivation_corpus` accuracy lifts** on the cases where entity
   resolution was the bottleneck. Predicting the lift quantitatively is
   hard because D5 (KB neighbor enumeration) and D16 walker fixes are
   still pending.

6. **Full pytest suite green** — no regression on existing tests. The
   Tier U schema migration is the highest-risk change; verify the
   existing Tier U tests still pass.

## Caveats this design accepts (operator-confirmed)

- **English Wikipedia only.** Non-English entities or claims in other
  languages are out of scope for v0.15. Corpora are English.
- **Freshness not addressed.** A new entity becoming prominent after
  Wikipedia's current state has no normalization until Wikipedia adds
  the redirect.
- **Wikipedia editorial correctness.** D47 inherits Wikipedia's editorial
  decisions on which redirect target is canonical. For prominent entities
  this is reliable; for niche ones, less so. Trying to second-guess with
  LLM judgment would re-introduce the variability we're avoiding.
- **The bare-reference + no-context corpus cases may still fail.** A test
  like `er_type_filter_001` ("Obama" with predicate=holds_role,
  expected_type=Q5, expected=Q76) provides only the predicate as
  disambiguation context. If Wikipedia's "Obama" → disambiguation page
  and the predicate alone doesn't unambiguously identify Barack Obama in
  the LLM's judgment, Stage 2 abstains and the test fails. This is
  Architectural: the corpus encodes system intent (the LLM should pick
  Barack Obama given the role-holding context), and D47's abstention bias
  is conservative on weak context. If the corpus needs to pass these
  cases, the prompt may need tuning, OR the test may need to provide
  richer context — both are calibration questions, not D47 design
  questions.

## Open questions (surfaced for v0.16, not blocking D47)

1. **Wikipedia article title → Wikidata Q-id direct lookup.** Today's
   path is normalized string → wbsearchentities → Q-id. A future
   optimization could use Wikipedia's sitelink API to skip
   wbsearchentities for the cases where the normalized title has a
   direct sitelink. Modest performance win; deferred until Phase 10.5
   data justifies it.

2. **Stage 2 caching across runs.** A persistent (surface_form,
   source_text_hash, candidates_hash) → selection cache would amortize
   the LLM cost across calibration runs that re-process the same
   inputs. Modest cost win; not load-bearing for v0.15.

3. **Cross-language normalization.** Out of scope per the design.
   v0.16 candidate when non-English corpora become relevant.

4. **The runner-vs-corpus shape question** that arose during D47
   planning. Many calibration runners (`_run_entity_resolution`,
   `_run_kb_mapping`, others) invoke substrate components directly,
   bypassing the pipeline. The shape divergence between "how the
   pipeline traverses the system" and "how the corpus runner traverses
   the system" is the same class of issue D24 named for runner-vs-corpus
   key mismatches. **Captured as a new v0.16 delta**: "audit each
   calibration runner's pipeline-traversal shape vs. the corpus's
   intent; surface where direct-substrate-call runners would miss
   downstream-step features (D33 type filter, D47 normalization, future
   pipeline steps)." Out of D47's implementation scope.

## Implementation order (operator-specified)

Per the session prompt's "Implementation order" section:

- **Step 1** (~1 day): MediaWiki client + Stage 1 logic + tests.
- **Step 2** (~0.5 day): Stage 2 LLM selection + tests.
- **Step 3** (~0.5 day): Pipeline integration (VerificationContext +
  LocalContext + Tier U schema + EntityResolver wiring + audit events).
- **Step 4** (~0.5 day): Integration tests, including the xfail flip.
- **Step 5** (~0.5 day): Calibration corpus re-measurement +
  `docs/phase_H/d47_validation.md`.

Total estimated effort: ~3 days. Operator-check between steps.

## Phase H sequencing reminder

D47 is the first of Phase H's three items. After D47 + validation,
proceed to D16 (walker fix, small focused investigation), then D5 (KB
neighbor enumeration, largest piece). `rc.11` tags after all three deltas
land. Phase 10.5 starts from rc.11.
