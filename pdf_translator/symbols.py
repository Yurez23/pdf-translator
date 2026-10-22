"""Neutral symbol identities/notation and verified embedded-font contracts.

Never infer a meaning from prose or from a Unicode character alone. Two fonts
with the same family have different CIDs; the stream digest guards collisions.
Evidence, exact source locations and detection limits are in docs/symbols.md.
"""

import re

LABELS = {
    "action_1": "[1 action]", "action_2": "[2 actions]", "action_3": "[3 actions]",
    "free_action": "[free action]", "reaction": "[reaction]",
    "opportunity": "[opportunity]", "complication": "[complication]",
    "special_activation": "[special activation]", "always_active": "[always active]",
    "unknown": "[unknown symbol]",
}
REGULAR = "7433b017d6cce42db879881e36526d1aa0e8d4463693a4ed1b45ea1983aa00ee"
SMALL_CAPS = "2a16ebc5dab2a2ee51842806485c9487f14a9d970173c9def46ce3bdeb8e1814"
FONT_GLYPHS = {
    ("CosmereDingbats-Regular", "8cbc5d1221969c061e00f2fab05d4ff0a141bcb6e24e7dd246557c610cab5490"): {
        (79, "O", "O"): "opportunity",
    },
    ("CosmereDingbats-Regular", REGULAR): {
        (79, "O", "O"): "opportunity", (67, "C", "C"): "complication",
        (31, "\u25b7", "uni25B7"): "free_action", (30, "\u21bb", "uni21BB"): "reaction",
        (82, "R", "R"): "reaction",
        (32, " ", "space"): "",  # Verified blank, not a missing icon.
    },
    ("CosmereDingbats-Regular-SC700", SMALL_CAPS): {
        (27, "\u25b6", "uni25B6"): "action_1", (50, "2", "two"): "action_2",
        (30, "\u25b7", "uni25B7"): "free_action", (31, "\u21bb", "uni21BB"): "reaction",
        (28, "\u2605", "uni2605"): "special_activation", (29, "\u221e", "infinity"): "always_active",
        (32, " ", "space"): "",
    },
}


def font_family(font: str) -> str:
    return re.sub(r"^[A-Z]{6}\+", "", font)


def identify_glyph(font: str, digest: str, cid: int | None, text: str, glyph_name: str) -> str:
    family = font_family(font)
    if family not in {key[0] for key in FONT_GLYPHS}:
        return ""
    return FONT_GLYPHS.get((family, digest), {}).get((cid, text, glyph_name), "unknown")
