
import copy
import json
import sys

from src.pii.detectors import LocalNLPDetector
from src.pii.redactor import redact_document
from src.pii.validator import FAIL, NEEDS_REVIEW, PASS, only_tags_differ, validate_document

# Validation of a sanitized copy before it may go to an LLM. Each case
# redacts a hand-built document with the real redactor, sometimes
# tampers with the result, and checks the verdict and which checks
# raised it. The rescan uses the real spaCy model.

nlp = LocalNLPDetector()

if nlp.nlp is None:
    print("spaCy model unavailable:", nlp.error)
    sys.exit(1)


def block(block_id, text, block_type="paragraph", children=(), metadata=None):
    return {
        "block_id": block_id,
        "block_type": block_type,
        "text": text,
        "order": 1,
        "metadata": metadata or {},
        "bounding_box": None,
        "children": list(children),
    }


def words_of(block_id, text):
    return [
        {**block(f"{block_id}_w{number}", word, "word"),
         "bounding_box": {"x": 10.0 * number, "y": 0.0, "width": 8.0, "height": 6.0}}
        for number, word in enumerate(text.split(), start=1)
    ]


def document(*blocks):
    return {
        "document_id": "validator_test",
        "file_name": "test.pdf",
        "source_type": "PDF",
        "extraction_method": "native",
        "pages": [{"page_number": 1, "metadata": {}, "blocks": list(blocks)}],
        "metadata": {},
    }


def finding(number, block_id, pii_type, text, value, **fields):
    start = text.index(value)

    return {
        "finding_id": f"F-{number:04d}",
        "type": pii_type,
        "text": value,
        "start": start,
        "end": start + len(value),
        "confidence": 0.9,
        "block_id": block_id,
        "review_status": "unreviewed",
        "action": "REDACT",
        **fields,
    }


def report(*findings):
    return {
        "document_id": "validator_test",
        "file_name": "test.pdf",
        "status": "completed",
        "scan_timestamp": "2026-10-11T00:00:00+00:00",
        "findings": list(findings),
    }


def redact(original, pii_report):
    return redact_document(original, pii_report)["sanitized_document"]


def statuses(validation):
    """
    The checks that did not pass, with their status.
    """

    return {name: status for name, status in validation["summary"].items() if status != PASS}


def first_block(sanitized):
    return sanitized["pages"][0]["blocks"][0]


CONTACT = "Contact Jane Doe at jane@example.com about the vendor review."


def contact_case():
    original = document(block("b1", CONTACT))
    pii_report = report(
        finding(1, "b1", "PERSON_NAME", CONTACT, "Jane Doe"),
        finding(2, "b1", "EMAIL", CONTACT, "jane@example.com"),
    )

    return original, pii_report, redact(original, pii_report)


# --------------------------------------------------
# CASES
# Each returns (checks as booleans, the validation to print on failure)
# --------------------------------------------------

def case_clean_document_passes():
    original, pii_report, sanitized = contact_case()
    validation = validate_document(original, pii_report, sanitized, nlp)

    return [validation["verdict"] == PASS, statuses(validation) == {}], validation


def case_tagged_copy_fails():
    original = document(block("b1", CONTACT))
    pii_report = report(finding(1, "b1", "PERSON_NAME", CONTACT, "Jane Doe"))
    tagged = redact_document(original, pii_report)["tagged_document"]
    validation = validate_document(original, pii_report, tagged, nlp)

    return [
        validation["verdict"] == FAIL,
        validation["summary"]["provenance"] == FAIL,
        validation["summary"]["known_values"] == FAIL,
        # The validation report never repeats the PII
        "Jane" not in json.dumps(validation),
    ], validation


def case_copy_from_another_report_run_fails():
    original, pii_report, sanitized = contact_case()
    validation = validate_document(
        original, {**pii_report, "scan_timestamp": "2026-10-12T00:00:00+00:00"}, sanitized, nlp
    )

    return [statuses(validation) == {"provenance": FAIL}], validation


def case_finding_not_applied_fails():
    text = "Owner Jane Doe, phone 555-0147 ext"
    original = document(block("b1", text))
    bad = {**finding(1, "b1", "PERSON_NAME", text, "Jane Doe"), "start": 0, "end": 8}
    pii_report = report(bad, finding(2, "b1", "PHONE", text, "555-0147"))
    validation = validate_document(original, pii_report, redact(original, pii_report), nlp)

    return [
        validation["verdict"] == FAIL,
        validation["summary"]["report"] == FAIL,
        validation["summary"]["replacements"] == FAIL,
    ], validation


def case_value_put_back_fails():
    original, pii_report, sanitized = contact_case()
    first_block(sanitized)["text"] = CONTACT
    validation = validate_document(original, pii_report, sanitized, nlp)

    return [
        validation["verdict"] == FAIL,
        validation["summary"]["replacements"] == FAIL,
        validation["summary"]["known_values"] == FAIL,
    ], validation


def case_rescan_finds_missed_pii():
    # The report missed the phone number in b2
    text = "Escalations: call +1 (212) 555-0147 after hours."
    original = document(block("b1", CONTACT), block("b2", text))
    pii_report = report(
        finding(1, "b1", "PERSON_NAME", CONTACT, "Jane Doe"),
        finding(2, "b1", "EMAIL", CONTACT, "jane@example.com"),
    )
    validation = validate_document(original, pii_report, redact(original, pii_report), nlp)

    return [statuses(validation) == {"rescan": FAIL}], validation


def case_value_missed_elsewhere_fails():
    # From risk_policy: an ID found in one table, missed in another
    first = "Employee ID: MER-IN-4488"
    second = "Access was transferred to MER-IN-4488 in March."
    original = document(block("b1", first), block("b2", second))
    pii_report = report(finding(1, "b1", "EMPLOYEE_ID", first, "MER-IN-4488"))
    validation = validate_document(original, pii_report, redact(original, pii_report), nlp)

    return [
        statuses(validation) == {"known_values": FAIL},
        "MER-IN" not in json.dumps(validation),
    ], validation


def case_name_part_needs_review():
    # From risk_policy: only the surname was found in the second block
    first = "Approved by Ifeoma C. Adeyemi on 3 March."
    second = "Escalate to Ifeoma C. Adeyemi before release."
    original = document(block("b1", first), block("b2", second))
    pii_report = report(
        finding(1, "b1", "PERSON_NAME", first, "Ifeoma C. Adeyemi"),
        finding(2, "b2", "PERSON_NAME", second, "Adeyemi"),
    )
    validation = validate_document(original, pii_report, redact(original, pii_report), nlp)

    return [
        validation["verdict"] == NEEDS_REVIEW,
        statuses(validation) == {"known_values": NEEDS_REVIEW},
    ], validation


def case_ordinary_words_not_searched():
    # "Vendor" of a wrong name "Vendor Tier" is used in lowercase too:
    # it is an ordinary word, not part of a name
    first = "Owner: Vendor Tier"
    second = "Each Vendor is assessed by the vendor team."
    original = document(block("b1", first), block("b2", second))
    pii_report = report(finding(1, "b1", "PERSON_NAME", first, "Vendor Tier"))
    validation = validate_document(original, pii_report, redact(original, pii_report), nlp)

    return [validation["verdict"] == PASS], validation


def case_false_positive_kept_passes():
    # spaCy reads "Artificial Intelligence" as a person (as in
    # tprm_training); a reviewer marked it, so the rescan excuses it
    text = "Artificial Intelligence\nAnalytics Platform"
    original = document(block("b1", text, "table_cell"))
    pii_report = report(
        finding(1, "b1", "PERSON_NAME", text, "Artificial Intelligence", review_status="false_positive")
    )
    validation = validate_document(original, pii_report, redact(original, pii_report), nlp)

    return [validation["verdict"] == PASS], validation


def case_low_confidence_ocr_needs_review():
    # From risk_policy p18_b44: OCR misread both label and number
    original = document(
        block("b1", "Passpon TK4820013,", metadata={"ocr_confidence": 44.0}),
        block("b2", "", "image", metadata={"requires_ocr": True}),
    )
    pii_report = report()
    validation = validate_document(original, pii_report, redact(original, pii_report), nlp)
    ocr = next(check for check in validation["checks"] if check["name"] == "ocr")

    return [
        statuses(validation) == {"ocr": NEEDS_REVIEW},
        sorted(item["block_id"] for item in ocr["issues"]) == ["b1", "b2"],
    ], validation


def case_ocr_word_left_visible_fails():
    text = "Policy owner: Miriam T. Achebe, Group CRO"
    original = document(block("b1", text, children=words_of("b1", text)))
    pii_report = report(finding(1, "b1", "PERSON_NAME", text, "Miriam T. Achebe"))
    sanitized = redact(original, pii_report)
    first_block(sanitized)["children"][2]["text"] = "Miriam"
    validation = validate_document(original, pii_report, sanitized, nlp)

    return [
        validation["verdict"] == FAIL,
        validation["summary"]["ocr"] == FAIL,
        validation["summary"]["structure"] == PASS,
    ], validation


def case_image_ocr_words_pass():
    text = "Approver\nJyoti Karwal"
    words = [{"text": word, "x": 0, "y": 0, "width": 5, "height": 5, "confidence": 90} for word in text.split()]
    original = document(block("img1", text, "image_ocr", metadata={"words": words}))
    pii_report = report(finding(1, "img1", "PERSON_NAME", text, "Jyoti Karwal"))
    validation = validate_document(original, pii_report, redact(original, pii_report), nlp)
    ocr = next(check for check in validation["checks"] if check["name"] == "ocr")

    return [
        validation["verdict"] == PASS,
        any("image files" in note for note in ocr["notes"]),
    ], validation


def case_continued_table_headers_pass():
    def table(table_id, first_row, metadata=None):
        return block(table_id, "", "table", children=[
            block(f"{table_id}_c{column}", text, "table_cell", metadata={"row": 1, "column": column})
            for column, text in enumerate(first_row, start=1)
        ], metadata={"rows": 1, "columns": 2, **(metadata or {})})

    original = document(
        table("t1", ["Owner", "Jane Doe"]),
        table("t2", ["Approve", "Yes"], {"continues_table": "t1", "column_headers": {"1": "Owner", "2": "Jane Doe"}}),
    )
    pii_report = report(finding(1, "t1_c2", "PERSON_NAME", "Jane Doe", "Jane Doe"))
    validation = validate_document(original, pii_report, redact(original, pii_report), nlp)

    return [validation["verdict"] == PASS], validation


def case_structure_changes_fail():
    original, pii_report, sanitized = contact_case()
    results = []

    for change in (
        lambda copy_: copy_["pages"][0]["blocks"].append(block("b9", "Extra")),
        lambda copy_: first_block(copy_).update(bounding_box={"x": 1, "y": 1, "width": 1, "height": 1}),
        lambda copy_: first_block(copy_).update(
            text=first_block(copy_)["text"].replace("vendor review", "vendor audit")
        ),
    ):
        changed = copy.deepcopy(sanitized)
        change(changed)
        validation = validate_document(original, pii_report, changed, nlp)
        results.append(validation["summary"]["structure"] == FAIL)

    return results, validation


def case_without_nlp_needs_review():
    original, pii_report, sanitized = contact_case()
    validation = validate_document(original, pii_report, sanitized, None)

    return [statuses(validation) == {"rescan": NEEDS_REVIEW}], validation


def case_check_that_cannot_run_fails():
    original, pii_report, sanitized = contact_case()
    del sanitized["pages"]
    validation = validate_document(original, pii_report, sanitized, nlp)

    return [
        validation["verdict"] == FAIL,
        validation["summary"]["structure"] == FAIL,
        validation["summary"]["provenance"] == PASS,
    ], validation


def case_only_tags_differ():
    tag = "[REDACTED: PERSON_NAME]"

    return [
        only_tags_differ("Contact Jane", f"Contact {tag}"),
        only_tags_differ("Jane, Doe", f"{tag}, {tag}"),
        only_tags_differ("JaneDoe", f"{tag}{tag}"),
        # The text after a tag also occurs inside the redacted value
        only_tags_differ("x-1 y-1 z", f"{tag}-1 z"),
        only_tags_differ("same", "same"),
        not only_tags_differ("Contact Jane", f"Call {tag}"),
        not only_tags_differ("Contact Jane", f"Contact Jane {tag}"),
        not only_tags_differ("Contact ", f"Contact {tag}"),
    ], None


CASES = [
    ("A cleanly redacted document passes", case_clean_document_passes),
    ("The tagged copy given as the sanitized one fails", case_tagged_copy_fails),
    ("A copy made from another run of the report fails", case_copy_from_another_report_run_fails),
    ("A finding the redactor could not apply fails", case_finding_not_applied_fails),
    ("A value put back into its block fails", case_value_put_back_fails),
    ("The rescan finds PII the report missed", case_rescan_finds_missed_pii),
    ("A value found in one block but missed in another fails", case_value_missed_elsewhere_fails),
    ("Part of a name beside a redacted surname needs review", case_name_part_needs_review),
    ("Ordinary words in a wrong name are not searched", case_ordinary_words_not_searched),
    ("A reviewed false positive kept in the text passes", case_false_positive_kept_passes),
    ("Low-confidence OCR text and unread images need review", case_low_confidence_ocr_needs_review),
    ("An OCR word left over a redacted value fails", case_ocr_word_left_visible_fails),
    ("Image OCR words hidden by the redactor pass", case_image_ocr_words_pass),
    ("Continued table headers rebuilt by the redactor pass", case_continued_table_headers_pass),
    ("Added blocks, moved boxes and changed text fail", case_structure_changes_fail),
    ("Without spaCy, the rescan needs review", case_without_nlp_needs_review),
    ("A check that cannot run fails; the others still run", case_check_that_cannot_run_fails),
    ("Text may differ only where a tag stands", case_only_tags_differ),
]

failures = 0

print("========== VALIDATOR TEST ==========\n")

for description, case in CASES:
    checks, validation = case()
    passed = all(checks)
    failures += not passed

    print(f"{'PASS' if passed else 'FAIL'}  {description}")

    if not passed:
        print(f"        failed checks: {[number for number, ok in enumerate(checks, start=1) if not ok]}")

        if validation:
            print(f"        verdict: {validation['verdict']}  {statuses(validation)}")

            for check in validation["checks"]:
                for item in check["issues"][:3]:
                    print(f"        {check['name']}: {item}")

print(f"\n{len(CASES) - failures} of {len(CASES)} cases passed")
print("====================================")

sys.exit(1 if failures else 0)
