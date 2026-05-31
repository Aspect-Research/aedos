# Phase 0 Ambiguities

## Ambiguity 1: Import path for v0.15 modules

**Question:** The plan says `uvicorn aedos_v0_15.app:app`, implying `aedos_v0_15` is a top-level package. But the package lives at `src/aedos_v0_15/`, and v0.14 uses `from src.xxx` absolute imports. What import structure does v0.15 use?

**Resolution chosen:** Use `src.aedos_v0_15` as the absolute import path everywhere. Tests import as `from src.aedos_v0_15.xxx import yyy`. The uvicorn invocation is `uvicorn src.aedos_v0_15.app:app` (or equivalently, the FastAPI app is started via `python -m uvicorn src.aedos_v0_15.app:app`). Within the package, modules use relative imports.

**Alternative rejected:** Making `aedos_v0_15` a top-level package by adding an extra packaging configuration. This would require modifying pyproject.toml significantly and could break v0.14's packaging. Keeping both under `src/` is the simpler approach consistent with v0.14's structure.

**Reasoning:** Consistency with v0.14's import style. The implementation plan says v0.14 is the reference — using the same `src.` prefix avoids a packaging regression.

## Ambiguity 2: Cost module dependency in LLM client

**Question:** The v0.14 LLM client imports `from src.cost import CallCost, cost_for_call`. v0.15 doesn't define a cost module — it tracks budget via the walker's resource budget, not per-call cost telemetry. Should the v0.15 LLM client include cost tracking?

**Resolution chosen:** Include minimal cost tracking inline in the LLM client (a simple `TokenUsage` dataclass instead of importing `src.cost`). This preserves the ability to track LLM call counts (needed for the walker's `max_llm_calls` budget) without depending on v0.14's cost module.

**Alternative rejected:** Removing cost tracking entirely. The walker's resource budget requires counting LLM calls, so some form of call tracking is necessary. Removing it would mean a more invasive change later.

**Reasoning:** Conservative — having the tracking available is better than not having it. It enables the walker's LLM-call budget enforcement.

## Ambiguity 3: Sandbox allow-list enforcement mechanism

**Question:** The v0.14 sandbox uses subprocess isolation without import restriction. v0.15 requires an explicit allow-list (`datetime, math, decimal, fractions, statistics, re, unicodedata, string`). How should the allow-list be enforced?

**Resolution chosen:** AST-based import scanning before execution. Parse the generated code with Python's `ast` module, walk the tree for `Import` and `ImportFrom` nodes, check each module name against the allow-list. Refuse execution if any import is not in the list. Then execute in a subprocess (same as v0.14).

**Alternative rejected:** RestrictedPython library or similar. This adds a dependency and has its own failure modes. AST scanning is simpler and sufficient for our threat model (the code is LLM-generated, not adversarial).

**Reasoning:** Conservative. AST scanning before subprocess execution adds defense-in-depth. Any code that passes the scanner and then somehow circumvents the subprocess isolation is outside our threat model (which is: prevent accidental network/IO in LLM-generated code).

## Ambiguity 4: HTTP cache LRU size and default TTL

**Question:** The plan says "in-process LRU" with "deployment-configurable TTLs" but doesn't specify defaults.

**Resolution chosen:** Default LRU size: 256 entries. Default TTL: 3600 seconds (1 hour) for Wikidata entity resolutions; 86400 seconds (24 hours) for Wikidata statement lookups. The config object exposes both as overridable parameters.

**Alternative rejected:** No default TTL (require explicit configuration). This would break cold-start deployments.

**Reasoning:** The architecture says the HTTP cache is for Wikidata queries. Entity resolution results are less stable than statement lookups (a Wikidata entity might get merged). 1 hour for resolutions, 24 hours for statements is a reasonable conservative default.

## Ambiguity 5: `audit_log` vs `verification_context` field in audit log schema

**Question:** The plan's `audit_log` schema has a `verification_context` field described as "nullable; reference to the verification result that triggered this, if applicable." In Phase 0, there are no verification results yet. Should the field be populated from Phase 0 events?

**Resolution chosen:** Leave `verification_context = None` for all Phase 0 audit events (row_created events from infrastructure setup). The field is populated starting in Phase 3 when actual verification contexts exist.

**Alternative rejected:** Omitting the field from Phase 0 writes. The field exists in the schema; None is the correct null value.

**Reasoning:** Schema is created once in Phase 0 and used throughout. The field being nullable covers the Phase 0 case correctly.
