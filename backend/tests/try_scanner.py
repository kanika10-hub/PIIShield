from src.pii.detectors import LocalNLPDetector
from src.pii.document_scanner import scan_document


# Edit the cells below to try the scanner on any table layout


def cell(row, column, text, row_span=1, column_span=1):
    return {
        "block_id": f"t1_row_{row}_cell_{column}",
        "block_type": "table_cell",
        "text": text,
        "metadata": {"row": row, "column": column, "row_span": row_span, "column_span": column_span},
        "bounding_box": None,
        "children": [],
    }


document = {
    "document_id": "demo",
    "file_name": "demo.docx",
    "source_type": "DOCX",
    "pages": [{
        "page_number": 1,
        "blocks": [
            {"block_id": "p1", "block_type": "paragraph", "metadata": {}, "children": [],
             "text": "The register below is maintained by Marcus Delgado-Reyes."},
            {"block_id": "t1", "block_type": "table", "text": "",
             "metadata": {"rows": 3, "columns": 3},
             "children": [
                 cell(1, 1, "Staff Member"), cell(1, 2, "Passport Number"), cell(1, 3, "Mobile"),
                 cell(2, 1, "Marcus Delgado-Reyes"), cell(2, 2, "K8274615"), cell(2, 3, "9876543210"),
                 cell(3, 1, "Delgado-Reyes (backup)"), cell(3, 2, "TK4820913"), cell(3, 3, "-"),
             ]},
        ],
    }],
}

result = scan_document(document, LocalNLPDetector())

print("Summary:", result["summary"])
print("Table:", {k: result["tables"][0][k] for k in ("title_rows", "header_rows", "column_headers")})

for f in result["findings"]:
    print(f"  {f['block_id']:18} {f['type']:16} {f['text']!r:24} {f['confidence']}  {f['detector']:10} context: {f['context']!r}")
