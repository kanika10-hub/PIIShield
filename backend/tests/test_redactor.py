
import copy
import sys

from src.pii.redactor import redact_document

# Redaction of scanner findings into a tagged and a sanitized copy.
# Documents and findings are built by hand, in the extractor and
# scanner formats, so each case checks one rule.


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
    """
    PDF word children: one per whitespace-separated token, with a box.
    """

    return [
        {**block(f"{block_id}_w{number}", word, "word"),
         "bounding_box": {"x": 10.0 * number, "y": 0.0, "width": 8.0, "height": 6.0}}
        for number, word in enumerate(text.split(), start=1)
    ]


def document(*blocks, properties=None):
    return {
        "document_id": "redactor_test",
        "file_name": "test.pdf",
        "source_type": "PDF",
        "extraction_method": "native",
        "pages": [{"page_number": 1, "metadata": {}, "blocks": list(blocks)}],
        "metadata": {"properties": properties} if properties else {},
    }


def finding(number, block_id, pii_type, text, value, occurrence=0, **fields):
    """
    A finding for value in text; occurrence picks a repeated value.
    """

    start = -1

    for _ in range(occurrence + 1):
        start = text.index(value, start + 1)

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


def report(*findings, **fields):
    return {
        "document_id": "redactor_test",
        "file_name": "test.pdf",
        "status": "completed",
        "scan_timestamp": "2026-10-11T00:00:00+00:00",
        "findings": list(findings),
        **fields,
    }


def texts(result, variant):
    return {
        item["block_id"]: item["text"]
        for page in result[f"{variant}_document"]["pages"]
        for item in page["blocks"]
    }


def statuses(result):
    return {entry["finding_id"]: entry["status"] for entry in result["redaction"]["audit"]}


# --------------------------------------------------
# CASES
# --------------------------------------------------

def case_tagged_and_sanitized():
    text = "Contact Jane Doe at jane@example.com"
    result = redact_document(
        document(block("b1", text)),
        report(
            finding(1, "b1", "PERSON_NAME", text, "Jane Doe"),
            finding(2, "b1", "EMAIL", text, "jane@example.com"),
        )
    )

    return [
        texts(result, "tagged")["b1"] == "Contact [PERSON_NAME: Jane Doe] at [EMAIL: jane@example.com]",
        texts(result, "sanitized")["b1"] == "Contact [REDACTED: PERSON_NAME] at [REDACTED: EMAIL]",
        result["redaction"]["status"] == "completed",
        result["sanitized_document"]["metadata"]["redaction"]["variant"] == "sanitized",
        result["tagged_document"]["metadata"]["redaction"]["variant"] == "tagged",
    ]


def case_nested_findings_merge():
    # From risk_policy p13_t1_row_3_cell_3: a PDF line wrap gives two
    # PHONE readings that start together
    text = "Mobile +91 98860\n55107 (India)"
    result = redact_document(
        document(block("b1", text)),
        report(
            finding(1, "b1", "PHONE", text, "+91 98860"),
            finding(2, "b1", "PHONE", text, "+91 98860\n55107"),
        )
    )

    return [
        texts(result, "sanitized")["b1"] == "Mobile [REDACTED: PHONE] (India)",
        texts(result, "tagged")["b1"] == "Mobile [PHONE: +91 98860\n55107] (India)",
        statuses(result) == {"F-0001": "redacted", "F-0002": "redacted"},
        result["redaction"]["summary"]["spans_replaced"] == 1,
    ]


def case_name_inside_email():
    text = "Write to john.smith@example.com today"
    result = redact_document(
        document(block("b1", text)),
        report(
            finding(1, "b1", "PERSON_NAME", text, "john.smith"),
            finding(2, "b1", "EMAIL", text, "john.smith@example.com"),
        )
    )

    return [
        texts(result, "sanitized")["b1"] == "Write to [REDACTED: EMAIL] today",
        statuses(result) == {"F-0001": "redacted", "F-0002": "redacted"},
    ]


def case_partial_overlap():
    # Neither finding contains the other: the span covers both
    text = "ref AB12-34CD end"
    result = redact_document(
        document(block("b1", text)),
        report(
            finding(1, "b1", "EMPLOYEE_ID", text, "AB12-34"),
            finding(2, "b1", "PASSPORT_NUMBER", text, "34CD"),
        )
    )

    return [
        texts(result, "sanitized")["b1"] == "ref [REDACTED: EMPLOYEE_ID] end",
    ]


def case_bad_offsets_reported():
    # The bad finding is reported, the good one in the same block is
    # still redacted, and the error does not repeat the value
    text = "Owner Jane Doe, phone 555-0147"
    bad = {**finding(1, "b1", "PERSON_NAME", text, "Jane Doe"), "start": 0, "end": 8}
    good = finding(2, "b1", "PHONE", text, "555-0147")
    result = redact_document(document(block("b1", text)), report(bad, good))
    errors = result["redaction"]["errors"]

    return [
        result["redaction"]["status"] == "needs_review",
        texts(result, "sanitized")["b1"] == "Owner Jane Doe, phone [REDACTED: PHONE]",
        statuses(result) == {"F-0001": "failed", "F-0002": "redacted"},
        [error["finding_id"] for error in errors] == ["F-0001"],
        "Jane" not in str(errors),
    ]


def case_unknown_block_and_missing_fields():
    text = "Jane Doe"
    unknown = finding(1, "nope", "PERSON_NAME", text, "Jane Doe")
    no_offsets = {**finding(2, "b1", "PERSON_NAME", text, "Jane Doe"), "start": None}
    result = redact_document(document(block("b1", text)), report(unknown, no_offsets))

    return [
        result["redaction"]["status"] == "needs_review",
        statuses(result) == {"F-0001": "failed", "F-0002": "failed"},
        texts(result, "sanitized")["b1"] == "Jane Doe",
    ]


def case_false_positive_skipped():
    text = "Vendor Tier review"
    result = redact_document(
        document(block("b1", text)),
        report(finding(1, "b1", "PERSON_NAME", text, "Vendor Tier", review_status="false_positive"))
    )

    return [
        texts(result, "sanitized")["b1"] == text,
        statuses(result) == {"F-0001": "skipped"},
        result["redaction"]["status"] == "completed",
    ]


def case_word_children():
    text = "Policy owner: Miriam T. Achebe, Group CRO"
    words = words_of("b1", text)
    result = redact_document(
        document(block("b1", text, children=words)),
        report(finding(1, "b1", "PERSON_NAME", text, "Miriam T. Achebe"))
    )
    sanitized = result["sanitized_document"]["pages"][0]["blocks"][0]["children"]
    tagged = result["tagged_document"]["pages"][0]["blocks"][0]["children"]
    name = "[REDACTED: PERSON_NAME]"

    return [
        [word["text"] for word in sanitized]
        == ["Policy", "owner:", name, name, name, "Group", "CRO"],
        all(word["bounding_box"] is not None for word in sanitized),
        [word["text"] for word in tagged] == text.split(),
    ]


def case_mismatched_words_all_hidden():
    text = "Owner: Jane Doe"
    words = words_of("b1", "Owner: Jane D0e")
    result = redact_document(
        document(block("b1", text, children=words)),
        report(finding(1, "b1", "PERSON_NAME", text, "Jane Doe"))
    )
    sanitized = result["sanitized_document"]["pages"][0]["blocks"][0]["children"]

    return [
        result["redaction"]["status"] == "completed_with_warnings",
        all(word["text"] == "[REDACTED]" for word in sanitized),
        [warning["block_id"] for warning in result["redaction"]["warnings"]] == ["b1"],
    ]


def case_image_ocr_words():
    text = "Approver\nJyoti Karwal"
    ocr_words = [
        {"text": word, "x": 0, "y": 0, "width": 5, "height": 5, "confidence": 90}
        for word in text.split()
    ]
    result = redact_document(
        document(block("img1", text, "image_ocr", metadata={"words": ocr_words})),
        report(finding(1, "img1", "PERSON_NAME", text, "Jyoti Karwal"))
    )
    words = result["sanitized_document"]["pages"][0]["blocks"][0]["metadata"]["words"]

    return [
        [word["text"] for word in words]
        == ["Approver", "[REDACTED: PERSON_NAME]", "[REDACTED: PERSON_NAME]"],
        all("x" in word for word in words),
    ]


def case_table_cell():
    cell_text = "m.achebe@cadencefg.com"
    table = block("t1", "", "table", children=[
        block("t1_r1_c1", "Email", "table_cell", metadata={"row": 1, "column": 1}),
        block("t1_r2_c1", cell_text, "table_cell", metadata={"row": 2, "column": 1}),
    ], metadata={"rows": 2, "columns": 1})
    result = redact_document(
        document(table),
        report(finding(1, "t1_r2_c1", "EMAIL", cell_text, cell_text))
    )
    cells = result["sanitized_document"]["pages"][0]["blocks"][0]["children"]

    return [
        [cell["text"] for cell in cells] == ["Email", "[REDACTED: EMAIL]"],
    ]


def case_property_and_comment_author():
    comment = block("c1", "Please check", metadata={"author": "Tobias Lindqvist"})
    result = redact_document(
        document(comment, properties={"author": "Miriam Achebe", "created": "2026-01-01"}),
        report(
            finding(1, "c1_author", "PERSON_NAME", "Tobias Lindqvist", "Tobias Lindqvist"),
            finding(2, "property_author", "PERSON_NAME", "Miriam Achebe", "Miriam Achebe"),
        )
    )
    sanitized = result["sanitized_document"]
    tagged = result["tagged_document"]

    return [
        sanitized["pages"][0]["blocks"][0]["metadata"]["author"] == "[REDACTED: PERSON_NAME]",
        sanitized["metadata"]["properties"]["author"] == "[REDACTED: PERSON_NAME]",
        sanitized["metadata"]["properties"]["created"] == "2026-01-01",
        tagged["metadata"]["properties"]["author"] == "[PERSON_NAME: Miriam Achebe]",
        result["redaction"]["status"] == "completed",
    ]


def case_continued_table_headers():
    # A header naming a person, copied into a chain of two continued
    # tables. Header text is whitespace-normalised in the copy.
    def table(table_id, first_row, metadata=None):
        return block(table_id, "", "table", children=[
            block(f"{table_id}_r1_c{column}", text, "table_cell", metadata={"row": 1, "column": column})
            for column, text in enumerate(first_row, start=1)
        ], metadata={"rows": 1, "columns": 2, **(metadata or {})})

    headers = {"1": "Owner", "2": "Jane Doe"}
    source = table("t1", ["Owner", "Jane  Doe"])
    first = table("t2", ["Approve", "Yes"], {"continues_table": "t1", "column_headers": dict(headers)})
    second = table("t3", ["Review", "No"], {"continues_table": "t2", "column_headers": dict(headers)})

    result = redact_document(
        document(source, first, second),
        report(finding(1, "t1_r1_c2", "PERSON_NAME", "Jane  Doe", "Jane  Doe"))
    )
    tables = result["sanitized_document"]["pages"][0]["blocks"]
    expected = {"1": "Owner", "2": "[REDACTED: PERSON_NAME]"}

    return [
        tables[1]["metadata"]["column_headers"] == expected,
        tables[2]["metadata"]["column_headers"] == expected,
        result["tagged_document"]["pages"][0]["blocks"][1]["metadata"]["column_headers"] == headers,
        result["redaction"]["status"] == "completed",
    ]


def case_original_unchanged():
    text = "Contact Jane Doe"
    original = document(block("b1", text, children=words_of("b1", text)))
    before = copy.deepcopy(original)
    redact_document(original, report(finding(1, "b1", "PERSON_NAME", text, "Jane Doe")))

    return [original == before]


def case_wrong_or_failed_report_refused():
    original = document(block("b1", "text"))
    refused = []

    for bad_report in (
        report(document_id="another_document"),
        report(status="failed", error="ValueError: bad offsets"),
    ):
        try:
            redact_document(original, bad_report)
            refused.append(False)
        except ValueError:
            refused.append(True)

    return refused


CASES = [
    ("Tagged and sanitized copies", case_tagged_and_sanitized),
    ("Nested findings merge into one span", case_nested_findings_merge),
    ("A name inside an email gets the email's tag", case_name_inside_email),
    ("Partly overlapping findings: the span covers both", case_partial_overlap),
    ("Bad offsets reported; the block's other findings still redacted", case_bad_offsets_reported),
    ("Unknown block and missing offsets are reported", case_unknown_block_and_missing_fields),
    ("A reviewed false positive is kept", case_false_positive_skipped),
    ("PDF word children touching a span are hidden", case_word_children),
    ("Words that do not match the text are all hidden", case_mismatched_words_all_hidden),
    ("Image OCR words touching a span are hidden", case_image_ocr_words),
    ("Table cells are redacted", case_table_cell),
    ("File property and comment author", case_property_and_comment_author),
    ("Continued table headers are rebuilt", case_continued_table_headers),
    ("The input document is not changed", case_original_unchanged),
    ("A report for another document, or a failed scan, is refused", case_wrong_or_failed_report_refused),
]

failures = 0

print("========== REDACTOR TEST ==========\n")

for description, case in CASES:
    checks = case()
    passed = all(checks)
    failures += not passed

    print(f"{'PASS' if passed else 'FAIL'}  {description}")

    if not passed:
        print(f"        failed checks: {[number for number, ok in enumerate(checks, start=1) if not ok]}")

print(f"\n{len(CASES) - failures} of {len(CASES)} cases passed")
print("===================================")

sys.exit(1 if failures else 0)
