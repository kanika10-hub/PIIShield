import json

INPUT_FILE = "output/risk_policy.json"

with open(INPUT_FILE, "r", encoding="utf-8") as f:
    data = json.load(f)

print("\n========== HEADING VALIDATION ==========\n")

print("Settings:", data["metadata"].get("heading_classification"))

total = 0

for page in data["pages"]:

    headings = [
        block for block in page["blocks"]
        if block["block_type"] == "heading"
    ]

    if not headings:
        continue

    print(f"\nPage {page['page_number']}:")

    for block in headings:

        classification = block["metadata"]["classification"]

        indent = "  " * classification["level"]

        split = " (split from merged OCR paragraph)" if "split_from" in block["metadata"] else ""

        print(
            f"{indent}[L{classification['level']} {classification['confidence']}] "
            f"{block['text']}{split}"
        )

        total += 1

print("\n========================================")
print("Total headings:", total)
