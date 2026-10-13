
import copy
import csv
import json
import sys
import tempfile
from pathlib import Path

from src.pii.exposure_report import build_exposure_report, check_no_values, write_combined
from src.pii.redactor import redact_document
from src.pii.validator import validate_document

# The exposure report built from the outputs of steps 1-4. Documents and
# findings are built by hand, in the extractor and scanner formats, then
# redacted and validated for real, so each case checks one rule.


def block(block_id, text, block_type="paragraph", children=(), metadata=None):
    return {
        "block_id": block_id,
        "block_type": block_type,
        "text": text,
        "order": 1,
        "metadata": {"location": "body", **(metadata or {})},
        "bounding_box": None,
        "children": list(children),
    }


def document(*blocks, source_type="PDF", properties=None, page_number=1):
    return {
        "document_id": "exposure_test",
        "file_name": f"test.{source_type.lower()}",
        "source_type": source_type,
        "extraction_method": "native",
        "pages": [{"page_number": page_number, "metadata": {}, "blocks": list(blocks)}],
        "metadata": {"properties": properties} if properties else {},
    }


DETECTORS = {
    "nlp": ("PERSON_NER", "ner"),
    "regex": ("EMAIL_REGEX", "regex"),
    "custom_rule": ("EMPLOYEE_ID_PREFIX", "regex"),
    "surname": ("PERSON_SURNAME", "context_rule"),
}


def finding(number, block_id, pii_type, text, value, detector="regex",
            page_number=1, block_type="paragraph", **fields):
    start = text.index(value)
    rule_id, method = DETECTORS[detector]

    return {
        "finding_id": f"F-{number:04d}",
        "type": pii_type,
        "text": value,
        "start": start,
        "end": start + len(value),
        "confidence": 0.94,
        "detector": detector,
        "rule_id": rule_id,
        "detection_method": method,
        "matched_pattern": "test",
        "context_trigger": None,
        "context": "",
        "page_number": page_number,
        "block_id": block_id,
        "block_type": block_type,
        "table_id": None,
        "row": None,
        "column": None,
        "bounding_box": None,
        "image": None,
        "review_status": "unreviewed",
        "action": "REDACT",
        **fields,
    }


def report(original, *findings, **fields):
    return {
        "document_id": original["document_id"],
        "file_name": original["file_name"],
        "source_type": original["source_type"],
        "scanner_version": "test",
        "status": "completed",
        "scan_timestamp": "2026-10-12T00:00:00+00:00",
        "summary": {"extraction_warnings": []},
        "findings": list(findings),
        **fields,
    }


def run(original, pii_report):
    """
    Steps 3-5 on a document and its report: (exposure, sanitized, validation).
    """

    sanitized = redact_document(original, pii_report)["sanitized_document"]
    validation = validate_document(original, pii_report, sanitized)

    return build_exposure_report(original, pii_report, sanitized, validation), sanitized, validation


def rows(exposure):
    return {row["finding_id"]: row for row in exposure["rows"]}


# --------------------------------------------------
# CASES
# --------------------------------------------------

def case_spec_columns():
    # The Phase 9 example: a name, an email and an employee ID on page 2
    text = "Owner Jane Doe, jane.doe@example.com, EMP-12345"
    original = document(block("p2_b1", text), page_number=2)
    exposure, _, _ = run(original, report(
        original,
        finding(1, "p2_b1", "PERSON_NAME", text, "Jane Doe", "nlp", page_number=2),
        finding(2, "p2_b1", "EMAIL", text, "jane.doe@example.com", "regex", page_number=2),
        finding(3, "p2_b1", "EMPLOYEE_ID", text, "EMP-12345", "custom_rule", page_number=2),
    ))
    table = [
        (row["document"], row["location"], row["pii_type"], row["detector"], row["confidence"], row["action"])
        for row in exposure["rows"]
    ]

    return [
        table == [
            ("test.pdf", "Page 2", "PERSON_NAME", "NER", 0.94, "Redacted"),
            ("test.pdf", "Page 2", "EMAIL", "Regex", 0.94, "Redacted"),
            ("test.pdf", "Page 2", "EMPLOYEE_ID", "Rule", 0.94, "Redacted"),
        ],
        exposure["summary"]["redacted"] == 3,
        exposure["summary"]["by_place"] == {"Page 2": 3},
        exposure["summary"]["by_type"]["EMAIL"] == {"found": 1, "redacted": 1},
    ]


def case_traceability():
    # Each row leads back to its finding, block and offsets, and to the
    # runs it was built from
    text = "Contact jane.doe@example.com"
    original = document(block("b1", text))
    pii_report = report(original, finding(1, "b1", "EMAIL", text, "jane.doe@example.com"))
    exposure, sanitized, validation = run(original, pii_report)
    row = rows(exposure)["F-0001"]

    return [
        (row["block_id"], row["start"], row["end"]) == ("b1", 8, 28),
        row["rule_id"] == "EMAIL_REGEX",
        row["replacement"] == "[REDACTED: EMAIL]",
        exposure["sources"]["scan_timestamp"] == pii_report["scan_timestamp"],
        exposure["sources"]["redaction_timestamp"] == sanitized["metadata"]["redaction"]["redaction_timestamp"],
        exposure["sources"]["validated_at"] == validation["validated_at"],
    ]


def case_no_values_in_report():
    # Fields that can hold a value are left out: context holds a row
    # label naming a person, context_trigger the people a surname names
    text = "Signed by Doe after Jane Doe reviewed it"
    original = document(block("b1", text))
    exposure, _, _ = run(original, report(
        original,
        finding(1, "b1", "PERSON_NAME", text, "Jane Doe", "nlp", context="Owner | Jane Doe"),
        {**finding(2, "b1", "PERSON_NAME", text, "Doe", "surname", context_trigger="Jane Doe")},
    ))
    serialised = json.dumps(exposure)

    return [
        "Jane" not in serialised,
        "Doe" not in serialised,
        len(exposure["rows"]) == 2,
    ]


def case_value_in_report_refused():
    pii_report = {"findings": [{"text": "Jane Doe"}, {"text": "555-0147"}]}
    refused = []

    for exposure in (
        {"rows": [{"location": "Page 1, Jane Doe"}]},
        {"summary": {"by_place": {"Jane  Doe": 1}}},
        {"rows": [{"validation_issues": ["call 555-0147"]}]},
    ):
        try:
            check_no_values(exposure, pii_report)
            refused.append(False)
        except ValueError as error:
            refused.append("Jane" not in str(error) and "555" not in str(error))

    # The document's own name is allowed
    check_no_values({"file_name": "Jane Doe.pdf", "rows": [{"document": "Jane Doe.pdf"}]}, pii_report)

    return refused


def case_locations():
    cell = block("t1_r2_c3", "jane@example.com", "table_cell", metadata={"row": 2, "column": 3})
    table = block("t1", "", "table", children=[cell], metadata={"rows": 2, "columns": 3})
    header_image = block("img1", "Jane Doe", "image_ocr", metadata={"location": "header"})
    notes = block("n1", "Call Jane Doe", metadata={"location": "notes"})
    comment = block("c1", "Please check", metadata={"location": "comment", "author": "Tobias Lindqvist"})

    def location(original, item):
        exposure, _, _ = run(original, report(original, item))
        return exposure["rows"][0]["location"]

    pdf = document(table)
    docx = document(header_image, comment, source_type="DOCX", properties={"last_modified_by": "Ana Park"})
    pptx = document(notes, source_type="PPTX", page_number=4)

    return [
        location(pdf, finding(
            1, "t1_r2_c3", "EMAIL", cell["text"], "jane@example.com",
            block_type="table_cell", table_id="t1", row=2, column=3
        )) == "Page 1, table row 2, column 3",
        location(docx, finding(1, "img1", "PERSON_NAME", "Jane Doe", "Jane Doe", "nlp", block_type="image_ocr"))
        == "Header, image text (OCR)",
        location(docx, finding(1, "c1_author", "PERSON_NAME", "Tobias Lindqvist", "Tobias Lindqvist", "nlp",
                               block_type="property"))
        == "Comment, comment author",
        location(docx, finding(1, "property_last_modified_by", "PERSON_NAME", "Ana Park", "Ana Park", "nlp",
                               page_number=None, block_type="property"))
        == "File properties, last modified by",
        location(pptx, finding(1, "n1", "PERSON_NAME", "Call Jane Doe", "Jane Doe", "nlp", page_number=4))
        == "Slide 4, Speaker notes",
    ]


def case_not_redacted_actions():
    text = "Vendor Tier owner Jane Doe"
    original = document(block("b1", text))
    bad = {**finding(2, "b1", "PERSON_NAME", text, "Jane Doe", "nlp"), "start": 0, "end": 8}
    exposure, _, _ = run(original, report(
        original,
        finding(1, "b1", "PERSON_NAME", text, "Vendor Tier", "nlp", review_status="false_positive"),
        bad,
    ))
    found = rows(exposure)

    return [
        found["F-0001"]["action"] == "Not redacted: reviewed as a false positive",
        found["F-0002"]["action"] == "Not redacted: redaction failed",
        exposure["summary"]["not_redacted"] == 2,
        exposure["validation"]["verdict"] == "FAIL",
        not exposure["validation"]["may_send_to_llm"],
    ]


def case_residual_exposure_on_its_finding():
    # The email is found in b1, but not in b2: the validator finds it
    # left there, and the exposure report flags the b1 finding
    first, second = "Write to jane@example.com", "or jane@example.com"
    original = document(block("b1", first), block("b2", second))
    exposure, _, _ = run(original, report(original, finding(1, "b1", "EMAIL", first, "jane@example.com")))
    row = rows(exposure)["F-0001"]
    residual = [item for item in exposure["validation"]["residual_exposure"] if item["check"] == "known_values"]

    return [
        row["action"] == "Redacted",
        row["validation"] == "FAIL",
        any("known_values" in issue for issue in row["validation_issues"]),
        exposure["summary"]["flagged_by_validation"] == 1,
        [item["finding_id"] for item in residual] == ["F-0001"],
        "jane" not in json.dumps(exposure),
    ]


def case_mismatched_inputs_refused():
    text = "Contact jane@example.com"
    original = document(block("b1", text))
    pii_report = report(original, finding(1, "b1", "EMAIL", text, "jane@example.com"))
    sanitized = redact_document(original, pii_report)["sanitized_document"]
    validation = validate_document(original, pii_report, sanitized)
    rescanned = {**pii_report, "scan_timestamp": "2026-10-13T00:00:00+00:00"}
    tagged = redact_document(original, pii_report)["tagged_document"]
    refused = []

    for inputs in (
        (original, rescanned, sanitized, validation),           # another scan
        (original, pii_report, tagged, validation),             # the tagged copy
        (original, pii_report, sanitized, {**validation, "document_id": "other"}),
        (original, {**pii_report, "status": "failed"}, sanitized, validation),
    ):
        try:
            build_exposure_report(*inputs)
            refused.append(False)
        except ValueError:
            refused.append(True)

    return refused


def case_combined_files():
    text = "Owner Jane Doe, jane.doe@example.com"
    original = document(block("p2_b1", text), page_number=2)
    exposure, _, _ = run(original, report(
        original,
        finding(1, "p2_b1", "PERSON_NAME", text, "Jane Doe", "nlp", page_number=2),
        finding(2, "p2_b1", "EMAIL", text, "jane.doe@example.com", page_number=2),
    ))

    # Two runs: one in the folder, one in a subfolder of its own
    with tempfile.TemporaryDirectory() as folder:
        saved = [Path(folder) / "test_exposure.json", Path(folder) / "job1" / "test_exposure.json"]

        for path in saved:
            path.parent.mkdir(exist_ok=True)
            path.write_text(json.dumps(exposure), encoding="utf-8")

        csv_path, md_path, count = write_combined(folder)

        with csv_path.open(encoding="utf-8-sig", newline="") as file:
            table = list(csv.reader(file))

        markdown = md_path.read_text(encoding="utf-8")

        for path in saved:
            path.unlink()

        _, _, emptied = write_combined(folder)
        removed = not csv_path.exists() and not md_path.exists()

    return [
        count == 2,
        table[0][:6] == ["Document", "Location", "PII Type", "Detector", "Confidence", "Action"],
        table[1][:6] == ["test.pdf", "Page 2", "PERSON_NAME", "NER", "0.94", "Redacted"],
        {row[-1] for row in table[1:]} == {"test_exposure.json", "job1/test_exposure.json"},
        "| Page 2 | EMAIL | Regex | 0.94 | Redacted |" in markdown,
        "Jane" not in markdown and "jane" not in markdown,
        emptied == 0 and removed,
    ]


def case_inputs_unchanged():
    text = "Contact Jane Doe"
    original = document(block("b1", text))
    pii_report = report(original, finding(1, "b1", "PERSON_NAME", text, "Jane Doe", "nlp"))
    sanitized = redact_document(original, pii_report)["sanitized_document"]
    validation = validate_document(original, pii_report, sanitized)
    before = copy.deepcopy((original, pii_report, sanitized, validation))
    build_exposure_report(original, pii_report, sanitized, validation)

    return [(original, pii_report, sanitized, validation) == before]


CASES = [
    ("Rows give document, location, type, detector, confidence, action", case_spec_columns),
    ("Rows trace to finding, block, offsets and runs", case_traceability),
    ("No PII value is in the report", case_no_values_in_report),
    ("A report holding a value is refused", case_value_in_report_refused),
    ("Locations: table cell, header image, comment author, property, notes", case_locations),
    ("False positives and failed redactions are not redacted", case_not_redacted_actions),
    ("A value left in the copy flags its finding", case_residual_exposure_on_its_finding),
    ("Inputs from different runs are refused", case_mismatched_inputs_refused),
    ("Combined CSV and Markdown, subfolders included", case_combined_files),
    ("The inputs are not changed", case_inputs_unchanged),
]

failures = 0

print("========== EXPOSURE REPORT TEST ==========\n")

for description, case in CASES:
    checks = case()
    passed = all(checks)
    failures += not passed

    print(f"{'PASS' if passed else 'FAIL'}  {description}")

    if not passed:
        print(f"        failed checks: {[number for number, ok in enumerate(checks, start=1) if not ok]}")

print(f"\n{len(CASES) - failures} of {len(CASES)} cases passed")
print("==========================================")

sys.exit(1 if failures else 0)
