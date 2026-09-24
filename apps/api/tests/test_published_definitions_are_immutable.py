"""A definition version, once written, is never edited again.

This is not a style rule. A running deployment registers each version with the checksum of the
file it was published from, and refuses to start a conversation on a version whose file no longer
matches. Editing a released version does not change behaviour; it stops the product.

It is worse than a single stopped product, because the way forward reads the way back: publishing
a new version loads the previous one to classify the change, so a corrupted version cannot be
published past. A deployment that has registered one is stuck until the file is restored to the
content it was registered with. Both of this repository's products stopped at once this way, and
the API reported itself unhealthy.

The runtime already refuses. What was missing was anything that refuses *before* it ships, which
is what this does: it compares every definition against the commit that introduced it.
"""
from __future__ import annotations

from pathlib import Path
import subprocess
import unittest

REPO_ROOT = Path(__file__).resolve().parents[3]

# Versions edited before this rule was enforced, each one listed rather than hidden. A definition
# here is not exempt from the rule; it is a debt recorded with its reason, and the list only ever
# shrinks. Nothing binds to this version - the shipped product runs v7 - so restoring it would
# change no behaviour, and rewriting it now would only invent a third content for a version whose
# history already has several.
ALREADY_EDITED = {
    "products/linear_simplified/definition/v4.yaml":
        "Edited four times on 2026-09-22 while the definition engine was being brought up, "
        "before any deployment ran on it. Nothing is bound to v4 today.",
}


def _git(*arguments: str) -> tuple[int, str]:
    finished = subprocess.run(
        ("git", *arguments), cwd=REPO_ROOT, capture_output=True, text=True, check=False,
    )
    return finished.returncode, finished.stdout.strip()


class PublishedDefinitionsAreImmutableTest(unittest.TestCase):
    def definitions(self) -> list[str]:
        code, listing = _git("ls-files", "products")
        self.assertEqual(code, 0, "could not list the definitions")
        return [path for path in listing.splitlines()
                if "/definition/v" in path and path.endswith(".yaml")]

    def introduced_in(self, path: str) -> str | None:
        """The commit that added this version, which is the content it was published from."""
        code, history = _git("log", "--diff-filter=A", "--format=%H", "--all", "--", path)
        return history.splitlines()[-1] if code == 0 and history else None

    def test_no_definition_version_differs_from_the_commit_that_introduced_it(self):
        edited = []
        for path in self.definitions():
            added = self.introduced_in(path)
            if added is None:
                continue  # never committed yet; it is being written now
            code, _ = _git("diff", "--quiet", added, "--", path)
            if code != 0 and path not in ALREADY_EDITED:
                edited.append(path)
        self.assertEqual(edited, [], (
            "These definition versions were edited after they were written:\n  "
            + "\n  ".join(edited)
            + "\n\nA version is immutable once it exists. A deployment holds the checksum it was "
              "published with, refuses every conversation on a version whose file no longer "
              "matches, and cannot publish past it either. Restore these files and put the "
              "change in a new version."
        ))

    def test_the_recorded_debt_is_still_real(self):
        """A listed exception that no longer applies is removed, not left to rot."""
        for path, reason in ALREADY_EDITED.items():
            with self.subTest(path=path):
                self.assertIn(path, self.definitions(), f"{path} is listed but no longer exists")
                added = self.introduced_in(path)
                self.assertIsNotNone(added)
                code, _ = _git("diff", "--quiet", added, "--", path)
                self.assertNotEqual(code, 0, f"{path} matches its first commit; remove it from the list")
                self.assertTrue(reason.strip(), f"{path} is listed without a reason")

    def test_the_check_can_see_the_definitions_it_guards(self):
        """A check that silently guards nothing is worse than no check at all."""
        self.assertTrue(self.definitions(), "no definitions were found, so nothing was checked")

    def test_the_check_can_see_the_history_it_compares_against(self):
        """In a shallow clone every version appears to have been added in the latest commit, so
        the comparison above would pass whatever had been edited. Refuse to run blind instead."""
        code, shallow = _git("rev-parse", "--is-shallow-repository")
        self.assertEqual(code, 0, "could not read the repository's history")
        self.assertEqual(shallow, "false", (
            "This clone is shallow, so a definition's first commit cannot be found. Fetch the "
            "full history (actions/checkout needs fetch-depth: 0) before running this check."
        ))


if __name__ == "__main__":
    unittest.main()
