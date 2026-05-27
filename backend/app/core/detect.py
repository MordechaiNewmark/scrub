"""
Detection layer.

Wraps Presidio's AnalyzerEngine (spaCy NER + regex recognizers) and adds:
  - legal-domain context words to boost confidence on names near case language
  - a few custom recognizers (case numbers, account numbers) common in legal docs
  - a high-recall threshold (we'd rather over-flag and let the human un-flag)

Detection works ONLY on normalized plaintext and emits character spans.
The format adapters are responsible for mapping those spans back to the
native document (docx run / pdf text instance).
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from functools import lru_cache
from typing import List

from presidio_analyzer import AnalyzerEngine, Pattern, PatternRecognizer
from presidio_analyzer.nlp_engine import NlpEngineProvider


# ---- Entity types we surface to the UI -------------------------------------
# Map Presidio's internal entity labels to friendly labels + a color for the UI.
ENTITY_META = {
    "PERSON":        {"label": "Name",            "color": "#a83254"},
    "US_SSN":        {"label": "Social Security #", "color": "#b4413c"},
    "EMAIL_ADDRESS": {"label": "Email",            "color": "#1f6f6f"},
    "PHONE_NUMBER":  {"label": "Phone",            "color": "#8a6d1f"},
    "CREDIT_CARD":   {"label": "Credit Card",      "color": "#6b3fa0"},
    "LOCATION":      {"label": "Address / Place",  "color": "#2f7a4f"},
    "DATE_TIME":     {"label": "Date",             "color": "#3f6ba0"},
    "US_BANK_NUMBER":{"label": "Account #",        "color": "#7a4f2f"},
    "CASE_NUMBER":   {"label": "Case / Docket #",  "color": "#4f2f7a"},
    "MEDICAL_ID":    {"label": "Medical Record #", "color": "#7a2f5f"},
}

# Short generic tags used by the "label" transform policy — substitute the
# detected value with a placeholder that names its type (e.g. "(ssn)") instead
# of redacting or replacing with a fake. Lowercased and parenthesized so the
# output reads as an annotation, not as real content.
ENTITY_TAG = {
    "PERSON":         "(name)",
    "US_SSN":         "(ssn)",
    "EMAIL_ADDRESS":  "(email)",
    "PHONE_NUMBER":   "(phone)",
    "CREDIT_CARD":    "(credit card)",
    "LOCATION":       "(address)",
    "DATE_TIME":      "(date)",
    "US_BANK_NUMBER": "(account)",
    "CASE_NUMBER":    "(case #)",
    "MEDICAL_ID":     "(medical id)",
}

# Which entities we actually want to detect. (Presidio supports many more.)
SUPPORTED_ENTITIES = list(ENTITY_META.keys())

# High-recall default: flag anything with at least this confidence.
# Lower = flag more aggressively. 0.25 lets ICD-10 fire without a medical
# context word — accepting some noise in exchange for recall on legal-intake
# docs where missing a diagnosis code matters more than a few extra flags.
SCORE_THRESHOLD = 0.25

# Legal-domain context words. Presidio boosts a candidate's score when one of
# these appears in the surrounding token window — so a borderline capitalized
# word next to "plaintiff" or "served on" gets promoted to a likely PERSON.
LEGAL_PERSON_CONTEXT = [
    "plaintiff", "defendant", "petitioner", "respondent", "client",
    "deponent", "witness", "counsel", "attorney", "esq", "atty",
    "served", "deposed", "represented", "v", "vs", "versus",
]


@dataclass
class Finding:
    type: str          # Presidio entity label, e.g. "PERSON"
    label: str         # friendly label for UI, e.g. "Name"
    color: str         # UI color
    start: int         # char offset in normalized text
    end: int
    text: str          # the matched substring
    score: float       # detection confidence 0..1
    cluster_id: int = -1   # assigned later by clustering

    def to_dict(self):
        return asdict(self)


def _build_custom_recognizers() -> List[PatternRecognizer]:
    """Recognizers for identifiers Presidio's built-ins miss or score too low."""
    # Presidio's built-in US_SSN recognizer scores the dashed NNN-NN-NNNN form
    # at exactly the default threshold (0.3), which gets filtered out in
    # practice. Add an explicit high-confidence recognizer for the common
    # written formats so they always surface.
    ssn_patterns = [
        Pattern(name="ssn_dashed", regex=r"\b\d{3}-\d{2}-\d{4}\b", score=0.75),
        Pattern(name="ssn_spaced", regex=r"\b\d{3} \d{2} \d{4}\b", score=0.6),
        Pattern(name="ssn_dotted", regex=r"\b\d{3}\.\d{2}\.\d{4}\b", score=0.5),
    ]
    ssn = PatternRecognizer(
        supported_entity="US_SSN",
        patterns=ssn_patterns,
        context=["ssn", "ss", "ssn#", "ss#", "social", "security", "tin"],
    )

    # Docket / case numbers. Legal docs use many formats:
    #   1:24-cv-08891           PACER federal docket
    #   Index No. 654321/2024   NY state index number
    #   CV-2024-0817            generic state criminal/civil (prefix-year-seq)
    #   24-CR-1234              short form (year-prefix-seq)
    case_patterns = [
        Pattern(name="federal_docket",
                regex=r"\b\d:\d{2}-[a-z]{2}-\d{3,6}\b", score=0.7),
        Pattern(name="index_no",
                regex=r"\bIndex\s+No\.?\s*\d{4,7}/\d{4}\b", score=0.7),
        Pattern(name="case_prefix_year",
                regex=r"\b[A-Z]{2,4}-\d{4}-\d{3,6}\b", score=0.6),
        Pattern(name="case_year_prefix",
                regex=r"\b\d{2,4}-[A-Z]{2,4}-\d{3,6}\b", score=0.6),
    ]
    case = PatternRecognizer(
        supported_entity="CASE_NUMBER",
        patterns=case_patterns,
        context=["case", "docket", "index", "no", "matter", "file"],
    )

    # Street addresses: house number + (1–4 title-cased words) + street suffix,
    # optionally followed by Apt/Suite/Unit/Bldg/#. spaCy's LOCATION model
    # catches city/state but reliably misses street lines, so we add a
    # high-confidence regex. Same supported_entity as spaCy so they share a
    # UI bucket; the overlap resolver will dedupe if both fire on one span.
    street_regex = (
        r"\b\d{1,6}\s+"
        r"(?:[A-Z][A-Za-z0-9.'\-]+\s+){1,4}"
        r"(?:Street|St|Avenue|Ave|Road|Rd|Lane|Ln|Drive|Dr|"
        r"Boulevard|Blvd|Court|Ct|Way|Place|Pl|Parkway|Pkwy|"
        r"Highway|Hwy|Circle|Cir|Terrace|Ter|Trail|Trl)\.?"
        r"(?:,?\s+(?:Apt|Suite|Ste|Unit|Bldg|#)\.?\s*[A-Za-z0-9\-]+)?"
        r"\b"
    )
    street = PatternRecognizer(
        supported_entity="LOCATION",
        patterns=[Pattern(name="street_address", regex=street_regex, score=0.75)],
        context=["address", "street", "residence", "resides", "located", "lives"],
    )

    # Identifier-level medical PII typical of legal-intake records.
    # Patterns are deliberately scored *below* the 0.30 threshold so they fire
    # only when a medical-context word ("MRN", "patient", "diagnosis", …) sits
    # nearby — otherwise a bare 6-digit number in any doc would over-flag.
    med_patterns = [
        # ICD-10 diagnosis code: letter (excluding U, reserved) + 2 digits,
        # optional decimal subcode of 1–4 digits.
        Pattern(name="icd10",
                regex=r"\b[A-TV-Z]\d{2}(?:\.\d{1,4})?\b", score=0.25),
        # MRN / chart number: a 6–10 digit run. Context word required.
        Pattern(name="mrn_digits",
                regex=r"\b\d{6,10}\b", score=0.10),
        # CPT procedure code: exactly 5 digits with strong medical context.
        Pattern(name="cpt",
                regex=r"\b\d{5}\b", score=0.05),
    ]
    med = PatternRecognizer(
        supported_entity="MEDICAL_ID",
        patterns=med_patterns,
        context=["mrn", "medical", "record", "patient", "chart", "diagnosis",
                 "dx", "icd", "npi", "cpt", "procedure", "provider"],
    )

    return [ssn, case, street, med]


@lru_cache(maxsize=1)
def get_analyzer() -> AnalyzerEngine:
    """
    Build the analyzer once and cache it. Loading the spaCy model is the
    expensive part (~hundreds of MB into memory), so we never want to do it
    per-request.
    """
    nlp_config = {
        "nlp_engine_name": "spacy",
        "models": [{"lang_code": "en", "model_name": "en_core_web_lg"}],
    }
    nlp_engine = NlpEngineProvider(nlp_configuration=nlp_config).create_engine()

    analyzer = AnalyzerEngine(nlp_engine=nlp_engine, supported_languages=["en"])

    for rec in _build_custom_recognizers():
        analyzer.registry.add_recognizer(rec)

    return analyzer


def detect(text: str) -> List[Finding]:
    """Run detection on normalized plaintext and return resolved findings."""
    if not text.strip():
        return []

    analyzer = get_analyzer()
    results = analyzer.analyze(
        text=text,
        language="en",
        entities=SUPPORTED_ENTITIES,
        score_threshold=SCORE_THRESHOLD,
        # Boost PERSON candidates near legal context words.
        context=LEGAL_PERSON_CONTEXT,
    )

    findings: List[Finding] = []
    for r in results:
        meta = ENTITY_META.get(r.entity_type)
        if not meta:
            continue
        findings.append(
            Finding(
                type=r.entity_type,
                label=meta["label"],
                color=meta["color"],
                start=r.start,
                end=r.end,
                text=text[r.start:r.end],
                score=round(r.score, 3),
            )
        )

    return _resolve_overlaps(findings)


def _resolve_overlaps(findings: List[Finding]) -> List[Finding]:
    """When two findings overlap, keep the longer (then higher-scoring) one."""
    findings.sort(key=lambda f: (f.start, -(f.end - f.start), -f.score))
    resolved: List[Finding] = []
    last_end = -1
    for f in findings:
        if f.start >= last_end:
            resolved.append(f)
            last_end = f.end
    return resolved
