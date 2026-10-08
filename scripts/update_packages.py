#!/usr/bin/env python3
"""Update the opencode and openchamber packages to their latest releases.

Both come from the npm registry:

- opencode: the platform package @opencode/cli-linux-x64-baseline, which holds
  the binary alone. Its version is @opencode/cli's latest.
- openchamber: @openchamber/web and its production dependencies, one tarball
  each. npm resolves them into a lockfile here (nothing is installed), from
  which package/openchamber/npm-modules.list gets the dependencies' install
  paths and URLs, and openchamber.hash the registry's sha512 of every tarball.

Each package's LICENSE comes from its GitHub tag; its sha256 is computed here.

  scripts/update_packages.py                    # both, to their latest versions
  scripts/update_packages.py opencode           # one of them
  scripts/update_packages.py openchamber=2.1.1  # a specific version
  scripts/update_packages.py --check            # only report what is newer

Needs network access, and npm for openchamber. Afterwards: review the diff,
build, run `make test`, and update the sizes in README.md.
"""

import argparse
import base64
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
REGISTRY = "https://registry.npmjs.org/"

OPENCODE_PACKAGE = "@opencode/cli-linux-x64-baseline"
OPENCODE_LICENSE = "https://raw.githubusercontent.com/anomalyco/opencode/v{version}/LICENSE"

OPENCHAMBER_PACKAGE = "@openchamber/web"
OPENCHAMBER_LICENSE = "https://raw.githubusercontent.com/openchamber/openchamber/v{version}/LICENSE"
# OpenChamber's own container runs on this Bun; package/bun should follow it.
OPENCHAMBER_DOCKERFILE = "https://raw.githubusercontent.com/openchamber/openchamber/v{version}/Dockerfile"
# Dependencies left out, beyond the packages for other platforms (os/cpu):
OPENCHAMBER_SKIP = {
    # 32 MiB of ONNX runtime for local speech recognition and synthesis, which
    # also needs models downloaded at run time. Without it, sherpa-onnx-node
    # (kept, 50 KiB) reports local speech as unavailable.
    "sherpa-onnx-linux-x64",
}


def fetch(url):
    with urllib.request.urlopen(url) as r:
        return r.read()


def registry(path):
    return json.loads(fetch(REGISTRY + path))


def latest(package):
    return registry(package)["dist-tags"]["latest"]


def sha512_hex(integrity):
    """The hex digest of an npm integrity string, "sha512-<base64>"."""
    if not integrity.startswith("sha512-"):
        sys.exit(f"not a sha512 integrity: {integrity}")
    return base64.b64decode(integrity[len("sha512-"):]).hex()


def package_path(name, *parts):
    return os.path.join(ROOT, "package", name, *parts)


def current_version(name):
    with open(package_path(name, f"{name}.mk")) as f:
        return re.search(rf"^{name.upper()}_VERSION = (\S+)$", f.read(), re.M).group(1)


def set_version(name, version):
    path = package_path(name, f"{name}.mk")
    with open(path) as f:
        mk = f.read()
    mk, n = re.subn(rf"^({name.upper()}_VERSION = )\S+$", rf"\g<1>{version}", mk, flags=re.M)
    assert n == 1
    with open(path, "w") as f:
        f.write(mk)


def write(name, filename, text):
    with open(package_path(name, filename), "w") as f:
        f.write(text)


def update_opencode(version):
    tarball = registry(f"{OPENCODE_PACKAGE}/{version}")["dist"]
    license_hash = hashlib.sha256(fetch(OPENCODE_LICENSE.format(version=version))).hexdigest()
    write("opencode", "opencode.hash",
          f"# From the npm registry: the integrity field of {OPENCODE_PACKAGE}\n"
          f"# {version} (npm view {OPENCODE_PACKAGE}@{version} dist.integrity)\n"
          f"sha512  {sha512_hex(tarball['integrity'])}  cli-linux-x64-baseline-{version}.tgz\n"
          f"# Locally computed, from the LICENSE file at the v{version} tag\n"
          f"sha256  {license_hash}  LICENSE\n")
    set_version("opencode", version)


def openchamber_lockfile(version):
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "package.json"), "w") as f:
            json.dump({"name": "cherry-openchamber", "private": True}, f)
        subprocess.run(["npm", "install", "--package-lock-only", "--omit=dev", "--ignore-scripts",
                        "--no-audit", "--no-fund", "--save-exact", f"{OPENCHAMBER_PACKAGE}@{version}"],
                       cwd=tmp, check=True, stdout=subprocess.DEVNULL)
        with open(os.path.join(tmp, "package-lock.json")) as f:
            return json.load(f)


def update_openchamber(version):
    main_path = f"node_modules/{OPENCHAMBER_PACKAGE}"
    main_hash = None
    modules = []
    for path, info in openchamber_lockfile(version)["packages"].items():
        if not path or info.get("dev") or info.get("inBundle") or info.get("link"):
            continue
        if "linux" not in info.get("os", ["linux"]) or "x64" not in info.get("cpu", ["x64"]):
            continue
        url = info["resolved"]
        if not url.startswith(REGISTRY):
            sys.exit(f"{path}: not from the npm registry: {url}")
        digest = sha512_hex(info["integrity"])
        if path == main_path:
            main_hash = digest
        elif path.rsplit("node_modules/", 1)[1] not in OPENCHAMBER_SKIP:
            modules.append((path[len("node_modules/"):], url, digest))
    if not main_hash:
        sys.exit(f"{main_path} is not in the lockfile")
    modules.sort()
    # Buildroot keeps every download of the package in one directory.
    tarballs = {}
    for _, url, digest in modules:
        name = url.rsplit("/", 1)[1]
        if tarballs.setdefault(name, digest) != digest:
            sys.exit(f"two different tarballs would both be saved as {name}")
    license_hash = hashlib.sha256(fetch(OPENCHAMBER_LICENSE.format(version=version))).hexdigest()

    write("openchamber", "npm-modules.list",
          f"# The production dependencies of {OPENCHAMBER_PACKAGE} {version}: install path\n"
          "# below node_modules/, tarball URL. Generated by scripts/update_packages.py;\n"
          "# the hashes are in openchamber.hash.\n"
          + "".join(f"{path} {url}\n" for path, url, _ in modules))
    write("openchamber", "openchamber.hash",
          "# From the npm registry: the integrity field of each package version, as\n"
          "# npm's lockfile records it. Generated by scripts/update_packages.py.\n"
          f"sha512  {main_hash}  web-{version}.tgz\n"
          f"# Locally computed, from the LICENSE file at the v{version} tag\n"
          f"sha256  {license_hash}  LICENSE\n"
          "# The dependencies, see npm-modules.list\n"
          + "".join(f"sha512  {digest}  {name}\n" for name, digest in sorted(tarballs.items())))
    set_version("openchamber", version)
    print(f"openchamber {version}: {len(modules)} dependencies, {len(tarballs)} tarballs")

    bun = re.search(r"^FROM oven/bun:(\S+)", fetch(OPENCHAMBER_DOCKERFILE.format(version=version)).decode(), re.M)
    if bun and bun.group(1) != current_version("bun"):
        print(f"note: OpenChamber {version}'s container runs on Bun {bun.group(1)}; package/bun has "
              f"{current_version('bun')} (update it by hand: version, hashes from the release's SHASUMS256.txt)")


PACKAGES = {
    "opencode": (lambda: latest("@opencode/cli"), update_opencode),
    "openchamber": (lambda: latest(OPENCHAMBER_PACKAGE), update_openchamber),
}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="report the latest versions, change nothing")
    parser.add_argument("packages", nargs="*", metavar="PACKAGE[=VERSION]",
                        help="opencode and/or openchamber (default: both), each to its latest or the given version")
    args = parser.parse_args()
    wanted = {}
    for arg in args.packages or PACKAGES:
        name, _, version = arg.partition("=")
        if name not in PACKAGES:
            parser.error(f"unknown package {name!r}; choose from {', '.join(PACKAGES)}")
        wanted[name] = version or None
    for name, version in wanted.items():
        find_latest, update = PACKAGES[name]
        current = current_version(name)
        version = version or find_latest()
        if version == current:
            print(f"{name} {current} is current")
            continue
        if args.check:
            print(f"{name} {current} -> {version}")
            continue
        print(f"{name} {current} -> {version}")
        update(version)
    if not args.check:
        print("next: review the diff, build, run `make test`, and update the sizes in README.md")


if __name__ == "__main__":
    main()
