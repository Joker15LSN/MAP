#!/usr/bin/env python3
"""Self-test for scripts/security_scan.py (S2-05).

Pure-stdlib unittest. Covers the second-round review acceptance matrix:

- substring allowlist (fake/example/changeme inside a real formatted
  token) is NO LONGER exempt and always reported;
- exemptions are exact (path, rule, line) with an expiry; expired
  exemptions stop applying (fail closed);
- oversized text (10 MiB+) is scanned STREAMING - canaries are still hit;
- the tree scope reads an EXPLICIT git commit (tree object), not the
  working tree;
- unreadable/binary members in strict scopes are recorded and fail the
  scan (exit 2), never a silent skip;
- scanner output never contains the matched secret itself.

Run:  python3 scripts/test_security_scan.py
Exit: 0 = all tests pass; 1 = at least one failure.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest import mock

SCRIPT = Path(__file__).resolve().parent / "security_scan.py"
REPO_ROOT = Path(__file__).resolve().parents[1]

sys.path.insert(0, str(REPO_ROOT))

import scripts.security_scan as scan  # noqa: E402


class ScanUnitTests(unittest.TestCase):
    def _hits(self, text: str) -> list[scan.Hit]:
        hits: list[scan.Hit] = []
        scan.scan_text(text, "tree:probe.txt", hits, relpath="probe.txt")
        return hits

    def test_substring_allowlist_no_longer_exempts(self) -> None:
        """fake/example/changeme inside a formatted token MUST be reported."""
        for token in (
            "sk-" + "fake-" + "abcdefghijklmnopqrstuvwxyz012345",  # fake substring
            "sk-" + "example-" + "abcdefghijklmnopqrstuvwxyz0123",  # example substring
            "sk-" + "changeme-" + "abcdefghijklmnopqrstuvwxyz012",  # changeme substring
        ):
            with self.subTest(token=token):
                hits = self._hits('key = "%s"' % token)
                self.assertTrue(hits, token)
                self.assertEqual(hits[0].pattern, "openai_key", token)

    def test_exact_placeholder_values_still_exempt(self) -> None:
        hits = self._hits('password = "changeme"')
        self.assertEqual(hits, [], "bare placeholder must stay exempt")

    def test_exemption_is_exact_line_and_rule(self) -> None:
        # the registered exemption covers .env.example:34 only; the same
        # value on any other line must hit.
        value = "MAP_POSTGRES_ADMIN_PASSWORD=map-admin-local"
        hits: list[scan.Hit] = []
        scan.scan_text(
            value + "\n",
            "tree:.env.example",
            hits,
            relpath=".env.example",
        )
        # line 1 is NOT the registered line 34, so it must hit
        self.assertTrue(hits, "same literal on a different line must hit")
        self.assertEqual(hits[0].pattern, "env_password_literal")

    def test_registered_exemption_line_is_skipped(self) -> None:
        hits: list[scan.Hit] = []
        lines = ["\n"] * 33 + ["MAP_POSTGRES_ADMIN_PASSWORD=map-admin-local\n"]
        scan.scan_text(
            "".join(lines), "tree:.env.example", hits, relpath=".env.example"
        )
        self.assertEqual(hits, [], "line 34 is the registered exemption")

    def test_expired_exemption_fails_closed(self) -> None:
        past = (date.today() - timedelta(days=1)).isoformat()
        expired = scan.Exemption(
            "probe.txt", "env_password_literal", 1,
            "expired", "platform-security", past,
            expected_fingerprint="sha256:0000000000000000",
        )
        self.assertTrue(scan._exemption_expired(expired))
        with mock.patch("scripts.security_scan.EXEMPTIONS", (expired,)):
            hits = self._hits("X_PASSWORD=somevalue")
            self.assertTrue(hits, "expired exemption must not apply")

    def test_10mb_text_is_scanned_streaming(self) -> None:
        """Oversized text can no longer be skipped: a canary must be hit."""
        filler = "x" * 1024 * 1024 * 10
        canary_line = 'tok = "' + "sk-" + "fake-" + "streaming-canary-0123456789abcdef" + '"\n'
        payload = (filler + "\n" + canary_line).encode("utf-8")
        self.assertGreater(len(payload), scan.MAX_TEXT_FILE_BYTES)
        hits: list[scan.Hit] = []
        unscanned: list[dict[str, str]] = []
        scanned = scan.scan_bytes_stream(
            iter([payload]), "tree:big.txt", "big.txt", hits, unscanned, strict=True
        )
        self.assertTrue(scanned)
        self.assertEqual(unscanned, [])
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].pattern, "openai_key")

    def test_binary_member_recorded_not_silently_skipped(self) -> None:
        hits: list[scan.Hit] = []
        unscanned: list[dict[str, str]] = []
        scanned = scan.scan_bytes_stream(
            iter([b"\x00\x01\x02binary"]),
            "tree:bin.dat",
            "bin.dat",
            hits,
            unscanned,
            strict=True,
        )
        self.assertFalse(scanned)
        self.assertEqual(unscanned, [{"location": "tree:bin.dat", "reason": "binary"}])
        self.assertEqual(hits, [])

    def test_strict_unscanned_members_fail_the_scan(self) -> None:
        """main() must exit 2 when strict scopes have unscanned members."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._git(repo, "init", "-q")
            (repo / "keep").write_text("content\n", encoding="utf-8")
            self._git(repo, "add", "-A")
            self._git(repo, "commit", "-q", "-m", "init")
            with mock.patch("scripts.security_scan.ROOT", repo):
                with mock.patch.object(
                    scan, "scope_tree",
                    side_effect=lambda hits, unscanned, *, commit=None, drifted_exemptions=None: unscanned.append(
                        {"location": "tree:missing.txt", "reason": "unreadable"}
                    ),
                ):
                    rc = scan.main(["--scope", "tree", "--fail-on-hit", "--json"])
        self.assertEqual(rc, 2, "unscanned strict member must fail closed")

    def test_tree_scope_reads_explicit_commit(self) -> None:
        """The tree scope scans the git TREE at --commit, not the worktree."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._git(repo, "init", "-q")
            (repo / "secret.txt").write_text(
                'tok = "' + "sk-" + "fake-" + "committed-canary-0123456789abcdef" + '"\n',
                encoding="utf-8",
            )
            self._git(repo, "add", "-A")
            self._git(repo, "commit", "-q", "-m", "planted canary")
            commit = self._git(repo, "rev-parse", "HEAD").strip()
            # rewrite the working tree: the scan must NOT see this version
            (repo / "secret.txt").write_text("clean content\n", encoding="utf-8")

            hits: list[scan.Hit] = []
            unscanned: list[dict[str, str]] = []
            with mock.patch("scripts.security_scan.ROOT", repo):
                scan.scope_tree(hits, unscanned, commit=commit)
            self.assertTrue(hits, "committed canary must be found at that commit")
            self.assertEqual(hits[0].pattern, "openai_key")

    def test_angle_bracket_substring_is_not_a_placeholder(self) -> None:
        """S3-03: a '<...>' SUBSTRING inside a real formatted token must hit;
        only a code-reviewed exact value is exempt."""
        hits = self._hits('api_key = "real' + '<secret>' + 'credential-value"')
        self.assertTrue(hits, "angle-bracket substring must not be exempt")
        # a REGISTERED exact value stays exempt
        self.assertEqual(self._hits('api_key = "<local-dev-password>"'), [])

    def test_unregistered_angle_bracket_whole_values_hit(self) -> None:
        """S4-04: ANY unregistered whole '<...>' value must hit - there is no
        placeholder shape exemption anymore."""
        for value in (
            "<production-primary-secret-value>",
            "<database-root-password>",
            "<any-unregistered-placeholder>",
            "<totally-made-up>",
        ):
            with self.subTest(value=value):
                self.assertTrue(
                    self._hits('api_key = "%s"' % value),
                    "unregistered %s must hit" % value,
                )
                self.assertTrue(
                    self._hits('password = "%s"' % value),
                    "unregistered %s must hit" % value,
                )

    def test_registered_exact_values_still_pass(self) -> None:
        """S4-04: only the code-reviewed exact values are exempt."""
        for value in sorted(scan._ALLOWED_EXACT_VALUES):
            with self.subTest(value=value):
                self.assertEqual(
                    self._hits('api_key = "%s"' % value),
                    [],
                    "registered %r must stay exempt" % value,
                )

    def test_exemption_value_drift_is_reported(self) -> None:
        """S3-03: replacing the value on an exempt line with a different
        (possibly real) credential must fail the scan with a drift note."""
        hits: list[scan.Hit] = []
        drifted: list[str] = []
        scan.scan_text(
            "MAP_POSTGRES_ADMIN_PASSWORD=map-admin-local\n",
            "tree:.env.example",
            hits,
            relpath=".env.example",
            drifted_exemptions=drifted,
        )
        self.assertTrue(hits, "line 1 is NOT the exempt line 34, so it must hit")
        # line 34 with the ORIGINAL value is exempt...
        lines = ["\n"] * 33 + ["MAP_POSTGRES_ADMIN_PASSWORD=map-admin-local\n"]
        hits2: list[scan.Hit] = []
        scan.scan_text(
            "".join(lines), "tree:.env.example", hits2, relpath=".env.example"
        )
        self.assertEqual(hits2, [])
        # ...but a DIFFERENT value on line 34 is a drifted exemption: the
        # hit is reported and the drift is annotated.
        lines = ["\n"] * 33 + ["MAP_POSTGRES_ADMIN_PASSWORD=real-password-value-77\n"]
        hits3: list[scan.Hit] = []
        drifted3: list[str] = []
        scan.scan_text(
            "".join(lines),
            "tree:.env.example",
            hits3,
            relpath=".env.example",
            drifted_exemptions=drifted3,
        )
        self.assertTrue(hits3, "drifted exempt line must hit")
        self.assertTrue(drifted3, "drift must be annotated")

    def test_oci_layout_image_tarball_is_scanned(self) -> None:
        """Docker Desktop saves OCI layouts (blobs/sha256/<digest>): a
        canary inside a gzip-compressed layer blob must still be found."""
        import gzip as gziplib
        import io
        import json as jsonlib
        import tarfile as tarfilelib

        canary = 'tok = "' + "sk-" + "fake-" + "oci-layer-canary-0123456789abc" + '"\n'
        layer_buf = io.BytesIO()
        with tarfilelib.open(fileobj=layer_buf, mode="w") as layer:
            info = tarfilelib.TarInfo("app/secrets.txt")
            payload_bytes = canary.encode()
            info.size = len(payload_bytes)
            layer.addfile(info, io.BytesIO(payload_bytes))
        layer_buf.seek(0)
        compressed = gziplib.compress(layer_buf.getvalue())

        outer_buf = io.BytesIO()
        with tarfilelib.open(fileobj=outer_buf, mode="w") as outer:
            manifest = jsonlib.dumps(
                [{"Config": "blobs/sha256/cfg", "Layers": ["blobs/sha256/layer"]}]
            ).encode()
            mi = tarfilelib.TarInfo("manifest.json")
            mi.size = len(manifest)
            outer.addfile(mi, io.BytesIO(manifest))
            cfg = jsonlib.dumps({"config": {}}).encode()
            ci = tarfilelib.TarInfo("blobs/sha256/cfg")
            ci.size = len(cfg)
            outer.addfile(ci, io.BytesIO(cfg))
            li = tarfilelib.TarInfo("blobs/sha256/layer")
            li.size = len(compressed)
            outer.addfile(li, io.BytesIO(compressed))

        hits: list[scan.Hit] = []
        unscanned: list[dict[str, str]] = []
        scan._scan_image_tarball("probe-oci", outer_buf.getvalue(), hits, [], unscanned)
        assert any(h.pattern == "openai_key" for h in hits), [
            h.to_dict() for h in hits
        ]

    def test_report_never_contains_the_secret(self) -> None:
        token = "sk-fake-redaction-probe-" + "0123456789abcdef"
        hits: list[scan.Hit] = []
        scan.scan_text('key = "%s"' % token, "tree:probe.txt", hits, relpath="probe.txt")
        self.assertTrue(hits)
        report = scan.build_report(hits, ["tree"], {})
        serialized = json.dumps(report, ensure_ascii=False)
        self.assertNotIn(token, serialized)
        self.assertIn("sha256:", serialized)

    # ---- S4-04: exemption-table integrity (fail closed at startup) ----

    @staticmethod
    def _exemption(**overrides) -> scan.Exemption:
        base = dict(
            path="probe.txt", rule="env_password_literal", line=1,
            reason="probe", owner="platform-security",
            expires_at=(date.today() + timedelta(days=30)).isoformat(),
            expected_fingerprint="sha256:0000000000000000",
        )
        base.update(overrides)
        return scan.Exemption(**base)

    def test_validate_exemptions_accepts_registered_table(self) -> None:
        self.assertEqual(scan.validate_exemptions(), [])

    def test_validate_exemptions_rejects_missing_fingerprint(self) -> None:
        problems = scan.validate_exemptions(
            (self._exemption(expected_fingerprint=""),)
        )
        self.assertTrue(problems, "empty fingerprint must be rejected")
        self.assertIn("no expected_fingerprint", problems[0])

    def test_validate_exemptions_rejects_duplicate(self) -> None:
        problems = scan.validate_exemptions(
            (self._exemption(), self._exemption())
        )
        self.assertTrue(problems, "duplicate exemption must be rejected")
        self.assertIn("duplicate", problems[0])

    def test_validate_exemptions_rejects_expired(self) -> None:
        past = (date.today() - timedelta(days=1)).isoformat()
        problems = scan.validate_exemptions(
            (self._exemption(expires_at=past),)
        )
        self.assertTrue(problems, "expired exemption must be rejected")
        self.assertIn("expired", problems[0])

    def test_main_fails_closed_on_invalid_exemption_table(self) -> None:
        """S4-04: an unhealthy exemption table makes main() exit non-zero
        BEFORE any scope runs."""
        with mock.patch(
            "scripts.security_scan.validate_exemptions",
            return_value=["duplicate exemption probe.txt:1"],
        ):
            rc = scan.main(["--scope", "tree", "--json"])
        self.assertEqual(rc, 2)

    def test_main_drift_exits_nonzero_without_fail_on_hit(self) -> None:
        """S4-04: exemption value drift is a config-integrity failure; the
        scan exits 1 even when --fail-on-hit is absent."""
        def fake_scope_tree(hits, unscanned, *, commit=None, drifted_exemptions=None):
            if drifted_exemptions is not None:
                drifted_exemptions.append("probe.txt:1: exempted value drifted")
        with mock.patch("scripts.security_scan.scope_tree", side_effect=fake_scope_tree):
            rc = scan.main(["--scope", "tree"])
        self.assertEqual(rc, 1)

    def test_counterexample_matrix_shared_across_scopes(self) -> None:
        """S4-04: the same unregistered-'<...>' counterexample matrix must
        hit in every scope: text scan, streaming scan, build-context and
        image layers."""
        counterexamples = (
            ('api_key = "<production-primary-secret-value>"', "literal_secret_assignment"),
            ('password = "<database-root-password>"', "literal_password_assignment"),
        )
        for text, pattern in counterexamples:
            with self.subTest(scope="scan_text", text=text):
                hits: list[scan.Hit] = []
                scan.scan_text(text, "tree:probe.txt", hits, relpath="probe.txt")
                self.assertTrue(hits)
                self.assertEqual(hits[0].pattern, pattern)
            with self.subTest(scope="scan_bytes_stream", text=text):
                hits = []
                scan.scan_bytes_stream(
                    iter([text.encode("utf-8")]), "tree:probe.txt", "probe.txt",
                    hits, [], strict=True,
                )
                self.assertTrue(hits)
                self.assertEqual(hits[0].pattern, pattern)
            with self.subTest(scope="build-context", text=text):
                with tempfile.TemporaryDirectory() as tmp:
                    repo = Path(tmp)
                    # scope_build_context iterates every declared build
                    # context, so the temp repo needs all three dirs.
                    for rel in (
                        "map_core",
                        "map-business-backend",
                        "map-observability/map-observability-backend",
                    ):
                        (repo / rel).mkdir(parents=True, exist_ok=True)
                    ctx = repo / "map_core"
                    (ctx / "secret.txt").write_text(text + "\n", encoding="utf-8")
                    hits = []
                    with mock.patch("scripts.security_scan.ROOT", repo):
                        scan.scope_build_context(hits, [])
                    self.assertTrue(
                        hits, "build-context must hit %r" % text
                    )
            with self.subTest(scope="image", text=text):
                import io
                import tarfile as tarfilelib
                buf = io.BytesIO()
                with tarfilelib.open(fileobj=buf, mode="w") as layer:
                    payload = (text + "\n").encode()
                    info = tarfilelib.TarInfo("app/secret.txt")
                    info.size = len(payload)
                    layer.addfile(info, io.BytesIO(payload))
                outer = io.BytesIO()
                with tarfilelib.open(fileobj=outer, mode="w") as outer_tar:
                    manifest = json.dumps(
                        [{"Config": "blobs/sha256/cfg", "Layers": ["blobs/sha256/layer"]}]
                    ).encode()
                    mi = tarfilelib.TarInfo("manifest.json")
                    mi.size = len(manifest)
                    outer_tar.addfile(mi, io.BytesIO(manifest))
                    cfg = json.dumps({"config": {}}).encode()
                    ci = tarfilelib.TarInfo("blobs/sha256/cfg")
                    ci.size = len(cfg)
                    outer_tar.addfile(ci, io.BytesIO(cfg))
                    li = tarfilelib.TarInfo("blobs/sha256/layer")
                    li.size = len(buf.getvalue())
                    outer_tar.addfile(li, io.BytesIO(buf.getvalue()))
                hits: list[scan.Hit] = []
                scan._scan_image_tarball(
                    "probe", outer.getvalue(), hits, [], []
                )
                self.assertTrue(hits, "image layer must hit %r" % text)

    @staticmethod
    def _git(repo: Path, *args: str) -> str:
        proc = subprocess.run(
            ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise RuntimeError(f"git {args} failed: {proc.stderr}")
        return proc.stdout


if __name__ == "__main__":
    unittest.main(verbosity=2)
