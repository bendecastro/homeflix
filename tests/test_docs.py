from __future__ import annotations

import re
from pathlib import Path
import subprocess
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
        self.assertIn("**two** Jellyfin connections", guide)
        self.assertIn("Library/Refresh", guide)
        self.assertIn("Library/Media/Updated", guide)
        self.assertIn("X-Emby-Token", guide)
        self.assertIn("built-in connection Test", guide)
        self.assertIn("/Notifications/Admin", guide)
        first_use = (ROOT / "docs" / "first-use.md").read_text(encoding="utf-8")
        self.assertIn("Emby / Jellyfin", first_use)
        self.assertIn("Webhook", first_use)
        self.assertIn("Library/Refresh", first_use)
        self.assertIn("/Library/Media/Updated", first_use)
        self.assertIn("/Notifications/Admin", first_use)
        self.assertIn("empty or unwatched", first_use)
        self.assertNotIn("asks Jellyfin where", first_use)
        self.assertNotIn("the refresh has no target", first_use)
        self.assertIn("request-to-library", first_use)
        self.assertIn("credentials_required", first_use)
        spec = (ROOT / "docs" / "specs" / "setup-reconciliation.md").read_text(encoding="utf-8")
        self.assertIn("(satisfied #6)", spec)
        self.assertIn("(satisfied #11)", spec)
        self.assertIn("(satisfied #12)", spec)
        self.assertIn("--clients", guide)
        self.assertIn("secrets usenet", guide)
        self.assertIn("credentials_required", guide)
        self.assertIn("request-to-library", guide)
        self.assertIn("fixture-tested only", guide)
        quickstart = (ROOT / "docs" / "quickstart.md").read_text(encoding="utf-8")
        self.assertIn("--clients", quickstart)
        self.assertIn("fixture-accepted", quickstart)
        verification = (ROOT / "docs" / "specs" / "verification.md").read_text(encoding="utf-8")
        self.assertIn("satisfied #12", verification)
        self.assertNotIn("Optional NZBGet SHALL remain stopped and unconfigured unless selected; provider servers SHALL remain disabled until credentials are present. `(pending #12)`", spec)
        self.assertNotIn("reliable Jellyfin discovery for both known and genuinely new imported titles. `(pending #3)`", spec)
        self.assertNotIn("fail-closed evidence before starting any risky service. `(pending #11", spec)
        backup = (ROOT / "docs" / "specs" / "backup-recovery.md").read_text(encoding="utf-8")
        self.assertIn("(satisfied #7)", backup)
        self.assertIn("(satisfied #8)", backup)
        self.assertNotIn("cannot be snapshotted consistently SHALL prevent artifact publication. `(pending #3)`", backup)
        self.assertNotIn("pending #8 for SSH", backup)
        self.assertEqual(len(re.findall(r"\]\(docs/agent-setup\.md\)", agents)), 1)
        self.assertIn("docs/quickstart.md", readme)
        self.assertRegex(readme, r"(?i)manual quickstart")
        manual = readme.split("## Manual quickstart fallback", 1)[1].split("## What you get", 1)[0]
        self.assertNotIn("sudo mkdir", manual)
        self.assertNotIn('"$DATA_ROOT"', manual)

    def test_implemented_spec_rows_cannot_return_to_pending_parent(self) -> None:
        specs = {
            "setup-reconciliation.md": (ROOT / "docs" / "specs" / "setup-reconciliation.md").read_text(encoding="utf-8"),
            "verification.md": (ROOT / "docs" / "specs" / "verification.md").read_text(encoding="utf-8"),
            "backup-recovery.md": (ROOT / "docs" / "specs" / "backup-recovery.md").read_text(encoding="utf-8"),
            "stack-contract.md": (ROOT / "docs" / "specs" / "stack-contract.md").read_text(encoding="utf-8"),
        }
        stale_pending = (
            "inspect live state and make only the smallest safe changes to setup-owned state. `(pending #3)`",
            "A no-change rerun SHALL NOT duplicate accounts, libraries, roots, connections, categories, clients, applications, or indexers. `(pending #3)`",
            "fail on ambiguous conflicting resources. `(satisfied #11 for torrent clients/apps; pending #3)`",
            "explicit static, read-only runtime, and disruptive verification intents. `(pending #3;",
            "live request-to-library remains pending #3",
            "cannot be snapshotted consistently SHALL prevent artifact publication. `(pending #3)`",
            "reliable Jellyfin discovery for both known and genuinely new imported titles. `(pending #3)`",
        )
        for name, text in specs.items():
            for phrase in stale_pending:
                with self.subTest(document=name, phrase=phrase):
                    self.assertNotIn(phrase, text)
            with self.subTest(document=name, phrase="pending #3"):
                self.assertNotIn("pending #3", text)
        verification = specs["verification.md"]
        self.assertIn("(satisfied #4", verification)
        self.assertIn("(satisfied #5", verification)
        self.assertIn("(satisfied #10", verification)
        self.assertIn("Disposable-host and private-production live acceptance remain separate.", verification)
        spec = specs["setup-reconciliation.md"]
        self.assertIn("(satisfied #6", spec)
        self.assertIn("(satisfied #11", spec)
        self.assertIn("(satisfied #12", spec)
        self.assertIn("live request-to-library", spec)

    def test_wiki_records_program_fixture_acceptance_without_live_claims(self) -> None:
        index = (ROOT / ".agent" / "index.md").read_text(encoding="utf-8")
        active = (ROOT / ".agent" / "tasks" / "active.md").read_text(encoding="utf-8")
        completed = (ROOT / ".agent" / "tasks" / "completed.md").read_text(encoding="utf-8")
        log = (ROOT / ".agent" / "log.md").read_text(encoding="utf-8")
        roadmap = (ROOT / ".agent" / "project" / "roadmap.md").read_text(encoding="utf-8")
        setup = (ROOT / ".agent" / "project" / "agent-first-setup.md").read_text(encoding="utf-8")
        sentence = "Disposable-host and private-production live acceptance remain separate."
        for document in (index, completed, log):
            self.assertIn(sentence, document)
        self.assertIn("fixture-accepted", index)
        self.assertIn("#13", completed)
        self.assertNotIn("Remaining slices cover program-level fixture acceptance.", index)
        self.assertNotIn("issue **#12**", active)
        self.assertNotIn("issue **#13**", active)
        self.assertNotIn("#12/#13 in flight", active)
        self.assertNotRegex(active, r"(?i)in this worktree:\s*issue \*\*#1[23]\*\*")
        self.assertIn("program is fixture-accepted", active)
        self.assertIn("not live production verification", index)
        self.assertIn("fixture-accepted", roadmap)
        self.assertIn("#4", setup)
        self.assertIn("fixture-accepted", setup)
        setup_normalized = re.sub(r"\s+", " ", setup)
        contradiction = (
            "The encrypted-storage and VPN/acquisition slices remain "
            "approved follow-up plans, not shipped features."
        )
        self.assertNotIn(contradiction, setup_normalized)
        self.assertNotRegex(
            setup_normalized,
            r"(?i)VPN/acquisition.{0,80}(?:approved follow-up|not shipped features)",
        )
        self.assertRegex(
            setup_normalized,
            r"(?i)VPN/acquisition.{0,80}(?:fixture-accepted shipped CLI|shipped CLI)",
        )
        self.assertRegex(
            setup_normalized,
            r"(?i)[Ee]ncrypted storage remains.{0,60}(?:later|approved follow-up)",
        )
        self.assertNotIn("/home/", index + active + completed)
        self.assertNotIn("Europe/Lisbon", index + active + completed)

    def test_runtime_artifacts_remain_git_ignored(self) -> None:
        for path in (".env", ".homeflix/", "docker-compose.override.yml"):
            with self.subTest(path=path):
                result = subprocess.run(
                    ["git", "check-ignore", "-q", "--", path],
                    cwd=ROOT,
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self.assertEqual(result.returncode, 0, f"{path} must remain gitignored")

    def test_superseded_acquisition_plan_is_not_an_executable_queue(self) -> None:
        plan = (ROOT / ".agent" / "project" / "agent-first-acquisition-plan.md").read_text(encoding="utf-8")
        self.assertRegex(plan, r"(?im)^Status:\s*Superseded")
        self.assertNotRegex(plan, r"(?m)^- \[ \]")
        self.assertIn("Do not execute this page as a separate plan", plan)
        self.assertIn("fail-closed", plan)
        self.assertIn("controlling-terminal", plan)
        self.assertIn("never store public IPs", plan)
        self.assertIn("Rollback stops the selected acquisition services", plan)

    def test_docs_and_help_expose_capabilities_not_a_brittle_sequence(self) -> None:
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        guide = (ROOT / "docs" / "agent-setup.md").read_text(encoding="utf-8")
        quickstart = (ROOT / "docs" / "quickstart.md").read_text(encoding="utf-8")
        configuration = (ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
        agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
        env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
        top_help = subprocess.run(
            [str(ROOT / "scripts" / "homeflix"), "--help"],
            check=False, capture_output=True, text=True,
        )
        verify_help = subprocess.run(
            [str(ROOT / "scripts" / "homeflix"), "verify", "--help"],
            check=False, capture_output=True, text=True,
        )
        setup_help = subprocess.run(
            [str(ROOT / "scripts" / "homeflix"), "setup", "--help"],
            check=False, capture_output=True, text=True,
        )
        backup_help = subprocess.run(
            [str(ROOT / "scripts" / "homeflix"), "backup", "--help"],
            check=False, capture_output=True, text=True,
        )
        self.assertEqual(top_help.returncode, 0, top_help.stderr)
        self.assertEqual(verify_help.returncode, 0, verify_help.stderr)
        self.assertEqual(setup_help.returncode, 0, setup_help.stderr)
        self.assertEqual(backup_help.returncode, 0, backup_help.stderr)
        self.assertIn("preflight", top_help.stdout)
        self.assertIn("verify", top_help.stdout)
        self.assertIn("backup", top_help.stdout)
        self.assertIn("vpn --disrupt", top_help.stdout)
        for phase in ("core", "contract", "vpn", "acquisition"):
            self.assertIn(phase, verify_help.stdout)
        self.assertIn("--disrupt", verify_help.stdout)
        self.assertIn("--clients", setup_help.stdout)
        self.assertIn("torrent", setup_help.stdout)
        self.assertIn("{create,list,retrieve,prune,restore}", backup_help.stdout)
        for document in (guide, quickstart):
            self.assertIn("--clients", document)
            self.assertIn("verify vpn --disrupt", document)
            self.assertIn("backup create", document)
        self.assertIn("capabilities, not a universal script", guide)
        self.assertNotIn("planned follow-ups, not shipped setup phases", readme)
        self.assertIn("fixture-accepted", readme)
        self.assertIn("secrets vpn", configuration)
        self.assertNotIn("There is no VPN-secret setup command in the current core slice.", configuration)
        self.assertNotIn("scripts/preflight.sh sources this file", env_example)
        self.assertNotIn("it reads `.env`", agents)
        self.assertIn("scripts/homeflix preflight", agents)

    def test_verify_remains_cli_only_without_verify_sh(self) -> None:
        self.assertFalse((ROOT / "scripts" / "verify.sh").exists())
        self.assertTrue((ROOT / "scripts" / "homeflix").is_file())
        help_text = subprocess.run(
            [str(ROOT / "scripts" / "homeflix"), "verify", "--help"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(help_text.returncode, 0, help_text.stderr)
        self.assertIn("contract", help_text.stdout)
        self.assertIn("core", help_text.stdout)
        self.assertIn("vpn", help_text.stdout)
        self.assertIn("--disrupt", help_text.stdout)
        self.assertIn("acquisition", help_text.stdout)

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
