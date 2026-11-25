import base64
import contextlib
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from scripts import prepare_luciole as prepare


OLD = "1" * 40
CURRENT = "2" * 40
HEAD = "3" * 40
CURRENT_TREE = "4" * 40
HEAD_TREE = "5" * 40
CURRENT_NAME = "luciole-0.1.0_pre20261124203731.ebuild"
OLD_NAME = "luciole-0.1.0_pre20261123203731.ebuild"
CANDIDATE_NAME = "luciole-0.1.0_pre20261125134857.ebuild"
EBUILD = (
    b'EAPI=8\nMY_COMMIT="' + CURRENT.encode() + b'"\n'
    b'SRC_URI="https://github.com/noctalia-dev/luciole/archive/${MY_COMMIT}.tar.gz"\n'
    b'LICENSE="MIT HPND"\nIUSE="test"\nDEPEND="dev-libs/wayland"\n'
    b'# Another occurrence of the old SHA stays untouched: ' + CURRENT.encode() + b'\n'
    b'src_configure() {\n\tmeson_src_configure\n}'
)


def version_file(version=b"0.1.0\n"):
    return {
        "type": "file",
        "path": "VERSION",
        "encoding": "base64",
        "content": base64.b64encode(version).decode(),
    }


def tree(sha, files):
    return {
        "sha": sha,
        "truncated": False,
        "tree": [
            {"path": path, "type": "blob", "mode": "100644", "sha": blob}
            for path, blob in files.items()
        ],
    }


class PrepareLucioleTest(unittest.TestCase):
    def setUp(self):
        workspace = Path(self.enterContext(TemporaryDirectory()))
        self.root = workspace / "accepted"
        self.package = self.root / prepare.PACKAGE
        self.package.mkdir(parents=True)
        self.parent = workspace / "staging"
        self.parent.mkdir()
        (self.package / CURRENT_NAME).write_bytes(EBUILD)
        (self.package / "Manifest").write_bytes(b"existing Manifest\n")
        (self.package / "metadata.xml").write_bytes(b"<pkgmetadata/>\n")
        (self.package / "files").mkdir()
        (self.package / "files/example.patch").write_bytes(b"packaging resource\n")
        self.compare_path = f"compare/{CURRENT}...{HEAD}?per_page=1"
        self.version_path = f"contents/VERSION?ref={HEAD}"
        self.commit_path = f"git/commits/{HEAD}"
        self.responses = {
            "git/ref/heads/main": {
                "ref": "refs/heads/main", "object": {"type": "commit", "sha": HEAD},
            },
            self.compare_path: {
                "status": "ahead", "ahead_by": 2, "behind_by": 0,
                "base_commit": {"sha": CURRENT}, "merge_base_commit": {"sha": CURRENT},
            },
            self.version_path: version_file(),
            self.commit_path: {
                "sha": HEAD, "tree": {"sha": HEAD_TREE},
                "committer": {"date": "2026-11-26T00:48:57+11:00"},
                "author": {"date": "2026-11-22T12:00:00Z"},
            },
            f"git/commits/{CURRENT}": {"sha": CURRENT, "tree": {"sha": CURRENT_TREE}},
            f"git/trees/{CURRENT_TREE}?recursive=1": tree(CURRENT_TREE, {
                "VERSION": OLD, "meson.build": OLD,
                "lib/assets/fonts/tabler-icons-license.txt": OLD,
            }),
            f"git/trees/{HEAD_TREE}?recursive=1": tree(HEAD_TREE, {
                "VERSION": OLD, "meson.build": HEAD, "PACKAGING.md": HEAD,
                "lib/protocols/text-input-unstable-v3.xml": HEAD,
                "src/unrelated.cpp": HEAD,
            }),
        }
        self.http = self.enterContext(patch.object(prepare, "urlopen", side_effect=self.respond))

    def respond(self, request, *, timeout):
        self.assertEqual(timeout, prepare.HTTP_TIMEOUT)
        self.assertTrue(request.full_url.startswith(prepare.API + "/"))
        result = self.responses[request.full_url.removeprefix(prepare.API + "/")]
        if isinstance(result, Exception):
            raise result
        if isinstance(result, bytes):
            return io.BytesIO(result)
        return io.BytesIO(json.dumps(result).encode())

    def discover(self):
        return prepare.discover(self.package)

    def run_cli(self, *args):
        output = self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        errors = self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        code = prepare.main(list(args), root=self.root)
        return code, output.getvalue(), errors.getvalue()

    def package_contents(self, directory):
        return {
            path.relative_to(directory).as_posix(): path.read_bytes()
            for path in directory.rglob("*") if path.is_file()
        }

    def add_previous(self):
        (self.package / OLD_NAME).write_bytes(EBUILD.replace(CURRENT.encode(), OLD.encode()))

    def test_discovery_pins_main_once_and_uses_utc_committer_date(self):
        plan = self.discover()
        self.assertEqual(plan.filename, CANDIDATE_NAME)
        self.assertEqual(plan.sha, HEAD)
        self.assertEqual(plan.committed_at.isoformat(), "2026-11-25T13:48:57+00:00")
        self.assertIsNone(plan.previous)
        self.assertEqual(plan.changes["VERSION"], "unchanged")
        self.assertEqual(plan.changes["meson.build"], "modified")
        self.assertEqual(plan.changes["PACKAGING.md"], "added")
        self.assertEqual(plan.changes["LICENSE"], "absent")
        self.assertEqual(plan.changes["lib/meson.build"], "absent")
        self.assertEqual(plan.changes["lib/assets/fonts/tabler-icons-license.txt"], "removed")
        self.assertEqual(plan.changes["lib/protocols/text-input-unstable-v3.xml"], "added")
        self.assertNotIn("src/unrelated.cpp", plan.changes)
        urls = [call.args[0].full_url for call in self.http.call_args_list]
        self.assertEqual([url for url in urls if "main" in url], [prepare.API + "/git/ref/heads/main"])
        self.assertIn(prepare.API + "/" + self.version_path, urls)
        self.assertIn(prepare.API + "/" + self.commit_path, urls)

    def test_preparation_preserves_bytes_and_accepted_tree(self):
        before = self.package_contents(self.package)
        code, output, errors = self.run_cli("--stage-dir", str(self.parent))
        self.assertEqual((code, errors), (0, ""))
        stage, = self.parent.iterdir()
        self.assertIn(str(stage), output)
        self.assertIn("Phase 4.2", (stage / "INCOMPLETE.txt").read_text())
        candidate = EBUILD.replace(
            b'MY_COMMIT="' + CURRENT.encode(), b'MY_COMMIT="' + HEAD.encode(), 1
        )
        self.assertEqual(self.package_contents(stage / prepare.PACKAGE), {
            **before, CANDIDATE_NAME: candidate,
        })
        self.assertEqual(self.package_contents(self.package), before)

    def test_rotation_drops_only_oldest_from_staged_tree(self):
        self.add_previous()
        before = self.package_contents(self.package)
        code, output, errors = self.run_cli("--stage-dir", str(self.parent))
        self.assertEqual((code, errors), (0, ""))
        self.assertIn(f"Drop {OLD_NAME}", output)
        stage, = self.parent.iterdir()
        staged = self.package_contents(stage / prepare.PACKAGE)
        self.assertEqual(set(staged), (before.keys() - {OLD_NAME}) | {CANDIDATE_NAME})
        self.assertEqual(staged[CURRENT_NAME], before[CURRENT_NAME])
        self.assertEqual(self.package_contents(self.package), before)

    def test_dry_run_does_not_create_stage_or_change_accepted_tree(self):
        self.add_previous()
        before = self.package_contents(self.root)
        code, output, errors = self.run_cli("--dry-run")
        self.assertEqual((code, errors), (0, ""))
        for value in (CANDIDATE_NAME, HEAD, f"Drop {OLD_NAME}", f"{CURRENT}...{HEAD}", "meson.build"):
            self.assertIn(value, output)
        self.assertEqual(self.package_contents(self.root), before)
        self.assertEqual(list(self.parent.iterdir()), [])

    def test_unchanged_sha_only_resolves_main_and_creates_no_stage(self):
        self.responses["git/ref/heads/main"]["object"]["sha"] = CURRENT
        code, output, errors = self.run_cli("--stage-dir", str(self.parent))
        self.assertEqual((code, errors), (0, ""))
        self.assertIn("up to date", output)
        self.assertEqual(self.http.call_count, 1)
        self.assertEqual(list(self.parent.iterdir()), [])

    def test_invalid_version(self):
        for version in (b"", b"v0.1.0", b"01.1.0", b"0.1.0-rc1", b"0.1.0\n0.2.0"):
            with self.subTest(version=version):
                self.responses[self.version_path] = version_file(version)
                with self.assertRaisesRegex(prepare.PreparationError, "VERSION"):
                    self.discover()

    def test_missing_version_and_http_failure(self):
        for code in (404, 503):
            with self.subTest(code=code):
                self.responses[self.version_path] = HTTPError(
                    prepare.API, code, "request failed", None, None,
                )
                with self.assertRaisesRegex(prepare.PreparationError, f"VERSION.*HTTP {code}"):
                    self.discover()

    def test_invalid_sha_and_malformed_ref_response(self):
        for response in ([], b"<html>", {"object": {}}, {
            "ref": "refs/heads/main", "object": {"type": "commit", "sha": "abc"},
        }):
            with self.subTest(response=response):
                self.responses["git/ref/heads/main"] = response
                with self.assertRaises(prepare.PreparationError):
                    self.discover()

    def test_inconsistent_commit_and_incomplete_tree_are_rejected(self):
        details = self.responses[self.commit_path]
        details["sha"] = OLD
        with self.assertRaisesRegex(prepare.PreparationError, "commit metadata"):
            self.discover()
        details["sha"] = HEAD
        self.responses[f"git/trees/{HEAD_TREE}?recursive=1"]["truncated"] = True
        with self.assertRaisesRegex(prepare.PreparationError, "tree"):
            self.discover()

    def test_invalid_committer_timestamp_is_rejected(self):
        for date in ("2026-11-25T13:48:57", "2026-11-25T13:48:57+01:60", "2026-02-30T13:48:57Z"):
            with self.subTest(date=date):
                self.responses[self.commit_path]["committer"]["date"] = date
                with self.assertRaisesRegex(prepare.PreparationError, "committer timestamp"):
                    self.discover()

    def test_backward_and_divergent_history_are_rejected(self):
        for status in ("behind", "diverged", "identical"):
            with self.subTest(status=status):
                self.responses[self.compare_path]["status"] = status
                with self.assertRaisesRegex(prepare.PreparationError, "forward history"):
                    self.discover()
        self.responses[self.compare_path]["status"] = "ahead"
        self.responses[self.compare_path]["merge_base_commit"]["sha"] = OLD
        with self.assertRaisesRegex(prepare.PreparationError, "forward history"):
            self.discover()

    def test_candidate_filename_collision(self):
        self.responses[self.commit_path]["committer"]["date"] = "2026-11-24T20:37:31Z"
        with self.assertRaisesRegex(prepare.PreparationError, "collision"):
            self.discover()

    def test_non_forward_version_is_rejected_and_version_parts_sort_numerically(self):
        self.responses[self.version_path] = version_file(b"0.0.9")
        with self.assertRaisesRegex(prepare.PreparationError, "newer"):
            self.discover()
        (self.package / CURRENT_NAME).rename(self.package / CURRENT_NAME.replace("0.1.0", "0.9.0"))
        self.responses[self.version_path] = version_file(b"0.10.0")
        self.assertEqual(self.discover().filename, CANDIDATE_NAME.replace("0.1.0", "0.10.0"))

    def test_missing_malformed_and_excess_ebuilds_are_rejected_before_http(self):
        current = self.package / CURRENT_NAME
        current.unlink()
        with self.assertRaisesRegex(prepare.PreparationError, "one or two"):
            self.discover()
        for name in ("luciole-9999.ebuild", "luciole-0.1.0_pre20261324203731.ebuild"):
            path = self.package / name
            path.write_bytes(EBUILD)
            with self.assertRaisesRegex(prepare.PreparationError, "snapshot"):
                self.discover()
            path.unlink()
        current.write_bytes(EBUILD)
        self.add_previous()
        (self.package / CANDIDATE_NAME).write_bytes(EBUILD)
        with self.assertRaisesRegex(prepare.PreparationError, "one or two"):
            self.discover()
        self.http.assert_not_called()

    def test_unexpected_assignments_and_duplicate_snapshot_shas_are_rejected(self):
        path = self.package / CURRENT_NAME
        for content in (
            EBUILD.replace(b'MY_COMMIT="', b'OTHER="', 1),
            EBUILD.replace(CURRENT.encode(), b"abc", 1),
            EBUILD + b'\nMY_COMMIT+="extra"\n',
            EBUILD + b'\nMY_COMMIT="${OTHER}"\n',
        ):
            with self.subTest(content=content):
                path.write_bytes(content)
                with self.assertRaisesRegex(prepare.PreparationError, "MY_COMMIT"):
                    self.discover()
        path.write_bytes(EBUILD)
        (self.package / OLD_NAME).write_bytes(EBUILD)
        with self.assertRaisesRegex(prepare.PreparationError, "duplicate SHA"):
            self.discover()
        self.http.assert_not_called()

    def test_stage_parent_must_be_outside_checkout(self):
        code, _, errors = self.run_cli("--stage-dir", str(self.root))
        self.assertEqual(code, 1)
        self.assertIn("outside", errors)
        self.http.assert_not_called()
