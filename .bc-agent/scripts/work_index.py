#!/usr/bin/env python3
"""Write the pipeline table in work/README.md from the open work items.

Usage: python3 .bc-agent/scripts/work_index.py [--check] [work-dir]
Tests: python3 -m unittest discover -s .bc-agent/scripts -p 'test_*.py'

Every item in 1-planned/ and 2-active/ needs a
`Blocked by:` line naming full item names (`030-media-panels`, comma-separated)
or `nothing`. Unknown names, dropped blockers, self-blocks and cycles among open
items are errors. A blocker in 3-done/ is satisfied; an item is ready when all
its blockers are done. A `Stage:` line is optional; an item without one sorts
in the last stage, and an unknown stage is an error.

The table is a topological order of the open items. Among the items available
at each point it picks: active before planned, ready before not ready, more
open items transitively unblocked first, then the stage order from the
`Stages:` line in `.bc-agent/AGENTS.md` (default: now, next, later,
unscheduled), then the name. `--check` writes nothing and exits 1 while the
table is out of date. Python 3.9 or later, standard library only.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

DEFAULT_STAGES = ("now", "next", "later", "unscheduled")
START = "<!-- work-index:start -->"
END = "<!-- work-index:end -->"
COMMAND = "python3 .bc-agent/scripts/work_index.py"
ITEM_NAME = re.compile(r"^\d{3}-")
MONTH = re.compile(r"^\d{4}-\d{2}$")
LINK = re.compile(r"!?\[([^\]]+)\]\((?:[^()]|\([^()]*\))*\)")
ABBREVIATION = re.compile(r"\b(?:e\.g|i\.e|vs|etc|Mr|Mrs|Ms|Dr)\.$", re.I)
WHY_LIMIT = 160


class WorkIndexError(Exception):
    pass


class StageError(WorkIndexError):
    pass


def read_text(path: Path) -> str:
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n")


def read_stages(project: Path) -> tuple[str, ...]:
    agents = project / ".bc-agent" / "AGENTS.md"
    if agents.is_file():
        match = re.search(r"^Stages:[ \t]*(.+)$", read_text(agents), re.M)
        if match:
            stages = tuple(s.strip() for s in match.group(1).split(",") if s.strip())
            if stages:
                return stages
    return DEFAULT_STAGES


def entries(work: Path, folder: str) -> list[str]:
    path = work / folder
    return sorted(name.name for name in path.iterdir() if ITEM_NAME.match(name.name)) if path.is_dir() else []


def first_sentence(why: str) -> str:
    why = LINK.sub(r"\1", why)
    in_code = False
    for i, char in enumerate(why):
        if char == "`":
            in_code = not in_code
        if char != "." or in_code or (i + 1 < len(why) and not why[i + 1].isspace()):
            continue
        if ABBREVIATION.search(why[: i + 1]):
            continue
        why = why[: i + 1]
        break
    if len(why) > WHY_LIMIT:
        why = why[: WHY_LIMIT - 1].rstrip() + "…"
    return why.replace("|", "\\|")


def read_item(work: Path, folder: str, name: str, stages: tuple[str, ...]) -> dict:
    rel = f"{folder}/{name}/plan.md" if (work / folder / name).is_dir() else f"{folder}/{name}"
    if not (work / rel).is_file():
        raise WorkIndexError(f"{rel}: missing")
    text = read_text(work / rel)
    stage_match = re.search(r"^Stage: (.+)$", text, re.M)
    stage = stage_match.group(1).strip() if stage_match else stages[-1]
    if stage not in stages:
        raise StageError(f"{rel}: unknown Stage line (one of: {', '.join(stages)})")
    why_match = re.search(r"^Why: (.+)$", text, re.M)
    why = first_sentence(why_match.group(1) if why_match else "")
    parts = re.split(r"^## Steps$", text, maxsplit=1, flags=re.M)
    steps = re.split(r"^## ", parts[1], maxsplit=1, flags=re.M)[0] if len(parts) > 1 else ""
    done = len(re.findall(r"^- \[x\]", steps, re.M | re.I))
    total = len(re.findall(r"^- \[[ x]\]", steps, re.M | re.I))
    blocked_match = re.search(r"^Blocked by:[ \t]*(.*)$", text, re.M)
    if not blocked_match or not blocked_match.group(1).strip():
        raise StageError(f"{rel}: missing Blocked by line (item names such as 030-media-panels, or nothing)")
    blocked = blocked_match.group(1).strip()
    blockers = [] if blocked == "nothing" else [b.strip() for b in blocked.split(",") if b.strip()]
    return {
        "blockers": blockers,
        "stage": stage, "name": name.removesuffix(".md"), "path": rel, "folder": folder[2:],
        "why": why, "steps": f"{done}/{total}" if total else "—",
    }


def closed_names(work: Path) -> dict[str, str]:
    """Map each done or dropped item name to its status folder."""
    names = {}
    folders = ["3-done", "4-dropped"]
    if (work / "3-done").is_dir():
        folders += [f"3-done/{m.name}" for m in (work / "3-done").iterdir() if m.is_dir() and MONTH.match(m.name)]
    for folder in folders:
        for name in entries(work, folder):
            names[name.removesuffix(".md")] = folder.split("/")[0]
    return names


def order_items(work: Path, items: list[dict], stages: tuple[str, ...]) -> list[dict]:
    open_items = {i["name"]: i for i in items}
    closed = closed_names(work)
    errors = []
    for i in items:
        for b in i["blockers"]:
            if b == i["name"]:
                errors.append(f"{i['path']}: Blocked by names the item itself")
            elif b in open_items:
                continue
            elif b not in closed:
                errors.append(f"{i['path']}: Blocked by names unknown item {b}")
            elif closed[b] == "4-dropped":
                errors.append(f"{i['path']}: Blocked by names dropped item {b}; change the plan")
    if errors:
        raise WorkIndexError("\n".join(errors))
    waits = {n: [b for b in i["blockers"] if b in open_items] for n, i in open_items.items()}
    dependents: dict[str, list[str]] = {n: [] for n in open_items}
    for n in sorted(open_items):
        for b in waits[n]:
            dependents[b].append(n)
    cycle = find_cycle(waits)
    if cycle:
        raise WorkIndexError(f"dependency cycle: {' -> '.join(cycle)}")

    def reach(n: str, seen: set[str]) -> set[str]:
        for d in dependents[n]:
            if d not in seen:
                seen.add(d)
                reach(d, seen)
        return seen

    def key(n: str) -> tuple:
        i = open_items[n]
        return (i["folder"] != "active", bool(waits[n]), -len(reach(n, set())), stages.index(i["stage"]), n)

    for n, i in open_items.items():
        i["waits"] = ", ".join(f"[{b}]({open_items[b]['path']})" for b in waits[n]) or "—"
        i["unblocks"] = ", ".join(dependents[n]) or "—"
    placed: list[str] = []
    remaining = set(open_items)
    while remaining:
        available = [n for n in remaining if all(b in placed for b in waits[n])]
        best = min(available, key=key)
        placed.append(best)
        remaining.remove(best)
    return [open_items[n] for n in placed]


def find_cycle(waits: dict[str, list[str]]) -> list[str] | None:
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(n: str) -> list[str] | None:
        state[n] = 1
        stack.append(n)
        for b in waits[n]:
            if state.get(b) == 1:
                return stack[stack.index(b):] + [b]
            if b not in state:
                found = visit(b)
                if found:
                    return found
        stack.pop()
        state[n] = 2
        return None

    for n in sorted(waits):
        if n not in state:
            found = visit(n)
            if found:
                return found
    return None


def build_block(work: Path, stages: tuple[str, ...]) -> str:
    items = []
    errors = []
    for folder in ("2-active", "1-planned"):
        for name in entries(work, folder):
            try:
                items.append(read_item(work, folder, name, stages))
            except StageError as error:
                errors.append(str(error))
    if errors:
        raise WorkIndexError("\n".join(errors))
    items = order_items(work, items, stages)
    done = len(entries(work, "3-done"))
    if (work / "3-done").is_dir():
        done += sum(len(entries(work, f"3-done/{month.name}"))
                    for month in (work / "3-done").iterdir() if month.is_dir() and MONTH.match(month.name))
    ideas_path = work / "0-ideas.md"
    if not ideas_path.is_file():
        raise WorkIndexError("0-ideas.md: missing")
    ideas = len(re.findall(r"^- ", read_text(ideas_path), re.M))
    return "\n".join([
        START,
        f"<!-- Generated by `{COMMAND}`; do not edit by hand. -->",
        "",
        "| # | Item | Folder | Waits on | Unblocks | Why | Steps |",
        "| --- | --- | --- | --- | --- | --- | --- |",
        *(f"| {n} | [{i['name']}]({i['path']}) | {i['folder']} | {i['waits']} | {i['unblocks']} | {i['why']} | {i['steps']} |"
          for n, i in enumerate(items, 1)),
        "",
        f"- [Ideas](0-ideas.md): {ideas}",
        f"- [Done](3-done/): {done}",
        f"- [Dropped](4-dropped/): {len(entries(work, '4-dropped'))}",
        END,
    ])


def main(argv: list[str]) -> int:
    check = "--check" in argv
    paths = [arg for arg in argv if arg != "--check"]
    work = Path(paths[0]) if paths else Path(__file__).resolve().parents[2] / "work"
    try:
        block = build_block(work, read_stages(work.resolve().parent))
        readme_path = work / "README.md"
        readme = read_text(readme_path)
        start, end = readme.find(START), readme.find(END)
        if start < 0 or end < start:
            raise WorkIndexError(f"{readme_path}: missing {START} … {END} markers")
        updated = readme[:start] + block + readme[end + len(END):]
        if check:
            if updated != readme:
                raise WorkIndexError(f"work/README.md is out of date; run {COMMAND}")
        elif updated != readme:
            readme_path.write_bytes(updated.encode("utf-8"))
    except (WorkIndexError, OSError, UnicodeDecodeError) as error:
        print(f"work-index: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
