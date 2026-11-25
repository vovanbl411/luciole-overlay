from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import io
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from scripts import gentoo_validate as gentoo
from scripts import prepare_luciole as prepare
from scripts import publish_luciole as publish
from scripts import validate_luciole as validation
from test_validate_luciole import CURRENT, CURRENT_VERSION, NEW, NEW_VERSION, OLD, OLD_VERSION, ebuild, record


BRANCH = f"automation/luciole-{NEW[:12]}"
BRANCH_REF = f"refs/heads/{BRANCH}"


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True).stdout


def pr(head=BRANCH, repository=prepare.OVERLAY, base="main"):
    return {
        "number": 17,
        "state": "open",
        "head": {"ref": head, "repo": {"full_name": repository}},
        "base": {"ref": base, "repo": {"full_name": prepare.OVERLAY}},
    }


class PrepareActionsTest(unittest.TestCase):
    def setUp(self):
        workspace = Path(self.enterContext(TemporaryDirectory()))
        self.root = workspace / "overlay"
        package = self.root / prepare.PACKAGE
        package.mkdir(parents=True)
        self.parent = workspace / "stage"
        self.parent.mkdir()
        self.output = workspace / "output"
        (self.root / ebuild(CURRENT_VERSION)).write_bytes(b'MY_COMMIT="' + CURRENT.encode() + b'"\n')
        self.snapshot, = prepare.read_snapshots(package)
        self.plan = prepare.Discovery(
            self.snapshot, None, NEW, "0.1.0", ebuild(NEW_VERSION).name,
            datetime(2026, 11, 25, 13, 48, 57, tzinfo=timezone.utc),
            {"meson.build": "modified"},
        )
        self.request = self.enterContext(patch.object(prepare, "overlay_request", return_value=[]))
        self.discover = self.enterContext(patch.object(prepare, "discover", return_value=self.plan))
        self.stdout = self.enterContext(redirect_stdout(io.StringIO()))
        self.stderr = self.enterContext(redirect_stderr(io.StringIO()))

    def run_cli(self, *arguments):
        return prepare.main([*arguments, "--github-output", str(self.output)], root=self.root)

    def outputs(self):
        return dict(line.split("=", 1) for line in self.output.read_text().splitlines())

    def test_no_upstream_update(self):
        self.discover.return_value = self.snapshot
        self.assertEqual(self.run_cli("--stage-dir", str(self.parent)), 0)
        self.assertEqual(self.outputs()["status"], "up_to_date")
        self.assertEqual(self.outputs()["has_candidate"], "false")
        self.assertEqual(list(self.parent.iterdir()), [])

    def test_existing_automated_pr_skips_discovery(self):
        self.request.return_value = [pr()]
        self.assertEqual(self.run_cli("--stage-dir", str(self.parent)), 0)
        self.assertEqual(self.outputs()["status"], "open_pr")
        self.assertEqual(self.outputs()["open_pr"], "17")
        self.discover.assert_not_called()

    def test_unrelated_prs_do_not_block_machine_readable_preparation(self):
        self.request.return_value = [
            pr(head="owner/fix"), pr(repository="fork/luciole-overlay"),
            pr(base="other"), pr(head="automation/luciole-invalid"),
        ]
        self.assertEqual(self.run_cli("--stage-dir", str(self.parent)), 0)
        outputs = self.outputs()
        stage, = self.parent.iterdir()
        self.assertEqual(outputs, {
            "status": "staged", "has_candidate": "true", "candidate_sha": NEW,
            "candidate_version": NEW_VERSION, "stage_dir": str(stage), "open_pr": "",
        })
        self.assertTrue((stage / ebuild(NEW_VERSION)).is_file())

    def test_dry_run_reports_open_pr_and_remains_read_only(self):
        self.request.return_value = [pr()]
        original = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(self.run_cli("--dry-run"), 0)
        self.assertEqual(self.outputs()["status"], "dry_run")
        self.assertEqual(self.outputs()["stage_dir"], "")
        self.assertEqual(self.outputs()["open_pr"], "17")
        self.assertIn(CURRENT, self.stdout.getvalue())
        self.assertIn(NEW, self.stdout.getvalue())
        self.assertIn("meson.build", self.stdout.getvalue())
        self.assertEqual(list(self.parent.iterdir()), [])
        self.assertEqual({path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}, original)

    def test_api_failure_yields_no_success_outputs_or_stage(self):
        self.request.side_effect = prepare.PreparationError("GitHub API pulls failed with HTTP 503.")
        self.assertEqual(self.run_cli("--dry-run"), 1)
        self.assertIn("HTTP 503", self.stderr.getvalue())
        self.assertFalse(self.output.exists())
        self.discover.assert_not_called()

    def test_malformed_and_paginated_open_pr_responses(self):
        self.request.return_value = [{}]
        with self.assertRaisesRegex(prepare.PreparationError, "Malformed"):
            prepare.open_snapshot_pr()
        self.request.side_effect = [[pr(head="owner/fix")] * 100, [pr()]]
        self.assertEqual(prepare.open_snapshot_pr(), 17)
        self.assertIn("page=2", self.request.call_args.args[0])


class GitHubRequestTest(unittest.TestCase):
    def test_auth_is_scoped_to_the_initial_request(self):
        with patch.dict(prepare.os.environ, {"GITHUB_TOKEN": "test-token"}):
            with patch.object(prepare, "urlopen", return_value=io.BytesIO(b"[]")) as urlopen:
                self.assertIsNone(prepare.open_snapshot_pr())
                request = urlopen.call_args.args[0]
                self.assertEqual(request.unredirected_hdrs["Authorization"], "Bearer test-token")
                self.assertNotIn("Authorization", request.headers)
            error = HTTPError("https://api.github.com/", 403, "forbidden", None, None)
            with patch.object(prepare, "urlopen", side_effect=error):
                with self.assertRaisesRegex(prepare.PreparationError, "HTTP 403"):
                    prepare.open_snapshot_pr()


class PublishTest(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}))
        self.workspace = Path(self.enterContext(TemporaryDirectory()))
        self.root = self.workspace / "overlay"
        self.root.mkdir()
        self.remote = self.workspace / "remote.git"
        self.artifact = self.workspace / "handoff"
        self.stdout = self.enterContext(redirect_stdout(io.StringIO()))
        self.open_pr = self.enterContext(patch.object(prepare, "open_snapshot_pr", return_value=None))
        self.request = self.enterContext(patch.object(prepare, "overlay_request", return_value={"number": 20, "draft": True}))

    def fixture(self, *, rotation=False):
        git(self.root, "init", "--initial-branch=main")
        git(self.root, "config", "user.name", "Fixture")
        git(self.root, "config", "user.email", "fixture@example.org")
        self.retained = b'EAPI=8\nMY_COMMIT="' + CURRENT.encode() + b'"\n' + gentoo.SOURCE + b"\n"
        self.current_record = record(CURRENT, b"accepted")
        accepted = {
            ebuild(CURRENT_VERSION): self.retained,
            validation.MANIFEST: self.current_record,
            validation.PACKAGE_METADATA: b"<pkgmetadata/>\n",
            Path("metadata/layout.conf"): b"masters = gentoo\nthin-manifests = true\n",
            Path("profiles/repo_name"): b"luciole-overlay\n",
            Path("README.md"): b"Owner documentation\n",
        }
        if rotation:
            accepted[ebuild(OLD_VERSION)] = self.retained.replace(CURRENT.encode(), OLD.encode())
            accepted[validation.MANIFEST] += record(OLD, b"obsolete")
        for path, content in accepted.items():
            target = self.root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        git(self.root, "add", ".")
        git(self.root, "commit", "-m", "accepted fixture")
        self.base, snapshots, _ = validation.accepted_base(self.root)
        git(self.workspace, "init", "--bare", "--initial-branch=main", str(self.remote))
        git(self.root, "push", str(self.remote), "main")
        receipt = {
            "candidate": {"version": NEW_VERSION, "sha": NEW},
            "committed_at": "2026-11-25T13:48:57+00:00",
            "packaging_changes": dict.fromkeys(prepare.PACKAGING_FILES, "unchanged") | {"meson.build": "modified"},
        }
        self.transferred = {
            **validation.expected_ebuilds(snapshots, receipt),
            validation.MANIFEST: self.current_record + record(NEW, b"candidate"),
        }
        self.metadata = validation.handoff_metadata(self.base, snapshots, receipt, self.transferred)
        for path, content in self.transferred.items():
            target = self.artifact / "overlay" / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        validation.write_json(self.artifact / "handoff.json", self.metadata)

    def publish(self, root=None):
        return publish.publish(root or self.root, self.artifact, remote=str(self.remote))

    def clone(self):
        clone = self.workspace / "retry"
        git(self.workspace, "clone", "--quiet", str(self.remote), str(clone))
        return clone

    def branch_head(self):
        return git(self.remote, "rev-parse", BRANCH_REF).decode().strip()

    def test_apply_exact_rotation_and_preserve_retained_ebuild(self):
        self.fixture(rotation=True)
        git(self.root, "config", "diff.renames", "true")
        metadata = publish.apply_handoff(self.root, self.artifact)
        self.assertEqual(metadata, self.metadata)
        self.assertEqual((self.root / ebuild(CURRENT_VERSION)).read_bytes(), self.retained)
        self.assertFalse((self.root / ebuild(OLD_VERSION)).exists())
        self.assertEqual((self.root / ebuild(NEW_VERSION)).read_bytes(), self.transferred[ebuild(NEW_VERSION)])
        self.assertEqual((self.root / validation.MANIFEST).read_bytes(), self.transferred[validation.MANIFEST])

    def test_stale_handoff_rejected_without_staging(self):
        self.fixture()
        git(self.root, "commit", "--allow-empty", "-m", "advance main")
        with self.assertRaisesRegex(gentoo.ValidationError, "Handoff metadata"):
            publish.apply_handoff(self.root, self.artifact)
        self.assertEqual(git(self.root, "status", "--porcelain"), b"")

    def test_unexpected_or_empty_diff_rejected(self):
        self.fixture()
        with self.assertRaisesRegex(publish.PublicationError, "Staged Git diff"):
            publish.verify_diff(self.root, self.metadata)
        publish.apply_handoff(self.root, self.artifact)
        (self.root / "README.md").write_text("Unexpected edit\n")
        git(self.root, "add", "README.md")
        with self.assertRaisesRegex(publish.PublicationError, "Staged Git diff"):
            publish.verify_diff(self.root, self.metadata)

    def test_existing_identical_branch_is_reused_after_pr_failure(self):
        self.fixture()
        self.request.side_effect = prepare.PreparationError("GitHub API pulls failed with HTTP 503.")
        with self.assertRaisesRegex(publish.PublicationError, "later run can reuse"):
            self.publish()
        head = self.branch_head()
        self.assertEqual(git(self.remote, "rev-parse", f"{head}^").decode().strip(), self.base)
        self.request.side_effect = None
        self.publish(self.clone())
        self.assertEqual(self.branch_head(), head)
        self.assertIn(f"Reusing verified branch {BRANCH}", self.stdout.getvalue())
        self.assertEqual(self.request.call_count, 2)

    def test_conflicting_branch_with_correct_parent_is_rejected(self):
        self.fixture()
        git(self.root, "commit", "--allow-empty", "-m", "different tree fixture")
        git(self.root, "push", str(self.remote), f"HEAD:{BRANCH_REF}")
        original = self.branch_head()
        with self.assertRaisesRegex(publish.PublicationError, "different parent or tree"):
            self.publish(self.clone())
        self.assertEqual(self.branch_head(), original)
        self.request.assert_not_called()

    def test_remote_main_advance_prevents_any_publication(self):
        self.fixture()
        another = self.clone()
        git(another, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.org",
            "commit", "--allow-empty", "-m", "advance main")
        git(another, "push", str(self.remote), "main")
        with self.assertRaisesRegex(publish.PublicationError, "Remote main changed"):
            self.publish()
        self.assertEqual(git(self.remote, "branch", "--list", BRANCH), b"")
        self.request.assert_not_called()

    def test_normal_push_refuses_to_fast_forward_an_existing_branch(self):
        self.fixture()
        git(self.root, "push", str(self.remote), f"HEAD:{BRANCH_REF}")
        git(self.root, "commit", "--allow-empty", "-m", "can fast-forward fixture")
        head = git(self.root, "rev-parse", "HEAD").decode().strip()
        with self.assertRaises(publish.PublicationError):
            publish.push_new_branch(self.root, str(self.remote), head, BRANCH_REF, None)
        self.assertEqual(self.branch_head(), self.base)

    def test_open_pr_prevents_branch_creation(self):
        self.fixture()
        self.open_pr.return_value = 17
        self.publish()
        self.assertEqual(git(self.remote, "branch", "--list", BRANCH), b"")
        self.assertEqual(git(self.root, "status", "--porcelain"), b"")
        self.request.assert_not_called()

    def test_pr_request_contains_review_details_and_draft_target(self):
        self.fixture(rotation=True)
        result = publish.pull_request(self.metadata, BRANCH)
        self.assertEqual(result["head"], BRANCH)
        self.assertEqual(result["base"], "main")
        self.assertIs(result["draft"], True)
        self.assertEqual(result["title"], f"[snapshot-update] Luciole {NEW_VERSION}")
        for expected in (CURRENT, NEW, CURRENT_VERSION, NEW_VERSION, OLD_VERSION,
                         self.metadata["compare_url"], 'modified "meson.build"', "Compilation", "runtime testing"):
            self.assertIn(expected, result["body"])
