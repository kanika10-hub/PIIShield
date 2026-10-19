"""
What to redact in the original file: the same findings, merged spans
and redaction tags as the sanitized JSON copy (src/pii/redactor.py), so
the file and the copy say the same thing.

    make_plan(document, report)     -> Plan

Besides the findings at their offsets, the file is searched for every
value the scan found (ValueMatcher), wherever it is: a name repeated in
a slide master, alt text, a hyperlink, custom XML. A one-word name
("Ford") is a word as well as a name, so it is only removed where the
scan reported it, as the validator only asks for it to be reviewed.
Values a reviewer marked as false positives are never removed by these
searches (Plan.protected).
"""

import re
from dataclasses import dataclass

from src.pii.redactor import (
    check_report, findings_by_block, merge_spans, redaction_tag, replace_spans, text_slots, walk
)
from src.pii.validator import is_false_positive, normalise, value_pattern


def tag_of(span):
    return redaction_tag(span["type"])


def span_triples(spans):
    """
    (start, end, tag) for each merged span.
    """

    return [(span["start"], span["end"], tag_of(span)) for span in spans]


class ValueMatcher:
    """
    Finds known values in any text: ignoring case, with any whitespace
    between their words, and not as part of a longer word.
    """

    def __init__(self, values):
        """
        values: value -> the tag to put in its place.
        """

        self.tags = {}

        for value, tag in values.items():
            self.tags.setdefault(normalise(value).casefold(), tag)

        self.regex = re.compile(
            "|".join(f"(?:{value_pattern(value)})" for value in sorted(values, key=len, reverse=True)),
            re.IGNORECASE
        ) if values else None

    def __bool__(self):
        return self.regex is not None

    def spans(self, text):
        """
        (start, end, tag) of every value in text, in order.
        """

        if self.regex is None or not text:
            return []

        return [
            (match.start(), match.end(), self.tags.get(normalise(match.group()).casefold(), "[REDACTED]"))
            for match in self.regex.finditer(text)
        ]


@dataclass
class Plan:
    document: dict
    report: dict
    spans: dict             # block_id -> merged spans of its text (start, end, type, items)
    blocks: dict            # block_id -> (page_number, block), every block of the document
    replaced_slots: dict    # file property / comment author slot -> its sanitized text
    outcomes: dict          # id(finding) -> audit record, for findings not redacted
    errors: list            # findings whose offsets do not point at their value
    values: ValueMatcher    # every value to remove wherever it is
    protected: set          # values reviewed as false positives, normalised and casefolded

    def is_protected(self, value):
        return normalise(value).casefold() in self.protected


def sweep_values(findings):
    """
    value -> tag for every value to search the whole file for.
    """

    values = {}

    for item in findings:
        value = normalise(item.get("text") or "")

        if is_false_positive(item) or not value:
            continue

        if item.get("type") == "PERSON_NAME" and len(value.split()) < 2:
            continue

        values.setdefault(value, redaction_tag(item["type"]))

    return values


def make_plan(document, report):
    """
    The plan for redacting the file a document was extracted from, with
    its PII report. Raises ValueError for a report that does not belong
    to the document, or whose scan failed.
    """

    check_report(document, report)

    findings = report["findings"]
    slots = text_slots(document)
    by_block, outcomes, errors = findings_by_block(findings, slots)
    spans = {block_id: merge_spans(items) for block_id, items in by_block.items()}

    # File properties and comment authors are not block text
    replaced_slots = {}

    for block_id, block_spans in spans.items():
        holder, key = slots[block_id]

        if key != "text":
            replaced_slots[block_id] = replace_spans(
                holder[key], block_spans, lambda span, value: tag_of(span)
            )

    return Plan(
        document=document,
        report=report,
        spans=spans,
        blocks={
            block["block_id"]: (page["page_number"], block)
            for page in document["pages"]
            for block in walk(page["blocks"])
        },
        replaced_slots=replaced_slots,
        outcomes=outcomes,
        errors=errors,
        values=ValueMatcher(sweep_values(findings)),
        protected={
            normalise(item.get("text") or "").casefold()
            for item in findings
            if is_false_positive(item) and normalise(item.get("text") or "")
        },
    )
