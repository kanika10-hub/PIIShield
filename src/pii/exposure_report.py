"""
Report the PII found in a document, where it was, how it was found and
what was done with it, without repeating any of the values.

    python -m src.pii.exposure_report output/risk_policy.json [output/risk_policy_pii.json]

reads the extracted document <name>.json, its PII report <name>_pii.json,
<name>_sanitized.json (the redaction audit), <name>_validation.json and,
if a reviewer answered issues on the web page, <name>_review.json, and
writes <name>_exposure.json. It then rebuilds the combined report
over every exposure report in backend/output and its subfolders (for a
document outside backend/output: in its own folder and subfolders):

    exposure_report.csv     one row per finding, every document
    exposure_report.md      the exposure summary of each document, then
                            the same rows as tables

A row, one per finding in the PII report:

    Document                file name
    Location                "Page 13, table row 3, column 2",
                            "Slide 2", "Header, image text (OCR)",
                            "File properties, author"
    PII Type                PERSON_NAME, EMAIL, ...
    Detector                NER (spaCy), Regex, Rule (labels, table
                            headers, surname mentions), or Manual review
                            (a word a reviewer hid, see review.py)
    Confidence              the rule's confidence: a ranking, not a
                            probability (see detectors.finding)
    Action                  "Redacted", or why it was not
    Validation              PASS, or the worst validator issue that
                            names this finding (a value still in the
                            sanitized copy, an OCR word left showing).
                            The validator keeps MAX_ISSUES per check:
                            past that, compare residual_exposure_total.
                            An issue a reviewer signed off is described
                            with "[checked by <reviewer>]"

and, for traceability, the finding_id (the value is in the PII report
under it), block_id, page, table cell, character offsets and rule_id.

The exposure report never holds a PII value. Fields that can hold one
are left out of the rows: text, context (a row label can be a name),
context_trigger (a surname mention names its people) and
matched_pattern. Before anything is written, every string in the report
is searched for every value the PII report found; a match refuses the
report rather than write it. Unreadable OCR text a reviewer hid
(UNREADABLE_TEXT) is a misreading, not a value, and is not searched for.

The report is refused if its inputs are not one run: the PII report,
the sanitized copy and the validation report must be for this document
and the same PII scan.

<name>_exposure.json:

    exposure_version, generated_at, document_id, file_name, source_type
    sources         the versions and timestamps of the scan, redaction
                    and validation the report was built from
    summary         total_findings, redacted, not_redacted,
                    flagged_by_validation, by_type (found and redacted),
                    by_detector, by_action, by_place (page, slide or part)
    validation      verdict, may_send_to_llm (what approved_copy decides:
                    PASS, or NEEDS_REVIEW with every issue signed off),
                    approved_after_review, final_verdict (PASS once the
                    copy may go, otherwise the verdict), each check's
                    status, and residual_exposure: every validator issue,
                    with its check
    review          manual_findings (finding_id, type, added_by, added_at)
                    and sign_offs (the issue's check, finding_id, block_id
                    and reason, the reviewer and the time)
    coverage_gaps   the scan's extraction warnings: places PII could hide
                    from it
    rows            as above
"""

import csv
import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from src.pii.document_scanner import findings_path
from src.pii.redactor import output_paths, save_json
from src.pii.validator import (
    NEEDS_REVIEW, PASS, SEVERITY, alternation, blocks_by_id, is_unreadable_text, issue_key,
    load_review, normalise, review_approval, validation_path, worst
)


# Change whenever the report's rows or summary change
EXPOSURE_VERSION = "1.2.0"

COMBINED_NAME = "exposure_report"

# backend/output, where main.py writes
OUTPUT_DIR = Path(__file__).resolve().parents[2] / "output"

# What a page is called in each source type. A DOCX has no fixed pages:
# its one page is the whole document.
PAGE_LABELS = {"PDF": "Page", "PPTX": "Slide"}

# Block metadata.location, as shown; "body" is not shown
PARTS = {
    "header": "Header",
    "footer": "Footer",
    "text_box": "Text box",
    "notes": "Speaker notes",
    "footnote": "Footnote",
    "endnote": "Endnote",
    "comment": "Comment",
    "template": "Slide template",
}

DETECTOR_LABELS = {"nlp": "NER", "regex": "Regex", "manual": "Manual review"}

ACTIONS = {
    "redacted": "Redacted",
    "skipped": "Not redacted: reviewed as a false positive",
    "failed": "Not redacted: redaction failed",
    None: "Not redacted: no redaction recorded",
}

# The CSV and Markdown columns: the first six are the report's table,
# the rest trace each row to its finding and block
COLUMNS = [
    ("Document", "document"),
    ("Location", "location"),
    ("PII Type", "pii_type"),
    ("Detector", "detector"),
    ("Confidence", "confidence"),
    ("Action", "action"),
    ("Validation", "validation"),
    ("Finding ID", "finding_id"),
    ("Block ID", "block_id"),
    ("Page", "page_number"),
    ("Table ID", "table_id"),
    ("Row", "row"),
    ("Column", "column"),
    ("Rule ID", "rule_id"),
    ("Review Status", "review_status"),
    ("Validation Issues", "validation_issues"),
]

# Shown in the Markdown tables, which must stay readable
MARKDOWN_COLUMNS = ["Location", "PII Type", "Detector", "Confidence", "Action", "Validation", "Finding ID", "Block ID"]

# Fields that hold the document's own name, not text from inside it
NAME_FIELDS = {"document", "document_id", "file_name", "files"}


# --------------------------------------------------
# ROWS
# --------------------------------------------------

def check_inputs(document, report, sanitized, validation):
    """
    Refuse inputs that are not one run of the pipeline on this document.
    """

    if report.get("status") == "failed":
        raise ValueError(f"The PII scan failed ({report.get('error')}): there is nothing to report")

    for source, data in (("PII report", report), ("sanitized copy", sanitized), ("validation report", validation)):
        for field in ("document_id", "file_name"):
            if data.get(field) != document.get(field):
                raise ValueError(
                    f"The {source} is for {field} {data.get(field)!r}, not {document.get(field)!r}"
                )

    redaction = sanitized.get("metadata", {}).get("redaction") or {}

    if redaction.get("variant") != "sanitized":
        raise ValueError("The sanitized copy has no sanitized redaction record: redactor.py did not make it")

    for source, timestamp in (
        ("sanitized copy", redaction.get("report_scan_timestamp")),
        ("validation report", validation.get("report_scan_timestamp")),
    ):
        if timestamp != report.get("scan_timestamp"):
            raise ValueError(
                f"The {source} was made from another run of the PII report: "
                "run the redactor and the validator again"
            )


def where(item, source_type, blocks):
    """
    (place, location) of a finding: the page, slide or part of the
    document it is in ("Page 13", "Header"), and that with the element
    it is in ("Page 13, table row 3, column 2").
    """

    block_id = item.get("block_id") or ""

    if item.get("page_number") is None:
        return "File properties", f"File properties, {block_id.removeprefix('property_').replace('_', ' ')}"

    label = PAGE_LABELS.get(source_type)
    parts = [f"{label} {item['page_number']}"] if label else []
    details = []

    # A comment's author is reported as a block of its own: <block_id>_author
    owner_id = block_id

    if item.get("block_type") == "property" and block_id.endswith("_author"):
        owner_id = block_id.removesuffix("_author")
        details.append("comment author")

    _, block = blocks.get(owner_id, (None, {}))
    part = PARTS.get(block.get("metadata", {}).get("location"))

    if part:
        parts.append(part)
    elif not parts:
        parts.append("Body")

    if item.get("block_type") == "table_cell":
        details.append(f"table row {item.get('row')}, column {item.get('column')}")

    elif item.get("block_type") == "image_ocr":
        details.append("image text (OCR)")

    place = ", ".join(parts)

    return place, ", ".join([place, *details])


def detector_label(item):
    return DETECTOR_LABELS.get(item.get("detector"), "Rule")


def validation_issues(validation):
    """
    The validator's issues, each with the check that raised it and its
    key (validator.issue_key, which a sign-off names), and those that
    name a finding, by finding_id.
    """

    issues = [
        {"check": check["name"], **item, "key": issue_key(check["name"], item)}
        for check in validation.get("checks", [])
        for item in check.get("issues", [])
    ]

    by_finding = defaultdict(list)

    for item in issues:
        if item.get("finding_id"):
            by_finding[item["finding_id"]].append(item)

    return issues, by_finding


def describe(item, signed=None):
    """
    One validator issue, as a row shows it. signed: the sign-offs that
    apply (validator.review_approval), by issue key.
    """

    count = f" ({item['occurrences']}x)" if "occurrences" in item else ""
    sign_off = (signed or {}).get(item.get("key"))
    checked = f" [checked by {sign_off['reviewer']}]" if sign_off else ""

    return f"{item['severity']} {item['check']}: {item['reason']}{count}{checked}"


def review_section(report, validation, approval):
    """
    What the reviewer did: the words they hid (manual findings) and the
    issues they signed off. Names, times and IDs only: the PII stays in
    the PII report, and the review file's own fields are not copied.
    """

    issues, _ = validation_issues(validation)

    return {
        "manual_findings": [
            {
                "finding_id": item.get("finding_id"),
                "type": item.get("type"),
                "added_by": (item.get("review") or {}).get("added_by"),
                "added_at": (item.get("review") or {}).get("added_at"),
            }
            for item in report.get("findings", [])
            if item.get("detector") == "manual"
        ],
        "sign_offs": [
            {
                "check": item["check"],
                "finding_id": item.get("finding_id"),
                "block_id": item.get("block_id"),
                "reason": item["reason"],
                "reviewer": approval["signed"][item["key"]].get("reviewer"),
                "signed_at": approval["signed"][item["key"]].get("signed_at"),
            }
            for item in issues
            if item["key"] in approval["signed"]
        ],
    }


def build_rows(document, report, sanitized, validation, signed=None):
    """
    One row per finding, in report order. A row holds no PII value.
    signed: the sign-offs that apply, by issue key.
    """

    blocks = blocks_by_id(document)
    source_type = report.get("source_type") or document.get("source_type")
    redaction = sanitized["metadata"]["redaction"]
    audit = {entry.get("finding_id"): entry for entry in redaction.get("audit", [])}
    _, issues_by_finding = validation_issues(validation)

    rows = []

    for item in report["findings"]:
        place, location = where(item, source_type, blocks)
        entry = audit.get(item.get("finding_id")) or {}
        status = entry.get("status")
        issues = issues_by_finding.get(item.get("finding_id"), [])
        confidence = item.get("confidence")

        rows.append({
            "finding_id": item.get("finding_id"),
            "document": report.get("file_name"),
            "place": place,
            "location": location,
            "page_number": item.get("page_number"),
            "block_id": item.get("block_id"),
            "block_type": item.get("block_type"),
            "table_id": item.get("table_id"),
            "row": item.get("row"),
            "column": item.get("column"),
            "start": item.get("start"),
            "end": item.get("end"),
            "pii_type": item.get("type"),
            "detector": detector_label(item),
            "detector_name": item.get("detector"),
            "detection_method": item.get("detection_method"),
            "rule_id": item.get("rule_id"),
            "confidence": round(confidence, 2) if isinstance(confidence, (int, float)) else None,
            "review_status": item.get("review_status"),
            "action": ACTIONS.get(status, ACTIONS[None]),
            "redaction_status": status,
            "replacement": entry.get("replacement"),
            "redaction_reason": entry.get("reason"),
            "validation": worst(issue["severity"] for issue in issues),
            "validation_issues": [describe(issue, signed) for issue in issues],
        })

    return rows


# --------------------------------------------------
# NO VALUES
# --------------------------------------------------

def report_strings(node, path=""):
    """
    Every string in the exposure report, keys included (by_place is
    keyed by location), as (path, text), apart from the fields that hold
    the document's own name. A key is checked before the path names it,
    so a path never shows a value.
    """

    if isinstance(node, str):
        yield path, node

    elif isinstance(node, dict):
        for key, value in node.items():
            if key in NAME_FIELDS:
                continue

            yield f"a key of {path or 'the report'}", str(key)
            yield from report_strings(value, f"{path}.{key}" if path else str(key))

    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from report_strings(value, f"{path}[{index}]")


def check_no_values(exposure, report):
    """
    Refuse an exposure report that holds any value the PII report found,
    false positives included. Not unreadable OCR text: a misread ":" or
    "the" would refuse every report.
    """

    pattern = alternation({
        normalise(item.get("text") or "")
        for item in report.get("findings", [])
        if normalise(item.get("text") or "") and not is_unreadable_text(item)
    })

    if pattern is None:
        return

    for path, text in report_strings(exposure):
        if pattern.search(text):
            raise ValueError(f"The exposure report would hold a PII value at {path}: it was not written")


# --------------------------------------------------
# REPORT
# --------------------------------------------------

def build_exposure_report(document, report, sanitized, validation, review=None):
    """
    The exposure report of one document (see the module docstring),
    from the outputs of steps 1-4 and the reviewer's sign-offs, if any
    (<name>_review.json).

    document is the extractor's JSON: a dictionary, or the
    ExtractedDocument an extraction pipeline still holds in memory.
    """

    if hasattr(document, "to_dict"):
        document = document.to_dict()

    check_inputs(document, report, sanitized, validation)

    approval = review_approval(validation, review)
    rows = build_rows(document, report, sanitized, validation, approval["signed"])
    redaction = sanitized["metadata"]["redaction"]
    issues, _ = validation_issues(validation)
    verdict = validation.get("verdict")

    found = Counter(row["pii_type"] for row in rows)
    redacted = Counter(row["pii_type"] for row in rows if row["redaction_status"] == "redacted")

    exposure = {
        "exposure_version": EXPOSURE_VERSION,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "document_id": document.get("document_id"),
        "file_name": document.get("file_name"),
        "source_type": report.get("source_type") or document.get("source_type"),
        "sources": {
            "scanner_version": report.get("scanner_version"),
            "scan_timestamp": report.get("scan_timestamp"),
            "scan_status": report.get("status"),
            "redactor_version": redaction.get("redactor_version"),
            "redaction_timestamp": redaction.get("redaction_timestamp"),
            "redaction_status": redaction.get("status"),
            "validator_version": validation.get("validator_version"),
            "validated_at": validation.get("validated_at"),
        },
        "summary": {
            "total_findings": len(rows),
            "redacted": sum(redacted.values()),
            "not_redacted": len(rows) - sum(redacted.values()),
            "flagged_by_validation": sum(1 for row in rows if row["validation"] != PASS),
            "by_type": {
                pii_type: {"found": count, "redacted": redacted[pii_type]}
                for pii_type, count in found.most_common()
            },
            "by_detector": dict(Counter(row["detector"] for row in rows).most_common()),
            "by_action": dict(Counter(row["action"] for row in rows).most_common()),
            "by_place": dict(Counter(row["place"] for row in rows).most_common()),
        },
        "validation": {
            "verdict": verdict,
            # What approved_copy decides, but for the copy's hash, checked when it is loaded
            "may_send_to_llm": approval["approved"],
            "approved_after_review": approval["after_review"],
            "final_verdict": approval["final_verdict"],
            "checks": validation.get("summary", {}),
            "residual_exposure_total": sum(
                check.get("issues_total", 0) for check in validation.get("checks", [])
            ),
            "residual_exposure": sorted(issues, key=lambda item: -SEVERITY.get(item["severity"], 0)),
        },
        "review": review_section(report, validation, approval),
        "coverage_gaps": list((report.get("summary") or {}).get("extraction_warnings", [])),
        "rows": rows,
    }

    check_no_values(exposure, report)

    return exposure


# --------------------------------------------------
# FILES
# --------------------------------------------------

def exposure_path(json_path):
    """
    Where the exposure report for an extracted JSON file goes: next to
    it, as <name>_exposure.json.
    """

    json_path = Path(json_path)

    return json_path.with_name(f"{json_path.stem}_exposure.json")


def combined_folder(json_path):
    """
    Where the combined report over json_path's document goes: backend/output
    if the document is anywhere inside it, so runs kept in subfolders of
    their own are reported together; otherwise the document's own folder.
    """

    folder = Path(json_path).resolve().parent

    return OUTPUT_DIR if folder == OUTPUT_DIR or OUTPUT_DIR in folder.parents else folder


def combined_paths(folder):
    folder = Path(folder)

    return folder / f"{COMBINED_NAME}.csv", folder / f"{COMBINED_NAME}.md"


def exposure_file(json_path, report_path=None):
    """
    Build the exposure report of an extracted JSON file from its PII
    report (by default <name>_pii.json), sanitized copy and validation
    report, and save it.
    """

    report_path = report_path or findings_path(json_path)
    _, sanitized_path = output_paths(json_path)

    loaded = []

    for path in (json_path, report_path, sanitized_path, validation_path(json_path)):
        with open(path, "r", encoding="utf-8") as file:
            loaded.append(json.load(file))

    exposure = build_exposure_report(*loaded, review=load_review(json_path))
    exposure["files"] = {
        "document": str(json_path),
        "report": str(report_path),
        "sanitized": str(sanitized_path),
        "validation": str(validation_path(json_path)),
    }

    output_path = exposure_path(json_path)
    save_json(exposure, output_path)

    return exposure, output_path


def load_exposures(folder):
    """
    The exposure reports saved in folder and its subfolders (one per
    run, when runs have folders of their own), by path. Each gets
    "report_file": its path from folder, which tells apart two runs on
    the same document.
    """

    folder = Path(folder)
    exposures = []

    for path in sorted(folder.rglob("*_exposure.json")):
        with path.open("r", encoding="utf-8") as file:
            exposure = json.load(file)

        exposure["report_file"] = path.relative_to(folder).as_posix()
        exposures.append(exposure)

    return exposures


def csv_value(value):
    if isinstance(value, list):
        return "; ".join(value)

    return "" if value is None else value


def markdown_cell(value):
    text = str(csv_value(value))

    return text.replace("|", "\\|").replace("\n", " ") or " "


def markdown_table(headers, rows):
    lines = [
        "| " + " | ".join(headers) + " |",
        "|" + "---|" * len(headers),
    ]

    lines.extend("| " + " | ".join(markdown_cell(value) for value in row) + " |" for row in rows)

    return lines


def verdict_text(exposure):
    verdict = exposure["validation"]["verdict"]

    if verdict == PASS:
        return "PASS: the sanitized copy may be sent to an LLM"

    if verdict == "FAIL":
        return "FAIL: PII remains, or the copy does not match its document. Do not send it to an LLM"

    # Exposure reports from before reviews were recorded have no such field
    if verdict == NEEDS_REVIEW and exposure["validation"].get("approved_after_review"):
        return (
            "PASS after review: validation raised issues that needed review (NEEDS_REVIEW), and "
            "a reviewer checked every one (see Review). The sanitized copy may be sent to an LLM"
        )

    return f"{verdict}: do not send the sanitized copy to an LLM until a person has reviewed the issues"


def verdict_label(exposure):
    """
    The verdict as the summary table shows it: PASS after a review that
    approved the copy.
    """

    if exposure["validation"]["verdict"] == NEEDS_REVIEW and exposure["validation"].get("approved_after_review"):
        return "PASS (after review)"

    return exposure["validation"]["verdict"]


def markdown(exposures, folder):
    """
    The combined report: a summary over all documents, each document's
    exposure, then its rows.
    """

    columns = dict(COLUMNS)
    types = list(dict.fromkeys(
        pii_type for exposure in exposures for pii_type in exposure["summary"]["by_type"]
    ))

    lines = [
        "# PII Traceability & Exposure Report",
        "",
        f"Generated {datetime.now(timezone.utc).isoformat(timespec='seconds')} from "
        f"{len(exposures)} documents in `{folder}`.",
        "",
        "No PII value appears in this report. Each row is traced to its value by its "
        "**Finding ID** in the restricted PII report `<name>_pii.json`, and to its place in "
        "the extracted document by its **Block ID** (character offsets are in "
        "`<name>_exposure.json`).",
        "",
        "## Exposure summary",
        "",
        *markdown_table(
            ["Document", "Source", "Findings", "Redacted", "Not redacted",
             "Flagged by validation", "Verdict", "May go to an LLM"],
            [
                [
                    exposure["file_name"], exposure["source_type"],
                    exposure["summary"]["total_findings"], exposure["summary"]["redacted"],
                    exposure["summary"]["not_redacted"], exposure["summary"]["flagged_by_validation"],
                    verdict_label(exposure),
                    "Yes" if exposure["validation"]["may_send_to_llm"] else "No",
                ]
                for exposure in exposures
            ]
        ),
        "",
        "## PII found, by type",
        "",
        *markdown_table(
            ["PII Type", *(exposure["file_name"] for exposure in exposures), "Total"],
            [
                [
                    pii_type,
                    *(exposure["summary"]["by_type"].get(pii_type, {}).get("found", 0) for exposure in exposures),
                    sum(exposure["summary"]["by_type"].get(pii_type, {}).get("found", 0) for exposure in exposures),
                ]
                for pii_type in types
            ]
        ),
    ]

    for exposure in exposures:
        summary = exposure["summary"]
        validation = exposure["validation"]
        sources = exposure["sources"]

        lines += [
            "",
            f"## {exposure['file_name']}",
            "",
            f"- **Verdict:** {verdict_text(exposure)}",
            f"- **Findings:** {summary['total_findings']}, redacted {summary['redacted']}, "
            f"not redacted {summary['not_redacted']}",
            "- **By type:** " + (", ".join(
                f"{pii_type} {counts['found']}" for pii_type, counts in summary["by_type"].items()
            ) or "none"),
            "- **By detector:** " + (", ".join(
                f"{detector} {count}" for detector, count in summary["by_detector"].items()
            ) or "none"),
            "- **Most exposed:** " + (", ".join(
                f"{place} ({count})" for place, count in list(summary["by_place"].items())[:10]
            ) or "none"),
            f"- **Built from:** scan {sources['scan_timestamp']} (scanner {sources['scanner_version']}), "
            f"redaction {sources['redaction_timestamp']}, validation {sources['validated_at']}",
            f"- **Report file:** `{exposure['report_file']}`",
            "",
            f"### Residual exposure ({validation['residual_exposure_total']} validator issues)",
            "",
        ]

        if validation["residual_exposure"]:
            lines += markdown_table(
                ["Severity", "Check", "Finding ID", "Where", "Page", "Reason", "Occurrences"],
                [
                    [
                        item["severity"], item["check"], item.get("finding_id"),
                        # A value left in the copy is reported where it was left
                        item.get("block_id") or ", ".join(item.get("locations", [])[:3]),
                        item.get("page_number"), item["reason"], item.get("occurrences"),
                    ]
                    for item in validation["residual_exposure"]
                ]
            )
        else:
            lines.append("None: every check passed.")

        review = exposure.get("review") or {}

        if review.get("manual_findings") or review.get("sign_offs"):
            lines += ["", "### Review", ""]

            if review.get("manual_findings"):
                lines += markdown_table(
                    ["Hidden by a reviewer", "PII Type", "Reviewer", "When"],
                    [
                        [item["finding_id"], item["type"], item["added_by"], item["added_at"]]
                        for item in review["manual_findings"]
                    ]
                )
                lines.append("")

            if review.get("sign_offs"):
                lines += markdown_table(
                    ["Checked by a reviewer", "Finding ID", "Where", "Reason", "Reviewer", "When"],
                    [
                        [
                            item["check"], item.get("finding_id"), item.get("block_id"), item["reason"],
                            item["reviewer"], item["signed_at"],
                        ]
                        for item in review["sign_offs"]
                    ]
                )

        lines += ["", "### Coverage gaps", ""]
        lines += [f"- {warning}" for warning in exposure["coverage_gaps"]] or ["None reported by the scan."]

        lines += ["", "### Findings", ""]

        if exposure["rows"]:
            lines += markdown_table(
                MARKDOWN_COLUMNS,
                [[row[columns[header]] for header in MARKDOWN_COLUMNS] for row in exposure["rows"]]
            )
        else:
            lines.append("No PII was found.")

    return "\n".join(lines) + "\n"


def write_combined(folder):
    """
    Rebuild exposure_report.csv and exposure_report.md from every
    <name>_exposure.json in folder and its subfolders. With none, both
    are deleted. Returns (csv_path, md_path, number of documents).
    """

    csv_path, md_path = combined_paths(folder)
    exposures = load_exposures(folder)

    if not exposures:
        csv_path.unlink(missing_ok=True)
        md_path.unlink(missing_ok=True)
        return csv_path, md_path, 0

    # utf-8-sig: Excel opens a CSV as UTF-8 only with the byte order mark
    with csv_path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file)
        writer.writerow([header for header, _ in COLUMNS] + ["Report File"])

        for exposure in exposures:
            for row in exposure["rows"]:
                writer.writerow([csv_value(row.get(key)) for _, key in COLUMNS] + [exposure["report_file"]])

    md_path.write_text(markdown(exposures, folder), encoding="utf-8")

    return csv_path, md_path, len(exposures)


def print_exposure(exposure):
    """
    Print the document's exposure summary.
    """

    summary = exposure["summary"]

    print(
        f"      Findings:    {summary['total_findings']}, redacted {summary['redacted']}, "
        f"not redacted {summary['not_redacted']}, flagged by validation {summary['flagged_by_validation']}"
    )
    print(f"      By detector: {summary['by_detector']}")

    places = list(summary["by_place"].items())[:5]

    if places:
        print(f"      Most exposed: {', '.join(f'{place} ({count})' for place, count in places)}")


def main():

    if len(sys.argv) > 1:
        json_path = Path(sys.argv[1])
    else:
        json_path = Path(input("Extracted JSON file: ").strip().strip('"'))

    report_path = Path(sys.argv[2]) if len(sys.argv) > 2 else findings_path(json_path)

    for path in (json_path, report_path, output_paths(json_path)[1], validation_path(json_path)):
        if not path.is_file():
            sys.exit(f"File not found: {path}")

    exposure, output_path = exposure_file(json_path, report_path)
    csv_path, md_path, count = write_combined(combined_folder(json_path))

    print(f"Exposure report: {output_path}")
    print_exposure(exposure)
    print(f"Combined ({count} documents): {csv_path}, {md_path}")
    print(f"Verdict: {verdict_text(exposure)}")


if __name__ == "__main__":
    main()
