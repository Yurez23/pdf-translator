"""PDF-only glyph provenance and symbol insertion before word/table extraction.

Own the extraction device per page: no global monkeypatch, font registration,
network, OCR, or changes to the input PDF. Text and tables share these chars.
"""

import hashlib

from pdfminer.pdfinterp import PDFPageInterpreter
from pdfminer.pdftypes import resolve1
from pdfminer.latin_enc import ENCODING
from pdfminer.psparser import PSLiteral
from pdfplumber.page import Page, PDFPageAggregatorWithMarkedContent
from pdfplumber.utils.exceptions import PdfminerException

from .models import ConversionWarning, Glyph
from .symbol_geometry import vector_symbols
from .symbols import LABELS, identify_glyph

_WIN_ANSI_GLYPHS = {row[3]: row[0] for row in ENCODING if row[3] is not None}


def encoding_glyphs(spec):
    """Resolve actual simple-font glyph selection separately from ToUnicode.

    Only the evidenced WinAnsi base and explicit Differences are supported.
    Unknown/custom bases or malformed differences decline semantic decoding.
    """
    encoding = resolve1(spec.get("Encoding"))
    if isinstance(encoding, PSLiteral):
        return dict(_WIN_ANSI_GLYPHS) if encoding.name == "WinAnsiEncoding" else {}
    if not isinstance(encoding, dict):
        return {}
    base = resolve1(encoding.get("BaseEncoding"))
    if not isinstance(base, PSLiteral) or base.name != "WinAnsiEncoding":
        return {}
    glyphs, code = dict(_WIN_ANSI_GLYPHS), None
    differences = resolve1(encoding.get("Differences", []))
    if not isinstance(differences, (list, tuple)):
        return {}
    for value in differences:
        value = resolve1(value)
        if type(value) is int and 0 <= value <= 255:
            code = value
        elif isinstance(value, PSLiteral) and code is not None and code <= 255:
            glyphs[code] = value.name
            code += 1
        else:
            return {}
    return glyphs


class _GlyphResources:
    """Per-page metadata wrapper; the document still owns its resource cache."""
    def __init__(self, inner):
        self.inner, self.encodings = inner, {}

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def get_font(self, objid, spec):
        font = self.inner.get_font(objid, spec)
        if "CosmereDingbats" in str(font.fontname):
            self.encodings[font] = encoding_glyphs(spec)
        return font


class _GlyphDevice(PDFPageAggregatorWithMarkedContent):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.font_digests = {}

    def render_char(self, matrix, font, fontsize, scaling, rise, cid, ncs, graphicstate):
        advance = super().render_char(matrix, font, fontsize, scaling, rise, cid, ncs, graphicstate)
        item = self.cur_item._objs[-1]
        item.source_cid = cid
        # Hash only candidate symbol fonts. A family name alone never decodes.
        if "CosmereDingbats" in str(font.fontname):
            item.encoding_glyph = self.rsrcmgr.encodings.get(font, {}).get(cid, "")
            if font not in self.font_digests:
                streams = [resolve1(v) for k, v in font.descriptor.items() if k.startswith("FontFile")]
                self.font_digests[font] = (hashlib.sha256(streams[0].get_data()).hexdigest()
                                           if len(streams) == 1 else "")
            item.font_sha256 = self.font_digests[font]
        return advance


class _GlyphPage(Page):
    @property
    def layout(self):
        if not hasattr(self, "_layout"):
            resources = _GlyphResources(self.pdf.rsrcmgr)
            device = _GlyphDevice(resources, pageno=self.page_number, laparams=self.pdf.laparams)
            try:
                PDFPageInterpreter(resources, device).process_page(self.page_obj)
            except Exception as exc:
                raise PdfminerException(exc) from exc
            self._layout = device.get_result()
        return self._layout

    def process_object(self, obj):
        result = super().process_object(obj)
        if result["object_type"] == "char":
            result["source_cid"] = getattr(obj, "source_cid", None)
            result["font_sha256"] = getattr(obj, "font_sha256", "")
            result["encoding_glyph"] = getattr(obj, "encoding_glyph", "")
        return result


def glyph_page(page):
    """Use an isolated device even if the caller already read the native page."""
    return _GlyphPage(page.pdf, page.page_obj, page_number=page.page_number,
                      initial_doctop=page.initial_doctop)


def _anchor(box, chars, *, cell_row=False):
    """Find a compatible horizontal baseline, including isolated table cells.

    An exact signature alone is insufficient: decline large art, off-baseline
    marks and symbols over ordinary letters. The short reserved gap may be
    between words, beside a title, or in an adjacent cost cell.
    """
    x0, top, x1, bottom = box
    candidates = []
    for char in chars:
        if not char.get("upright", True) or char["text"].isspace() or char.get("symbol"):
            continue
        size = char["size"]
        if not (.25*size <= bottom-top <= 1.3*size and x1-x0 <= size*2):
            continue
        centre = (top+bottom)/2
        if not char["top"]-size*.1 <= centre <= char["bottom"]:
            continue
        gap = max(char["x0"]-x1, x0-char["x1"], 0)
        overlap = min(x1, char["x1"])-max(x0, char["x0"])
        if overlap > min(x1-x0, char["x1"]-char["x0"])*.3:
            return None
        if cell_row or gap <= size*10:
            candidates.append((gap, abs(centre-(char["top"]+char["bottom"])/2), char))
    return min(candidates, key=lambda c: c[:2])[2] if candidates else None


def _warning(page, warn):
    warn(ConversionWarning("unknown_game_symbol",
         "Обнаружен неопознанный игровой знак; сохранён [unknown symbol]. Проверьте исходную страницу.",
         (page.page_number,)))


def recover_symbols(page, warn):
    chars = [dict(c) for c in page.chars]
    for char in chars:
        if char.get("symbol"):
            continue
        raw = char["text"]
        char["raw_text"] = raw
        symbol = identify_glyph(char["fontname"], char.get("font_sha256", ""), char.get("source_cid"), raw,
                                char.get("encoding_glyph", ""))
        if symbol:
            char["symbol"] = symbol
            char["text"] = LABELS[symbol]
            if symbol == "unknown":
                _warning(page, warn)
    inserted = []
    after = {}
    tables = None
    for symbol, box in vector_symbols(page.curves):
        # Some producers keep both an outlined and a text representation.
        if any(c.get("symbol") and min(box[2], c["x1"])-max(box[0], c["x0"]) > 0
               and min(box[3], c["bottom"])-max(box[1], c["top"]) > 0 for c in chars+inserted):
            continue
        enclosed = [c for c in chars if not c["text"].isspace()
                    and box[0] <= c["x0"] and c["x1"] <= box[2]
                    and box[1] <= c["top"] and c["bottom"] <= box[3]]
        numbered = symbol == "complication" and bool(enclosed) and all(c["text"].isdecimal() for c in enclosed)
        anchor = min(enclosed, key=lambda c: c["x0"]) if numbered else _anchor(box, chars)
        if anchor is None:
            # Empty cost cells can be far from text. Use an established ruling
            # row, not a nearest-word guess across the whole page.
            if tables is None:
                try:
                    tables = page.find_tables()
                except (ValueError, TypeError, IndexError):
                    tables = []  # The reader reports its normal detection failure.
            for table in tables:
                for row in table.rows:
                    cells = [c for c in row.cells if c]
                    if not cells:
                        continue
                    row_top, row_bottom = min(c[1] for c in cells), max(c[3] for c in cells)
                    if (table.bbox[0] <= box[0] <= box[2] <= table.bbox[2]
                            and row_top <= box[1] <= box[3] <= row_bottom):
                        row_chars = [c for c in chars if row_top <= (c["top"]+c["bottom"])/2 <= row_bottom
                                     and table.bbox[0] <= c["x0"] <= table.bbox[2]]
                        anchor = _anchor(box, row_chars, cell_row=True)
                    if anchor is not None:
                        break
                if anchor is not None:
                    break
        if anchor is None:
            continue
        char = dict(anchor)
        char.update(text=LABELS[symbol], raw_text="", symbol=symbol, origin="vector",
                    source_cid=None, font_sha256="", encoding_glyph="", symbol_bbox=box,
                    x0=box[0], x1=box[2], width=box[2]-box[0])
        if numbered:
            # The glyph surrounds a printed number. Keep that original number
            # exactly once and place the semantic marker before it, without
            # claiming a diagram-to-die-face correspondence (PDF-06).
            char.update(text=LABELS[symbol]+" ", x1=min(c["x0"] for c in enclosed),
                        width=min(c["x0"] for c in enclosed)-box[0])
        # Baseline/size/style belong to the neighbouring text, while the real
        # vector extent remains in Glyph.bbox. Labels never enlarge geometry.
        inserted.append(char)
        after.setdefault(id(anchor), []).append(char)
        if symbol == "unknown":
            _warning(page, warn)
    result = page.filter(lambda _: True)
    # pdfplumber groups CONSECUTIVE upright/rotated runs before geometric
    # sorting. Append-at-end would strand symbols beyond rotated credits.
    ordered = [item for c in chars for item in (c, *after.get(id(c), ()))]
    result._objects = {**page.objects, "char": ordered}
    return result


def glyph_provenance(char):
    return Glyph(char.get("raw_text", char["text"]), char["fontname"],
                 char.get("symbol_bbox", tuple(char[k] for k in ("x0", "top", "x1", "bottom"))),
                 char.get("source_cid"), char.get("font_sha256", ""),
                 char.get("symbol", ""), char.get("origin", "text"), char.get("encoding_glyph", ""))
