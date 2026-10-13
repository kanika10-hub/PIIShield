
import sys
import time
from collections import Counter

from src.models.document import ExtractedDocument
from src.pii.detectors import LocalNLPDetector
from src.pii.document_scanner import findings_path, save_findings, scan_document
from src.structure.pdf_tables import reconstruct_tables

# Rebuilds the tables in an already extracted PDF, without re-running OCR.
# tests/test_pdf_extractor.py does the same as part of a full extraction.
PDF_FILE = "input/risk_policy.pdf"
JSON_FILE = "output/risk_policy.json"


def all_blocks(blocks):
    for block in blocks:
        yield block
        yield from all_blocks(block.children)


def word_ids(document):
    return Counter(
        block.block_id
        for page in document.pages
        for block in all_blocks(page.blocks)
        if block.block_type == "word"
    )


document = ExtractedDocument.load_json(JSON_FILE)
words_before = word_ids(document)

started = time.perf_counter()
reconstruct_tables(document, PDF_FILE)
seconds = time.perf_counter() - started

tables = [
    (page.page_number, block)
    for page in document.pages
    for block in page.blocks
    if block.block_type == "table"
]

problems = []

# Every OCR word kept exactly once
if word_ids(document) != words_before:
    problems.append("OCR words were lost or duplicated")

ids = [block.block_id for page in document.pages for block in all_blocks(page.blocks)]

if len(ids) != len(set(ids)):
    problems.append("duplicate block IDs")

for page_number, table in tables:
    rows, columns = table.metadata["rows"], table.metadata["columns"]

    if columns < 2 or rows < 2:
        problems.append(f"{table.block_id}: {rows}x{columns} is not a table")

    header = [cell for cell in table.children if cell.metadata["row"] == 1]

    if len(header) != columns:
        problems.append(f"{table.block_id}: header row has {len(header)} of {columns} cells")

# Running again must not change anything
snapshot = document.to_dict()

if reconstruct_tables(document, PDF_FILE).to_dict() != snapshot:
    problems.append("a second run changed the document")

merged = sum(
    1 for _, table in tables for cell in table.children
    if cell.metadata["row_span"] > 1
)

print("========== PDF TABLE RECONSTRUCTION ==========\n")
print(f"Tables: {len(tables)} on {len({page for page, _ in tables})} pages ({seconds:.1f}s)")
print(f"Cells: {sum(len(table.children) for _, table in tables)} ({merged} merged across rows)")
print(f"OCR words moved into tables: {sum(len(cell.children) for _, table in tables for cell in table.children)}")

for page_number, table in tables:
    header = " | ".join(
        cell.text.replace("\n", " ")
        for cell in table.children
        if cell.metadata["row"] == 1
    )
    print(f"  {table.block_id:8} {table.metadata['rows']:>2}x{table.metadata['columns']}  {header[:90]}")

print("\nProblems:", problems or "none")

document.save_json(JSON_FILE)
print("Output:", JSON_FILE)

# The tables changed, so the PII findings are refreshed too
findings = scan_document(document, LocalNLPDetector())
save_findings(findings, findings_path(JSON_FILE))
print("PII findings:", findings["summary"]["total_findings"], findings["summary"]["by_type"])
print("PII output:", findings_path(JSON_FILE))

sys.exit(1 if problems else 0)
