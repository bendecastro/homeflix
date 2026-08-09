from __future__ import annotations

import re
from pathlib import Path
import unittest
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
INTENTS = (
    "Set up Homeflix core on this Debian or Ubuntu machine using my existing mounted storage.",
    "Resume Homeflix core setup and verify the live deployment.",
    "Show me a dry-run plan for Homeflix core setup without changing this machine.",
)
CHECKED_DOCS = (ROOT / "README.md", ROOT / "AGENTS.md", ROOT / "docs" / "agent-setup.md")


def markdown_anchor(headline: str) -> str:
    value = re.sub(r"[^\w\- ]", "", headline.strip().casefold())
    return re.sub(r"[ -]+", "-", value).strip("-")


class DocumentationContractTests(unittest.TestCase):
    def test_agent_first_intents_and_routes_are_exact(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        guide = (ROOT / "docs" / "agent-setup.md").read_text(encoding="utf-8")
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        for intent in INTENTS:
            self.assertIn(intent, readme)
            self.assertIn(intent, guide)
            self.assertNotIn(intent, agents, "AGENTS must route rather than duplicate the guide")
        self.assertEqual(len(re.findall(r"\]\(docs/agent-setup\.md\)", agents)), 1)
        self.assertIn("docs/quickstart.md", readme)
        self.assertRegex(readme, r"(?i)manual quickstart")

    def test_repo_local_markdown_links_exist_and_anchors_are_valid(self) -> None:
        link_pattern = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")
        for document in CHECKED_DOCS:
            text = document.read_text(encoding="utf-8")
            for raw_target in link_pattern.findall(text):
                target = raw_target.strip().split(maxsplit=1)[0].strip("<>")
                if re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I):
                    continue
                path_text, _, anchor = unquote(target).partition("#")
                candidate = document if not path_text else (document.parent / path_text).resolve()
                with self.subTest(document=document.relative_to(ROOT), target=target):
                    self.assertTrue(candidate.exists(), f"missing local link target: {target}")
                    try:
                        candidate.relative_to(ROOT)
                    except ValueError:
                        self.fail(f"local link escapes repository: {target}")
                    if anchor and candidate.is_file() and candidate.suffix.casefold() == ".md":
                        headings = re.findall(r"^#{1,6}\s+(.+?)\s*$", candidate.read_text(encoding="utf-8"), re.M)
                        anchors = {markdown_anchor(heading) for heading in headings}
                        self.assertIn(anchor.casefold(), anchors, f"missing Markdown anchor: {target}")


if __name__ == "__main__":
    unittest.main()
