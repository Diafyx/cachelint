"""Offline diagnostics for LLM prompt caching."""

from cachelint._compare import (
    ComparisonResult,
    ContextDifference,
    Divergence,
    Finding,
    compare_requests,
)
from cachelint._corpus import (
    CorpusGroup,
    CorpusLimits,
    CorpusPattern,
    CorpusResult,
    analyze_corpus,
)
from cachelint._estimate import PrefixEstimate
from cachelint._evidence import (
    CorpusUsageGroup,
    CorpusUsageTrace,
    ObservedCorpusResult,
    ObservedFinding,
    analyze_observed_corpus,
)
from cachelint._ingest import (
    CanonicalRequest,
    IncomparableRequestsError,
    InvalidRequestError,
    Provider,
    Segment,
    SourceLocation,
    canonicalize_request,
)
from cachelint._observed import (
    UsageGroup,
    UsageLimits,
    UsageObservation,
    UsageResult,
    UsageTrace,
    analyze_usage,
)
from cachelint._version import __version__
from cachelint.baseline import baseline_from_report, load_baseline
from cachelint.diagnosis import (
    DiagnosisFinding,
    DiagnosisLimits,
    DiagnosisResult,
    diagnose_requests,
)
from cachelint.diagnosis_report import (
    DiagnosisCorpusGroup,
    DiagnosisCorpusResult,
    RankedDiagnosisFinding,
    analyze_diagnosis,
    diagnosis_report,
    render_diagnosis_json,
    render_diagnosis_text,
)
from cachelint.regression import default_policy, evaluate_baseline, load_policy

__all__ = [
    "__version__",
    "CanonicalRequest",
    "ComparisonResult",
    "CorpusGroup",
    "CorpusLimits",
    "CorpusPattern",
    "CorpusResult",
    "CorpusUsageGroup",
    "CorpusUsageTrace",
    "ContextDifference",
    "Divergence",
    "DiagnosisCorpusGroup",
    "DiagnosisCorpusResult",
    "DiagnosisFinding",
    "DiagnosisLimits",
    "DiagnosisResult",
    "Finding",
    "IncomparableRequestsError",
    "InvalidRequestError",
    "ObservedCorpusResult",
    "ObservedFinding",
    "Provider",
    "PrefixEstimate",
    "RankedDiagnosisFinding",
    "Segment",
    "SourceLocation",
    "UsageGroup",
    "UsageLimits",
    "UsageObservation",
    "UsageResult",
    "UsageTrace",
    "analyze_usage",
    "analyze_corpus",
    "analyze_diagnosis",
    "analyze_observed_corpus",
    "canonicalize_request",
    "compare_requests",
    "diagnose_requests",
    "diagnosis_report",
    "baseline_from_report",
    "load_baseline",
    "default_policy",
    "evaluate_baseline",
    "load_policy",
    "render_diagnosis_json",
    "render_diagnosis_text",
]
