"""
OCR for images embedded in documents.
"""

from io import BytesIO

import pytesseract
from PIL import Image
from pytesseract import Output

from src.models.document import DocumentBlock


def ocr_image(image):
    """
    Read the text of a PIL image in one Tesseract pass.

    Returns (text, words). text has one line per OCR line. words are
    the words of text in the same order, with their boxes in image
    pixels and their Tesseract confidence (0-100), so a position in the
    text can be traced back to a box in the image.
    """

    data = pytesseract.image_to_data(image, output_type=Output.DICT)

    lines = {}
    words = []

    for index, raw_text in enumerate(data["text"]):
        text = raw_text.strip()

        if not text:
            continue

        line = (data["block_num"][index], data["par_num"][index], data["line_num"][index])
        lines.setdefault(line, []).append(text)

        words.append({
            "text": text,
            "x": data["left"][index],
            "y": data["top"][index],
            "width": data["width"][index],
            "height": data["height"][index],
            "confidence": data["conf"][index],
            "line_number": data["line_num"][index]
        })

    text = "\n".join(" ".join(line_words) for line_words in lines.values())

    return text, words


class ImageReader:
    """
    Turns embedded images into image_ocr blocks. Each distinct image is
    OCR'd once per document: a logo in every header is read once.
    """

    def __init__(self):
        self.results = {}

    def block(self, block_id, key, image_bytes, bounding_box=None, metadata=None):
        """
        An image_ocr block with the image's OCR text and words, or an
        image block marked requires_ocr if the image cannot be read: a
        format Pillow cannot open (EMF, WMF), or Tesseract missing.

        key identifies the image within the document (its file part,
        its PDF object number).
        """

        if key not in self.results:
            try:
                image = Image.open(BytesIO(image_bytes)).convert("RGB")
                self.results[key] = (ocr_image(image), image.size)
            except (OSError, ValueError, Image.DecompressionBombError) as error:
                self.results[key] = error

        result = self.results[key]
        metadata = dict(metadata or {})

        if isinstance(result, Exception):
            return DocumentBlock(
                block_id=block_id,
                block_type="image",
                bounding_box=bounding_box,
                metadata={
                    **metadata,
                    "requires_ocr": True,
                    "ocr_error": f"{type(result).__name__}: {result}"
                }
            )

        (text, words), (width, height) = result
        confidences = [float(word["confidence"]) for word in words if float(word["confidence"]) >= 0]

        return DocumentBlock(
            block_id=block_id,
            block_type="image_ocr",
            text=text,
            bounding_box=bounding_box,
            metadata={
                **metadata,
                "source": "ocr",
                "ocr_engine": "Tesseract",
                "image_width": width,
                "image_height": height,
                "ocr_confidence": (
                    round(sum(confidences) / len(confidences), 1) if confidences else None
                ),
                "word_count": len(words),
                "words": words
            }
        )
