"""Apply a verified Luciole handoff and open its draft snapshot pull request."""

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

if __package__:
    from . import prepare_luciole as prepare, validate_luciole as validation
else:
    import prepare_luciole as prepare
    import validate_luciole as validation


REMOTE = f"https://github.com/{prepare.OVERLAY}.git"
MAIN = "refs/heads/main"


class PublicationError(RuntimeError):
    pass


def git(root: Path, *args: str, env: dict | None = None) -> bytes:
    result = subprocess.run(["git", *args], cwd=root, env=env, capture_output=True)
    if result.returncode:
        # Remote Git commands use an environment-only authorization header.
        raise PublicationError(f"Git {args[0]} failed (exit {result.returncode}).")
    return result.stdout


def verify_diff(root: Path, metadata: dict) -> None:
    names = git(root, "diff", "--cached", "--no-renames", "--name-status", "-z", "HEAD")
    fields = names.decode("utf-8").split("\0")[:-1]
    actual = set(zip(fields[::2], fields[1::2]))
    expected = {
        (status, path)
        for operation, status in (("add", "A"), ("modify", "M"), ("delete", "D"))
        for path in metadata["expected_diff"][operation]
    }
    if not actual or len(fields) != 2 * len(expected) or actual != expected:
        raise PublicationError("Staged Git diff does not match the handoff’s exact additions, modifications and deletions.")
    git(root, "diff", "--cached", "--check")
    if git(root, "diff", "--name-only", "-z") or git(root, "ls-files", "--others", "--exclude-standard", "-z"):
        raise PublicationError("Unexpected unstaged or untracked files after applying the handoff.")
    for status, path in actual:
        if status != "D" and hashlib.sha256(git(root, "show", f":{path}")).hexdigest() != metadata["sha256"][path]:
            raise PublicationError("Staged Git content differs from the verified handoff.")


def apply_handoff(root: Path, artifact: Path) -> dict:
    if git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all"):
        raise PublicationError("The publication checkout must start clean.")
    base = validation.accepted_base(root)
    metadata = validation.verify_handoff(artifact, *base)
    diff = metadata["expected_diff"]
    for path in diff["add"]:
        with (root / path).open("xb") as target:
            target.write(validation.read_file(artifact / "overlay" / path))
    for path in diff["modify"]:
        (root / path).write_bytes(validation.read_file(artifact / "overlay" / path))
    for path in diff["delete"]:
        (root / path).unlink()
    git(root, "add", "-A", "--", *diff["add"], *diff["modify"], *diff["delete"])
    verify_diff(root, metadata)
    return metadata


def pull_request(metadata: dict, branch: str) -> dict:
    previous, candidate = metadata["previous"], metadata["candidate"]
    deleted = metadata["expected_diff"]["delete"]
    rotation = (
        f"Retain `{previous['version']}`. "
        + (f"Remove `{Path(deleted[0]).name}`." if deleted else "Retain both snapshots after adding the candidate.")
    )
    packaging = metadata["packaging_changes"]
    changes = "\n".join(f"{status} {json.dumps(path)}" for path, status in sorted(packaging.items()))
    review = "Review these upstream packaging changes before merging." if any(
        status in {"added", "removed", "modified"} for status in packaging.values()
    ) else "The monitored upstream packaging files are unchanged."
    return {
        "title": f"[snapshot-update] Luciole {candidate['version']}",
        "base": "main",
        "head": branch,
        "draft": True,
        "body": f"""## Snapshot

| | Package version | Upstream SHA |
| --- | --- | --- |
| Previous | `{previous['version']}` | `{previous['sha']}` |
| Candidate | `{candidate['version']}` | `{candidate['sha']}` |

{rotation}

[Upstream comparison]({metadata['compare_url']})

## Upstream packaging files

{review}

```text
{changes}
```

## Validation

Portage version ordering, Manifest generation, both archive checksums and
sizes, pkgcheck, and exact candidate dependency resolution passed in the
pinned Gentoo container. Dependency resolution included build dependencies
with automatic unmasking off and Luciole's `test` flag disabled.

Compilation, upstream test execution, and runtime testing remain separate.
Review the package diff and upstream changes before merging.
""",
    }


def git_auth(token: str) -> dict:
    authorization = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {
        **os.environ,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {authorization}",
    }


def remote_refs(root: Path, remote: str, branch_ref: str, env: dict | None) -> dict:
    refs = {}
    for line in git(root, "ls-remote", "--refs", remote, MAIN, branch_ref, env=env).decode("ascii").splitlines():
        sha, _, name = line.partition("\t")
        if not prepare.is_sha(sha) or name not in {MAIN, branch_ref} or name in refs:
            raise PublicationError("Unexpected Git remote ref response.")
        refs[name] = sha
    return refs


def require_main(refs: dict, base: str) -> None:
    if refs.get(MAIN) != base:
        raise PublicationError("Remote main changed since preparation. Prepare a fresh handoff.")


def push_new_branch(root: Path, remote: str, head: str, branch_ref: str, env: dict | None) -> None:
    # Git supplies the advertised remote SHA to pre-push. A normal push
    # could otherwise fast-forward a branch created after our ls-remote.
    with TemporaryDirectory(prefix="luciole-push-") as hooks:
        hook = Path(hooks) / "pre-push"
        hook.write_text(
            '#!/bin/sh\n'
            'read -r source source_sha target target_sha || exit 1\n'
            'test "$target_sha" = 0000000000000000000000000000000000000000 || exit 1\n'
            'if read -r unexpected; then exit 1; fi\n',
            encoding="ascii",
        )
        hook.chmod(0o700)
        try:
            git(root, "-c", f"core.hooksPath={hooks}", "push", remote, f"{head}:{branch_ref}", env=env)
        except PublicationError as error:
            raise PublicationError(f"Normal push could not create {branch_ref}. Check the branch and retry.") from error


def publish(root: Path, artifact: Path, *, remote: str = REMOTE, env: dict | None = None) -> None:
    if number := prepare.open_snapshot_pr():
        print(f"Automated Luciole snapshot PR #{number} is already open.")
        return
    metadata = apply_handoff(root, artifact)
    base = metadata["base_commit"]
    candidate = metadata["candidate"]
    branch = f"automation/luciole-{candidate['sha'][:12]}"
    branch_ref = f"refs/heads/{branch}"
    tree = git(root, "write-tree").decode("ascii").strip()
    refs = remote_refs(root, remote, branch_ref, env)
    require_main(refs, base)
    head = refs.get(branch_ref)
    if head:
        git(root, "fetch", "--no-tags", remote, branch_ref, env=env)
        parents = git(root, "rev-list", "--parents", "-n", "1", "FETCH_HEAD").decode("ascii").split()
        remote_tree = git(root, "rev-parse", "FETCH_HEAD^{tree}").decode("ascii").strip()
        if parents != [head, base] or remote_tree != tree:
            raise PublicationError(f"Existing branch {branch} has a different parent or tree.")
        print(f"Reusing verified branch {branch}.")
    else:
        head = git(
            root, "-c", "user.name=github-actions[bot]",
            "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com",
            "commit-tree", tree, "-p", base, "-m", f"update: Luciole {candidate['version']}",
        ).decode("ascii").strip()
        if number := prepare.open_snapshot_pr():
            print(f"Automated Luciole snapshot PR #{number} is already open.")
            return
        refs = remote_refs(root, remote, branch_ref, env)
        require_main(refs, base)
        if branch_ref in refs:
            raise PublicationError(f"Branch {branch} appeared during preparation. Retry to verify it.")
        push_new_branch(root, remote, head, branch_ref, env)
        print(f"Pushed {branch}.")

    try:
        refs = remote_refs(root, remote, branch_ref, env)
        require_main(refs, base)
        if refs.get(branch_ref) != head:
            raise PublicationError("The automation branch changed before draft PR creation.")
        if number := prepare.open_snapshot_pr():
            print(f"Automated Luciole snapshot PR #{number} is already open.")
            return
        created = prepare.overlay_request("pulls", payload=pull_request(metadata, branch))
        number = prepare.field(created, "number")
        if type(number) is not int or number < 1 or prepare.field(created, "draft") is not True:
            raise PublicationError("GitHub did not confirm creation of a draft pull request.")
    except (PublicationError, prepare.PreparationError) as error:
        raise PublicationError(
            f"{error} Branch {branch} was pushed or reused. A later run can reuse an identical branch."
        ) from error
    print(f"Draft PR created at https://github.com/{prepare.OVERLAY}/pull/{number}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("apply", "publish"))
    parser.add_argument("artifact", type=Path)
    parser.add_argument("--repository", type=Path, default=prepare.ROOT, help="Checkout for local apply.")
    args = parser.parse_args()
    try:
        if args.command == "apply":
            metadata = apply_handoff(args.repository, args.artifact)
            print(f"Staged verified snapshot {metadata['candidate']['version']}.")
        else:
            token = os.environ.get("GITHUB_TOKEN")
            if (
                not token or os.environ.get("GITHUB_ACTIONS") != "true"
                or os.environ.get("GITHUB_REPOSITORY") != prepare.OVERLAY
                or os.environ.get("GITHUB_REF") != MAIN or args.repository != prepare.ROOT
            ):
                raise PublicationError("Publication requires the trusted main-branch GitHub Actions job.")
            publish(prepare.ROOT, args.artifact, env=git_auth(token))
    except (PublicationError, prepare.PreparationError, validation.gentoo.ValidationError,
            OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Luciole publication failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
