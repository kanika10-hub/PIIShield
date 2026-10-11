import json

INPUT_FILE = "output/tprm_training_complete.json"

with open(INPUT_FILE, "r", encoding="utf-8") as file:
    data = json.load(file)

blocks = data["pages"][0]["blocks"]

print("\n========== DOCX COMPLETE VALIDATION ==========\n")

print("File:", data["file_name"])
print("Source:", data["source_type"])
print("Extraction method:", data["extraction_method"])
print("Total blocks:", len(blocks))

types = {}

for block in blocks:
    block_type = block["block_type"]
    types[block_type] = types.get(block_type, 0) + 1

print("\nBlock counts:")

for block_type, count in types.items():
    print(f"{block_type}: {count}")

image_blocks = [
    block for block in blocks
    if block["block_type"] == "image_ocr"
]

print("\nImage OCR blocks:", len(image_blocks))

for block in image_blocks[:5]:
    print(f"\n{block['block_id']}:")
    print("  Word positions:", len(block["metadata"].get("words", [])))
    print(" ", block["text"][:200])

print("\n==============================================")
