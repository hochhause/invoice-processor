"""
md_clean: strip degenerate table rows from markitdown output.
Adapted from C:\ClaudeTools\markitdown\md-clean.py for in-process use.
"""
import re


def is_table_row(line):
    s = line.strip()
    return s.startswith("|") and s.endswith("|")


def is_sep_row(line):
    s = line.strip()
    if not s.startswith("|"):
        return False
    cells = [c.strip() for c in s.strip("|").split("|")]
    return all(re.match(r"^-*$", c) for c in cells)


def parse_cells(line):
    return [c.strip() for c in line.strip().strip("|").split("|")]


def process_block(block):
    rows = []
    col_content = []

    for line in block:
        if is_sep_row(line):
            rows.append(("sep", []))
            continue
        cells = parse_cells(line)
        rows.append(("data", cells))
        while len(col_content) < len(cells):
            col_content.append([])
        for i, c in enumerate(cells):
            col_content[i].append(c)

    active = [i for i, col in enumerate(col_content) if any(col)]
    if not active:
        return []

    result = []
    for kind, cells in rows:
        if kind == "sep":
            result.append("| " + " | ".join(["---"] * len(active)) + " |")
            continue
        kept = [cells[i] if i < len(cells) else "" for i in active]
        if not any(kept):
            continue
        result.append("| " + " | ".join(kept) + " |")

    data_rows = [r for r in result if not r.startswith("| ---")]
    if not data_rows:
        return []

    deduped = []
    prev = None
    for r in result:
        if r.startswith("| ---") and r == prev:
            continue
        deduped.append(r)
        prev = r

    final = []
    for idx, r in enumerate(deduped):
        if r.startswith("| ---"):
            before = any(not deduped[j].startswith("| ---") for j in range(max(0, idx - 1), idx))
            after = any(not deduped[j].startswith("| ---") for j in range(idx + 1, min(len(deduped), idx + 2)))
            if not (before or after):
                continue
        final.append(r)

    return final


def clean_markdown(text: str) -> str:
    lines = text.splitlines()
    output = []
    i = 0
    while i < len(lines):
        if is_table_row(lines[i]):
            block = []
            while i < len(lines) and is_table_row(lines[i]):
                block.append(lines[i])
                i += 1
            cleaned = process_block(block)
            if cleaned:
                output.extend(cleaned)
        else:
            output.append(lines[i])
            i += 1

    final, blanks = [], 0
    for line in output:
        if line.strip() == "":
            blanks += 1
            if blanks <= 2:
                final.append(line)
        else:
            blanks = 0
            final.append(line)

    return "\n".join(final)
