
from pathlib import Path
from io import BytesIO

from docx import Document
from PIL import Image, UnidentifiedImageError
import pytesseract
from pytesseract import Output


def extract_docx_images(file_path, output_dir="output/docx_images"):

    doc = Document(file_path)
    output_path = Path(output_dir)
    output_path.mkdir(parents=True, exist_ok=True)

    results = []

    # Extract images embedded in the DOCX package
    for index, image_part in enumerate(
        doc.part.package.image_parts, start=1
    ):

        image_bytes = image_part.blob

        # Skip formats Pillow cannot open (e.g. SVG, which Word stores
        # alongside a PNG copy of the same picture)
        try:
            image = Image.open(BytesIO(image_bytes)).convert("RGB")
        except UnidentifiedImageError:
            continue

        image_file = output_path / f"image_{index}.png"
        image.save(image_file)

        # OCR with word-level positions
        data = pytesseract.image_to_data(
            image,
            output_type=Output.DICT
        )

        words = []

        for i, raw_text in enumerate(data["text"]):
            text = raw_text.strip()

            if not text:
                continue

            words.append({
                "text": text,
                "x": data["left"][i],
                "y": data["top"][i],
                "width": data["width"][i],
                "height": data["height"][i],
                "confidence": data["conf"][i],
                "line_number": data["line_num"][i]
            })

        full_text = pytesseract.image_to_string(image).strip()

        results.append({
            "image_id": f"docx_image_{index}",
            "image_file": str(image_file),
            "width": image.width,
            "height": image.height,
            "text": full_text,
            "words": words,
            "word_count": len(words),
            "source": "docx_embedded_image",
            "ocr_engine": "Tesseract"
        })

    return results
