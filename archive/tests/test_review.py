
import json
import sys
import tempfile
from pathlib import Path

from src.pii.detectors import LocalNLPDetector
from src.pii.document_scanner import findings_path, save_findings
from src.pii.exposure_report import exposure_file, markdown
from src.pii.redactor import output_paths, redact_document, redact_file
from src.pii.review import (
    ReviewError, check_reviewer, find_issue, hidden_all_report, hidden_report, kept_review, leftover_findings,
    save_review, sign_off, signed_off_all_review, signed_off_review, standing_sign_offs,
    unreadable_findings, withdrawn_all_review, withdrawn_review
)
from src.pii.validator import (
    FAIL, NEEDS_REVIEW, PASS, NotApproved, approved_copy, issue_key, review_approval,
    review_path, validate_document, validate_file
)

# A reviewer's answers to a NEEDS_REVIEW validation (src/pii/review.py):
# "Hide it" adds the leftover words of a name, or OCR text read too badly
# to scan, as manual findings, and "Checked, it's fine" signs an issue
# off. Each case writes a hand-built
# document and its PII report to a temporary folder, runs the real
# redactor and validator, answers, and checks the verdict and the
# release gate. The rescan uses the real spaCy model.

nlp = LocalNLPDetector()

if nlp.nlp is None:
    print("spaCy model unavailable:", nlp.error)
    sys.exit(1)

REVIEWER = "KR"


def block(block_id, text, block_type="paragraph", metadata=None, children=()):
    return {
        "block_id": block_id,
        "block_type": block_type,
        "text": text,
        "order": 1,
        "metadata": metadata or {},
        "bounding_box": None,
        "children": list(children),
    }


def ocr_paragraph(block_id, text, confidences):
    """
    An OCR'd paragraph read well on the whole, whose word children carry
    these confidences.
    """

    words = [
        {**block(f"{block_id}_w{number}", word, "word", {"ocr_confidence": confidence, "line": 1}),
         "bounding_box": {"x": 10.0 * number, "y": 0.0, "width": 8.0, "height": 6.0}}
        for number, (word, confidence) in enumerate(zip(text.split(), confidences), start=1)
    ]

    return block(block_id, text, metadata={"ocr_confidence": 85.0}, children=words)


def document(*blocks):
    return {
        "document_id": "review_test",
        "file_name": "test.pdf",
        "source_type": "PDF",
        "extraction_method": "native",
        "pages": [{"page_number": 1, "metadata": {}, "blocks": list(blocks)}],
        "metadata": {},
    }


def finding(number, block_id, pii_type, text, value):
    start = text.index(value)

    return {
        "finding_id": f"F-{number:04d}",
        "type": pii_type,
        "text": value,
        "start": start,
        "end": start + len(value),
        "confidence": 0.9,
        "detector": "nlp",
        "page_number": 1,
        "block_id": block_id,
        "block_type": "paragraph",
        "review_status": "unreviewed",
        "action": "REDACT",
    }


def report(*findings):
    return {
        "document_id": "review_test",
        "file_name": "test.pdf",
        "source_type": "PDF",
        "status": "completed",
        "scan_timestamp": "2026-10-14T00:00:00+00:00",
        "summary": {"total_findings": len(findings), "extraction_warnings": []},
        "findings": list(findings),
    }


def run(folder, original, pii_report):
    """
    Save the document and its report as the pipeline names them, redact
    and validate. Returns the document's path and the validation.
    """

    json_path = Path(folder) / "review_test.json"
    json_path.write_text(json.dumps(original), encoding="utf-8")
    save_findings(pii_report, findings_path(json_path))

    return json_path, rerun(json_path)


def rerun(json_path):
    redact_file(json_path)
    validation, _ = validate_file(json_path, nlp_detector=nlp)

    return validation


def released(json_path):
    try:
        approved_copy(json_path)
        return True
    except NotApproved:
        return False


def refused(answer):
    try:
        answer()
        return False
    except ReviewError:
        return True


def keys(validation, severity=NEEDS_REVIEW, check_name=None):
    return [
        issue_key(check["name"], item)
        for check in validation["checks"]
        for item in check["issues"]
        if item["severity"] == severity and check_name in (None, check["name"])
    ]


# Only the surname was found in the second block: its first name is left
FIRST = "Approved by Ifeoma Adeyemi on 3 March."
SECOND = "Escalate to Ifeoma Adeyemi before release."


def name_left_case():
    original = document(block("b1", FIRST), block("b2", SECOND))
    pii_report = report(
        finding(1, "b1", "PERSON_NAME", FIRST, "Ifeoma Adeyemi"),
        finding(2, "b2", "PERSON_NAME", SECOND, "Adeyemi"),
    )

    return original, pii_report


# Text the scan could not vouch for: one block read badly by OCR, one
# image never read
def unreadable_case():
    original = document(
        block("b1", "Passpon TK4820013,", metadata={"ocr_confidence": 44.0}),
        block("b2", "", "image", metadata={"requires_ocr": True}),
    )

    return original, report()


def sanitized_texts(json_path):
    sanitized = json.loads(output_paths(json_path)[1].read_text(encoding="utf-8"))

    return [item["text"] for item in sanitized["pages"][0]["blocks"]]


def hide(json_path, key):
    """
    "Hide it" as jobs.Jobs.hide applies it: the issue's text added to the
    PII report, the review file deleted, redaction and validation run
    again, and the sign-offs of unchanged issues kept.
    """

    sign_offs = standing_sign_offs(json_path)
    pii_report, added = hidden_report(json_path, key, REVIEWER)
    save_findings(pii_report, findings_path(json_path))
    review_path(json_path).unlink(missing_ok=True)

    validation = rerun(json_path)
    kept = kept_review(sign_offs, validation)

    if kept:
        save_review(json_path, kept)

    return pii_report, added, validation


def manual_findings(pii_report):
    return [item for item in pii_report["findings"] if item["finding_id"].startswith("M-")]


TAG = "[REDACTED: UNREADABLE_TEXT]"


# --------------------------------------------------
# CASES
# Each returns a list of checks as booleans
# --------------------------------------------------

def case_hide_leftover_name_passes():
    with tempfile.TemporaryDirectory() as folder:
        json_path, before = run(folder, *name_left_case())
        key = keys(before, check_name="known_values")[0]

        pii_report, added = hidden_report(json_path, key, REVIEWER)
        save_findings(pii_report, findings_path(json_path))
        after = rerun(json_path)

        manual = [item for item in pii_report["findings"] if item["finding_id"].startswith("M-")]
        sanitized = json.loads(output_paths(json_path)[1].read_text(encoding="utf-8"))
        second = sanitized["pages"][0]["blocks"][1]["text"]

        return [
            before["verdict"] == NEEDS_REVIEW,
            added == 1 and [item["text"] for item in manual] == ["Ifeoma"],
            manual[0]["block_id"] == "b2" and manual[0]["detector"] == "manual",
            manual[0]["review_status"] == "confirmed" and manual[0]["review"]["added_by"] == REVIEWER,
            pii_report["summary"]["total_findings"] == 3 and pii_report["summary"]["manual_findings"] == 1,
            "Ifeoma" not in second,
            after["verdict"] == PASS,
            released(json_path),
        ]


def case_refused_answers():
    original, pii_report = name_left_case()
    validation = validate_document(
        original, pii_report, redact_document(original, pii_report)["sanitized_document"], nlp
    )
    ocr_original, ocr_report = unreadable_case()

    with tempfile.TemporaryDirectory() as folder:
        json_path, ocr_validation = run(folder, ocr_original, ocr_report)
        ocr_key, image_key = keys(ocr_validation)

        return [
            refused(lambda: check_reviewer("  ", pii_report)),
            refused(lambda: check_reviewer("x" * 81, pii_report)),
            # The name is personal data the scan found: it would end up in the audit report
            refused(lambda: check_reviewer("Adeyemi", pii_report)),
            check_reviewer("  K  R ", pii_report) == "K R",
            refused(lambda: find_issue(validation, "not-a-key")),
            # An image never read has no text to hide
            refused(lambda: hidden_report(json_path, image_key, REVIEWER)),
            refused(lambda: leftover_findings(original, pii_report, "ocr", {"severity": NEEDS_REVIEW}, REVIEWER)),
            refused(lambda: unreadable_findings(original, pii_report, "known_values", {"severity": NEEDS_REVIEW, "finding_id": "F-0001"}, REVIEWER)),
            # Nothing signed off yet, so nothing to withdraw
            refused(lambda: withdrawn_review(json_path, ocr_key)),
        ]


def case_hide_unreadable_block_passes():
    # The badly read block is hidden in full. A well-read block repeats
    # part of the misreading: it is not searched for there
    text = "Passpon TK4820013,"
    original = document(
        block("b1", text, metadata={"ocr_confidence": 44.0}),
        block("b2", "Passpon office: see the vendor review."),
    )

    with tempfile.TemporaryDirectory() as folder:
        json_path, before = run(folder, original, report())
        pii_report, added, after = hide(json_path, keys(before)[0])
        manual = manual_findings(pii_report)
        texts = sanitized_texts(json_path)
        exposure, _ = exposure_file(json_path)

        return [
            before["verdict"] == NEEDS_REVIEW,
            added == 1 and [(item["type"], item["text"]) for item in manual] == [("UNREADABLE_TEXT", text)],
            manual[0]["review"]["added_by"] == REVIEWER,
            texts == [TAG, "Passpon office: see the vendor review."],
            after["verdict"] == PASS,
            released(json_path),
            exposure["validation"]["final_verdict"] == PASS,
        ]


def case_hide_unreadable_block_keeps_found_values():
    # The email was found: its tag stays, the text around it is hidden
    text = "Mail to jane@example.com by Friday"
    original = document(block("b1", text, metadata={"ocr_confidence": 51.0}))
    pii_report = report(finding(1, "b1", "EMAIL", text, "jane@example.com"))

    with tempfile.TemporaryDirectory() as folder:
        json_path, before = run(folder, original, pii_report)
        hidden, added, after = hide(json_path, keys(before)[0])

        return [
            added == 2 and [item["text"] for item in manual_findings(hidden)] == ["Mail to", "by Friday"],
            sanitized_texts(json_path) == [f"{TAG} [REDACTED: EMAIL] {TAG}"],
            after["verdict"] == PASS,
        ]


def case_hide_uncertain_words():
    # One read of the value is uncertain: every place it stands is hidden,
    # the rest of the well-read block stays
    text = "Passpon TK48Z0913 on file TK48Z0913"
    original = document(ocr_paragraph("b1", text, [96, 41, 95, 94, 97]))

    with tempfile.TemporaryDirectory() as folder:
        json_path, before = run(folder, original, report())
        ocr = next(check for check in before["checks"] if check["name"] == "ocr")
        pii_report, added, after = hide(json_path, keys(before)[0])
        sanitized = json.loads(output_paths(json_path)[1].read_text(encoding="utf-8"))
        paragraph = sanitized["pages"][0]["blocks"][0]

        return [
            before["verdict"] == NEEDS_REVIEW and ocr["issues"][0]["reason"].startswith("1 OCR words"),
            added == 2 and {item["text"] for item in manual_findings(pii_report)} == {"TK48Z0913"},
            paragraph["text"] == f"Passpon {TAG} on file {TAG}",
            [word["text"] for word in paragraph["children"]] == ["Passpon", TAG, "on", "file", TAG],
            after["verdict"] == PASS,
            released(json_path),
        ]


def case_hide_keeps_unchanged_sign_offs():
    # Two blocks read badly and an image never read. Two issues are
    # checked, then the first block is hidden: the checks stand
    original = document(
        block("b1", "Passpon TK4820013,", metadata={"ocr_confidence": 44.0}),
        block("b2", "Ref: 0091 ACCT", metadata={"ocr_confidence": 38.0}),
        block("b3", "", "image", metadata={"requires_ocr": True}),
    )

    with tempfile.TemporaryDirectory() as folder:
        json_path, before = run(folder, original, report())
        first, second, image = keys(before)

        for key in (second, image):
            save_review(json_path, signed_off_review(json_path, key, REVIEWER))

        signed_before = review_approval(before, json.loads(review_path(json_path).read_text(encoding="utf-8")))
        _, _, after = hide(json_path, first)
        kept = json.loads(review_path(json_path).read_text(encoding="utf-8"))
        approval = review_approval(after, kept)

        return [
            signed_before["final_verdict"] == NEEDS_REVIEW and signed_before["final_checks"]["ocr"] == NEEDS_REVIEW,
            after["verdict"] == NEEDS_REVIEW and keys(after) == [second, image],
            sorted(entry["key"] for entry in kept["sign_offs"]) == sorted([second, image]),
            kept["validated_at"] == after["validated_at"],
            approval["after_review"] and approval["final_verdict"] == PASS,
            approval["final_checks"]["ocr"] == PASS,
            released(json_path),
            # A sign-off for an issue no longer there is not kept
            kept_review({"not-a-key": {"key": "not-a-key"}}, after) is None,
        ]


def case_hide_all_ocr_issues():
    # Two blocks read badly, an uncertain word and an image never read.
    # The second block was checked as fine: it is left as it is
    original = document(
        block("b1", "Passpon TK4820013,", metadata={"ocr_confidence": 44.0}),
        block("b2", "Ref: 0091 ACCT", metadata={"ocr_confidence": 38.0}),
        ocr_paragraph("b3", "Passpon TK48Z0913 on file", [96, 41, 95, 94]),
        block("b4", "", "image", metadata={"requires_ocr": True}),
    )

    with tempfile.TemporaryDirectory() as folder:
        json_path, before = run(folder, original, report())
        first, second, words, image = keys(before)
        save_review(json_path, signed_off_review(json_path, second, REVIEWER))

        # As jobs.Jobs.hide_all applies it
        sign_offs = standing_sign_offs(json_path)
        pii_report, added, issues = hidden_all_report(json_path, REVIEWER)
        save_findings(pii_report, findings_path(json_path))
        review_path(json_path).unlink()
        after = rerun(json_path)
        save_review(json_path, kept_review(sign_offs, after))

        texts = sanitized_texts(json_path)

        return [
            issues == 2 and added == 2,
            [item["block_id"] for item in manual_findings(pii_report)] == ["b1", "b3"],
            texts[:3] == [TAG, "Ref: 0091 ACCT", f"Passpon {TAG} on file"],
            keys(after) == [second, image],
            # The checked block and the image are all that is left: nothing to hide
            refused(lambda: hidden_all_report(json_path, REVIEWER)),
        ]


def case_check_all_ocr_issues():
    # Two blocks read badly and an image never read: "Checked, all fine"
    # signs off both blocks at once, not the image; Undo takes both back
    original = document(
        block("b1", "Passpon TK4820013,", metadata={"ocr_confidence": 44.0}),
        block("b2", "Ref: 0091 ACCT", metadata={"ocr_confidence": 38.0}),
        block("b3", "", "image", metadata={"requires_ocr": True}),
    )

    with tempfile.TemporaryDirectory() as folder:
        json_path, validation = run(folder, original, report())
        first, second, image = keys(validation)

        review = signed_off_all_review(json_path, REVIEWER)
        save_review(json_path, review)
        partial = review_approval(validation, review)

        save_review(json_path, signed_off_review(json_path, image, REVIEWER))
        approved = released(json_path)
        again = refused(lambda: signed_off_all_review(json_path, REVIEWER))

        save_review(json_path, withdrawn_all_review(json_path))
        undone = json.loads(review_path(json_path).read_text(encoding="utf-8"))

        return [
            sorted(entry["key"] for entry in review["sign_offs"]) == sorted([first, second]),
            {(entry["reviewer"], entry["signed_at"]) for entry in review["sign_offs"]} == {(REVIEWER, review["sign_offs"][0]["signed_at"])},
            partial["open"] == 1 and not partial["approved"],
            approved,
            # Nothing left to check
            again,
            [entry["key"] for entry in undone["sign_offs"]] == [image],
            not released(json_path),
            refused(lambda: withdrawn_all_review(json_path)),
        ]


def case_sign_offs_release_the_copy():
    with tempfile.TemporaryDirectory() as folder:
        json_path, validation = run(folder, *unreadable_case())
        first, second = keys(validation)

        blocked = not released(json_path)

        save_review(json_path, signed_off_review(json_path, first, REVIEWER))
        half = released(json_path)

        save_review(json_path, signed_off_review(json_path, second, REVIEWER))
        both = released(json_path)
        text = approved_copy(json_path)["pages"][0]["blocks"][0]["text"] if both else None

        save_review(json_path, withdrawn_review(json_path, second))
        withdrawn = released(json_path)

        return [
            validation["verdict"] == NEEDS_REVIEW and len(keys(validation)) == 2,
            blocked, not half, both, text == "Passpon TK4820013,",
            not withdrawn,
        ]


def case_changed_copy_voids_sign_offs():
    with tempfile.TemporaryDirectory() as folder:
        json_path, validation = run(folder, *unreadable_case())

        for key in keys(validation):
            save_review(json_path, signed_off_review(json_path, key, REVIEWER))

        approved = released(json_path)

        # A new copy, validated again: the sign-offs were for the old one
        sanitized_path = output_paths(json_path)[1]
        sanitized_path.write_text(sanitized_path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        validate_file(json_path, nlp_detector=nlp)

        return [approved, not released(json_path)]


def case_fail_cannot_be_signed_off():
    contact = "Contact Jane Doe at jane@example.com about the vendor review."
    original = document(block("b1", contact))
    # The email was never found: the rescan fails the copy
    pii_report = report(finding(1, "b1", "PERSON_NAME", contact, "Jane Doe"))

    with tempfile.TemporaryDirectory() as folder:
        json_path, validation = run(folder, original, pii_report)
        fail_keys = keys(validation, FAIL)

        return [
            validation["verdict"] == FAIL,
            bool(fail_keys),
            refused(lambda: signed_off_review(json_path, fail_keys[0], REVIEWER)),
            not review_approval(validation)["approved"],
        ]


def case_issues_not_all_listed_block_approval():
    listed = {"severity": NEEDS_REVIEW, "reason": "low OCR confidence", "block_id": "b1"}
    validation = {
        "document_id": "review_test", "file_name": "test.pdf", "validated_at": "t",
        "verdict": NEEDS_REVIEW,
        "files": {"sanitized_sha256": "h"},
        "checks": [{"name": "ocr", "status": NEEDS_REVIEW, "issues_total": 2, "issues": [listed]}],
    }
    review = sign_off(None, validation, report(), issue_key("ocr", listed), REVIEWER)
    approval = review_approval(validation, review)

    return [
        len(approval["signed"]) == 1 and approval["open"] == 0,
        not approval["approved"] and "only some" in approval["reason"],
    ]


def case_audit_report_shows_the_review():
    with tempfile.TemporaryDirectory() as folder:
        json_path, validation = run(folder, *unreadable_case())

        for key in keys(validation):
            save_review(json_path, signed_off_review(json_path, key, REVIEWER))

        signed, _ = exposure_file(json_path)
        signed["report_file"] = "review_test_exposure.json"
        text = markdown([signed], folder)

    # "Hide it" on another document: its manual finding is a row
    with tempfile.TemporaryDirectory() as folder:
        json_path, before = run(folder, *name_left_case())
        pii_report, _ = hidden_report(json_path, keys(before, check_name="known_values")[0], REVIEWER)
        save_findings(pii_report, findings_path(json_path))
        rerun(json_path)
        hidden, _ = exposure_file(json_path)

    manual_rows = [row for row in hidden["rows"] if row["finding_id"].startswith("M-")]

    return [
        signed["validation"]["may_send_to_llm"] and signed["validation"]["approved_after_review"],
        signed["validation"]["verdict"] == NEEDS_REVIEW and signed["validation"]["final_verdict"] == PASS,
        [item["reviewer"] for item in signed["review"]["sign_offs"]] == [REVIEWER, REVIEWER],
        "PASS after review" in text and "PASS (after review)" in text and "### Review" in text,
        len(manual_rows) == 1 and manual_rows[0]["detector"] == "Manual review",
        hidden["review"]["manual_findings"][0]["added_by"] == REVIEWER,
        hidden["validation"]["verdict"] == PASS and not hidden["validation"]["approved_after_review"],
    ]


CASES = [
    ("\"Hide it\" hides a first name left beside its surname, and the copy passes", case_hide_leftover_name_passes),
    ("\"Hide it\" hides a badly read OCR block, and the copy passes", case_hide_unreadable_block_passes),
    ("Hiding a badly read block keeps the tags of values found in it", case_hide_unreadable_block_keeps_found_values),
    ("\"Hide it\" hides a low-confidence value-like word wherever it stands", case_hide_uncertain_words),
    ("\"Hide it\" keeps the sign-offs of the issues it did not change", case_hide_keeps_unchanged_sign_offs),
    ("\"Hide all OCR issues\" hides every OCR issue not checked as fine, at once", case_hide_all_ocr_issues),
    ("\"Checked, all fine\" signs off every OCR issue at once; Undo takes them back", case_check_all_ocr_issues),
    ("Answers that cannot be applied are refused", case_refused_answers),
    ("Every issue signed off releases the copy; one withdrawn blocks it", case_sign_offs_release_the_copy),
    ("Sign-offs given for another copy are void", case_changed_copy_voids_sign_offs),
    ("A FAIL can never be signed off", case_fail_cannot_be_signed_off),
    ("Issues the report does not all list cannot all be signed off", case_issues_not_all_listed_block_approval),
    ("The audit report shows what the reviewer hid and checked", case_audit_report_shows_the_review),
]

failures = 0

print("========== REVIEW TEST ==========\n")

for description, case in CASES:
    checks = case()
    passed = all(checks)
    failures += not passed

    print(f"{'PASS' if passed else 'FAIL'}  {description}")

    if not passed:
        print(f"        failed checks: {[number for number, ok in enumerate(checks, start=1) if not ok]}")

print(f"\n{len(CASES) - failures} of {len(CASES)} cases passed")
print("=================================")

sys.exit(1 if failures else 0)
