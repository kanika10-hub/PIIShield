import json
from pathlib import Path

DOCX_FILE = "output/tprm_training.json"
IMAGE_OCR_FILE = "output/tprm_images_ocr.json"
OUTPUT_FILE = "output/tprm_training_complete.json"


def merge_docx_content(docx_file=DOCX_FILE, image_ocr_file=IMAGE_OCR_FILE,
                       output_file=OUTPUT_FILE):

    with open(docx_file, "r", encoding="utf-8") as file:
        document = json.load(file)

    with open(image_ocr_file, "r", encoding="utf-8") as file:
        images = json.load(file)

    # Add OCR text from embedded images as separate blocks
    blocks = document["pages"][0]["blocks"]

    native_block_count = len(blocks)

    next_order = max(
        (block["order"] for block in blocks),
        default=0
    ) + 1

    images_with_text = 0

    for image in images:

        if not image["text"].strip():
            continue

        block = {
            "block_id": image["image_id"],
            "block_type": "image_ocr",
            "text": image["text"],
            "order": next_order,
            "metadata": {
                "source": image["source"],
                "image_file": image["image_file"],
                "ocr_engine": image["ocr_engine"],
                "word_count": image["word_count"],
                # Word positions are relative to the image, not the Word page
                "words": image["words"]
            },
            "bounding_box": None,
            "children": []
        }

        blocks.append(block)

        next_order += 1
        images_with_text += 1

    # Image text is now extracted, so it is no longer a limitation
    limitations = document["metadata"].get("limitations", [])

    if "Embedded image text is not extracted yet" in limitations:
        limitations.remove("Embedded image text is not extracted yet")

    Path(output_file).parent.mkdir(parents=True, exist_ok=True)

    with open(output_file, "w", encoding="utf-8") as file:
        json.dump(document, file, indent=2, ensure_ascii=False)

    print("DOCX content merge completed.")
    print("Native blocks:", native_block_count)
    print("Images processed:", len(images))
    print("Images with text:", images_with_text)
    print("Output:", output_file)

    return document


if __name__ == "__main__":
    merge_docx_content()
