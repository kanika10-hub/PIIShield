
import json
from pathlib import Path

path = Path("output/risk_policy.json")

with path.open("r", encoding="utf-8") as file:
    document = json.load(file)


def walk(blocks):
    for block in blocks:
        yield block
        yield from walk(block.get("children", []))


print("========== PDF STRUCTURE INSPECTION ==========")
print("File:", document.get("file_name"))
print("Source:", document.get("source_type"))

pages = document.get("pages", [])
print("Total pages:", len(pages))

if not pages:
    raise SystemExit("No pages found in the JSON.")

page = pages[0]
blocks = list(walk(page.get("blocks", [])))

print("\nPage 1 block types:")
counts = {}

for block in blocks:
    block_type = block.get("block_type", "unknown")
    counts[block_type] = counts.get(block_type, 0) + 1

print(counts)

print("\nFirst 10 word blocks:")

words = [b for b in blocks if b.get("block_type") == "word"]

for block in words[:10]:
    print({
        "text": block.get("text"),
        "bounding_box": block.get("bounding_box"),
        "metadata": block.get("metadata", {})
    })

print("\nPage metadata:")
print(page.get("metadata", {}))

print("==============================================")
