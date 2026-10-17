#!/bin/sh
# The top-level Makefile names the image after the nearest release tag, a
# lightweight one as GitHub creates them, and passes the name to Buildroot:
# run on a scratch repository with MAKE=echo, so Buildroot's command lines are
# printed instead of run.
set -eu
cd "$(dirname "$0")/.."
makefile=$PWD/Makefile

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
# This test's own git identity and configuration, none of the calling make's
# flags or name.
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1 \
	GIT_AUTHOR_NAME=test GIT_AUTHOR_EMAIL=test@example.com \
	GIT_COMMITTER_NAME=test GIT_COMMITTER_EMAIL=test@example.com
unset MAKEFLAGS MFLAGS MAKELEVEL BR2_EXTERNAL_CHERRY_VERSION
repo=$tmp/repo
git init -q "$repo"
# The checked-out submodule the Makefile wants, untracked here.
mkdir "$repo/buildroot"
: >"$repo/buildroot/Makefile"

commit() {
	echo "$1" >>"$repo/file"
	git -C "$repo" add file
	git -C "$repo" commit -q -m "$1"
}
commit_id() {
	git -C "$repo" rev-parse --short HEAD
}
# The name on the command line of Buildroot's build: the last of the two
# lines, after the one that configures it.
version() {
	make -s --no-print-directory -C "$repo" -f "$makefile" MAKE=echo O="$tmp/out" "$@" build |
		sed -n '$s/.* BR2_EXTERNAL_CHERRY_VERSION=\([^ ]*\).*/\1/p'
}
n=0
expect() {
	[ "$2" = "$1" ] || {
		echo "version: $3: named '$2', not '$1'" >&2
		exit 1
	}
	n=$((n + 1))
}

commit one
expect "g$(commit_id)" "$(version)" "no release tag"
git -C "$repo" tag v0.0.1
expect v0.0.1 "$(version)" "the release's commit"
commit two
git -C "$repo" tag sdk-2026.10.17.001
expect "v0.0.1-1-g$(commit_id)" "$(version)" "the next commit, an SDK release's"
expect vX "$(version BR2_EXTERNAL_CHERRY_VERSION=vX)" "a name given"
echo change >>"$repo/file"
expect "v0.0.1-1-g$(commit_id)-dirty" "$(version)" "a changed file"
git -C "$repo" checkout -q file
: >"$repo/untracked"
expect "v0.0.1-1-g$(commit_id)" "$(version)" "an untracked file"

echo "version: $n cases passed"
