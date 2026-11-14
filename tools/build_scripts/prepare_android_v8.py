#!/usr/bin/env python3
"""Build and cache a matching V8 library, snapshot and Rust bindings for Android.

rusty_v8 publishes no prebuilt static libraries for Android targets, so the
Android build cannot use the default crate download: it 404s on GitHub with
"no downloader (deno, python, curl) could fetch the URL". Instead this helper
builds the V8 version pinned in the bridge's Cargo.lock from source and
supplies the resulting static library and generated Rust bindings together.
The embedded startup snapshot comes from that same build.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
import urllib.request
import zipfile

try:
    import fcntl
except ImportError:  # Windows: Gradle serializes task execution instead.
    fcntl = None

# Ensure all file reads/writes and subprocess streams on Windows use UTF-8
_orig_read_text = Path.read_text
_orig_write_text = Path.write_text

def _safe_read_text(self, *args, **kwargs):
    kwargs.setdefault("encoding", "utf-8")
    kwargs.setdefault("errors", "replace")
    return _orig_read_text(self, *args, **kwargs)

def _safe_write_text(self, *args, **kwargs):
    kwargs.setdefault("encoding", "utf-8")
    kwargs.setdefault("errors", "replace")
    return _orig_write_text(self, *args, **kwargs)

Path.read_text = _safe_read_text
Path.write_text = _safe_write_text

REPO_ROOT = Path(__file__).resolve().parents[2]
BRIDGE_LOCK = REPO_ROOT / "apps/flutter/native/operit-flutter-bridge/Cargo.lock"
IS_WINDOWS = sys.platform.startswith("win")
CACHE_ROOT = Path(os.environ["OPERIT_V8_CACHE_ROOT"]) if "OPERIT_V8_CACHE_ROOT" in os.environ else (Path("D:/b") if IS_WINDOWS else REPO_ROOT / "tools/release/work/android-v8")
HOST_TAG = {"win32": "windows-x86_64", "darwin": "darwin-x86_64"}.get(sys.platform, "linux-x86_64")
# rusty_v8's build script maps only the x86_64 and aarch64 android targets to
# GN cpus; armv7 would end up as "unknown".
TARGETS = {
    "aarch64-linux-android": "arm64",
    "x86_64-linux-android": "x64",
}
# Android permits JIT (iOS does not), so the optimizing tiers and WebAssembly
# stay enabled. The sandbox, pointer compression and caged heap stay disabled
# to keep both mobile builds on the same memory layout, avoiding their large
# aligned virtual-address reservations. Temporal is disabled because the
# published source crate omits Temporal's Rust vendor dependencies; current
# runtime plugins do not use Temporal. Intl stays on. The vendored Chromium
# build config asserts that Android builds pick a static analysis mode
# explicitly, and source builds need none of it.
GN_OPTIONS = {
    "v8_jitless": False,
    "v8_enable_webassembly": True,
    "v8_enable_sandbox": False,
    "v8_enable_pointer_compression": False,
    "cppgc_enable_caged_heap": False,
    "v8_enable_temporal_support": False,
    "use_custom_libcxx": True,
    "android_static_analysis": "off",
}
# Must stay >= Chromium's min_supported_sdk_version (23, 27 with ASAN) and
# match the app's minSdkVersion so the library loads on every supported device.
DEFAULT_API_LEVEL = 24
# The NDK version rusty_v8's build script would download itself, used only as
# a last resort when no local NDK matches.
NDK_FALLBACK_URL = "https://dl.google.com/android/repository/android-ndk-r26c-linux.zip"
# rusty_v8's build script hardcodes this API level in its NDK guard path.
NDK_GUARD_API_LEVEL = 24
# Pinned gn/ninja are resolved through these CIPD endpoints; the crate's own
# downloader opens raw connections that bypass proxy environment settings.
CIPD_RESOLVE_URL = ("https://chrome-infra-packages.appspot.com/_ah/api/repo/v1/"
                    "instance/resolve?package_name={}&version={}")
CIPD_INSTANCE_URL = ("https://chrome-infra-packages.appspot.com/_ah/api/repo/v1/"
                     "instance?package_name={}&instance_id={}")
ARTIFACTS = ("librusty_v8.a", "src_binding.rs", "args.gn")


def run(command, **kwargs):
    print("+ " + shlex.join(map(str, command)), file=sys.stderr, flush=True)
    kwargs.setdefault("check", True)
    if kwargs.get("text"):
        kwargs.setdefault("encoding", "utf-8")
        kwargs.setdefault("errors", "replace")
    return subprocess.run(list(map(str, command)), **kwargs)


def output(command):
    return run(command, capture_output=True, text=True, encoding="utf-8", errors="replace").stdout.strip()


def api_level():
    value = int(os.environ.get("OPERIT_ANDROID_V8_API_LEVEL", DEFAULT_API_LEVEL))
    if value < 23:
        raise RuntimeError(f"OPERIT_ANDROID_V8_API_LEVEL must be >= 23, got {value}")
    return value


def version_from_lock(lock=BRIDGE_LOCK):
    packages = tomllib.loads(lock.read_text())["package"]
    versions = {p["version"] for p in packages if p["name"] == "v8"}
    if len(versions) != 1:
        raise RuntimeError(f"Expected exactly one V8 version in {lock}: {versions}")
    return versions.pop()


def configuration(version, target, ndk=None):
    cpu = TARGETS[target]
    # target_os/target_cpu/v8_target_cpu/use_sysroot/treat_warnings_as_errors
    # are set by rusty_v8's build script for Android cross builds; they are
    # recorded here so a build-script change shows up as a cache mismatch.
    # The vendored Chromium config references android_ndk_version without
    # declaring it, and it must match the NDK feeding the build.
    gn = {"target_os": "android", "target_cpu": cpu, "v8_target_cpu": cpu,
          "use_sysroot": True, "treat_warnings_as_errors": False,
          "android_ndk_api_level": api_level(),
          "android_ndk_version": ndk_version(ndk), **GN_OPTIONS}
    return {"recipe": 1, "version": version, "target": target, "gn": gn}


def ndk_version(ndk):
    """Derive the android_ndk_version GN value from the NDK directory name."""
    if ndk is None:
        return "unknown"
    if re.fullmatch(r"\d+\.\d+\.\d+", ndk.name):
        return ndk.name
    match = re.search(r"r(\d+)([a-z])", ndk.name, re.IGNORECASE)
    if match:
        return f"r{match.group(1)}{match.group(2).lower()}"
    return ndk.name


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def check_gn(path, config):
    args = {}
    for line in path.read_text().splitlines():
        match = re.fullmatch(r"\s*(\w+)\s*=\s*(.*?)\s*", line)
        if match:
            args[match[1]] = match[2]
    for name, value in config["gn"].items():
        if args.get(name) != json.dumps(value):
            raise RuntimeError(f"V8 configuration mismatch: {name}={args.get(name)}, expected {value!r}")


def cache_valid(directory, config):
    try:
        manifest = json.loads((directory / "manifest.json").read_text())
        if manifest["configuration"] != config:
            return False
        if any(manifest["sha256"][name] != digest(directory / name) for name in ARTIFACTS):
            return False
        check_gn(directory / "args.gn", config)
        return True
    except (OSError, ValueError, KeyError, RuntimeError):
        return False


def store_artifacts(gn_out, directory, config):
    """Publish only a complete source build; never mix downloaded bindings and libraries."""
    check_gn(gn_out / "args.gn", config)
    directory.mkdir(parents=True, exist_ok=True)
    for name in ARTIFACTS:
        source = gn_out / ("obj/librusty_v8.a" if name == "librusty_v8.a" else name)
        temporary = directory / (name + ".tmp")
        shutil.copyfile(source, temporary)
        temporary.replace(directory / name)
    manifest = {"configuration": config,
                "sha256": {name: digest(directory / name) for name in ARTIFACTS}}
    temporary = directory / "manifest.json.tmp"
    temporary.write_text(json.dumps(manifest, indent=2) + "\n")
    temporary.replace(directory / "manifest.json")


def remove_path(path):
    """Delete a file, symlink, directory junction or directory tree."""
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def link_directory(link, target):
    """Create a symlink, or a directory junction on Windows (no privileges needed)."""
    link.parent.mkdir(parents=True, exist_ok=True)
    if IS_WINDOWS:
        run(["cmd", "/c", "mklink", "/J", str(link).replace("/", "\\"), str(target).replace("/", "\\")])
    else:
        link.symlink_to(target, target_is_directory=True)


def ndk_prebuilt(ndk):
    return ndk / "toolchains/llvm/prebuilt" / HOST_TAG


def ndk_toolchain_clang(ndk, api_level_value=NDK_GUARD_API_LEVEL):
    base = ndk_prebuilt(ndk) / "bin" / f"aarch64-linux-android{api_level_value}-clang++"
    for cand in (base, base.with_suffix(".cmd"), base.with_suffix(".exe")):
        if cand.is_file():
            return cand
    return base


def is_usable_ndk(ndk):
    return ndk_toolchain_clang(ndk).is_file() and ndk_toolchain_clang(ndk, api_level()).is_file()


def candidate_ndks():
    for name in ("OPERIT_ANDROID_V8_NDK", "ANDROID_NDK_HOME", "ANDROID_NDK_ROOT", "ANDROID_NDK"):
        if os.environ.get(name):
            yield Path(os.environ[name])
    sdk = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    homes = [Path(sdk) / "ndk"] if sdk else []
    homes.append(Path.home() / "Android" / "ndk")
    for home in homes:
        if home.is_dir():
            # Prefer the newest installed NDK.
            for entry in sorted(home.iterdir(), reverse=True):
                yield entry


def download_fallback_ndk(cache):
    if HOST_TAG != "linux-x86_64":
        raise RuntimeError(
            "No usable Android NDK found; set OPERIT_ANDROID_V8_NDK to an NDK r23+ "
            f"installation with toolchains/llvm/prebuilt/{HOST_TAG}")
    directory = cache / "android-ndk-r26c"
    if not is_usable_ndk(directory):
        archive = cache / "android-ndk-r26c-linux.zip"
        cache.mkdir(parents=True, exist_ok=True)
        print(f"Downloading fallback NDK: {NDK_FALLBACK_URL}", file=sys.stderr, flush=True)
        with urllib.request.urlopen(NDK_FALLBACK_URL, timeout=1800) as response, \
                archive.open("wb") as file:
            shutil.copyfileobj(response, file)
        remove_path(directory)
        run(["unzip", "-d", cache, "-o", "-q", archive])
        archive.unlink()
    return directory


def find_ndk(cache):
    for candidate in candidate_ndks():
        if is_usable_ndk(candidate):
            return candidate.resolve()
    return download_fallback_ndk(cache)


def place_ndk(source, ndk):
    """Expose the NDK inside the V8 crate source where it is looked up.

    rusty_v8's build script probes third_party/android_ndk (hardcoded
    linux-x86_64 guard path) before downloading a Linux-only NDK zip, and the
    vendored Chromium config defaults android_ndk_root to
    third_party/android_toolchain/ndk. Both get junctions for the host prebuilt
    and for the hardcoded guard path so one NDK serves every consumer.
    """
    for destination in (source / "third_party/android_ndk",
                        source / "third_party/android_toolchain/ndk"):
        if ndk_toolchain_clang(destination).is_file() \
                and ndk_toolchain_clang(destination, api_level()).is_file():
            continue
        remove_path(destination)
        prebuilt = destination / "toolchains/llvm/prebuilt"
        for name in sorted({HOST_TAG, "linux-x86_64"}):
            remove_path(prebuilt / name)
            link_directory(prebuilt / name, ndk_prebuilt(ndk))


def patch_android_config(source):
    """Declare android_ndk_version and support Windows hosts in the vendored Chromium build config."""
    # 1. Allow Windows host in all GN build configs (e.g., BUILDCONFIG.gn, toolchain.gni, etc.)
    for gnf in source.rglob("*.gn*"):
        if not gnf.is_file():
            continue
        try:
            content = gnf.read_text()
            modified = False

            # Allow Windows host in BUILDCONFIG.gn
            if 'assert(host_os == "linux"' in content:
                content = re.sub(
                    r'assert\(host_os == "linux",\s*"Android builds are only supported on Linux\."\)',
                    '# assert(host_os == "linux", "Android builds are only supported on Linux.")',
                    content
                )
                modified = True

            # Allow Windows host in assertions checking is_win (such as in build/toolchain/win)
            if 'assert(is_win' in content:
                content = re.sub(
                    r'assert\(\s*is_win\b([^\)]*)\)',
                    r'assert((is_win || host_os == "win")\1)',
                    content
                )
                modified = True

            # Allow Windows host cross-compilation in snapshot_toolchain.gni
            if 'is_win && v8_current_cpu == "arm64"' in content:
                content = content.replace(
                    'is_win && v8_current_cpu == "arm64"',
                    '(is_win || host_os == "win") && v8_current_cpu == "arm64"'
                )
                modified = True

            # Ensure Clang toolchain binaries append .exe when host_os == "win"
            if 'cxx = "${prefix}/clang++"' in content and '_tool_ext' not in content:
                content = content.replace(
                    'cc = "${prefix}/clang"',
                    '_tool_ext = ""\n    if (host_os == "win") {\n      _tool_ext = ".exe"\n    }\n    cc = "${prefix}/clang${_tool_ext}"'
                )
                content = content.replace('cxx = "${prefix}/clang++"', 'cxx = "${prefix}/clang++${_tool_ext}"')
                content = content.replace('ar = "${prefix}/llvm-ar"', 'ar = "${prefix}/llvm-ar${_tool_ext}"')
                content = content.replace('readelf = "${prefix}/llvm-readelf"', 'readelf = "${prefix}/llvm-readelf${_tool_ext}"')
                content = content.replace('nm = "${prefix}/llvm-nm"', 'nm = "${prefix}/llvm-nm${_tool_ext}"')
                modified = True

            if 'cxx = "${toolprefix}clang++"' in content and '_tool_ext' not in content:
                content = content.replace(
                    'cc = "${toolprefix}clang"',
                    '_tool_ext = ""\n    if (host_os == "win") {\n      _tool_ext = ".exe"\n    }\n    cc = "${toolprefix}clang${_tool_ext}"'
                )
                content = content.replace('cxx = "${toolprefix}clang++"', 'cxx = "${toolprefix}clang++${_tool_ext}"')
                content = content.replace('ar = "${toolprefix}llvm-ar"', 'ar = "${toolprefix}llvm-ar${_tool_ext}"')
                content = content.replace('readelf = "${toolprefix}llvm-readelf"', 'readelf = "${toolprefix}llvm-readelf${_tool_ext}"')
                content = content.replace('nm = "${toolprefix}llvm-nm"', 'nm = "${toolprefix}llvm-nm${_tool_ext}"')
                modified = True

            if 'cc = "$_prefix/clang"' in content and '_prefix_ext' not in content:
                content = content.replace(
                    'cc = "$_prefix/clang"',
                    '_prefix_ext = ""\n    if (host_os == "win") {\n      _prefix_ext = ".exe"\n    }\n    cc = "$_prefix/clang${_prefix_ext}"'
                )
                content = content.replace('cxx = "$_prefix/clang++"', 'cxx = "$_prefix/clang++${_prefix_ext}"')
                content = content.replace('ar = "$_prefix/llvm-ar"', 'ar = "$_prefix/llvm-ar${_prefix_ext}"')
                modified = True

            if 'strip = rebase_path("$clang_base_path/bin/llvm-strip"' in content and '_strip_ext' not in content:
                content = content.replace(
                    'strip = rebase_path("$clang_base_path/bin/llvm-strip"',
                    '_strip_ext = ""\n    if (host_os == "win") {\n      _strip_ext = ".exe"\n    }\n    strip = rebase_path("$clang_base_path/bin/llvm-strip${_strip_ext}"'
                )
                modified = True

            if modified:
                print(f"Patched GN config: {gnf.relative_to(source)}", file=sys.stderr, flush=True)
                gnf.write_text(content)
        except Exception as e:
            print(f"Warning: could not patch {gnf}: {e}", file=sys.stderr, flush=True)

    # 2. Allow Windows host in build/config/android/config.gni
    config = source / "build/config/android/config.gni"
    if config.is_file():
        cfg_content = config.read_text()
        target_host_os = '  } else if (host_os == "mac") {\n    android_host_os = "darwin"'
        repl_host_os = '  } else if (host_os == "mac") {\n    android_host_os = "darwin"\n  } else if (host_os == "win") {\n    android_host_os = "windows"'
        if target_host_os in cfg_content and 'android_host_os = "windows"' not in cfg_content:
            cfg_content = cfg_content.replace(target_host_os, repl_host_os)

        if "android_ndk_version" not in cfg_content:
            cfg_content += (
                "\n# Added by Operit2\'s prepare_android_v8.py: the vendored config\n"
                "# references android_ndk_version without declaring it upstream.\n"
                "declare_args() {\n"
                "  android_ndk_version = \"unknown\"\n"
                "}\n"
            )
        config.write_text(cfg_content)

    # 3. Define clang_x64_v8_arm64 in build/toolchain/win/BUILD.gn if missing
    win_build_gn = source / "build/toolchain/win/BUILD.gn"
    if win_build_gn.is_file():
        wbg_content = win_build_gn.read_text()
        if "x64_v8_arm64" not in wbg_content:
            wbg_content += (
                "\n# Added by Operit2 for Android ARM64 cross-compile on Windows host\n"
                "win_toolchains(\"x64_v8_arm64\") {\n"
                "  toolchain_arch = \"x64\"\n"
                "  clang_toolchain_prefix = \"clang_\"\n"
                "}\n"
            )
            print("Added clang_x64_v8_arm64 toolchain to build/toolchain/win/BUILD.gn", file=sys.stderr, flush=True)
            win_build_gn.write_text(wbg_content)


def patch_v8_build_rs(source):
    """Disable ccache/sccache in rusty_v8's build.rs.

    On Windows CI runners, Strawberry Perl's 32-bit ccache.exe gets detected
    by which("ccache"). It uses GetFullPathNameA with a 260-character MAX_PATH limit,
    which fails when resolving deep relative paths to V8 headers. Disabling
    ccache forces direct clang-cl execution, which natively supports long paths.
    """
    build_rs = source / "build.rs"
    if not build_rs.is_file():
        return
    content = build_rs.read_text()
    modified = False

    # 1. Neutralize cc_wrapper function
    new_content = re.sub(
        r"fn cc_wrapper\s*\([^)]*\)\s*\{[\s\S]*?\n\}",
        "fn cc_wrapper(_gn_args: &mut Vec<String>, _sccache_path: &Path) {\n  // Disabled by Operit2\n}",
        content,
    )
    if new_content != content:
        content = new_content
        modified = True

    # 2. Prevent checking ccache / sccache env and binary
    for old, new in (
        ('env::var_os("SCCACHE")', 'env::var_os("__OPERIT_DISABLED_SCCACHE__")'),
        ('env::var_os("CCACHE")', 'env::var_os("__OPERIT_DISABLED_CCACHE__")'),
        ('which("sccache")', 'which("__operit_disabled_sccache__")'),
        ('which("ccache")', 'which("__operit_disabled_ccache__")'),
    ):
        if old in content:
            content = content.replace(old, new)
            modified = True

    # 3. Use prebuilt src_binding.rs in build_binding() to avoid libclang failure on Windows host
    build_binding_needle = "fn build_binding() {"
    build_binding_hook = """fn build_binding() {
  let out_path = build_dir().join("gn_out").join("src_binding.rs");
  for cand in [
    get_dirs().root.join("gen/src_binding_release_aarch64-unknown-linux-gnu.rs"),
    get_dirs().root.join("gen/src_binding_release_aarch64-linux-android.rs"),
  ] {
    if cand.is_file() {
      if let Some(parent) = out_path.parent() {
        std::fs::create_dir_all(parent).ok();
      }
      std::fs::copy(&cand, &out_path).expect("Could not copy prebuilt src_binding.rs");
      println!("cargo:rustc-env=RUSTY_V8_SRC_BINDING_PATH={}", out_path.display());
      return;
    }
  }"""
    if build_binding_needle in content and "src_binding_release_aarch64-unknown-linux-gnu.rs" not in content:
        content = content.replace(build_binding_needle, build_binding_hook)
        modified = True

    if modified:
        build_rs.write_text(content)
        print("Patched rusty_v8 build.rs to disable ccache/sccache and use prebuilt bindings",
              file=sys.stderr, flush=True)


def stub_missing_pydeps(source):
    """Create empty placeholders for python dependency manifests the crate omits.

    The vendored build/android/BUILD.gn evaluates python_library targets whose
    pydeps_file manifests are not shipped in the published crate. They only
    carry data dependencies of test-only targets that a rusty_v8 build never
    compiles, so empty manifests let GN evaluate the build files.
    """
    build_android = source / "build/android"
    for match in re.finditer(r'pydeps_file = "([^"]+)"', (build_android / "BUILD.gn").read_text()):
        manifest = build_android / match.group(1)
        if manifest.is_file():
            continue
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text("# stub: omitted from the published rusty_v8 crate\n")


def install_linux_sysroot(source):
    """Install the host debian sysroot that GN's linux toolchains assert on.

    rusty_v8's build script only installs it for android x86_64 targets, but
    the aarch64 cross build's host snapshot toolchain (clang_x64_v8_arm64)
    needs the same amd64 sysroot.
    """
    if IS_WINDOWS or sys.platform == "darwin":
        return
    linux = source / "build/linux"
    if linux.is_dir() and any(linux.glob("debian_bullseye_amd64-sysroot")):
        return
    run([sys.executable, "build/linux/sysroot_scripts/install-sysroot.py",
         "--arch=amd64"], cwd=source)


def prepare_icu(source):
    deps = (source / "v8/DEPS").read_text()
    revision = re.search(r"/chromium/deps/icu\.git'\s*\+\s*'@'\s*\+\s*'([a-f0-9]{40})'", deps)
    if not revision:
        raise RuntimeError("Cannot find V8's pinned ICU revision")
    for variant in ("common", "android"):
        destination = source / "third_party/icu" / variant / "icudtl.dat"
        if destination.is_file() and destination.stat().st_size > 0:
            continue
        url = ("https://chromium.googlesource.com/chromium/deps/icu/+/"
               f"{revision[1]}/{variant}/icudtl.dat?format=TEXT")
        print(f"Fetching pinned ICU data: {variant} @ {revision[1]}", file=sys.stderr, flush=True)
        with urllib.request.urlopen(url, timeout=180) as response:
            data = base64.b64decode(response.read(), validate=True)
        if not data:
            raise RuntimeError("Downloaded empty ICU data")
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(".tmp")
        temporary.write_bytes(data)
        temporary.replace(destination)


def brew_prefix(formula):
    brew = shutil.which("brew") or next(
        (str(p) for p in (Path("/opt/homebrew/bin/brew"), Path("/usr/local/bin/brew")) if p.is_file()), None)
    if not brew:
        return None
    result = subprocess.run([brew, "--prefix", formula], capture_output=True, text=True)
    return Path(result.stdout.strip()) if result.returncode == 0 else None


def find_host_clang():
    """Locate a host clang for GN's host toolchain (None downloads Chromium's).

    System or distro clang installs often use the pre-16 compiler-runtime
    layout and miss libclang_rt.builtins.a at the path Chromium's toolchains
    compute, so the hermetic download is the reliable Linux default.
    OPERIT_ANDROID_V8_LLVM overrides it; macOS prefers Homebrew LLVM like the
    iOS helper.
    """
    if "OPERIT_ANDROID_V8_LLVM" in os.environ:
        llvm = Path(os.environ["OPERIT_ANDROID_V8_LLVM"])
        if not (llvm / "bin/clang").is_file():
            raise RuntimeError(f"OPERIT_ANDROID_V8_LLVM has no bin/clang: {llvm}")
        return llvm
    if sys.platform == "darwin":
        llvm = brew_prefix("llvm@23") or brew_prefix("llvm")
        if not llvm or not (llvm / "bin/clang").is_file():
            raise RuntimeError("Install V8 build tools: brew install llvm@23")
        return llvm
    return None


def find_libclang():
    """Locate libclang 21.1+ for the source bindings generation."""
    if IS_WINDOWS:
        directory = (Path(os.environ["OPERIT_LIBCLANG_DIR"])
                     if "OPERIT_LIBCLANG_DIR" in os.environ else None)
        if not directory or not (directory / "libclang.dll").is_file():
            raise RuntimeError("Set OPERIT_LIBCLANG_DIR to the directory containing libclang.dll (21.1+)")
        return directory
    pattern = "libclang.so*"
    if sys.platform == "darwin":
        llvm = find_host_clang()
        candidates = [llvm / "lib"]
    else:
        candidates = [Path("/usr/lib/llvm21/lib")]
        candidates += sorted(Path("/usr/lib").glob("llvm*/lib"), reverse=True)
        candidates += [Path("/usr/lib/x86_64-linux-gnu"), Path("/usr/lib64"), Path("/usr/lib")]
    libraries = []
    for directory in candidates:
        libraries += sorted(directory.glob(pattern))
    for library in libraries:
        match = re.search(r"libclang\.so\.(\d+)", library.name)
        if match and int(match.group(1)) >= 21:
            return library.parent
    if sys.platform == "darwin" and libraries:
        return libraries[0].parent
    raise RuntimeError(
        "V8 source bindings require LLVM/libclang 21.1 or newer "
        "(apt install llvm-21-dev / brew install llvm@23)")


def find_lld(clang_base):
    """Locate the host ld.lld used by Chromium's toolchain (not needed on Windows)."""
    if IS_WINDOWS:
        return None
    candidates = []
    if clang_base:
        candidates.append(clang_base / "bin/ld.lld")
    if sys.platform == "darwin":
        for formula in ("lld@23", "lld"):
            prefix = brew_prefix(formula)
            if prefix:
                candidates.append(prefix / "bin/ld.lld")
    candidates += sorted(Path("/usr/lib").glob("llvm*/bin/ld.lld"), reverse=True)
    candidates.append(Path("/usr/bin/ld.lld"))
    for candidate in candidates:
        if candidate.is_file():
            return candidate.parent
    raise RuntimeError("Install the LLVM linker: apt install lld / brew install lld@23")


def deps_version(source, name):
    """Read a pinned tool version from V8's DEPS (as the crate's v8_deps.py does)."""
    deps = (source / "v8/DEPS").read_text()
    match = re.search(rf"'{name}':\s*'([^']+)'", deps)
    if not match:
        raise RuntimeError(f"Cannot find V8's pinned {name}")
    return match.group(1)


def fetch_bytes(url, timeout):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.read()


def cipd_tool_tag():
    system = {"darwin": "mac", "win32": "windows"}.get(sys.platform, "linux")
    machine = "arm64" if platform.machine().lower() == "aarch64" else "amd64"
    return f"{system}-{machine}"


def extract_tool_archive(data, directory):
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        archive.extractall(directory)
        if not IS_WINDOWS:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                extracted = directory / info.filename
                mode = info.external_attr >> 16
                if mode:
                    os.chmod(extracted, os.stat(extracted).st_mode | mode)


def ensure_gn_ninja(cache, source):
    """Provision the pinned gn and ninja, returning their paths for GN/NINJA.

    rusty_v8's ninja_gn_binaries.py fetches the CIPD metadata over raw
    http.client connections that bypass proxy environment settings, so this
    resolves and downloads the same pinned packages with urllib instead.
    """
    tag = cipd_tool_tag()
    paths = {}
    for tool in ("gn", "ninja"):
        binary = f"{tool}.exe" if IS_WINDOWS else tool
        destination = cache / "tools" / tool / binary
        paths[tool] = destination
        if destination.is_file():
            continue
        package = f"gn/gn/{tag}" if tool == "gn" else f"infra/3pp/tools/ninja/{tag}"
        version = deps_version(source, f"{tool}_version")
        print(f"Fetching pinned {tool}: {version}", file=sys.stderr, flush=True)
        resolved = json.loads(fetch_bytes(CIPD_RESOLVE_URL.format(package, version), 300))
        instance = json.loads(fetch_bytes(CIPD_INSTANCE_URL.format(package, resolved["instance_id"]), 300))
        destination.parent.mkdir(parents=True, exist_ok=True)
        extract_tool_archive(fetch_bytes(instance["fetch_url"], 1800), destination.parent)
        if not destination.is_file():
            raise RuntimeError(f"CIPD archive did not contain {binary} ({package} {version})")
    return paths


def populate_clang_dir(clang_dir, ndk, source):
    """Ensure a clang directory has full compiler aliases, NDK llvm utilities, and builtins."""
    clang_dir = Path(clang_dir)
    bin_p = clang_dir / "bin"
    bin_p.mkdir(parents=True, exist_ok=True)

    # 1. Alias clang-cl.exe to clang.exe, clang++.exe, and non-exe aliases
    clang_cl = bin_p / "clang-cl.exe"
    if clang_cl.is_file():
        for target_name in ("clang.exe", "clang++.exe", "clang", "clang++"):
            dst = bin_p / target_name
            if not dst.exists():
                try:
                    shutil.copyfile(clang_cl, dst)
                    print(f"Created compiler alias: {dst.name} from clang-cl.exe", file=sys.stderr, flush=True)
                except Exception as e:
                    print(f"Warning: could not copy {clang_cl} to {dst}: {e}", file=sys.stderr, flush=True)

    # 2. Search NDK for llvm utilities (llvm-ar, llvm-nm, llvm-readelf, llvm-strip, lld, ld.lld)
    ndk_search_paths = [Path(ndk), Path(source) / "third_party/android_ndk", Path(source) / "third_party/android_toolchain/ndk"]
    sdk = os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT")
    if sdk:
        ndk_search_paths.append(Path(sdk) / "ndk")

    ndk_bin = None
    for sp in ndk_search_paths:
        if not sp.is_dir():
            continue
        candidates = list(sp.glob("toolchains/llvm/prebuilt/*/bin"))
        if candidates and candidates[0].is_dir():
            ndk_bin = candidates[0]
            break
        if (sp / "bin").is_dir():
            ndk_bin = sp / "bin"
            break

    if ndk_bin:
        for util in ("llvm-ar", "llvm-nm", "llvm-readelf", "llvm-strip", "lld", "ld.lld"):
            src_exe = ndk_bin / f"{util}.exe"
            if not src_exe.is_file():
                src_exe = ndk_bin / util
            if src_exe.is_file():
                for dst_name in (f"{util}.exe", util):
                    dst = bin_p / dst_name
                    if not dst.exists():
                        try:
                            shutil.copyfile(src_exe, dst)
                            print(f"Copied {dst_name} from NDK to clang bin", file=sys.stderr, flush=True)
                        except Exception as e:
                            print(f"Warning: could not copy {src_exe} to {dst}: {e}", file=sys.stderr, flush=True)

        for c_name in ("clang", "clang++"):
            for ext in (".exe", ""):
                dst = bin_p / f"{c_name}{ext}"
                if not dst.exists():
                    src_c = ndk_bin / f"{c_name}.exe"
                    if not src_c.is_file():
                        src_c = ndk_bin / c_name
                    if src_c.is_file():
                        try:
                            shutil.copyfile(src_c, dst)
                            print(f"Copied {dst.name} from NDK to clang bin", file=sys.stderr, flush=True)
                        except Exception as e:
                            print(f"Warning: could not copy {src_c} to {dst}: {e}", file=sys.stderr, flush=True)

    # 3. Create aliases without .exe for any other binaries
    if IS_WINDOWS:
        for exe in bin_p.glob("*.exe"):
            alias = exe.with_suffix("")
            if not alias.exists():
                try:
                    shutil.copyfile(exe, alias)
                except Exception as e:
                    pass

    # 4. Populate compiler-rt builtins for Android
    ndk_builtins = []
    for sp in ndk_search_paths:
        if sp.is_dir():
            ndk_builtins += [p for p in sp.rglob("libclang_rt.builtins*.a") if p.is_file()]

    clang_lib_dirs = list(clang_dir.glob("lib/clang/*/lib"))
    if not clang_lib_dirs:
        clang_lib_dirs = [clang_dir / "lib/clang/23/lib"]

    for clang_lib in clang_lib_dirs:
        dest_linux = clang_lib / "linux"
        dest_linux.mkdir(parents=True, exist_ok=True)
        for b in ndk_builtins:
            dest_file = dest_linux / b.name
            if not dest_file.exists():
                try:
                    shutil.copyfile(b, dest_file)
                    print(f"Copied builtin {b.name} -> {dest_file}", file=sys.stderr, flush=True)
                except Exception as e:
                    pass


def patch_clang_update_script(source, ndk):
    update_script = source / "tools/clang/scripts/update.py"
    if not update_script.is_file():
        return
    content = update_script.read_text()
    if "# Added by Operit2 for Android Clang & LLVM tools setup" in content:
        return
    ndk_clean = str(ndk).replace('\\', '/')
    hook = f'''  ret = UpdatePackage(args.package, args.host_os, args.preserve_gcs_signature,
                       output_dir)
  # Added by Operit2 for Android Clang & LLVM tools setup
  try:
    import shutil
    from pathlib import Path
    out_p = Path(output_dir)
    bin_p = out_p / "bin"
    bin_p.mkdir(parents=True, exist_ok=True)

    clang_cl = bin_p / "clang-cl.exe"
    if clang_cl.is_file():
      for target_name in ("clang.exe", "clang++.exe", "clang", "clang++"):
        dst = bin_p / target_name
        if not dst.exists():
          shutil.copyfile(clang_cl, dst)
          print(f"Created compiler alias: {{dst.name}} from clang-cl.exe")

    ndk_p = Path(r"{ndk_clean}")
    ndk_bin_candidates = list(ndk_p.glob("toolchains/llvm/prebuilt/*/bin"))
    if not ndk_bin_candidates and (ndk_p / "bin").is_dir():
      ndk_bin_candidates = [ndk_p / "bin"]
    if ndk_bin_candidates:
      ndk_bin = ndk_bin_candidates[0]
      for util in ("llvm-ar", "llvm-nm", "llvm-readelf", "llvm-strip", "lld", "ld.lld"):
        src_exe = ndk_bin / f"{{util}}.exe"
        if not src_exe.is_file():
          src_exe = ndk_bin / util
        if src_exe.is_file():
          for dst_name in (f"{{util}}.exe", util):
            dst = bin_p / dst_name
            if not dst.exists():
              shutil.copyfile(src_exe, dst)
              print(f"Copied {{dst_name}} from NDK to clang bin")

      for c_name in ("clang", "clang++"):
        for ext in (".exe", ""):
          dst = bin_p / f"{{c_name}}{{ext}}"
          if not dst.exists():
            src_c = ndk_bin / f"{{c_name}}.exe"
            if not src_c.is_file():
              src_c = ndk_bin / c_name
            if src_c.is_file():
              shutil.copyfile(src_c, dst)
              print(f"Copied {{dst.name}} from NDK to clang bin")

    for b in ndk_p.rglob("libclang_rt.builtins*.a"):
      if b.is_file():
        for cr in out_p.glob("lib/clang/*/lib"):
          dest_dir = cr / "linux"
          dest_dir.mkdir(parents=True, exist_ok=True)
          dst_b = dest_dir / b.name
          if not dst_b.exists():
            shutil.copyfile(b, dst_b)
            print(f"Copied builtin {{b.name}} -> {{dst_b}}")
  except Exception as e:
    print(f"Warning: could not complete clang/ndk setup in update.py: {{e}}")
  return ret'''

    # If old hook exists, remove it first
    if "# Added by Operit2 for Android compiler-rt builtins" in content:
        import re
        content = re.sub(
            r'  ret = UpdatePackage\(.*?\n  return ret',
            '  return UpdatePackage(args.package, args.host_os, args.preserve_gcs_signature,\n                       output_dir)',
            content,
            flags=re.DOTALL
        )

    targets = [
        "  return UpdatePackage(args.package, args.host_os, args.preserve_gcs_signature,\n                       output_dir)",
        "  return UpdatePackage(\n    args.package, args.host_os, args.preserve_gcs_signature, output_dir\n  )",
    ]
    for target in targets:
        if target in content:
            content = content.replace(target, hook)
            update_script.write_text(content)
            print("Patched tools/clang/scripts/update.py with full Clang & LLVM tools setup", file=sys.stderr, flush=True)
            return

    import re
    match = re.search(r'  return UpdatePackage\([^)]+\)', content)
    if match:
        content = content[:match.start()] + hook + content[match.end():]
        update_script.write_text(content)
        print("Patched tools/clang/scripts/update.py (via regex)", file=sys.stderr, flush=True)


def ensure_clang_builtins(source, cache, config, ndk):
    """Ensure Chromium clang has the required Android compiler-rt builtins and tools from NDK."""
    target_build_dir = (cache / "build").resolve() / config["target"] / "release"
    probe_target_dir = (cache / "probe/target").resolve() / config["target"] / "release"
    update_script = source / "tools/clang/scripts/update.py"

    for clang_dir in [probe_target_dir / "clang", target_build_dir / "clang"]:
        clang_dir.mkdir(parents=True, exist_ok=True)
        if update_script.is_file():
            print(f"Ensuring Chromium clang is downloaded to {clang_dir}", file=sys.stderr, flush=True)
            try:
                run([sys.executable, str(update_script), "--output-dir", str(clang_dir)])
            except Exception as e:
                print(f"Warning: update.py failed for {clang_dir}: {e}", file=sys.stderr, flush=True)
        populate_clang_dir(clang_dir, ndk, source)


def build(cache, config, ndk):
    probe = cache / "probe"
    (probe / "src").mkdir(parents=True, exist_ok=True)
    (probe / "Cargo.toml").write_text(
        '[package]\nname="operit-android-v8-build"\nversion="0.0.0"\nedition="2021"\n'
        '[lib]\ncrate-type=["staticlib"]\n[dependencies]\n'
        f'v8="={config["version"]}"\n[workspace]\n')
    (probe / "src/lib.rs").write_text('pub fn version() -> &\'static str { v8::V8::get_version() }\n')
    metadata = json.loads(output(["cargo", "metadata", "--manifest-path", probe / "Cargo.toml",
                                  "--format-version", "1"]))
    source = Path(next(p["manifest_path"] for p in metadata["packages"] if p["name"] == "v8")).parent
    if IS_WINDOWS:
        short_source = Path("D:/s")
        if short_source.exists() or short_source.is_symlink():
            try:
                os.rmdir(short_source)
            except OSError:
                run(["cmd", "/c", "rmdir", str(short_source).replace("/", "\\")], check=False)
        link_directory(short_source, source)
        source = short_source
        (probe / "Cargo.toml").write_text(
            '[package]\nname="operit-android-v8-build"\nversion="0.0.0"\nedition="2021"\n'
            '[lib]\ncrate-type=["staticlib"]\n[dependencies]\n'
            'v8 = { path = "D:/s" }\n[workspace]\n')
        cargo_lock = probe / "Cargo.lock"
        if cargo_lock.is_file():
            cargo_lock.unlink()

    patch_android_config(source)
    patch_v8_build_rs(source)
    patch_clang_update_script(source, ndk)
    stub_missing_pydeps(source)
    install_linux_sysroot(source)
    place_ndk(source, ndk)
    prepare_icu(source)
    gn_ninja = ensure_gn_ninja(cache, source)
    clang = find_host_clang()
    libclang = find_libclang()
    lld = find_lld(clang)
    ensure_clang_builtins(source, cache, config, ndk)
    try:
        run(["rustup", "target", "add", config["target"]], check=False)
    except OSError:
        print("rustup not found; assuming the Rust target is already installed",
              file=sys.stderr, flush=True)
    env = os.environ.copy()
    for name in ("DOCS_RS", "RUSTY_V8_ARCHIVE", "RUSTY_V8_SRC_BINDING_PATH", "GN_ARGS",
                 "EXTRA_GN_ARGS", "CARGO_ENCODED_RUSTFLAGS", "V8_FORCE_DEBUG", "CCACHE", "SCCACHE"):
        env.pop(name, None)
    env["CCACHE_DISABLE"] = "1"
    # Filter out Strawberry Perl and any directory containing ccache from PATH
    clean_path = []
    for p in env.get("PATH", "").split(os.pathsep):
        if not p or "strawberry" in p.lower():
            continue
        if (Path(p) / "ccache.exe").is_file() or (Path(p) / "ccache").is_file():
            continue
        clean_path.append(p)
    env["PATH"] = os.pathsep.join(clean_path)
    # GN_ARGS carries only what rusty_v8's build script does not set itself.
    args = {**GN_OPTIONS, "android_ndk_api_level": api_level(),
            "android_ndk_version": ndk_version(ndk), "android_ndk_root": str(ndk).replace("\\", "/")}
    if lld:
        args["lld_path"] = str(lld)
    env.update(V8_FROM_SOURCE="1", PYTHON=sys.executable,
               CARGO_TARGET_DIR=str((cache / "build").resolve()),
               GN=str(gn_ninja["gn"]), NINJA=str(gn_ninja["ninja"]),
               GN_ARGS=" ".join(f"{name}={json.dumps(value)}" for name, value in args.items()))
    # Pre-populate prebuilt src_binding.rs for Android ARM64
    gnu_binding = source / "gen/src_binding_release_aarch64-unknown-linux-gnu.rs"
    android_binding = source / "gen/src_binding_release_aarch64-linux-android.rs"
    if gnu_binding.is_file():
        if not android_binding.is_file():
            try:
                shutil.copyfile(gnu_binding, android_binding)
            except Exception:
                pass
        target_gn_out = (cache / "build").resolve() / config["target"] / "release/gn_out"
        target_gn_out.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copyfile(gnu_binding, target_gn_out / "src_binding.rs")
        except Exception:
            pass
        env["RUSTY_V8_SRC_BINDING_PATH"] = str(target_gn_out / "src_binding.rs")

    # Bindgen derives the triple from Cargo's TARGET but gets no sysroot, so it
    # falls back to the host's glibc headers, whose default wordsize makes
    # uintptr_t 32-bit and breaks the V8 headers. Point it at the NDK sysroot
    # via the lowercase spelling bindgen 0.72 reads (uppercase is ignored).
    sysroot = ndk_prebuilt(ndk) / "sysroot"
    sysroot_arg = str(sysroot).replace("\\", "/")
    for android_target in TARGETS:
        env[f"BINDGEN_EXTRA_CLANG_ARGS_{android_target.replace('-', '_')}"] = (
            f"--target={android_target}{api_level()} --sysroot={sysroot_arg}")
    if clang:
        env["CLANG_BASE_PATH"] = str(clang)
    env["LIBCLANG_PATH"] = str(libclang)
    cargo_cmd = ["cargo", "build", "--manifest-path", str(probe / "Cargo.toml"), "--release",
                 "--target", config["target"], "--jobs", os.environ.get("OPERIT_ANDROID_V8_JOBS", "6")]
    if not IS_WINDOWS:
        cargo_cmd.insert(2, "--locked")
    run(cargo_cmd, env=env)
    return (cache / "build").resolve() / config["target"] / "release/gn_out"


def prepare(target, env_file, cache_root=CACHE_ROOT):
    cache = cache_root / version_from_lock()
    cache.mkdir(parents=True, exist_ok=True)
    # Protect shared source downloads/toolchain placements as well as each output set.
    with (cache / "build.lock").open("a") as lock:
        if fcntl:
            fcntl.flock(lock, fcntl.LOCK_EX)
        ndk = find_ndk(cache)
        config = configuration(version_from_lock(), target, ndk)
        directory = cache / target
        if not cache_valid(directory, config):
            print(f"Building V8 {config['version']} for {target} (first build takes time)",
                  file=sys.stderr, flush=True)
            store_artifacts(build(cache, config, ndk), directory, config)
        else:
            print(f"Using verified V8 cache: {directory}", file=sys.stderr, flush=True)
        values = {"RUSTY_V8_ARCHIVE": str(directory / "librusty_v8.a"),
                  "RUSTY_V8_ARCHIVE_SHA256": digest(directory / "librusty_v8.a"),
                  "RUSTY_V8_SRC_BINDING_PATH": str(directory / "src_binding.rs")}
        env_file.parent.mkdir(parents=True, exist_ok=True)
        env_file.write_text("unset V8_FROM_SOURCE DOCS_RS V8_FORCE_DEBUG\n" + "".join(
            f"export {name}={shlex.quote(value)}\n" for name, value in values.items()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=TARGETS, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    args = parser.parse_args()
    prepare(args.target, args.env_file)


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"Android V8 preparation failed: {error}", file=sys.stderr)
        raise SystemExit(1)
