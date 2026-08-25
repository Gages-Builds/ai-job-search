"""ADR-0004: the mechanical wall against LinkedIn sourcing.

Plain English, for a non-engineer: this fork bans pointing tooling at
LinkedIn - no scraping its job listings, no automating searches or people
lookups there, no portal integration for it, ever. Half of that ban was done
by hand: the LinkedIn portal skill was deleted, along with every instruction
that built LinkedIn URLs. But deleting code only proves the capability is gone
on the day it is deleted. Nothing stops a later edit from quietly adding the
URLs back - a written rule inside documentation cannot stop anyone, because
its only reader is another prompt (or a person in a hurry), and neither one
can enforce anything.

This file is the other half of the ban. It is a test that runs on every push
and reads the real repository, asserting that:

1. the deleted skill stays deleted,
2. no portal skill mentions LinkedIn at all,
3. no program code contains anything that could reach LinkedIn's servers,
4. every LinkedIn URL left in the repo is somebody's personal profile page -
   never a jobs page, a people-search page, or a company page,
5. no search-engine query targets the site.

If anyone reintroduces the capability, the build fails and names the exact
file and line, so the attempt happens in the open instead of by accident.
A ban nobody checks is a wish; this file is the checking.
"""
import os
import re
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# The complete licence, in one place. Every file listed here may name LinkedIn
# or carry a LinkedIn URL without failing the wall, and each entry states why.
# Anything NOT listed must pass the real checks - notably README.md, whose one
# LinkedIn link is the permitted personal-profile form and is deliberately not
# exempted from check 4.
EXEMPT_FILES = {
    # Upstream's historical record; history is never rewritten, so old entries
    # describing the once-shipped linkedin-search CLI stay as they are.
    "CHANGELOG.md",
    # This file names the forbidden patterns in order to forbid them; scanning
    # itself would always trip.
    "tests/test_adr0004_linkedin_wall.py",
    # Privacy ignore rules: they name the profile-export path in order to keep
    # that export OUT of git.
    ".gitignore",
    # Enforces the same privacy ignore rules above (fails CI if they vanish).
    "tools/security_guards.py",
}

# Check 3 scope: program code only.
CODE_SUFFIXES = {".ts", ".js", ".mjs", ".py"}
FORBIDDEN_CODE_STRINGS = ("linkedin.com", "api.linkedin", "voyager", "jobs-guest")

# Check 4: any linkedin.com/<path> occurrence must be the profile form
# linkedin.com/in/<handle> and nothing else.
LINKEDIN_URL_RE = re.compile(r"linkedin\.com/[^\s\"'<>{}()\[\]|`]+", re.IGNORECASE)
PROFILE_PREFIX = "linkedin.com/in/"

# Check 5: no search-engine query may aim at LinkedIn.
FORBIDDEN_SITE_QUERY = "site:linkedin.com"

SKIP_DIR_NAMES = {".git", "node_modules"}

MAX_EXCERPT = 160


def tracked_files():
    """Repo files per git ls-files, falling back to a tree walk without git."""
    try:
        result = subprocess.run(
            ["git", "ls-files"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return walked_files()
    return [REPO_ROOT / line for line in result.stdout.splitlines() if line.strip()]


def walked_files():
    """Fallback file discovery: walk the tree, skipping VCS/dependency dirs."""
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIR_NAMES]
        for name in filenames:
            yield Path(dirpath) / name


def text_lines(path):
    """(lineno, line) pairs for a text file; empty list for binary files."""
    try:
        raw = path.read_bytes()
    except OSError:
        return []
    if b"\0" in raw:  # null byte -> treat as binary, not text
        return []
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return []
    return list(enumerate(text.splitlines(), start=1))


def rel(path):
    return path.relative_to(REPO_ROOT).as_posix()


def excerpt(line):
    stripped = line.strip()
    return stripped[:MAX_EXCERPT] + ("…" if len(stripped) > MAX_EXCERPT else "")


def violations(predicate):
    """Collect 'file:line: excerpt' entries where predicate(lineno, line) holds."""
    found = []
    for path in tracked_files():
        if not path.is_file() or rel(path) in EXEMPT_FILES:
            continue
        for lineno, line in text_lines(path):
            if predicate(lineno, line):
                found.append(f"{rel(path)}:{lineno}: {excerpt(line)}")
    return sorted(found)


class TestSkillIsGone(unittest.TestCase):
    def test_linkedin_search_skill_directory_does_not_exist(self):
        skill_dir = REPO_ROOT / ".agents" / "skills" / "linkedin-search"
        self.assertFalse(
            skill_dir.exists(),
            "ADR-0004 violation: .agents/skills/linkedin-search/ exists again - "
            "the LinkedIn portal skill must stay deleted, not disabled.",
        )


class TestNoPortalSkillTargetsLinkedin(unittest.TestCase):
    def test_no_skill_md_under_agents_skills_mentions_linkedin(self):
        offenders = []
        for skill_md in sorted((REPO_ROOT / ".agents" / "skills").glob("*/SKILL.md")):
            offenders.extend(
                violations_of_file(skill_md, lambda line: "linkedin" in line.lower())
            )
        self.assertEqual(
            offenders,
            [],
            "ADR-0004 violation: a portal skill under .agents/skills/ references "
            "LinkedIn - no skill may target that site:\n  " + "\n  ".join(offenders),
        )


def violations_of_file(path, line_predicate):
    """'file:line: excerpt' entries in one file whose lines match a predicate."""
    if rel(path) in EXEMPT_FILES:
        return []
    return [
        f"{rel(path)}:{lineno}: {excerpt(line)}"
        for lineno, line in text_lines(path)
        if line_predicate(line)
    ]


class TestNoCodeReachesLinkedinHosts(unittest.TestCase):
    def test_no_forbidden_host_strings_in_code_files(self):
        offenders = []
        for path in tracked_files():
            if not path.is_file() or path.suffix.lower() not in CODE_SUFFIXES:
                continue
            if rel(path) in EXEMPT_FILES:
                continue
            for lineno, line in text_lines(path):
                lowered = line.lower()
                for needle in FORBIDDEN_CODE_STRINGS:
                    if needle in lowered:
                        offenders.append(
                            f"{rel(path)}:{lineno}: contains forbidden string "
                            f"'{needle}': {excerpt(line)}"
                        )
        self.assertEqual(
            offenders,
            [],
            "ADR-0004 violation: program code reaches toward LinkedIn's hosts:\n  "
            + "\n  ".join(sorted(offenders)),
        )


class TestOnlyProfileFormUrlsRemain(unittest.TestCase):
    def test_every_linkedin_url_is_a_personal_profile(self):
        offenders = []
        for path in tracked_files():
            if not path.is_file() or rel(path) in EXEMPT_FILES:
                continue
            for lineno, line in text_lines(path):
                for match in LINKEDIN_URL_RE.finditer(line):
                    url = match.group(0)
                    if not url.lower().startswith(PROFILE_PREFIX):
                        offenders.append(
                            f"{rel(path)}:{lineno}: non-profile LinkedIn URL "
                            f"'{url}' (only linkedin.com/in/<handle> is permitted)"
                        )
        self.assertEqual(
            offenders,
            [],
            "ADR-0004 violation: a LinkedIn URL other than a personal profile "
            "form slipped back in:\n  " + "\n  ".join(sorted(offenders)),
        )


class TestNoSearchQueryAimsAtLinkedin(unittest.TestCase):
    def test_site_linkedin_dot_com_appears_nowhere(self):
        offenders = violations(lambda _lineno, line: FORBIDDEN_SITE_QUERY in line.lower())
        self.assertEqual(
            offenders,
            [],
            "ADR-0004 violation: a search-engine query aims at LinkedIn:\n  "
            + "\n  ".join(offenders),
        )


if __name__ == "__main__":
    unittest.main()
