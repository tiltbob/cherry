#!/usr/bin/env python3
"""Cherry's Secure Boot keys: a PKCS#11 token (a Nitrokey HSM 2) is the root of trust.

Three RSA-2048 keys are generated on the token and never leave it:

  PK    the platform key: whoever holds it owns the machine's Secure Boot keys
  KEK   the key exchange key: signs changes to db and dbx
  db    the Cherry Secure Boot CA, whose certificate is the one entry of db
        Cherry ships: the firmware boots an EFI binary signed by any
        certificate the CA issued

A fourth key signs releases in CI, which cannot reach the token: `issue` makes
it outside the token, has the CA certify it, and hands the private key over
once, for a GitHub secret. A new one replaces it without touching a machine.

Commands:
  init    generate the three keys on the token, and write their certificates
          (PK.crt, KEK.crt, db.crt) and their signed variable updates (PK.auth,
          self-signed; KEK.auth, signed by the PK; db.auth, signed by the KEK) to
          board/x86_64/secureboot/, which the image enrolls in setup mode
  issue   a new release signing key and its certificate, release.crt, from the CA
  check   the files in board/x86_64/secureboot/ are what init and issue write,
          their signatures verify, and the chain holds (make check)

The token is reached through OpenSC's PKCS#11 module (or --module), with
pkcs11-tool for key generation and OpenSSL 3's pkcs11-provider for signing:
apt install opensc pkcs11-provider. The PIN is asked once, or taken from
CHERRY_HSM_PIN.
"""

import argparse
import datetime
import getpass
import os
import struct
import subprocess
import sys
import tempfile
import urllib.parse
import uuid

KEYS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "board", "x86_64", "secureboot")

# The owner of every signature list entry Cherry writes, and the GUIDs of the UEFI specification.
CHERRY_OWNER = uuid.UUID("37310110-2be7-463c-b42a-63e438edf4d7")
EFI_GLOBAL_VARIABLE = uuid.UUID("8be4df61-93ca-11d2-aa0d-00e098032b8c")  # PK, KEK
EFI_IMAGE_SECURITY_DATABASE = uuid.UUID("d719b2cb-3d3a-4596-a3bc-dad00e67656f")  # db, dbx
EFI_CERT_X509 = uuid.UUID("a5c059a1-94e4-4aa7-87b5-ab155c2bf072")
EFI_CERT_SHA256 = uuid.UUID("c1c41626-504c-4092-aca9-41f936934328")
EFI_CERT_TYPE_PKCS7 = uuid.UUID("4aafd29d-68df-49ee-8aa9-347d375665a7")
# NON_VOLATILE | BOOTSERVICE_ACCESS | RUNTIME_ACCESS | TIME_BASED_AUTHENTICATED_WRITE_ACCESS
AUTH_ATTRS = 0x27
APPEND_WRITE = 0x40
WIN_CERT_TYPE_EFI_GUID = 0x0EF1
OID_SIGNED_DATA = bytes.fromhex("06092a864886f70d010702")  # 1.2.840.113549.1.7.2

# name: (token object label, certificate subject, variable, its GUID, the key that signs its update)
KEYS = {
    "PK": ("cherry-pk", "/CN=Cherry Platform Key/", "PK", EFI_GLOBAL_VARIABLE, "PK"),
    "KEK": ("cherry-kek", "/CN=Cherry Key Exchange Key/", "KEK", EFI_GLOBAL_VARIABLE, "PK"),
    "db": ("cherry-db", "/CN=Cherry Secure Boot CA/", "db", EFI_IMAGE_SECURITY_DATABASE, "KEK"),
}
ROOT_DAYS = 30 * 365 + 7  # firmware never checks validity dates
RELEASE_DAYS = 5 * 365 + 1

# The token's key certificates: self-signed CAs, as `openssl req -x509` makes them by default.
ROOT_EXTENSIONS = """[ext]
basicConstraints = critical, CA:true
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always
"""
RELEASE_EXTENSIONS = """[ext]
basicConstraints = critical, CA:false
keyUsage = critical, digitalSignature
extendedKeyUsage = codeSigning
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid:always
"""

OPENSC_MODULES = [
    "/usr/lib/x86_64-linux-gnu/opensc-pkcs11.so",
    "/usr/lib/aarch64-linux-gnu/opensc-pkcs11.so",
    "/usr/lib64/opensc-pkcs11.so",
    "/usr/lib/opensc-pkcs11.so",
    "/usr/local/lib/opensc-pkcs11.so",
    "/opt/homebrew/lib/opensc-pkcs11.so",
]


def fail(message):
    sys.exit(f"secureboot: {message}")


def note(message):
    print(message, file=sys.stderr)


def run(cmd, env=None, data=None):
    """Run a tool; its stdout as bytes, or fail with its stderr."""
    p = subprocess.run(cmd, input=data, capture_output=True, env=env)
    if p.returncode != 0:
        fail(f"{cmd[0]} {cmd[1]} failed:\n{p.stderr.decode(errors='replace').strip()}")
    return p.stdout


# --- UEFI structures -------------------------------------------------------------------------------------------

def esl(sig_type, entries):
    """An EFI_SIGNATURE_LIST of same-sized entries, each owned by Cherry."""
    size = 16 + len(entries[0])
    assert all(len(e) == len(entries[0]) for e in entries)
    return (sig_type.bytes_le + struct.pack("<III", 28 + size * len(entries), 0, size)
            + b"".join(CHERRY_OWNER.bytes_le + e for e in entries))


def efi_time(when):
    """EFI_TIME of an authenticated variable: Pad1, Nanosecond, TimeZone, Daylight and Pad2 zero."""
    return struct.pack("<HBBBBBBIhBB", when.year, when.month, when.day, when.hour, when.minute, when.second,
                       0, 0, 0, 0, 0)


def to_be_signed(name, guid, attrs, timestamp, data):
    """What the signature of an EFI_VARIABLE_AUTHENTICATION_2 covers."""
    return name.encode("utf-16-le") + guid.bytes_le + struct.pack("<I", attrs) + timestamp + data


def auth2(timestamp, signed_data, data):
    """EFI_VARIABLE_AUTHENTICATION_2 and the data: a .auth file, what SetVariable() takes after the attributes."""
    return (timestamp + struct.pack("<IHH", 24 + len(signed_data), 0x0200, WIN_CERT_TYPE_EFI_GUID)
            + EFI_CERT_TYPE_PKCS7.bytes_le + signed_data + data)


def parse_auth2(blob):
    """(timestamp, PKCS#7 SignedData, data) of a .auth file."""
    length, revision, cert_type = struct.unpack_from("<IHH", blob, 16)
    if revision != 0x0200 or cert_type != WIN_CERT_TYPE_EFI_GUID or blob[24:40] != EFI_CERT_TYPE_PKCS7.bytes_le:
        raise ValueError("not an EFI_VARIABLE_AUTHENTICATION_2 with a PKCS#7 signature")
    if length < 24 or 16 + length > len(blob):
        raise ValueError(f"bad signature length {length}")
    return blob[:16], blob[40:16 + length], blob[16 + length:]


def parse_esl(blob):
    """[(signature type, owner, data)] of a sequence of EFI_SIGNATURE_LISTs."""
    entries, off = [], 0
    while off < len(blob):
        sig_type = uuid.UUID(bytes_le=blob[off:off + 16])
        list_size, header_size, size = struct.unpack_from("<III", blob, off + 16)
        if list_size < 28 or off + list_size > len(blob) or size <= 16 or (list_size - 28 - header_size) % size:
            raise ValueError(f"bad EFI_SIGNATURE_LIST at {off}")
        for at in range(off + 28 + header_size, off + list_size, size):
            entries.append((sig_type, uuid.UUID(bytes_le=blob[at:at + 16]), blob[at + 16:at + size]))
        off += list_size
    return entries


# --- DER: UEFI wants a bare PKCS#7 SignedData, OpenSSL reads and writes it inside a ContentInfo -----------------

def der_tlv(blob, off=0):
    """(tag, content start, end) of the DER element at off."""
    tag, first = blob[off], blob[off + 1]
    if first < 0x80:
        return tag, off + 2, off + 2 + first
    n = first & 0x7F
    return tag, off + 2 + n, off + 2 + n + int.from_bytes(blob[off + 2:off + 2 + n], "big")


def der(tag, content):
    n = len(content)
    if n < 0x80:
        return bytes([tag, n]) + content
    length = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([tag, 0x80 | len(length)]) + length + content


def unwrap_content_info(blob):
    _, start, _ = der_tlv(blob)  # ContentInfo SEQUENCE
    _, _, oid_end = der_tlv(blob, start)
    if blob[start:oid_end] != OID_SIGNED_DATA:
        raise ValueError("not a PKCS#7 SignedData")
    _, inner, _ = der_tlv(blob, oid_end)  # [0] EXPLICIT
    _, _, end = der_tlv(blob, inner)
    return blob[inner:end]


def wrap_content_info(signed_data):
    return der(0x30, OID_SIGNED_DATA + der(0xA0, signed_data))


# --- the token -------------------------------------------------------------------------------------------------

class Token:
    """The PKCS#11 token: key generation with pkcs11-tool, signing through OpenSSL's pkcs11-provider."""

    def __init__(self, args, tmp):
        self.module = args.module or os.environ.get("PKCS11_MODULE") or next(
            (m for m in OPENSC_MODULES if os.path.exists(m)), None)
        if not self.module:
            fail("OpenSC's PKCS#11 module not found: install opensc, or pass --module")
        self.label = args.token
        pin = os.environ.get("CHERRY_HSM_PIN") or getpass.getpass("User PIN of the token: ")
        # OpenSSL reads the PIN from a file of a private directory, pkcs11-tool from the environment: neither
        # shows it in the process list.
        self.pin_file = os.path.join(tmp, "pin")
        with open(os.open(self.pin_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as f:
            f.write(pin)
        self.env = dict(os.environ, CHERRY_HSM_PIN=pin, PKCS11_PROVIDER_MODULE=self.module)
        self.provider = ["-provider", "pkcs11", "-provider", "default"]

    def tool(self, *args):
        cmd = ["pkcs11-tool", "--module", self.module]
        if self.label:
            cmd += ["--token-label", self.label]
        return run(cmd + ["--login", "--pin", "env:CHERRY_HSM_PIN", *args], env=self.env).decode()

    def private_keys(self):
        out = self.tool("--list-objects", "--type", "privkey")
        return [line.split(":", 1)[1].strip() for line in out.splitlines() if line.strip().startswith("label:")]

    def generate(self, label):
        # The ID is the label: unique, and what pairs the key with the certificate stored next to it.
        self.tool("--keypairgen", "--key-type", "rsa:2048", "--usage-sign", "--label", label,
                  "--id", label.encode().hex())

    def store_certificate(self, label, der_file):
        self.tool("--write-object", der_file, "--type", "cert", "--label", label, "--id", label.encode().hex())

    def key_uri(self, label):
        path = f"object={urllib.parse.quote(label)};type=private"
        if self.label:
            path = f"token={urllib.parse.quote(self.label)};{path}"
        return f"pkcs11:{path}?pin-source=file:{urllib.parse.quote(self.pin_file)}"

    def openssl(self, command, *args, data=None):
        return run(["openssl", command, *self.provider, *args], env=self.env, data=data)


def write_config(tmp, text):
    path = os.path.join(tmp, "ext.cnf")
    with open(path, "w") as f:
        f.write(text)
    return path


def signed_update(token, tmp, var, data, signer_crt, signer_label, when):
    """The .auth file that sets `var` to `data`, signed by the signer's key on the token."""
    _, _, name, guid, _ = KEYS[var]
    timestamp = efi_time(when)
    tbs = os.path.join(tmp, f"{var}.tbs")
    with open(tbs, "wb") as f:
        f.write(to_be_signed(name, guid, AUTH_ATTRS, timestamp, data))
    # A detached signature without signed attributes, as the UEFI specification asks.
    p7 = token.openssl("smime", "-sign", "-binary", "-noattr", "-md", "sha256", "-outform", "DER",
                       "-signer", signer_crt, "-inkey", token.key_uri(signer_label), "-in", tbs)
    return auth2(timestamp, unwrap_content_info(p7), data)


def pem_to_der(pem_file):
    return run(["openssl", "x509", "-in", pem_file, "-outform", "DER"])


def cmd_init(args):
    files = [f"{var}.{ext}" for var in KEYS for ext in ("crt", "auth")]
    existing = [f for f in files if os.path.exists(os.path.join(args.dir, f))]
    if existing:
        fail(f"{', '.join(existing)} exist already: init makes a new root of trust only once")
    with tempfile.TemporaryDirectory(prefix="cherry-secureboot-") as tmp:
        token = Token(args, tmp)
        present = set(token.private_keys())
        taken = [label for label, *_ in KEYS.values() if label in present]
        if taken:
            fail(f"the token holds {', '.join(taken)} already: init generates them, it never reuses a key")
        os.makedirs(args.dir, exist_ok=True)
        ext = write_config(tmp, ROOT_EXTENSIONS)
        crt = {}
        for var, (label, subject, *_) in KEYS.items():
            note(f"generating {label} on the token")
            token.generate(label)
            crt[var] = os.path.join(args.dir, f"{var}.crt")
            token.openssl("req", "-new", "-x509", "-key", token.key_uri(label), "-subj", subject, "-sha256",
                          "-days", str(args.days), "-config", ext, "-extensions", "ext", "-out", crt[var])
            der_file = os.path.join(tmp, f"{var}.der")
            with open(der_file, "wb") as f:
                f.write(pem_to_der(crt[var]))
            token.store_certificate(label, der_file)
        when = datetime.datetime.now(datetime.timezone.utc)
        for var, (*_, signer) in KEYS.items():
            data = esl(EFI_CERT_X509, [pem_to_der(crt[var])])
            update = signed_update(token, tmp, var, data, crt[signer], KEYS[signer][0], when)
            with open(os.path.join(args.dir, f"{var}.auth"), "wb") as f:
                f.write(update)
    check(args.dir, require_release=False)
    note(f"wrote {', '.join(files)} to {args.dir}.\n"
         "Commit them, then make the release signing key: scripts/secureboot.py issue")


def cmd_issue(args):
    ca = os.path.join(args.dir, "db.crt")
    if not os.path.exists(ca):
        fail(f"no {ca}: run scripts/secureboot.py init first")
    if args.key_out != "-" and os.path.exists(args.key_out):
        fail(f"{args.key_out} exists")
    with tempfile.TemporaryDirectory(prefix="cherry-secureboot-") as tmp:
        token = Token(args, tmp)
        key = os.path.join(tmp, "release.key")
        run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", key])
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        crt = os.path.join(tmp, "release.crt")
        token.openssl("req", "-new", "-x509", "-key", key, "-CA", ca, "-CAkey", token.key_uri(KEYS["db"][0]),
                      "-subj", f"/CN=Cherry Release Signing {stamp}/", "-sha256", "-days", str(args.days),
                      "-config", write_config(tmp, RELEASE_EXTENSIONS), "-extensions", "ext", "-out", crt)
        with open(key, "rb") as f:
            pem = f.read()
        if args.key_out == "-":
            sys.stdout.buffer.write(pem)
        else:
            with open(os.open(args.key_out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as f:
                f.write(pem)
        os.replace(crt, os.path.join(args.dir, "release.crt"))
    check(args.dir, require_release=True)
    where = "stdout" if args.key_out == "-" else args.key_out
    note(f"wrote release.crt to {args.dir} and its private key to {where}.\n"
         "Store the key as the secret SECUREBOOT_KEY of the release environment, e.g.\n"
         "  gh secret set SECUREBOOT_KEY --env release < <key file>\n"
         "then delete the key file, and commit release.crt.")


# --- check -----------------------------------------------------------------------------------------------------

def verify_signature(tmp, var, update, signer_crt):
    """The .auth's signature verifies, signed by signer_crt itself; return the data it sets."""
    timestamp, signed_data, data = parse_auth2(update)
    _, _, name, guid, _ = KEYS[var]
    paths = {k: os.path.join(tmp, f"{var}.{k}") for k in ("p7", "tbs", "signer")}
    with open(paths["p7"], "wb") as f:
        f.write(wrap_content_info(signed_data))
    with open(paths["tbs"], "wb") as f:
        f.write(to_be_signed(name, guid, AUTH_ATTRS, timestamp, data))
    p = subprocess.run(["openssl", "smime", "-verify", "-binary", "-inform", "DER", "-in", paths["p7"],
                        "-content", paths["tbs"], "-CAfile", signer_crt, "-purpose", "any", "-no_check_time",
                        "-signer", paths["signer"], "-out", os.devnull], capture_output=True)
    if p.returncode != 0:
        raise ValueError(f"its signature does not verify with {os.path.basename(signer_crt)}: "
                         + p.stderr.decode(errors="replace").strip().splitlines()[-1])
    if pem_to_der(paths["signer"]) != pem_to_der(signer_crt):
        raise ValueError(f"signed by another certificate than {os.path.basename(signer_crt)}")
    if timestamp[7:] != bytes(9):
        raise ValueError("its timestamp has a nonzero Pad1, Nanosecond, TimeZone, Daylight or Pad2")
    return data


def rsa_bits(crt):
    text = run(["openssl", "x509", "-in", crt, "-noout", "-text"]).decode()
    for line in text.splitlines():
        if "Public-Key: (" in line:
            return int(line.split("(")[1].split()[0])
    return 0


def check(keys_dir, require_release=False):
    """Every file of init (and issue) is there and right; return the number of files, 0 when there are none."""
    names = [f"{var}.{ext}" for var in KEYS for ext in ("crt", "auth")] + ["release.crt"]
    present = [n for n in names if os.path.exists(os.path.join(keys_dir, n))]
    if not present:
        return 0
    missing = [n for n in names[:-1] if n not in present]
    if require_release and "release.crt" not in present:
        missing.append("release.crt")
    if missing:
        fail(f"{keys_dir} lacks {', '.join(missing)}: init writes them all")
    path = {n: os.path.join(keys_dir, n) for n in present}
    with tempfile.TemporaryDirectory(prefix="cherry-secureboot-") as tmp:
        for var, (*_, signer) in KEYS.items():
            crt = path[f"{var}.crt"]
            if rsa_bits(crt) != 2048:
                fail(f"{var}.crt: not an RSA-2048 key, the size every firmware verifies")
            with open(path[f"{var}.auth"], "rb") as f:
                update = f.read()
            try:
                data = verify_signature(tmp, var, update, path[f"{signer}.crt"])
                entries = parse_esl(data)
            except ValueError as e:
                fail(f"{var}.auth: {e}")
            if entries != [(EFI_CERT_X509, CHERRY_OWNER, pem_to_der(crt))]:
                fail(f"{var}.auth does not set {var} to {var}.crt alone")
        if subprocess.run(["openssl", "verify", "-CAfile", path["db.crt"], "-x509_strict", path["db.crt"]],
                          capture_output=True).returncode != 0:
            fail("db.crt: not a self-signed CA certificate")
        if "release.crt" in path:
            if subprocess.run(["openssl", "verify", "-CAfile", path["db.crt"], "-purpose", "any", path["release.crt"]],
                              capture_output=True).returncode != 0:
                fail("release.crt: not issued by db.crt")
            if rsa_bits(path["release.crt"]) != 2048:
                fail("release.crt: not an RSA-2048 key")
    return len(present)


def cmd_check(args):
    if check(args.dir) == 0:
        print(f"secureboot: no keys in {args.dir} yet (scripts/secureboot.py init)")
        return
    issued = " that issued release.crt" if os.path.exists(os.path.join(args.dir, "release.crt")) else ""
    print(f"secureboot: {args.dir}: the .auth files set PK, KEK and db to their certificates, signed by the PK, "
          f"the PK and the KEK; db.crt is a CA{issued}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dir", default=os.path.normpath(KEYS_DIR), help="the key files (default: %(default)s)")
    sub = parser.add_subparsers(dest="command", required=True)
    for name, days in (("init", ROOT_DAYS), ("issue", RELEASE_DAYS)):
        p = sub.add_parser(name)
        p.add_argument("--module", help="the token's PKCS#11 module (default: OpenSC's)")
        p.add_argument("--token", help="the token's label (default: any token holding the keys)")
        p.add_argument("--days", type=int, default=days, help="validity of the certificates (default: %(default)s)")
        if name == "issue":
            p.add_argument("--key-out", required=True, metavar="FILE",
                           help="where the release signing key goes, a new file or - for stdout")
    sub.add_parser("check")
    args = parser.parse_args()
    {"init": cmd_init, "issue": cmd_issue, "check": cmd_check}[args.command](args)


if __name__ == "__main__":
    main()
