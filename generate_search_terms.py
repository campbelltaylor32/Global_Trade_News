"""
generate_search_terms.py
─────────────────────────────────────────────────────────────────────────
Reads a CSV of (cmd_code, cmd_desc) — like the project's
commodity_final.csv — and produces a CSV of GDELT search terms ready
for loading into the commodity_search_terms MySQL table.

Strategy
────────
  1. Deduplicate cmd_codes across HS revisions.  Multiple descriptions
     for the same chapter are merged so the extractor sees all variants.
  2. Programmatic extraction from each description: split on
     punctuation, strip qualifiers ("n.e.s.", "thereof", parentheticals),
     and pull short content phrases.
  3. Curated supplement: a hand-compiled dictionary that adds the
     trade-press vocabulary pure extraction misses (e.g. "petroleum"
     for chapter 27, "wheat" for chapter 10).  Each curated term is
     tagged source='curated' in the output so you can audit/edit.
  4. Output CSV: cmd_code, search_term, term_type, language, priority,
     is_active, source, notes.  Review before loading into MySQL.

Usage
─────
  python generate_search_terms.py \
      --input  commodity_final.csv \
      --output commodity_search_terms.csv
"""

from __future__ import annotations

import argparse
import csv
import re
from collections import defaultdict
from pathlib import Path


# ─────────────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────────────

# Words to drop during extraction — too generic to be useful alone.
STOP_WORDS = {
    "the", "and", "or", "of", "for", "to", "in", "on", "by", "with",
    "such", "other", "thereof", "edible", "live", "raw",
    "certain", "miscellaneous", "etc",
    "kind", "kinds", "type", "types", "form", "forms",
    "similar", "like", "including", "containing", "based",
    "made", "non", "not", "elsewhere", "specified", "included",
    "nes", "nec",
    "articles", "products", "preparations", "manufactures",
    "accessories", "fittings", "fixtures", "supports", "stuffed",
    "all", "any", "are", "are", "their", "there",
    "from", "without", "into", "between",
    "whether",
}

# Phrases to strip wholesale from descriptions before tokenising.
STRIP_PHRASES = [
    r"\bn\.?e\.?s\.?\b",
    r"\bn\.?e\.?c\.?\b",
    r"not elsewhere specified or included",
    r"not elsewhere specified",
    r"parts?\s+(?:and|or)\s+accessories?\s+thereof",
    r"parts?\s+thereof",
    r"and\s+the\s+like",
    r"and\s+similar(?:\s+\w+)?",
    r"of any kind",
    r"whether or not(?:\s+(?:\w+|,))*",
    r"intended for(?:\s+\w+){0,8}",
    r"\bsuch as\b(?:\s+\w+){0,5}",
]

# Curated supplement: trade-press vocabulary for chapters where
# extraction from cmd_desc won't surface the conventional terms.
# Manually compiled — this is the "domain knowledge" layer.
CURATED_TERMS: dict[str, list[str]] = {
    "01": ["livestock", "cattle", "live cattle", "pigs", "poultry"],
    "02": ["beef", "pork", "chicken", "lamb", "meat exports"],
    "03": ["fish", "seafood", "salmon", "tuna", "shrimp", "fishmeal"],
    "04": ["dairy", "milk", "cheese", "butter", "milk powder", "skim milk powder"],
    "07": ["vegetables", "onions", "tomatoes", "potatoes", "garlic"],
    "08": ["fruit", "bananas", "apples", "oranges", "avocados", "grapes"],
    "09": ["coffee", "tea", "spices", "pepper", "vanilla", "saffron"],
    "10": ["wheat", "corn", "maize", "rice", "barley", "oats", "sorghum", "rye"],
    "12": ["soybeans", "rapeseed", "canola", "sunflower seeds", "palm kernel", "oilseeds"],
    "15": ["palm oil", "soybean oil", "vegetable oil", "olive oil",
           "sunflower oil", "tallow", "rapeseed oil"],
    "17": ["sugar", "raw sugar", "sugarcane", "molasses"],
    "18": ["cocoa", "cocoa beans", "chocolate"],
    "24": ["tobacco", "cigarettes", "vapes"],
    "25": ["cement", "salt", "sulfur", "limestone", "gypsum"],
    "26": ["iron ore", "copper ore", "bauxite", "nickel ore", "rare earth ores",
           "manganese ore", "zinc ore"],
    "27": ["petroleum", "crude oil", "natural gas", "lng", "gasoline",
           "diesel", "jet fuel", "coal", "coking coal", "thermal coal",
           "fuel oil", "lpg", "heating oil"],
    "28": ["lithium", "uranium", "ammonia", "sulfuric acid", "chlorine",
           "hydrogen", "phosphoric acid"],
    "29": ["ethylene", "propylene", "methanol", "benzene", "xylene", "ethanol"],
    "30": ["pharmaceuticals", "vaccines", "drugs", "medicines",
           "active pharmaceutical ingredients", "generic drugs"],
    "31": ["fertilizer", "urea", "potash", "phosphate",
           "nitrogen fertilizer", "dap fertilizer", "map fertilizer"],
    "39": ["plastics", "polyethylene", "polypropylene", "pvc", "pet resin"],
    "40": ["rubber", "natural rubber", "tires", "synthetic rubber"],
    "41": ["leather", "hides", "cattle hides"],
    "44": ["lumber", "timber", "plywood", "softwood lumber", "hardwood"],
    "47": ["wood pulp", "paper pulp"],
    "48": ["paper", "paperboard", "newsprint", "containerboard"],
    "50": ["silk"],
    "51": ["wool"],
    "52": ["cotton", "cotton yarn", "cotton lint"],
    "53": ["jute", "flax", "hemp"],
    "61": ["apparel", "clothing", "garments"],
    "62": ["apparel", "clothing", "garments"],
    "64": ["footwear", "shoes", "sneakers"],
    "71": ["gold", "silver", "platinum", "palladium", "diamonds", "jewellery"],
    "72": ["steel", "iron ore", "rebar", "hot-rolled coil", "hrc",
           "billet", "pig iron", "stainless steel"],
    "73": ["steel pipes", "steel tubes", "steel products"],
    "74": ["copper", "copper concentrate", "copper cathode", "refined copper"],
    "75": ["nickel"],
    "76": ["aluminum", "aluminium", "bauxite", "alumina", "aluminum sheet"],
    "78": ["lead", "lead ingot"],
    "79": ["zinc"],
    "80": ["tin"],
    "81": ["tungsten", "cobalt", "molybdenum", "titanium", "rare earths",
           "rare earth metals"],
    "84": ["machinery", "industrial equipment", "machine tools",
           "semiconductor equipment", "lithography"],
    "85": ["semiconductors", "chips", "batteries", "lithium batteries",
           "ev batteries", "solar panels", "transformers", "consumer electronics"],
    "87": ["automobiles", "auto parts", "electric vehicles", "evs", "trucks",
           "passenger vehicles"],
    "88": ["aircraft", "airplane parts", "jet engines", "commercial aircraft"],
    "89": ["ships", "vessels", "shipbuilding", "container ships", "tankers"],
    "90": ["medical devices", "optical instruments", "lithography machines"],
    "93": ["arms", "weapons", "ammunition", "firearms", "military equipment"],
}

# Single-word terms that are too noisy by themselves — pair with a
# context word ("market", "exports", "prices") via the loader's query
# builder, OR drop in favor of a more specific phrase.  Listed here so
# the generator can flag them in the output.
NOISY_SINGLE_WORDS = {
    "gold", "silver", "lead", "tin", "iron", "steel", "wheat", "cotton",
    "rubber", "coffee", "tea", "fish", "meat", "wood", "paper",
    "copper", "zinc", "nickel", "salt", "stone", "sugar", "rice",
}


# ─────────────────────────────────────────────────────────────────────
# EXTRACTION
# ─────────────────────────────────────────────────────────────────────

def clean_phrase(phrase: str) -> str:
    p = phrase.lower()
    for pat in STRIP_PHRASES:
        p = re.sub(pat, " ", p, flags=re.IGNORECASE)
    p = re.sub(r"\([^)]*\)", " ", p)        # strip parentheticals
    p = re.sub(r"[';,.:]", " ", p)
    p = re.sub(r"\s+", " ", p).strip()
    return p


def extract_terms(description: str) -> set[str]:
    """Extract candidate search terms from one HS description."""
    if not description:
        return set()
    terms: set[str] = set()

    # Split on semicolons first — each part is usually a distinct sub-cat.
    for part in re.split(r"[;:]", description):
        part = clean_phrase(part)
        if not part:
            continue

        words = [w for w in part.split()
                 if w not in STOP_WORDS and len(w) > 2]
        if not words:
            continue

        # The whole short phrase (up to 3 content words)
        if 1 <= len(words) <= 3:
            terms.add(" ".join(words))

        # Individual content words ≥ 4 chars
        for w in words:
            if len(w) >= 4:
                terms.add(w)

    return terms


# ─────────────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────────────

def build_rows(input_path: Path) -> list[dict]:
    # Group descriptions by cmd_code (handles HS-revision duplicates).
    desc_by_code: dict[str, list[str]] = defaultdict(list)
    with open(input_path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            code = (row.get("cmd_code") or "").strip()
            desc = (row.get("cmd_desc") or "").strip()
            if code and desc and desc not in desc_by_code[code]:
                desc_by_code[code].append(desc)

    out: list[dict] = []
    for code in sorted(desc_by_code.keys()):
        descriptions = desc_by_code[code]
        extracted = set()
        for d in descriptions:
            extracted.update(extract_terms(d))

        curated = CURATED_TERMS.get(code.zfill(2), [])
        curated_lc = {t.lower() for t in curated}

        # Curated terms first — they're the most reliable matches.
        for term in curated:
            out.append({
                "cmd_code":    code,
                "search_term": term,
                "term_type":   "primary",
                "language":    "en",
                "priority":    1,
                "is_active":   1,
                "source":      "curated",
                "notes":       _noise_note(term),
            })

        # Then extracted terms, deduped against curated.
        # If the chapter has curated terms, extracted ones become 'synonym'.
        has_curated = bool(curated)
        for term in sorted(extracted):
            if term.lower() in curated_lc:
                continue
            if len(term) < 4:
                continue
            word_count = len(term.split())
            out.append({
                "cmd_code":    code,
                "search_term": term,
                "term_type":   "synonym" if has_curated else "primary",
                "language":    "en",
                "priority":    3 if word_count == 1 else 4,
                "is_active":   1,
                "source":      "extracted",
                "notes":       _noise_note(term),
            })

    return out


def _noise_note(term: str) -> str:
    if term.lower() in NOISY_SINGLE_WORDS:
        return "noisy alone — pair with context word in loader"
    return ""


def write_csv(rows: list[dict], path: Path) -> None:
    fields = ["cmd_code", "search_term", "term_type", "language",
              "priority", "is_active", "source", "notes"]
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Generate GDELT search terms from HS commodity descriptions."
    )
    ap.add_argument("--input",  required=True,
                    help="CSV with cmd_code,cmd_desc columns")
    ap.add_argument("--output", default="commodity_search_terms.csv",
                    help="Output CSV path")
    args = ap.parse_args()

    rows = build_rows(Path(args.input))
    write_csv(rows, Path(args.output))

    # Summary
    by_code = defaultdict(list)
    for r in rows:
        by_code[r["cmd_code"]].append(r)
    curated_codes = sum(
        1 for code, rs in by_code.items()
        if any(r["source"] == "curated" for r in rs)
    )
    print(f"  cmd_codes processed:        {len(by_code)}")
    print(f"  cmd_codes with curated set: {curated_codes}")
    print(f"  total search terms:         {len(rows)}")
    print(f"  output:                     {args.output}")


if __name__ == "__main__":
    main()
