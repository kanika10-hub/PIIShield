
import json
from collections import Counter

INPUT_FILE = "output/tprm_training.json"

with open(INPUT_FILE, "r", encoding="utf-8") as file:
    data = json.load(file)

print("\n========== DOCX VALIDATION ==========\n")

print("File:", data["file_name"])
print("Source type:", data["source_type"])
print("Extraction method:", data["extraction_method"])
print("Pages/sections:", len(data["pages"]))

blocks = data["pages"][0]["blocks"]

block_types = Counter(block["block_type"] for block in blocks)

tables = [block for block in blocks if block["block_type"] == "table"]

cells = [cell for table in tables for cell in table["children"]]

print("\nTop-level blocks:", len(blocks))

for block_type, count in sorted(block_types.items()):
    print(f"  {block_type}: {count}")

print("Table cell blocks (inside tables):", len(cells))
print("Empty table cells:", sum(1 for cell in cells if not cell["text"]))
print("Merged table cells:", sum(
    1 for cell in cells
    if cell["metadata"]["row_span"] > 1 or cell["metadata"]["column_span"] > 1
))

# Top-level order values must run 1..N in sequence
orders = [block["order"] for block in blocks]

if orders == list(range(1, len(blocks) + 1)):
    print("\nReading order: OK (1 to", len(blocks), "with no gaps)")
else:
    print("\nReading order: FAIL - order values are not sequential")

# Cells must sit inside a table, never at the top level
if block_types.get("table_cell"):
    print("Table structure: FAIL -", block_types["table_cell"], "cells at top level")
else:
    print("Table structure: OK (all cells nested in tables)")

print("\nFirst 15 blocks in reading order:")

for block in blocks[:15]:

    if block["block_type"] == "table":
        metadata = block["metadata"]
        first_text = next(
            (cell["text"] for cell in block["children"] if cell["text"]),
            ""
        )
        print(
            f"{block['order']}: [table {metadata['rows']}x{metadata['columns']}, "
            f"{len(block['children'])} cells] {first_text[:80]}"
        )
    else:
        print(f"{block['order']}: [{block['block_type']}] {block['text'][:100]}")

print("\nSample table cells:")

for cell in [cell for cell in cells if cell["text"]][:10]:
    print("-", cell["text"][:120])

print("\n====================================")
