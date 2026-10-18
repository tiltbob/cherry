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

A fifth, the PCR key, signs the policy that binds the disks' keys to a release
(scripts/pcrsign.py): its public half goes into every release's UKI, its private
half into a GitHub secret too. `pcr` makes it, outside the token: the CA has no
part in it, as the signed UKI vouches for the key. A new one leaves every disk
sealed under the old one locked: it is never replaced lightly.

Commands:
  init    generate the three keys on the token, and write their certificates
          (PK.crt, KEK.crt, db.crt) and their signed variable updates (PK.auth,
          self-signed; KEK.auth, signed by the PK; db.auth, signed by the KEK) to
          board/x86_64/secureboot/, which the image enrolls in setup mode
  issue   a new release signing key and its certificate, release.crt, from the CA
  pcr     the PCR key: its public half, pcr.pem, to board/x86_64/secureboot/,
          and the private key for the secret
  check   the files in board/x86_64/secureboot/ are what init, issue and pcr
          write, their signatures verify, and the chain holds (make check)

The token is reached through OpenSC's pkcs11-tool and PKCS#11 module (or
--module): it generates the keys and makes every signature with them, over
certificates and signed data this script puts together. OpenSSL makes the
release key and checks the results. apt install opensc (or dnf, pacman).
The PIN is asked once, or taken from CHERRY_HSM_PIN.
"""

import argparse
import base64
import datetime
import getpass
import hashlib
import os
import shlex
import shutil
import struct
import subprocess
import sys
import tempfile
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

# name: (token object label, certificate subject's CN, variable, its GUID, the key that signs its update)
KEYS = {
    "PK": ("cherry-pk", "Cherry Platform Key", "PK", EFI_GLOBAL_VARIABLE, "PK"),
    "KEK": ("cherry-kek", "Cherry Key Exchange Key", "KEK", EFI_GLOBAL_VARIABLE, "PK"),
    "db": ("cherry-db", "Cherry Secure Boot CA", "db", EFI_IMAGE_SECURITY_DATABASE, "KEK"),
}
ROOT_DAYS = 30 * 365 + 7  # firmware never checks validity dates
RELEASE_DAYS = 5 * 365 + 1

# DER object identifiers, whole.
OID_DATA = bytes.fromhex("06092a864886f70d010701")  # 1.2.840.113549.1.7.1
OID_SHA256 = bytes.fromhex("0609608648016503040201")  # 2.16.840.1.101.3.4.2.1
OID_RSA = bytes.fromhex("06092a864886f70d010101")  # rsaEncryption
OID_SHA256_RSA = bytes.fromhex("06092a864886f70d01010b")  # sha256WithRSAEncryption
OID_CN = bytes.fromhex("0603550403")
OID_BASIC_CONSTRAINTS = bytes.fromhex("0603551d13")
OID_KEY_USAGE = bytes.fromhex("0603551d0f")
OID_EXT_KEY_USAGE = bytes.fromhex("0603551d25")
OID_SUBJECT_KEY_ID = bytes.fromhex("0603551d0e")
OID_AUTHORITY_KEY_ID = bytes.fromhex("0603551d23")
OID_CODE_SIGNING = bytes.fromhex("06082b06010505070303")

OPENSC_MODULES = [
    "/usr/lib/x86_64-linux-gnu/opensc-pkcs11.so",
    "/usr/lib/aarch64-linux-gnu/opensc-pkcs11.so",
    "/usr/lib64/opensc-pkcs11.so",
    "/usr/lib/opensc-pkcs11.so",
    "/usr/local/lib/opensc-pkcs11.so",
    "/opt/homebrew/lib/opensc-pkcs11.so",
]


def fail(message):
    print(f"secureboot: {message}", file=sys.stderr)
    sys.exit(1)


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


def der_children(blob):
    """The DER elements, whole, inside the constructed element blob."""
    _, start, end = der_tlv(blob)
    children = []
    while start < end:
        _, _, child_end = der_tlv(blob, start)
        children.append(blob[start:child_end])
        start = child_end
    return children


def der_int(n):
    return der(0x02, n.to_bytes(n.bit_length() // 8 + 1, "big"))


def der_time(t):
    """UTCTime until 2049, GeneralizedTime from 2050, as RFC 5280 asks."""
    if t.year < 2050:
        return der(0x17, t.strftime("%y%m%d%H%M%SZ").encode())
    return der(0x18, t.strftime("%Y%m%d%H%M%SZ").encode())


def algorithm(oid):
    return der(0x30, oid + b"\x05\x00")  # with NULL parameters


def wrap_content_info(signed_data):
    return der(0x30, OID_SIGNED_DATA + der(0xA0, signed_data))


# --- X.509 and PKCS #7, around signatures the token makes -----------------------------------------------------

def name(cn):
    return der(0x30, der(0x31, der(0x30, OID_CN + der(0x0C, cn.encode()))))


def extension(oid, value, critical=False):
    return der(0x30, oid + (b"\x01\x01\xff" if critical else b"") + der(0x04, value))


def tbs_fields(cert):
    """serialNumber, signature, issuer, validity, subject, subjectPublicKeyInfo, ... of a certificate."""
    fields = der_children(der_children(cert)[0])
    return fields[1:] if fields[0][0] == 0xA0 else fields


def key_identifier(public_key):
    """The SHA-1 of a SubjectPublicKeyInfo's key, as OpenSSL's subjectKeyIdentifier = hash."""
    bits = der_children(public_key)[1]
    _, start, end = der_tlv(bits)
    return hashlib.sha1(bits[start + 1:end]).digest()


def root_extensions():
    """The token's keys: CAs, as `openssl req -x509` makes them."""
    return [extension(OID_BASIC_CONSTRAINTS, der(0x30, b"\x01\x01\xff"), critical=True)]


def release_extensions():
    return [extension(OID_BASIC_CONSTRAINTS, der(0x30, b""), critical=True),
            extension(OID_KEY_USAGE, b"\x03\x02\x07\x80", critical=True),  # digitalSignature
            extension(OID_EXT_KEY_USAGE, der(0x30, OID_CODE_SIGNING))]


def certificate(token, signer, cn, public_key, issuer, days, extensions):
    """A certificate of public_key, as DER, signed by the token's key `signer`: issued by the certificate
    issuer, or self-signed when issuer is None."""
    subject = name(cn)
    if issuer is None:
        issuer_name, issuer_key = subject, public_key
    else:
        issuer_name, issuer_key = tbs_fields(issuer)[4], tbs_fields(issuer)[5]
    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    serial = int.from_bytes(os.urandom(16), "big") >> 1 or 1
    extensions = extensions + [extension(OID_SUBJECT_KEY_ID, der(0x04, key_identifier(public_key))),
                               extension(OID_AUTHORITY_KEY_ID, der(0x30, der(0x80, key_identifier(issuer_key))))]
    tbs = der(0x30, der(0xA0, der_int(2)) + der_int(serial) + algorithm(OID_SHA256_RSA) + issuer_name
              + der(0x30, der_time(now) + der_time(now + datetime.timedelta(days=days)))
              + subject + public_key + der(0xA3, der(0x30, b"".join(extensions))))
    return der(0x30, tbs + algorithm(OID_SHA256_RSA) + der(0x03, b"\x00" + token.sign(signer, tbs)))


def signed_data(signer_cert, signature):
    """A detached PKCS #7 SignedData without signed attributes, as UEFI asks: version 1, SHA-256, RSA, and the
    signer's certificate. The signature is over the SHA-256 of the content itself."""
    serial, _, issuer = tbs_fields(signer_cert)[:3]
    signer_info = der(0x30, der_int(1) + der(0x30, issuer + serial) + algorithm(OID_SHA256) + algorithm(OID_RSA)
                      + der(0x04, signature))
    return der(0x30, der_int(1) + der(0x31, algorithm(OID_SHA256)) + der(0x30, OID_DATA)
               + der(0xA0, signer_cert) + der(0x31, signer_info))


def pem(cert):
    b64 = base64.b64encode(cert).decode()
    return "".join(["-----BEGIN CERTIFICATE-----\n"] + [b64[i:i + 64] + "\n" for i in range(0, len(b64), 64)]
                   + ["-----END CERTIFICATE-----\n"])


# --- the token -------------------------------------------------------------------------------------------------

def key_id(label):
    """The token object ID of a key and its certificate: the label, in hex."""
    return label.encode().hex()


class Token:
    """The PKCS#11 token, through pkcs11-tool."""

    def __init__(self, args, tmp):
        self.module = args.module or os.environ.get("PKCS11_MODULE") or next(
            (m for m in OPENSC_MODULES if os.path.exists(m)), None)
        if not self.module:
            fail("OpenSC's PKCS#11 module not found: install opensc, or pass --module")
        if not shutil.which("pkcs11-tool"):
            fail("pkcs11-tool not found: install opensc")
        self.label = args.token
        self.tmp = tmp
        # pkcs11-tool reads the PIN from its environment, so the PIN shows in no process list.
        pin = os.environ.get("CHERRY_HSM_PIN") or getpass.getpass("User PIN of the token: ")
        self.env = dict(os.environ, CHERRY_HSM_PIN=pin)
        # The PIN, checked before anything else: a login that fails would otherwise only show as missing keys.
        p = subprocess.run(self.command("--list-objects", "--type", "privkey"), capture_output=True, env=self.env)
        if p.returncode != 0:
            err = p.stderr.decode(errors="replace").strip()
            hint = (" A Nitrokey HSM 2 blocks its PIN after three wrong ones in a row; sc-hsm-tool shows the tries "
                    "left." if "CKR_PIN_" in err else "")
            fail(f"cannot log in to the token.{hint}\n" + "\n".join(err.splitlines()[-3:]))

    def command(self, *args):
        cmd = ["pkcs11-tool", "--module", self.module]
        if self.label:
            cmd += ["--token-label", self.label]
        return cmd + ["--login", "--pin", "env:CHERRY_HSM_PIN", *args]

    def tool(self, *args):
        return run(self.command(*args), env=self.env).decode()

    def labels(self, kind):
        """The labels of the token's objects of a kind: privkey, pubkey or cert."""
        out = self.tool("--list-objects", "--type", kind)
        return [line.split(":", 1)[1].strip() for line in out.splitlines() if line.strip().startswith("label:")]

    def delete_commands(self, labels):
        """The pkcs11-tool commands that delete the keys and certificates of these labels, for a person to run."""
        base = ["pkcs11-tool", "--module", self.module] + (["--token-label", self.label] if self.label else [])
        return "\n".join("  " + shlex.join(base + ["--login", "--delete-object", "--type", kind,
                                                   "--id", key_id(label), "--label", label])
                         for kind in ("privkey", "pubkey", "cert") for label in labels if label in self.labels(kind))

    def generate(self, label):
        self.tool("--keypairgen", "--key-type", "rsa:2048", "--usage-sign", "--label", label, "--id", key_id(label))

    def public_key(self, label):
        """The SubjectPublicKeyInfo, DER, of the token's key `label`."""
        out = os.path.join(self.tmp, "public-key.der")
        self.tool("--read-object", "--type", "pubkey", "--id", key_id(label), "--output-file", out)
        with open(out, "rb") as f:
            return f.read()

    def sign(self, label, data):
        """An RSA PKCS #1 v1.5 signature of the SHA-256 of data, by the token's key `label`. By its ID: given
        only a label, pkcs11-tool --sign takes the token's first key."""
        path = os.path.join(self.tmp, "to-sign")
        with open(path, "wb") as f:
            f.write(data)
        self.tool("--sign", "--mechanism", "SHA256-RSA-PKCS", "--id", key_id(label),
                  "--input-file", path, "--output-file", path + ".sig")
        with open(path + ".sig", "rb") as f:
            return f.read()

    def store_certificate(self, label, cert):
        path = os.path.join(self.tmp, "certificate.der")
        with open(path, "wb") as f:
            f.write(cert)
        self.tool("--write-object", path, "--type", "cert", "--label", label, "--id", key_id(label))


def signed_update(token, var, data, signer_cert, signer, when):
    """The .auth file that sets `var` to `data`, signed by the token's key `signer` of the certificate
    signer_cert (DER)."""
    _, _, name_, guid, _ = KEYS[var]
    timestamp = efi_time(when)
    signature = token.sign(signer, to_be_signed(name_, guid, AUTH_ATTRS, timestamp, data))
    return auth2(timestamp, signed_data(signer_cert, signature), data)


def pem_to_der(pem_file):
    return run(["openssl", "x509", "-in", pem_file, "-outform", "DER"])


def write(path, content, mode=0o644, new=False):
    """Write a file; with new, only one that does not exist yet."""
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if new else os.O_TRUNC)
    with open(os.open(path, flags, mode), "wb") as f:
        f.write(content.encode() if isinstance(content, str) else content)


def cmd_init(args):
    files = [f"{var}.{ext}" for var in KEYS for ext in ("crt", "auth")]
    existing = [f for f in files if os.path.exists(os.path.join(args.dir, f))]
    if existing:
        fail(f"{', '.join(existing)} exist already: init makes a new root of trust only once")
    with tempfile.TemporaryDirectory(prefix="cherry-secureboot-") as tmp:
        token = Token(args, tmp)
        present = set(token.labels("privkey"))
        taken = [label for label, *_ in KEYS.values() if label in present]
        if taken:
            fail(f"the token holds {', '.join(taken)} already: init generates them, it never reuses a key. If an "
                 "init that stopped left them, nothing uses them: delete them, then run init again:\n"
                 + token.delete_commands(taken))
        os.makedirs(args.dir, exist_ok=True)
        crt, made, written = {}, [], []
        try:
            for var, (label, cn, *_) in KEYS.items():
                note(f"generating {label} on the token")
                made.append(label)
                token.generate(label)
                crt[var] = certificate(token, label, cn, token.public_key(label), None, args.days, root_extensions())
                written.append(os.path.join(args.dir, f"{var}.crt"))
                write(written[-1], pem(crt[var]))
                token.store_certificate(label, crt[var])
            when = datetime.datetime.now(datetime.timezone.utc)
            for var, (*_, signer) in KEYS.items():
                data = esl(EFI_CERT_X509, [crt[var]])
                update = signed_update(token, var, data, crt[signer], KEYS[signer][0], when)
                written.append(os.path.join(args.dir, f"{var}.auth"))
                write(written[-1], update)
            check(args.dir, require_release=False)
        except SystemExit:
            # Nothing of a stopped init stays in use: its files go, and the person deletes what it generated on
            # the token, which init never reuses.
            for path in written:
                if os.path.exists(path):
                    os.remove(path)
            note("init stopped, and removed the files it wrote. Delete what it generated on the token, then run "
                 "init again:\n" + token.delete_commands(made))
            raise
    note(f"wrote {', '.join(files)} to {args.dir}.\n"
         "Commit them, then make the release signing key: scripts/secureboot.py issue")


def cmd_pcr(args):
    public = os.path.join(args.dir, "pcr.pem")
    if os.path.exists(public):
        fail(f"{public} exists: a new PCR key leaves every disk sealed under the one there locked. To replace it "
             "all the same, remove the file first")
    if args.key_out != "-" and os.path.exists(args.key_out):
        fail(f"{args.key_out} exists")
    os.makedirs(args.dir, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cherry-secureboot-") as tmp:
        key = os.path.join(tmp, "pcr.key")
        run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", key])
        pem = run(["openssl", "pkey", "-in", key, "-pubout"])
        with open(key, "rb") as f:
            key_pem = f.read()
    write(public, pem, new=True)
    if args.key_out == "-":
        sys.stdout.buffer.write(key_pem)
    else:
        write(args.key_out, key_pem, 0o600, new=True)
    check(args.dir)
    where = "stdout" if args.key_out == "-" else args.key_out
    note(f"wrote pcr.pem to {args.dir} and its private key to {where}.\n"
         "The key is the secret PCR_KEY of the release environment (gh secret set PCR_KEY --env release); "
         "commit pcr.pem.")


def cmd_issue(args):
    ca = os.path.join(args.dir, "db.crt")
    if not os.path.exists(ca):
        fail(f"no {ca}: run scripts/secureboot.py init first")
    if args.key_out != "-" and os.path.exists(args.key_out):
        fail(f"{args.key_out} exists")
    with tempfile.TemporaryDirectory(prefix="cherry-secureboot-") as tmp:
        token = Token(args, tmp)
        label = KEYS["db"][0]
        if label not in token.labels("privkey"):
            fail(f"the token holds no {label}, the key of {ca}: is it the token init used? --token picks one, by "
                 "its label as pkcs11-tool -L shows it")
        key = os.path.join(tmp, "release.key")
        run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", key])
        public_key = run(["openssl", "pkey", "-in", key, "-pubout", "-outform", "DER"])
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
        cert = pem(certificate(token, label, f"Cherry Release Signing {stamp}", public_key, pem_to_der(ca),
                               args.days, release_extensions()))
        crt = os.path.join(tmp, "release.crt")
        write(crt, cert)
        # Before the key goes anywhere: the key of db.crt signed the certificate.
        if subprocess.run(["openssl", "verify", "-CAfile", ca, crt], capture_output=True).returncode != 0:
            fail(f"the token's {label} is not the key of {ca}: is it the token init used? --token picks one")
        # release.crt, then the key: renamed into place within its own directory, as the temporary one may be on
        # another file system (a tmpfs /tmp).
        release = os.path.join(args.dir, "release.crt")
        write(release + ".new", cert)
        os.replace(release + ".new", release)
        with open(key, "rb") as f:
            key_pem = f.read()
        if args.key_out == "-":
            sys.stdout.buffer.write(key_pem)
        else:
            write(args.key_out, key_pem, 0o600, new=True)
    check(args.dir, require_release=True)
    where = "stdout" if args.key_out == "-" else args.key_out
    note(f"wrote release.crt to {args.dir} and its private key to {where}.\n"
         "The key is the secret SECUREBOOT_KEY of the release environment (gh secret set SECUREBOOT_KEY --env "
         "release); commit release.crt.")


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


def check_pcr(keys_dir):
    """pcr.pem, if there, is an RSA-2048 public key, as pcr makes it and systemd's signatures take it."""
    public = os.path.join(keys_dir, "pcr.pem")
    if not os.path.exists(public):
        return False
    p = subprocess.run(["openssl", "pkey", "-pubin", "-in", public, "-noout", "-text"], capture_output=True, text=True)
    if p.returncode != 0 or "Public-Key: (2048 bit)" not in p.stdout or "Modulus:" not in p.stdout:
        fail("pcr.pem: not an RSA-2048 public key")
    return True


def check(keys_dir, require_release=False):
    """Every file of init (and issue, and pcr) is there and right; return the number of files, 0 when there are
    none."""
    names = [f"{var}.{ext}" for var in KEYS for ext in ("crt", "auth")] + ["release.crt"]
    present = [n for n in names if os.path.exists(os.path.join(keys_dir, n))]
    if not present:
        return int(check_pcr(keys_dir))
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
        for var in KEYS:
            crt = path[f"{var}.crt"]
            if subprocess.run(["openssl", "verify", "-check_ss_sig", "-x509_strict", "-CAfile", crt, crt],
                              capture_output=True).returncode != 0:
                fail(f"{var}.crt: not a CA certificate signed by its own key")
        if "release.crt" in path:
            if subprocess.run(["openssl", "verify", "-CAfile", path["db.crt"], "-purpose", "any", path["release.crt"]],
                              capture_output=True).returncode != 0:
                fail("release.crt: not issued by db.crt")
            if rsa_bits(path["release.crt"]) != 2048:
                fail("release.crt: not an RSA-2048 key")
    return len(present) + int(check_pcr(keys_dir))


def cmd_check(args):
    n = check(args.dir)
    pcr = "; pcr.pem is the PCR key" if os.path.exists(os.path.join(args.dir, "pcr.pem")) else ""
    if n == 0 or (n == 1 and pcr):
        print(f"secureboot: no Secure Boot keys in {args.dir} yet (scripts/secureboot.py init){pcr}")
        return
    issued = " that issued release.crt" if os.path.exists(os.path.join(args.dir, "release.crt")) else ""
    print(f"secureboot: {args.dir}: the .auth files set PK, KEK and db to their certificates, signed by the PK, "
          f"the PK and the KEK; db.crt is a CA{issued}{pcr}")


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
    p = sub.add_parser("pcr")
    p.add_argument("--key-out", required=True, metavar="FILE",
                   help="where the PCR key's private half goes, a new file or - for stdout")
    sub.add_parser("check")
    args = parser.parse_args()
    {"init": cmd_init, "issue": cmd_issue, "pcr": cmd_pcr, "check": cmd_check}[args.command](args)


if __name__ == "__main__":
    main()
