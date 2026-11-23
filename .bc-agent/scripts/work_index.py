#!/usr/bin/env python3
"""Write the pipeline table in work/README.md from the open work items.

Usage: python3 .bc-agent/scripts/work_index.py [--check] [work-dir]

Every item in 1-planned/ and 2-active/ needs a `Blocked by:` line naming full
item names (`012-search-ranking`, comma-separated) or `nothing`. A `Stage:` line
is optional; an unknown stage is an error. Unknown names, dropped blockers, self-blocks and cycles among open
items are errors. A blocker in 3-done/ is satisfied; an item is ready when all
its blockers are done.

The optional work/order.md is the owner's ranking of open items, top first, as
a numbered list (`1. 012-search-ranking`, ASCII digits); other lines are ignored.
Lines end at \r\n, \r or \n; whitespace in a line is space or tab only.
Names in 3-done/ or 4-dropped/ (or their month folders) are skipped unless an
open item has the same name; an unknown or repeated name is an error.
Without the file there is no owner order; the three tiers below still apply.
Unlisted blockers do not inherit a listed item’s rank; dashboard-written orders list every open item, so this
only affects hand-written partial lists.

The table is a topological order of the open items: an item never precedes an
open item it is transitively blocked by. Among the items available at each
point it picks: active items, then planned items that an active item is
(transitively) blocked by, then other planned items, then owner order (listed before
unlisted), ready before not ready, more open items transitively unblocked
first, then the stage order from the
`Stages:` line in `.bc-agent/AGENTS.md` (default: now, next, later,
unscheduled; an item without a `Stage:` line sorts after every stage), then
the name. A valid owner order is a fixed point: ranking by
it reproduces it. `--check` writes nothing and exits 1 while the
table is out of date, and, when work/order.md exists, while it leaves out an
open item (the message names where to add it: directly above the first listed
item that waits on it, else at the bottom) or lists an item above an item it
is Blocked by. The owner's dashboard writes every open item and never puts one
above its blocker, so either fault comes from an agent. Python 3.9 or later, standard library only.
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
ORDER_LINE = re.compile(r"^[ \t]*[0-9]+\.[ \t]+([0-9]{3}-[^ \t]+)[ \t]*$")
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
    stage = stage_match.group(1).strip() if stage_match else None
    if stage is not None and stage not in stages:
        raise StageError(f"{rel}: unknown Stage line (one of: {', '.join(stages)})")
    why_match = re.search(r"^Why: (.+)$", text, re.M)
    why = first_sentence(why_match.group(1) if why_match else "")
    parts = re.split(r"^## Steps$", text, maxsplit=1, flags=re.M)
    steps = re.split(r"^## ", parts[1], maxsplit=1, flags=re.M)[0] if len(parts) > 1 else ""
    done = len(re.findall(r"^- \[x\]", steps, re.M | re.I))
    total = len(re.findall(r"^- \[[ x]\]", steps, re.M | re.I))
    blocked_match = re.search(r"^Blocked by:[ \t]*(.*)$", text, re.M)
    if not blocked_match or not blocked_match.group(1).strip():
        raise StageError(f"{rel}: missing Blocked by line (item names such as 012-search-ranking, or nothing)")
    blocked = blocked_match.group(1).strip()
    blockers = [] if blocked == "nothing" else [b.strip() for b in blocked.split(",") if b.strip()]
    return {
        "blockers": blockers,
        "stage": stage, "name": name.removesuffix(".md"), "path": rel, "folder": folder[2:],
        "why": why, "steps": f"{done}/{total}" if total else "—",
    }


def archive_folders(work: Path, top: str) -> list[str]:
    """A status folder plus its month folders."""
    folders = [top]
    if (work / top).is_dir():
        folders += [f"{top}/{m.name}" for m in (work / top).iterdir() if m.is_dir() and MONTH.match(m.name)]
    return folders


def closed_names(work: Path) -> dict[str, str]:
    """Map each done or dropped item name to its status folder."""
    names = {}
    for folder in archive_folders(work, "3-done") + archive_folders(work, "4-dropped"):
        for name in entries(work, folder):
            names[name.removesuffix(".md")] = folder.split("/")[0]
    return names


def read_owner_order(work: Path, open_items: dict | set, closed: dict[str, str]) -> tuple[dict[str, int], list[str]]:
    """Rank of each open item listed in work/order.md, plus errors for unknown or repeated names."""
    path = work / "order.md"
    if not path.is_file():
        return {}, []
    rank: dict[str, int] = {}
    seen: set[str] = set()
    errors = []
    for line in re.split(r"\r\n|\r|\n", read_text(path)):
        match = ORDER_LINE.match(line)
        if not match:
            continue
        name = match.group(1)
        if name in closed and name not in open_items:
            continue
        if name in seen:
            errors.append(f"order.md: names {name} more than once")
            continue
        seen.add(name)
        if name in open_items:
            rank[name] = len(rank)
        elif name not in closed:
            errors.append(f"order.md: names unknown item {name}")
    return rank, errors


def order_items(work: Path, items: list[dict], stages: tuple[str, ...]) -> list[dict]:
    open_items = {i["name"]: i for i in items}
    closed = closed_names(work)
    rank, errors = read_owner_order(work, open_items, closed)
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

    feeds_active: set[str] = set()
    stack = [b for n, i in open_items.items() if i["folder"] == "active" for b in waits[n]]
    while stack:
        b = stack.pop()
        if b not in feeds_active:
            feeds_active.add(b)
            stack.extend(waits[b])

    def key(n: str) -> tuple:
        i = open_items[n]
        tier = 0 if i["folder"] == "active" else 1 if n in feeds_active else 2
        return (tier, rank.get(n, float("inf")), bool(waits[n]), -len(reach(n, set())), stages.index(i["stage"]) if i["stage"] else len(stages), n)

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
        open_names = {n.removesuffix(".md") for f in ("2-active", "1-planned") for n in entries(work, f)}
        errors += read_owner_order(work, open_names, closed_names(work))[1]
        raise WorkIndexError("\n".join(errors))
    items = order_items(work, items, stages)
    done = sum(len(entries(work, f)) for f in archive_folders(work, "3-done"))
    dropped = sum(len(entries(work, f)) for f in archive_folders(work, "4-dropped"))
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
        f"- [Dropped](4-dropped/): {dropped}",
        END,
    ])


def owner_order_errors(work: Path, stages: tuple[str, ...]) -> list[str]:
    """What --check reports about work/order.md: open items it leaves out, and items listed above an item they wait on.

    The owner's dashboard writes every open item and refuses to put one above its blocker, so either fault means an
    agent created an item or added a Blocked by line without placing it; the message says where it goes.
    """
    if not (work / "order.md").is_file():
        return []
    items = {i["name"]: i for i in (read_item(work, f, n, stages) for f in ("2-active", "1-planned") for n in entries(work, f))}
    rank, _ = read_owner_order(work, items, closed_names(work))
    waits = {n: [b for b in i["blockers"] if b in items] for n, i in items.items()}

    def waits_on(n: str, target: str, seen: set[str]) -> bool:
        for b in waits[n]:
            if b == target or (b not in seen and (seen.add(b) or waits_on(b, target, seen))):
                return True
        return False

    listed = sorted(rank, key=rank.get)
    errors = []
    for n in sorted(set(items) - set(rank)):
        first = next((m for m in listed if waits_on(m, n, set())), None)
        where = f"directly above {first}, the first listed item that waits on it" if first else "at the bottom"
        errors.append(f"order.md does not list {n}; add it {where}, and move no other line (the owner sets this order)")
    for n in listed:
        for b in waits[n]:
            if b in rank and rank[b] > rank[n]:
                errors.append(f"order.md lists {n} above {b}, which it waits on; check with the owner before changing the Blocked by line or the order")
    return errors


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
            errors = owner_order_errors(work, read_stages(work.resolve().parent))
            if updated != readme:
                errors.insert(0, f"work/README.md is out of date; run {COMMAND}")
            if errors:
                raise WorkIndexError("\n".join(errors))
        elif updated != readme:
            readme_path.write_bytes(updated.encode("utf-8"))
    except (WorkIndexError, OSError, UnicodeDecodeError) as error:
        print(f"work-index: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
