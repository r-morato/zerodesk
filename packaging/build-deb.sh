#!/usr/bin/env bash
# Build build/zerodesk_<version>-<rev>_arm64.deb. Pure Python: no cross-compiler.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PKG=zerodesk
REV="${REV:-1}"
ARCH="${ARCH:-arm64}"
MAINTAINER="${MAINTAINER:-Roger Morato <64442406+r-morato@users.noreply.github.com>}"
VERSION="$(python3 -c "import sys; sys.path.insert(0, '$ROOT'); import zerodesk; print(zerodesk.__version__)")"
FULLVER="${VERSION}-${REV}"
OUT="$ROOT/build"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
chmod 0755 "$STAGE"

APP="$STAGE/usr/share/APPLaunch"
LIB="$STAGE/usr/lib/$PKG"
install -d "$LIB/$PKG" "$LIB/web/vendor" "$APP/bin" "$APP/applications" "$APP/share/images" \
           "$STAGE/usr/lib/udev/rules.d" "$STAGE/usr/share/doc/$PKG" "$STAGE/DEBIAN"

install -m 0644 "$ROOT"/zerodesk/*.py "$LIB/$PKG/"
install -m 0644 "$ROOT/packaging/run.py" "$LIB/run.py"
install -m 0755 "$ROOT/packaging/zerodesk-askpass" "$LIB/zerodesk-askpass"
install -m 0644 "$ROOT"/web/*.html "$ROOT"/web/*.js "$ROOT"/web/*.css "$ROOT"/web/*.svg "$LIB/web/"
install -m 0644 "$ROOT"/web/vendor/* "$LIB/web/vendor/"
install -m 0755 "$ROOT/packaging/zerodesk.sh" "$APP/bin/$PKG"
install -m 0644 "$ROOT/packaging/zerodesk.desktop" "$APP/applications/$PKG.desktop"
install -m 0644 "$ROOT/packaging/zerodesk.png" "$APP/share/images/$PKG.png"
install -m 0644 "$ROOT/packaging/70-zerodesk.rules" "$STAGE/usr/lib/udev/rules.d/70-$PKG.rules"
install -m 0644 "$ROOT/LICENSE" "$STAGE/usr/share/doc/$PKG/copyright"

SIZE_KB="$(du -sk --exclude=DEBIAN "$STAGE" | cut -f1)"
cat > "$STAGE/DEBIAN/control" <<CTRL
Package: $PKG
Version: $FULLVER
Architecture: $ARCH
Maintainer: $MAINTAINER
Installed-Size: $SIZE_KB
Depends: python3 (>= 3.11), python3-aiohttp, python3-pil, python3-evdev, openssl, sudo
Section: admin
Priority: optional
Homepage: https://github.com/r-morato/zerodesk
Description: Control your CardputerZero from a browser on your network
 ZeroDesk runs an HTTPS web interface on your local network with a live
 view of the Cardputer's screen and keyboard, a shell, a file browser,
 an app launcher and system stats. Browsers pair with a one-time code
 shown on the device. Runs as your user, never as root.
CTRL

mkdir -p "$OUT"
DEB="$OUT/${PKG}_${FULLVER}_${ARCH}.deb"
dpkg-deb --root-owner-group -Zxz --build "$STAGE" "$DEB" >/dev/null
echo "$DEB"
