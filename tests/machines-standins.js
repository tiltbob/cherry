// What tests/mcp.js and tests/extension.js share: stand-ins for systemctl,
// machinectl, busctl and ip on PATH, which log their arguments and keep the
// machine's state (images, running containers) in files, and the profiles
// the environments are created from, in temporary directories. The
// environments module (package/cherry/environments.js) under test reads
// its directories from CHERRY_IMAGES_DIR, CHERRY_IMAGE_LOGS and
// CHERRY_PROFILE_DIRS, which env() gives.
"use strict";
const fs = require("fs");
const os = require("os");
const path = require("path");

// The machine's state, in files of $STATE with a name per line: images,
// running containers, and dead ones (running, but never letting a shell in);
// the switches the tests flip: FAIL_UNIT (a cherry-image@ unit that fails),
// SLOW_UNIT (one that takes two seconds), KEEP_RUNNING (poweroff does nothing).
const STANDINS = {
  // cherry-image@build:<name>.service: a log, slowness and a failure as told;
  // and what a container answers to the status script.
  systemctl: `
echo "systemctl $*" >>"$CALLS"
case $1 in
is-system-running) echo running; exit 0 ;;
--failed) echo "bad.service loaded failed failed The bad one"; exit 0 ;;
esac
unit=$3 instance=\${3#cherry-image@}; instance=\${instance%.service}
printf 'line %s\\n' $(seq 1 100) >"$LOGS/$instance.log"
echo "end of $instance" >>"$LOGS/$instance.log"
[ "$unit" != "$SLOW_UNIT" ] || sleep 2
[ "$unit" != "$FAIL_UNIT" ] || { echo "Job for $unit failed." >&2; exit 1; }
name=\${instance#build:}
grep -qxF "$name" "$STATE/images" || echo "$name" >>"$STATE/images"`,
  // shell runs the command here, through a stand-in pty: CRLF line ends, an
  // escape sequence first, and nothing at all once stdin is at EOF, as
  // systemd's pty forwarder ends then.
  machinectl: `
echo "machinectl $*" >>"$CALLS"
[ "$1" != --no-ask-password ] || shift
while case $1 in --setenv=*) export "\${1#--setenv=}" ;; *) false ;; esac; do shift; done
case $1 in
show-image) grep -qxF "$2" "$STATE/images" ;;
remove) grep -qxF "$2" "$STATE/images" || { echo "No image '$2' known" >&2; exit 1; }
	grep -qxF "$2" "$STATE/running" && { echo "$2 is running" >&2; exit 1; }
	grep -vxF "$2" "$STATE/images" >"$STATE/i"; mv "$STATE/i" "$STATE/images" ;;
start) grep -qxF "$2" "$STATE/images" || { echo "No image '$2' known" >&2; exit 1; }
	echo "$2" >>"$STATE/running" ;;
poweroff) [ -n "$KEEP_RUNNING" ] || { grep -vxF "$2" "$STATE/running" >"$STATE/r"; mv "$STATE/r" "$STATE/running"; } ;;
terminate) grep -vxF "$2" "$STATE/running" >"$STATE/r"; mv "$STATE/r" "$STATE/running" ;;
-q) [ "$2" = shell ] && [ "$4" = /bin/sh ] && [ "$5" = -c ] || exit 1
	grep -qxF "$3" "$STATE/running" && ! grep -qxF "$3" "$STATE/dead" ||
		{ echo "Failed to get shell PTY: machine $3 is not up" >&2; exit 1; }
	if timeout 0.5 cat >/dev/null; then exit 0; fi
	printf '\\033]3008;start=x\\033\\\\'
	/bin/sh -c "$6" "$7" "$8" 2>&1 | sed 's/$/\\r/' ;;
*) exit 1 ;;
esac`,
  busctl: `
echo "busctl $*" >>"$CALLS"
case $6 in
ListImages) printf '{"type":"a(ssbttto)","data":[['
	sep=
	while read -r i; do printf '%s["%s","directory",false,1,1700000000000000,123456,"/i/%s"]' "$sep" "$i" "$i"; sep=,; done <"$STATE/images"
	printf ']]}\\n' ;;
ListMachines) printf '{"type":"a(ssso)","data":[[[".host","host","","/m/host"]'
	while read -r m; do printf ',["%s","container","nspawn","/m/%s"]' "$m" "$m"; done <"$STATE/running"
	printf ']]}\\n' ;;
*) exit 1 ;;
esac`,
  // The container's veth link, as the status script reads it.
  ip: `
echo "ip $*" >>"$CALLS"
echo '2: host0    inet 10.0.0.5/28 brd 10.0.0.15 scope global dynamic host0\\       valid_lft 3598sec preferred_lft 3598sec'`,
};

// The profiles: web, a full recipe, in the image's directory and again in
// the local one, which wins; plain, with image.conf alone; and two
// directories that are no profile.
function writeProfiles(dirs) {
  const web = path.join(dirs.profiles, "web");
  fs.mkdirSync(path.join(web, "extra", "etc"), { recursive: true });
  fs.mkdirSync(path.join(web, "extra", "usr", "bin"), { recursive: true });
  fs.mkdirSync(path.join(web, "extra", "etc", "systemd", "system", "multi-user.target.wants"), { recursive: true });
  fs.writeFileSync(path.join(web, "image.conf"), "Distribution=debian\nRelease=trixie\nPackages=nginx-light curl\n");
  fs.writeFileSync(path.join(web, "profile.conf"), "Title=Web (image)\nDescription=nginx on Debian\n");
  fs.writeFileSync(path.join(web, "extra", "etc", "motd"), "hello\n");
  fs.writeFileSync(path.join(web, "extra", "usr", "bin", "hi"), "#!/bin/sh\necho hi\n", { mode: 0o755 });
  fs.symlinkSync("/usr/lib/systemd/system/nginx.service",
    path.join(web, "extra", "etc", "systemd", "system", "multi-user.target.wants", "nginx.service"));
  fs.writeFileSync(path.join(web, "postinst"), "#!/bin/sh\nset -eu\nsystemctl enable systemd-networkd\n", { mode: 0o755 });
  fs.writeFileSync(path.join(web, "image.nspawn"), "[Exec]\nEnvironment=MODE=test\n");
  const plain = path.join(dirs.profiles, "plain");
  fs.mkdirSync(plain);
  fs.writeFileSync(path.join(plain, "image.conf"), "Distribution=arch\nRelease=\n");
  fs.mkdirSync(path.join(dirs.profiles, "Bad_Name"));
  fs.writeFileSync(path.join(dirs.profiles, "Bad_Name", "image.conf"), "Distribution=debian\n");
  fs.mkdirSync(path.join(dirs.profiles, "empty"));
  const local = path.join(dirs.local, "web");
  fs.mkdirSync(local, { recursive: true });
  fs.writeFileSync(path.join(local, "image.conf"), "Distribution=debian\nRelease=trixie\n");
  fs.writeFileSync(path.join(local, "profile.conf"), "Title=Web (local)\n");
}

// A temporary world: {dirs, env, reset, called, remove}. env is the
// environment a process under test needs on top of process.env.
function standins(prefix) {
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), prefix));
  const dirs = Object.fromEntries(["bin", "recipes", "logs", "state", "profiles", "local"]
    .map((d) => [d, path.join(tmp, d)]));
  Object.values(dirs).forEach((d) => fs.mkdirSync(d));
  const calls = path.join(tmp, "calls");
  for (const [name, body] of Object.entries(STANDINS)) {
    fs.writeFileSync(path.join(dirs.bin, name),
      `#!/bin/sh\nFAIL_UNIT=$(cat "$STATE/FAIL_UNIT") SLOW_UNIT=$(cat "$STATE/SLOW_UNIT") KEEP_RUNNING=$(cat "$STATE/KEEP_RUNNING")\n${body}\n`,
      { mode: 0o755 });
  }
  writeProfiles(dirs);
  const env = {
    PATH: `${dirs.bin}:${process.env.PATH}`, CALLS: calls, STATE: dirs.state, LOGS: dirs.logs,
    CHERRY_IMAGES_DIR: dirs.recipes, CHERRY_IMAGE_LOGS: dirs.logs, CHERRY_PROFILE_DIRS: `${dirs.local}:${dirs.profiles}`,
  };
  // reset(state, switches): the machine's state and the stand-ins' switches, afresh.
  const reset = (state = {}, switches = {}) => {
    fs.writeFileSync(calls, "");
    for (const key of ["images", "running", "dead"]) {
      fs.writeFileSync(path.join(dirs.state, key), (state[key] ?? []).map((i) => i + "\n").join(""));
    }
    for (const key of ["FAIL_UNIT", "SLOW_UNIT", "KEEP_RUNNING"]) fs.writeFileSync(path.join(dirs.state, key), switches[key] ?? "");
  };
  reset();
  const called = () => fs.readFileSync(calls, "utf8").trim().split("\n").filter(Boolean);
  const remove = () => fs.rmSync(tmp, { recursive: true, force: true });
  return { tmp, dirs, env, reset, called, remove };
}

module.exports = { standins };
