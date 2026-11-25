"""Validate a prepared snapshot and verify its restricted publication handoff."""

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory

if __package__:
    from . import gentoo_validate as gentoo, prepare_luciole as prepare
else:
    import gentoo_validate as gentoo
    import prepare_luciole as prepare


PACKAGE = prepare.PACKAGE
MANIFEST = PACKAGE / "Manifest"
PACKAGE_METADATA = PACKAGE / "metadata.xml"
REPO_FILES = (Path("metadata/layout.conf"), Path("profiles/repo_name"))
PORTAGE = "gentoo/portage@sha256:ce69e5b771bfe9404731e846b835346c34f2c82817ac3626f13af198689a1961"
STAGE3 = "gentoo/stage3@sha256:d8536e52677e3efe4caf048ca588bc685bc2d24d7fdfc882de1cdc6d38c81d96"
VALIDATION = {
    "platform": "linux/amd64",
    "portage_image": PORTAGE,
    "stage3_image": STAGE3,
    "passed": ["portage-vercmp", "portage-manifest", "both-distfiles", "pkgcheck", "emerge-default-no-test"],
    "dependency_keywords": list(gentoo.RESOLVER_KEYWORDS),
    "dependency_use": list(gentoo.RESOLVER_USE),
}
CONTAINER_SETUP = """set -eu
mkdir -p /overlay /etc/portage/repos.conf
cp -R /input/. /overlay/
printf '[gentoo]\nlocation = /var/db/repos/gentoo\nauto-sync = no\n' > /etc/portage/repos.conf/gentoo.conf
printf '[luciole-overlay]\nlocation = /overlay\nmasters = gentoo\nauto-sync = no\n' > /etc/portage/repos.conf/luciole-overlay.conf
exec python3 -B /gentoo_validate.py "$@"
"""
MAX_FILE_SIZE = 1024 * 1024


def read_file(path: Path) -> bytes:
    mode = path.lstat().st_mode
    if not stat.S_ISREG(mode) or mode & 0o111 or path.stat().st_size > MAX_FILE_SIZE:
        raise gentoo.ValidationError(f"Expected a regular non-executable file at {path}.")
    return path.read_bytes()


def check_inventory(directory: Path, files: set[Path]) -> None:
    if directory.is_symlink() or not directory.is_dir():
        raise gentoo.ValidationError(f"Expected a regular directory at {directory}.")
    actual = {item.name for item in directory.iterdir()}
    expected = {path.parts[0] for path in files}
    if actual != expected:
        raise gentoo.ValidationError(
            f"Unexpected inventory in {directory}: missing={sorted(expected - actual)}, "
            f"unexpected={sorted(actual - expected)}."
        )
    for name in sorted(expected):
        children = {Path(*path.parts[1:]) for path in files if path.parts[0] == name and len(path.parts) > 1}
        if children:
            check_inventory(directory / name, children)
        else:
            read_file(directory / name)


def read_json(path: Path) -> dict:
    def unique_object(items):
        value = dict(items)
        if len(value) != len(items):
            raise gentoo.ValidationError(f"Duplicate JSON member in {path.name}.")
        return value

    data = json.loads(read_file(path), object_pairs_hook=unique_object)
    if not isinstance(data, dict):
        raise gentoo.ValidationError(f"Expected a JSON object in {path.name}.")
    return data


def write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def snapshot_info(snapshot: prepare.Snapshot) -> dict:
    return {"version": snapshot.filename.removeprefix("luciole-").removesuffix(".ebuild"), "sha": snapshot.sha}


def candidate_name(data: object) -> str:
    if not isinstance(data, dict) or data.keys() != {"version", "sha"}:
        raise gentoo.ValidationError("Invalid candidate identity.")
    version, sha = data["version"], data["sha"]
    filename = f"luciole-{version}.ebuild"
    if not isinstance(version, str) or not prepare.SNAPSHOT.fullmatch(filename) or not prepare.is_sha(sha):
        raise gentoo.ValidationError("Invalid candidate version or SHA.")
    return filename


def validate_receipt(receipt: dict) -> str:
    if receipt.keys() != {"candidate", "committed_at", "packaging_changes"}:
        raise gentoo.ValidationError("Unexpected discovery metadata fields.")
    filename = candidate_name(receipt["candidate"])
    timestamp = receipt["committed_at"]
    if not isinstance(timestamp, str):
        raise gentoo.ValidationError("Invalid candidate committer timestamp.")
    date = datetime.fromisoformat(timestamp)
    if (
        date.tzinfo != timezone.utc or date.microsecond or date.isoformat() != timestamp
        or not filename.endswith(f"_pre{date.year:04d}{date:%m%d%H%M%S}.ebuild")
    ):
        raise gentoo.ValidationError("Candidate committer timestamp does not match its version.")
    changes = receipt["packaging_changes"]
    if not isinstance(changes, dict) or not prepare.PACKAGING_FILES.issubset(changes):
        raise gentoo.ValidationError("Missing packaging-file change summary.")
    for path, status in changes.items():
        if (
            not all(part and part not in {".", ".."} for part in path.split("/"))
            or "\\" in path or any(ord(character) < 32 for character in path)
            or (path not in prepare.PACKAGING_FILES and not prepare.LICENSE_NAME.search(path))
            or status not in ("absent", "added", "modified", "removed", "unchanged")
        ):
            raise gentoo.ValidationError("Invalid packaging-file change summary.")
    return filename


def git_output(root: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", *args], cwd=root)


def accepted_base(root: Path) -> tuple[str, list[prepare.Snapshot], dict[Path, bytes]]:
    commit = git_output(root, "rev-parse", "HEAD").decode("ascii").strip()
    if not prepare.is_sha(commit):
        raise gentoo.ValidationError("Expected the full base overlay Git SHA.")
    snapshots = prepare.read_snapshots(root / PACKAGE)
    paths = {PACKAGE / snapshot.filename for snapshot in snapshots} | {MANIFEST, PACKAGE_METADATA}
    check_inventory(root / PACKAGE, {path.relative_to(PACKAGE) for path in paths})
    listed = git_output(root, "ls-tree", "-rz", "--name-only", commit, "--", str(PACKAGE))
    if set(listed.decode().rstrip("\0").split("\0")) != {path.as_posix() for path in paths}:
        raise gentoo.ValidationError("Accepted package inventory differs from the base overlay commit.")
    data = {path: git_output(root, "show", f"{commit}:{path.as_posix()}") for path in (*paths, *REPO_FILES)}
    if any(read_file(root / path) != content for path, content in data.items()):
        raise gentoo.ValidationError("Accepted package or repository metadata differs from the base overlay commit.")
    if any(snapshot.content != data[PACKAGE / snapshot.filename] for snapshot in snapshots):
        raise gentoo.ValidationError("Accepted ebuilds changed while reading the base overlay.")
    return commit, snapshots, data


def expected_ebuilds(snapshots: list[prepare.Snapshot], receipt: dict) -> dict[Path, bytes]:
    filename = validate_receipt(receipt)
    candidate_sha = receipt["candidate"]["sha"]
    if any(filename == snapshot.filename or candidate_sha == snapshot.sha for snapshot in snapshots):
        raise gentoo.ValidationError("Candidate overlaps an accepted snapshot.")
    retained = snapshots[-1]
    candidate = prepare.PIN.sub(b'MY_COMMIT="' + candidate_sha.encode("ascii") + b'"', retained.content)
    return {PACKAGE / retained.filename: retained.content, PACKAGE / filename: candidate}


def verify_manifest(base: dict[Path, bytes], snapshots: list[prepare.Snapshot],
                    receipt: dict, content: bytes) -> None:
    retained = snapshots[-1]
    aliases = {gentoo.distfile_alias(snapshot.content, snapshot.sha) for snapshot in snapshots}
    before = gentoo.parse_manifest(base[MANIFEST], aliases)
    retained_alias = gentoo.distfile_alias(retained.content, retained.sha)
    candidate_alias = gentoo.distfile_alias(retained.content, receipt["candidate"]["sha"])
    after = gentoo.parse_manifest(content, {retained_alias, candidate_alias})
    gentoo.preserve_distfile(before, after, retained_alias)


def handoff_metadata(commit: str, snapshots: list[prepare.Snapshot],
                     receipt: dict, transferred: dict[Path, bytes]) -> dict:
    previous = snapshot_info(snapshots[-1])
    candidate = receipt["candidate"]
    dropped = snapshots[0] if len(snapshots) == 2 else None
    return {
        **receipt,
        "schema_version": 1,
        "base_commit": commit,
        "previous": previous,
        "compare_url": f"{prepare.UPSTREAM}/compare/{previous['sha']}...{candidate['sha']}",
        "validation": VALIDATION,
        "expected_diff": {
            "add": [(PACKAGE / candidate_name(candidate)).as_posix()],
            "modify": [MANIFEST.as_posix()],
            "delete": [(PACKAGE / dropped.filename).as_posix()] if dropped else [],
        },
        "sha256": {path.as_posix(): hashlib.sha256(content).hexdigest() for path, content in transferred.items()},
    }


def verify_handoff(artifact: Path, commit: str, snapshots: list[prepare.Snapshot], base: dict[Path, bytes]) -> dict:
    if artifact.is_symlink() or not artifact.is_dir():
        raise gentoo.ValidationError("Expected a regular handoff directory.")
    metadata = read_json(artifact / "handoff.json")
    if type(metadata.get("schema_version")) is not int or metadata["schema_version"] != 1:
        raise gentoo.ValidationError("Unsupported handoff schema.")
    if not {"candidate", "committed_at", "packaging_changes"}.issubset(metadata):
        raise gentoo.ValidationError("Handoff is missing discovery metadata.")
    receipt = {key: metadata[key] for key in ("candidate", "committed_at", "packaging_changes")}
    ebuilds = expected_ebuilds(snapshots, receipt)
    paths = {*ebuilds, MANIFEST}
    check_inventory(artifact, {Path("handoff.json"), *(Path("overlay") / path for path in paths)})
    transferred = {path: read_file(artifact / "overlay" / path) for path in paths}
    if metadata != handoff_metadata(commit, snapshots, receipt, transferred):
        raise gentoo.ValidationError("Handoff metadata, expected diff or file checksums do not match.")
    if any(transferred[path] != content for path, content in ebuilds.items()):
        raise gentoo.ValidationError("Handoff ebuilds do not match the accepted template and candidate SHA.")
    verify_manifest(base, snapshots, receipt, transferred[MANIFEST])
    return metadata


def run_container(work: Path, repository: Path, result: Path,
                  snapshots: list[prepare.Snapshot], receipt: dict) -> None:
    config = work / "docker-config"
    config.mkdir()
    docker = ["docker", "--config", str(config)]
    create = [*docker, "create", "--platform", "linux/amd64", PORTAGE, "/bin/true"]
    print(f"+ {shlex.join(create)}", flush=True)
    volume = subprocess.check_output(create, text=True).strip()
    try:
        current = snapshot_info(snapshots[-1])
        candidate = receipt["candidate"]
        old_sha = snapshots[0].sha if len(snapshots) == 2 else ""
        command = [
            *docker, "run", "--rm", "--platform", "linux/amd64", "--volumes-from", f"{volume}:ro",
            "--mount", f"type=bind,src={repository},dst=/input,readonly",
            "--mount", f"type=bind,src={result},dst=/result",
            "--mount", f"type=bind,src={Path(gentoo.__file__).resolve()},dst=/gentoo_validate.py,readonly",
            STAGE3, "/bin/bash", "-c", CONTAINER_SETUP, "luciole-validation",
            candidate["version"], current["version"], candidate["sha"], current["sha"], old_sha,
        ]
        print(f"+ {shlex.join(command)}", flush=True)
        subprocess.run(command, check=True)
    finally:
        subprocess.run([*docker, "rm", "-v", volume], check=True, stdout=subprocess.DEVNULL)


def validate_stage(stage: Path, root: Path) -> Path:
    if stage.is_symlink():
        raise gentoo.ValidationError("Expected a regular stage directory.")
    stage, root = stage.resolve(strict=True), root.resolve(strict=True)
    if stage.is_relative_to(root):
        raise gentoo.ValidationError("Stage must be outside the accepted checkout.")
    commit, snapshots, base = accepted_base(root)
    receipt = read_json(stage / "candidate.json")
    ebuilds = expected_ebuilds(snapshots, receipt)
    inputs = {**ebuilds, MANIFEST: base[MANIFEST], PACKAGE_METADATA: base[PACKAGE_METADATA]}
    marker = Path("INCOMPLETE.txt")
    check_inventory(stage, {*inputs, marker, Path("candidate.json")})
    if any(read_file(stage / path) != content for path, content in inputs.items()):
        raise gentoo.ValidationError("Staged package does not match the expected rotation and base overlay.")
    aliases = {gentoo.distfile_alias(snapshot.content, snapshot.sha) for snapshot in snapshots}
    gentoo.parse_manifest(base[MANIFEST], aliases)

    with TemporaryDirectory(prefix=".validation-", dir=stage) as temporary:
        work = Path(temporary)
        repository, result = work / "repository", work / "result"
        result.mkdir()
        for path, content in {**inputs, **{path: base[path] for path in REPO_FILES}}.items():
            target = repository / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        run_container(work, repository, result, snapshots, receipt)
        check_inventory(result, {Path("Manifest")})
        manifest = read_file(result / "Manifest")
        verify_manifest(base, snapshots, receipt, manifest)
        if accepted_base(root) != (commit, snapshots, base):
            raise gentoo.ValidationError("Accepted base changed during validation.")

        artifact = work / "handoff"
        transferred = {**ebuilds, MANIFEST: manifest}
        for path, content in transferred.items():
            target = artifact / "overlay" / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        write_json(artifact / "handoff.json", handoff_metadata(commit, snapshots, receipt, transferred))
        verify_handoff(artifact, commit, snapshots, base)

        pending_manifest = work / "Manifest"
        pending_manifest.write_bytes(manifest)
        artifact.rename(stage / "handoff")
        pending_manifest.replace(stage / MANIFEST)
        (stage / marker).unlink()
    return stage / "handoff"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "verify"))
    parser.add_argument("directory", type=Path, help="Prepared stage or handoff directory.")
    args = parser.parse_args()
    try:
        if args.command == "validate":
            artifact = validate_stage(args.directory, prepare.ROOT)
            print(f"Verified handoff at {artifact}")
        else:
            verify_handoff(args.directory, *accepted_base(prepare.ROOT))
            print(f"Verified handoff at {args.directory}")
    except (gentoo.ValidationError, prepare.PreparationError, OSError,
            ValueError, subprocess.CalledProcessError) as error:
        print(f"Luciole validation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
