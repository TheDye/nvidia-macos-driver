#!/bin/bash
set -euo pipefail
N=$HOME/nvmtl-build; S=$N/rebase263/mesa; B=$S/build263; L=$N/build.lock; O=$N/rebase263/out
R=$(cd "$(dirname "$0")/.." && pwd)
export PATH="$N/rebase263/py/bin:$N/rebase263/bin:$HOME/tools/glslang/bin:$HOME/bin:$HOME/.cargo/bin:/usr/local/bin:/opt/homebrew/bin:$PATH"
# pip --user's script directory follows the selected Python, not a fixed 3.9.
export PATH="$(python3 -m site --user-base)/bin:$PATH"
if command -v brew >/dev/null 2>&1; then
  LLVM_PREFIX=$(brew --prefix llvm 2>/dev/null || true)
  if [ -d "$LLVM_PREFIX" ]; then
    export PATH="$PATH:$LLVM_PREFIX/bin"
    export LIBCLANG_PATH="${LIBCLANG_PATH:-$LLVM_PREFIX/lib}"
  fi
fi
python3 -c 'import mako, yaml, packaging.version' || { echo "STOP: python3 needs mako, pyyaml and packaging (pip install --user)"; exit 2; }
for tool in meson ninja python3 clang rustc bindgen cbindgen glslangValidator pkg-config; do
  command -v "$tool" >/dev/null 2>&1 || { echo "STOP: missing build dependency: $tool"; exit 2; }
done
[ -f "$S/VERSION" ] || { echo "STOP: no source tree at $S"; exit 2; }
[ ! -e "$S/.git" ] || { echo "STOP: $S has a .git (mesa would stamp git-<sha> into the binary)"; exit 2; }
until mkdir "$L" 2>/dev/null; do echo "waiting for build.lock ($(cat "$L/owner" 2>/dev/null || true))"; sleep 10; done
echo "build263 $$" > "$L/owner"
trap 'if [ -n "${T:-}" ]; then rm -f "$T" "$T.self"; fi; rm -f "$L/owner"; rmdir "$L"' EXIT

# Mesa uses a source-relative SDK path. Never replace an existing checkout/link.
SDK=$S/src/nouveau/vulkan/nvkmd/nvrm/open-gpu-kernel-modules
if [ ! -e "$SDK" ] && [ ! -L "$SDK" ]; then
  [ -f "$N/ogkm610/src/common/sdk/nvidia/inc/nvtypes.h" ] || { echo "STOP: OGKM 610 SDK missing at $N/ogkm610"; exit 2; }
  ln -s "$N/ogkm610" "$SDK"
fi
[ -f "$SDK/src/common/sdk/nvidia/inc/nvtypes.h" ] || { echo "STOP: invalid OGKM SDK path: $SDK"; exit 2; }

common=(-Dbuildtype=debugoptimized -Dgallium-drivers= -Dplatforms= -Degl=disabled -Dglx=disabled
  -Dglvnd=disabled -Dshared-glapi=disabled -Dvulkan-layers= -Dtools= -Dbuild-tests=false
  -Dexpat=disabled -Dzstd=disabled -Dlibunwind=disabled -Dlmsensors=disabled -Dvalgrind=disabled
  -Dxlib-lease=disabled -Dgallium-rusticl=false)
# Bootstrap the matching shader tools locally if absent. Keep LLVM/SPIRV-Tools
# in this host-only build, not the distributable NVK dylib. No meson install.
if ! command -v mesa_clc >/dev/null 2>&1 || ! command -v vtn_bindgen2 >/dev/null 2>&1; then
  H=$S/build-clc263
  setup=(setup); [ ! -f "$H/build.ninja" ] || setup=(setup --reconfigure)
  meson "${setup[@]}" "$H" "$S" "${common[@]}" -Dvulkan-drivers= -Dllvm=enabled \
    -Dmesa-clc=enabled -Dinstall-mesa-clc=true -Dspirv-tools=enabled > "$N/rebase263/setup-clc.log" 2>&1 \
    || { echo "STOP: shader-tool setup failed"; tail -30 "$N/rebase263/setup-clc.log"; exit 3; }
  nice ninja -C "$H" -j "${NVMTL_BUILD_JOBS:-4}" src/compiler/clc/mesa_clc src/compiler/spirv/vtn_bindgen2 \
    > "$N/rebase263/ninja-clc.log" 2>&1 \
    || { echo "STOP: shader-tool build failed"; tail -40 "$N/rebase263/ninja-clc.log"; exit 4; }
  export PATH="$H/src/compiler/clc:$H/src/compiler/spirv:$PATH"
fi
# Only the distributable runtime targets the package's oldest supported macOS.
# Keep the host shader tools above on the host SDK/default deployment target.
export MACOSX_DEPLOYMENT_TARGET=15.5
setup=(setup); [ ! -f "$B/build.ninja" ] || setup=(setup --reconfigure)
meson "${setup[@]}" "$B" "$S" "${common[@]}" -Dvulkan-drivers=nouveau -Dllvm=disabled \
  -Dmesa-clc=system -Dspirv-tools=disabled > "$N/rebase263/setup.log" 2>&1 \
  || { echo "STOP: meson setup failed"; tail -30 "$N/rebase263/setup.log"; exit 3; }
# Explicit arguments also invalidate cached objects from a newer host-default
# target; Rust's deployment target comes from the environment on this rebuild.
meson configure "$B" "-Dc_args=-mmacosx-version-min=15.5 -ffile-prefix-map=$S=/src -ffile-prefix-map=$HOME=/build" "-Dcpp_args=-mmacosx-version-min=15.5 -ffile-prefix-map=$S=/src -ffile-prefix-map=$HOME=/build" "-Dc_link_args=-mmacosx-version-min=15.5" "-Dcpp_link_args=-mmacosx-version-min=15.5" "-Drust_args=--remap-path-prefix=$S=/src --remap-path-prefix=$HOME=/build -C link-arg=-mmacosx-version-min=15.5" > "$N/rebase263/configure.log" 2>&1 \
  || { echo "STOP: meson configure failed"; tail -10 "$N/rebase263/configure.log"; exit 3; }
rc=0
nice ninja -C "$B" -j "${NVMTL_BUILD_JOBS:-4}" -k 0 src/nouveau/vulkan/libvulkan_nouveau.dylib > "$N/rebase263/ninja.log" 2>&1 || rc=$?
echo "ninja rc=$rc  errors=$(grep -c -E '(^|:[[:space:]])(fatal )?error(\[E[0-9]+\])?:' "$N/rebase263/ninja.log" || true)  FAILED=$(grep -c '^FAILED' "$N/rebase263/ninja.log" || true)"
[ "$rc" = 0 ] || { tail -40 "$N/rebase263/ninja.log"; exit 4; }
check_symbols() {
  local unresolved
  unresolved=$(nm -m "$1" | awk '/\(undefined\)/ && !/weak/ && /dynamically looked up/')
  [ -z "$unresolved" ] || { echo "STOP: unresolved strong references (dlopen fails in WindowServer):"; echo "$unresolved"; return 5; }
}
check_runtime() {
  python3 - "$1" <<'PY'
import re, subprocess, sys
path = sys.argv[1]
arch = subprocess.check_output(["lipo", "-archs", path], text=True).strip()
load = subprocess.check_output(["otool", "-l", path], text=True)
versions = re.findall(r"\bminos\s+(\d+(?:\.\d+)*)", load)
if arch != "x86_64" or len(versions) != 1:
    sys.exit(f"STOP: unexpected runtime architecture/deployment metadata: {arch}, {versions}")
version = tuple(map(int, versions[0].split(".")))
version += (0,) * (3 - len(version))
if version > (15, 5, 0):
    sys.exit(f"STOP: runtime minos {versions[0]} exceeds package minimum macOS 15.5")
print(f"runtime compatible: {arch}, minos {versions[0]}")
PY
}
check_symbols "$B/src/nouveau/vulkan/libvulkan_nouveau.dylib"
check_runtime "$B/src/nouveau/vulkan/libvulkan_nouveau.dylib"
mkdir -p "$O"; D=$O/libvulkan_nouveau.dylib
# Preserve the previous verified output on ANY fixup/sign/check failure. Stage
# on the same filesystem so publication is a single atomic rename, never a copy.
T=$(mktemp "$O/.libvulkan_nouveau.XXXXXX")
cp "$B/src/nouveau/vulkan/libvulkan_nouveau.dylib" "$T"
strip -S "$T"   # debug-map stabs name every .o by absolute path
# Reviewed AUTO contract: ONLY the exact qualified raw Mesa build may bind these
# weak hidden generated placeholders module-private (matching upstream ELF intent).
# Host/dependency collisions intentionally stop interposing; this is NOT generic
# Mach-O semantic equivalence. Physical qualification is macOS 26 only, not 15.5.
qualified_raw_sha=ec60a89dcb54fb65e9edcdb2ca649b4db226170c7a0aa5d1991e8d89da9d0779
raw_sha=$(shasum -a 256 "$B/src/nouveau/vulkan/libvulkan_nouveau.dylib" | awk '{print $1}')
stripped_sha=$(shasum -a 256 "$T" | awk '{print $1}')
mode=${NVMTL_NVK_SELF_BIND:-auto}
rewrite=0
case "$mode" in
  auto)
    if [ "$raw_sha" = "$qualified_raw_sha" ]; then
      rewrite=1
      echo "AUTO: qualified raw SHA; accepting private hidden generated namespace contract"
    else
      echo "WARNING: AUTO unknown raw SHA $raw_sha; preserving original flat bindings" >&2
    fi ;;
  0) echo "preserving original flat bindings (SELF optimization disabled)" ;;
  1) rewrite=1; echo "WARNING: explicit experimental SELF/no-interposition contract; unknown builds require requalification" >&2 ;;
  *) echo "STOP: NVMTL_NVK_SELF_BIND must be auto, 0 or 1"; exit 6 ;;
esac
if [ "$rewrite" = 1 ]; then
  # Nonempty even for unknown explicit1 inputs: macOS bash3 + nounset rejects
  # expanding an empty array (and can report status0 instead of failing closed).
  expected=(--verify-rewrite)
  [ "$raw_sha" != "$qualified_raw_sha" ] || expected+=(--expected-opcode-offset 18340120)
  python3 "$R/tools/nvk_flat_bind_fix.py" --nvk-generated-dir "$B" --accept-no-interposition "$T" "$T.self" \
    || { echo "STOP: flat-namespace rebind/provenance validation failed"; exit 6; }
  python3 "$R/tools/nvk_flat_bind_fix.py" "${expected[@]}" --nvk-generated-dir "$B" --accept-no-interposition "$T" "$T.self" \
    || { echo "STOP: unexpected binding opcode diff; refusing publication"; exit 6; }
  mv "$T.self" "$T"
  echo "qualification: macOS 26 only; macOS 15.5 dyld unqualified (minos metadata is not runtime evidence)"
fi
echo "qualification-input: mode=$mode SELF=$rewrite raw_sha256=$raw_sha stripped_sha256=$stripped_sha"
codesign --force --sign - "$T" || { echo "STOP: code signing failed"; exit 6; }
check_symbols "$T"
check_runtime "$T"
codesign --verify --strict --verbose=2 "$T"
otool -L "$T"
final_sha=$(shasum -a 256 "$T" | awk '{print $1}')
mv "$T" "$D"
T=
echo "built: sha256=$final_sha  uuid $(otool -l "$D" | awk '/uuid/{print $2; exit}')  $D"
