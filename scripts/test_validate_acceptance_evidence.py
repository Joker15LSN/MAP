#!/usr/bin/env python3
"""Self-test for scripts/validate_acceptance_evidence.py (S2-01 / S4-03).

Pure-stdlib unittest, no third-party dependencies, runnable anywhere the
gate runs (system python3 + git). Each test builds a throwaway git
repository in a temp dir and drives the validator with --root/--profile,
covering:

- the S2-01 failure matrix: blocked evidence, stale sha, wrong directory
  layout, wrong task/ac ids, extra (schema-rejected) fields, expired waivers,
  artifact hash tampering, dependency cycles, freeze sha ancestry and the
  evidence-only-tail rule;
- the S4-03 trusted-source matrix: unsigned pass/waiver evidence, forged
  producer, forged command + exit_code=0, copied/out-of-dir artifacts,
  modified commit/command/artifact, wrong issuer, wrong workflow, unknown key
  id, and the reject-any-../ artifact path rule.

Run:  python3 scripts/test_validate_acceptance_evidence.py
Exit: 0 = all tests pass; 1 = at least one failure.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "validate_acceptance_evidence.py"
REPO_SCHEMA = Path(__file__).resolve().parents[1] / "TODO" / "evidence-manifest.schema.json"

sys.path.insert(0, str(Path(__file__).resolve().parent))

import evidence_signing  # noqa: E402

FAKE_SHA_1 = "a" * 40
FAKE_SHA_2 = "b" * 40

# S5-02: the CI identity bound into every attestation by the self-test.
TEST_REPOSITORY = "test-org/test-repo"
TEST_GIT_REF = "refs/heads/main"
TEST_RUN_ID = "test-run-1"
TEST_RUN_ATTEMPT = "1"


def _future_iso(offset_hours: float = 1.0) -> str:
    from datetime import datetime, timedelta, timezone

    return (
        datetime.now(timezone.utc) + timedelta(hours=offset_hours)
    ).isoformat().replace("+00:00", "Z")


def make_manifest(
    *,
    task: str,
    ac: str,
    status: str,
    implementation_sha: str,
    artifact_path: str,
    artifact_sha: str,
    extra_fields: dict | None = None,
    waiver_expires_at: str | None = None,
    started_at: str | None = None,
) -> dict:
    # S5-02: started_at defaults to the FUTURE relative to the fixture
    # commits, so the "attestation after implementation" check passes;
    # counter-tests pass an explicit past timestamp.
    manifest: dict = {
        "schema_version": "1.1.0",
        "task_id": task,
        "ac_id": ac,
        "status": status,
        "baseline_sha": FAKE_SHA_1,
        "implementation_sha": implementation_sha,
        "environment_digest": "e" * 64,
        "started_at": started_at or _future_iso(),
        "finished_at": started_at or _future_iso(),
        "command": "pytest tests/ -q",
        "exit_code": 0 if status == "pass" else 1,
        "artifacts": [
            {
                "path": artifact_path,
                "sha256": artifact_sha,
                "media_type": "application/json",
            }
        ],
        "assertions": [
            {"name": "behavior", "expected": True, "actual": True, "result": "pass"}
        ],
        "finding_ids": [],
        "waiver_id": None,
        "waiver_owner": None,
        "waiver_expires_at": None,
        "not_applicable_reason": None,
        "blocked_reason": "pending upstream capability" if status == "blocked" else None,
        "blocker_owner": "platform-security" if status == "blocked" else None,
        "superseded_by": None,
        "superseded_reason": None,
        "attestation": None,
        "producer": {"agent": "validator-self-test", "version": "1.0.0"},
    }
    if status == "not-applicable-approved":
        manifest["waiver_id"] = "W-1"
        manifest["waiver_owner"] = "platform-security"
        manifest["waiver_expires_at"] = waiver_expires_at or "2099-01-01T00:00:00Z"
        manifest["not_applicable_reason"] = "out of scope by design"
    if status == "superseded":
        manifest["superseded_by"] = "tmp/acceptance/other"
        manifest["superseded_reason"] = "replaced by newer evidence"
    if extra_fields:
        manifest.update(extra_fields)
    return manifest


class BaseValidatorTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self._git("init", "-q")
        (self.root / "TODO").mkdir(parents=True)
        (self.root / "TODO" / "evidence-manifest.schema.json").write_text(
            REPO_SCHEMA.read_text(encoding="utf-8"), encoding="utf-8"
        )
        # S4-03: a test signing key + pinned trust config, so the validator can
        # verify attestations offline. The private key exists only in memory.
        self.secret_hex, self.public_hex = evidence_signing.generate_keypair()
        self.issuer = "test-ci"
        self.workflow = "test-workflow/gate-final"
        self.key_id = "test-key"
        trust_dir = self.root / "TODO" / "evidence-trust"
        trust_dir.mkdir(parents=True, exist_ok=True)
        self.trust_config = {
            "schema_version": "2.0.0",
            "expected_issuer": self.issuer,
            "expected_workflow": self.workflow,
            "expected_repository": TEST_REPOSITORY,
            "allowed_refs": ["refs/heads/main", "refs/heads/release*"],
            "keys": {
                self.key_id: {
                    "algorithm": "ed25519",
                    "public_key": self.public_hex,
                }
            },
        }
        (trust_dir / "trusted_keys.json").write_text(
            json.dumps(self.trust_config), encoding="utf-8"
        )
        self.profile = self.root / "TODO" / "acceptance-profile.yaml"
        # S5-02: the release validator demands an EXTERNALLY injected trust
        # anchor; the self-test plays the protected-CI role and injects the
        # digest of the mirror it just pinned.
        self._prev_anchor = {
            name: os.environ.get(name)
            for name in (
                "MAP_EVIDENCE_TRUST_DIGEST",
                "MAP_EVIDENCE_TRUST_PUBLIC_KEY",
                # S6-04: the expected protected-CI run identity the
                # validator must compare attestations against.
                "MAP_EVIDENCE_EXPECTED_RUN_ID",
                "MAP_EVIDENCE_EXPECTED_RUN_ATTEMPT",
            )
        }
        os.environ["MAP_EVIDENCE_TRUST_DIGEST"] = evidence_signing.trust_config_digest(
            self.trust_config
        )
        os.environ["MAP_EVIDENCE_EXPECTED_RUN_ID"] = TEST_RUN_ID
        os.environ["MAP_EVIDENCE_EXPECTED_RUN_ATTEMPT"] = TEST_RUN_ATTEMPT

    def tearDown(self) -> None:
        for name, value in self._prev_anchor.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        self._tmp.cleanup()

    def _git(self, *args: str) -> str:
        proc = subprocess.run(
            ["git", "-C", str(self.root), "-c", "user.email=t@t", "-c", "user.name=t", *args],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"git {args} failed: {proc.stderr}")
        return proc.stdout.strip()

    @staticmethod
    def _sha256(path: Path) -> str:
        import hashlib

        return hashlib.sha256(path.read_bytes()).hexdigest()

    def write_profile(self, registry: dict) -> None:
        self.profile.write_text(
            json.dumps(
                {
                    "schema_version": "1.1.0",
                    "profile_id": "self-test",
                    "task_registry": registry,
                }
            ),
            encoding="utf-8",
        )

    def commit_all(self, message: str = "fixture") -> str:
        self._git("add", "-A")
        self._git("commit", "-q", "-m", message)
        return self._git("rev-parse", "HEAD")

    def write_evidence(self, task: str, sha: str, ac: str, manifest: dict) -> None:
        target = self.root / "tmp" / "acceptance" / task / sha / ac
        target.mkdir(parents=True, exist_ok=True)
        (target / "evidence-manifest.json").write_text(
            json.dumps(manifest), encoding="utf-8"
        )

    def run_validator(self, *extra: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--root",
                str(self.root),
                "--profile",
                str(self.profile),
                *extra,
            ],
            capture_output=True,
            text=True,
        )

    def _artifact(self, task: str, sha: str, ac: str, name: str = "artifact.txt") -> tuple[str, str]:
        """Write a fixture artifact INSIDE the AC's own dir and return (path, sha)."""
        d = self.root / "tmp" / "acceptance" / task / sha / ac / "logs"
        d.mkdir(parents=True, exist_ok=True)
        f = d / name
        f.write_text("fixture artifact", encoding="utf-8")
        return str(f.relative_to(self.root)), self._sha256(f)

    def _sign(self, manifest: dict, **overrides) -> dict:
        kwargs = dict(
            issuer=self.issuer,
            workflow=self.workflow,
            key_id=self.key_id,
            repository=TEST_REPOSITORY,
            git_ref=TEST_GIT_REF,
            run_id=TEST_RUN_ID,
            run_attempt=TEST_RUN_ATTEMPT,
        )
        kwargs.update(overrides)
        return evidence_signing.sign_manifest(
            manifest,
            self.secret_hex,
            **kwargs,
        )

    def _manifest(
        self,
        *,
        task: str,
        ac: str,
        status: str,
        implementation_sha: str,
        extra_fields: dict | None = None,
        waiver_expires_at: str | None = None,
        sign: bool | None = None,
        artifact_path: str | None = None,
        artifact_sha: str | None = None,
    ) -> dict:
        if artifact_path is None:
            artifact_path, artifact_sha = self._artifact(task, implementation_sha, ac)
        manifest = make_manifest(
            task=task,
            ac=ac,
            status=status,
            implementation_sha=implementation_sha,
            artifact_path=artifact_path,
            artifact_sha=artifact_sha,
            extra_fields=extra_fields,
            waiver_expires_at=waiver_expires_at,
        )
        if sign is None:
            sign = status in ("pass", "not-applicable-approved")
        if sign:
            manifest = self._sign(manifest)
        return manifest

    def freeze_repo(self, registry: dict) -> tuple[str, str]:
        """Commit profile + trust config, then signed pass evidence for each AC.

        Returns (freeze_sha, head_after_evidence). The evidence commit touches
        only tmp/acceptance/** so --require-final passes the evidence-only-tail
        rule.
        """
        self.write_profile(registry)
        freeze = self.commit_all("freeze")
        for task, spec in registry.items():
            for ac in spec.get("acceptance_ids", []):
                manifest = self._manifest(
                    task=task, ac=ac, status="pass", implementation_sha=freeze
                )
                self.write_evidence(task, freeze, ac, manifest)
        head = self.commit_all("evidence at freeze")
        return freeze, head


class StructureAndEligibilityTests(BaseValidatorTest):
    def test_all_pass_is_releasable_in_final_mode(self) -> None:
        freeze, _head = self.freeze_repo(
            {"P1-TEST-A": {"depends_on": [], "acceptance_ids": ["AC-A-01"]}}
        )
        proc = self.run_validator("--require-final", "--report-json",
                                  str(self.root / "report.json"))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        report = json.loads((self.root / "report.json").read_text())
        self.assertTrue(report["releasable"])
        self.assertEqual(report["status_counts"]["pass"], 1)
        self.assertEqual(report["freeze_sha"], freeze)

    def test_blocked_evidence_fails_final_but_passes_structure(self) -> None:
        self.write_profile({"P1-TEST-A": {"depends_on": [], "acceptance_ids": ["AC-A-01"]}})
        freeze = self.commit_all("freeze")
        self.write_evidence(
            "P1-TEST-A",
            freeze,
            "AC-A-01",
            self._manifest(
                task="P1-TEST-A",
                ac="AC-A-01",
                status="blocked",
                implementation_sha=freeze,
            ),
        )
        self.commit_all("evidence")

        structure = self.run_validator()
        self.assertEqual(structure.returncode, 0, structure.stderr)
        self.assertIn("structure only", structure.stdout)

        final = self.run_validator(
            "--require-final", "--report-json", str(self.root / "report.json")
        )
        self.assertEqual(final.returncode, 1)
        self.assertIn("NOT RELEASABLE", final.stderr)
        self.assertIn("AC-A-01 (task P1-TEST-A): blocked", final.stderr)
        report = json.loads((self.root / "report.json").read_text())
        self.assertFalse(report["releasable"])
        self.assertEqual(
            report["not_releasable"], [{"ac_id": "AC-A-01", "task_id": "P1-TEST-A", "status": "blocked"}]
        )

    def _assert_status_not_releasable(self, status: str) -> None:
        self.write_profile({"P1-TEST-A": {"depends_on": [], "acceptance_ids": ["AC-A-01"]}})
        freeze = self.commit_all("freeze")
        self.write_evidence(
            "P1-TEST-A",
            freeze,
            "AC-A-01",
            self._manifest(
                task="P1-TEST-A",
                ac="AC-A-01",
                status=status,
                implementation_sha=freeze,
            ),
        )
        self.commit_all("evidence")
        proc = self.run_validator("--require-final")
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertIn("NOT RELEASABLE", proc.stderr)

    def test_fail_evidence_fails_final(self) -> None:
        self._assert_status_not_releasable("fail")

    def test_running_evidence_fails_final(self) -> None:
        self._assert_status_not_releasable("running")

    def test_not_run_evidence_fails_final(self) -> None:
        self._assert_status_not_releasable("not-run")

    def test_superseded_evidence_fails_final(self) -> None:
        self.write_profile({"P1-TEST-A": {"depends_on": [], "acceptance_ids": ["AC-A-01"]}})
        freeze = self.commit_all("freeze")
        self.write_evidence(
            "P1-TEST-A",
            freeze,
            "AC-A-01",
            self._manifest(
                task="P1-TEST-A",
                ac="AC-A-01",
                status="superseded",
                implementation_sha=freeze,
            ),
        )
        self.commit_all("evidence")
        proc = self.run_validator("--require-final")
        # a fully superseded set has no current evidence: final mode fails
        self.assertEqual(proc.returncode, 1)
        self.assertIn("no current (non-superseded) evidence exists", proc.stderr)

    def test_unexpired_waiver_is_releasable(self) -> None:
        self.write_profile({"P1-TEST-A": {"depends_on": [], "acceptance_ids": ["AC-A-01"]}})
        freeze = self.commit_all("freeze")
        self.write_evidence(
            "P1-TEST-A",
            freeze,
            "AC-A-01",
            self._manifest(
                task="P1-TEST-A",
                ac="AC-A-01",
                status="not-applicable-approved",
                implementation_sha=freeze,
                waiver_expires_at="2099-01-01T00:00:00Z",
            ),
        )
        self.commit_all("evidence")
        proc = self.run_validator("--require-final")
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_expired_waiver_fails(self) -> None:
        self.write_profile({"P1-TEST-A": {"depends_on": [], "acceptance_ids": ["AC-A-01"]}})
        freeze = self.commit_all("freeze")
        self.write_evidence(
            "P1-TEST-A",
            freeze,
            "AC-A-01",
            self._manifest(
                task="P1-TEST-A",
                ac="AC-A-01",
                status="not-applicable-approved",
                implementation_sha=freeze,
                waiver_expires_at="2020-01-01T00:00:00Z",
            ),
        )
        self.commit_all("evidence")
        proc = self.run_validator("--require-final")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("waiver expired", proc.stderr)


class IntegrityTests(BaseValidatorTest):
    def test_current_evidence_in_wrong_task_directory_fails(self) -> None:
        """S3-05: evidence for AC-A-01 (task P1-TEST-A) placed under the
        VALID task directory P1-TEST-B with a self-consistent manifest must
        fail final validation (registry task must match the directory)."""
        self.write_profile(
            {
                "P1-TEST-A": {"depends_on": [], "acceptance_ids": ["AC-A-01"]},
                "P1-TEST-B": {"depends_on": [], "acceptance_ids": ["AC-B-01"]},
            }
        )
        freeze = self.commit_all("freeze")
        self.write_evidence(
            "P1-TEST-B",
            freeze,
            "AC-A-01",
            self._manifest(
                task="P1-TEST-B",
                ac="AC-A-01",
                status="pass",
                implementation_sha=freeze,
            ),
        )
        self.commit_all("evidence")
        proc = self.run_validator("--require-final")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("misplaced", proc.stderr)

    def test_artifact_path_escape_is_rejected(self) -> None:
        """S4-03: an artifact path with a '..' component (../) fails."""
        self.write_profile(
            {"P1-TEST-A": {"depends_on": [], "acceptance_ids": ["AC-A-01"]}}
        )
        freeze = self.commit_all("freeze")
        manifest = self._manifest(
            task="P1-TEST-A", ac="AC-A-01", status="blocked", implementation_sha=freeze
        )
        manifest["artifacts"] = [
            {
                "path": "../outside.txt",
                "sha256": "f" * 64,
                "media_type": "text/plain",
            }
        ]
        self.write_evidence("P1-TEST-A", freeze, "AC-A-01", manifest)
        self.commit_all("evidence")
        proc = self.run_validator()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("artifact", proc.stderr)

    def test_absolute_artifact_path_is_rejected(self) -> None:
        self.write_profile(
            {"P1-TEST-A": {"depends_on": [], "acceptance_ids": ["AC-A-01"]}}
        )
        freeze = self.commit_all("freeze")
        manifest = self._manifest(
            task="P1-TEST-A", ac="AC-A-01", status="blocked", implementation_sha=freeze
        )
        manifest["artifacts"] = [
            {
                "path": "/etc/passwd",
                "sha256": "f" * 64,
                "media_type": "text/plain",
            }
        ]
        self.write_evidence("P1-TEST-A", freeze, "AC-A-01", manifest)
        self.commit_all("evidence")
        proc = self.run_validator()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("relative path", proc.stderr)

    def test_artifact_path_with_dotdot_component_fails(self) -> None:
        """S4-03: a '..' component INSIDE the evidence tree is rejected even
        when resolve() would collapse it back inside the tree."""
        self.write_profile(
            {"P1-TEST-A": {"depends_on": [], "acceptance_ids": ["AC-A-01"]}}
        )
        freeze = self.commit_all("freeze")
        art_path, art_sha = self._artifact("P1-TEST-A", freeze, "AC-A-01")
        manifest = self._manifest(
            task="P1-TEST-A", ac="AC-A-01", status="blocked", implementation_sha=freeze
        )
        manifest["artifacts"] = [
            {
                "path": "tmp/acceptance/P1-TEST-A/" + freeze + "/AC-A-01/logs/../artifact.txt",
                "sha256": art_sha,
                "media_type": "text/plain",
            }
        ]
        self.write_evidence("P1-TEST-A", freeze, "AC-A-01", manifest)
        self.commit_all("evidence")
        proc = self.run_validator()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("no '..' component", proc.stderr)

    def test_artifact_outside_own_dir_fails(self) -> None:
        """S4-03: an artifact in a sibling AC directory (not this manifest's
        own task/sha/ac dir) is rejected."""
        self.write_profile(
            {
                "P1-TEST-A": {"depends_on": [], "acceptance_ids": ["AC-A-01", "AC-A-99"]},
            }
        )
        freeze = self.commit_all("freeze")
        # a real file under a DIFFERENT AC's dir (the "copied old log" shape)
        copied_rel, copied_sha = self._artifact("P1-TEST-A", freeze, "AC-A-99")
        manifest = self._manifest(
            task="P1-TEST-A", ac="AC-A-01", status="blocked", implementation_sha=freeze
        )
        manifest["artifacts"] = [
            {"path": copied_rel, "sha256": copied_sha, "media_type": "text/plain"}
        ]
        self.write_evidence("P1-TEST-A", freeze, "AC-A-01", manifest)
        self.commit_all("evidence")
        proc = self.run_validator()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("task/sha/ac directory", proc.stderr)

    def test_stale_sha_not_marked_superseded_fails(self) -> None:
        self.write_profile({"P1-TEST-A": {"depends_on": [], "acceptance_ids": ["AC-A-01"]}})
        freeze = self.commit_all("freeze")
        self.write_evidence(
            "P1-TEST-A",
            freeze,
            "AC-A-01",
            self._manifest(
                task="P1-TEST-A",
                ac="AC-A-01",
                status="pass",
                implementation_sha=freeze,
            ),
        )
        self.write_evidence(
            "P1-TEST-A",
            FAKE_SHA_2,
            "AC-A-01",
            self._manifest(
                task="P1-TEST-A",
                ac="AC-A-01",
                status="pass",
                implementation_sha=freeze,
            ),
        )
        self.commit_all("evidence")
        proc = self.run_validator()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("stale manifest", proc.stderr)

    def test_stale_sha_marked_superseded_is_ok(self) -> None:
        self.write_profile({"P1-TEST-A": {"depends_on": [], "acceptance_ids": ["AC-A-01"]}})
        freeze = self.commit_all("freeze")
        self.write_evidence(
            "P1-TEST-A",
            freeze,
            "AC-A-01",
            self._manifest(
                task="P1-TEST-A",
                ac="AC-A-01",
                status="pass",
                implementation_sha=freeze,
            ),
        )
        self.write_evidence(
            "P1-TEST-A",
            FAKE_SHA_2,
            "AC-A-01",
            self._manifest(
                task="P1-TEST-A",
                ac="AC-A-01",
                status="superseded",
                implementation_sha=FAKE_SHA_2,
            ),
        )
        self.commit_all("evidence")
        proc = self.run_validator()
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_ac_id_directory_mismatch_fails(self) -> None:
        self.write_profile({"P1-TEST-A": {"depends_on": [], "acceptance_ids": ["AC-A-01"]}})
        freeze = self.commit_all("freeze")
        self.write_evidence(
            "P1-TEST-A",
            freeze,
            "AC-A-99",
            self._manifest(
                task="P1-TEST-A",
                ac="AC-A-01",
                status="pass",
                implementation_sha=freeze,
            ),
        )
        self.commit_all("evidence")
        proc = self.run_validator()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("ac_id", proc.stderr)

    def test_task_id_directory_mismatch_fails(self) -> None:
        self.write_profile(
            {
                "P1-TEST-A": {"depends_on": [], "acceptance_ids": ["AC-A-01"]},
                "P1-TEST-B": {"depends_on": [], "acceptance_ids": ["AC-B-01"]},
            }
        )
        freeze = self.commit_all("freeze")
        self.write_evidence(
            "P1-TEST-B",
            freeze,
            "AC-A-01",
            self._manifest(
                task="P1-TEST-A",
                ac="AC-A-01",
                status="pass",
                implementation_sha=freeze,
            ),
        )
        self.commit_all("evidence")
        proc = self.run_validator()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("does not match directory", proc.stderr)
        self.assertIn("task_id", proc.stderr)

    def test_implementation_sha_directory_mismatch_fails(self) -> None:
        self.write_profile({"P1-TEST-A": {"depends_on": [], "acceptance_ids": ["AC-A-01"]}})
        freeze = self.commit_all("freeze")
        self.write_evidence(
            "P1-TEST-A",
            freeze,
            "AC-A-01",
            self._manifest(
                task="P1-TEST-A",
                ac="AC-A-01",
                status="pass",
                implementation_sha=FAKE_SHA_2,
            ),
        )
        self.commit_all("evidence")
        proc = self.run_validator()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("does not match directory", proc.stderr)

    def test_extra_manifest_field_fails_schema(self) -> None:
        self.write_profile({"P1-TEST-A": {"depends_on": [], "acceptance_ids": ["AC-A-01"]}})
        freeze = self.commit_all("freeze")
        self.write_evidence(
            "P1-TEST-A",
            freeze,
            "AC-A-01",
            self._manifest(
                task="P1-TEST-A",
                ac="AC-A-01",
                status="pass",
                implementation_sha=freeze,
                extra_fields={"surprise_field": "x"},
            ),
        )
        self.commit_all("evidence")
        proc = self.run_validator()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("unknown field", proc.stderr)

    def test_artifact_hash_tampering_fails(self) -> None:
        self.write_profile({"P1-TEST-A": {"depends_on": [], "acceptance_ids": ["AC-A-01"]}})
        freeze = self.commit_all("freeze")
        manifest = self._manifest(
            task="P1-TEST-A", ac="AC-A-01", status="blocked", implementation_sha=freeze
        )
        manifest["artifacts"][0]["sha256"] = "f" * 64
        self.write_evidence("P1-TEST-A", freeze, "AC-A-01", manifest)
        self.commit_all("evidence")
        proc = self.run_validator()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("hash mismatch", proc.stderr)

    def test_dependency_cycle_fails(self) -> None:
        self.write_profile(
            {
                "P1-TEST-A": {"depends_on": ["P1-TEST-B"], "acceptance_ids": ["AC-A-01"]},
                "P1-TEST-B": {"depends_on": ["P1-TEST-A"], "acceptance_ids": ["AC-B-01"]},
            }
        )
        self.commit_all("freeze")
        proc = self.run_validator()
        self.assertEqual(proc.returncode, 1)
        self.assertIn("dependency cycle", proc.stderr)

    def test_freeze_sha_not_ancestor_fails_final(self) -> None:
        self.write_profile({"P1-TEST-A": {"depends_on": [], "acceptance_ids": ["AC-A-01"]}})
        orphan = self.commit_all("freeze")
        self._git("checkout", "-q", "--orphan", "detached")
        self._git("rm", "-q", "-rf", ".")
        (self.root / "other.txt").write_text("unrelated" + chr(10), encoding="utf-8")
        (self.root / "TODO").mkdir(parents=True, exist_ok=True)
        (self.root / "TODO" / "evidence-manifest.schema.json").write_text(
            REPO_SCHEMA.read_text(encoding="utf-8"), encoding="utf-8"
        )
        trust_dir = self.root / "TODO" / "evidence-trust"
        trust_dir.mkdir(parents=True, exist_ok=True)
        (trust_dir / "trusted_keys.json").write_text(
            json.dumps(
                {
                    "schema_version": "2.0.0",
                    "expected_issuer": self.issuer,
                    "expected_workflow": self.workflow,
                    "expected_repository": TEST_REPOSITORY,
                    "allowed_refs": ["refs/heads/main"],
                    "keys": {
                        self.key_id: {
                            "algorithm": "ed25519",
                            "public_key": self.public_hex,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        self.write_profile(
            {"P1-TEST-A": {"depends_on": [], "acceptance_ids": ["AC-A-01"]}}
        )
        self.commit_all("new root")
        self.write_evidence(
            "P1-TEST-A",
            orphan,
            "AC-A-01",
            self._manifest(
                task="P1-TEST-A",
                ac="AC-A-01",
                status="pass",
                implementation_sha=orphan,
            ),
        )
        self.commit_all("evidence")
        proc = self.run_validator("--require-final")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("not HEAD or an ancestor", proc.stderr)

    def test_non_evidence_tail_fails_final(self) -> None:
        self.write_profile({"P1-TEST-A": {"depends_on": [], "acceptance_ids": ["AC-A-01"]}})
        freeze = self.commit_all("freeze")
        self.write_evidence(
            "P1-TEST-A",
            freeze,
            "AC-A-01",
            self._manifest(
                task="P1-TEST-A",
                ac="AC-A-01",
                status="pass",
                implementation_sha=freeze,
            ),
        )
        self.commit_all("evidence")
        (self.root / "src").mkdir(parents=True)
        (self.root / "src" / "app.py").write_text("print('x')" + chr(10), encoding="utf-8")
        self.commit_all("product change after freeze")
        proc = self.run_validator("--require-final")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("non-evidence paths", proc.stderr)

    def test_evidence_only_tail_passes_final(self) -> None:
        self.write_profile(
            {"P1-TEST-A": {"depends_on": [], "acceptance_ids": ["AC-A-01", "AC-A-02"]}}
        )
        freeze = self.commit_all("freeze")
        for ac in ("AC-A-01", "AC-A-02"):
            self.write_evidence(
                "P1-TEST-A",
                freeze,
                ac,
                self._manifest(
                    task="P1-TEST-A",
                    ac=ac,
                    status="pass",
                    implementation_sha=freeze,
                ),
            )
        self.commit_all("evidence at freeze")
        proc = self.run_validator("--require-final")
        self.assertEqual(proc.returncode, 0, proc.stderr)


class AttestationTests(BaseValidatorTest):
    """S4-03: pass evidence must have a trusted, signature-verifiable source."""

    def _signed_pass(self, task: str, ac: str, sha: str) -> dict:
        return self._manifest(task=task, ac=ac, status="pass", implementation_sha=sha)

    def test_unsigned_pass_manifest_fails(self) -> None:
        self.write_profile({"P1-TEST-A": {"depends_on": [], "acceptance_ids": ["AC-A-01"]}})
        freeze = self.commit_all("freeze")
        self.write_evidence(
            "P1-TEST-A",
            freeze,
            "AC-A-01",
            self._manifest(
                task="P1-TEST-A",
                ac="AC-A-01",
                status="pass",
                implementation_sha=freeze,
                sign=False,
            ),
        )
        self.commit_all("evidence")
        proc = self.run_validator("--require-final")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("attestation", proc.stderr)

    def test_forged_producer_fails(self) -> None:
        self.write_profile({"P1-TEST-A": {"depends_on": [], "acceptance_ids": ["AC-A-01"]}})
        freeze = self.commit_all("freeze")
        manifest = self._signed_pass("P1-TEST-A", "AC-A-01", freeze)
        manifest["producer"] = {"agent": "attacker", "version": "9.9.9"}
        self.write_evidence("P1-TEST-A", freeze, "AC-A-01", manifest)
        self.commit_all("evidence")
        proc = self.run_validator("--require-final")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("signature", proc.stderr)

    def test_command_false_exit_zero_unsigned_fails(self) -> None:
        """A forged pass manifest (command=false, exit_code=0, arbitrary
        producer) is unsigned and must never reach a releasable state."""
        self.write_profile({"P1-TEST-A": {"depends_on": [], "acceptance_ids": ["AC-A-01"]}})
        freeze = self.commit_all("freeze")
        manifest = self._manifest(
            task="P1-TEST-A",
            ac="AC-A-01",
            status="pass",
            implementation_sha=freeze,
            sign=False,
        )
        manifest["command"] = "false"
        manifest["exit_code"] = 0
        manifest["producer"] = {"agent": "attacker", "version": "9.9.9"}
        self.write_evidence("P1-TEST-A", freeze, "AC-A-01", manifest)
        self.commit_all("evidence")
        proc = self.run_validator("--require-final")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("attestation", proc.stderr)

    def test_command_tamper_fails(self) -> None:
        self.write_profile({"P1-TEST-A": {"depends_on": [], "acceptance_ids": ["AC-A-01"]}})
        freeze = self.commit_all("freeze")
        manifest = self._signed_pass("P1-TEST-A", "AC-A-01", freeze)
        manifest["command"] = "false"
        self.write_evidence("P1-TEST-A", freeze, "AC-A-01", manifest)
        self.commit_all("evidence")
        proc = self.run_validator("--require-final")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("signature", proc.stderr)

    def test_artifact_tamper_fails(self) -> None:
        self.write_profile({"P1-TEST-A": {"depends_on": [], "acceptance_ids": ["AC-A-01"]}})
        freeze = self.commit_all("freeze")
        manifest = self._signed_pass("P1-TEST-A", "AC-A-01", freeze)
        manifest["artifacts"][0]["sha256"] = "f" * 64
        self.write_evidence("P1-TEST-A", freeze, "AC-A-01", manifest)
        self.commit_all("evidence")
        proc = self.run_validator("--require-final")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("signature", proc.stderr)

    def test_modified_commit_fails(self) -> None:
        self.write_profile({"P1-TEST-A": {"depends_on": [], "acceptance_ids": ["AC-A-01"]}})
        freeze = self.commit_all("freeze")
        manifest = self._signed_pass("P1-TEST-A", "AC-A-01", freeze)
        manifest["baseline_sha"] = "c" * 40
        self.write_evidence("P1-TEST-A", freeze, "AC-A-01", manifest)
        self.commit_all("evidence")
        proc = self.run_validator("--require-final")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("signature", proc.stderr)

    def test_wrong_issuer_fails(self) -> None:
        self.write_profile({"P1-TEST-A": {"depends_on": [], "acceptance_ids": ["AC-A-01"]}})
        freeze = self.commit_all("freeze")
        manifest = evidence_signing.sign_manifest(
            make_manifest(
                task="P1-TEST-A",
                ac="AC-A-01",
                status="pass",
                implementation_sha=freeze,
                artifact_path=self._artifact("P1-TEST-A", freeze, "AC-A-01")[0],
                artifact_sha=self._artifact("P1-TEST-A", freeze, "AC-A-01")[1],
            ),
            self.secret_hex,
            issuer="evil-issuer",
            workflow=self.workflow,
            key_id=self.key_id,
            repository=TEST_REPOSITORY,
            git_ref=TEST_GIT_REF,
            run_id=TEST_RUN_ID,
        )
        self.write_evidence("P1-TEST-A", freeze, "AC-A-01", manifest)
        self.commit_all("evidence")
        proc = self.run_validator("--require-final")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("issuer", proc.stderr)

    def test_wrong_workflow_fails(self) -> None:
        self.write_profile({"P1-TEST-A": {"depends_on": [], "acceptance_ids": ["AC-A-01"]}})
        freeze = self.commit_all("freeze")
        manifest = evidence_signing.sign_manifest(
            make_manifest(
                task="P1-TEST-A",
                ac="AC-A-01",
                status="pass",
                implementation_sha=freeze,
                artifact_path=self._artifact("P1-TEST-A", freeze, "AC-A-01")[0],
                artifact_sha=self._artifact("P1-TEST-A", freeze, "AC-A-01")[1],
            ),
            self.secret_hex,
            issuer=self.issuer,
            workflow="evil-workflow/deploy",
            key_id=self.key_id,
            repository=TEST_REPOSITORY,
            git_ref=TEST_GIT_REF,
            run_id=TEST_RUN_ID,
        )
        self.write_evidence("P1-TEST-A", freeze, "AC-A-01", manifest)
        self.commit_all("evidence")
        proc = self.run_validator("--require-final")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("workflow", proc.stderr)

    def test_unknown_key_id_fails(self) -> None:
        self.write_profile({"P1-TEST-A": {"depends_on": [], "acceptance_ids": ["AC-A-01"]}})
        freeze = self.commit_all("freeze")
        manifest = evidence_signing.sign_manifest(
            make_manifest(
                task="P1-TEST-A",
                ac="AC-A-01",
                status="pass",
                implementation_sha=freeze,
                artifact_path=self._artifact("P1-TEST-A", freeze, "AC-A-01")[0],
                artifact_sha=self._artifact("P1-TEST-A", freeze, "AC-A-01")[1],
            ),
            self.secret_hex,
            issuer=self.issuer,
            workflow=self.workflow,
            key_id="untrusted-key",
            repository=TEST_REPOSITORY,
            git_ref=TEST_GIT_REF,
            run_id=TEST_RUN_ID,
        )
        self.write_evidence("P1-TEST-A", freeze, "AC-A-01", manifest)
        self.commit_all("evidence")
        proc = self.run_validator("--require-final")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("not a trusted key", proc.stderr)


class TrustAnchorTests(BaseValidatorTest):
    """S5-02: the trust root lives OUTSIDE the reviewed range and the
    attestation is CI-bound (repository / protected ref / post-commit)."""

    def test_trust_root_replaced_and_self_signed_fails_final(self) -> None:
        """The review's counter-example: replace the trust root in the SAME
        implementation commit, self-sign with the new key - the final gate
        must fail because the external anchor no longer matches."""
        self.write_profile({"P1-TEST-A": {"depends_on": [], "acceptance_ids": ["AC-A-01"]}})
        freeze = self.commit_all("freeze")
        # Attacker swaps the pinned key AND self-signs with the new one.
        attacker_secret, attacker_public = evidence_signing.generate_keypair()
        self.trust_config["keys"] = {
            "attacker-key": {
                "algorithm": "ed25519",
                "public_key": attacker_public,
            }
        }
        trust_dir = self.root / "TODO" / "evidence-trust"
        (trust_dir / "trusted_keys.json").write_text(
            json.dumps(self.trust_config), encoding="utf-8"
        )
        manifest = evidence_signing.sign_manifest(
            make_manifest(
                task="P1-TEST-A",
                ac="AC-A-01",
                status="pass",
                implementation_sha=freeze,
                artifact_path=self._artifact("P1-TEST-A", freeze, "AC-A-01")[0],
                artifact_sha=self._artifact("P1-TEST-A", freeze, "AC-A-01")[1],
            ),
            attacker_secret,
            issuer=self.issuer,
            workflow=self.workflow,
            key_id="attacker-key",
            repository=TEST_REPOSITORY,
            git_ref=TEST_GIT_REF,
            run_id=TEST_RUN_ID,
        )
        self.write_evidence("P1-TEST-A", freeze, "AC-A-01", manifest)
        self.commit_all("attacker rewrite")
        proc = self.run_validator("--require-final")
        self.assertEqual(proc.returncode, 1, proc.stdout)
        self.assertIn("externally pinned digest", proc.stderr)

    def test_attestation_from_wrong_repository_fails(self) -> None:
        self.write_profile({"P1-TEST-A": {"depends_on": [], "acceptance_ids": ["AC-A-01"]}})
        freeze = self.commit_all("freeze")
        manifest = evidence_signing.sign_manifest(
            make_manifest(
                task="P1-TEST-A",
                ac="AC-A-01",
                status="pass",
                implementation_sha=freeze,
                artifact_path=self._artifact("P1-TEST-A", freeze, "AC-A-01")[0],
                artifact_sha=self._artifact("P1-TEST-A", freeze, "AC-A-01")[1],
            ),
            self.secret_hex,
            issuer=self.issuer,
            workflow=self.workflow,
            key_id=self.key_id,
            repository="evil-org/evil-repo",
            git_ref=TEST_GIT_REF,
            run_id=TEST_RUN_ID,
        )
        self.write_evidence("P1-TEST-A", freeze, "AC-A-01", manifest)
        self.commit_all("evidence")
        proc = self.run_validator("--require-final")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("repository", proc.stderr)

    def test_attestation_from_unprotected_ref_fails(self) -> None:
        self.write_profile({"P1-TEST-A": {"depends_on": [], "acceptance_ids": ["AC-A-01"]}})
        freeze = self.commit_all("freeze")
        manifest = evidence_signing.sign_manifest(
            make_manifest(
                task="P1-TEST-A",
                ac="AC-A-01",
                status="pass",
                implementation_sha=freeze,
                artifact_path=self._artifact("P1-TEST-A", freeze, "AC-A-01")[0],
                artifact_sha=self._artifact("P1-TEST-A", freeze, "AC-A-01")[1],
            ),
            self.secret_hex,
            issuer=self.issuer,
            workflow=self.workflow,
            key_id=self.key_id,
            repository=TEST_REPOSITORY,
            git_ref="refs/heads/feature-branch",
            run_id=TEST_RUN_ID,
        )
        self.write_evidence("P1-TEST-A", freeze, "AC-A-01", manifest)
        self.commit_all("evidence")
        proc = self.run_validator("--require-final")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("git_ref", proc.stderr)

    def test_attestation_predating_implementation_commit_fails(self) -> None:
        self.write_profile({"P1-TEST-A": {"depends_on": [], "acceptance_ids": ["AC-A-01"]}})
        freeze = self.commit_all("freeze")
        manifest = self._manifest(
            task="P1-TEST-A",
            ac="AC-A-01",
            status="pass",
            implementation_sha=freeze,
        )
        # Rewind the attestation window to BEFORE the implementation commit.
        manifest["started_at"] = "2020-01-01T00:00:00Z"
        manifest["finished_at"] = "2020-01-01T00:01:00Z"
        manifest = self._sign(manifest)
        self.write_evidence("P1-TEST-A", freeze, "AC-A-01", manifest)
        self.commit_all("evidence")
        proc = self.run_validator("--require-final")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("not after the implementation commit", proc.stderr)

    def test_structure_tolerates_unsigned_pass_but_final_rejects(self) -> None:
        """S5-02: local runs record pass facts without an attestation.
        Structure mode (explicitly not release evidence) tolerates it;
        the release validator rejects it."""
        self.write_profile({"P1-TEST-A": {"depends_on": [], "acceptance_ids": ["AC-A-01"]}})
        freeze = self.commit_all("freeze")
        self.write_evidence(
            "P1-TEST-A",
            freeze,
            "AC-A-01",
            self._manifest(
                task="P1-TEST-A",
                ac="AC-A-01",
                status="pass",
                implementation_sha=freeze,
                sign=False,
            ),
        )
        self.commit_all("evidence")
        structure = self.run_validator()
        self.assertEqual(structure.returncode, 0, structure.stderr)
        final = self.run_validator("--require-final")
        self.assertEqual(final.returncode, 1)
        self.assertIn("without an attestation", final.stderr)

    def test_final_without_external_anchor_fails(self) -> None:
        """S5-02: without the externally injected anchor the release
        validator refuses to trust a checkout-supplied trust root."""
        self.freeze_repo(
            {"P1-TEST-A": {"depends_on": [], "acceptance_ids": ["AC-A-01"]}}
        )
        os.environ.pop("MAP_EVIDENCE_TRUST_DIGEST", None)
        try:
            proc = self.run_validator("--require-final")
        finally:
            os.environ["MAP_EVIDENCE_TRUST_DIGEST"] = (
                evidence_signing.trust_config_digest(self.trust_config)
            )
        self.assertEqual(proc.returncode, 1)
        self.assertIn("externally injected trust anchor", proc.stderr)



class RunFreshnessTests(BaseValidatorTest):
    """S6-04: an older CI run's attestation can never be replayed past the
    release gate - the validator compares the run identity against the
    EXTERNALLY injected expected run and rejects stale/future issuing.
    """

    def _freeze_with_pass(self, **sign_overrides):
        self.write_profile({"P1-TEST-A": {"depends_on": [], "acceptance_ids": ["AC-A-01"]}})
        freeze = self.commit_all("freeze")
        manifest = self._manifest(
            task="P1-TEST-A", ac="AC-A-01", status="pass", implementation_sha=freeze
        )
        manifest = self._sign(manifest, **sign_overrides)
        self.write_evidence("P1-TEST-A", freeze, "AC-A-01", manifest)
        self.commit_all("evidence")

    def test_replay_of_old_run_id_fails(self) -> None:
        """A manifest legitimately signed by an EARLIER protected run must
        not validate when the gate expects the CURRENT run."""
        self._freeze_with_pass(run_id="some-old-run")
        proc = self.run_validator("--require-final")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("expected protected-CI run", proc.stderr)

    def test_wrong_run_attempt_fails(self) -> None:
        self._freeze_with_pass(run_attempt="99")
        proc = self.run_validator("--require-final")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("run_attempt", proc.stderr)

    def test_missing_expected_run_identity_fails(self) -> None:
        """Without the externally injected expected run identity the
        release validator refuses to validate at all."""
        self._freeze_with_pass()
        os.environ.pop("MAP_EVIDENCE_EXPECTED_RUN_ID", None)
        os.environ.pop("MAP_EVIDENCE_EXPECTED_RUN_ATTEMPT", None)
        try:
            proc = self.run_validator("--require-final")
        finally:
            os.environ["MAP_EVIDENCE_EXPECTED_RUN_ID"] = TEST_RUN_ID
            os.environ["MAP_EVIDENCE_EXPECTED_RUN_ATTEMPT"] = TEST_RUN_ATTEMPT
        self.assertEqual(proc.returncode, 1)
        self.assertIn("MAP_EVIDENCE_EXPECTED_RUN_ID", proc.stderr)

    def test_future_issued_at_fails(self) -> None:
        from datetime import datetime, timedelta, timezone

        future = (
            datetime.now(timezone.utc) + timedelta(hours=2)
        ).isoformat().replace("+00:00", "Z")
        self._freeze_with_pass(issued_at=future)
        proc = self.run_validator("--require-final")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("in the future", proc.stderr)

    def test_stale_issued_at_before_commit_fails(self) -> None:
        """An attestation ISSUED before the implementation commit cannot
        attest this commit (stale-signature replay)."""
        self._freeze_with_pass(issued_at="2020-01-01T00:00:00Z")
        proc = self.run_validator("--require-final")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("issued_at", proc.stderr)

    def test_current_run_with_attempt_still_passes(self) -> None:
        """The current protected run's offline verification keeps
        passing with run_id + run_attempt + issued_at enforced."""
        self._freeze_with_pass()
        proc = self.run_validator("--require-final")
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
