# Copyright 2026 Gentoo Authors
# Distributed under the terms of the GNU General Public License v2

EAPI=8

PYTHON_COMPAT=( python3_{11..15} )
inherit meson python-any-r1 xdg

MY_COMMIT="94db0a89bbacd2480f4d890766d3603f866b3755"

DESCRIPTION="Native Wayland file manager using the Noctalia UI library"
HOMEPAGE="https://github.com/noctalia-dev/luciole"
SRC_URI="https://github.com/noctalia-dev/luciole/archive/${MY_COMMIT}.tar.gz -> ${PN}-${MY_COMMIT}.tar.gz"
S="${WORKDIR}/${PN}-${MY_COMMIT}"

LICENSE="MIT HPND"
SLOT="0"
KEYWORDS="~amd64"
IUSE="test"
RESTRICT="!test? ( test )"

COMMON_DEPEND="
	app-arch/libarchive:=[lzma]
	app-arch/xz-utils:=
	>=dev-cpp/tomlplusplus-3.4:=
	>=dev-libs/glib-2.76:2
	>=dev-libs/libxml2-2.9:2=
	dev-libs/wayland:0
	gnome-base/librsvg:2
	media-libs/fontconfig:1.0
	media-libs/freetype:2
	media-libs/harfbuzz:=
	media-libs/libglvnd
	media-libs/libpng:0=
	>=net-misc/curl-7.85:=[ssl]
	virtual/acl:=
	x11-libs/cairo
	>=x11-libs/gdk-pixbuf-2.42:2[jpeg]
	x11-libs/libxkbcommon
	>=x11-libs/pango-1.50
"
RDEPEND="
	${COMMON_DEPEND}
	gnome-base/dconf
	gnome-base/gsettings-desktop-schemas
	gnome-base/gvfs
	sys-apps/dbus
	virtual/ttf-fonts
	x11-misc/shared-mime-info
	x11-themes/adwaita-icon-theme
	|| (
		media-libs/mesa[opengl,wayland]
		x11-drivers/nvidia-drivers[wayland(-)]
	)
"
DEPEND="
	${COMMON_DEPEND}
	dev-cpp/nlohmann_json
	>=dev-libs/stb-20240201
	>=dev-libs/wayland-protocols-1.49
	test? (
		gui-libs/gdk-pixbuf-loader-webp
		media-libs/libheif[gdk-pixbuf,aom]
		media-libs/libjxl[gdk-pixbuf]
		media-libs/libwebp:=
	)
"
BDEPEND="
	dev-util/wayland-scanner
	virtual/pkgconfig
	test? (
		${PYTHON_DEPS}
		app-arch/7zip
		app-text/poppler[utils]
		dev-util/glib-utils
		>=media-video/ffmpeg-6.1[libaom,webp,x264]
		media-video/ffmpegthumbnailer[png]
		sys-apps/dbus
	)
"

src_configure() {
	local emesonargs=(
		-Dnative_optimizations=false
		-Dbenchmarks=false
		-Dbuild_revision="${MY_COMMIT}"
		$(meson_feature test tests)
	)
	meson_src_configure
}

src_install() {
	meson_src_install
	dodoc lib/protocols/text-input-unstable-v3.xml
}
