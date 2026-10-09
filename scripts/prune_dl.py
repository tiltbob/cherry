#!/usr/bin/env python3
"""Prune Buildroot's download directory to what the configurations need.

Buildroot never deletes a download: dl/ keeps every version ever fetched, the
tarballs it vendored, and the git clones it fetched from, and CI's download
cache grows with it. This asks Buildroot (`make br-show-info`) what the given
configurations still need, and removes the rest from dl/: with --delete; without
it, it only reports. For a package still in a configuration, its git fetch
cache (dl/<package>/git, with its git.readme) and .lock file stay.

  scripts/prune_dl.py                                # report, for every configs/*_defconfig
  scripts/prune_dl.py --delete                       # prune
  scripts/prune_dl.py --config cherry_x86_64_defconfig --delete   # one configuration (CI)

The directory is $BR2_DL_DIR, or dl/ in the repository.
"""

import argparse
import glob
import json
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Buildroot's fetch caches for packages downloaded from version control.
VCS_DIRS = {"git", "svn", "hg", "bzr", "cvs"}


def needed(config):
    """The download directories and files of the packages of one configuration."""
    out = subprocess.run(["make", "-s", f"DEFCONFIG={config}", "br-show-info"], cwd=ROOT, check=True,
                         capture_output=True, text=True).stdout
    info = json.loads(next(line for line in out.splitlines() if line.startswith("{")))
    dirs, files = set(), set()
    for package in info.values():
        directory = package.get("dl_dir")
        if not directory:
            continue
        dirs.add(directory)
        for download in package.get("downloads", []):
            files.add(os.path.join(directory, download["source"]))
    return dirs, files


def size(path):
    if os.path.islink(path) or os.path.isfile(path):
        return os.lstat(path).st_size
    return sum(os.lstat(os.path.join(root, f)).st_size for root, _, fs in os.walk(path) for f in fs)


def remove(path):
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path)
    else:
        os.remove(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", action="append", metavar="NAME_defconfig",
                        help="keep what this configuration needs (repeatable; default: every configs/*_defconfig)")
    parser.add_argument("--dl", default=os.environ.get("BR2_DL_DIR") or os.path.join(ROOT, "dl"),
                        help="the download directory (default: $BR2_DL_DIR, or dl/)")
    parser.add_argument("--delete", action="store_true", help="remove what is not needed, rather than report it")
    args = parser.parse_args()

    configs = args.config or sorted(os.path.basename(c)
                                    for c in glob.glob(os.path.join(ROOT, "configs", "*_defconfig")))
    dirs, files = set(), set()
    for config in configs:
        d, f = needed(config)
        dirs |= d
        files |= f
    if not os.path.isdir(args.dl):
        sys.exit(f"{args.dl}: no such directory")

    unneeded, kept = [], 0
    for top in sorted(os.listdir(args.dl)):
        path = os.path.join(args.dl, top)
        if top not in dirs:
            unneeded.append(path)
            continue
        for entry in sorted(os.listdir(path)):
            if entry in VCS_DIRS or entry in (".lock", "git.readme") or os.path.join(top, entry) in files:
                kept += size(os.path.join(path, entry))
            else:
                unneeded.append(os.path.join(path, entry))
    total = sum(size(p) for p in unneeded)
    for path in unneeded:
        print(f"{size(path) / 1e6:8.1f} MB  {os.path.relpath(path, args.dl)}")
        if args.delete:
            remove(path)
    verb = "removed" if args.delete else "not needed by " + ", ".join(configs)
    print(f"{len(unneeded)} entries, {total / 1e6:.0f} MB {verb}; {kept / 1e6:.0f} MB needed stay in {args.dl}")


if __name__ == "__main__":
    main()
