"""Tests for work_index.py. Run: python3 -m unittest discover -s .bc-agent/scripts -p 'test_*.py'"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import work_index  # noqa: E402

STAGES = ("stage 1", "stage 2", "unscheduled")


def item(blocked_by: str | None = "nothing", stage: str | None = "stage 1") -> str:
    lines = ["# t", "", "Why: because."] + ([f"Stage: {stage}"] if stage is not None else [])
    if blocked_by is not None:
        lines.append(f"Blocked by: {blocked_by}")
    return "\n".join(lines + ["", "## Steps", "", "- [ ] a", ""])


class WorkIndexTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.work = Path(self.tmp.name) / "work"
        self.work.mkdir()
        (self.work / "0-ideas.md").write_text("- idea\n")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def add(self, rel: str, text: str = "") -> None:
        path = self.work / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text or item())

    def rows(self) -> list[list[str]]:
        block = work_index.build_block(self.work, STAGES)
        lines = [l for l in block.splitlines() if l.startswith("| ") and not l.startswith("| #") and "---" not in l]
        return [[c.strip() for c in l.strip("|").split(" | ")] for l in lines]

    def names(self) -> list[str]:
        return [r[1].split("]")[0].lstrip("[") for r in self.rows()]

    def assertFails(self, pattern: str) -> None:
        with self.assertRaisesRegex(work_index.WorkIndexError, pattern):
            work_index.build_block(self.work, STAGES)

    def test_missing_blocked_by_line(self) -> None:
        self.add("1-planned/001-a.md", item(blocked_by=None))
        self.assertFails(r"001-a\.md.*Blocked by")

    def test_missing_stage_sorts_last(self) -> None:
        self.add("1-planned/001-a.md", item(stage=None))
        self.add("1-planned/002-b.md", item(stage="stage 2"))
        self.assertEqual(self.names(), ["002-b", "001-a"])

    def test_unknown_stage(self) -> None:
        self.add("1-planned/001-a.md", item(stage="someday"))
        self.assertFails(r"001-a\.md.*unknown Stage")

    def test_unknown_name(self) -> None:
        self.add("1-planned/001-a.md", item("099-ghost"))
        self.assertFails(r"001-a\.md.*unknown.*099-ghost")

    def test_dropped_blocker(self) -> None:
        self.add("4-dropped/002-gone.md")
        self.add("1-planned/001-a.md", item("002-gone"))
        self.assertFails(r"001-a\.md.*dropped.*002-gone")

    def test_self_block(self) -> None:
        self.add("1-planned/001-a.md", item("001-a"))
        self.assertFails(r"001-a\.md.*itself")

    def test_cycle(self) -> None:
        self.add("1-planned/001-a.md", item("002-b"))
        self.add("1-planned/002-b/plan.md", item("001-a"))
        self.assertFails(r"cycle.*001-a.*002-b")

    def test_done_blocker_is_satisfied(self) -> None:
        self.add("3-done/2026-01/005-old/plan.md")
        self.add("1-planned/001-a.md", item("005-old"))
        self.assertEqual(self.rows()[0][3], "—")

    def test_unblocks_most_first(self) -> None:
        self.add("1-planned/001-few.md")
        self.add("1-planned/002-many.md")
        self.add("1-planned/003-x.md", item("002-many"))
        self.add("1-planned/004-y.md", item("003-x"))
        self.add("1-planned/005-z.md", item("001-few"))
        self.assertEqual(self.names(), ["002-many", "001-few", "003-x", "004-y", "005-z"])

    def test_blocked_after_blocker_even_with_earlier_stage(self) -> None:
        self.add("1-planned/001-late.md", item(stage="unscheduled"))
        self.add("1-planned/002-early.md", item("001-late", stage="stage 1"))
        self.assertEqual(self.names(), ["001-late", "002-early"])

    def test_active_first_then_stage_then_name(self) -> None:
        self.add("1-planned/001-a.md", item(stage="stage 2"))
        self.add("1-planned/002-b.md", item(stage="stage 1"))
        self.add("2-active/003-c.md", item(stage="unscheduled"))
        self.assertEqual(self.names(), ["003-c", "002-b", "001-a"])

    def test_columns(self) -> None:
        self.add("1-planned/001-a.md")
        self.add("1-planned/002-b.md", item("001-a"))
        self.add("1-planned/003-c/plan.md", item("001-a, 002-b"))
        rows = {r[1].split("]")[0].lstrip("["): r for r in self.rows()}
        self.assertEqual([r[0] for r in self.rows()], ["1", "2", "3"])
        self.assertEqual(rows["001-a"][3:5], ["—", "002-b, 003-c"])
        self.assertEqual(rows["003-c"][3], "[001-a](1-planned/001-a.md), [002-b](1-planned/002-b.md)")
        self.assertEqual(rows["003-c"][4], "—")
        self.assertEqual(rows["001-a"][2], "planned")


if __name__ == "__main__":
    unittest.main()
