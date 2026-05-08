"""Legacy v0.13 stack, preserved through the v0.14.x minor-version line.

The v0.14 cutover moved the v2 stack (formerly ``src/aedos_v2/``)
to top-level ``src/``; the v0.13 modules that v2 doesn't depend on
moved here. Hot rollback to
v1 is supported by re-mounting ``src/legacy/app.py`` at ``/`` until
v0.15 deletes this directory.

Truly-shared infrastructure (``llm_client``, ``llm_clients``,
``cost``, ``cache``, ``verifiers``) stays at top-level ``src/`` and
is consumed by both stacks.
"""
