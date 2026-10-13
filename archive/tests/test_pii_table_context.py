
import sys

from src.pii.document_scanner import analyse_table, scan_document


def make_document(table_id, rows, columns, cells):
    """
    Build a one-table document in the extractors' JSON format.
    cells: (row, column, text) or (row, column, text, row_span, column_span)
    """

    children = []

    for order, cell in enumerate(cells, start=1):
        row, column, text, row_span, column_span = (*cell, 1, 1)[:5]

        children.append({
            "block_id": f"{table_id}_row_{row}_cell_{column}",
            "block_type": "table_cell",
            "text": text,
            "order": order,
            "metadata": {
                "row": row,
                "column": column,
                "row_span": row_span,
                "column_span": column_span
            },
            "bounding_box": None,
            "children": []
        })

    return {
        "document_id": "table_context_test",
        "file_name": "test.docx",
        "source_type": "DOCX",
        "pages": [{
            "page_number": 1,
            "blocks": [{
                "block_id": table_id,
                "block_type": "table",
                "text": "",
                "metadata": {"rows": rows, "columns": columns},
                "children": children
            }]
        }]
    }


# (description, document, expected header rows, expected findings as (type, text, row, column))
CASES = [
    (
        "Header row: passport, employee ID and free-text columns",
        make_document("t1", 2, 3, [
            (1, 1, "Passport Number"), (1, 2, "Employee ID"), (1, 3, "Description"),
            (2, 1, "TK4820913"), (2, 2, "CFG-20931"), (2, 3, "TK4820913"),
        ]),
        [1],
        {
            ("PASSPORT_NUMBER", "TK4820913", 2, 1),
            ("EMPLOYEE_ID", "CFG-20931", 2, 2),
        },
    ),
    (
        "Title row, then a two-row header with a group heading",
        make_document("t2", 4, 3, [
            (1, 1, "Staff Identity Register", 1, 3),
            (2, 1, "Name", 2, 1), (2, 2, "Identity documents", 1, 2),
            (3, 2, "Passport No."), (3, 3, "Employee ID"),
            (4, 1, "Adaeze Onwuka"), (4, 2, "K8274615"), (4, 3, "CFG-20931"),
        ]),
        [2, 3],
        {
            ("PASSPORT_NUMBER", "K8274615", 4, 2),
            ("EMPLOYEE_ID", "CFG-20931", 4, 3),
        },
    ),
    (
        "Label/value table: the row label is the context",
        make_document("t3", 4, 2, [
            (1, 1, "Third-Party Risk Assessment", 1, 2),
            (2, 1, "Client Name"), (2, 2, "Cadence"),
            (3, 1, "Mobile"), (3, 2, "9876543210"),
            (4, 1, "SSN"), (4, 2, "900-42-7781"),
        ]),
        [2],
        {
            ("PHONE", "9876543210", 3, 2),
            ("US_SSN", "900-42-7781", 4, 2),
        },
    ),
    (
        "No header: the first row is data",
        make_document("t4", 2, 2, [
            (1, 1, "+1 (212) 555-0147"), (1, 2, "m.achebe@cadencefg.com"),
            (2, 1, "+44 20 7555 0171"), (2, 2, "t.lindqvist@cadencefg.com"),
        ]),
        [],
        {
            ("PHONE", "+1 (212) 555-0147", 1, 1),
            ("EMAIL", "m.achebe@cadencefg.com", 1, 2),
            ("PHONE", "+44 20 7555 0171", 2, 1),
            ("EMAIL", "t.lindqvist@cadencefg.com", 2, 2),
        },
    ),
    (
        "A non-ID employee header gives no ID context",
        make_document("t5", 2, 2, [
            (1, 1, "Division"), (1, 2, "Employee count"),
            (2, 1, "Operations"), (2, 2, "1500"),
        ]),
        [1],
        set(),
    ),
]

failures = 0

print("========== TABLE CONTEXT TEST ==========\n")

for description, document, expected_headers, expected in CASES:

    result = scan_document(document)
    table = result["tables"][0]

    actual = {
        (item["type"], item["text"], item["row"], item["column"])
        for item in result["findings"]
    }

    # Title and header cells get only the table title as context: the
    # first header cell is a column header, not a label for its neighbours
    table_block = document["pages"][0]["blocks"][0]
    analysis = analyse_table(table_block)
    top_rows = set(analysis["title_rows"]) | set(analysis["header_rows"])

    title = " ".join(
        " ".join(cell["text"].split())
        for cell in table_block["children"]
        if cell["metadata"]["row"] in analysis["title_rows"] and cell["text"].strip()
    )

    leaked = [
        cell["block_id"]
        for cell in table_block["children"]
        if cell["metadata"]["row"] in top_rows
        and analysis["contexts"][cell["block_id"]] != title
    ]

    passed = table["header_rows"] == expected_headers and actual == expected and not leaked
    failures += not passed

    print(f"{'PASS' if passed else 'FAIL'}  {description}")
    print(f"        title rows: {table['title_rows']}  header rows: {table['header_rows']}")
    print(f"        column headers: {table['column_headers']}")

    for item in result["findings"]:
        print(
            f"        r{item['row']}c{item['column']} {item['type']} "
            f"{item['text']!r} ({item['confidence']}) context: {item['context']!r}"
        )

    if table["header_rows"] != expected_headers:
        print(f"        expected header rows: {expected_headers}")

    for missing in sorted(expected - actual):
        print(f"        missed:     {missing}")

    for extra in sorted(actual - expected):
        print(f"        unexpected: {extra}")

    for cell_id in leaked:
        print(f"        header cell with a row label: {cell_id} {analysis['contexts'][cell_id]!r}")

print(f"\n{len(CASES) - failures} of {len(CASES)} cases passed")
print("========================================")

sys.exit(1 if failures else 0)
