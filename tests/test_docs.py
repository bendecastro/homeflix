from __future__ import annotations

import re
from pathlib import Path
import tempfile
import unittest
from urllib.parse import unquote


ROOT = Path(__file__).resolve().parents[1]
INTENTS = (
    "Set up Homeflix core on this Debian or Ubuntu machine using my existing mounted storage.",
    "Resume Homeflix core setup and verify the live deployment.",
    "Show me a dry-run plan for Homeflix core setup without changing this machine.",
)
CHECKED_DOCS = (
    ROOT / "README.md",
    ROOT / "AGENTS.md",
    ROOT / "docs" / "agent-setup.md",
    ROOT / "docs" / "configuration.md",
)
STACK_CONTRACT_WORKFLOW = ROOT / ".github" / "workflows" / "stack-contract.yml"
LINK_PATTERN = re.compile(r"(?<!!)\[[^\]]*\]\(([^)]+)\)")


def markdown_anchor(headline: str) -> str:
    value = re.sub(r"[^\w\- ]", "", headline.strip().casefold())
    return re.sub(r"[ -]+", "-", value).strip("-")


def extract_markdown_targets(text: str) -> list[str]:
    return LINK_PATTERN.findall(text)


def validate_local_target(document: Path, raw_target: str, *, root: Path = ROOT) -> Path | None:
    stripped = raw_target.strip()
    if stripped.startswith("<"):
        closing = stripped.find(">")
        target = stripped[1:closing] if closing >= 0 else stripped
    else:
        target = stripped.split(maxsplit=1)[0]
    if re.match(r"^[a-z][a-z0-9+.-]*:", target, re.I):
        return None
    path_text, _, anchor = unquote(target).partition("#")
    candidate = (document if not path_text else document.parent / path_text).resolve()
    repository_root = root.resolve()
    try:
        candidate.relative_to(repository_root)
    except ValueError as error:
        raise AssertionError(f"local link escapes repository: {target}") from error
    if not candidate.exists():
        raise AssertionError(f"missing local link target: {target}")
    if anchor and candidate.is_file() and candidate.suffix.casefold() == ".md":
        headings = re.findall(r"^#{1,6}\s+(.+?)\s*$", candidate.read_text(encoding="utf-8"), re.M)
        anchors = {markdown_anchor(heading) for heading in headings}
        if anchor.casefold() not in anchors:
            raise AssertionError(f"missing Markdown anchor: {target}")
    return candidate


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
        manual = readme.split("## Manual quickstart fallback", 1)[1].split("## What you get", 1)[0]
        self.assertNotIn("sudo mkdir", manual)
        self.assertNotIn('"$DATA_ROOT"', manual)

    def test_public_environment_template_uses_only_portable_examples(self) -> None:
        template = (ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIn("DATA_ROOT=/srv/homeflix/data", template)
        self.assertIn("TZ=UTC", template)
        self.assertIn("BACKUP_DEST=", template)
        self.assertIn("BACKUP_KEEP=7", template)
        for private_value in ("/mnt/sidecar", "Europe/Lisbon", "/home/ben", "beelink"):
            self.assertNotIn(private_value, template)

    def test_stack_contract_ci_renders_example_env_and_runs_cli_json(self) -> None:
        workflow = STACK_CONTRACT_WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("docker compose --env-file .env.example config --format json", workflow)
        self.assertIn("scripts/homeflix --json verify contract", workflow)
        self.assertNotIn("config --quiet", workflow)

    def test_glances_does_not_mount_docker_socket(self) -> None:
        compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        glances = compose.split("\n  glances:", 1)[1].split("\n  deunhealth:", 1)[0]
        self.assertNotIn("docker.sock", glances)
        self.assertIn("pid: host", glances)
        self.assertIn("/var/run/docker.sock", compose)

    def test_repo_local_markdown_links_exist_and_anchors_are_valid(self) -> None:
        for document in CHECKED_DOCS:
            for raw_target in extract_markdown_targets(document.read_text(encoding="utf-8")):
                with self.subTest(document=document.relative_to(ROOT), target=raw_target):
                    validate_local_target(document, raw_target)

    def test_local_target_validation_edge_cases_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            root = base / "repository"
            docs = root / "docs"
            docs.mkdir(parents=True)
            source = docs / "source.md"
            source.write_text("source\n", encoding="utf-8")
            target = docs / "space name.md"
            target.write_text("# Valid Anchor\n", encoding="utf-8")
            (base / "outside.md").write_text("# Outside\n", encoding="utf-8")

            cases = (
                ("https://example.invalid/not-fetched", None),
                ("space%20name.md#valid-anchor", target),
                ("<space%20name.md#valid-anchor> \"title\"", target),
            )
            for raw, expected in cases:
                with self.subTest(raw=raw):
                    self.assertEqual(validate_local_target(source, raw, root=root), expected)
            for raw, message in (
                ("../../outside.md", "escapes repository"),
                ("space%20name.md#missing-anchor", "missing Markdown anchor"),
            ):
                with self.subTest(raw=raw), self.assertRaisesRegex(AssertionError, message):
                    validate_local_target(source, raw, root=root)
            self.assertEqual(extract_markdown_targets("[`inline code`](space%20name.md)"), ["space%20name.md"])


if __name__ == "__main__":
    unittest.main()
