import os
import stat
import tempfile
from pathlib import Path
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from suite_cloud.mail.tests.fixtures import error_log_count
from suite_cloud.telemetry.agent import build
from suite_cloud.telemetry.agent.build import ARCHITECTURES, AgentBuild, AgentBuildError
from suite_cloud.utils import clear_config_cache

# Stands in for `go build`: records the environment and arguments in the output file, or fails.
FAKE_GO = """#!/bin/sh
if [ -n "$FAKE_GO_FAIL" ]; then echo "boom: $GOARCH" >&2; exit 1; fi
args="$*"
out=""
while [ $# -gt 0 ]; do [ "$1" = "-o" ] && out="$2"; shift; done
echo "GOOS=$GOOS GOARCH=$GOARCH CGO_ENABLED=$CGO_ENABLED GOFLAGS=$GOFLAGS $args" > "$out"
"""


class AgentBuildFixture(IntegrationTestCase):
    """A fake Go toolchain on PATH and a small source tree, so no test needs Go."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="agent-build-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        (bin_dir / "go").write_text(FAKE_GO)
        (bin_dir / "go").chmod(stat.S_IRWXU)
        self.source = self.tmp / "agent"
        (self.source / "cmd").mkdir(parents=True)
        (self.source / "VERSION").write_text("0.1.0\n")
        (self.source / "go.mod").write_text("module example\n")
        (self.source / "cmd" / "main.go").write_text("package main\n")
        self.root = self.tmp / "private"
        self.root.mkdir()
        self.env = patch.dict(os.environ, {"PATH": f"{bin_dir}:{os.environ['PATH']}"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(clear_config_cache)
        self.built_before = frappe.db.get_single_value("Suite Cloud Settings", "agent_built_version")

    def build(self, **kwargs) -> AgentBuild:
        return AgentBuild(root=self.root, source=self.source, **kwargs)


class TestAgentBuild(AgentBuildFixture):
    def test_build_writes_both_binaries_with_the_version_baked_in(self) -> None:
        build = self.build()
        version = build.run()

        self.assertRegex(version, r"^0\.1\.0\+[0-9a-f]{8}$")
        self.assertTrue(build.is_built)
        for architecture, goarch in ARCHITECTURES.items():
            content = build.binary_path(architecture).read_text()
            self.assertIn(f"GOOS=linux GOARCH={goarch} CGO_ENABLED=0", content)
            self.assertIn(f"-X main.version={version}", content)
            # A writable module cache, so the site's files can always be deleted.
            self.assertIn("GOFLAGS=-mod=readonly -modcacherw", content)
        self.assertEqual(frappe.db.get_single_value("Suite Cloud Settings", "agent_built_version"), version)
        self.assertFalse(frappe.db.get_single_value("Suite Cloud Settings", "agent_build_error"))
        self.assertEqual([p.name for p in self.root.iterdir() if not p.name.startswith(".")], [version])

    def test_a_failed_build_leaves_no_partial_directory_and_records_the_error(self) -> None:
        errors_before = error_log_count()
        with patch.dict(os.environ, {"FAKE_GO_FAIL": "1"}):
            self.assertRaisesRegex(AgentBuildError, "boom", self.build().run)

        self.assertEqual([p.name for p in self.root.iterdir()], [])
        self.assertIn("boom", frappe.db.get_single_value("Suite Cloud Settings", "agent_build_error"))
        self.assertEqual(error_log_count(), errors_before + 1)
        self.assertFalse(self.build().is_built)

    def test_the_version_follows_the_sources(self) -> None:
        first = self.build().version
        (self.source / "cmd" / "main.go").write_text("package main\n\nfunc main() {}\n")
        second = self.build().version
        self.assertNotEqual(first, second)
        self.assertTrue(first.startswith("0.1.0+") and second.startswith("0.1.0+"))

    def test_prune_keeps_the_new_and_the_previous_build(self) -> None:
        for name in ("0.0.9+aaaaaaaa", "0.1.0+bbbbbbbb", ".gocache"):
            (self.root / name).mkdir()
        frappe.db.set_single_value("Suite Cloud Settings", "agent_built_version", "0.1.0+bbbbbbbb")
        clear_config_cache()

        version = self.build().run()
        names = sorted(p.name for p in self.root.iterdir())
        self.assertEqual(names, sorted([".gocache", "0.1.0+bbbbbbbb", version]))

    def test_a_second_build_is_not_queued_while_one_runs(self) -> None:
        with patch.object(build, "is_job_enqueued", return_value=True):
            self.assertFalse(build.enqueue_build())
        with (
            patch.object(build, "is_job_enqueued", return_value=False),
            patch.object(build.frappe, "enqueue") as enqueue,
        ):
            self.assertTrue(build.enqueue_build())
        self.assertEqual(enqueue.call_args.kwargs["job_id"], build.JOB_ID)

    def test_migrate_queues_a_build_only_when_outdated_and_go_exists(self) -> None:
        frappe.flags.in_test = False
        self.addCleanup(lambda: setattr(frappe.flags, "in_test", True))
        with (
            patch.object(build, "AgentBuild", lambda: self.build()),
            patch.object(build, "enqueue_build") as enqueue,
        ):
            build.build_if_outdated()
            enqueue.assert_called_once()

            enqueue.reset_mock()
            self.build().run()
            build.build_if_outdated()
            enqueue.assert_not_called()

            with patch.object(build, "find_go", return_value=None):
                (self.source / "VERSION").write_text("0.2.0\n")
                build.build_if_outdated()
                enqueue.assert_not_called()

    def test_build_agent_needs_a_toolchain(self) -> None:
        settings = frappe.get_single("Suite Cloud Settings")
        with patch.object(build, "find_go", return_value=None):
            self.assertRaisesRegex(frappe.ValidationError, "Go toolchain", settings.build_agent)
        with (
            patch.object(build, "enqueue_build", return_value=True),
            patch.object(build, "AgentBuild", lambda: self.build()),
        ):
            self.assertEqual(settings.build_agent()["queued"], True)

    def test_has_quota_reads_the_release_and_never_raises(self) -> None:
        versions = {
            "0.1.9+abcdef12": False,
            "0.2.0+abcdef12": True,
            "0.2.0-rc1+abcdef12": False,
            "0.2.1-rc1+abcdef12": True,
            "0.10.0": True,
            "1.0.0": True,
            "dev": False,
            "0.2": False,
        }
        for version, expected in versions.items():
            with self.subTest(version=version):
                self.assertEqual(AgentBuild(version=version).has_quota, expected)
