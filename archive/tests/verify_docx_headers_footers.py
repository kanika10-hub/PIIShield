import json
from collections import Counter
from pathlib import Path

path = Path("output/tprm_training.json")

data = json.loads(path.read_text(encoding="utf-8"))


def walk(blocks):
    for block in blocks:
        yield block
        yield from walk(block.get("children", []))


blocks = []

for page in data.get("pages", []):
    blocks.extend(walk(page.get("blocks", [])))

# Image blocks carry source "docx_header_footer" but no source_part
header_footer = [
    block for block in blocks
    if block.get("metadata", {}).get("source_part")
    or block.get("metadata", {}).get("source") == "docx_header_footer"
    or block.get("block_type") in (
        "header_footer_reference",
        "header_footer"
    )
]

print("========== HEADER/FOOTER VERIFICATION ==========")
print("File:", path)
print("Total top-level and nested blocks:", len(blocks))
print("Header/footer-related blocks:", len(header_footer))
print("By type:", dict(Counter(block.get("block_type") for block in header_footer)))

for block in header_footer:
    metadata = block.get("metadata", {})
    print(
        block.get("block_id"),
        "| type:", block.get("block_type"),
        "| section:", metadata.get("section"),
        "| part:", metadata.get("part_type"),
        "| visible:", metadata.get("visible"),
        "| text:", (block.get("text") or metadata.get("image_part") or "")[:80]
    )

print("================================================")
