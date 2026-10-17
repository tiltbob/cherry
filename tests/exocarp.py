#!/usr/bin/env python3
"""exocarp, the SSH host key agent (package/exocarp/src/exocarp.c), against a swtpm standing in for the machine's
TPM. Built here with the host's compiler and tpm2-tss, it writes the public key of the TPM's primary key for its
template, for its owner alone: the key tpm2_createprimary shows for that template, and the same key after the TPM
restarts and after tpm2_clear. Then it serves the ssh-agent protocol: ssh-add lists the key; ssh-keygen signs with
it and verifies the signature; raw requests get their answers (a listing, a signature, which openssl verifies, a
failure for another key, for a malformed request and for an unknown one; two requests in one write and one split
across two; a connection kept open while another is served; a connection closed on an impossible length); and sshd,
with HostKeyAgent and the public key, completes a key exchange with a client, which records the TPM's key.
Needs cc, pkg-config and tpm2-tss's development files (libtss2-dev), swtpm, openssl, and ssh-add and ssh-keygen
(openssh-client); tpm2-tools for the comparison with tpm2_createprimary and the clear, and sshd (openssh-server)
for the key exchange, each left out without. Skipped, saying so, without the others."""

import base64
import os
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
SOURCE = os.path.join(ROOT, "package/exocarp/src/exocarp.c")
# The template, as package/exocarp/src/exocarp.c has it, for tpm2_createprimary.
TEMPLATE = ["-C", "e", "-G", "ecc256:ecdsa-sha256", "-g", "sha256",
            "-a", "fixedtpm|fixedparent|sensitivedataorigin|userwithauth|sign|noda"]
KEY_TYPE = b"ecdsa-sha2-nistp256"
SSH_AGENT_FAILURE = 5
SSH_AGENTC_REQUEST_IDENTITIES = 11
SSH_AGENT_IDENTITIES_ANSWER = 12
SSH_AGENTC_SIGN_REQUEST = 13
SSH_AGENT_SIGN_RESPONSE = 14


def fail(message):
    print(f"exocarp: {message}", file=sys.stderr)
    sys.exit(1)


def check(condition, message):
    if not condition:
        fail(message)


def skip(message):
    print(f"exocarp: SKIPPED, {message} (package/exocarp unchecked)")
    sys.exit(0)


def run(cmd, **kwargs):
    return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, **kwargs)


def wait_for(path, proc, what, log=None, timeout=10):
    deadline = time.monotonic() + timeout
    while not os.path.exists(path):
        if proc.poll() is not None or time.monotonic() > deadline:
            with open(log) if log else open(os.devnull) as f:
                fail(f"{what} did not start\n{f.read()}")
        time.sleep(0.05)


class Swtpm:
    """A swtpm with its state in a directory, so a restart is the same TPM, on a unix socket (the swtpm TCTI wants
    the control socket at <path>.ctrl). Reached directly, not through a resource manager: transient keys stay in
    it until flushed, and a restart flushes them all, as a reset does."""

    def __init__(self, state, path):
        self.state, self.path = state, path
        os.makedirs(state, exist_ok=True)
        self.start()

    def start(self):
        # Its chatter (a line per client gone) in a log, shown when it fails to start.
        self.log = open(os.path.join(self.state, "swtpm.log"), "a")
        self.proc = subprocess.Popen(["swtpm", "socket", "--tpm2", "--tpmstate", f"dir={self.state}",
                                      "--server", f"type=unixio,path={self.path}",
                                      "--ctrl", f"type=unixio,path={self.path}.ctrl",
                                      "--flags", "not-need-init,startup-clear"], stdout=self.log, stderr=self.log)
        wait_for(self.path, self.proc, "swtpm", self.log.name)
        wait_for(self.path + ".ctrl", self.proc, "swtpm", self.log.name)

    def stop(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(10)
        self.log.close()

    def restart(self):
        self.stop()
        self.start()

    @property
    def tcti(self):
        return f"swtpm:path={self.path}"


def string(data):
    return struct.pack(">I", len(data)) + data


def strings(data):
    """The strings an SSH blob is made of."""
    out = []
    while data:
        n = struct.unpack(">I", data[:4])[0]
        out.append(data[4:4 + n])
        data = data[4 + n:]
    return out


def message(kind, body=b""):
    return struct.pack(">I", 1 + len(body)) + bytes([kind]) + body


def recv_exact(conn, n):
    data = b""
    while len(data) < n:
        chunk = conn.recv(n - len(data))
        if not chunk:
            return None
        data += chunk
    return data


def recv_message(conn):
    """(type, body) of the agent's next reply, or None once it closed the connection."""
    header = recv_exact(conn, 4)
    if header is None:
        return None
    body = recv_exact(conn, struct.unpack(">I", header)[0])
    return body[0], body[1:]


def connect(path):
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(30)
    conn.connect(path)
    return conn


def ecdsa_der(blob):
    """The SSH ECDSA signature blob as DER, for openssl: SEQUENCE of the two INTEGERs, whose encoding is the mpints'."""
    kind, rs = strings(blob)
    check(kind == KEY_TYPE, f"a signature of type {kind!r}")
    r, s = strings(rs)
    integers = b"".join(b"\x02" + bytes([len(x)]) + x for x in (r, s))
    return b"\x30" + bytes([len(integers)]) + integers


def public_key(exe, tcti, path):
    """The public key the agent writes: a P-256 key in OpenSSH's format, the file its owner's alone."""
    p = run([exe, "-T", tcti, "-p", path])
    check(p.returncode == 0, f"exocarp -p failed:\n{p.stdout}")
    with open(path) as f:
        line = f.read()
    check(stat.S_IMODE(os.stat(path).st_mode) == 0o600, f"{path} is not mode 600")
    fields = line.split()
    check(len(fields) == 3 and fields[0] == KEY_TYPE.decode() and fields[2] == "exocarp" and line.endswith("\n"),
          f"not a public key line: {line!r}")
    parts = strings(base64.b64decode(fields[1]))
    check(parts[:2] == [KEY_TYPE, b"nistp256"] and len(parts) == 3 and len(parts[2]) == 65 and parts[2][0] == 4,
          f"not a P-256 key blob: {parts}")
    return line


def main():
    for tool in ("cc", "pkg-config", "swtpm", "openssl", "ssh-add", "ssh-keygen"):
        if not shutil.which(tool):
            skip(f"{tool} is not installed")
    flags = run(["pkg-config", "--cflags", "--libs", "tss2-esys", "tss2-tctildr", "tss2-rc", "libcrypto"])
    if flags.returncode != 0:
        skip("tpm2-tss's development files are not installed (libtss2-dev)")
    left_out = []
    # Short: the sockets' paths are limited to 108 bytes.
    with tempfile.TemporaryDirectory(prefix="exocarp-") as work:
        exe = os.path.join(work, "exocarp")
        p = run(["cc", "-std=gnu11", "-O2", "-Wall", "-Wextra", "-Werror", "-o", exe, SOURCE] + flags.stdout.split())
        check(p.returncode == 0, f"exocarp.c does not build without warnings:\n{p.stdout}")

        tpm = Swtpm(os.path.join(work, "state"), os.path.join(work, "tpm"))
        env = dict(os.environ, TPM2TOOLS_TCTI=tpm.tcti, SSH_AUTH_SOCK=os.path.join(work, "socket"))
        agent = sshd = None
        try:
            # The key: the same at every start of the agent and of the TPM, and the endorsement seed's, which a
            # clear keeps.
            key_path = os.path.join(work, "key.pub")
            key = public_key(exe, tcti=tpm.tcti, path=key_path)
            check(public_key(exe, tpm.tcti, os.path.join(work, "again.pub")) == key, "the key changed between runs")
            tpm.restart()
            check(public_key(exe, tpm.tcti, os.path.join(work, "restart.pub")) == key,
                  "the key changed when the TPM restarted")
            if shutil.which("tpm2_createprimary") and shutil.which("tpm2_clear"):
                pem = os.path.join(work, "primary.pem")
                p = run(["tpm2_createprimary", "-Q"] + TEMPLATE
                        + ["-c", os.path.join(work, "primary.ctx"), "-f", "pem", "-o", pem], env=env)
                check(p.returncode == 0, f"tpm2_createprimary failed:\n{p.stdout}")
                tpm.restart()  # flushes what tpm2_createprimary left loaded
                p = run(["ssh-keygen", "-i", "-m", "PKCS8", "-f", pem])
                check(p.returncode == 0 and p.stdout.split() == key.split()[:2],
                      f"tpm2_createprimary shows another key for the template:\n{p.stdout}\n{key}")
                p = run(["tpm2_clear"], env=env)
                check(p.returncode == 0, f"tpm2_clear failed:\n{p.stdout}")
                check(public_key(exe, tpm.tcti, os.path.join(work, "cleared.pub")) == key,
                      "the key changed when the TPM was cleared")
            else:
                left_out.append("tpm2_createprimary and the clear unchecked without tpm2-tools")

            # The agent, on a socket of its own: for its owner alone.
            log_path = os.path.join(work, "agent.log")
            agent = subprocess.Popen([exe, "-T", tpm.tcti, "-l", env["SSH_AUTH_SOCK"]], stderr=open(log_path, "w"))
            wait_for(env["SSH_AUTH_SOCK"], agent, "the agent", log_path)
            check(stat.S_IMODE(os.stat(env["SSH_AUTH_SOCK"]).st_mode) == 0o600, "the socket is not mode 600")
            p = run(["ssh-add", "-L"], env=env)
            check(p.returncode == 0 and p.stdout == key, f"ssh-add -L does not list the key:\n{p.stdout}")

            # Raw requests. The first connection stays open, as sshd keeps one per session, while others are served.
            blob = base64.b64decode(key.split()[1])
            first = connect(env["SSH_AUTH_SOCK"])
            first.sendall(message(SSH_AGENTC_REQUEST_IDENTITIES))
            listing = (SSH_AGENT_IDENTITIES_ANSWER, struct.pack(">I", 1) + string(blob) + string(b"exocarp"))
            check(recv_message(first) == listing, "the identities answer is wrong")
            second = connect(env["SSH_AUTH_SOCK"])
            data = b"a key exchange hash, say"
            second.sendall(message(SSH_AGENTC_SIGN_REQUEST, string(blob) + string(data) + struct.pack(">I", 0)))
            reply = recv_message(second)
            check(reply is not None and reply[0] == SSH_AGENT_SIGN_RESPONSE, f"no signature: {reply}")
            signature = ecdsa_der(strings(reply[1])[0])
            with open(os.path.join(work, "data"), "wb") as f:
                f.write(data)
            with open(os.path.join(work, "signature.der"), "wb") as f:
                f.write(signature)
            p = run(["ssh-keygen", "-e", "-m", "PKCS8", "-f", key_path])
            check(p.returncode == 0, f"ssh-keygen -e failed:\n{p.stdout}")
            with open(os.path.join(work, "key.pem"), "w") as f:
                f.write(p.stdout)
            p = run(["openssl", "dgst", "-sha256", "-verify", os.path.join(work, "key.pem"),
                     "-signature", os.path.join(work, "signature.der"), os.path.join(work, "data")])
            check(p.returncode == 0 and p.stdout.strip() == "Verified OK", f"openssl does not verify:\n{p.stdout}")
            other = blob[:-1] + bytes([blob[-1] ^ 1])
            second.sendall(message(SSH_AGENTC_SIGN_REQUEST, string(other) + string(data) + struct.pack(">I", 0)))
            check(recv_message(second) == (SSH_AGENT_FAILURE, b""), "another key's request did not fail")
            second.sendall(message(SSH_AGENTC_SIGN_REQUEST, string(blob) + string(data)))
            check(recv_message(second) == (SSH_AGENT_FAILURE, b""), "a request without flags did not fail")
            second.sendall(message(1))
            check(recv_message(second) == (SSH_AGENT_FAILURE, b""), "an unknown request did not fail")
            second.sendall(message(SSH_AGENTC_REQUEST_IDENTITIES) + message(SSH_AGENTC_REQUEST_IDENTITIES))
            check(recv_message(second) == listing and recv_message(second) == listing,
                  "two requests in one write were not both answered")
            split = message(SSH_AGENTC_SIGN_REQUEST, string(blob) + string(data) + struct.pack(">I", 0))
            second.sendall(split[:7])
            time.sleep(0.2)
            second.sendall(split[7:])
            reply = recv_message(second)
            check(reply is not None and reply[0] == SSH_AGENT_SIGN_RESPONSE, f"a split request got {reply}")
            second.close()
            first.sendall(message(SSH_AGENTC_REQUEST_IDENTITIES))
            check(recv_message(first) == listing, "the first connection was not served after the second")
            first.close()
            third = connect(env["SSH_AUTH_SOCK"])
            third.sendall(struct.pack(">I", 0x7fffffff) + b"x")
            check(recv_message(third) is None, "an impossible length did not close the connection")
            third.close()
            with open(log_path) as f:
                messages = f.read().splitlines()
            check(messages == ["sign request for a key that is not the host key", "malformed sign request",
                               "request of type 1 is not supported",
                               "request of 2147483647 bytes: not the agent protocol"],
                  "the agent's log is not the failures:\n" + "\n".join(messages))

            # OpenSSH signs through the agent, with the public key alone, and verifies.
            p = run(["ssh-keygen", "-Y", "sign", "-f", key_path, "-n", "cherry", os.path.join(work, "data")], env=env)
            check(p.returncode == 0, f"ssh-keygen -Y sign failed:\n{p.stdout}")
            with open(os.path.join(work, "allowed_signers"), "w") as f:
                f.write("cherry " + " ".join(key.split()[:2]) + "\n")
            with open(os.path.join(work, "data")) as f:
                p = run(["ssh-keygen", "-Y", "verify", "-f", os.path.join(work, "allowed_signers"), "-I", "cherry",
                         "-n", "cherry", "-s", os.path.join(work, "data.sig")], stdin=f)
            check(p.returncode == 0 and p.stdout.startswith("Good "), f"ssh-keygen -Y verify failed:\n{p.stdout}")

            # sshd, with the agent and the public key, and a client that verifies the host key in the key exchange
            # and gets as far as authentication, where it has no key.
            if shutil.which("sshd"):
                with socket.socket() as s:
                    s.bind(("127.0.0.1", 0))
                    port = s.getsockname()[1]
                config = os.path.join(work, "sshd_config")
                with open(config, "w") as f:
                    f.write(f"Port {port}\nListenAddress 127.0.0.1\nHostKeyAgent {env['SSH_AUTH_SOCK']}\n"
                            f"HostKey {key_path}\nPidFile none\nAuthorizedKeysFile none\nPasswordAuthentication no\n"
                            "KbdInteractiveAuthentication no\nUsePAM no\n")
                sshd_log = os.path.join(work, "sshd.log")
                sshd = subprocess.Popen([shutil.which("sshd"), "-D", "-e", "-f", config], stderr=open(sshd_log, "w"))
                deadline = time.monotonic() + 10
                while True:
                    try:
                        socket.create_connection(("127.0.0.1", port), timeout=1).close()
                        break
                    except OSError:
                        if sshd.poll() is not None or time.monotonic() > deadline:
                            with open(sshd_log) as f:
                                fail(f"sshd did not start:\n{f.read()}")
                        time.sleep(0.1)
                known_hosts = os.path.join(work, "known_hosts")
                p = run(["ssh", "-p", str(port), "-F", "/dev/null", "-o", f"UserKnownHostsFile={known_hosts}",
                         "-o", "StrictHostKeyChecking=accept-new", "-o", "BatchMode=yes", "-o", "IdentityAgent=none",
                         "-o", "IdentitiesOnly=yes", "-o", "ConnectTimeout=10", "cherry@127.0.0.1", "true"])
                check(p.returncode == 255 and "Permission denied" in p.stdout,
                      f"ssh did not get as far as authentication (exit {p.returncode}):\n{p.stdout}")
                with open(known_hosts) as f:
                    recorded = f.read().split()
                check(recorded[1:3] == key.split()[:2], f"the client recorded another host key: {recorded}")
                # sshd tries a HostKey as a private key first: a readable one of its own gets a warning about
                # its permissions, hence the key file's mode (OpenSSH 9.6 also logs that it could not load the
                # public key as a private key; 10.5, the image's, does not when an agent is set).
                with open(sshd_log) as f:
                    noise = [line for line in f.read().splitlines() if "permission" in line.lower()]
                check(noise == [], "sshd complained about the key file's permissions:\n" + "\n".join(noise))
            else:
                left_out.append("sshd's key exchange unchecked without sshd")
        finally:
            for proc in (sshd, agent):
                if proc is not None and proc.poll() is None:
                    proc.terminate()
                    proc.wait(10)
            tpm.stop()
        check(agent.returncode == 0, f"the agent exited with {agent.returncode} on SIGTERM")
    print("exocarp: the key is the TPM's primary key, the same after a restart and a clear; ssh-add, ssh-keygen,"
          " the raw requests and sshd's key exchange went as they should"
          + "".join(f"; {what}" for what in left_out))


if __name__ == "__main__":
    main()
