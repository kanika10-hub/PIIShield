"""
Text inside images: the OCR words that hold a value are filled with a
solid black box, nothing written in it (OCR reads nothing back from it).
The pixels themselves change, so the value is gone from the image file,
not hidden behind a shape.

    covered_words(text, words, spans)   the OCR words each span touches
    line_boxes(boxes)                   word boxes merged into one per line
    span_areas(...)                     the areas to fill for each span
    redact_image(data, areas)           the image bytes with areas filled
"""

import re
from io import BytesIO

from PIL import Image, ImageDraw


FILL = (0, 0, 0)            # black: a solid redaction box

# OCR word boxes are tight around the letters: anti-aliased edges and
# accents can sit just outside them
PAD = 2                     # pixels

# Formats Pillow writes back as they were read. Others (EMF, WMF) are
# never OCR'd, so nothing in them is ever placed.
WRITABLE = {"PNG", "JPEG", "GIF", "BMP", "TIFF", "WEBP"}


def covered_words(text, words, spans):
    """
    For each span (start, end, tag) of text, the words (dicts with x, y,
    width, height) it touches. words are one per whitespace-separated
    token of text, in order (the OCR words of an image, the word children
    of a PDF block). None if they are not: no span can then be placed.
    """

    tokens = list(re.finditer(r"\S+", text))

    if [token.group() for token in tokens] != [word.get("text") for word in words]:
        return None

    return [
        [word for token, word in zip(tokens, words) if token.start() < end and start < token.end()]
        for start, end, _ in spans
    ]


def box_of(word):
    return (word["x"], word["y"], word["x"] + word["width"], word["y"] + word["height"])


def line_boxes(boxes):
    """
    Word boxes (x0, y0, x1, y1), in reading order, merged into one box
    per line: a word continues the line when it overlaps it by half its
    height and does not start left of it.
    """

    lines = []

    for box in boxes:
        if lines:
            x0, y0, x1, y1 = lines[-1]
            overlap = min(y1, box[3]) - max(y0, box[1])

            if overlap > 0.5 * min(y1 - y0, box[3] - box[1]) and box[0] >= x0:
                lines[-1] = (min(x0, box[0]), min(y0, box[1]), max(x1, box[2]), max(y1, box[3]))
                continue

        lines.append(tuple(box))

    return lines


def span_areas(spans, words_per_span):
    """
    (box, tag) areas for spans whose words were found: a span over two
    lines fills both, and its tag goes in the first.
    """

    areas = []

    for (_, _, tag), words in zip(spans, words_per_span):
        for number, box in enumerate(line_boxes([box_of(word) for word in words])):
            areas.append((box, tag if number == 0 else None))

    return areas


def redact_image(data, areas):
    """
    The image with each area ((x0, y0, x1, y1) in pixels, and the tag of
    its value, not drawn) filled black, in the format it was in. Raises
    ValueError if the image cannot be read or written back.
    """

    try:
        image = Image.open(BytesIO(data))
        image.load()
    except (OSError, Image.DecompressionBombError) as error:
        raise ValueError(f"the image cannot be read: {error}") from error

    image_format = image.format

    if image_format not in WRITABLE:
        raise ValueError(f"{image_format} images cannot be written back")

    transparent = image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info
    canvas = image.convert("RGBA" if transparent else "RGB")
    draw = ImageDraw.Draw(canvas)

    for (x0, y0, x1, y1), _ in areas:
        box = (
            max(0, int(x0) - PAD), max(0, int(y0) - PAD),
            min(canvas.width, int(x1 + 0.999) + PAD), min(canvas.height, int(y1 + 0.999) + PAD)
        )
        draw.rectangle(box, fill=FILL)

    options = {"dpi": image.info["dpi"]} if "dpi" in image.info else {}
    output = BytesIO()

    if image_format == "JPEG":
        canvas.convert("RGB").save(output, "JPEG", quality=95, **options)
    elif image_format == "GIF":
        canvas.convert("RGB").convert("P", palette=Image.Palette.ADAPTIVE).save(output, "GIF")
    elif image_format == "BMP":
        canvas.convert("RGB").save(output, "BMP")
    else:
        canvas.save(output, image_format, **options)

    return output.getvalue()


def image_size(data):
    """
    (width, height) in pixels, or None if Pillow cannot read the image.
    """

    try:
        with Image.open(BytesIO(data)) as image:
            return image.size
    except (OSError, Image.DecompressionBombError):
        return None
