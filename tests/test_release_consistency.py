"""One version, everywhere it is written.

``CHANGELOG.md`` -> *Como cortar uma release*, step 3: ``pyproject.toml``,
``README.md:1`` and the ``demo/demo.py`` banner carry the same version, and
"um destes divergindo é o defeito que a auditoria já apontou". Until now that
was a checklist item. A checklist item is a promise; this is the measurement.
"""

from __future__ import annotations

import json
import re
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

    def test_the_publisher_builds_both_packages(self):
        self.assertIn("python -m build packaging/mcp --outdir dist/", self._code(self.publish))
