"""The docs only name files and commands that exist."""
import os
import re
import unittest

from tests import REPO

DOCS = ["AGENTS.md", "README.md", "CHEATSHEET.md", "training/README.md",
        "docs/architecture.md", "docs/testing.md", "docs/upgrading.md",
        ".agents/skills/add-feature/SKILL.md", ".agents/skills/debug/SKILL.md"]
TOP = ("src/", "docs/", "scripts/", "tests/", "training/", "vendor/",
       ".agents/", "future_features/", "paper/")
# generated, private or removed locations: named in the docs, not in git
NOT_IN_GIT = ("training/private", "training/samples", "data/", "output/",
              "logs/", ".venv", "vendor/dumpscribe ", "test-inbox")
BACKTICKED = re.compile(r"`([^`\s]+)`")
LINK = re.compile(r"\]\(([^)#\s]+)")
# two letters or more: "./run s" + Tab in the cheat sheet is a partial word
RUN_COMMAND = re.compile(r"\./run ([a-z][a-z-]+)")


def read(rel):
    with open(os.path.join(REPO, rel), encoding="utf-8") as f:
        return f.read()


def is_repo_path(token):
    if any(c in token for c in "<>*{}$%~|"):
        return False
    if token.startswith(NOT_IN_GIT) or (token + " ").startswith(NOT_IN_GIT):
        return False
    return token.startswith(TOP)


def dispatched_commands():
    """Command names in run's case statement (``help|-h|--help)`` too)."""
    labels = re.findall(r"^    ([a-z|-]+)\)$", read("run"), re.M)
    return {name for label in labels for name in label.split("|")}


class DocsTest(unittest.TestCase):
    def test_every_doc_listed_exists(self):
        for rel in DOCS:
            self.assertTrue(os.path.exists(os.path.join(REPO, rel)), rel)

    def test_named_repo_paths_exist(self):
        missing = []
        for rel in DOCS:
            text = read(rel)
            for token in BACKTICKED.findall(text):
                path = token.rstrip(".,:;)")
                if is_repo_path(path) and not os.path.exists(
                        os.path.join(REPO, path)):
                    missing.append(f"{rel}: {path}")
            for target in LINK.findall(text):
                if target.startswith(("http://", "https://", "mailto:")):
                    continue
                path = os.path.normpath(os.path.join(os.path.dirname(rel),
                                                     target))
                if not os.path.exists(os.path.join(REPO, path)):
                    missing.append(f"{rel}: link {target}")
        self.assertEqual(missing, [])

    def test_named_run_commands_exist(self):
        known = dispatched_commands() | {"serve"}
        unknown = sorted({f"{rel}: ./run {cmd}" for rel in DOCS
                          for cmd in RUN_COMMAND.findall(read(rel))
                          if cmd not in known})
        self.assertEqual(unknown, [])

    def test_agents_md_points_at_every_maintainer_doc(self):
        agents = read("AGENTS.md")
        for rel in DOCS:
            if rel.startswith("docs/"):
                self.assertIn(rel, agents)


if __name__ == "__main__":
    unittest.main()
