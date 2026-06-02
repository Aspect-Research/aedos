"""Aedos v0.16.2 live-deployment backend.

A network-facing FastAPI service, kept SEPARATE from the engine package
(`src/aedos/`). It imports the engine as a library (`build_pipeline`,
`ChatWrapper`, `TierU`) and adds the deployment concerns the engine deliberately
does not carry: an access gate, CORS, rate limiting, per-session Tier-U scoping
(the A+ model), a session reset, and a "run Aedos on this text" endpoint.

Nothing here changes verdict logic; it is transport + multi-tenant plumbing.
"""
