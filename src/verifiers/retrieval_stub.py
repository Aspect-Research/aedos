"""Back-compat surface for the v0.1 retrieval stub.

Kept so existing imports (``from src.verifiers.retrieval_stub import retrieval_verify``)
keep working. The actual implementation lives in ``retrieval_verifier`` now.
"""

from src.verifiers.retrieval_verifier import (  # noqa: F401
    RetrievalResult,
    RetrievalVerifier,
    retrieval_verify,
)
