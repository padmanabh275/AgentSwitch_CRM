"""Convert the gap analysis markdown into a styled Word document.

Handles the subset of Markdown used by docs/gap-analysis.md: ATX headings,
pipe tables with alignment, blockquotes, ordered and unordered lists, fenced
code blocks, horizontal rules, and inline bold/italic/code/links. Mermaid
blocks are flattened to a labelled flow line, since Word cannot render them.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

INK = RGBColor(0x1F, 0x38, 0x64)
ACCENT = RGBColor(0x2F, 0x54, 0x96)
SLATE = RGBColor(0x44, 0x54, 0x6A)
MUTED = RGBColor(0x69, 0x70, 0x7C)
CODE_INK = RGBColor(0x9C, 0x27, 0x4E)

HEADER_FILL = "1F3864"
BAND_FILL = "F4F6F9"
QUOTE_FILL = "F7F9FC"
RULE_COLOR = "C9D1DC"

BODY_FONT = "Calibri"
HEAD_FONT = "Calibri Light"
MONO_FONT = "Consolas"

INLINE = re.compile(
    r"(\*\*.+?\*\*|`[^`]+`|\[[^\]]+\]\([^)]+\)|(?<!\*)\*(?!\*)[^*]+\*(?!\*))"
)


def set_cell_fill(cell, color: str) -> None:
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), color)
    cell._tc.get_or_add_tcPr().append(shd)


def repeat_as_header(row) -> None:
    tbl_header = OxmlElement("w:tblHeader")
    tbl_header.set(qn("w:val"), "true")
    row._tr.get_or_add_trPr().append(tbl_header)


def set_cell_margins(table, top=60, bottom=60, left=110, right=110) -> None:
    margins = OxmlElement("w:tblCellMar")
    for side, value in (("top", top), ("bottom", bottom), ("left", left), ("right", right)):
        node = OxmlElement(f"w:{side}")
        node.set(qn("w:w"), str(value))
        node.set(qn("w:type"), "dxa")
        margins.append(node)
    table._tbl.tblPr.append(margins)


def add_paragraph_border(paragraph, edge: str, color: str, size: int = 18) -> None:
    p_pr = paragraph._p.get_or_add_pPr()
    borders = p_pr.find(qn("w:pBdr"))
    if borders is None:
        borders = OxmlElement("w:pBdr")
        p_pr.append(borders)
    node = OxmlElement(f"w:{edge}")
    node.set(qn("w:val"), "single")
    node.set(qn("w:sz"), str(size))
    node.set(qn("w:space"), "8")
    node.set(qn("w:color"), color)
    borders.append(node)


def shade_paragraph(paragraph, color: str) -> None:
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), color)
    paragraph._p.get_or_add_pPr().append(shd)


def add_hyperlink(paragraph, text: str, url: str):
    part = paragraph.part
    r_id = part.relate_to(
        url,
        "http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink",
        is_external=True,
    )
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), r_id)
    run = OxmlElement("w:r")
    r_pr = OxmlElement("w:rPr")
    color = OxmlElement("w:color")
    color.set(qn("w:val"), "2F5496")
    underline = OxmlElement("w:u")
    underline.set(qn("w:val"), "single")
    r_pr.append(color)
    r_pr.append(underline)
    run.append(r_pr)
    text_node = OxmlElement("w:t")
    text_node.text = text
    run.append(text_node)
    link.append(run)
    paragraph._p.append(link)


def write_inline(paragraph, text: str, *, base_size: Pt | None = None, color=None,
                 bold_all: bool = False, italic_all: bool = False) -> None:
    """Render inline markdown into runs on an existing paragraph."""
    for token in INLINE.split(text):
        if not token:
            continue
        if token.startswith("**") and token.endswith("**"):
            run = paragraph.add_run(token[2:-2])
            run.bold = True
        elif token.startswith("`") and token.endswith("`"):
            run = paragraph.add_run(token[1:-1])
            run.font.name = MONO_FONT
            run.font.color.rgb = CODE_INK
            run.font.size = (base_size or Pt(10.5)) - Pt(0.5)
        elif token.startswith("[") and "](" in token:
            label, _, url = token[1:-1].partition("](")
            add_hyperlink(paragraph, label, url)
            continue
        elif token.startswith("*") and token.endswith("*"):
            run = paragraph.add_run(token[1:-1])
            run.italic = True
        else:
            run = paragraph.add_run(token.replace("\\|", "|"))
        if bold_all:
            run.bold = True
        if italic_all:
            run.italic = True
        if color is not None and run.font.color.rgb is None:
            run.font.color.rgb = color
        if base_size is not None and run.font.size is None:
            run.font.size = base_size


def configure_styles(doc: Document) -> None:
    normal = doc.styles["Normal"]
    normal.font.name = BODY_FONT
    normal.font.size = Pt(10.5)
    normal.font.color.rgb = RGBColor(0x1A, 0x1A, 0x1A)
    normal._element.rPr.rFonts.set(qn("w:eastAsia"), BODY_FONT)
    normal.paragraph_format.space_after = Pt(8)
    normal.paragraph_format.line_spacing = 1.15

    specs = [
        ("Heading 1", HEAD_FONT, 22, INK, 22, 8),
        ("Heading 2", HEAD_FONT, 15, ACCENT, 16, 6),
        ("Heading 3", BODY_FONT, 12, SLATE, 12, 4),
        ("Heading 4", BODY_FONT, 11, SLATE, 10, 3),
    ]
    for name, font, size, color, before, after in specs:
        style = doc.styles[name]
        style.font.name = font
        style.font.size = Pt(size)
        style.font.color.rgb = color
        style.font.bold = name != "Heading 1"
        style.paragraph_format.space_before = Pt(before)
        style.paragraph_format.space_after = Pt(after)
        style.paragraph_format.keep_with_next = True


def add_toc_field(doc: Document) -> None:
    paragraph = doc.add_paragraph()
    run = paragraph.add_run()
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = r'TOC \o "1-3" \h \z \u'
    separate = OxmlElement("w:fldChar")
    separate.set(qn("w:fldCharType"), "separate")
    placeholder = OxmlElement("w:t")
    placeholder.text = "Right-click and choose Update Field to build the table of contents."
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    for node in (begin, instr, separate, placeholder, end):
        run._r.append(node)


def force_field_update(doc: Document) -> None:
    settings = doc.settings.element
    update = OxmlElement("w:updateFields")
    update.set(qn("w:val"), "true")
    settings.append(update)


def add_page_number_footer(section) -> None:
    paragraph = section.footer.paragraphs[0]
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    prefix = paragraph.add_run("AgentSwitch Gap Report    |    ")
    prefix.font.size = Pt(8)
    prefix.font.color.rgb = MUTED
    run = paragraph.add_run()
    run.font.size = Pt(8)
    run.font.color.rgb = MUTED
    begin = OxmlElement("w:fldChar")
    begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = "PAGE"
    end = OxmlElement("w:fldChar")
    end.set(qn("w:fldCharType"), "end")
    for node in (begin, instr, end):
        run._r.append(node)


def add_rule(doc: Document) -> None:
    paragraph = doc.add_paragraph()
    paragraph.paragraph_format.space_before = Pt(4)
    paragraph.paragraph_format.space_after = Pt(10)
    add_paragraph_border(paragraph, "bottom", RULE_COLOR, size=8)


def add_quote(doc: Document, lines: list[str]) -> None:
    text = " ".join(lines).strip()
    paragraph = doc.add_paragraph()
    fmt = paragraph.paragraph_format
    fmt.left_indent = Cm(0.4)
    fmt.right_indent = Cm(0.2)
    fmt.space_before = Pt(8)
    fmt.space_after = Pt(10)
    add_paragraph_border(paragraph, "left", "2F5496", size=24)
    shade_paragraph(paragraph, QUOTE_FILL)
    write_inline(paragraph, text, base_size=Pt(10), color=RGBColor(0x2B, 0x33, 0x40))


def split_row(line: str) -> list[str]:
    body = line.strip().strip("|")
    parts = re.split(r"(?<!\\)\|", body)
    return [p.strip() for p in parts]


def parse_alignment(cells: list[str]) -> list[int]:
    alignment = []
    for cell in cells:
        left = cell.startswith(":")
        right = cell.endswith(":")
        if left and right:
            alignment.append(WD_ALIGN_PARAGRAPH.CENTER)
        elif right:
            alignment.append(WD_ALIGN_PARAGRAPH.RIGHT)
        else:
            alignment.append(WD_ALIGN_PARAGRAPH.LEFT)
    return alignment


def add_table(doc: Document, rows: list[list[str]], alignment: list[int]) -> None:
    headers, body = rows[0], rows[1:]
    headless = all(not h for h in headers)
    data = body if headless else rows
    table = doc.add_table(rows=len(data), cols=len(headers))
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.autofit = True
    # The gap register and capability matrix run to seven columns; they only
    # stay legible on a portrait page at a smaller size with tighter margins.
    wide = len(headers) >= 7
    body_size = Pt(8.5) if wide else Pt(9.5)
    if wide:
        set_cell_margins(table, left=70, right=70)
    else:
        set_cell_margins(table)

    for r, row_cells in enumerate(data):
        is_header = not headless and r == 0
        for c in range(len(headers)):
            value = row_cells[c] if c < len(row_cells) else ""
            cell = table.cell(r, c)
            paragraph = cell.paragraphs[0]
            paragraph.alignment = alignment[c] if c < len(alignment) else WD_ALIGN_PARAGRAPH.LEFT
            paragraph.paragraph_format.space_after = Pt(2)
            paragraph.paragraph_format.space_before = Pt(2)
            if is_header:
                write_inline(paragraph, value, base_size=body_size,
                             color=RGBColor(0xFF, 0xFF, 0xFF), bold_all=True)
                set_cell_fill(cell, HEADER_FILL)
            else:
                bold_first = headless and c == 0
                write_inline(paragraph, value, base_size=body_size, bold_all=bold_first)
                if (r % 2 == 1) if headless else (r % 2 == 0):
                    set_cell_fill(cell, BAND_FILL)
        if is_header:
            repeat_as_header(table.rows[r])

    doc.add_paragraph().paragraph_format.space_after = Pt(4)


def add_mermaid_figure(doc: Document, source: list[str], index: int) -> None:
    labels: list[str] = []
    for line in source:
        for match in re.finditer(r'\[\"?(.+?)\"?\]', line):
            label = match.group(1).strip()
            if label and label not in labels:
                labels.append(label)
    if not labels:
        return
    paragraph = doc.add_paragraph()
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    paragraph.paragraph_format.space_before = Pt(6)
    paragraph.paragraph_format.space_after = Pt(2)
    for edge in ("top", "bottom", "left", "right"):
        add_paragraph_border(paragraph, edge, RULE_COLOR, size=6)
    shade_paragraph(paragraph, QUOTE_FILL)
    run = paragraph.add_run("  \u2192  ".join(labels))
    run.font.size = Pt(9)
    run.font.color.rgb = SLATE
    caption = doc.add_paragraph()
    caption.alignment = WD_ALIGN_PARAGRAPH.CENTER
    caption.paragraph_format.space_after = Pt(10)
    caption_run = caption.add_run(f"Figure {index}. Mention-to-edge pipeline.")
    caption_run.font.size = Pt(8.5)
    caption_run.italic = True
    caption_run.font.color.rgb = MUTED


def add_code_block(doc: Document, source: list[str]) -> None:
    paragraph = doc.add_paragraph()
    paragraph.paragraph_format.left_indent = Cm(0.3)
    paragraph.paragraph_format.space_after = Pt(10)
    shade_paragraph(paragraph, "F2F2F2")
    run = paragraph.add_run("\n".join(source))
    run.font.name = MONO_FONT
    run.font.size = Pt(8.5)


def build_cover(doc: Document, meta_rows: list[list[str]]) -> None:
    for _ in range(3):
        doc.add_paragraph()

    title = doc.add_paragraph()
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    title.paragraph_format.space_after = Pt(0)
    run = title.add_run("AgentSwitch")
    run.font.name = HEAD_FONT
    run.font.size = Pt(40)
    run.font.color.rgb = INK

    subtitle = doc.add_paragraph()
    subtitle.alignment = WD_ALIGN_PARAGRAPH.CENTER
    subtitle.paragraph_format.space_after = Pt(4)
    sub_run = subtitle.add_run("CRM and Multiagent Platform")
    sub_run.font.name = HEAD_FONT
    sub_run.font.size = Pt(18)
    sub_run.font.color.rgb = ACCENT

    kicker = doc.add_paragraph()
    kicker.alignment = WD_ALIGN_PARAGRAPH.CENTER
    kicker.paragraph_format.space_after = Pt(18)
    kicker_run = kicker.add_run("COMPETITIVE GAP REPORT")
    kicker_run.font.size = Pt(11)
    kicker_run.bold = True
    kicker_run.font.color.rgb = SLATE

    strap = doc.add_paragraph()
    strap.alignment = WD_ALIGN_PARAGRAPH.CENTER
    strap.paragraph_format.space_after = Pt(28)
    strap_run = strap.add_run(
        "What the MCP catalog exposes today, where it trails the platforms "
        "it will be compared against, and what to build next."
    )
    strap_run.italic = True
    strap_run.font.size = Pt(11)
    strap_run.font.color.rgb = MUTED

    if meta_rows:
        add_table(doc, [["", ""], *meta_rows], [WD_ALIGN_PARAGRAPH.LEFT, WD_ALIGN_PARAGRAPH.LEFT])

    doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)

    toc_heading = doc.add_paragraph()
    toc_heading.paragraph_format.space_after = Pt(10)
    toc_run = toc_heading.add_run("Contents")
    toc_run.font.name = HEAD_FONT
    toc_run.font.size = Pt(20)
    toc_run.font.color.rgb = INK
    add_toc_field(doc)
    doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)


def convert(md_path: Path, out_path: Path) -> None:
    lines = md_path.read_text(encoding="utf-8").splitlines()

    doc = Document()
    section = doc.sections[0]
    section.top_margin = Cm(2.0)
    section.bottom_margin = Cm(2.0)
    section.left_margin = Cm(2.2)
    section.right_margin = Cm(2.2)
    configure_styles(doc)
    add_page_number_footer(section)

    # The markdown opens with a centered HTML title block and a metadata table
    # that become the cover page; body content starts at the Contents list.
    body_start = next(i for i, line in enumerate(lines) if line.strip() == "## Contents")
    body_end = next(i for i, line in enumerate(lines) if line.strip().startswith("## 1."))

    meta_rows: list[list[str]] = []
    for line in lines[:body_start]:
        stripped = line.strip()
        if stripped.startswith("|") and not re.match(r"^\|[\s:|-]+\|$", stripped):
            cells = split_row(stripped)
            if any(cells):
                meta_rows.append(cells)

    build_cover(doc, meta_rows)

    i = body_end
    figure_index = 1
    first_section = True
    pending_quote: list[str] = []

    def flush_quote() -> None:
        nonlocal pending_quote
        if pending_quote:
            add_quote(doc, pending_quote)
            pending_quote = []

    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()

        if stripped.startswith(">"):
            pending_quote.append(stripped.lstrip("> ").strip())
            i += 1
            continue
        flush_quote()

        if not stripped:
            i += 1
            continue

        if stripped.startswith("```"):
            lang = stripped[3:].strip()
            block: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                block.append(lines[i])
                i += 1
            i += 1
            if lang == "mermaid":
                add_mermaid_figure(doc, block, figure_index)
                figure_index += 1
            else:
                add_code_block(doc, block)
            continue

        if re.fullmatch(r"-{3,}", stripped):
            add_rule(doc)
            i += 1
            continue

        heading = re.match(r"^(#{1,4})\s+(.*)$", stripped)
        if heading:
            level = len(heading.group(1))
            text = heading.group(2).strip()
            if level == 2 and re.match(r"^\d+\.", text):
                # The Contents page already ends with a break, so the first
                # numbered section must not add a second one.
                if first_section:
                    first_section = False
                else:
                    doc.paragraphs[-1].add_run().add_break(WD_BREAK.PAGE)
            paragraph = doc.add_paragraph(style=f"Heading {min(level, 4)}")
            write_inline(paragraph, text)
            i += 1
            continue

        if stripped.startswith("|"):
            table_lines = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                table_lines.append(lines[i].strip())
                i += 1
            if len(table_lines) < 2:
                continue
            header = split_row(table_lines[0])
            alignment = parse_alignment(split_row(table_lines[1]))
            rows = [header] + [split_row(line) for line in table_lines[2:]]
            add_table(doc, rows, alignment)
            continue

        bullet = re.match(r"^[-*]\s+(.*)$", stripped)
        if bullet:
            paragraph = doc.add_paragraph(style="List Bullet")
            paragraph.paragraph_format.space_after = Pt(3)
            write_inline(paragraph, bullet.group(1))
            i += 1
            continue

        numbered = re.match(r"^\d+\.\s+(.*)$", stripped)
        if numbered:
            paragraph = doc.add_paragraph(style="List Number")
            paragraph.paragraph_format.space_after = Pt(3)
            write_inline(paragraph, numbered.group(1))
            i += 1
            continue

        paragraph = doc.add_paragraph()
        write_inline(paragraph, stripped)
        i += 1

    flush_quote()
    force_field_update(doc)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(out_path)
    print(f"WROTE {out_path}")


if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent
    source = Path(sys.argv[1]) if len(sys.argv) > 1 else root / "docs" / "gap-analysis.md"
    target = Path(sys.argv[2]) if len(sys.argv) > 2 else root / "docs" / "AgentSwitch-Gap-Report.docx"
    convert(source, target)
