"""
What the web interface shows of a job's output files. Everything here
is read from the files the pipeline wrote; nothing is computed again.

    document_view(document)                 an extracted document, or its
                                            tagged or sanitized copy, as
                                            pages of blocks
    findings_view(report, exposure)         the PII report's findings,
                                            with each one's location and
                                            what was done with it
    validation_view(report, sanitized, validation, exposure, review)
                                            the verdict, every check and
                                            issue, how far the review has
                                            got, the extraction warnings
                                            and the redaction's errors
"""

from src.pii.review import MANUAL_DETECTOR, can_hide
from src.pii.validator import issue_key, review_approval, unreadable_issue

PAGE_LABELS = {"PDF": "Page", "PPTX": "Slide"}

# Block metadata the page shows beside a block's text
BLOCK_FIELDS = ("location", "source", "ocr_confidence", "level")


def page_label(source_type, page_number):
    label = PAGE_LABELS.get(source_type)

    if page_number is None:
        return "File properties"

    return f"{label} {page_number}" if label else "Document"


def block_view(block):
    """
    One block, without its OCR words (they repeat the text). A table
    holds its cells; a comment holds its author.
    """

    metadata = block.get("metadata", {})

    view = {
        "block_id": block["block_id"],
        "block_type": block["block_type"],
        "text": block.get("text") or "",
        **{field: metadata[field] for field in BLOCK_FIELDS if field in metadata},
    }

    if block["block_type"] == "table":
        view["rows"] = metadata.get("rows")
        view["columns"] = metadata.get("columns")
        view["cells"] = [
            {
                "block_id": cell["block_id"],
                "text": cell.get("text") or "",
                "row": cell["metadata"].get("row"),
                "column": cell["metadata"].get("column"),
                "row_span": cell["metadata"].get("row_span", 1),
                "column_span": cell["metadata"].get("column_span", 1),
            }
            for cell in block.get("children", [])
            if cell.get("block_type") == "table_cell"
        ]

    if block["block_type"] == "image":
        view["note"] = "Image that could not be read: its text was never scanned"

    # The scanner reports a comment's author under <block_id>_author
    if metadata.get("author"):
        view["author"] = {"block_id": f"{block['block_id']}_author", "text": str(metadata["author"])}

    return view


def document_view(document):
    """
    A document as the page shows it: its pages in order, each with its
    blocks in reading order, and its file properties (they can name
    people: the scanner reports them as property_<name>).
    """

    source_type = document.get("source_type")
    metadata = document.get("metadata", {})

    return {
        "document_id": document.get("document_id"),
        "file_name": document.get("file_name"),
        "source_type": source_type,
        "extraction_method": document.get("extraction_method"),
        "variant": metadata.get("redaction", {}).get("variant", "original"),
        "pages": [
            {
                "page_number": page["page_number"],
                "label": page_label(source_type, page["page_number"]),
                "hidden": bool(page.get("metadata", {}).get("hidden")),
                "low_text": bool(page.get("metadata", {}).get("low_text")),
                "blocks": [block_view(block) for block in page["blocks"]],
            }
            for page in document.get("pages", [])
        ],
        "properties": [
            {"block_id": f"property_{name}", "name": name, "text": str(value)}
            for name, value in metadata.get("properties", {}).items()
        ],
        "limitations": metadata.get("limitations", []),
    }


def fallback_location(item, source_type):
    """
    A finding's location when there is no exposure report to take it
    from: its page, and its table cell.
    """

    location = page_label(source_type, item.get("page_number"))

    if item.get("table_id"):
        location += f", table row {item.get('row')}, column {item.get('column')}"

    return location


def findings_view(report, exposure=None):
    """
    The findings, in report order, each with its value: the page runs on
    this computer and shows the value to the person reviewing it.
    Location, detector, action and validation come from the exposure
    report where there is one.
    """

    source_type = report.get("source_type")
    rows = {row["finding_id"]: row for row in (exposure or {}).get("rows", [])}
    findings = []

    for item in report.get("findings", []):
        row = rows.get(item.get("finding_id"), {})

        findings.append({
            "finding_id": item.get("finding_id"),
            "type": item.get("type"),
            "value": item.get("text"),
            "confidence": item.get("confidence"),
            "page_number": item.get("page_number"),
            "page_label": page_label(source_type, item.get("page_number")),
            "location": row.get("location") or fallback_location(item, source_type),
            "block_id": item.get("block_id"),
            "block_type": item.get("block_type"),
            "start": item.get("start"),
            "end": item.get("end"),
            "detector": row.get("detector") or item.get("detector"),
            "rule_id": item.get("rule_id"),
            "context_trigger": item.get("context_trigger"),
            "review_status": item.get("review_status"),
            "action": row.get("action"),
            "validation": row.get("validation"),
            "validation_issues": row.get("validation_issues", []),
            "ocr_confidence": (item.get("image") or {}).get("ocr_confidence"),
        })

    return {
        "status": report.get("status"),
        "error": report.get("error"),
        "summary": report.get("summary"),
        "findings": findings,
    }


def reviewed_validation(validation, review):
    """
    The validation report with each issue's key (what a review answer
    names), whether "Hide it" can fix it, and its sign-off, if any; and
    the status of the document and of each check once the sign-offs are
    counted (final_verdict, final_status: see review_approval).
    """

    approval = review_approval(validation, review)

    def issue_view(check_name, item):
        key = issue_key(check_name, item)
        signed = approval["signed"].get(key)

        return {
            **item,
            "key": key,
            "can_hide": can_hide(check_name, item),
            # Badly read OCR text: the page answers these all at once
            "unreadable": unreadable_issue(check_name, item),
            "signed_off": {"reviewer": signed.get("reviewer"), "signed_at": signed.get("signed_at")} if signed else None,
        }

    return {
        **validation,
        "final_verdict": approval["final_verdict"],
        "checks": [
            {
                **check,
                "final_status": approval["final_checks"].get(check["name"], check.get("status")),
                "issues": [issue_view(check["name"], item) for item in check.get("issues", [])],
            }
            for check in validation.get("checks", [])
        ],
    }


def review_view(report, validation, review):
    """
    How far the review has got: the blocks a reviewer hid text in (their
    manual findings by block: IDs, types, who and when, never the text),
    the issues checked and still open, and how many OCR issues "Hide all"
    would hide.
    """

    hidden = {}

    for item in report.get("findings", []):
        if item.get("detector") != MANUAL_DETECTOR:
            continue

        added = item.get("review") or {}
        entry = hidden.setdefault(item.get("block_id"), {
            "block_id": item.get("block_id"),
            "page_number": item.get("page_number"),
            "types": [],
            "findings": 0,
            "added_by": added.get("added_by"),
            "added_at": added.get("added_at"),
        })
        entry["findings"] += 1

        if item.get("type") not in entry["types"]:
            entry["types"].append(item.get("type"))

        # The latest answer that hid text here
        if (added.get("added_at") or "") > (entry["added_at"] or ""):
            entry["added_by"], entry["added_at"] = added.get("added_by"), added.get("added_at")

    approval = review_approval(validation, review) if validation else None

    return {
        "hidden": list(hidden.values()),
        "checked": len(approval["signed"]) if approval else 0,
        "open": approval["open"] if approval else 0,
        "needing_review": approval["total"] if approval else 0,
        "ocr_hideable": sum(
            1
            for check in (validation or {}).get("checks", [])
            for item in check.get("issues", [])
            if unreadable_issue(check["name"], item) and issue_key(check["name"], item) not in approval["signed"]
        ),
    }


def validation_view(report, sanitized=None, validation=None, exposure=None, review=None):
    """
    Everything the page shows about whether the sanitized copy is safe:
    the verdict and each check with its issues (and their sign-offs), how
    far the review has got, the scan's extraction warnings, the
    redaction's errors and warnings, and the exposure summary. None of it
    holds a PII value.
    """

    progress = review_view(report, validation, review)

    if validation:
        validation = reviewed_validation(validation, review)

    redaction = (sanitized or {}).get("metadata", {}).get("redaction") or {}

    return {
        "report_status": report.get("status"),
        "report_error": report.get("error"),
        "review": progress,
        "extraction_warnings": (report.get("summary") or {}).get("extraction_warnings", []),
        "redaction": {
            "status": redaction.get("status"),
            "summary": redaction.get("summary"),
            "errors": redaction.get("errors", []),
            "warnings": redaction.get("warnings", []),
        } if redaction else None,
        "validation": validation,
        "exposure": {
            "summary": exposure.get("summary"),
            "coverage_gaps": exposure.get("coverage_gaps"),
            "validation": exposure.get("validation"),
        } if exposure else None,
    }
