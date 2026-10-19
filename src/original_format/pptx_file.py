"""
PowerPoint: the redacted copy of a PPTX file.

The file is read again by the PPTX extractor in locate mode
(text_sources.SourceMap), so each block's text maps back, character by
character, to the a:t elements it came from. A file whose text is not
the extracted document's is refused. Each finding is replaced there by
its tag, at the scanner's offsets: the tag takes the formatting of the
run the value starts in. Then:

    file properties     the values the scan found in them
    comment authors     their names as the scan redacted them, initials
                        cleared, and every threaded comment author's
                        account (userId: often an email) cleared
    images              the OCR words of each value are filled (images.py),
                        slide template images (logos, backdrops) too
    charts, SmartArt    their values replaced in every part that holds
                        them (the chart's workbook, the diagram's drawing)
    every part          searched for every value the scan found (plan.py,
                        ooxml.sweep_tree): slide masters and layouts,
                        comments, alt text, hyperlink targets, app
                        properties, embedded packages
    SVG copies          of pictures removed: the redacted pixels show
    thumbnail           the preview picture of the first slide is removed

    redact_pptx(source_path, plan, output_path) -> (summary, errors)
"""

from pptx import Presentation
from pptx.opc.constants import RELATIONSHIP_TYPE as RT

from src.extractors.pptx_extractor import P188_NS, THREADED_AUTHORS, slide_blocks
from src.extractors.text_sources import ImageSource, PartsSource, SourceMap, TextSource
from src.original_format.docx_file import check_located
from src.original_format.images import redact_image
from src.original_format.ooxml import (
    PartTrees, Unread, drop_svg_copies, finder, image_areas, located_pieces, replace_pieces, sweep_part,
    sweep_relationships
)
from src.original_format.plan import ValueMatcher, span_triples, sweep_values


def set_author(author, name):
    """
    A comment author's name, and its initials, which would give it away.
    """

    author.set("name", name)

    if author.get("initials") is not None:
        author.set("initials", "")


def clear_accounts(presentation, trees):
    """
    The account of every threaded comment author (userId: an email or a
    sign-in name), which nothing reads or scans. Returns the number
    cleared.
    """

    count = 0

    for rel in presentation.part.rels.values():
        if rel.is_external or rel.reltype != THREADED_AUTHORS:
            continue

        root = trees.root(rel.target_part)

        for author in root.iter(f"{{{P188_NS}}}author") if root is not None else ():
            if author.get("userId"):
                author.set("userId", "")
                trees.touch(rel.target_part)
                count += 1

    return count


def redact_pptx(source_path, plan, output_path):
    """
    Write the redacted copy of the PPTX file the plan's document was
    extracted from. Returns (summary, errors): the counts of what was
    replaced, and each finding that could not be (block_id, finding_id,
    reason, never the value). Raises ValueError if the file does not
    match the document.
    """

    presentation = Presentation(str(source_path))
    sources = SourceMap()
    reader = Unread()
    seen = set()

    for slide_number, slide in enumerate(presentation.slides, start=1):
        slide_blocks(slide, slide_number, reader, sources, seen)

    check_located(sources, plan, source_path.name)

    trees = PartTrees(sources.trees)
    part_values = {}                # partname -> value -> tag, for charts and diagrams
    image_parts = {}                # partname -> (part, areas, block_ids)
    errors = []
    summary = {"spans_replaced": 0, "images_redacted": 0, "values_swept": 0, "svg_copies_removed": 0}

    def failed(block_id, spans, reason):
        errors.extend(
            {"block_id": block_id, "finding_id": item.get("finding_id"), "reason": reason}
            for span in spans for item in span["items"]
        )

    for block_id, spans in plan.spans.items():
        source = sources.blocks.get(block_id)

        if block_id.startswith("property_"):
            setattr(presentation.core_properties, block_id[len("property_"):], plan.replaced_slots[block_id])
            summary["spans_replaced"] += len(spans)

        elif block_id in plan.replaced_slots and source is not None:
            set_author(source, plan.replaced_slots[block_id])
            summary["spans_replaced"] += len(spans)

        elif isinstance(source, TextSource):
            try:
                replace_pieces(located_pieces(source), span_triples(spans))
            except ValueError as error:
                failed(block_id, spans, str(error))
                continue

            summary["spans_replaced"] += len(spans)

        elif isinstance(source, PartsSource):
            text = plan.blocks[block_id][1].get("text") or ""

            for part in source.parts:
                values = part_values.setdefault(str(part.partname), {})

                for start, end, tag in span_triples(spans):
                    values.setdefault(text[start:end], tag)

            summary["spans_replaced"] += len(spans)

        elif isinstance(source, ImageSource):
            areas = image_areas(plan.blocks[block_id][1], spans)

            if areas is None:
                failed(block_id, spans, "its OCR words do not match its text, so the values cannot be placed")
                continue

            _, all_areas, block_ids = image_parts.setdefault(str(source.part.partname), (source.part, [], []))
            all_areas.extend(areas)
            block_ids.append((block_id, spans))

        else:
            failed(block_id, spans, "the block was not found in the file")

    for part, areas, block_ids in image_parts.values():
        try:
            part._blob = redact_image(part.blob, areas)
            summary["images_redacted"] += 1
            summary["spans_replaced"] += sum(len(spans) for _, spans in block_ids)
        except ValueError as error:
            for block_id, spans in block_ids:
                failed(block_id, spans, str(error))

    # Every value the scan found, wherever else it is in the file
    base_values = sweep_values(plan.report["findings"])
    package = presentation.part.package

    for part in package.iter_parts():
        extra = part_values.get(str(part.partname))
        matcher = ValueMatcher({**base_values, **extra}) if extra else plan.values
        find = finder(matcher, plan)

        if matcher:
            summary["values_swept"] += sweep_part(part, trees, find)
            summary["values_swept"] += sweep_relationships(part.rels, find)

    if plan.values:
        summary["values_swept"] += sweep_relationships(package._rels, finder(plan.values, plan))

    # Office would show a picture's SVG copy, not its redacted pixels
    for part in list(package.iter_parts()):
        summary["svg_copies_removed"] += drop_svg_copies(part, trees)

    summary["values_swept"] += clear_accounts(presentation, trees)
    trees.write_back()

    # A picture of the first slide, as it was
    for rel_id in [rel_id for rel_id, rel in package._rels.items() if rel.reltype == RT.THUMBNAIL]:
        package.drop_rel(rel_id)

    presentation.save(str(output_path))

    return summary, errors
