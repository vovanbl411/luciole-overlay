from contextlib import redirect_stdout
from datetime import datetime, timezone
import hashlib
from io import StringIO
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from scripts import gentoo_validate as gentoo
from scripts import prepare_luciole as prepare
from scripts import validate_luciole as validation


BASE, OLD, CURRENT, NEW = (character * 40 for character in "1234")
OLD_VERSION = "0.1.0_pre20261123203731"
CURRENT_VERSION = "0.1.0_pre20261124203731"
NEW_VERSION = "0.1.0_pre20261125134857"


def ebuild(version):
    return validation.PACKAGE / f"luciole-{version}.ebuild"


def record(sha, archive):
    return (
        f"DIST luciole-{sha}.tar.gz {len(archive)} BLAKE2B {hashlib.blake2b(archive).hexdigest()} "
        f"SHA512 {hashlib.sha512(archive).hexdigest()}\n"
    ).encode()


class HandoffTest(unittest.TestCase):
    def setUp(self):
        self.workspace = Path(self.enterContext(TemporaryDirectory()))
        self.accepted = self.workspace / "accepted"
        self.staging = self.workspace / "staging"
        (self.accepted / validation.PACKAGE).mkdir(parents=True)
        self.staging.mkdir()
        self.old_record = record(OLD, b"old archive")
        self.current_record = record(CURRENT, b"current archive")
        self.new_record = record(NEW, b"new archive")
        self.generated = self.current_record + self.new_record
        self.base = {
            ebuild(CURRENT_VERSION): b'EAPI=8\nMY_COMMIT="' + CURRENT.encode() + b'"\n' + gentoo.SOURCE + b"\n",
            validation.MANIFEST: self.current_record,
            validation.PACKAGE_METADATA: b"<pkgmetadata/>\n",
            Path("metadata/layout.conf"): b"masters = gentoo\nthin-manifests = true\n",
            Path("profiles/repo_name"): b"luciole-overlay\n",
        }

    def make_stage(self, *, rotation=False):
        if rotation:
            self.base[ebuild(OLD_VERSION)] = self.base[ebuild(CURRENT_VERSION)].replace(CURRENT.encode(), OLD.encode())
            self.base[validation.MANIFEST] = self.old_record + self.current_record
        for path, content in self.base.items():
            target = self.accepted / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        self.snapshots = prepare.read_snapshots(self.accepted / validation.PACKAGE)
        self.plan = prepare.Discovery(
            self.snapshots[-1], self.snapshots[0] if rotation else None, NEW, "0.1.0",
            ebuild(NEW_VERSION).name, datetime(2026, 11, 25, 13, 48, 57, tzinfo=timezone.utc),
            dict.fromkeys(prepare.PACKAGING_FILES, "unchanged"),
        )
        self.stage = prepare.stage(self.accepted / validation.PACKAGE, self.staging, self.plan)
        self.docker = self.enterContext(patch.object(validation, "run_container", side_effect=self.container))
        self.enterContext(patch.object(validation, "accepted_base", return_value=(BASE, self.snapshots, self.base)))

    def container(self, _work, repository, result, _snapshots, _receipt):
        self.assertEqual({path.name for path in (repository / validation.PACKAGE).glob("*.ebuild")},
                         {ebuild(CURRENT_VERSION).name, ebuild(NEW_VERSION).name})
        self.assertEqual((repository / validation.MANIFEST).read_bytes(), self.base[validation.MANIFEST])
        (result / "Manifest").write_bytes(self.generated)

    def verify(self, artifact):
        validation.verify_handoff(artifact, BASE, self.snapshots, self.base)

    def test_successful_validation_produces_restricted_checked_handoff(self):
        self.make_stage()
        artifact = validation.validate_stage(self.stage, self.accepted)
        self.verify(artifact)
        self.assertEqual(
            {path.relative_to(artifact).as_posix() for path in artifact.rglob("*") if path.is_file()},
            {"handoff.json", *(f"overlay/{path}" for path in
                              (ebuild(CURRENT_VERSION), ebuild(NEW_VERSION), validation.MANIFEST))},
        )
        metadata = validation.read_json(artifact / "handoff.json")
        self.assertEqual(metadata["base_commit"], BASE)
        self.assertEqual(metadata["previous"], {"version": CURRENT_VERSION, "sha": CURRENT})
        self.assertEqual(metadata["candidate"], {"version": NEW_VERSION, "sha": NEW})
        self.assertEqual(metadata["expected_diff"]["delete"], [])
        self.assertEqual((self.stage / validation.MANIFEST).read_bytes(), self.generated)
        self.assertFalse((self.stage / "INCOMPLETE.txt").exists())
        self.assertEqual({path: (self.accepted / path).read_bytes() for path in self.base}, self.base)

    def test_rotation_records_deletion_and_drops_obsolete_distfile(self):
        self.make_stage(rotation=True)
        artifact = validation.validate_stage(self.stage, self.accepted)
        metadata = validation.read_json(artifact / "handoff.json")
        self.assertEqual(metadata["expected_diff"]["delete"], [str(ebuild(OLD_VERSION))])
        self.assertFalse((artifact / "overlay" / ebuild(OLD_VERSION)).exists())
        self.assertNotIn(self.old_record, (artifact / "overlay" / validation.MANIFEST).read_bytes())
        self.assertEqual((self.accepted / validation.MANIFEST).read_bytes(), self.old_record + self.current_record)

    def test_staging_rejects_ebuild_content_changes_and_unexpected_inventory(self):
        self.make_stage()
        staged = self.stage / ebuild(NEW_VERSION)
        original = staged.read_bytes()
        staged.write_bytes(original + b"# not the controlled replacement\n")
        with self.assertRaisesRegex(gentoo.ValidationError, "expected rotation"):
            validation.validate_stage(self.stage, self.accepted)
        staged.write_bytes(original)
        (self.stage / ebuild(OLD_VERSION)).write_bytes(original)
        with self.assertRaisesRegex(gentoo.ValidationError, "inventory"):
            validation.validate_stage(self.stage, self.accepted)
        self.docker.assert_not_called()

    def test_container_failure_produces_no_handoff_and_preserves_incomplete_stage(self):
        self.make_stage()
        self.docker.side_effect = subprocess.CalledProcessError(1, ["docker", "run"])
        with self.assertRaises(subprocess.CalledProcessError):
            validation.validate_stage(self.stage, self.accepted)
        self.assertTrue((self.stage / "INCOMPLETE.txt").is_file())
        self.assertFalse((self.stage / "handoff").exists())
        self.assertEqual((self.stage / validation.MANIFEST).read_bytes(), self.current_record)

    def test_manifest_integrity_failure_prevents_handoff_even_after_container_success(self):
        self.make_stage(rotation=True)
        for invalid in (
            self.current_record,
            self.generated + self.old_record,
            self.generated + self.current_record,
            record(CURRENT, b"CURRENT ARCHIVE") + self.new_record,
        ):
            with self.subTest(invalid=invalid):
                self.generated = invalid
                with self.assertRaises(gentoo.ValidationError):
                    validation.validate_stage(self.stage, self.accepted)
                self.assertFalse((self.stage / "handoff").exists())
                self.assertTrue((self.stage / "INCOMPLETE.txt").exists())
                self.assertEqual((self.accepted / validation.MANIFEST).read_bytes(),
                                 self.old_record + self.current_record)

    def test_handoff_checksum_and_base_must_match(self):
        self.make_stage()
        artifact = validation.validate_stage(self.stage, self.accepted)
        with self.assertRaisesRegex(gentoo.ValidationError, "checksums"):
            validation.verify_handoff(artifact, NEW, self.snapshots, self.base)
        (artifact / "overlay" / validation.MANIFEST).write_bytes(self.generated + b"\n")
        with self.assertRaisesRegex(gentoo.ValidationError, "checksums"):
            self.verify(artifact)

    def test_handoff_rejects_unexpected_entries_symlinks_and_executables(self):
        self.make_stage()
        artifact = validation.validate_stage(self.stage, self.accepted)
        excess = artifact / "workflow.yml"
        excess.touch()
        with self.assertRaisesRegex(gentoo.ValidationError, "inventory"):
            self.verify(artifact)
        excess.unlink()
        path = artifact / "overlay" / ebuild(CURRENT_VERSION)
        path.unlink()
        path.symlink_to(self.accepted / ebuild(CURRENT_VERSION))
        with self.assertRaisesRegex(gentoo.ValidationError, "regular"):
            self.verify(artifact)
        path.unlink()
        path.write_bytes(self.base[ebuild(CURRENT_VERSION)])
        path.chmod(0o755)
        with self.assertRaisesRegex(gentoo.ValidationError, "non-executable"):
            self.verify(artifact)

    def test_handoff_rejects_path_traversal_in_metadata(self):
        self.make_stage()
        artifact = validation.validate_stage(self.stage, self.accepted)
        metadata_path = artifact / "handoff.json"
        data = validation.read_json(metadata_path)
        data["expected_diff"]["delete"] = ["../outside.ebuild"]
        validation.write_json(metadata_path, data)
        with self.assertRaisesRegex(gentoo.ValidationError, "expected diff"):
            self.verify(artifact)
        data["candidate"]["version"] = "../outside"
        validation.write_json(metadata_path, data)
        with self.assertRaisesRegex(gentoo.ValidationError, "candidate version"):
            self.verify(artifact)


class GentooTest(unittest.TestCase):
    def test_version_ordering_uses_portage_result(self):
        vercmp = Mock(return_value=1)
        gentoo.require_newer(NEW_VERSION, CURRENT_VERSION, vercmp)
        vercmp.assert_called_once_with(NEW_VERSION, CURRENT_VERSION)
        for order in (0, -1, None):
            with self.subTest(order=order), self.assertRaisesRegex(gentoo.ValidationError, "vercmp"):
                vercmp.return_value = order
                gentoo.require_newer(NEW_VERSION, CURRENT_VERSION, vercmp)

    def test_candidate_only_keyword_acceptance(self):
        atom = f"=gui-apps/luciole-{NEW_VERSION}::luciole-overlay"
        self.assertEqual(gentoo.candidate_keywords(atom, "amd64", {"amd64"}), f"{atom} ~amd64\n")
        self.assertEqual(gentoo.candidate_keywords(atom, "amd64", {"amd64", "~amd64"}), "")
        with self.assertRaisesRegex(gentoo.ValidationError, "incompatible"):
            gentoo.candidate_keywords(atom, "arm64", {"arm64"})

    def test_manifest_requires_exact_sizes_and_checksum_fields(self):
        valid = record(CURRENT, b"data")
        alias = f"luciole-{CURRENT}.tar.gz"
        self.assertEqual(gentoo.parse_manifest(valid, {alias})[alias][0], 4)
        for invalid in (
            valid.replace(b" 4 ", b" 0 "), valid.replace(b"SHA512", b"BLAKE2B"),
            valid.replace(b"SHA512 ", b"SHA512 g"), valid.replace(b"DIST", b"EBUILD"),
        ):
            with self.subTest(invalid=invalid), self.assertRaises(gentoo.ValidationError):
                gentoo.parse_manifest(invalid, {alias})

    def test_verifies_the_bytes_of_each_archive(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        name = f"luciole-{CURRENT}.tar.gz"
        archive = directory / name
        records = gentoo.parse_manifest(record(CURRENT, b"data"), {name})
        with self.assertRaisesRegex(gentoo.ValidationError, "Missing archive"):
            gentoo.verify_archives(directory, records)
        archive.write_bytes(b"data")
        gentoo.verify_archives(directory, records)
        archive.write_bytes(b"DATA")
        with self.assertRaisesRegex(gentoo.ValidationError, "checksum mismatch"):
            gentoo.verify_archives(directory, records)

    def test_container_exports_manifest_only_after_all_commands_succeed(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        (directory / gentoo.PACKAGE).mkdir(parents=True)
        output = directory / "result"
        output.mkdir()
        archives = directory / "distfiles"
        archives.mkdir()
        for version, sha in ((CURRENT_VERSION, CURRENT), (NEW_VERSION, NEW)):
            (directory / ebuild(version)).write_bytes(
                b'MY_COMMIT="' + sha.encode() + b'"\n' + gentoo.SOURCE + b"\n",
            )
            (archives / f"luciole-{sha}.tar.gz").write_bytes(sha.encode())
        manifest = directory / validation.MANIFEST
        retained, added = record(CURRENT, CURRENT.encode()), record(NEW, NEW.encode())
        portage = Mock(settings={"ARCH": "amd64", "ACCEPT_KEYWORDS": "amd64"})
        vercmp = Mock(return_value=1)
        self.enterContext(redirect_stdout(StringIO()))
        self.enterContext(patch.object(gentoo, "DISTDIR", archives))
        self.enterContext(patch.object(gentoo.shutil, "which", return_value="/usr/bin/pkgcheck"))
        settings = self.enterContext(patch.object(gentoo, "write_config"))
        command = self.enterContext(patch.object(gentoo, "run"))

        for failure in ("emerge", "pkgcheck", None):
            with self.subTest(failure=failure):
                manifest.write_bytes(retained)

                def run(program, *args, **kwargs):
                    if args[-1] == "manifest":
                        manifest.write_bytes(retained + added)
                    if program == failure:
                        raise subprocess.CalledProcessError(1, (program, *args))

                command.side_effect = run
                arguments = (directory, output, NEW_VERSION, CURRENT_VERSION, NEW, CURRENT, "")
                if failure:
                    with self.assertRaises(subprocess.CalledProcessError):
                        gentoo.validate(*arguments, portage=portage, vercmp=vercmp)
                    self.assertEqual(list(output.iterdir()), [])
                else:
                    gentoo.validate(*arguments, portage=portage, vercmp=vercmp)
                    self.assertEqual((output / "Manifest").read_bytes(), retained + added)
        atom = f"=gui-apps/luciole-{NEW_VERSION}::luciole-overlay"
        self.assertIn(f"{atom} -test\n", settings.call_args.args[1])
        command.assert_any_call("emerge", "-pv", "--with-bdeps=y", "--autounmask=n", atom)
        command.assert_any_call("pkgcheck", "scan", "gui-apps/luciole", "--exit", cwd=directory)
