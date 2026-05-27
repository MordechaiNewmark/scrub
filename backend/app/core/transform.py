"""
Clustering + transformation.

clustering: group findings that refer to the same entity so substitution is
            stable ("James Lin" -> always the same fake name everywhere).
            v1 strategy: case-insensitive exact match within an entity type,
            plus a simple alias rule for PERSON (last-name matches a known
            full name). Good enough for real docs; full coreference is a
            later enhancement.

transform:  per cluster, apply a policy (keep / redact / fake). Fake values are
            generated with a Faker instance seeded by the cluster id, so the
            same entity yields the same fake value across the whole document
            AND across runs, and fakes are format-preserving per type.
"""
from __future__ import annotations

from typing import Dict, List

from faker import Faker

from .detect import Finding, ENTITY_TAG


def cluster(findings: List[Finding]) -> List[Finding]:
    """Assign cluster_id to each finding. Mutates and returns the list."""
    key_to_id: Dict[str, int] = {}
    # Track PERSON full names so "Lin" can attach to "James Lin".
    person_lastname_to_id: Dict[str, int] = {}
    next_id = 0

    # Process longer PERSON mentions first so full names anchor the cluster.
    order = sorted(findings, key=lambda f: -(f.end - f.start))

    for f in order:
        norm = f.text.lower().strip()

        if f.type == "PERSON":
            parts = norm.split()
            last = parts[-1] if parts else norm
            if norm in key_to_id:
                f.cluster_id = key_to_id[norm]
            elif last in person_lastname_to_id and len(parts) == 1:
                # bare last name -> attach to the full-name cluster
                f.cluster_id = person_lastname_to_id[last]
            else:
                f.cluster_id = next_id
                key_to_id[norm] = next_id
                if len(parts) >= 2:
                    person_lastname_to_id[last] = next_id
                next_id += 1
        else:
            key = f"{f.type}::{norm}"
            if key not in key_to_id:
                key_to_id[key] = next_id
                next_id += 1
            f.cluster_id = key_to_id[key]

    return findings


def _seeded_faker(cluster_id: int) -> Faker:
    fake = Faker("en_US")
    fake.seed_instance(cluster_id * 7919 + 17)  # stable per cluster
    return fake


def fake_value(entity_type: str, original: str, cluster_id: int) -> str:
    """Generate a format-preserving fake value for an entity."""
    fake = _seeded_faker(cluster_id)

    if entity_type == "PERSON":
        # Preserve a leading title if present (Mr./Dr./Judge/Atty.).
        title = ""
        first_word = original.split()[0] if original.split() else ""
        if first_word.rstrip(".").lower() in {"mr", "mrs", "ms", "dr", "hon", "judge", "atty"}:
            title = first_word + " "
        return f"{title}{fake.name()}"

    if entity_type == "US_SSN":
        return fake.ssn()
    if entity_type == "EMAIL_ADDRESS":
        return fake.email()
    if entity_type == "PHONE_NUMBER":
        return fake.numerify("(###) ###-####")
    if entity_type == "CREDIT_CARD":
        return fake.credit_card_number()
    if entity_type == "US_BANK_NUMBER":
        return fake.numerify("##########")
    if entity_type == "LOCATION":
        return fake.street_address()
    if entity_type == "DATE_TIME":
        return fake.date(pattern="%b %d, %Y")
    if entity_type == "CASE_NUMBER":
        return fake.numerify("#:##-cv-#####")
    if entity_type == "MEDICAL_ID":
        # Preserve shape for ICD-10 (e.g. "M54.5"); otherwise emit a digit run
        # in the same length bucket so MRNs/CPTs round-trip as plausible IDs.
        if "." in original and original[:1].isalpha():
            return fake.bothify("?##.#", letters="ABCDEFGHIJKLMNOPQRSTVWXYZ")
        return fake.numerify("#" * max(5, len(original)))

    return "[REDACTED]"


def replacement_for(f: Finding, policy: str) -> str:
    """The string this finding should become under the given policy."""
    if policy == "keep":
        return f.text
    if policy == "redact":
        return "[REDACTED]"
    if policy == "fake":
        return fake_value(f.type, f.text, f.cluster_id)
    if policy == "label":
        return ENTITY_TAG.get(f.type, "(redacted)")
    return f.text
