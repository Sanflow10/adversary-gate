"""One version, everywhere it is written.

``CHANGELOG.md`` -> *Como cortar uma release*, step 3: ``pyproject.toml``,
``README.md:1`` and the ``demo/demo.py`` banner carry the same version, and
"um destes divergindo é o defeito que a auditoria já apontou". Until now that
was a checklist item. A checklist item is a promise; this is the measurement.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _pyproject_version() -> str:
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', (ROOT / "pyproject.toml").read_text())
    assert match, "pyproject.toml has no version"
    return match.group(1)


class TestOneVersion(unittest.TestCase):
    def setUp(self):
        self.version = _pyproject_version()

    def test_readme_title(self):
        first = (ROOT / "README.md").read_text().splitlines()[0]
        self.assertEqual(first, f"# AdversaryGate (v{self.version})")

    def test_package_version(self):
        """``adversary-gate --version`` reads this; it must not lag pyproject (AG-027)."""
        init = (ROOT / "src" / "adversary_gate" / "__init__.py").read_text()
        match = re.search(r'(?m)^__version__\s*=\s*"([^"]+)"', init)
        self.assertTrue(match, "src/adversary_gate/__init__.py has no __version__")
        self.assertEqual(match.group(1), self.version)

    def test_one_top_level_package(self):
        """The wheel installs ``adversary_gate`` and nothing else at the top level (AG-027)."""
        top = sorted(
            p.name for p in (ROOT / "src").iterdir()
            if not p.name.startswith((".", "_")) and not p.name.endswith(".egg-info")
        )
        self.assertEqual(top, ["adversary_gate"])

    def test_demo_banner(self):
        demo = (ROOT / "demo" / "demo.py").read_text()
        banners = re.findall(r"AdversaryGate v([0-9][^ ]*) — demonstração ao vivo", demo)
        self.assertTrue(banners, "demo/demo.py has no version banner")
        self.assertEqual(set(banners), {self.version})

    def test_site_installs_the_version_being_shipped(self):
        """The product page tells people what to install; it must not lag a release."""
        site = (ROOT / "site" / "index.html").read_text()
        badge = re.findall(r"data-version>v([^<]+)<", site)
        pinned = re.findall(r"adversary-gate@v(\d+\.\d+\.\d+)", site)
        self.assertTrue(badge, "site/index.html has no version badge")
        self.assertTrue(pinned, "site/index.html pins no version in its install snippets")
        self.assertEqual(set(badge) | set(pinned), {self.version})

    def test_newest_changelog_section(self):
        """The first numbered section is the version being shipped -- not an older one."""
        changelog = (ROOT / "CHANGELOG.md").read_text()
        sections = re.findall(r"(?m)^## \[(\d+\.\d+\.\d+)\]", changelog)
        self.assertTrue(sections, "CHANGELOG.md has no numbered section")
        self.assertEqual(sections[0], self.version)


if __name__ == "__main__":
    unittest.main()


class TestMcpPackage(unittest.TestCase):
    """``adversary-gate-mcp`` ships no code; it is a pin. A pin to another version is a different server."""

    def setUp(self):
        self.version = _pyproject_version()
        # No tomllib: the suite also runs on 3.10.
        text = (ROOT / "packaging" / "mcp" / "pyproject.toml").read_text()
        self.project = {
            "name": re.search(r'(?m)^name\s*=\s*"([^"]+)"', text).group(1),
            "version": re.search(r'(?m)^version\s*=\s*"([^"]+)"', text).group(1),
            "dependencies": re.findall(r'(?m)^\s+"(adversary-gate[^"]*)",$', text),
            "scripts": dict(re.findall(r'(?m)^(adversary-gate-mcp)\s*=\s*"([^"]+)"', text)),
        }
        self.server = json.loads((ROOT / "server.json").read_text())

    def test_same_version_as_the_core(self):
        self.assertEqual(self.project["version"], self.version)

    def test_pins_the_core_at_that_version(self):
        self.assertEqual(self.project["dependencies"], [f"adversary-gate[mcp]=={self.version}"])

    def test_entry_point_is_the_core_server(self):
        self.assertEqual(
            self.project["scripts"],
            {"adversary-gate-mcp": "adversary_gate.integrations.mcp_server:main"},
        )

    def test_server_json_names_this_release(self):
        self.assertEqual(self.server["version"], self.version)
        (package,) = self.server["packages"]
        self.assertEqual(package["identifier"], self.project["name"])
        self.assertEqual(package["version"], self.version)

    def test_readme_proves_ownership_for_the_registry(self):
        """The registry reads ``mcp-name: <server name>`` from the package's PyPI description."""
        readme = (ROOT / "packaging" / "mcp" / "README.md").read_text()
        self.assertIn(f"mcp-name: {self.server['name']} ", readme)


class TestOneRouteToPyPI(unittest.TestCase):
    """Both packages, through Trusted Publishing, from a tag a person pushed.

    3.0.2 and 3.0.3 went out through ``release.yml``: it created the tag with
    the workflow token -- unsigned -- and uploaded the core alone with an API
    token. Events from that token start no other workflow, so
    ``pypi-publish.yml``, the only one that builds ``packaging/mcp``, never
    ran: ``uvx adversary-gate-mcp`` kept serving 3.0.0, pinned to the core
    3.0.0, under every fix 3.0.1 made, and the core files carried no
    provenance attestation. Read as data, like the Action's contract.
    """

    @classmethod
    def setUpClass(cls):
        workflows = ROOT / ".github" / "workflows"
        cls.release = (workflows / "release.yml").read_text()
        cls.publish = (workflows / "pypi-publish.yml").read_text()

    @staticmethod
    def _code(text: str) -> str:
        """The workflow without its comments: what runs, not what explains it."""
        return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))

    def test_the_release_workflow_never_uploads(self):
        code = self._code(self.release)
        self.assertNotIn("pypi-publish", code)
        self.assertNotIn("PYPI_API_TOKEN", code)

    def test_the_release_workflow_never_creates_the_tag(self):
        code = self._code(self.release)
        self.assertNotIn("--target", code)
        self.assertIn("no tag $TAG yet", code)

    def test_a_lightweight_tag_is_refused(self):
        self.assertIn('git cat-file -t "refs/tags/$TAG"', self._code(self.release))

    def test_no_release_page_before_both_packages_are_on_pypi(self):
        code = self._code(self.release)
        self.assertIn("for PROJECT in adversary-gate adversary-gate-mcp", code)
        self.assertLess(code.index("adversary-gate-mcp"), code.index("gh release create"))

    def test_the_publisher_reads_no_token(self):
        code = self._code(self.publish)
        self.assertNotIn("PYPI_API_TOKEN", code)
        self.assertNotIn("password:", code)
        self.assertIn("id-token: write", code)

    def test_the_publisher_verifies_the_tag_signature_before_it_builds(self):
        # Until here the publisher checked the tag's name only: a lightweight
        # or unsigned vX.Y.Z tag reached PyPI with full attestations.
        code = self._code(self.publish)
        self.assertIn("verify-tag", code)
        self.assertIn("gpg.format=ssh", code)
        self.assertIn('git cat-file -t "refs/tags/$TAG"', code)
        self.assertLess(code.index("verify-tag"), code.index("python -m build"))

    def test_the_release_workflow_verifies_the_tag_signature(self):
        # Annotated is not signed: `git tag -a` passed the lightweight check.
        code = self._code(self.release)
        self.assertIn("verify-tag", code)
        self.assertLess(code.index("verify-tag"), code.index("gh release create"))

    def test_the_release_key_comes_from_outside_the_tree(self):
        # A key read from the tagged tree would let a push bring its own key.
        for name, text in (("pypi-publish.yml", self.publish), ("release.yml", self.release)):
            code = self._code(text)
            with self.subTest(workflow=name):
                self.assertIn("${{ vars.RELEASE_ALLOWED_SIGNERS }}", code)
                self.assertIn('if [ -z "${ALLOWED_SIGNERS//[[:space:]]/}" ]; then', code)
                self.assertIn('allowedSignersFile="$RUNNER_TEMP/allowed_signers"', code)

    def test_the_publisher_builds_both_packages(self):
        self.assertIn("python -m build packaging/mcp --outdir dist/", self._code(self.publish))


def _step_script(workflow: str, step_name: str) -> str:
    """The ``run: |`` block of one named step, dedented: what the runner executes."""
    lines = workflow.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == f"- name: {step_name}")
    run = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == "run: |")
    indent = len(lines[run]) - len(lines[run].lstrip())
    body = []
    for line in lines[run + 1:]:
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        body.append(line)
    return textwrap.dedent("\n".join(body)) + "\n"


@unittest.skipUnless(shutil.which("ssh-keygen") and shutil.which("git"), "needs ssh-keygen and git")
class TestWorkflowTokens(unittest.TestCase):
    """Every workflow states what its token may do. Without a ``permissions``
    block the token gets the repository's default, which can be write access,
    and the CI job runs the patch's code (3.0.8)."""

    def test_every_workflow_declares_its_permissions(self):
        for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
            with self.subTest(workflow=path.name):
                self.assertRegex(path.read_text(), r"(?m)^\s*permissions:", path.name)

    def test_ci_reads_and_writes_nothing(self):
        text = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
        self.assertRegex(text, r"(?m)^permissions:\n  contents: read\n")


class TestTheFloatingTagFollowsSignedTags(unittest.TestCase):
    """An unsigned ``v3.9.9`` counted as the newest 3.x, and the floating ``v3``
    then never moved again (3.0.8)."""

    def test_only_signed_tags_are_candidates(self):
        text = (ROOT / ".github" / "workflows" / "release.yml").read_text()
        step = text[text.index("- name: Move the floating major tag"):]
        self.assertIn("verify-tag", step)
        self.assertIn("allowed_signers", step)


class TestTheTagSignatureStepRuns(unittest.TestCase):
    """The publisher's signature step, run as written against real tags.

    A static check says the words are there; this says a tag without the
    release key's signature stops the step, and one with it passes.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.script = _step_script(
            (ROOT / ".github" / "workflows" / "pypi-publish.yml").read_text(),
            "The tag is signed by a release key",
        )
        self.keys = {}
        for who in ("release", "stranger"):
            key = self.tmp / who
            subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", who, "-f", str(key)],
                           check=True)
            self.keys[who] = key
        origin = self.tmp / "origin.git"
        self.work = self.tmp / "work"
        self._git(self.tmp, "init", "-q", "--bare", str(origin))
        self._git(self.tmp, "init", "-q", str(self.work))
        self._git(self.work, "remote", "add", "origin", str(origin))
        (self.work / "f").write_text("x\n")
        self._git(self.work, "add", "f")
        self._git(self.work, "commit", "-q", "-m", "c")
        self.origin = origin

    def _git(self, cwd, *args):
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        return subprocess.run(["git", *args], cwd=cwd, env=env, check=True,
                              capture_output=True, text=True)

    def _tag(self, name, signer=None):
        if signer is None:
            self._git(self.work, "tag", "-a", name, "-m", name)
        else:
            self._git(self.work, "-c", "gpg.format=ssh",
                      "-c", f"user.signingkey={self.keys[signer]}.pub",
                      "tag", "-s", name, "-m", name)
        self._git(self.work, "push", "-q", "origin", f"refs/tags/{name}")

    def _run(self, tag, allowed):
        runner_temp = Path(tempfile.mkdtemp(prefix=f"rt-{tag}-", dir=self.tmp))
        env = dict(os.environ, TAG=tag, ALLOWED_SIGNERS=allowed, RUNNER_TEMP=str(runner_temp))
        return subprocess.run(["bash", "-c", self.script], cwd=self.work, env=env,
                              capture_output=True, text=True)

    def _allowed(self, who):
        """One allowed_signers line: principal, key type, key."""
        key_type, key = Path(f"{self.keys[who]}.pub").read_text().split()[:2]
        return f"{who} {key_type} {key}"

    def test_a_tag_signed_by_the_release_key_passes(self):
        self._tag("v9.9.1", signer="release")
        result = self._run("v9.9.1", self._allowed("release"))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_tag_signed_by_another_key_is_refused(self):
        self._tag("v9.9.2", signer="stranger")
        result = self._run("v9.9.2", self._allowed("release"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not signed by a key in RELEASE_ALLOWED_SIGNERS", result.stdout)

    def test_an_annotated_unsigned_tag_is_refused(self):
        self._tag("v9.9.3")
        result = self._run("v9.9.3", self._allowed("release"))
        self.assertNotEqual(result.returncode, 0)

    def test_a_lightweight_tag_is_refused(self):
        self._git(self.work, "tag", "v9.9.4")
        self._git(self.work, "push", "-q", "origin", "refs/tags/v9.9.4")
        result = self._run("v9.9.4", self._allowed("release"))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("lightweight", result.stdout)

    def test_no_variable_publishes_nothing(self):
        self._tag("v9.9.5", signer="release")
        for allowed in ("", "  \n"):
            with self.subTest(allowed=repr(allowed)):
                result = self._run("v9.9.5", allowed)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("RELEASE_ALLOWED_SIGNERS is not set", result.stdout)
