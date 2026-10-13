
import json

from src.extractors.docx_image_extractor import extract_docx_images

INPUT_FILE = "input/tprm_training.docx"
OUTPUT_FILE = "output/tprm_images_ocr.json"

results = extract_docx_images(INPUT_FILE)

with open(OUTPUT_FILE, "w", encoding="utf-8") as file:
    json.dump(results, file, indent=2, ensure_ascii=False)

print("Image OCR completed.")
print("Images processed:", len(results))
print("Images containing text:", sum(
    1 for item in results if item["text"]
))
print("Output:", OUTPUT_FILE)

for item in results[:5]:
    print(f"\n{item['image_id']}:")
    print(item["text"][:300] or "[No text detected]")
