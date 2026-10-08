#!/usr/bin/env python3
"""Update Cherry's packages to their latest upstream releases.

For each package in PACKAGES, this finds the newest release upstream, sets the
version (and, where releases live at new URLs, the site) in package/<name>/
<name>.mk, and refreshes package/<name>/<name>.hash:

- Most packages go through Buildroot itself: `make br-<name>-source` downloads
  the new release (git clone and vendoring included) and reports the hash it
  computed, which replaces the old one; `make br-<name>-extract` then gives the
  license files to hash. So Buildroot must be able to download, which the
  first `make` sets up.
- opencode and openchamber come from the npm registry, whose integrity fields
  give their sha512 directly. For openchamber, npm resolves @openchamber/web's
  production dependencies into a lockfile (nothing is installed), from which
  npm-modules.list gets their install paths and URLs, and openchamber.hash
  their hashes.

cherry, cherry-stage1 and rpmstrap are Cherry's own code, with no upstream.

  scripts/update_packages.py                # every package, to its latest
  scripts/update_packages.py kubo pacman    # some of them
  scripts/update_packages.py toml11=v4.3.0  # a specific version
  scripts/update_packages.py --check        # only report what is newer

Needs network access, git, and npm for openchamber. Afterwards: review the
diff (comments in .hash files that claim a checked signature need that check
again), build, run `make test`, and update what README.md says about sizes.
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

ROOT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
OUTPUT = os.environ.get("O") or os.path.join(ROOT, "output", "cherry_x86_64")
REGISTRY = "https://registry.npmjs.org/"
HASH_LINE = re.compile(r"^(sha256|sha512)\s+([0-9a-f]+)\s+(\S+)\s*$")


def fetch(url):
    with urllib.request.urlopen(url) as r:
        return r.read()


def fetch_json(url):
    return json.loads(fetch(url))


def version_key(version):
    """Numbers in the version, for ordering: 1.0.145 > 1.0.9, 20260909 > 20260902."""
    return tuple(int(x) for x in re.findall(r"\d+", version))


# --- the packages' .mk and .hash files -------------------------------------

def package_file(name, filename):
    return os.path.join(ROOT, "package", name, filename)


def mk_prefix(name):
    return name.upper().replace("-", "_")


def mk_get(name, var):
    with open(package_file(name, f"{name}.mk")) as f:
        return re.search(rf"^{mk_prefix(name)}_{var} = (.*)$", f.read(), re.M).group(1).strip()


def mk_set(name, var, value):
    path = package_file(name, f"{name}.mk")
    with open(path) as f:
        mk = f.read()
    mk, n = re.subn(rf"^({mk_prefix(name)}_{var} = ).*$", lambda m: m.group(1) + value, mk, flags=re.M)
    assert n == 1, f"{name}: no {var} line"
    with open(path, "w") as f:
        f.write(mk)


def read_hashes(name):
    with open(package_file(name, f"{name}.hash")) as f:
        return f.read()


def write_hashes(name, text):
    with open(package_file(name, f"{name}.hash"), "w") as f:
        f.write(text)


def set_hash(name, filename, algo, digest):
    lines = read_hashes(name).splitlines(keepends=True)
    for i, line in enumerate(lines):
        m = HASH_LINE.match(line)
        if m and m.group(3) == filename:
            lines[i] = f"{algo}  {digest}  {filename}\n"
            break
    else:
        lines.append(f"{algo}  {digest}  {filename}\n")
    write_hashes(name, "".join(lines))


# --- Buildroot --------------------------------------------------------------

def make(target):
    """Run a Buildroot target through the top-level Makefile; return its output."""
    p = subprocess.run(["make", "-C", ROOT, f"br-{target}"], capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


def show_info(name):
    rc, out = make(f"{name}-show-info")
    for line in out.splitlines():
        if line.startswith("{"):
            return json.loads(line)[name]
    sys.exit(f"{name}: no show-info:\n{out[-2000:]}")


def update_with_buildroot(name, version, site=None):
    """Set the version (and site), then let Buildroot download the release
    and tell the hashes; hash the license files from the extracted source."""
    old_version = mk_get(name, "VERSION")
    substitutions = [(old_version, version)]
    if site:
        substitutions.append((mk_get(name, "SITE"), site))
        mk_set(name, "SITE", site)
    mk_set(name, "VERSION", version)
    info = show_info(name)
    downloads = [d["source"] for d in info["downloads"]]
    files = downloads + [f for f in info["license_files"] if f not in downloads]

    # The old hashes, under the new names: Buildroot reports what it got
    # instead. Lines for files that are gone go; new files get a line.
    text = read_hashes(name)
    for old, new in substitutions:
        text = text.replace(old, new)
    lines = [line for line in text.splitlines(keepends=True)
             if not (m := HASH_LINE.match(line)) or m.group(3) in files]
    known = {m.group(3) for line in lines if (m := HASH_LINE.match(line))}
    lines += [f"sha256  {'0' * 64}  {f}\n" for f in files if f not in known]
    write_hashes(name, "".join(lines))

    for _ in range(len(downloads) + 1):
        rc, out = make(f"{name}-source")
        if rc == 0:
            break
        wrong = re.findall(r"^ERROR: (\S+) has wrong (sha256|sha512) hash:\n(?:.*\n)*?ERROR: got\s*: ([0-9a-f]+)$",
                           out, re.M)
        if not wrong:
            sys.exit(f"{name}: download failed:\n{out[-3000:]}")
        for filename, algo, digest in wrong:
            set_hash(name, filename, algo, digest)
    else:
        sys.exit(f"{name}: the hashes still don't check out")

    rc, out = make(f"{name}-extract")
    if rc != 0:
        sys.exit(f"{name}: extraction failed:\n{out[-3000:]}")
    for filename in info["license_files"]:
        if filename in downloads:
            continue
        with open(os.path.join(OUTPUT, info["source_dir"], filename), "rb") as f:
            set_hash(name, filename, "sha256", hashlib.sha256(f.read()).hexdigest())
    if re.search(r"signature|pgp", read_hashes(name), re.I):
        print(f"note: {name}.hash mentions a signature check; do it again for {version}")


# --- where to look for releases ----------------------------------------------
# Each finder takes a version, or None for the latest, and returns (version,
# options for the update), the version in the form the .mk uses.

def git_tags(url, pattern):
    """The highest tag matching pattern; the version is its group, or the tag."""
    def find(version):
        out = subprocess.run(["git", "ls-remote", "--tags", "--refs", url],
                             capture_output=True, text=True, check=True).stdout
        versions = []
        for line in out.splitlines():
            m = re.fullmatch(pattern, line.split("refs/tags/", 1)[1])
            if m:
                versions.append(m.group(m.lastindex or 0))
        if version and version not in versions:
            sys.exit(f"no tag of {url} matching {pattern} gives version {version}")
        if not versions:
            sys.exit(f"no tag of {url} matches {pattern}")
        return version or max(versions, key=version_key), {}
    return find


def github_tags(repo, pattern):
    return git_tags(f"https://github.com/{repo}", pattern)


def gitlab_release(host, project, pattern, asset):
    """The newest GitLab release whose tag matches pattern, or the one of the
    given version; the site is where its asset (named with the version) was
    uploaded."""
    def find(version):
        for release in fetch_json(f"https://{host}/api/v4/projects/{project}/releases?per_page=100"):
            m = re.fullmatch(pattern, release["tag_name"])
            if not m or (version and m.group(m.lastindex or 0) != version):
                continue
            version = m.group(m.lastindex or 0)
            name = asset.format(version=version)
            for link in release["assets"]["links"]:
                if link["name"] == name:
                    return version, {"site": link["url"].rsplit("/", 1)[0]}
            sys.exit(f"release {release['tag_name']} of project {project} has no asset {name}")
        sys.exit(f"no release of project {project} matches {pattern}" + (f" and version {version}" if version else ""))
    return find


def debian_snapshot(source, filename, binary=None):
    """The newest version of a Debian source package (its binary package when
    given), with no backport or NMU suffix, or the given version, and the
    snapshot.debian.org site that holds its file, named with the version."""
    def find(version):
        api = "https://snapshot.debian.org/mr/package"
        if not version:
            versions = [v["version"] for v in fetch_json(f"{api}/{source}/")["result"]]
            version = next(v for v in versions if re.fullmatch(r"[0-9][0-9.]*", v))
        where = f"binfiles/{binary}/{version}" if binary else "srcfiles"
        name = filename.format(version=version)
        for entries in fetch_json(f"{api}/{source}/{version}/{where}?fileinfo=1")["fileinfo"].values():
            for e in entries:
                if e["name"] == name and e["archive_name"] == "debian":
                    return version, {"site": f"https://snapshot.debian.org/archive/debian/{e['first_seen']}{e['path']}"}
        sys.exit(f"snapshot.debian.org has no {name}")
    return find


def buildroot_package(name):
    """The version of one of Buildroot's own packages."""
    def find(version):
        with open(os.path.join(ROOT, "buildroot", "package", name, f"{name}.mk")) as f:
            return version or re.search(rf"^{name.upper()}_VERSION = (\S+)$", f.read(), re.M).group(1), {}
    return find


def npm(package):
    def find(version):
        return version or fetch_json(f"{REGISTRY}{package}")["dist-tags"]["latest"], {}
    return find


# --- opencode and openchamber, from the npm registry ----------------------

OPENCODE_PACKAGE = "@opencode/cli-linux-x64-baseline"
OPENCHAMBER_PACKAGE = "@openchamber/web"
# OpenChamber's own container runs on this Bun; package/bun follows it.
OPENCHAMBER_DOCKERFILE = "https://raw.githubusercontent.com/openchamber/openchamber/v{version}/Dockerfile"
# Dependencies left out, beyond the packages for other platforms (os/cpu):
OPENCHAMBER_SKIP = {
    # 32 MiB of ONNX runtime for local speech recognition and synthesis, which
    # also needs models downloaded at run time. Without it, sherpa-onnx-node
    # (kept, 50 KiB) reports local speech as unavailable.
    "sherpa-onnx-linux-x64",
}


def sha512_hex(integrity):
    """The hex digest of an npm integrity string, "sha512-<base64>"."""
    if not integrity.startswith("sha512-"):
        sys.exit(f"not a sha512 integrity: {integrity}")
    return base64.b64decode(integrity[len("sha512-"):]).hex()


def license_sha256(url):
    return hashlib.sha256(fetch(url)).hexdigest()


def update_opencode(version, _options):
    tarball = fetch_json(f"{REGISTRY}{OPENCODE_PACKAGE}/{version}")["dist"]
    license_hash = license_sha256(f"https://raw.githubusercontent.com/anomalyco/opencode/v{version}/LICENSE")
    write_hashes("opencode",
                 f"# From the npm registry: the integrity field of {OPENCODE_PACKAGE}\n"
                 f"# {version} (npm view {OPENCODE_PACKAGE}@{version} dist.integrity)\n"
                 f"sha512  {sha512_hex(tarball['integrity'])}  cli-linux-x64-baseline-{version}.tgz\n"
                 f"# Locally computed, from the LICENSE file at the v{version} tag\n"
                 f"sha256  {license_hash}  LICENSE\n")
    mk_set("opencode", "VERSION", version)


def openchamber_lockfile(version):
    with tempfile.TemporaryDirectory() as tmp:
        with open(os.path.join(tmp, "package.json"), "w") as f:
            json.dump({"name": "cherry-openchamber", "private": True}, f)
        subprocess.run(["npm", "install", "--package-lock-only", "--omit=dev", "--ignore-scripts",
                        "--no-audit", "--no-fund", "--save-exact", f"{OPENCHAMBER_PACKAGE}@{version}"],
                       cwd=tmp, check=True, stdout=subprocess.DEVNULL)
        with open(os.path.join(tmp, "package-lock.json")) as f:
            return json.load(f)


def update_openchamber(version, _options):
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
    license_hash = license_sha256(f"https://raw.githubusercontent.com/openchamber/openchamber/v{version}/LICENSE")

    with open(package_file("openchamber", "npm-modules.list"), "w") as f:
        f.write(f"# The production dependencies of {OPENCHAMBER_PACKAGE} {version}: install path\n"
                "# below node_modules/, tarball URL. Generated by scripts/update_packages.py;\n"
                "# the hashes are in openchamber.hash.\n"
                + "".join(f"{path} {url}\n" for path, url, _ in modules))
    write_hashes("openchamber",
                 "# From the npm registry: the integrity field of each package version, as\n"
                 "# npm's lockfile records it. Generated by scripts/update_packages.py.\n"
                 f"sha512  {main_hash}  web-{version}.tgz\n"
                 f"# Locally computed, from the LICENSE file at the v{version} tag\n"
                 f"sha256  {license_hash}  LICENSE\n"
                 "# The dependencies, see npm-modules.list\n"
                 + "".join(f"sha512  {digest}  {name}\n" for name, digest in sorted(tarballs.items())))
    mk_set("openchamber", "VERSION", version)
    print(f"openchamber {version}: {len(modules)} dependencies, {len(tarballs)} tarballs")

    bun = re.search(r"^FROM oven/bun:(\S+)", fetch(OPENCHAMBER_DOCKERFILE.format(version=version)).decode(), re.M)
    if bun and bun.group(1) != mk_get("bun", "VERSION"):
        print(f"note: OpenChamber {version}'s container runs on Bun {bun.group(1)}, package/bun has "
              f"{mk_get('bun', 'VERSION')}: scripts/update_packages.py bun={bun.group(1)}")


# --- the table ----------------------------------------------------------------
# name: (how to find the latest version, how to update; the default goes
# through Buildroot). Versions are in the form the package's .mk uses: for
# the git packages, the tag itself.

GITLAB_ARCH = "gitlab.archlinux.org"
PACKAGES = {
    "arch-install-scripts": (git_tags(f"https://{GITLAB_ARCH}/archlinux/arch-install-scripts.git", r"v(\d+)"),),
    "archlinux-keyring": (gitlab_release(GITLAB_ARCH, 19588, r"\d{8}", "archlinux-keyring-{version}.tar.gz"),),
    "bun": (github_tags("oven-sh/bun", r"bun-v(\d+\.\d+\.\d+)"),),
    "debian-archive-keyring": (debian_snapshot("debian-archive-keyring", "debian-archive-keyring_{version}_all.deb",
                                               binary="debian-archive-keyring"),),
    "debootstrap": (debian_snapshot("debootstrap", "debootstrap_{version}.tar.gz"),),
    "debootstrap-pkgdetails": (debian_snapshot("base-installer", "base-installer_{version}.tar.xz"),),
    "distribution-gpg-keys": (github_tags("rpm-software-management/distribution-gpg-keys",
                                          r"distribution-gpg-keys-\d+\.\d+-\d+"),),
    "dnf5": (github_tags("rpm-software-management/dnf5", r"\d+\.\d+\.\d+\.\d+"),),
    "kubo": (github_tags("ipfs/kubo", r"v\d+\.\d+\.\d+"),),
    "librepo": (github_tags("rpm-software-management/librepo", r"\d+\.\d+\.\d+"),),
    "libsolv-rpm": (github_tags("openSUSE/libsolv", r"\d+\.\d+\.\d+"),),
    "openchamber": (npm(OPENCHAMBER_PACKAGE), update_openchamber),
    "opencode": (npm("@opencode/cli"), update_opencode),
    "pacman": (gitlab_release(GITLAB_ARCH, 19637, r"v(\d+\.\d+\.\d+)", "pacman-{version}.tar.xz"),),
    "rpm-sequoia": (github_tags("rpm-software-management/rpm-sequoia", r"v\d+\.\d+\.\d+"),),
    "rpm6": (github_tags("rpm-software-management/rpm", r"rpm-(\d+\.\d+\.\d+)-release"),),
    # The same source as Buildroot's shadow package.
    "shadow-useradd": (buildroot_package("shadow"),),
    "toml11": (github_tags("ToruNiina/toml11", r"v\d+\.\d+\.\d+"),),
}


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="report the latest versions, change nothing")
    parser.add_argument("packages", nargs="*", metavar="PACKAGE[=VERSION]",
                        help="packages to update (default: all), each to its latest or to the given version")
    args = parser.parse_args()
    wanted = {}
    for arg in args.packages or PACKAGES:
        name, _, version = arg.partition("=")
        if name not in PACKAGES:
            parser.error(f"unknown package {name!r}; choose from {', '.join(PACKAGES)}")
        wanted[name] = version or None
    for name, version in wanted.items():
        find, *custom = PACKAGES[name]
        current = mk_get(name, "VERSION")
        version, options = find(version)
        if version == current:
            print(f"{name} {current} is current")
            continue
        print(f"{name} {current} -> {version}")
        if args.check:
            continue
        if custom:
            custom[0](version, options)
        else:
            update_with_buildroot(name, version, **options)
    if not args.check:
        print("next: review the diff, build, run `make test`, and update the sizes in README.md")


if __name__ == "__main__":
    main()
