import json
from collections import Counter
from pathlib import Path

FILES = {
    "PPTX": "output/organizational_pack.json",
    "PDF": "output/risk_policy.json",
    "DOCX": "output/tprm_training.json",
}

REPORT_FILE = "output/extraction_validation_report.json"

LOW_CONFIDENCE = 50


def all_blocks(blocks):
    """
    Yield every block, including nested children (table cells, OCR words).
    """

    for block in blocks:
        yield block
        yield from all_blocks(block.get("children", []))


def bbox_is_valid(bbox):

    for key in ("x", "y", "width", "height"):
        value = bbox.get(key)
        if not isinstance(value, (int, float)) or value < 0:
            return False

    return True


def validate_document(file_type, file_path):

    report = {
        "file_type": file_type,
        "file_path": file_path,
        "status": "FAIL",
        "errors": [],
        "warnings": [],
        "metrics": {},
    }

    path = Path(file_path)

    if not path.exists():
        report["errors"].append("Output file does not exist.")
        return report

    try:
        with path.open("r", encoding="utf-8") as file:
            document = json.load(file)
    except (OSError, json.JSONDecodeError) as error:
        report["errors"].append(
            f"Could not read valid JSON: {error}"
        )
        return report

    # Pages and blocks
    pages = document.get("pages", [])

    if not pages:
        report["errors"].append("Document has no pages.")
        return report

    empty_pages = [page.get("page_number") for page in pages if not page.get("blocks")]

    if empty_pages:
        report["warnings"].append(f"Pages with no blocks: {empty_pages}")

    blocks = [block for page in pages for block in all_blocks(page.get("blocks", []))]

    if not blocks:
        report["errors"].append("Document has no blocks.")
        return report

    # Block IDs
    ids = [block.get("block_id") for block in blocks]

    missing_ids = sum(1 for block_id in ids if not block_id)
    duplicate_ids = sorted(
        block_id for block_id, count in Counter(ids).items()
        if block_id and count > 1
    )

    if missing_ids:
        report["errors"].append(f"{missing_ids} blocks have no block_id.")

    if duplicate_ids:
        report["errors"].append(
            f"{len(duplicate_ids)} duplicate block_ids, e.g. {duplicate_ids[:5]}"
        )

    # Order values
    missing_order = sum(1 for block in blocks if not isinstance(block.get("order"), int))

    if missing_order:
        report["errors"].append(f"{missing_order} blocks have no order value.")

    # Bounding boxes
    malformed = [
        block.get("block_id") for block in blocks
        if block.get("bounding_box") is not None and not bbox_is_valid(block["bounding_box"])
    ]

    if malformed:
        report["errors"].append(
            f"{len(malformed)} malformed bounding boxes, e.g. {malformed[:5]}"
        )

    # Empty table cells can be intentional.
    # Preserve them because blank RACI cells carry meaning.
    # Images and container blocks (e.g. tables) carry no text by design.
    empty_blocks_by_type = {}

    for block in blocks:
        if block.get("block_type") == "image" or block.get("children"):
            continue
        if not block.get("text", "").strip():
            block_type = block.get("block_type", "unknown")
            empty_blocks_by_type[block_type] = (
                empty_blocks_by_type.get(block_type, 0) + 1
            )

    # Exclude table_cell blocks from the unexpected-empty warning.
    unexpected_empty_blocks = {
        block_type: count
        for block_type, count in empty_blocks_by_type.items()
        if block_type != "table_cell"
    }

    if unexpected_empty_blocks:
        report["warnings"].append(
            "Unexpected empty blocks by type: "
            f"{unexpected_empty_blocks}"
        )

    report["metrics"]["empty_blocks_by_type"] = empty_blocks_by_type
    report["metrics"]["intentional_blank_table_cells_preserved"] = (
        empty_blocks_by_type.get("table_cell", 0)
    )

    # Missing location information
    no_location = {}

    for block in blocks:
        if block.get("bounding_box") is None:
            no_location[block.get("block_type")] = no_location.get(block.get("block_type"), 0) + 1

    if no_location:
        report["warnings"].append(f"Blocks without a bounding box, by type: {no_location}")

    # OCR confidence (PDF word blocks and DOCX image OCR words)
    confidences = [
        block["metadata"]["ocr_confidence"]
        for block in blocks
        if block.get("block_type") == "word" and "ocr_confidence" in block.get("metadata", {})
    ]

    for block in blocks:
        for word in block.get("metadata", {}).get("words", []):
            if "confidence" in word:
                confidences.append(word["confidence"])

    if confidences:
        low = sum(1 for value in confidences if value < LOW_CONFIDENCE)

        report["metrics"]["ocr_words"] = len(confidences)
        report["metrics"]["ocr_mean_confidence"] = round(sum(confidences) / len(confidences), 1)
        report["metrics"]["ocr_low_confidence_words"] = low

        if low:
            report["warnings"].append(
                f"{low} of {len(confidences)} OCR words have confidence below {LOW_CONFIDENCE}."
            )

    # Known limitations recorded by the extractor itself
    limitations = document.get("metadata", {}).get("limitations", [])

    if limitations:
        report["warnings"].append(f"Known extractor limitations: {limitations}")

    # Metrics
    types = {}

    for block in blocks:
        types[block.get("block_type")] = types.get(block.get("block_type"), 0) + 1

    report["metrics"]["pages"] = len(pages)
    report["metrics"]["blocks_including_children"] = len(blocks)
    report["metrics"]["block_types"] = types

    if report["errors"]:
        report["status"] = "FAIL"
    elif report["warnings"]:
        report["status"] = "WARN"
    else:
        report["status"] = "PASS"

    return report


def main():

    reports = [
        validate_document(file_type, file_path)
        for file_type, file_path in FILES.items()
    ]

    Path(REPORT_FILE).parent.mkdir(parents=True, exist_ok=True)

    with open(REPORT_FILE, "w", encoding="utf-8") as file:
        json.dump(reports, file, indent=2, ensure_ascii=False)

    print("\n========== EXTRACTION VALIDATION ==========")

    for report in reports:

        print(f"\n[{report['status']}] {report['file_type']} - {report['file_path']}")

        for error in report["errors"]:
            print(f"  ERROR:   {error}")

        for warning in report["warnings"]:
            print(f"  WARNING: {warning}")

        for name, value in report["metrics"].items():
            print(f"  {name}: {value}")

    print("\n===========================================")
    print("Report:", REPORT_FILE)


if __name__ == "__main__":
    main()
