"""Render the same ordered blocks into Markdown and plain text."""

import re

from .models import Block
from .diagram_export import references, annotate_cells


def _escape(text: str) -> str:
    return re.sub(r"([\\`*_\[\]<>])", r"\\\1", text)


def _paragraph(text: str) -> str:
    text = _escape(text)
    # Source prose must not accidentally become a heading, list, or rule.
    return re.sub(r"^(#{1,6}|>|[-+])(?=\s)|^(\d+)\.(?=\s)", lambda m: "\\" + m[0] if m[1] else m[2] + r"\.", text)


def render(blocks: list[Block]) -> tuple[str, str]:
    md: list[str] = []
    txt: list[str] = []
    for block in blocks:
        if block.role == "cell_diagram" and block.children:
            # Preserve the existing cell-row rendering; only add title ownership.
            child_md, child_txt = render(block.children)
            md.append(child_md.rstrip("\n"))
            txt.append(child_txt.rstrip("\n"))
            continue
        content=annotate_cells(block) if block.diagram and block.diagram.status in ('cells','correspondence') else block.text
        if block.kind == "table":
            rows = [[_escape(cell).replace("|", r"\|") for cell in row] for row in block.rows]
            if not rows:
                continue
            table = ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join("---" for _ in rows[0]) + " |"]
            table.extend("| " + " | ".join(row) + " |" for row in rows[1:])
            md.append("\n".join(table))
            txt.append("\n".join("\t".join(row) for row in block.rows))
        elif block.kind == "heading":
            if block.role in ("table_title", "table_group", "title_label", "aside_title", "graphic_label"):
                md.append("**" + _escape(content) + "**")
            else:
                md.append("#" * max(1, min(block.level, 6)) + " " + _escape(content))
            txt.append(content)
        elif block.kind == "aside" and block.children:
            if block.diagram:
                inner_mds,inner_txts=[],[]
                for child in block.children:
                    node=next((n for n in block.diagram.nodes if n.kind=="card" and
                               child.role=="diagram_node" and child.sources[0]==n.source),None)
                    child_md,child_txt=render([child])
                    if node:
                        inner_mds.append(f"**Node {node.id}**");inner_txts.append(f"Node {node.id}")
                    inner_mds.append(child_md.rstrip("\n"));inner_txts.append(child_txt.rstrip("\n"))
                generated=references(block.diagram)
                inner_mds.append(generated);inner_txts.append(generated)
                inner_md="\n\n".join(inner_mds)+"\n";inner_txt="\n\n".join(inner_txts)+"\n"
            else:
                inner_md, inner_txt = render(block.children)
            if block.role == "graphic_labels":
                notice = "Graphic labels are listed in source order; their visual associations are not reconstructed. Check the source PDF."
                inner_md = inner_md.rstrip("\n") + "\n\n" + notice + "\n"
                inner_txt = inner_txt.rstrip("\n") + "\n\n" + notice + "\n"
            # Quoted blank lines keep paragraphs in ONE CommonMark blockquote.
            # A bare blank line would terminate it and create separate boxes.
            md.append("\n".join("> " + line if line else ">" for line in inner_md.rstrip("\n").split("\n")))
            txt.append(inner_txt.rstrip("\n"))
        elif block.kind in ("aside", "footnote", "raw"):
            md.append("> " + _escape(content))
            txt.append(content)
        elif block.kind == "list_item":
            text = re.sub(r"^[•●▪◦‣◆◇*–]\s+", "- ", block.text)
            # Keep ordered-list markers, escape only their payload.
            match = re.match(r"^([-]|\d+[.)]|[A-Za-z][.)]|\(\d+\))\s+(.*)", text)
            # Hanging speaker/record labels ending in a colon have no source
            # bullet; retain their paragraphs without inventing list markers.
            colon_label = not match and block.role == 'labelled_entry'
            md.append((match[1] + " " + _escape(match[2])) if match else
                      _paragraph(text) if colon_label else "- " + _escape(text))
            txt.append(block.text)
        else:
            md.append(_paragraph(content))
            txt.append(content)
        if block.diagram and block.diagram.status=='correspondence':
            generated=references(block.diagram)
            md.append('\n'.join('> '+line for line in generated.splitlines()))
            txt.append(generated)
    return ("\n\n".join(md) + "\n" if md else "", "\n\n".join(txt) + "\n" if txt else "")
