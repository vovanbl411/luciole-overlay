"""Discover and stage a Luciole snapshot for local review."""

import argparse
import base64
from dataclasses import dataclass
from datetime import datetime, timezone
from http.client import HTTPException
import json
import os
from pathlib import Path
import re
import shutil
import sys
from tempfile import mkdtemp
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = Path("gui-apps/luciole")
UPSTREAM = "https://github.com/noctalia-dev/luciole"
API = "https://api.github.com/repos/noctalia-dev/luciole"
OVERLAY = "vovanbl411/luciole-overlay"
AUTOMATION_REF = re.compile(r"automation/luciole-[0-9a-f]{12}")
HTTP_TIMEOUT = 20
SHA = re.compile(r"[0-9a-f]{40}")
VERSION = r"(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*)){2}"
SNAPSHOT = re.compile(rf"luciole-({VERSION})_pre([0-9]{{14}})\.ebuild")
COMMIT_DATE = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])"
)
PIN = re.compile(rb'^MY_COMMIT="([0-9a-f]{40})"$', re.MULTILINE)
ASSIGNMENT = re.compile(rb"(?<![\w{$])MY_COMMIT(?:\[[^\]]*\])?[ \t]*\+?=")
PACKAGING_FILES = {
    "VERSION", "PACKAGING.md", "meson.build", "meson_options.txt", "lib/meson.build",
    "nix/package.nix", "LICENSE", "lib/assets/fonts/tabler-icons-license.txt",
    "lib/protocols/text-input-unstable-v3.xml",
}
LICENSE_NAME = re.compile(r"(?:^|[._/-])(?:licen[cs]es?|copying|copyright|notice)(?:$|[._/-])", re.I)
INCOMPLETE = (
    "Incomplete candidate. Phase 4.2 Portage validation and Manifest regeneration "
    "are required before publication."
)


class PreparationError(RuntimeError):
    pass


@dataclass(frozen=True)
class Snapshot:
    filename: str
    order: tuple[int, ...]
    sha: str
    content: bytes


@dataclass(frozen=True)
class Discovery:
    current: Snapshot
    previous: Snapshot | None
    sha: str
    version: str
    filename: str
    committed_at: datetime
    changes: dict[str, str]


def read_snapshots(package: Path) -> list[Snapshot]:
    if package.is_symlink() or package.parent.is_symlink() or not package.is_dir():
        raise PreparationError(f"Expected a regular package directory at {package}.")
    paths = sorted(package.glob("*.ebuild"))
    if not 1 <= len(paths) <= 2:
        raise PreparationError("Expected one or two accepted Luciole snapshot ebuilds.")

    snapshots = []
    for path in paths:
        match = SNAPSHOT.fullmatch(path.name)
        if not match or path.is_symlink() or not path.is_file():
            raise PreparationError(f"Unexpected Luciole snapshot ebuild {path.name!r}.")
        version, stamp = match.groups()
        try:
            datetime.strptime(stamp, "%Y%m%d%H%M%S")
            order = (*map(int, version.split(".")), int(stamp))
        except ValueError as error:
            raise PreparationError(f"Invalid Luciole snapshot version in {path.name!r}.") from error
        content = path.read_bytes()
        pins = list(PIN.finditer(content))
        assignments = sum(
            len(ASSIGNMENT.findall(line)) for line in content.splitlines()
            if not line.lstrip().startswith(b"#")
        )
        if len(pins) != 1 or assignments != 1 or b"\0" in content:
            raise PreparationError(f"Expected exactly one controlled MY_COMMIT assignment in {path.name!r}.")
        snapshots.append(Snapshot(path.name, order, pins[0][1].decode("ascii"), content))

    if len({snapshot.sha for snapshot in snapshots}) != len(snapshots):
        raise PreparationError("Accepted Luciole snapshots have a duplicate SHA.")
    return sorted(snapshots, key=lambda snapshot: snapshot.order)


def github_request(path: str, *, repository_api: str = API, payload: dict | None = None):
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "luciole-overlay-prepare",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if payload is not None:
        headers["Content-Type"] = "application/json"
    request = Request(
        f"{repository_api}/{path}", data=json.dumps(payload).encode() if payload is not None else None,
        headers=headers,
    )
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        request.add_unredirected_header("Authorization", f"Bearer {token}")
    try:
        with urlopen(request, timeout=HTTP_TIMEOUT) as response:
            return json.load(response)
    except HTTPError as error:
        error.close()
        raise PreparationError(f"GitHub API {path} failed with HTTP {error.code}.") from error
    except (URLError, OSError, HTTPException) as error:
        raise PreparationError(f"GitHub API {path} request failed.") from error
    except (ValueError, RecursionError) as error:
        raise PreparationError(f"Malformed JSON from GitHub API {path}.") from error


def github_json(path: str) -> dict:
    payload = github_request(path)
    if not isinstance(payload, dict):
        raise PreparationError(f"Expected a JSON object from GitHub API {path}.")
    return payload


def overlay_request(path: str, *, payload: dict | None = None):
    return github_request(path, repository_api=f"https://api.github.com/repos/{OVERLAY}", payload=payload)


def open_snapshot_pr() -> int | None:
    for page in range(1, 101):
        pulls = overlay_request(f"pulls?state=open&base=main&per_page=100&page={page}")
        if not isinstance(pulls, list) or len(pulls) > 100:
            raise PreparationError("Malformed open pull request list.")
        for pull in pulls:
            number = field(pull, "number")
            head_ref = field(pull, "head", "ref")
            head = field(pull, "head")
            head_repo = field(head, "repo", "full_name")
            base_repo = field(pull, "base", "repo", "full_name")
            base_ref = field(pull, "base", "ref")
            if (
                type(number) is not int or number < 1 or not isinstance(head_ref, str)
                or "repo" not in head or (head["repo"] is not None and not isinstance(head_repo, str))
                or not isinstance(base_repo, str) or not isinstance(base_ref, str)
                or field(pull, "state") != "open"
            ):
                raise PreparationError("Malformed item in the open pull request list.")
            if head_repo == base_repo == OVERLAY and base_ref == "main" and AUTOMATION_REF.fullmatch(head_ref):
                return number
        if len(pulls) < 100:
            return None
    raise PreparationError("Open pull request pagination limit reached.")


def field(payload: object, *keys: str) -> object:
    for key in keys:
        if not isinstance(payload, dict):
            return None
        payload = payload.get(key)
    return payload


def is_sha(value: object) -> bool:
    return isinstance(value, str) and SHA.fullmatch(value) is not None


def require_forward_history(base: str, head: str) -> None:
    compare = github_json(f"compare/{base}...{head}?per_page=1")
    ahead, behind = compare.get("ahead_by"), compare.get("behind_by")
    if (
        compare.get("status") != "ahead"
        or field(compare, "base_commit", "sha") != base
        or field(compare, "merge_base_commit", "sha") != base
        or type(ahead) is not int or ahead < 1
        or type(behind) is not int or behind != 0
    ):
        raise PreparationError(f"Expected forward history from {base} to {head}.")


def read_version(sha: str) -> str:
    data = github_json(f"contents/VERSION?ref={sha}")
    encoded = data.get("content")
    if (
        data.get("type") != "file" or data.get("path") != "VERSION"
        or data.get("encoding") != "base64" or not isinstance(encoded, str)
    ):
        raise PreparationError(f"Invalid VERSION response for {sha}.")
    try:
        version = base64.b64decode("".join(encoded.splitlines()), validate=True).decode("ascii")
    except ValueError as error:
        raise PreparationError(f"Invalid VERSION encoding for {sha}.") from error
    if version.endswith("\n"):
        version = version[:-1].removesuffix("\r")
    if not re.fullmatch(VERSION, version):
        raise PreparationError(f"VERSION at {sha} must use X.Y.Z with canonical decimal components.")
    return version


def commit_metadata(sha: str) -> dict:
    data = github_json(f"git/commits/{sha}")
    if data.get("sha") != sha or not is_sha(field(data, "tree", "sha")):
        raise PreparationError(f"Invalid commit metadata for {sha}.")
    return data


def committer_time(commit: dict) -> datetime:
    date = field(commit, "committer", "date")
    try:
        if isinstance(date, str) and COMMIT_DATE.fullmatch(date):
            return datetime.fromisoformat(date.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (ValueError, OverflowError):
        pass
    raise PreparationError(f"Invalid committer timestamp for {commit['sha']}.")


def packaging_entries(tree_sha: str) -> dict[str, tuple[str, str, str]]:
    data = github_json(f"git/trees/{tree_sha}?recursive=1")
    entries = data.get("tree")
    if data.get("sha") != tree_sha or data.get("truncated") is not False or not isinstance(entries, list):
        raise PreparationError(f"Expected a complete GitHub tree for {tree_sha}.")
    watched = {}
    for entry in entries:
        path = field(entry, "path")
        if not isinstance(path, str) or not path:
            raise PreparationError(f"Invalid entry in GitHub tree {tree_sha}.")
        if path not in PACKAGING_FILES and not LICENSE_NAME.search(path):
            continue
        sha, mode, kind = entry.get("sha"), entry.get("mode"), entry.get("type")
        if (
            path in watched or not is_sha(sha)
            or (kind, mode) not in (
                ("blob", "100644"), ("blob", "100755"), ("blob", "120000"),
                ("tree", "040000"), ("commit", "160000"),
            )
        ):
            raise PreparationError(f"Invalid packaging entry in GitHub tree {tree_sha}.")
        watched[path] = (sha, mode, kind)
    return watched


def packaging_changes(base_tree: str, head_tree: str) -> dict[str, str]:
    # Compare both complete trees since GitHub caps the compare API's file list.
    before, after = packaging_entries(base_tree), packaging_entries(head_tree)
    changes = {}
    for path in sorted(PACKAGING_FILES | before.keys() | after.keys()):
        if path not in before:
            changes[path] = "added" if path in after else "absent"
        elif path not in after:
            changes[path] = "removed"
        else:
            changes[path] = "unchanged" if before[path] == after[path] else "modified"
    return changes


def discover(package: Path) -> Snapshot | Discovery:
    snapshots = read_snapshots(package)
    current = snapshots[-1]
    ref = github_json("git/ref/heads/main")
    sha = field(ref, "object", "sha")
    if ref.get("ref") != "refs/heads/main" or field(ref, "object", "type") != "commit" or not is_sha(sha):
        raise PreparationError("Upstream main did not resolve to a full commit SHA.")
    if sha == current.sha:
        return current

    previous = snapshots[0] if len(snapshots) == 2 else None
    if previous and sha == previous.sha:
        raise PreparationError(f"Expected forward history, but upstream main points to the previous snapshot {sha}.")
    require_forward_history(current.sha, sha)
    version = read_version(sha)
    commit = commit_metadata(sha)
    committed_at = committer_time(commit)
    stamp = f"{committed_at.year:04d}{committed_at:%m%d%H%M%S}"
    filename = f"luciole-{version}_pre{stamp}.ebuild"
    if len(filename) > 255:
        raise PreparationError("Upstream VERSION is too long for a snapshot filename.")
    if any(filename == snapshot.filename for snapshot in snapshots):
        raise PreparationError(f"Candidate filename collision at {filename!r}.")
    order = (*map(int, version.split(".")), int(stamp))
    if order <= current.order:
        raise PreparationError("Candidate version must be newer than the current snapshot.")

    base_tree = commit_metadata(current.sha)["tree"]["sha"]
    changes = packaging_changes(base_tree, commit["tree"]["sha"])
    return Discovery(current, previous, sha, version, filename, committed_at, changes)


def stage(package: Path, parent: Path, plan: Discovery) -> Path:
    accepted = [plan.previous, plan.current] if plan.previous else [plan.current]
    if read_snapshots(package) != accepted:
        raise PreparationError("Accepted ebuilds changed during discovery.")

    staging_root = Path(mkdtemp(prefix="luciole-candidate-", dir=parent))
    try:
        (staging_root / "INCOMPLETE.txt").write_text(INCOMPLETE + "\n", encoding="utf-8")
        receipt = {
            "candidate": {
                "version": plan.filename.removeprefix("luciole-").removesuffix(".ebuild"),
                "sha": plan.sha,
            },
            "committed_at": plan.committed_at.isoformat(),
            "packaging_changes": plan.changes,
        }
        (staging_root / "candidate.json").write_text(
            json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8",
        )
        staged_package = staging_root / PACKAGE
        def ignore(directory, _names):
            if plan.previous and Path(directory) == package:
                return [plan.previous.filename]
            return []

        shutil.copytree(package, staged_package, symlinks=True, ignore=ignore)
        if read_snapshots(staged_package) != [plan.current]:
            raise PreparationError("Accepted ebuilds changed during staging.")

        content = PIN.sub(b'MY_COMMIT="' + plan.sha.encode("ascii") + b'"', plan.current.content)
        target = staged_package / plan.filename
        with target.open("xb") as candidate:
            candidate.write(content)
        shutil.copymode(staged_package / plan.current.filename, target)
    except (OSError, PreparationError):
        shutil.rmtree(staging_root)
        raise
    return staging_root


def print_plan(plan: Discovery) -> None:
    commit_range = f"{plan.current.sha}...{plan.sha}"
    print(f"Add {plan.filename} ({plan.sha})")
    print(f"Upstream VERSION {plan.version}")
    print(f"UTC committer timestamp {plan.committed_at.isoformat()}")
    print(f"Keep {plan.current.filename} ({plan.current.sha})")
    print(f"Drop {plan.previous.filename}" if plan.previous else "Drop no snapshots")
    print(f"Compare {UPSTREAM}/compare/{commit_range}")
    print("Upstream packaging files")
    for path, change in plan.changes.items():
        print(f"  {change:9} {path!r}")


def main(argv: list[str] | None = None, *, root: Path = ROOT) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="Report the plan without creating files.")
    mode.add_argument(
        "--stage-dir", type=Path, metavar="PARENT",
        help="Create a candidate under this existing external directory.",
    )
    parser.add_argument("--github-output", type=Path, help="Check open snapshot PRs and append Actions outputs.")
    options = parser.parse_args(argv)
    try:
        root = root.resolve()
        output = None
        if options.github_output is not None:
            output = options.github_output.resolve()
            if output.is_relative_to(root) or "\n" in str(output) or "\r" in str(output):
                raise PreparationError("Actions output must be outside the checkout.")
        package = root / PACKAGE
        parent = None
        if options.stage_dir is not None:
            parent = options.stage_dir.resolve(strict=True)
            if not parent.is_dir() or parent.is_relative_to(root):
                raise PreparationError("The staging parent must be a directory outside the checkout.")
        result = {"status": "up_to_date", "has_candidate": "false", "candidate_sha": "",
                  "candidate_version": "", "stage_dir": "", "open_pr": ""}
        if output is not None:
            number = open_snapshot_pr()
            if number:
                print(f"Automated Luciole snapshot PR #{number} is already open.")
                result["open_pr"] = str(number)
                if not options.dry_run:
                    result["status"] = "open_pr"
                    append_outputs(output, result)
                    return 0
            else:
                print("No open automated Luciole snapshot PR.")
        plan = discover(package)
        if isinstance(plan, Snapshot):
            print(f"Luciole is up to date with upstream main ({plan.sha}): {plan.filename}")
        else:
            result.update(
                status="dry_run" if options.dry_run else "staged", has_candidate="true",
                candidate_sha=plan.sha,
                candidate_version=plan.filename.removeprefix("luciole-").removesuffix(".ebuild"),
            )
            print_plan(plan)
            if options.dry_run:
                print("Dry run. No candidate files created.")
            else:
                staged = stage(package, parent, plan)
                result["stage_dir"] = str(staged)
                print(f"Staged candidate at {staged}")
                print(INCOMPLETE)
        if output is not None:
            append_outputs(output, result)
    except (PreparationError, OSError) as error:
        print(f"Luciole preparation failed: {error}", file=sys.stderr)
        return 1
    return 0


def append_outputs(output: Path, result: dict[str, str]) -> None:
    if any("\r" in value or "\n" in value for value in result.values()):
        raise PreparationError("Actions output values must fit on a single line.")
    with output.open("a", encoding="utf-8") as stream:
        stream.writelines(f"{name}={value}\n" for name, value in result.items())


if __name__ == "__main__":
    sys.exit(main())
