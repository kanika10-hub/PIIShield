import json


INPUT_FILE = "output/organizational_pack.json"


def main():

    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        document = json.load(f)

    print("\n========== PPTX VALIDATION ==========\n")

    print("File:")
    print(document["file_name"])

    print("\nSource type:")
    print(document["source_type"])

    print("\nExtraction method:")
    print(document["extraction_method"])

    print("\nNumber of slides:")
    print(len(document["pages"]))

    print("\n-------------------------------------")

    for page in document["pages"]:

        slide_number = page["page_number"]
        blocks = page["blocks"]

        print(f"\nSLIDE {slide_number}")
        print(f"Blocks: {len(blocks)}")

        for block in blocks:

            text = block["text"].replace("\n", " / ")

            print(f"\n  {block['order']}. [{block['block_type']}] {text}")

            bbox = block["bounding_box"]

            if bbox:
                print(
                    f"     position: x={bbox['x']}, y={bbox['y']}, "
                    f"width={bbox['width']}, height={bbox['height']}"
                )

            if block["block_type"] == "image":
                print(f"     requires_ocr: {block['metadata'].get('requires_ocr')}")

            if block["block_type"] == "table":

                print(f"     Table cells: {len(block['children'])}")

                for cell in block["children"]:

                    cell_text = cell["text"].replace("\n", " / ")
                    row = cell["metadata"]["row"]
                    column = cell["metadata"]["column"]

                    print(f"       [table_cell] r{row}c{column}: {cell_text}")


if __name__ == "__main__":
    main()
