"""Offline diagnostics for LLM prompt caching."""

from cachelint._compare import (
    ComparisonResult,
    ContextDifference,
    Divergence,
    Finding,
    compare_requests,
)
from cachelint._estimate import PrefixEstimate
from cachelint._ingest import (
    CanonicalRequest,
    IncomparableRequestsError,
    InvalidRequestError,
    Provider,
    Segment,
    SourceLocation,
    canonicalize_request,
)
from cachelint._version import __version__

__all__ = [
    "__version__",
    "CanonicalRequest",
    "ComparisonResult",
    "ContextDifference",
    "Divergence",
    "Finding",
    "IncomparableRequestsError",
    "InvalidRequestError",
    "Provider",
    "PrefixEstimate",
    "Segment",
    "SourceLocation",
    "canonicalize_request",
    "compare_requests",
]
