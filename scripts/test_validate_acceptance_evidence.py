#!/usr/bin/env python3
"""Self-test for scripts/validate_acceptance_evidence.py (S2-01).

Pure-stdlib unittest, no third-party dependencies, runnable anywhere the
gate runs (system python3 + git). Each test builds a throwaway git
repository in a temp dir and drives the validator with --root/--profile,
covering the S2-01 failure matrix: blocked evidence, stale sha, wrong
directory layout, wrong task/ac ids, extra (schema-rejected) fields,
expired waivers, artifact hash tampering, dependency cycles, freeze sha
ancestry and the evidence-only-tail rule.

Run:  python3 scripts/test_validate_acceptance_evidence.py
Exit: 0 = all tests pass; 1 = at least one failure.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent / "validate_acceptance_evidence.py"
REPO_SCHEMA = Path(__file__).resolve().parents[1] / "TODO" / "evidence-manifest.schema.json"

FAKE_SHA_1 = "a" * 40
FAKE_SHA_2 = "b" * 40


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
) -> dict:
    manifest: dict = {
        "schema_version": "1.1.0",
        "task_id": task,
        "ac_id": ac,
        "status": status,
        "baseline_sha": FAKE_SHA_1,
        "implementation_sha": implementation_sha,
        "environment_digest": "e" * 64,
        "started_at": "2026-08-13T10:00:00Z",
        "finished_at": "2026-08-13T10:01:00Z",
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
        (self.root / "artifact.txt").write_text("fixture artifact\n", encoding="utf-8")
        self.artifact_sha = self._sha256(self.root / "artifact.txt")
        self.profile = self.root / "TODO" / "acceptance-profile.yaml"

    def tearDown(self) -> None:
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

    def freeze_repo(self, registry: dict) -> tuple[str, str]:
        """Commit the profile + artifact, then also commit evidence tree.

        Returns (freeze_sha, head_after_evidence). The evidence commit
        touches only tmp/acceptance/** so --require-final passes the
        evidence-only-tail rule.
        """
        self.write_profile(registry)
        freeze = self.commit_all("freeze")
        for task, spec in registry.items():
            for ac in spec.get("acceptance_ids", []):
                self.write_evidence(
                    task,
                    freeze,
                    ac,
                    make_manifest(
                        task=task,
                        ac=ac,
                        status="pass",
                        implementation_sha=freeze,
                        artifact_path="artifact.txt",
                        artifact_sha=self.artifact_sha,
                    ),
                )
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
            make_manifest(
                task="P1-TEST-A",
                ac="AC-A-01",
                status="blocked",
                implementation_sha=freeze,
                artifact_path="artifact.txt",
                artifact_sha=self.artifact_sha,
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
            make_manifest(
                task="P1-TEST-A",
                ac="AC-A-01",
                status=status,
                implementation_sha=freeze,
                artifact_path="artifact.txt",
                artifact_sha=self.artifact_sha,
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
            make_manifest(
                task="P1-TEST-A",
                ac="AC-A-01",
                status="superseded",
                implementation_sha=freeze,
                artifact_path="artifact.txt",
                artifact_sha=self.artifact_sha,
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
            make_manifest(
                task="P1-TEST-A",
                ac="AC-A-01",
                status="not-applicable-approved",
                implementation_sha=freeze,
                artifact_path="artifact.txt",
                artifact_sha=self.artifact_sha,
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
            make_manifest(
                task="P1-TEST-A",
                ac="AC-A-01",
                status="not-applicable-approved",
                implementation_sha=freeze,
                artifact_path="artifact.txt",
                artifact_sha=self.artifact_sha,
                waiver_expires_at="2020-01-01T00:00:00Z",
            ),
        )
        self.commit_all("evidence")
        proc = self.run_validator("--require-final")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("waiver expired", proc.stderr)


class IntegrityTests(BaseValidatorTest):
    def test_stale_sha_not_marked_superseded_fails(self) -> None:
        self.write_profile({"P1-TEST-A": {"depends_on": [], "acceptance_ids": ["AC-A-01"]}})
        freeze = self.commit_all("freeze")
        self.write_evidence(
            "P1-TEST-A",
            freeze,
            "AC-A-01",
            make_manifest(
                task="P1-TEST-A",
                ac="AC-A-01",
                status="pass",
                implementation_sha=freeze,
                artifact_path="artifact.txt",
                artifact_sha=self.artifact_sha,
            ),
        )
        # a SECOND manifest under a different sha dir that is not superseded
        # but claims the freeze implementation_sha
        self.write_evidence(
            "P1-TEST-A",
            FAKE_SHA_2,
            "AC-A-01",
            make_manifest(
                task="P1-TEST-A",
                ac="AC-A-01",
                status="pass",
                implementation_sha=freeze,
                artifact_path="artifact.txt",
                artifact_sha=self.artifact_sha,
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
            make_manifest(
                task="P1-TEST-A",
                ac="AC-A-01",
                status="pass",
                implementation_sha=freeze,
                artifact_path="artifact.txt",
                artifact_sha=self.artifact_sha,
            ),
        )
        self.write_evidence(
            "P1-TEST-A",
            FAKE_SHA_2,
            "AC-A-01",
            make_manifest(
                task="P1-TEST-A",
                ac="AC-A-01",
                status="superseded",
                implementation_sha=FAKE_SHA_2,
                artifact_path="artifact.txt",
                artifact_sha=self.artifact_sha,
            ),
        )
        self.commit_all("evidence")
        proc = self.run_validator()
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_ac_id_directory_mismatch_fails(self) -> None:
        self.write_profile({"P1-TEST-A": {"depends_on": [], "acceptance_ids": ["AC-A-01"]}})
        freeze = self.commit_all("freeze")
        # manifest claims AC-A-01 but sits in a directory named AC-A-99
        self.write_evidence(
            "P1-TEST-A",
            freeze,
            "AC-A-99",
            make_manifest(
                task="P1-TEST-A",
                ac="AC-A-01",
                status="pass",
                implementation_sha=freeze,
                artifact_path="artifact.txt",
                artifact_sha=self.artifact_sha,
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
        # manifest claims task P1-TEST-A but sits in the P1-TEST-B directory
        self.write_evidence(
            "P1-TEST-B",
            freeze,
            "AC-A-01",
            make_manifest(
                task="P1-TEST-A",
                ac="AC-A-01",
                status="pass",
                implementation_sha=freeze,
                artifact_path="artifact.txt",
                artifact_sha=self.artifact_sha,
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
            make_manifest(
                task="P1-TEST-A",
                ac="AC-A-01",
                status="pass",
                implementation_sha=FAKE_SHA_2,
                artifact_path="artifact.txt",
                artifact_sha=self.artifact_sha,
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
            make_manifest(
                task="P1-TEST-A",
                ac="AC-A-01",
                status="pass",
                implementation_sha=freeze,
                artifact_path="artifact.txt",
                artifact_sha=self.artifact_sha,
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
        self.write_evidence(
            "P1-TEST-A",
            freeze,
            "AC-A-01",
            make_manifest(
                task="P1-TEST-A",
                ac="AC-A-01",
                status="pass",
                implementation_sha=freeze,
                artifact_path="artifact.txt",
                artifact_sha="f" * 64,
            ),
        )
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
        # replace HEAD with an unrelated root commit -> freeze is no longer
        # an ancestor of HEAD
        self._git("checkout", "-q", "--orphan", "detached")
        self._git("rm", "-q", "-rf", ".")
        (self.root / "other.txt").write_text("unrelated\n", encoding="utf-8")
        (self.root / "artifact.txt").write_text("fixture artifact\n", encoding="utf-8")
        (self.root / "TODO").mkdir(parents=True, exist_ok=True)
        (self.root / "TODO" / "evidence-manifest.schema.json").write_text(
            REPO_SCHEMA.read_text(encoding="utf-8"), encoding="utf-8"
        )
        self.write_profile(
            {"P1-TEST-A": {"depends_on": [], "acceptance_ids": ["AC-A-01"]}}
        )
        self.commit_all("new root")
        self.write_evidence(
            "P1-TEST-A",
            orphan,
            "AC-A-01",
            make_manifest(
                task="P1-TEST-A",
                ac="AC-A-01",
                status="pass",
                implementation_sha=orphan,
                artifact_path="artifact.txt",
                artifact_sha=self.artifact_sha,
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
            make_manifest(
                task="P1-TEST-A",
                ac="AC-A-01",
                status="pass",
                implementation_sha=freeze,
                artifact_path="artifact.txt",
                artifact_sha=self.artifact_sha,
            ),
        )
        self.commit_all("evidence")
        # a PRODUCT change lands after the freeze sha
        (self.root / "src").mkdir(parents=True)
        (self.root / "src" / "app.py").write_text("print('x')\n", encoding="utf-8")
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
                make_manifest(
                    task="P1-TEST-A",
                    ac=ac,
                    status="pass",
                    implementation_sha=freeze,
                    artifact_path="artifact.txt",
                    artifact_sha=self.artifact_sha,
                ),
            )
        self.commit_all("evidence at freeze")
        proc = self.run_validator("--require-final")
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
