# Luciole overlay

[Русский](README.md)

This overlay packages Luciole, a native Wayland file manager, as
`gui-apps/luciole` for `~amd64`.

## Snapshot preparation

Snapshots use `<VERSION>_pre<YYYYMMDDHHMMSS>`, where `VERSION` is the
upstream `X.Y.Z` version and the suffix is the commit's committer timestamp
in UTC. The ebuild filenames and their `MY_COMMIT` values record the
accepted snapshots. The latest ebuild is current. A staged update keeps it
as the previous snapshot and drops the oldest ebuild if two are present.

Run from the overlay root with Python 3.10 or later and access to the public
GitHub API.

```sh
python3 scripts/prepare_luciole.py --dry-run
python3 scripts/prepare_luciole.py --stage-dir /tmp
```

Each invocation resolves upstream `main` once and uses that commit for
discovery. The dry run prints the version, SHA, rotation, and upstream
packaging changes without creating files. If the current ebuild already
pins that commit, both commands exit without staging anything.

Preparation creates a new directory under the specified staging parent
outside the checkout. It copies the package files and adds a candidate
ebuild with only the `MY_COMMIT` value changed. Changes to upstream packaging
files are listed for owner review and recorded in `candidate.json`. The
staging directory starts with the accepted Manifest and an `INCOMPLETE.txt`
marker.

## Validate a staged snapshot

Validation needs Docker with Linux amd64 support, Git, and network access
for the pinned images, distfiles, and validation tools. It uses this Gentoo
repository and stage3 pair.

```sh
docker pull --platform linux/amd64 gentoo/portage@sha256:ce69e5b771bfe9404731e846b835346c34f2c82817ac3626f13af198689a1961
docker pull --platform linux/amd64 gentoo/stage3@sha256:d8536e52677e3efe4caf048ca588bc685bc2d24d7fdfc882de1cdc6d38c81d96
python3 scripts/prepare_luciole.py --stage-dir /tmp
stage=/tmp/luciole-candidate-REPLACE_ME
python3 -B scripts/validate_luciole.py validate "$stage"
python3 -B scripts/validate_luciole.py verify "$stage/handoff"
```

Set `stage` to the directory printed by preparation. The validator prints the
exact Docker invocation. It mounts the pinned Gentoo tree and a copy of the
staged package read-only, then configures an overlay inside the disposable
container with repository synchronization disabled. Docker and Portage
configuration for the candidate is temporary.

The validator checks the ebuild inventory and SHA pins against the accepted
Git base, runs Portage's `vercmp`, fetches the retained archive and checks its
accepted size and hashes, then runs `ebuild <candidate-ebuild> manifest`.
The resulting thin Manifest must contain exactly the two staged archive
aliases, with the retained record intact. The validator verifies both
archives and runs `pkgcheck scan gui-apps/luciole --exit` and
`emerge -pv --with-bdeps=y --autounmask=n` for the exact candidate atom.
It reads the stage3 keyword policy, adds an exact candidate entry if
`~amd64` is needed, and resolves with the candidate's `test` flag disabled.

The pinned profile reports `ARCH=amd64` and `ACCEPT_KEYWORDS=amd64`.
It needs `~amd64` for `wayland-protocols-1.49`, the version required by
Luciole. The validator also uses exact-version USE entries for
`gdk-pixbuf[jpeg]` for Luciole, `cairo[X]` and `libglvnd[X]` for GTK,
`freetype[harfbuzz]` for Pango, `gcr:0[gtk]` through GVfs, and
`mesa[wayland]` for the EGL/OpenGL ES dependency. These entries apply
only inside the container.

The separate Wayland dependency preview with `test` enabled used the same
`emerge -pv --with-bdeps=y --autounmask=n` command. In the disposable
container, it used `test` in place of `-test` for the exact Luciole atom
and added `=media-libs/libheif-1.21.2 gdk-pixbuf` and
`=media-video/ffmpeg-8.1.2 libaom webp x264` to `package.use`.
The handoff records the mandatory preview with `test` disabled.

When the checks pass, `handoff/` contains `handoff.json` and the generated
Manifest and two ebuilds under `overlay/gui-apps/luciole/`. The JSON records
the base commit, upstream pins and comparison, validation gates, file
checksums, and the expected additions, modifications, and deletions. The
validator verifies these files against the base before removing the
incomplete marker. A failed gate leaves the candidate incomplete.

Preparation stages package files. Validation checks metadata, distfile
integrity, and whether Portage can resolve dependencies. Compilation,
upstream test execution, and runtime checks in a Wayland session are
separate steps.

## Snapshot updates on GitHub

The **Prepare Luciole snapshot** workflow is scheduled for 06:43 UTC daily.
To run it manually, select `main` in the Actions tab. Enable `dry_run` to
report open-PR status, the current and proposed versions and SHAs, rotation,
and upstream packaging changes without staging a candidate. Push and pull
request CI runs the offline Python tests.

The prepare job has read access to contents and pull requests. An open PR
from this repository's `automation/luciole-<12 SHA characters>` namespace
to `main` pauses candidate preparation. The publish job has write access to
contents and pull requests. It downloads the artifact ID returned by the
successful prepare job in the same run and checks the base commit, checksums,
and exact package diff. The successful prepare job supplies the Gentoo
validation evidence. The checksum check verifies the transferred files.

The owner reviews the Draft PR, including upstream packaging changes and
the ebuild and Manifest diff, completes any build or runtime checks needed
for acceptance, then marks the PR ready and merges it manually. If a push
succeeds but PR creation fails, rerun the workflow. It can reuse the branch
when its only parent is the same base and its complete tree matches. An
advanced base requires a fresh handoff. A conflicting branch stops the run
and needs owner review.

Keep `main` as the default branch for the schedule. In **Settings → Actions
→ General**, enable **Allow GitHub Actions to create
and approve pull requests** and allow the pinned `actions/checkout`,
`actions/upload-artifact`, and `actions/download-artifact` revisions if
the repository uses an action allowlist. Repository rules must allow the
workflow to create `automation/luciole-*` branches. [GitHub Actions repository settings](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/enabling-features-for-your-repository/managing-github-actions-settings-for-a-repository)

## Build and session requirements

Luciole needs a C++23 compiler and standard library with `std::print`,
`std::expected` and `std::move_only_function`. Upstream verified GCC/libstdc++
16.2. The minimum supported compiler version still needs build testing.

The desktop session needs an EGL/OpenGL ES driver for Wayland and a working
session D-Bus. The package requires GVfs for Trash. The optional FileChooser
integration needs `sys-apps/xdg-desktop-portal` and a session that selects the
`luciole` backend.

Gentoo builds GdkPixbuf with its native loaders and disables Glycin. The
`test` USE flag enables the upstream Meson suite. Upstream documents a
test in that suite that needs Glycin to read PNG-compressed ICO files.

## Optional features

| Feature | Gentoo dependency or setting |
| --- | --- |
| SFTP, SMB, WebDAV | Enable `sftp`, `samba` or `http` on `gnome-base/gvfs` for the corresponding protocol. For secure WebDAV use `net-libs/glib-networking[ssl]`. |
| DNS-SD discovery | `gnome-base/gvfs[zeroconf]` and a running Avahi service |
| Device mounting and LUKS unlocking | `gnome-base/gvfs[udisks]` and a working UDisks2 service |
| Media previews and metadata | `media-video/ffmpeg` and, for video filmstrips, `media-video/ffmpegthumbnailer[png]` |
| PDF previews and metadata | `app-text/poppler[utils]` |
| Encrypted 7z archives | `app-arch/7zip` |
| WebP, AVIF/HEIF and JPEG XL images | `gui-libs/gdk-pixbuf-loader-webp`, `media-libs/libheif[gdk-pixbuf]` and `media-libs/libjxl[gdk-pixbuf]`, respectively |

[Upstream packaging notes on main](https://github.com/noctalia-dev/luciole/blob/main/PACKAGING.md)
