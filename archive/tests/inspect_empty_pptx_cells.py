import json

INPUT_FILE = "output/organizational_pack.json"


def inspect_blocks(blocks, location=""):

    for block in blocks:

        block_type = block.get("block_type", "")
        text = block.get("text", "")
        block_id = block.get("block_id", "unknown")

        if block_type == "table_cell" and not text.strip():
            print(
                f"Empty cell: {block_id} | "
                f"Location: {location} | "
                f"Metadata: {block.get('metadata', {})}"
            )

        children = block.get("children", [])

        if children:
            inspect_blocks(children, block_id)


with open(INPUT_FILE, "r", encoding="utf-8") as file:
    document = json.load(file)

for page in document.get("pages", []):

    page_number = page.get("page_number")

    inspect_blocks(
        page.get("blocks", []),
        location=f"slide_{page_number}"
    )
