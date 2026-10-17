#!/bin/sh
# package/cherry/cherry-image builds, from the cherry user's recipe, the image
# the README says, and nothing else: run with the real cherry-bootstrap and
# cherry-image-setup, and stand-ins on PATH for what needs root or a machine.
# The bootstrap tools make a small busybox root with a stand-in package
# manager that logs its arguments; systemd-nspawn chroots into it in a user
# namespace (unshare -r), with the host's /dev and the image's hostname, and
# runs the setup there, as root; systemd-run runs its command as the calling
# user, with a pipe on stdin that it cannot open again, as the cherry user
# cannot open root's. The directories are all under a temporary one
# (CHERRY_MACHINES_DIR and the others). Needs a static busybox, unshare and
# user namespaces, and GNU tar.
set -eu
cd "$(dirname "$0")/.."
src=$PWD/package/cherry

skip() {
	echo "image: SKIPPED, $*"
	exit 0
}
busybox=$(command -v busybox) || skip "busybox is not installed"
for tool in unshare tar; do
	command -v "$tool" >/dev/null || skip "$tool is not installed"
done

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
mkdir "$tmp/bin" "$tmp/libexec" "$tmp/logs" "$tmp/machines" "$tmp/recipes"
ln -s "$src/cherry-bootstrap" "$src/cherry-image-setup" "$tmp/libexec/"
export PATH="$tmp/bin:$PATH" CALLS="$tmp/calls" BUSYBOX="$busybox" LOGS_DIRECTORY="$tmp/logs" \
	CHERRY_MACHINES_DIR="$tmp/machines" CHERRY_IMAGES_DIR="$tmp/recipes" CHERRY_LIBEXEC="$tmp/libexec"
machines=$CHERRY_MACHINES_DIR recipes=$CHERRY_IMAGES_DIR

# mktree <dir> <package manager>: a merged-/usr root with busybox, a machine
# ID, the host's /etc/hostname (as debootstrap copies it), and a package
# manager that logs its arguments to /var/log/pm.
cat >"$tmp/bin/mktree" <<'EOF'
#!/bin/sh
set -eu
mkdir -p "$1/usr/bin" "$1/etc" "$1/var/log" "$1/dev"
ln -s usr/bin "$1/bin"
cp "$BUSYBOX" "$1/usr/bin/busybox"
for applet in sh cat cp rm mkdir chmod ls echo printf test [ hostname; do
	ln -s busybox "$1/usr/bin/$applet"
done
echo 0123456789abcdef0123456789abcdef >"$1/etc/machine-id"
echo 'cherry-????????' >"$1/etc/hostname"
printf '#!/bin/sh\necho "%s $*" >>/var/log/pm\n' "$2" >"$1/usr/bin/$2"
chmod 0755 "$1/usr/bin/$2"
EOF
# The bootstrap tools: debootstrap, rpmstrap and pacstrap, each making the
# tree it is given with its distribution's package manager.
for tool in debootstrap:apt-get rpmstrap:dnf pacstrap:pacman; do
	cat >"$tmp/bin/${tool%:*}" <<EOF
#!/bin/sh
echo "${tool%:*} \$*" >>"\$CALLS"
for arg; do
	case \$arg in "\$CHERRY_MACHINES_DIR"/*) mktree "\$arg" ${tool#*:} ;; esac
done
EOF
done
cat >"$tmp/bin/systemctl" <<'EOF'
#!/bin/sh
echo "systemctl $*" >>"$CALLS"
EOF
cat >"$tmp/bin/systemd-run" <<'EOF'
#!/bin/sh
args="$*"
while [ "$1" != -- ]; do shift; done
shift
echo "systemd-run ${args%% -- *} -- $*" >>"$CALLS"
# A pipe on stdin can be read but not opened again (/dev/stdin), as the
# cherry user cannot open root's.
if [ -p /dev/stdin ]; then chmod 0 /proc/self/fd/0; fi
exec "$@"
EOF
cat >"$tmp/bin/systemd-nspawn" <<'EOF'
#!/bin/sh
echo "systemd-nspawn $*" >>"$CALLS"
for arg; do
	case $arg in
	--directory=*) dir=${arg#*=} ;;
	--hostname=*) hostname=${arg#*=} ;;
	esac
	command=$arg
done
# shellcheck disable=SC2016 # the inner shell's arguments
exec unshare -r -m -u "$BUSYBOX" sh -c 'hostname "$2" && mount --rbind /dev "$0/dev" && exec chroot "$0" "$1"' \
	"$dir" "$command" "$hostname"
EOF
chmod +x "$tmp/bin"/*

mktree "$tmp/probe" apt-get
unshare -r chroot "$tmp/probe" /usr/bin/busybox true 2>/dev/null ||
	skip "cannot run busybox in a chroot in a user namespace (unshare -r: user namespaces off, or busybox not static?)"

n=0
die() {
	echo "image: $*" >&2
	exit 1
}
run() {
	: >"$CALLS"
	set +e
	out=$("$src/cherry-image" "$1" 2>&1)
	rc=$?
	set -e
	log=$LOGS_DIRECTORY/$1.log
	n=$((n + 1))
}
# refused <instance> <message>: exits non-zero with the message, calls no tool, creates nothing.
refused() {
	run "$1"
	[ "$rc" -ne 0 ] || die "'$1' was accepted"
	case $out in
	*"$2"*) ;;
	*) die "'$1': expected '$2', got: $out" ;;
	esac
	[ ! -s "$CALLS" ] || die "'$1': a tool was called: $(cat "$CALLS")"
	[ -z "$(ls -A "$machines")" ] || die "'$1': something was created: $(ls -A "$machines")"
}
# failed <instance> <message>: exits non-zero with the message in its log, and leaves no image.
failed() {
	run "$1"
	[ "$rc" -ne 0 ] || die "'$1' succeeded"
	grep -qF "$2" "$log" || die "'$1': expected '$2' in the log: $(cat "$log")"
	[ -z "$(ls -A "$machines/.cherry-image" 2>/dev/null)" ] || die "'$1' left its staging behind"
}
# recipe <name> <image.conf line>...: a recipe with just image.conf.
recipe() {
	rm -rf "${recipes:?}/$1"
	mkdir -p "$recipes/$1"
	r=$recipes/$1
	shift
	printf '%s\n' "$@" >"$r/image.conf"
}

refused web "bad instance 'web'"
refused nope:web "bad instance 'nope:web'"
# Images never leave the machine: no exports.
refused tar:web "bad instance 'tar:web'"
refused raw:web "bad instance 'raw:web'"
refused build: "bad image name ''"
refused build:.web "bad image name '.web'"
refused build:-web "bad image name '-web'"
refused build:my_web "bad image name 'my_web'"
refused build:a.b "bad image name 'a.b'"
refused "build:$(printf '%049d' 0)" "longer than 48 characters"

failed build:none "no recipe: $recipes/none/image.conf"
recipe odd Distribution=debian Release=trixie Colour=red
failed build:odd "image.conf: unknown setting: Colour=red"
recipe arch Distribution=arch Release=rolling
failed build:arch "Arch Linux has no release"
[ ! -e "$machines/arch" ] || die "a refused bootstrap left an image"

# A Debian image from a full recipe, the last line of image.conf without a newline.
recipe web '# a web server' Distribution=debian Release=trixie 'Packages=nginx-light curl'
printf 'Packages=less' >>"$r/image.conf"
mkdir -p "$r/extra/usr/bin" "$r/extra/bin" "$r/extra/etc"
printf '#!/bin/sh\necho hello\n' >"$r/extra/usr/bin/hello"
chmod 0755 "$r/extra/usr/bin/hello"
echo tool >"$r/extra/bin/tool"
echo welcome >"$r/extra/etc/motd"
ln -s /etc/motd "$r/extra/etc/greeting"
echo secret >"$tmp/secret"
ln -s "$tmp/secret" "$r/extra/etc/secret"
cat >"$r/postinst" <<'EOF'
set -eu
echo "postinst on $(hostname)"
cat /etc/motd >/var/postinst-ran
EOF
printf '[Exec]\nEnvironment=MODE=test\n' >"$r/image.nspawn"
run build:web
[ "$rc" -eq 0 ] || die "build:web failed: $out $(cat "$log")"
[ "$out" = "build web, log in $log" ] || die "unexpected output: $out"
img=$machines/web
[ -d "$img" ] || die "build:web made no image"
[ -z "$(ls -A "$machines/.cherry-image")" ] || die "build:web left its staging behind: $(ls -A "$machines/.cherry-image")"
[ ! -e "$img/.cherry-image" ] || die "the staged recipe is still in the image"
grep -qxF "debootstrap --variant=minbase --include=systemd,systemd-sysv,dbus trixie $machines/.cherry-image/web" "$CALLS" ||
	die "debootstrap was not called right: $(cat "$CALLS")"
grep -qxF "systemd-run --quiet --pipe --wait --collect -p User=cherry -- cat $recipes/web/image.conf" "$CALLS" ||
	die "image.conf was not read as the cherry user: $(cat "$CALLS")"
grep -qxF "systemd-run --quiet --pipe --wait --collect -p User=cherry -- tar -C $recipes/web -cf - ." "$CALLS" ||
	die "the recipe was not read as the cherry user: $(cat "$CALLS")"
grep -qxF "systemd-nspawn --quiet --pipe --register=no --settings=no -U --machine=cherry-image-web --hostname=web --directory=$machines/.cherry-image/web /.cherry-image/setup" "$CALLS" ||
	die "systemd-nspawn was not called right: $(cat "$CALLS")"
expected="apt-get update
apt-get install -y --no-install-recommends nginx-light curl less
apt-get clean"
[ "$(cat "$img/var/log/pm")" = "$expected" ] || die "apt-get was called: $(cat "$img/var/log/pm")"
[ "$(stat -c %a "$img/usr/bin/hello")" = 755 ] || die "extra/usr/bin/hello lost its mode"
[ "$("$img/usr/bin/hello")" = hello ] || die "extra/usr/bin/hello is not the recipe's"
[ -L "$img/bin" ] || die "extra/bin replaced the image's /bin link"
[ "$(cat "$img/usr/bin/tool")" = tool ] || die "extra/bin/tool did not go through the image's /bin link"
[ "$(readlink "$img/etc/greeting")" = /etc/motd ] || die "extra/etc/greeting is not the link it was"
[ "$(readlink "$img/etc/secret")" = "$tmp/secret" ] || die "extra/etc/secret was followed out of the recipe"
[ "$(cat "$img/var/postinst-ran")" = welcome ] || die "postinst did not run after extra/ was copied"
grep -qxF "postinst on web" "$log" || die "postinst did not run with the image's hostname, or its output is not in the log"
[ ! -e "$img/etc/hostname" ] || die "the host's /etc/hostname is still in the image: $(cat "$img/etc/hostname")"
[ -e "$img/etc/machine-id" ] || die "/etc/machine-id was removed"
[ ! -s "$img/etc/machine-id" ] || die "/etc/machine-id was not emptied"
cmp -s "$r/image.nspawn" "$machines/web.nspawn" || die "the settings were not installed as web.nspawn"
grep -qxF "built $img ($(du -sh "$img" | cut -f 1)): machinectl start web" "$log" || die "the log lacks the built line: $(cat "$log")"

touch "$img/keep"
failed build:web "$img exists: remove it first (machinectl remove web)"
[ -e "$img/keep" ] || die "building an existing image touched it"

# The settings only from a file of the recipe, not through a link out of it.
recipe linked Distribution=debian Release=trixie
ln -s "$tmp/secret" "$r/image.nspawn"
echo stale >"$machines/linked.nspawn"
run build:linked
[ "$rc" -eq 0 ] || die "build:linked failed: $(cat "$log")"
[ ! -e "$machines/linked.nspawn" ] || die "a linked image.nspawn, or a stale one, was installed: $(cat "$machines/linked.nspawn")"
[ "$(cat "$machines/linked/var/log/pm")" = "apt-get clean" ] ||
	die "apt-get did more than clean up with no packages: $(cat "$machines/linked/var/log/pm")"

recipe broken Distribution=debian Release=trixie
printf 'echo "postinst fails"\nexit 3\n' >"$r/postinst"
failed build:broken "postinst fails"
[ ! -e "$machines/broken" ] || die "a failed postinst left an image"

recipe fedora Distribution=fedora Release=44 Packages=httpd
mkdir -p "$r/extra/etc"
echo www >"$r/extra/etc/hostname"
run build:fedora
[ "$rc" -eq 0 ] || die "build:fedora failed: $(cat "$log")"
grep -qxF "rpmstrap fedora 44 $machines/.cherry-image/fedora" "$CALLS" || die "rpmstrap was not called right: $(cat "$CALLS")"
expected="dnf -y --setopt=install_weak_deps=False install httpd
dnf clean all"
[ "$(cat "$machines/fedora/var/log/pm")" = "$expected" ] || die "dnf was called: $(cat "$machines/fedora/var/log/pm")"
[ "$(cat "$machines/fedora/etc/hostname")" = www ] || die "the recipe's /etc/hostname was not kept"

recipe arch Distribution=arch Packages=nginx
run build:arch
[ "$rc" -eq 0 ] || die "build:arch failed: $(cat "$log")"
grep -qxF "pacstrap -K -c $machines/.cherry-image/arch base" "$CALLS" || die "pacstrap was not called right: $(cat "$CALLS")"
[ "$(cat "$machines/arch/var/log/pm")" = "pacman -Syu --noconfirm --needed nginx" ] ||
	die "pacman was called: $(cat "$machines/arch/var/log/pm")"

echo "image: $n cases passed"
