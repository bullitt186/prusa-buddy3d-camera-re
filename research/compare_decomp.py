#!/usr/bin/env python3
"""Sequence-align two ExportAllDecomp corpora after address normalization."""

import argparse
import csv
import difflib
import hashlib
import re
from pathlib import Path


AUTO_SYMBOL = re.compile(r"\b(FUN|DAT|LAB|UNK|PTR|SUB|switchD|caseD)_[0-9a-fA-F]+\b")
SUFFIXED_ADDRESS = re.compile(r"\b([A-Za-z][A-Za-z0-9_.$@<>-]*?)_[0-9a-fA-F]{6,8}\b")
LARGE_HEX = re.compile(r"\b0x[0-9a-fA-F]{5,}\b")
HEADER = re.compile(r"^/\* (?:entry|name|body):.*?\*/\n", re.MULTILINE)


def normalize(text: str) -> str:
    text = HEADER.sub("", text)
    text = AUTO_SYMBOL.sub(lambda m: f"{m.group(1)}_ADDR", text)
    text = SUFFIXED_ADDRESS.sub(lambda m: f"{m.group(1)}_ADDR", text)
    # Ghidra emits relocated pointers as raw literals in PLT/init code. Constants
    # this large are addresses in this 32-bit image; protocol enums/ranges remain.
    text = LARGE_HEX.sub("0xADDR", text)
    return "\n".join(line.rstrip() for line in text.splitlines()).strip()


def load(root: Path):
    rows = []
    with (root / "functions.tsv").open(newline="", encoding="utf-8") as stream:
        for row in csv.DictReader(stream, delimiter="\t"):
            text = normalize((root / row["file"]).read_text(encoding="utf-8"))
            row["normalized"] = text
            row["hash"] = hashlib.sha256(text.encode()).hexdigest()
            rows.append(row)
    return rows


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("old", type=Path)
    parser.add_argument("new", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    old = load(args.old)
    new = load(args.new)
    matcher = difflib.SequenceMatcher(
        None, [row["hash"] for row in old], [row["hash"] for row in new], autojunk=True
    )

    with args.output.open("w", encoding="utf-8") as out:
        out.write(f"old_functions={len(old)}\nnew_functions={len(new)}\n")
        unchanged = sum(block.size for block in matcher.get_matching_blocks())
        out.write(f"sequence_identical={unchanged}\n")
        out.write(f"old_unmatched={len(old) - unchanged}\n")
        out.write(f"new_unmatched={len(new) - unchanged}\n\n")

        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue
            out.write(f"[{tag}] old[{i1}:{i2}] new[{j1}:{j2}]\n")
            old_block = old[i1:i2]
            new_block = new[j1:j2]
            for label, block in (("OLD", old_block), ("NEW", new_block)):
                for row in block:
                    out.write(
                        f"  {label} {row['entry']} {row['name']} "
                        f"body={row['body_addresses']} sha256={row['hash'][:16]}\n"
                    )

            if old_block and new_block and len(old_block) <= 20 and len(new_block) <= 20:
                scored = []
                for oi, left in enumerate(old_block):
                    for nj, right in enumerate(new_block):
                        ratio = difflib.SequenceMatcher(
                            None,
                            left["normalized"].splitlines(),
                            right["normalized"].splitlines(),
                            autojunk=True,
                        ).ratio()
                        scored.append((ratio, oi, nj))
                used_old = set()
                used_new = set()
                for ratio, oi, nj in sorted(scored, reverse=True):
                    if oi in used_old or nj in used_new:
                        continue
                    used_old.add(oi)
                    used_new.add(nj)
                    out.write(
                        f"  PAIR similarity={ratio:.5f} "
                        f"{old_block[oi]['entry']} -> {new_block[nj]['entry']}\n"
                    )
            out.write("\n")


if __name__ == "__main__":
    main()
