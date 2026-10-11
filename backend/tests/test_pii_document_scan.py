
import json
import sys
import time

from src.pii.detectors import LocalNLPDetector, detect_pii
from src.pii.document_scanner import DocumentNLP, scan_file


def all_blocks(blocks):
    for block in blocks:
        yield block
        yield from all_blocks(block.get("children", []))

FILES = [
    "output/tprm_training.json",
    "output/organizational_pack.json",
    "output/risk_policy.json",
]

# Header decisions expected on the real tables (table_id: header rows)
EXPECTED_HEADER_ROWS = {
    # Cover page and footer layout tables have no header
    "table_1": [], "table_2": [], "table_3": [], "table_16": [],
    # Data tables with a header row on top
    **{f"table_{number}": [1] for number in range(4, 15)},
    # RACI matrices and the contact directory
    "slide6_table6": [1], "slide7_table6": [1], "slide8_table6": [1],
}

nlp = LocalNLPDetector()
print("NLP status:", nlp.error or "Model loaded")

failures = 0

for path in FILES:

    started = time.perf_counter()
    result = scan_file(path, nlp_detector=nlp)
    seconds = time.perf_counter() - started

    summary = result["summary"]

    print(f"\n========== {result['file_name']} ({result['source_type']}) ==========")
    print(
        f"Blocks scanned: {summary['blocks_scanned']} "
        f"({summary['unique_texts_scanned']} unique) in {seconds:.2f}s"
    )
    print("Findings:", summary["total_findings"], summary["by_type"])
    print("Tables:", len(result["tables"]))

    for table in result["tables"]:
        expected = EXPECTED_HEADER_ROWS.get(table["table_id"])
        status = ""

        if expected is not None:
            ok = table["header_rows"] == expected
            failures += not ok
            status = "OK  " if ok else f"FAIL (expected {expected}) "

        headers = "; ".join(
            header[:25] for header in table["column_headers"].values()
        )

        print(
            f"  {status}{table['table_id']} {table['rows']}x{table['columns']} "
            f"title={table['title_rows']} header={table['header_rows']} [{headers}]"
        )

    # Findings in table cells that the cell text alone would not produce
    with open(path, "r", encoding="utf-8") as file:
        document = json.load(file)

    texts = {
        block["block_id"]: block.get("text", "")
        for page in document["pages"]
        for block in all_blocks(page["blocks"])
    }

    # Same NLP set-up as the scan (document-wide ordinary words), but
    # spaCy only on the blocks that have findings in context
    with_context = list(dict.fromkeys(
        texts[item["block_id"]] for item in result["findings"] if item["context"]
    ))
    plain_nlp = None

    if nlp.nlp is not None:
        plain_nlp = DocumentNLP(nlp, with_context)
        plain_nlp.common_words = nlp.common_words([
            block["text"]
            for page in document["pages"]
            for block in all_blocks(page["blocks"])
            if block["block_type"] not in ("word", "table") and block.get("text", "").strip()
        ])

    plain_results = {text: detect_pii(text, plain_nlp) for text in with_context}

    # Surname mentions come from the document-wide pass, not from context
    from_context = [
        item for item in result["findings"]
        if item["context"]
        and item["detector"] != "surname"
        and not any(
            plain["type"] == item["type"]
            and plain["start"] == item["start"]
            and plain["end"] == item["end"]
            for plain in plain_results[texts[item["block_id"]]]
        )
    ]

    print("Findings that needed table context:", len(from_context))

    for item in from_context[:10]:
        print(f"  {item['type']} {item['text']!r} ({item['confidence']}) context: {item['context'][:70]!r}")

print(f"\nHeader checks failed: {failures}")

sys.exit(1 if failures else 0)
