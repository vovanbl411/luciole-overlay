"""Validate the staged Luciole package inside the pinned Gentoo container."""

import hashlib
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys


PACKAGE = Path("gui-apps/luciole")
DISTFILE = re.compile(r"luciole-[0-9a-f]{40}\.tar\.gz")
SOURCE = (
    b'SRC_URI="https://github.com/noctalia-dev/luciole/archive/${MY_COMMIT}.tar.gz'
    b' -> ${PN}-${MY_COMMIT}.tar.gz"'
)
HASHES = {"BLAKE2B": 128, "SHA512": 128}
DISTDIR = Path("/var/cache/distfiles")
# Portage reported these dependencies against the pinned stable amd64 profile.
RESOLVER_KEYWORDS = ("=dev-libs/wayland-protocols-1.49 ~amd64",)
RESOLVER_USE = (
    "=x11-libs/gdk-pixbuf-2.44.7 jpeg",
    "=x11-libs/cairo-1.18.4-r1 X",
    "=media-libs/libglvnd-1.7.0 X",
    "=media-libs/freetype-2.14.3 harfbuzz",
    "=app-crypt/gcr-3.41.2-r2 gtk",
    "=media-libs/mesa-26.1.8 wayland",
)


class ValidationError(RuntimeError):
    pass


def distfile_alias(content: bytes, sha: str) -> str:
    declarations = [line for line in content.splitlines() if line.startswith(b"SRC_URI=")]
    if declarations != [SOURCE]:
        raise ValidationError("Expected the controlled Luciole SRC_URI and distfile alias.")
    return f"luciole-{sha}.tar.gz"


def parse_manifest(content: bytes, expected: set[str]) -> dict:
    records = {}
    for line in content.decode("ascii").splitlines():
        fields = line.split()
        if len(fields) != 7 or fields[0] != "DIST":
            raise ValidationError("Expected thin Manifest DIST records with BLAKE2B and SHA512.")
        name, size = fields[1:3]
        if not DISTFILE.fullmatch(name) or name in records:
            raise ValidationError(f"Invalid or duplicate Manifest DIST name {name!r}.")
        if not re.fullmatch(r"[1-9][0-9]{0,19}", size):
            raise ValidationError(f"Invalid Manifest size for {name}.")
        hashes = dict(zip(fields[3::2], fields[4::2]))
        if hashes.keys() != HASHES.keys() or any(
            not re.fullmatch(rf"[0-9a-f]{{{HASHES[name]}}}", digest)
            for name, digest in hashes.items()
        ):
            raise ValidationError(f"Invalid Manifest hashes for {name}.")
        records[name] = (int(size), hashes)
    if records.keys() != expected:
        raise ValidationError(
            f"Manifest DIST inventory differs: missing={sorted(expected - records.keys())}, "
            f"unexpected={sorted(records.keys() - expected)}."
        )
    return records


def preserve_distfile(before: dict, after: dict, retained: str) -> None:
    if retained not in before or before[retained] != after.get(retained):
        raise ValidationError(f"Accepted DIST record changed for {retained}.")


def verify_archives(directory: Path, records: dict) -> None:
    for name, (size, checksums) in records.items():
        archive = directory / name
        digests = {key: hashlib.new(key.lower()) for key in checksums}
        if not archive.is_file() or archive.stat().st_size != size:
            raise ValidationError(f"Missing archive or incorrect archive size for {name}.")
        with archive.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                for digest in digests.values():
                    digest.update(chunk)
        if any(checksums[key] != digest.hexdigest() for key, digest in digests.items()):
            raise ValidationError(f"Archive checksum mismatch for {name}.")


def require_newer(candidate: str, previous: str, vercmp) -> None:
    result = vercmp(candidate, previous)
    if result is None or result <= 0:
        raise ValidationError(f"Portage vercmp rejected {previous} -> {candidate}.")


def candidate_keywords(atom: str, arch: str, accepted: set[str]) -> str:
    if arch != "amd64":
        raise ValidationError(f"Stage3 ARCH={arch!r} is incompatible with Luciole KEYWORDS=~amd64.")
    if "~amd64" in accepted or "~*" in accepted or "**" in accepted:
        return ""
    return f"{atom} ~amd64\n"


def run(*command: str, cwd: Path | None = None) -> None:
    print(f"+ {shlex.join(command)}", flush=True)
    subprocess.run(command, cwd=cwd, check=True)


def validate(root: Path, output: Path, candidate: str, previous: str, candidate_sha: str,
             previous_sha: str, old_sha: str, *, portage, vercmp) -> None:
    package = root / PACKAGE
    current_ebuild = package / f"luciole-{candidate}.ebuild"
    previous_ebuild = package / f"luciole-{previous}.ebuild"
    if set(package.glob("*.ebuild")) != {current_ebuild, previous_ebuild} or candidate == previous:
        raise ValidationError("Unexpected staged ebuild inventory.")
    for path, sha in ((current_ebuild, candidate_sha), (previous_ebuild, previous_sha)):
        if not re.fullmatch("[0-9a-f]{40}", sha) or not re.search(
            rb'^MY_COMMIT="' + sha.encode("ascii") + rb'"$', path.read_bytes(), re.MULTILINE,
        ):
            raise ValidationError(f"Staged SHA does not match {path.name}.")
    require_newer(candidate, previous, vercmp)
    print(f"Portage vercmp: {candidate} > {previous}", flush=True)

    manifest = package / "Manifest"
    retained = distfile_alias(previous_ebuild.read_bytes(), previous_sha)
    added = distfile_alias(current_ebuild.read_bytes(), candidate_sha)
    accepted = {retained}
    if old_sha:
        accepted.add(f"luciole-{old_sha}.tar.gz")
    before = parse_manifest(manifest.read_bytes(), accepted)
    run("ebuild", str(previous_ebuild), "fetch")
    verify_archives(DISTDIR, {retained: before[retained]})
    run("ebuild", str(current_ebuild), "manifest")
    after = parse_manifest(manifest.read_bytes(), {retained, added})
    preserve_distfile(before, after, retained)
    verify_archives(DISTDIR, after)

    atom = f"=gui-apps/luciole-{candidate}::luciole-overlay"
    arch = portage.settings["ARCH"]
    accepted_keywords = set(portage.settings["ACCEPT_KEYWORDS"].split())
    print(f"Stage3 ARCH={arch}; ACCEPT_KEYWORDS={' '.join(sorted(accepted_keywords))}", flush=True)
    keywords = candidate_keywords(atom, arch, accepted_keywords)
    write_config("package.accept_keywords", keywords + "\n".join(RESOLVER_KEYWORDS) + "\n")
    write_config("package.use", "\n".join((f"{atom} -test", *RESOLVER_USE)) + "\n")
    run("emerge", "-pv", "--with-bdeps=y", "--autounmask=n", atom)

    if shutil.which("pkgcheck") is None:
        run("emerge", "--oneshot", "--autounmask=n", "dev-util/pkgcheck")
    run("pkgcheck", "scan", "gui-apps/luciole", "--exit", cwd=root)
    shutil.copyfile(manifest, output / "Manifest")


def write_config(directory: str, lines: str) -> None:
    path = Path("/etc/portage") / directory / "luciole-validation"
    path.parent.mkdir(exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        stream.write(lines)
    print(f"{path} includes {lines.rstrip()}", flush=True)


def main() -> int:
    try:
        import portage
        from portage.versions import vercmp

        validate(Path("/overlay"), Path("/result"), *sys.argv[1:], portage=portage, vercmp=vercmp)
    except (ValidationError, OSError, ValueError, ImportError, subprocess.CalledProcessError) as error:
        print(f"Gentoo validation failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
