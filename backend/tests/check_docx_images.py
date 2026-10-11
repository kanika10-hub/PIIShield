
from docx import Document

INPUT_FILE = "input/tprm_training.docx"

doc = Document(INPUT_FILE)

print("========== DOCX IMAGE CHECK ==========")
print("Inline images:", len(doc.inline_shapes))

for index, shape in enumerate(doc.inline_shapes, start=1):
    print(
        f"Image {index}: "
        f"width={shape.width}, "
        f"height={shape.height}"
    )

print("======================================")
