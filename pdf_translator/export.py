"""Render the same ordered blocks into Markdown and plain text."""

import re

from .models import Block


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
        if block.kind == "table":
            rows = [[_escape(cell).replace("|", r"\|") for cell in row] for row in block.rows]
            if not rows:
                continue
            table = ["| " + " | ".join(rows[0]) + " |", "| " + " | ".join("---" for _ in rows[0]) + " |"]
            table.extend("| " + " | ".join(row) + " |" for row in rows[1:])
            md.append("\n".join(table))
            txt.append("\n".join("\t".join(row) for row in block.rows))
        elif block.kind == "heading":
            if block.role in ("table_title", "title_label", "aside_title"):
                md.append("**" + _escape(block.text) + "**")
            else:
                md.append("#" * max(1, min(block.level, 6)) + " " + _escape(block.text))
            txt.append(block.text)
        elif block.kind == "aside" and block.children:
            inner_md, inner_txt = render(block.children)
            # Quoted blank lines keep paragraphs in ONE CommonMark blockquote.
            # A bare blank line would terminate it and create separate boxes.
            md.append("\n".join("> " + line if line else ">" for line in inner_md.rstrip("\n").split("\n")))
            txt.append(inner_txt.rstrip("\n"))
        elif block.kind in ("aside", "footnote", "raw"):
            md.append("> " + _escape(block.text))
            txt.append(block.text)
        elif block.kind == "list_item":
            text = re.sub(r"^[•●▪◦‣*–]\s+", "- ", block.text)
            # Keep ordered-list markers, escape only their payload.
            match = re.match(r"^([-]|\d+[.)]|[A-Za-z][.)]|\(\d+\))\s+(.*)", text)
            md.append((match[1] + " " + _escape(match[2])) if match else "- " + _escape(text))
            txt.append(block.text)
        else:
            md.append(_paragraph(block.text))
            txt.append(block.text)
    return ("\n\n".join(md) + "\n" if md else "", "\n\n".join(txt) + "\n" if txt else "")
