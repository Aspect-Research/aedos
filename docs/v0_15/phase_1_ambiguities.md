# Phase 1 Ambiguities — Extraction (Layer 1)

## Ambiguity 1: Does `extract()` filter INERT_PROSE claims or return them flagged?

**Resolution:** `extract()` returns all non-future claims as `Claim` objects with `triage_decision` set. INERT_PROSE claims are not dropped inside the extractor — they are flagged and the caller (Layer 2 router) acts on the decision. Rationale: dropping inside the extractor makes triage behavior unobservable in tests; the architecture says triage *determines* which claims are routed, implying the determination is Layer 1's job and the routing action is Layer 2's.

## Ambiguity 2: First-person canonicalization in quoted spans

**Resolution:** "I" in any text the extractor is processing canonicalizes to the asserting party — including "I" inside a quoted sentence within the extracted text (e.g., `He said "I am the president"` → subject canonicalizes to asserting party because the whole text is attributed to the asserting party). This is the caller's text and the caller is the asserting party for the whole span.

## Ambiguity 3: What happens to the original subject/predicate/object in multi-participant decomposition?

**Resolution:** When `participants` is non-empty, the raw claim's `object` field becomes a `target` binary claim (event_id → `target` → object). The original `predicate` field is dropped (replaced by `has_participant`, `event_type`, `target`, and any temporal qualifier claims). Rationale: the architecture example uses `target` for the event's object, and the original predicate is encoded in `event_type`.

## Ambiguity 4: `valid_until=before_present` semantics

**Resolution:** `before_present` is a string sentinel (the literal string `"before_present"`). It means the claim's validity ended at some unspecified point before the current verification time. It is NOT a datetime; it is treated as a category in temporal scope comparison. The architecture uses it to distinguish "was X" (ended, unspecified when) from "is X" (currently in force).

## Ambiguity 5: Hard-claim discipline in post-processing

**Resolution:** Hard-claim discipline is primarily a prompting constraint (the system prompt explicitly forbids fabricating claims about context entities). Post-processing adds one heuristic check: if neither the subject nor object of a returned claim appears as a substring in the input text (case-insensitive, with first-person pronouns excepted), the claim is dropped. This catches the failure mode where the LLM ignores the prompt and invents claims about prior-conversation entities.
