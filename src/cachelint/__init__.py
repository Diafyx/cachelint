"""Offline diagnostics for LLM prompt caching."""

from cachelint._compare import (
    ComparisonResult,
    ContextDifference,
    Divergence,
    Finding,
    compare_requests,
)
from cachelint._corpus import CorpusGroup, CorpusLimits, CorpusResult, analyze_corpus
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
    "CorpusGroup",
    "CorpusLimits",
    "CorpusResult",
    "ContextDifference",
    "Divergence",
    "Finding",
    "IncomparableRequestsError",
    "InvalidRequestError",
    "Provider",
    "PrefixEstimate",
    "Segment",
    "SourceLocation",
    "analyze_corpus",
    "canonicalize_request",
    "compare_requests",
]
