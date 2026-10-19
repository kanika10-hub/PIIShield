"""
A reviewer's answer to one issue of a NEEDS_REVIEW validation, given on
the web page (src/web/jobs.py). Each answer names the issue by its key
(validator.issue_key) and is recorded with the reviewer's name and time.

    hide        "Hide it" adds the text an issue is about to the PII
                report as manual findings (M-0001, ...):
                  known_values: the words of a name the scan found, left
                  elsewhere in the copy (PERSON_NAME)
                  ocr: a block read with low confidence, all of its text;
                  or its low-confidence words that look like part of a
                  value (UNREADABLE_TEXT, see validator.unreadable_issue)
                The redactor, validator and exposure report then run
                again, so the issue clears only if the text is really gone.
    hide all    "Hide all": the same for every ocr issue that can be
                hidden and was not signed off, in one run
                (hidden_all_report).
    sign off    "Checked, it's fine": a person checked the issue. Saved
                in <name>_review.json for this validation of this exact
                copy. Once every NEEDS_REVIEW issue is signed off,
                approved_copy() lets the copy go (validator.review_approval).
                A FAIL can never be signed off.
    sign off all
                "Checked, all fine": the same for every unreadable OCR
                issue at once (sign_off_all; withdraw_all undoes it).

Hiding text makes a new copy, validated again: the sign-offs given for
the old one are void. jobs.py deletes the review file before running
again, then gives the new validation the sign-offs of the issues it
still holds unchanged (kept_review): an issue's key changes with any
change to the issue.

Manual findings live in <name>_pii.json: a new scan of the document
(main.py, or a new upload) does not keep them.
"""

import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from src.pii.document_scanner import (
    LOW_OCR_CONFIDENCE, collect_units, findings_path, image_details, uncertain_value_words,
    word_lists
)
from src.pii.redactor import save_json
from src.pii.validator import (
    NEEDS_REVIEW, UNREADABLE_TEXT, alternation, findings_to_redact, issue_key, load_review,
    normalise, review_approval, review_path, searched_values, unreadable_issue,
    validation_path, value_pattern
)


# Change whenever the review file's format changes
REVIEW_VERSION = "1.0.0"

MANUAL_DETECTOR = "manual"

# The reviewer's name goes into the shareable audit report
MAX_REVIEWER_CHARS = 80


class ReviewError(Exception):
    """
    The reviewer's answer cannot be applied. The message says why, for
    the page to show, and never holds a PII value.
    """


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_json(path, what):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ReviewError(f"This scan has no readable {what}: {type(error).__name__}") from error


def check_reviewer(reviewer, report):
    """
    The reviewer's name, with its spaces tidied. It goes into the
    shareable audit report, so it must not hold a value the scan found
    (exposure_report would refuse to write the report).
    """

    name = " ".join(str(reviewer or "").split())

    if not name:
        raise ReviewError("Enter your name: it is recorded with your review.")

    if len(name) > MAX_REVIEWER_CHARS:
        raise ReviewError(f"The name is longer than {MAX_REVIEWER_CHARS} characters.")

    pattern = alternation({
        normalise(item.get("text") or "")
        for item in report.get("findings", [])
        if normalise(item.get("text") or "")
    })

    if pattern and pattern.search(name):
        raise ReviewError(
            "The name matches personal data found in this document: use your initials "
            "or a reviewer ID instead."
        )

    return name


def find_issue(validation, key):
    """
    (check name, issue) of the validation issue with this key.
    """

    for check in validation.get("checks", []):
        for item in check.get("issues", []):
            if issue_key(check["name"], item) == key:
                return check["name"], item

    raise ReviewError("This issue is no longer in the validation report: reload the page.")


def name_part_issue(check_name, item):
    """
    True for a word of a name the scan found, still in the sanitized
    copy somewhere else.
    """

    return check_name == "known_values" and item.get("severity") == NEEDS_REVIEW and bool(item.get("finding_id"))


def can_hide(check_name, item):
    """
    True for an issue "Hide it" can fix: a leftover part of a name, or
    OCR text read too badly to scan.
    """

    return name_part_issue(check_name, item) or unreadable_issue(check_name, item)


# --------------------------------------------------
# HIDE
# --------------------------------------------------

def manual_finding(unit, start, end, pii_type, reviewer, added_at):
    """
    A finding the reviewer added, in the format scan_document gives its
    findings, for the scan unit (page_number, block, table, context; see
    collect_units) it is in.
    """

    page_number, block, table, context = unit

    return {
        "type": pii_type,
        "text": block["text"][start:end],
        "start": start,
        "end": end,
        "confidence": 1.0,
        "detector": MANUAL_DETECTOR,
        "rule_id": "MANUAL_REVIEW",
        "detection_method": "manual",
        "matched_pattern": None,
        "context_trigger": None,
        "context": context,
        "page_number": page_number,
        "block_id": block["block_id"],
        "block_type": block["block_type"],
        "table_id": table["block_id"] if table else None,
        "row": block["metadata"].get("row") if table else None,
        "column": block["metadata"].get("column") if table else None,
        "bounding_box": block.get("bounding_box"),
        "image": image_details(block, start, end),
        "review_status": "confirmed",
        "action": "REDACT",
        "review": {"added_by": reviewer, "added_at": added_at},
    }


def leftover_findings(document, report, check_name, item, reviewer):
    """
    Manual findings for each place a known_values issue's name parts are
    left: the words check_known_values attributes to the issue's finding
    (validator.searched_values), wherever the scanner reads text and no
    finding already hides them.
    """

    if not name_part_issue(check_name, item):
        raise ReviewError("Only a part of a name left in the copy can be hidden this way.")

    _, name_parts = searched_values(document, report)
    pattern = alternation([part for part, finding_id in name_parts.items() if finding_id == item["finding_id"]])

    if pattern is None:
        raise ReviewError("This issue is no longer in the PII report: reload the page.")

    hidden = findings_to_redact(document, report)
    units, _ = collect_units(document)
    added_at = timestamp()
    found = []

    for unit in units:
        block = unit[1]
        spans = [(other["start"], other["end"]) for other in hidden.get(block["block_id"], [])]

        for match in pattern.finditer(block["text"]):
            if not any(start < match.end() and match.start() < end for start, end in spans):
                found.append(manual_finding(unit, match.start(), match.end(), "PERSON_NAME", reviewer, added_at))

    if not found:
        raise ReviewError(
            "The words left are in a part of the file this page cannot hide. If they are "
            "harmless, use \"Checked, it's fine\"."
        )

    return found


def unreadable_findings(document, report, check_name, item, reviewer, units=None, hidden=None):
    """
    Manual findings (UNREADABLE_TEXT) that hide the OCR text an ocr issue
    is about, where no finding hides it yet:

        a block read with low confidence: all of its text, as one finding
        for each stretch between the findings already in it
        low-confidence words that look like part of a value: each such
        word, everywhere it stands in its block (the validator checks
        that no redacted value is left in its block)

    units (collect_units) and hidden (validator.findings_to_redact) are
    worked out from document and report unless given: hidden_all_report
    gives them once for every issue.
    """

    if not unreadable_issue(check_name, item):
        raise ReviewError("Only OCR text read with low confidence can be hidden this way.")

    if units is None:
        units, _ = collect_units(document)

    if hidden is None:
        hidden = findings_to_redact(document, report)

    unit = next((unit for unit in units if unit[1]["block_id"] == item["block_id"]), None)

    if unit is None:
        raise ReviewError("This block is no longer in the extracted document: reload the page.")

    block = unit[1]
    text = block["text"]
    taken = [(other["start"], other["end"]) for other in hidden.get(block["block_id"], [])]
    confidence = block.get("metadata", {}).get("ocr_confidence")
    spans = []

    if confidence is not None and confidence < LOW_OCR_CONFIDENCE:
        position = 0

        for start, end in sorted(taken) + [(len(text), len(text))]:
            # The stretch before this finding, without its edge whitespace
            for match in re.finditer(r"\S(?:.*\S)?", text[position:start], re.DOTALL):
                spans.append((position + match.start(), position + match.end()))

            position = max(position, end)

    else:
        uncertain = {
            words[index].get("text")
            for words in word_lists(block)
            for index in uncertain_value_words(words)
        }

        # Longest first: "=99.9%" is hidden whole, not around "99.9%"
        for word in sorted(filter(None, uncertain), key=len, reverse=True):
            for match in re.finditer(value_pattern(word), text):
                if not any(match.start() < end and start < match.end() for start, end in taken + spans):
                    spans.append(match.span())

    if not spans:
        raise ReviewError(
            "This text is already hidden, or is in a part of the file this page cannot hide. "
            "If it is harmless, use \"Checked, it's fine\"."
        )

    added_at = timestamp()

    return [manual_finding(unit, start, end, UNREADABLE_TEXT, reviewer, added_at) for start, end in sorted(spans)]


def add_findings(report, findings):
    """
    Add manual findings to a PII report, numbered after any earlier ones,
    and count them in its summary. The scan_timestamp is kept: the
    redaction and validation are checked against it.
    """

    numbers = [
        int(item["finding_id"][2:])
        for item in report["findings"]
        if str(item.get("finding_id")).startswith("M-") and item["finding_id"][2:].isdigit()
    ]
    first = max(numbers, default=0) + 1

    report["findings"].extend(
        {"finding_id": f"M-{number:04d}", **item}
        for number, item in enumerate(findings, start=first)
    )

    summary = report.get("summary") or {}
    summary["total_findings"] = len(report["findings"])
    summary["by_type"] = dict(Counter(item["type"] for item in report["findings"]))
    summary["manual_findings"] = sum(1 for item in report["findings"] if item.get("detector") == MANUAL_DETECTOR)
    report["summary"] = summary


def hidden_report(json_path, key, reviewer):
    """
    The PII report of an extracted JSON file with the text of the issue
    (leftover name parts, or unreadable OCR text) added as manual
    findings. Nothing is saved: the caller saves it, deletes the review
    file (its sign-offs are for the old copy) and runs redaction,
    validation and the exposure report again.

    Returns (report, number of findings added).
    """

    document = read_json(json_path, "extracted document")
    report = read_json(findings_path(json_path), "PII report")
    validation = read_json(validation_path(json_path), "validation report")

    if report.get("status") == "failed":
        raise ReviewError("The PII scan failed: there is nothing to review.")

    reviewer = check_reviewer(reviewer, report)
    check_name, item = find_issue(validation, key)

    if unreadable_issue(check_name, item):
        found = unreadable_findings(document, report, check_name, item, reviewer)
    elif name_part_issue(check_name, item):
        found = leftover_findings(document, report, check_name, item, reviewer)
    else:
        raise ReviewError("This issue has no text to hide. If it is harmless, use \"Checked, it's fine\".")

    add_findings(report, found)

    return report, len(found)


def hidden_all_report(json_path, reviewer):
    """
    "Hide all" (for every OCR issue): the PII report of an extracted JSON file with
    the text of every listed unreadable OCR issue (validator.unreadable_issue)
    added as manual findings, in one go. Issues a reviewer signed off as
    fine are left as they are. Nothing is saved, as for hidden_report.

    Returns (report, number of findings added, number of issues hidden).
    """

    document = read_json(json_path, "extracted document")
    report = read_json(findings_path(json_path), "PII report")
    validation = read_json(validation_path(json_path), "validation report")

    if report.get("status") == "failed":
        raise ReviewError("The PII scan failed: there is nothing to review.")

    reviewer = check_reviewer(reviewer, report)
    signed = review_approval(validation, load_review(json_path))["signed"]
    units, _ = collect_units(document)
    hidden = findings_to_redact(document, report)
    found = []
    issues = 0

    for check in validation.get("checks", []):
        for item in check.get("issues", []):
            if not unreadable_issue(check["name"], item) or issue_key(check["name"], item) in signed:
                continue

            try:
                found.extend(unreadable_findings(document, report, check["name"], item, reviewer, units, hidden))
                issues += 1
            except ReviewError:
                # Nothing left in this block that the page can hide
                continue

    if not found:
        raise ReviewError("There is no OCR text left to hide: answer the remaining issues one by one.")

    add_findings(report, found)

    return report, len(found), issues


# --------------------------------------------------
# SIGN OFF
# --------------------------------------------------

def current_review(review, validation):
    """
    The review of this validation: review if it was given for it, or a
    new one (sign-offs for another copy or validation are void).
    """

    fields = {
        "document_id": validation.get("document_id"),
        "file_name": validation.get("file_name"),
        "validated_at": validation.get("validated_at"),
        "sanitized_sha256": validation.get("files", {}).get("sanitized_sha256"),
    }

    if review and all(review.get(field) == value for field, value in fields.items()):
        return review

    return {"review_version": REVIEW_VERSION, **fields, "sign_offs": []}


def standing_sign_offs(json_path):
    """
    The sign-offs that apply to the current validation of an extracted
    JSON file (validator.review_approval), by issue key: those "Hide it"
    may keep.
    """

    validation = read_json(validation_path(json_path), "validation report")

    return review_approval(validation, load_review(json_path))["signed"]


def kept_review(sign_offs, validation):
    """
    After "Hide it": a review of the new validation with the sign-offs
    (issue key -> sign-off, see standing_sign_offs) of the issues it
    still lists unchanged, so the reviewer does not answer again what
    the hiding did not touch. None if no issue is left unchanged.
    """

    keys = {
        issue_key(check["name"], item)
        for check in validation.get("checks", [])
        for item in check.get("issues", [])
        if item.get("severity") == NEEDS_REVIEW
    }

    kept = [entry for key, entry in (sign_offs or {}).items() if key in keys]

    if not kept:
        return None

    review = current_review(None, validation)
    review["sign_offs"] = kept

    return review


def sign_off(review, validation, report, key, reviewer):
    """
    The review with the issue signed off. Only a NEEDS_REVIEW issue can
    be: a FAIL means PII is known to remain.
    """

    reviewer = check_reviewer(reviewer, report)
    check_name, item = find_issue(validation, key)

    if item.get("severity") != NEEDS_REVIEW:
        raise ReviewError("Only an issue that needs review can be signed off: a FAIL cannot.")

    review = current_review(review, validation)
    review["sign_offs"] = [entry for entry in review["sign_offs"] if entry.get("key") != key]
    review["sign_offs"].append({
        "key": key,
        "check": check_name,
        "reviewer": reviewer,
        "signed_at": timestamp(),
    })

    return review


def withdraw(review, validation, key):
    """
    The review without the issue's sign-off.
    """

    review = current_review(review, validation)
    kept = [entry for entry in review["sign_offs"] if entry.get("key") != key]

    if len(kept) == len(review["sign_offs"]):
        raise ReviewError("This issue is not signed off: reload the page.")

    review["sign_offs"] = kept

    return review


def signed_off_review(json_path, key, reviewer):
    """
    The review file's content with the issue signed off. Not saved: see
    save_review.
    """

    report = read_json(findings_path(json_path), "PII report")
    validation = read_json(validation_path(json_path), "validation report")

    return sign_off(load_review(json_path), validation, report, key, reviewer)


def withdrawn_review(json_path, key):
    validation = read_json(validation_path(json_path), "validation report")

    return withdraw(load_review(json_path), validation, key)


def unreadable_keys(validation):
    """
    The keys of the listed issues about OCR text read too badly to scan
    (validator.unreadable_issue): those the page answers all at once.
    """

    return [
        issue_key(check["name"], item)
        for check in validation.get("checks", [])
        for item in check.get("issues", [])
        if unreadable_issue(check["name"], item)
    ]


def sign_off_all(review, validation, report, reviewer):
    """
    "Checked, all fine" for every unreadable OCR issue not signed off yet:
    one sign-off each, with the same reviewer and time.
    """

    reviewer = check_reviewer(reviewer, report)
    review = current_review(review, validation)
    signed = {entry.get("key") for entry in review["sign_offs"]}
    signed_at = timestamp()
    added = [key for key in unreadable_keys(validation) if key not in signed]

    if not added:
        raise ReviewError("Every OCR issue is already answered: reload the page.")

    review["sign_offs"].extend(
        {"key": key, "check": "ocr", "reviewer": reviewer, "signed_at": signed_at}
        for key in added
    )

    return review


def withdraw_all(review, validation):
    """
    The review without the sign-offs of any unreadable OCR issue.
    """

    keys = set(unreadable_keys(validation))
    review = current_review(review, validation)
    kept = [entry for entry in review["sign_offs"] if entry.get("key") not in keys]

    if len(kept) == len(review["sign_offs"]):
        raise ReviewError("No OCR issue is signed off: reload the page.")

    review["sign_offs"] = kept

    return review


def signed_off_all_review(json_path, reviewer):
    report = read_json(findings_path(json_path), "PII report")
    validation = read_json(validation_path(json_path), "validation report")

    return sign_off_all(load_review(json_path), validation, report, reviewer)


def withdrawn_all_review(json_path):
    validation = read_json(validation_path(json_path), "validation report")

    return withdraw_all(load_review(json_path), validation)


def save_review(json_path, review):
    save_json(review, review_path(json_path))
