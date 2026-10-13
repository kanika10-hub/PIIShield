"""
Redact the PII found by document_scanner from an extracted document
(the common JSON format).

Two copies of the document are written, next to it:

    <name>_tagged.json      each value labelled in place:
                            "Contact [PERSON_NAME: Jane Doe]"
                            For review and demonstration only: it still
                            holds the PII.
    <name>_sanitized.json   each value replaced by a redaction tag:
                            "Contact [REDACTED: PERSON_NAME]"
                            The only copy that may go downstream (to an
                            LLM), once validator.py has rescanned it.

Values are replaced at the offsets (start, end) the scanner reported in
the block text. A finding whose offsets do not point at its value (a
stale report, an edited document) is reported in errors and left alone,
never guessed at. A finding a reviewer marked "false_positive" is kept.

Findings that overlap (a PHONE inside a longer PHONE, a name inside an
email) become one span, tagged with the type of the longest finding, so
no part of either value is left behind.

The sanitized copy also hides the other places a block's text repeats:

    word children           the OCR words of a PDF paragraph or table cell
    metadata.words          the OCR words of an image
    column_headers          the header text a continued PDF table copies
                            from the table it continues

A word that touches a redacted span gets the span's tag as its text and
keeps its bounding box, so the original file can be redacted later.

Both copies get metadata.redaction:

    variant                 "tagged" or "sanitized"
    redactor_version, redaction_timestamp,
    report_scan_timestamp, report_status
                            which report was applied, and when
    status                  "completed", "completed_with_warnings"
                            (warnings is not empty) or "needs_review"
                            (errors is not empty: PII may remain)
    summary                 findings, redacted, skipped, failed,
                            spans_replaced, by_type
    errors, warnings        block_id, finding_id where there is one,
                            reason. They never include the PII itself.
    audit                   one record per finding, in report order:
                            finding_id, block_id, type, status
                            ("redacted", "skipped" or "failed"), and
                            replacement or reason

    python -m src.pii.redactor output/risk_policy.json [output/risk_policy_pii.json]
"""

import copy
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from src.pii.document_scanner import findings_path, word_lists


# Change whenever the redaction rules change
REDACTOR_VERSION = "1.0.0"

REQUIRED_FIELDS = ("finding_id", "block_id", "type", "text", "start", "end")

# Text of a word whose place in the block text is unknown (see redact_words)
UNPLACED_WORD = "[REDACTED]"

# Every tag the sanitized copy can hold: redaction_tag() and UNPLACED_WORD
REDACTION_TAG = re.compile(r"\[REDACTED(?:: [A-Z0-9_]+)?\]")


def tag(pii_type, value):
    return f"[{pii_type}: {value}]"


def redaction_tag(pii_type):
    return f"[REDACTED: {pii_type}]"


# --------------------------------------------------
# WHERE FINDINGS POINT
# --------------------------------------------------

def walk(blocks):
    for block in blocks:
        yield block
        yield from walk(block.get("children", []))


def text_slots(document):
    """
    Every text a finding can point to, by the finding's block_id, as
    (holder, key): the text is holder[key].

    A block holds its text in "text". The scanner also reports file
    properties ("property_author") and comment authors
    ("<block_id>_author"), which are held in metadata (see
    document_scanner.property_block).
    """

    slots = {}

    def add(block_id, holder, key):
        if block_id in slots:
            raise ValueError(f"block_id {block_id} is not unique in the document")

        slots[block_id] = (holder, key)

    for page in document["pages"]:
        for block in walk(page["blocks"]):
            add(block["block_id"], block, "text")

            metadata = block.get("metadata", {})

            if isinstance(metadata.get("author"), str):
                add(f"{block['block_id']}_author", metadata, "author")

    properties = document.get("metadata", {}).get("properties", {})

    for name, value in properties.items():
        if isinstance(value, str):
            add(f"property_{name}", properties, name)

    return slots


def check_report(document, report):
    """
    Refuse a report that cannot be applied to this document.
    """

    if report.get("status") == "failed":
        raise ValueError(f"The PII scan failed ({report.get('error')}): nothing to redact from")

    for field in ("document_id", "file_name"):
        if report.get(field) != document.get(field):
            raise ValueError(
                f"The PII report is for {field} {report.get(field)!r}, "
                f"not {document.get(field)!r}"
            )

    if not isinstance(report.get("findings"), list):
        raise ValueError("The PII report has no findings list")


def check_finding(item, slots):
    """
    Why a finding cannot be redacted, or None if its offsets point at
    exactly its value. The reason never includes the value.
    """

    missing = [field for field in REQUIRED_FIELDS if item.get(field) is None]

    if missing:
        return f"missing {', '.join(missing)}"

    if item["block_id"] not in slots:
        return "block not found in the document"

    holder, key = slots[item["block_id"]]
    text = holder.get(key) or ""
    start, end = item["start"], item["end"]

    if not (isinstance(start, int) and isinstance(end, int) and 0 <= start < end <= len(text)):
        return f"offsets {start}-{end} are outside the block text"

    if text[start:end] != item["text"]:
        return f"offsets {start}-{end} do not point at the reported value"

    return None


# --------------------------------------------------
# SPANS AND WORDS
# --------------------------------------------------

def merge_spans(items):
    """
    The spans to replace in one text: findings that overlap become one
    span covering all of them. In text order, as dicts with start, end,
    type (that of the longest finding) and items (the findings).
    """

    spans = []

    for item in sorted(items, key=lambda x: (x["start"], x["end"])):

        if spans and item["start"] < spans[-1]["end"]:
            spans[-1]["end"] = max(spans[-1]["end"], item["end"])
            spans[-1]["items"].append(item)

        else:
            spans.append({"start": item["start"], "end": item["end"], "items": [item]})

    for span in spans:
        longest = max(
            span["items"],
            key=lambda x: (x["end"] - x["start"], x.get("confidence") or 0)
        )
        span["type"] = longest["type"]

    return spans


def replace_spans(text, spans, replacement):
    """
    text with each span replaced by replacement(span, value).
    spans are in text order and do not overlap.
    """

    parts = []
    position = 0

    for span in spans:
        parts.append(text[position:span["start"]])
        parts.append(replacement(span, text[span["start"]:span["end"]]))
        position = span["end"]

    parts.append(text[position:])

    return "".join(parts)


def redact_words(words, text, spans):
    """
    Hide the words that touch a redacted span. The extractors write one
    word per whitespace-separated token of the block text, in order, so
    token offsets place each word; a word touching a span gets the span's
    tag (both tags if it touches two).

    Returns False if the words do not match the text: the spans cannot
    be placed, so every word is hidden.
    """

    tokens = list(re.finditer(r"\S+", text))

    if [token.group() for token in tokens] != [word.get("text") for word in words]:
        for word in words:
            word["text"] = UNPLACED_WORD

        return False

    for token, word in zip(tokens, words):
        tags = [
            redaction_tag(span["type"])
            for span in spans
            if span["start"] < token.end() and token.start() < span["end"]
        ]

        if tags:
            word["text"] = "".join(tags)

    return True


# --------------------------------------------------
# CONTINUED TABLES
# --------------------------------------------------

def tables_by_id(document):
    """
    The document's tables in reading order. Tables are page blocks.
    """

    return {
        block["block_id"]: block
        for page in document["pages"]
        for block in page["blocks"]
        if block["block_type"] == "table"
    }


def inherited_headers(tables, table_id):
    """
    The column headers a table continuing table_id copies from it,
    built as pdf_tables.column_headers_of builds them: table_id's own
    inherited headers, or the text of its first row.
    """

    source = tables[table_id]

    if "column_headers" in source["metadata"]:
        return dict(source["metadata"]["column_headers"])

    return {
        str(cell["metadata"]["column"]): " ".join(cell["text"].split())
        for cell in source["children"]
        if cell["metadata"]["row"] == 1
    }


def redact_inherited_headers(original, sanitized, errors):
    """
    A PDF table continued from the previous page holds a copy of the
    previous table's header text (metadata.column_headers). Where
    redaction changed that header, rebuild the copy from the sanitized
    table. Tables are visited in reading order, so a chain of continued
    tables is rebuilt from its start.
    """

    original_tables = tables_by_id(original)
    sanitized_tables = tables_by_id(sanitized)

    for table_id, table in sanitized_tables.items():
        metadata = table["metadata"]

        if "column_headers" not in metadata:
            continue

        source_id = metadata.get("continues_table")

        if source_id not in sanitized_tables:
            errors.append({
                "block_id": table_id,
                "reason": f"column headers copied from table {source_id!r}, which is not in the document"
            })
            continue

        before = inherited_headers(original_tables, source_id)
        after = inherited_headers(sanitized_tables, source_id)

        if after == before:
            continue

        # The copy must be what the source table gives, or rebuilding it
        # could drop text the redaction never saw
        if metadata["column_headers"] == before:
            metadata["column_headers"] = after

        else:
            errors.append({
                "block_id": table_id,
                "reason": f"column headers copied from {source_id} contain redacted "
                          "text but do not match that table, so they cannot be rebuilt"
            })


# --------------------------------------------------
# REDACTION
# --------------------------------------------------

def audit_entry(item, status, **details):
    return {
        "finding_id": item.get("finding_id"),
        "block_id": item.get("block_id"),
        "type": item.get("type"),
        "status": status,
        **details,
    }


def findings_by_block(findings, slots):
    """
    Sort a report's findings into those to redact, by block_id, and
    those that are not: a reviewed false positive is skipped, and one
    whose offsets do not point at its value has failed. The redacted
    file in its original format (src/original_format) is made from the
    same choice.

    Returns (by_block, outcomes, errors): outcomes maps id(finding) to
    the audit record of each finding not redacted, errors lists the
    failed ones.
    """

    by_block = {}
    outcomes = {}
    errors = []

    for item in findings:

        if item.get("review_status") == "false_positive":
            outcomes[id(item)] = audit_entry(item, "skipped", reason="reviewed as a false positive")
            continue

        problem = check_finding(item, slots)

        if problem:
            outcomes[id(item)] = audit_entry(item, "failed", reason=problem)
            errors.append({
                "finding_id": item.get("finding_id"),
                "block_id": item.get("block_id"),
                "reason": problem
            })
            continue

        by_block.setdefault(item["block_id"], []).append(item)

    return by_block, outcomes, errors


def redact_document(document, report):
    """
    Apply a PII report (document_scanner.scan_document) to the document
    it was made from. The document is not changed.

    document is the extractor's JSON: a dictionary, or the
    ExtractedDocument an extraction pipeline still holds in memory.

    Returns {"tagged_document", "sanitized_document", "redaction"}:
    the two copies, and the metadata.redaction record both carry
    (see the module docstring).
    """

    if hasattr(document, "to_dict"):
        document = document.to_dict()

    check_report(document, report)

    tagged = copy.deepcopy(document)
    sanitized = copy.deepcopy(document)

    slots = text_slots(document)
    tagged_slots = text_slots(tagged)
    sanitized_slots = text_slots(sanitized)

    findings = report["findings"]
    by_block, outcomes, errors = findings_by_block(findings, slots)
    warnings = []

    def record(item, status, **details):
        outcomes[id(item)] = audit_entry(item, status, **details)

    spans_replaced = 0

    for block_id, items in by_block.items():
        spans = merge_spans(items)
        holder, key = slots[block_id]
        text = holder[key]

        tagged_holder, _ = tagged_slots[block_id]
        tagged_holder[key] = replace_spans(
            text, spans, lambda span, value: tag(span["type"], value)
        )

        sanitized_holder, _ = sanitized_slots[block_id]
        sanitized_holder[key] = replace_spans(
            text, spans, lambda span, value: redaction_tag(span["type"])
        )

        # Only a block's own text has word lists; properties do not
        if key == "text":
            for words in word_lists(sanitized_holder):
                if not redact_words(words, text, spans):
                    warnings.append({
                        "block_id": block_id,
                        "reason": f"its {len(words)} words do not match its text, "
                                  "so all of them were hidden"
                    })

        for span in spans:
            for item in span["items"]:
                record(item, "redacted", replacement=redaction_tag(span["type"]))

        spans_replaced += len(spans)

    redact_inherited_headers(document, sanitized, errors)

    audit = [outcomes[id(item)] for item in findings]
    statuses = Counter(entry["status"] for entry in audit)

    if errors:
        status = "needs_review"
    elif warnings:
        status = "completed_with_warnings"
    else:
        status = "completed"

    redaction = {
        "redactor_version": REDACTOR_VERSION,
        "redaction_timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "report_scan_timestamp": report.get("scan_timestamp"),
        "report_status": report.get("status"),
        "status": status,
        "summary": {
            "findings": len(findings),
            "redacted": statuses["redacted"],
            "skipped": statuses["skipped"],
            "failed": statuses["failed"],
            "spans_replaced": spans_replaced,
            "by_type": dict(Counter(
                entry["type"] for entry in audit if entry["status"] == "redacted"
            )),
        },
        "errors": errors,
        "warnings": warnings,
        "audit": audit,
    }

    tagged.setdefault("metadata", {})["redaction"] = {"variant": "tagged", **redaction}
    sanitized.setdefault("metadata", {})["redaction"] = {"variant": "sanitized", **redaction}

    return {
        "tagged_document": tagged,
        "sanitized_document": sanitized,
        "redaction": redaction,
    }


# --------------------------------------------------
# FILES
# --------------------------------------------------

def output_paths(json_path):
    """
    Where the two copies of an extracted JSON file go: next to it, as
    <name>_tagged.json and <name>_sanitized.json.
    """

    json_path = Path(json_path)

    return (
        json_path.with_name(f"{json_path.stem}_tagged.json"),
        json_path.with_name(f"{json_path.stem}_sanitized.json"),
    )


def save_json(data, output_path):

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", encoding="utf-8") as file:
        json.dump(data, file, indent=2, ensure_ascii=False)


def redact_file(json_path, report_path=None):
    """
    Redact an extracted JSON file with its PII report (by default
    <name>_pii.json next to it) and save both copies.
    """

    report_path = report_path or findings_path(json_path)

    with open(json_path, "r", encoding="utf-8") as file:
        document = json.load(file)

    with open(report_path, "r", encoding="utf-8") as file:
        report = json.load(file)

    result = redact_document(document, report)
    tagged_path, sanitized_path = output_paths(json_path)

    save_json(result["tagged_document"], tagged_path)
    save_json(result["sanitized_document"], sanitized_path)

    return result, tagged_path, sanitized_path


def main():

    if len(sys.argv) > 1:
        json_path = Path(sys.argv[1])
    else:
        json_path = Path(input("Extracted JSON file: ").strip().strip('"'))

    report_path = Path(sys.argv[2]) if len(sys.argv) > 2 else findings_path(json_path)

    for path in (json_path, report_path):
        if not path.is_file():
            sys.exit(f"File not found: {path}")

    result, tagged_path, sanitized_path = redact_file(json_path, report_path)
    redaction = result["redaction"]
    summary = redaction["summary"]

    print(f"Tagged (contains PII): {tagged_path}")
    print(f"Sanitized:             {sanitized_path}")
    print(f"\nStatus: {redaction['status']}")
    print(
        f"Findings: {summary['findings']}  redacted: {summary['redacted']}  "
        f"skipped: {summary['skipped']}  failed: {summary['failed']}  "
        f"spans replaced: {summary['spans_replaced']}"
    )
    print(f"By type: {summary['by_type']}")

    for error in redaction["errors"]:
        print(f"  Error: {error.get('finding_id') or ''} {error['block_id']}: {error['reason']}")

    for warning in redaction["warnings"]:
        print(f"  Warning: {warning['block_id']}: {warning['reason']}")


if __name__ == "__main__":
    main()
