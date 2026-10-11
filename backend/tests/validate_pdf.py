import json

INPUT_FILE = "output/risk_policy.json"

with open(INPUT_FILE, "r", encoding="utf-8") as f:
    data = json.load(f)

print("\n========== PDF VALIDATION ==========\n")

print("File:", data["file_name"])
print("Source type:", data["source_type"])
print("Extraction method:", data["extraction_method"])
print("Pages:", len(data["pages"]))

total_blocks = 0

for page in data["pages"]:

    page_number = page["page_number"]
    blocks = page["blocks"]

    total_blocks += len(blocks)

    print(
        f"\nPage {page_number}: "
        f"{len(blocks)} OCR blocks"
    )

    # Show first 5 OCR blocks
    for block in blocks[:5]:

        # Rebuilt tables hold their text and confidence in the cells
        if block["block_type"] == "table":
            print(
                f"  [{block['block_id']}] table "
                f"{block['metadata']['rows']}x{block['metadata']['columns']}"
            )
            continue

        print(
            f"  [{block['block_id']}] "
            f"{block['text']} "
            f"(confidence: "
            f"{block['metadata']['ocr_confidence']})"
        )

print("\n====================================")
print("Total OCR blocks:", total_blocks)
