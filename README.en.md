# Luciole overlay

[Русский](README.md)

This overlay packages Luciole, a native Wayland file manager, as
`gui-apps/luciole-0.1.0_pre20261124203731` for `~amd64`. The ebuild pins
upstream commit `5505f60d28a3919d4e0ec25d61db040e82151f0c`.

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

[Upstream packaging notes](https://github.com/noctalia-dev/luciole/blob/5505f60d28a3919d4e0ec25d61db040e82151f0c/PACKAGING.md)
