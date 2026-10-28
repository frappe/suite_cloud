"""Builds the Go agent on the bench host into the site's private files."""

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

import frappe
from frappe import _
from frappe.utils import cint, now
from frappe.utils.background_jobs import is_job_enqueued

from suite_cloud.utils import clear_config_cache, get_config, log_error, log_exception

SETTINGS = "Suite Cloud Settings"
JOB_ID = "agent-build"
BUILD_TIMEOUT = 600  # seconds, per architecture
GO_FALLBACK = "/usr/local/go/bin/go"
ARCHITECTURES = {"x86_64": "amd64", "aarch64": "arm64"}  # ansible_architecture -> GOARCH
SOURCE_GLOBS = ("VERSION", "go.mod", "go.sum", "**/*.go")
QUOTA_RELEASE = (0, 2, 0)  # the first release that answers the sending-limit hook
RELEASE = re.compile(r"(\d+)\.(\d+)\.(\d+)(-[^+]+)?")  # 0.2.0, 0.2.0-rc1, 0.2.0+547d6a38


class AgentBuildError(Exception):
    pass


def find_go() -> str | None:
    go = shutil.which("go")
    if go:
        return go
    return GO_FALLBACK if os.access(GO_FALLBACK, os.X_OK) else None


def source_dir() -> Path:
    return Path(frappe.get_app_path("suite_cloud")).parent / "agent"


class AgentBuild:
    """One version of the agent: its sources and its binaries under the site's private files.

    The version is the VERSION file plus a digest of the Go sources, so a source change rolls out
    on its own and a VERSION bump stays a human signal.
    """

    def __init__(
        self, root: Path | None = None, source: Path | None = None, version: str | None = None
    ) -> None:
        self.root = root or Path(frappe.get_site_path("private", "files", "agent")).resolve()
        self.source = source or source_dir()
        self.pinned_version = version

    @classmethod
    def last_built(cls, **kwargs) -> AgentBuild | None:
        """The build the settings record as last completed, whatever the sources say now: what
        the nodes get until the next build finishes."""

        version = get_config("agent_built_version")
        return cls(version=version, **kwargs) if version else None

    @property
    def version(self) -> str:
        if self.pinned_version:
            return self.pinned_version
        return f"{(self.source / 'VERSION').read_text().strip()}+{self.source_digest[:8]}"

    @property
    def has_quota(self) -> bool:
        """Older builds refuse a config.json with a quota section, so it waits for this one. As in
        semver, a prerelease such as 0.2.0-rc1 comes before its release. A version without a
        leading ``major.minor.patch`` counts as older: this sits on the cluster plan's path, which
        must not fail over it."""

        release = RELEASE.match(self.version)
        if not release:
            return False
        parts = tuple(int(part) for part in release.groups()[:3])
        return parts > QUOTA_RELEASE if release.group(4) else parts >= QUOTA_RELEASE

    @property
    def source_digest(self) -> str:
        digest = hashlib.sha256()
        for path in sorted(p for pattern in SOURCE_GLOBS for p in self.source.glob(pattern) if p.is_file()):
            digest.update(str(path.relative_to(self.source)).encode())
            digest.update(path.read_bytes())
        return digest.hexdigest()

    @property
    def directory(self) -> Path:
        return self.root / self.version

    def binary_path(self, architecture: str) -> Path:
        return self.directory / f"suite-cloud-agent-linux-{ARCHITECTURES[architecture]}"

    @property
    def is_built(self) -> bool:
        return all(self.binary_path(arch).is_file() for arch in ARCHITECTURES)

    def run(self) -> str:
        """Builds every architecture into a temporary directory, then moves it into place whole."""

        go = find_go()
        if not go:
            raise AgentBuildError("No Go toolchain found on this bench host (PATH or /usr/local/go/bin/go).")
        version = self.version
        staging = self.root / f".{version}.tmp"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        try:
            for architecture, goarch in ARCHITECTURES.items():
                self.compile(go, goarch, staging / self.binary_path(architecture).name)
        except Exception as e:
            shutil.rmtree(staging, ignore_errors=True)
            self.record(version, error=str(e))
            raise

        shutil.rmtree(self.directory, ignore_errors=True)
        os.replace(staging, self.directory)
        previous = get_config("agent_built_version")
        self.record(version, error=None)
        self.prune(keep={version, previous})
        return version

    def compile(self, go: str, goarch: str, output: Path) -> None:
        env = {
            **os.environ,
            "CGO_ENABLED": "0",
            "GOOS": "linux",
            "GOARCH": goarch,
            "GOCACHE": str(self.root / ".gocache"),
            # Modules are downloaded once and checked against go.sum; the sources are never edited.
            "GOMODCACHE": str(self.root / ".gomodcache"),
            "GOFLAGS": "-mod=readonly",
            "GOTOOLCHAIN": "local",
        }
        command = [
            go,
            "build",
            "-trimpath",
            "-ldflags",
            f"-s -w -X main.version={self.version}",
            "-o",
            str(output),
            "./cmd/suite-cloud-agent",
        ]
        try:
            result = subprocess.run(
                command,
                cwd=self.source,
                env=env,
                capture_output=True,
                text=True,
                timeout=BUILD_TIMEOUT,
                check=False,
            )
        except subprocess.TimeoutExpired as e:
            raise AgentBuildError(f"go build {goarch} did not finish in {BUILD_TIMEOUT} seconds") from e
        if result.returncode != 0:
            raise AgentBuildError(f"go build {goarch} failed:\n{result.stderr[-4000:]}")

    def record(self, version: str, error: str | None) -> None:
        values = {"agent_build_error": (error or "")[:1000]}
        if error is None:
            values.update({"agent_built_version": version, "agent_built_at": now()})
        else:
            log_error("Agent build failed", error)
        for field, value in values.items():
            frappe.db.set_single_value(SETTINGS, field, value)
        clear_config_cache()
        frappe.publish_realtime(
            "suite_cloud_agent_build", {"version": version, "error": error}, after_commit=False
        )

    def prune(self, keep: set[str]) -> None:
        """Drops older builds; the previous one stays so a Server Job mid-copy can finish."""

        for entry in self.root.iterdir():
            if entry.is_dir() and not entry.name.startswith(".") and entry.name not in keep:
                shutil.rmtree(entry, ignore_errors=True)


def enqueue_build() -> bool:
    """Queues one build; a build already queued or running is left alone."""

    if is_job_enqueued(JOB_ID):
        return False
    frappe.enqueue(
        build_agent,
        queue="long",
        timeout=len(ARCHITECTURES) * BUILD_TIMEOUT + 60,
        job_id=JOB_ID,
        deduplicate=True,
        enqueue_after_commit=True,
    )
    return True


def build_agent() -> str:
    """The queued job. A failed build is recorded on the settings, so the job itself returns."""

    try:
        return AgentBuild().run()
    except AgentBuildError as e:
        return str(e)


def is_telemetry_enabled() -> bool:
    return bool(cint(get_config("enable_telemetry")))


def is_quota_enabled() -> bool:
    """The hook and the Agents' quota section wait for telemetry to be on and a build with quota."""

    build = AgentBuild.last_built()
    return is_telemetry_enabled() and bool(build) and build.has_quota


def build_if_outdated() -> None:
    """After a migrate: queue a build when the sources changed. Never fails the migrate."""

    if frappe.flags.in_test:
        return
    try:
        build = AgentBuild()
        if build.version == get_config("agent_built_version") and build.is_built:
            return
        if not find_go():
            if is_telemetry_enabled():
                log_error("Agent not built", _("Telemetry is on but this bench host has no Go toolchain."))
            return
        enqueue_build()
    except Exception:
        log_exception("Agent build not queued")
