#!/usr/bin/env bash
#
# Cross-compile pydantic-core 2.46.5 for Chaquopy (Android Python 3.13, minSdk 24)
# and drop the wheels into android/pip-repo/:
#
#   pydantic_core-2.46.5-0-cp313-cp313-android_24_arm64_v8a.whl
#   pydantic_core-2.46.5-0-cp313-cp313-android_24_x86_64.whl
#
# Tested on Windows 11 + Git Bash. Prerequisites:
#   - rustup-managed Rust with cargo
#   - uv (https://docs.astral.sh/uv/)
#   - Android NDK (default: E:/Android/Sdk/ndk/30.0.14904198, override with ANDROID_NDK_HOME)
#   - curl, unzip
#
# Everything is built in a temporary directory; nothing is written outside the
# repo except the tooling venv at android/scripts/.tooling-venv (cached, reused
# between runs) and cargo/rustup caches in the user profile.
#
set -euo pipefail

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
PIP_REPO="$REPO_ROOT/android/pip-repo"
TOOLING_VENV="$SCRIPT_DIR/.tooling-venv"

PYDANTIC_CORE_VERSION="2.46.5"
# Chaquopy target artifact providing libpython3.13.so + headers per ABI
# (https://repo1.maven.org/maven2/com/chaquo/python/target/)
CHAQUOPY_TARGET_VERSION="3.13.9-0"
ANDROID_API_LEVEL=24

NDK_DEFAULT="E:/Android/Sdk/ndk/30.0.14904198"
export ANDROID_NDK_HOME="${ANDROID_NDK_HOME:-$NDK_DEFAULT}"
NDK_LLVM_BIN="$ANDROID_NDK_HOME/toolchains/llvm/prebuilt/windows-x86_64/bin"

MATURIN="$TOOLING_VENV/Scripts/python.exe -m maturin"

# ABI table: cargo-ndk name | rust triple | clang target prefix | python ext arch | wheel abi tag
ABIS=(
  "arm64-v8a|aarch64-linux-android|aarch64|aarch64|arm64_v8a"
  "x86_64|x86_64-linux-android|x86_64|x86_64|x86_64"
)

log() { echo "[build_pydantic_core] $*"; }

# --------------------------------------------------------------------------
# 1. Tooling
# --------------------------------------------------------------------------
log "adding rust targets"
rustup target add aarch64-linux-android x86_64-linux-android

if ! command -v cargo-ndk >/dev/null 2>&1; then
  log "installing cargo-ndk"
  cargo install cargo-ndk --locked
fi

if [ ! -x "$TOOLING_VENV/Scripts/python.exe" ]; then
  log "creating tooling venv at $TOOLING_VENV"
  uv venv "$TOOLING_VENV" --python 3.13
  uv pip install --python "$TOOLING_VENV/Scripts/python.exe" "maturin>=1.10,<2"
fi

# --------------------------------------------------------------------------
# 2. Sources (PyPI sdist; the GitHub repo has no tags beyond v2.41.x)
# --------------------------------------------------------------------------
BUILD_DIR="$(mktemp -d /tmp/pydantic-core-build.XXXXXX)"
trap 'log "cleaning up $BUILD_DIR"; rm -rf "$BUILD_DIR"' EXIT
log "build dir: $BUILD_DIR"

SDIST_URL="https://files.pythonhosted.org/packages/af/f9/8a06bea35ef8daf588f707784c973a7046e0034c8d8cfb08828eeffb8b75/pydantic_core-2.46.5.tar.gz"
log "downloading pydantic-core $PYDANTIC_CORE_VERSION sdist"
curl -fsSL -o "$BUILD_DIR/sdist.tar.gz" "$SDIST_URL"
tar -xzf "$BUILD_DIR/sdist.tar.gz" -C "$BUILD_DIR"
SRC_DIR="$BUILD_DIR/pydantic_core-$PYDANTIC_CORE_VERSION"

# --------------------------------------------------------------------------
# 3. Chaquopy target artifacts (libpython3.13.so per ABI)
# --------------------------------------------------------------------------
for entry in "${ABIS[@]}"; do
  IFS='|' read -r NDK_ABI _ _ _ _ <<< "$entry"
  ZIP="$BUILD_DIR/target-$NDK_ABI.zip"
  URL="https://repo1.maven.org/maven2/com/chaquo/python/target/$CHAQUOPY_TARGET_VERSION/target-$CHAQUOPY_TARGET_VERSION-$NDK_ABI.zip"
  log "downloading Chaquopy target for $NDK_ABI"
  curl -fsSL -o "$ZIP" "$URL"
  mkdir -p "$BUILD_DIR/chaquopy-target/$NDK_ABI"
  unzip -q -o "$ZIP" -d "$BUILD_DIR/chaquopy-target/$NDK_ABI"
done

# --------------------------------------------------------------------------
# 4. Build per ABI
# --------------------------------------------------------------------------
mkdir -p "$PIP_REPO"

for entry in "${ABIS[@]}"; do
  IFS='|' read -r NDK_ABI RUST_TRIPLE CLANG_PREFIX PY_EXT_ARCH WHEEL_ABI <<< "$entry"

  log "building $NDK_ABI ($RUST_TRIPLE)"

  TARGET_LIB_DIR="$(cygpath -m "$BUILD_DIR/chaquopy-target/$NDK_ABI/jniLibs/$NDK_ABI")"

  # pyo3 cross-compilation config. maturin reads PYO3_CONFIG_FILE first and
  # skips interpreter discovery entirely; pyo3 links libpython on Android
  # (even with the extension-module feature), so lib_name/lib_dir are needed.
  PYO3_CFG="$BUILD_DIR/pyo3-android-$PY_EXT_ARCH.cfg"
  cat > "$PYO3_CFG" <<EOF
implementation=CPython
version=3.13
shared=true
abi3=false
lib_name=python3.13
lib_dir=$TARGET_LIB_DIR
ext_suffix=.cpython-313-$PY_EXT_ARCH-linux-android.so
pointer_width=64
suppress_build_script_link_lines=false
EOF

  # Base cross env from cargo-ndk (CC/AR/sysroot vars).
  source <(cargo ndk-env --target "$NDK_ABI" --platform "$ANDROID_API_LEVEL")

  # NOTE: the cargo-ndk linker wrapper binary panics on Windows when invoked
  # without the .exe suffix, and picks the wrong ABI when not driven through
  # `cargo ndk`. Point cargo straight at NDK clang with an explicit --target
  # instead. RUSTFLAGS must be per-target so host build scripts keep using the
  # normal MSVC linker.
  TRIPLE_UPPER="$(echo "$RUST_TRIPLE" | tr 'a-z-' 'A-Z_')"
  export "CARGO_TARGET_${TRIPLE_UPPER}_LINKER=$NDK_LLVM_BIN/clang.exe"
  export "CARGO_TARGET_${TRIPLE_UPPER}_RUSTFLAGS=-C link-arg=--target=${CLANG_PREFIX}-linux-android${ANDROID_API_LEVEL} -C link-arg=-static-libgcc"
  unset RUSTFLAGS

  export PYO3_CONFIG_FILE="$(cygpath -m "$PYO3_CFG")"
  export PYO3_CROSS_LIB_DIR="$TARGET_LIB_DIR"
  export ANDROID_API_LEVEL

  (cd "$SRC_DIR" && $MATURIN build --release --target "$RUST_TRIPLE" --out "dist/$NDK_ABI")

  # maturin already emits the correct cp313-cp313-android_24_<abi> tag;
  # only the optional build tag "0" is missing from the file name.
  SRC_WHL="$SRC_DIR/dist/$NDK_ABI/pydantic_core-$PYDANTIC_CORE_VERSION-cp313-cp313-android_${ANDROID_API_LEVEL}_${WHEEL_ABI}.whl"
  DST_WHL="$PIP_REPO/pydantic_core-$PYDANTIC_CORE_VERSION-0-cp313-cp313-android_${ANDROID_API_LEVEL}_${WHEEL_ABI}.whl"
  cp "$SRC_WHL" "$DST_WHL"
  log "wrote $DST_WHL"
done

# --------------------------------------------------------------------------
# 5. Verify
# --------------------------------------------------------------------------
for entry in "${ABIS[@]}"; do
  IFS='|' read -r NDK_ABI RUST_TRIPLE _ PY_EXT_ARCH WHEEL_ABI <<< "$entry"
  WHL="$PIP_REPO/pydantic_core-$PYDANTIC_CORE_VERSION-0-cp313-cp313-android_${ANDROID_API_LEVEL}_${WHEEL_ABI}.whl"
  SO="pydantic_core/_pydantic_core.cpython-313-$PY_EXT_ARCH-linux-android.so"

  log "verifying $WHL"
  "$TOOLING_VENV/Scripts/python.exe" -m zipfile -l "$WHL" | grep -q "$SO" \
    || { echo "ERROR: $SO missing from $WHL"; exit 1; }
  unzip -p "$WHL" "pydantic_core-$PYDANTIC_CORE_VERSION.dist-info/WHEEL" | grep -q "Tag: cp313-cp313-android_${ANDROID_API_LEVEL}_${WHEEL_ABI}" \
    || { echo "ERROR: wrong Tag in $WHL"; exit 1; }

  # .so must not depend on libgcc_s.so.1 (Chaquopy runtime lacks it)
  mkdir -p "$BUILD_DIR/check/$NDK_ABI"
  unzip -p "$WHL" "$SO" > "$BUILD_DIR/check/$NDK_ABI/ext.so"
  NEEDED="$("$NDK_LLVM_BIN/llvm-readelf.exe" -d "$BUILD_DIR/check/$NDK_ABI/ext.so" | grep NEEDED || true)"
  echo "$NEEDED"
  if echo "$NEEDED" | grep -q "libgcc_s"; then
    echo "ERROR: $NDK_ABI .so still depends on libgcc_s"
    exit 1
  fi
done

log "done. wheels are in $PIP_REPO"
