"""Parse/export book text without handing Markdown structure to a model."""

import re
from collections.abc import Mapping

from .models import DocumentNode as Node, ProtectedToken, TranslationDocument, TranslationError, TranslationUnit

MARKER = re.compile(r"⟦P\d{5}:\d+⟧")


def escape_inline(text: str) -> str:
    return re.sub(r"([\\`*_\[\]<>|~])", r"\\\1", text)


def restore_inline(unit: TranslationUnit, text: str, *, markdown: bool) -> str:
    tokens = {token.marker: token for token in unit.protected}
    parts = []
    cursor = 0
    for match in MARKER.finditer(text):
        literal = text[cursor:match.start()]
        parts.append(escape_inline(literal) if markdown else literal)
        token = tokens.get(match[0])
        if token is None:
            raise TranslationError(f"Неизвестный защищённый маркер в {unit.id}.")
        parts.append(token.markdown if markdown else token.text)
        cursor = match.end()
    literal = text[cursor:]
    parts.append(escape_inline(literal) if markdown else literal)
    return "".join(parts)


def parse_document(text: str, *, markdown: bool) -> TranslationDocument:
    if MARKER.search(text):
        raise TranslationError("Исходник содержит зарезервированные маркеры ⟦P00000:0⟧; замените их перед переводом.")
    if not text.strip() or "\x00" in text:
        raise TranslationError("Нужен непустой текстовый файл UTF-8 без нулевых символов.")
    units: list[TranslationUnit] = []
    warnings: list[str] = []
    headings: dict[int, str] = {}

    def add(content, kind: str, *, literal: bool = False) -> str:
        if len(units) >= 100000:
            raise TranslationError("Слишком много текстовых элементов; выберите отдельную главу.")
        identifier = f"u{len(units):05d}"
        protected: list[ProtectedToken] = []

        def protect(md: str, txt: str = "") -> str:
            marker = f"⟦P{len(units):05d}:{len(protected)}⟧"
            protected.append(ProtectedToken(marker, md, txt))
            return marker

        if literal:
            payload = content
        else:
            fragments = []
            links = []
            for token in content:
                if token.type == "text":
                    fragments.append(token.content)
                elif token.type == "softbreak":
                    fragments.append(" ")
                elif token.type == "hardbreak":
                    fragments.append(protect("  \n", "\n"))
                elif token.type in ("strong_open", "strong_close", "em_open", "em_close", "s_open", "s_close"):
                    fragments.append(protect(token.markup))
                elif token.type == "code_inline":
                    runs = [len(m[0]) for m in re.finditer(r"`+", token.content)]
                    fence = "`" * max(1, max(runs, default=0) + 1)
                    pad = " " if token.content.startswith(("`", " ")) or token.content.endswith(("`", " ")) else ""
                    fragments.append(protect(fence + pad + token.content + pad + fence, token.content))
                elif token.type == "link_open":
                    href = token.attrGet("href") or ""
                    href = href.replace("\\", "%5C").replace("(", "%28").replace(")", "%29").replace(" ", "%20")
                    title = token.attrGet("title")
                    suffix = "](" + href + (' "' + title.replace('"', '&quot;') + '"' if title else '') + ")"
                    links.append((suffix, " (" + href + ")"))
                    fragments.append(protect("["))
                elif token.type == "link_close":
                    if not links:
                        raise TranslationError("Повреждённая структура Markdown-ссылки.")
                    fragments.append(protect(*links.pop()))
                elif token.type == "image":
                    href = (token.attrGet("src") or "").replace("(", "%28").replace(")", "%29").replace(" ", "%20")
                    alt = token.content
                    fragments.append(protect("![" + escape_inline(alt) + "](" + href + ")", alt))
                    warnings.append(f"{identifier}: изображение и его alt-текст сохранены без перевода.")
                elif token.type == "html_inline":
                    fragments.append(protect(escape_inline(token.content), token.content))
                    warnings.append(f"{identifier}: встроенный HTML сохранён как буквальный текст.")
                else:
                    raise TranslationError(f"Неподдержанный inline-элемент Markdown: {token.type}.")
            payload = "".join(fragments)
        payload = re.sub(r"[\s\u00a0]+", " ", payload).strip()
        context = " / ".join(headings[level] for level in sorted(headings))
        units.append(TranslationUnit(identifier, payload, kind, tuple(protected), context))
        return identifier

    if not markdown:
        nodes = []
        for paragraph in re.split(r"\n\s*\n", text.replace("\r\n", "\n").strip()):
            lines = paragraph.splitlines()
            if len(lines) >= 2 and all("\t" in line for line in lines):
                rows = [line.split("\t") for line in lines]
                if len({len(row) for row in rows}) != 1:
                    raise TranslationError("TXT-таблица содержит строки с разным количеством столбцов.")
                nodes.append(Node("table", rows=[[add(cell, "table_cell", literal=True) for cell in row] for row in rows]))
            else:
                value = " ".join(line.strip() for line in lines).strip()
                match = re.match(r"^([•●▪◦‣*–-]|\d+[.)]|[A-Za-z][.)]|\(\d+\))\s+(.*)$", value)
                if match:
                    item = Node("paragraph", unit_id=add(match[2], "list_item", literal=True))
                    nodes.append(Node("literal_list", children=[item], marker=match[1]))
                else:
                    nodes.append(Node("paragraph", unit_id=add(value, "paragraph", literal=True)))
        warnings.append("TXT: уровни заголовков и границы вставок отсутствуют в исходном формате и не восстанавливаются.")
        return TranslationDocument(tuple(nodes), tuple(units), tuple(dict.fromkeys(warnings)))

    try:
        from markdown_it import MarkdownIt
    except ImportError:
        raise TranslationError("Для Markdown установите зависимости: python -m pip install -e '.[translation]'.") from None
    tokens = MarkdownIt("commonmark", {"html": True}).enable(["table", "strikethrough"]).parse(text)
    index = 0

    def parse_nodes(closing: str = "", depth: int = 0) -> list[Node]:
        nonlocal index
        if depth > 32:
            raise TranslationError("Слишком глубокая вложенность Markdown (больше 32).")
        result = []
        while index < len(tokens):
            token = tokens[index]
            index += 1
            if token.type == closing:
                return result
            if token.type in ("heading_open", "paragraph_open"):
                inline = tokens[index]
                if inline.type != "inline":
                    raise TranslationError("Не удалось прочитать текст Markdown-блока.")
                index += 2  # inline and the corresponding close token
                kind = "heading" if token.type == "heading_open" else "paragraph"
                level = int(token.tag[1:]) if kind == "heading" else 0
                if level and depth == 0:
                    for old in [key for key in headings if key >= level]:
                        del headings[old]
                    headings[level] = inline.content
                result.append(Node(kind, unit_id=add(inline.children or [], kind), level=level))
            elif token.type == "blockquote_open":
                result.append(Node("quote", children=parse_nodes("blockquote_close", depth + 1)))
            elif token.type in ("bullet_list_open", "ordered_list_open"):
                result.append(Node("list", children=parse_nodes(token.type.replace("_open", "_close"), depth + 1),
                                   marker=token.markup, start=int(token.attrGet("start") or 1)))
            elif token.type == "list_item_open":
                result.append(Node("item", children=parse_nodes("list_item_close", depth + 1)))
            elif token.type == "table_open":
                rows = []
                while index < len(tokens) and tokens[index].type != "table_close":
                    current = tokens[index]
                    index += 1
                    if current.type == "tr_open":
                        rows.append([])
                    elif current.type == "inline":
                        rows[-1].append(add(current.children or [], "table_cell"))
                index += 1
                if not rows or len({len(row) for row in rows}) != 1:
                    raise TranslationError("Не удалось сохранить прямоугольную Markdown-таблицу.")
                result.append(Node("table", rows=rows))
            elif token.type in ("fence", "code_block", "html_block"):
                result.append(Node("verbatim", text=token.content, marker=token.info if token.type == "fence" else ""))
                warnings.append(f"Блок {token.type} сохранён без перевода.")
            elif token.type == "hr":
                result.append(Node("rule"))
            else:
                raise TranslationError(f"Неподдержанный блочный элемент Markdown: {token.type}.")
        if closing:
            raise TranslationError("Незакрытый Markdown-контейнер.")
        return result

    nodes = parse_nodes()
    if not units:
        raise TranslationError("В документе нет текста для перевода (только код или разделители).")
    return TranslationDocument(tuple(nodes), tuple(units), tuple(dict.fromkeys(warnings)))


def render_document(document: TranslationDocument, translations: Mapping[str, str]) -> tuple[str, str]:
    units = {unit.id: unit for unit in document.units}

    def render(nodes: list[Node] | tuple[Node, ...], markdown: bool) -> str:
        blocks = []
        for node in nodes:
            if node.kind in ("heading", "paragraph"):
                unit = units[node.unit_id]
                value = restore_inline(unit, translations.get(unit.id, unit.text), markdown=markdown)
                if markdown and node.kind == "heading":
                    value = "#" * node.level + " " + value
                elif markdown:
                    value = re.sub(r"^(#{1,6}|>|[-+])(?=\s)|^(\d+)\.(?=\s)",
                                   lambda m: "\\" + m[0] if m[1] else m[2] + r"\.", value)
                blocks.append(value)
            elif node.kind == "table":
                rows = [[restore_inline(units[cell], translations.get(cell, units[cell].text), markdown=markdown)
                         for cell in row] for row in node.rows]
                if markdown:
                    values = ["| " + " | ".join(row) + " |" for row in rows]
                    values.insert(1, "| " + " | ".join("---" for _ in rows[0]) + " |")
                    blocks.append("\n".join(values))
                else:
                    blocks.append("\n".join("\t".join(row) for row in rows))
            elif node.kind == "quote":
                value = render(node.children, markdown)
                blocks.append("\n".join("> " + line if line else ">" for line in value.split("\n")) if markdown else value)
            elif node.kind in ("list", "literal_list"):
                items = []
                ordered = node.marker.endswith((".", ")")) and node.kind == "list"
                for index, item in enumerate(node.children):
                    marker = str(node.start + index) + node.marker if ordered else node.marker
                    if node.kind == "list" and not ordered:
                        marker = "-"
                    value = render([item], markdown)
                    lines = value.split("\n")
                    items.append(marker + " " + lines[0] + "".join("\n" + " " * (len(marker) + 1) + line if line else "\n" for line in lines[1:]))
                blocks.append("\n\n".join(items))
            elif node.kind == "item":
                blocks.append(render(node.children, markdown))
            elif node.kind == "verbatim":
                if markdown:
                    fence = "`" * max(3, max((len(m[0]) for m in re.finditer(r"`+", node.text)), default=0) + 1)
                    blocks.append(fence + node.marker.replace("`", " ") + "\n" + node.text.rstrip("\n") + "\n" + fence)
                else:
                    blocks.append(node.text.rstrip("\n"))
            elif node.kind == "rule":
                if markdown:
                    blocks.append("---")
        return "\n\n".join(blocks)

    return render(document.nodes, True) + "\n", render(document.nodes, False) + "\n"
