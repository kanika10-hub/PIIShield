import pymupdf
import pytesseract
from PIL import Image
import io
from pytesseract import Output

INPUT_FILE = "input/risk_policy.pdf"

# Open PDF
pdf = pymupdf.open(INPUT_FILE)

# First page only
page = pdf[0]

# Render page as image
pix = page.get_pixmap(matrix=pymupdf.Matrix(2, 2))

# Convert to PIL image
image = Image.open(io.BytesIO(pix.tobytes("png")))

# OCR with word-level information
data = pytesseract.image_to_data(
    image,
    output_type=Output.DICT
)

print("\n========== OCR WORDS ==========\n")

word_count = 0

for i in range(len(data["text"])):

    text = data["text"][i].strip()

    if text == "":
        continue

    x = data["left"][i]
    y = data["top"][i]
    width = data["width"][i]
    height = data["height"][i]
    confidence = data["conf"][i]

    print(
        f"WORD: {text} | "
        f"X: {x} | Y: {y} | "
        f"W: {width} | H: {height} | "
        f"CONF: {confidence}"
    )

    word_count += 1

print("\n================================")
print("Words detected:", word_count)
