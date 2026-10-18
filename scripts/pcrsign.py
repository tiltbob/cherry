#!/usr/bin/env python3
"""The signed PCR 11 policy of Cherry's UKI, which binds the disks' keys to the boot state.

systemd-stub measures the UKI's sections into PCR 11 of the TPM, and systemd extends the words sysinit and
ready into it as the boot goes on. A disk's key is sealed to the policy "PCR 11 holds a value the PCR key
signed": the key's public half is in the UKI, its .pcrpkey section, and its signatures over the values PCR 11 is
expected to hold in the .pcrsig section; systemd-cryptsetup hands one to the TPM, which unseals the key only if
PCR 11 holds that value (README: Disks). This computes those values as systemd-measure does, which Buildroot's
host systemd doesn't build (it needs the TPM library), signs them and checks them.

Cherry's phases: cherry-var.service unlocks /var before sysinit, cherry-storage.service the pools after it, and
cherry-storage@ a claimed disk at any time once the machine is ready. So the policy is signed for PCR 11 as the
stub left it, extended by sysinit, and by sysinit and ready.

Commands, on a UKI:
  measure UKI             the values PCR 11 is expected to hold, one per phase
  sign --key KEY UKI      sign the policy with the PCR key's private half, into the .pcrsig section, which must
                          be there with room: ukify --policy-digest leaves the digests there, unsigned, for this
                          (the release job of .github/workflows/build.yml). --output writes a copy instead.
  check UKI               the key in .pcrpkey signed every phase's policy in .pcrsig; with --unsigned, the
                          digests are there for that key, unsigned (make, the release job, the smoke test)

Run as systemd-measure (a link of that name, which board/x86_64/post-image.sh gives ukify with --tools=), it
takes that tool's verbs and arguments instead: calculate, policy-digest and sign, with the sections as files
(--linux=, --osrel=, --cmdline=, --initrd=, --uname=, --sbat=, --pcrpkey=, ...), --bank=sha256, --phase=
(without any: Cherry's), --private-key=, --public-key= and --json=. The PCR key is an RSA key.

Needs python3 and openssl.
"""

import argparse
import base64
import collections
import hashlib
import json
import os
import struct
import subprocess
import sys
import tempfile

PCR = 11
# The sections systemd-stub measures into PCR 11, in this order (systemd's src/fundamental/uki.h): each as the
# SHA-256 of its name with its trailing NUL, then that of its contents, and an empty one not at all. .pcrsig,
# which holds the signatures over the result, is left out.
SECTIONS = (".linux", ".osrel", ".cmdline", ".initrd", ".ucode", ".splash", ".dtb", ".uname", ".sbat", ".pcrpkey",
            ".profile", ".dtbauto", ".hwids", ".efifw")
# The phases Cherry unlocks disks in: PCR 11 as the stub left it, then extended by systemd-pcrextend with each
# colon-separated word.
PHASES = ("", "sysinit", "sysinit:ready")
TPM2_CC_POLICY_PCR = 0x17F
TPM2_ALG_SHA256 = 0x0B
# The signature and the key as systemd-stub leaves them in the initramfs, and cherry-pit in /run/systemd.
SIGNATURE_FILE = "tpm2-pcr-signature.json"
PUBLIC_KEY_FILE = "tpm2-pcr-public-key.pem"

Section = collections.namedtuple("Section", "name header offset size room")


def fail(message):
    print(f"pcrsign: {message}", file=sys.stderr)
    sys.exit(1)


def sha256(data):
    return hashlib.sha256(data).digest()


def extend(pcr, data):
    """A PCR extended with data: the hash of its old value and the hash of the data."""
    return sha256(pcr + sha256(data))


def measure(sections, phase=""):
    """PCR 11 once the stub measured these sections ({name: contents}) and systemd the phase's words."""
    pcr = bytes(32)
    for name in SECTIONS:
        data = sections.get(name)
        if data:
            pcr = extend(pcr, name.encode() + b"\0")
            pcr = extend(pcr, data)
    for word in phase.split(":"):
        if word:
            pcr = extend(pcr, word.encode())
    return pcr


def policy(values, previous=bytes(32)):
    """The TPM's PolicyPCR digest for these PCRs ({index: value}, SHA-256 bank) holding these values: the hash of
    the policy so far, the command, the marshalled PCR selection, and the hash of the values in index order."""
    select = bytearray(3)
    for index in values:
        select[index // 8] |= 1 << (index % 8)
    selection = struct.pack(">IHB", 1, TPM2_ALG_SHA256, len(select)) + bytes(select)
    digest = sha256(b"".join(values[index] for index in sorted(values)))
    return sha256(previous + struct.pack(">I", TPM2_CC_POLICY_PCR) + selection + digest)


# --- OpenSSL: keys and signatures ---------------------------------------------------------------------------------

def openssl(*args, data=None):
    p = subprocess.run(["openssl", *args], input=data, capture_output=True)
    if p.returncode != 0:
        fail(f"openssl {args[0]} failed: {p.stderr.decode(errors='replace').strip().splitlines()[-1]}")
    return p.stdout


def public_key(key_path):
    """The PEM public key of a private key file."""
    return openssl("pkey", "-in", key_path, "-pubout")


def public_der(public_pem):
    """A public key as DER (SubjectPublicKeyInfo), to compare keys by."""
    return openssl("pkey", "-pubin", "-outform", "DER", data=public_pem)


def fingerprint(public_pem):
    """The SHA-256 of the key as PKCS #1 RSAPublicKey DER (OpenSSL's i2d_PublicKey): how systemd names the key in
    a signature, and finds the one of a disk's token in the UKI's."""
    p = subprocess.run(["openssl", "rsa", "-pubin", "-RSAPublicKey_out", "-outform", "DER"], input=public_pem,
                       capture_output=True)
    if p.returncode != 0:
        fail("the PCR key is not an RSA key, which systemd's signatures take")
    return sha256(p.stdout).hex()


def sign_digest(key_path, digest):
    """An RSA PKCS #1 v1.5 signature over the SHA-256 of the digest, as systemd-measure signs a policy."""
    return openssl("dgst", "-sha256", "-sign", key_path, data=digest)


def verify_digest(public_pem, digest, signature):
    with tempfile.TemporaryDirectory(prefix="pcrsign-") as tmp:
        pub, sig = os.path.join(tmp, "public.pem"), os.path.join(tmp, "signature")
        with open(pub, "wb") as f:
            f.write(public_pem)
        with open(sig, "wb") as f:
            f.write(signature)
        p = subprocess.run(["openssl", "dgst", "-sha256", "-verify", pub, "-signature", sig], input=digest,
                           capture_output=True)
    return p.returncode == 0


def entries(sections, public_pem, key_path=None, phases=PHASES):
    """The .pcrsig entries for the SHA-256 bank, one per phase: the PCRs, the key's fingerprint, the policy
    digest, and its signature with the private key when given; as systemd-measure's JSON."""
    pkfp = fingerprint(public_pem)
    bank = []
    for phase in phases:
        pol = policy({PCR: measure(sections, phase)})
        entry = {"pcrs": [PCR], "pkfp": pkfp, "pol": pol.hex()}
        if key_path:
            entry["sig"] = base64.b64encode(sign_digest(key_path, pol)).decode()
        bank.append(entry)
    return {"sha256": bank}


def signed(pcrsig, public_pem, sections, phase):
    """Whether the .pcrsig entries hold a signature by this key over the phase's policy that verifies, or, with
    None, the unsigned digest alone: a one-word reason otherwise."""
    pkfp, pol = fingerprint(public_pem), policy({PCR: measure(sections, phase)}).hex()
    found = [e for e in pcrsig.get("sha256", []) if e.get("pkfp") == pkfp and e.get("pol") == pol]
    if not found:
        return "missing"
    if not any("sig" in e for e in found):
        return None
    if not any(verify_digest(public_pem, bytes.fromhex(pol), base64.b64decode(e["sig"])) for e in found if "sig" in e):
        return "bad"
    return True


# --- The UKI, a PE binary -----------------------------------------------------------------------------------------

def pe_sections(data):
    """The sections of a PE binary, by name: where their headers and contents are, and what the stub loads of
    them (the smaller of the virtual and raw sizes, as ukify sizes them) in the room the file gives them."""
    if data[:2] != b"MZ":
        raise ValueError("not a PE binary (no MZ header)")
    at = struct.unpack_from("<I", data, 0x3C)[0]
    if data[at:at + 4] != b"PE\0\0":
        raise ValueError("not a PE binary (no PE signature)")
    count, optional = struct.unpack_from("<H", data, at + 6)[0], struct.unpack_from("<H", data, at + 20)[0]
    sections = {}
    for i in range(count):
        header = at + 24 + optional + 40 * i
        name, virtual, _, raw, pointer = struct.unpack_from("<8sIIII", data, header)
        name = name.rstrip(b"\0").decode()
        sections[name] = Section(name, header, pointer, min(virtual, raw), raw)
    return sections


def contents(data, sections):
    return {s.name: data[s.offset:s.offset + s.size] for s in sections.values()}


def read_uki(path):
    """(the file's bytes, its sections' contents by name)."""
    with open(path, "rb") as f:
        data = bytearray(f.read())
    try:
        return data, contents(data, pe_sections(data))
    except (ValueError, struct.error) as e:
        fail(f"{path}: {e}")


def pcrsig_of(uki):
    try:
        return json.loads(uki[".pcrsig"].rstrip(b"\0").decode())
    except KeyError:
        fail("no .pcrsig section: ukify --pcr-private-key= signs one, --policy-digest leaves one to sign")
    except (UnicodeDecodeError, ValueError) as e:
        fail(f".pcrsig is not JSON: {e}")


def write_pcrsig(data, text):
    """The .pcrsig section's contents replaced, in the room the file gives them, in the UKI's bytes."""
    section = pe_sections(data).get(".pcrsig")
    if not section:
        fail("no .pcrsig section to write to: ukify --policy-digest leaves one")
    encoded = text.encode()
    if len(encoded) > section.room:
        fail(f"no room in .pcrsig for the signatures: {len(encoded)} bytes into {section.room}")
    data[section.offset:section.offset + section.room] = encoded + bytes(section.room - len(encoded))
    struct.pack_into("<I", data, section.header + 8, len(encoded))


# --- Commands on a UKI --------------------------------------------------------------------------------------------

def cmd_measure(args):
    _, uki = read_uki(args.uki)
    for phase in PHASES:
        print(f"{phase or ':'} {measure(uki, phase).hex()}")


def cmd_sign(args):
    data, uki = read_uki(args.uki)
    if ".pcrpkey" not in uki:
        fail(f"{args.uki} has no .pcrpkey section: no PCR key to sign for")
    public_pem = public_key(args.key)
    if public_der(public_pem) != public_der(uki[".pcrpkey"]):
        fail(f"{args.key} is not the private half of the key in {args.uki}'s .pcrpkey")
    # The digests there must be this file's: what ukify measured is what the stub will.
    stored = {e.get("pol") for e in pcrsig_of(uki).get("sha256", [])}
    ours = {policy({PCR: measure(uki, phase)}).hex() for phase in PHASES}
    if not ours <= stored:
        fail(f"the digests in {args.uki}'s .pcrsig are not those of its sections: build it again")
    write_pcrsig(data, json.dumps(entries(uki, public_pem, args.key), separators=(",", ":")))
    output = args.output or args.uki
    with open(output + ".new", "wb") as f:
        f.write(data)
    os.replace(output + ".new", output)
    print(f"pcrsign: {output}: PCR 11 policy signed for {len(PHASES)} phases with {args.key}")


def cmd_check(args):
    _, uki = read_uki(args.uki)
    if ".pcrpkey" not in uki:
        fail(f"{args.uki} has no .pcrpkey section: no PCR key")
    public_pem = bytes(uki[".pcrpkey"])
    pcrsig = pcrsig_of(uki)
    problems = []
    for phase in PHASES:
        state = signed(pcrsig, public_pem, uki, phase)
        name = f"the phase {phase}" if phase else "the stub's measurement alone"
        if state == "missing":
            problems.append(f"no policy digest for {name}")
        elif state == "bad":
            problems.append(f"the signature over {name}'s policy does not verify")
        elif state is None and not args.unsigned:
            problems.append(f"{name}'s policy is not signed")
        elif state is True and args.unsigned:
            problems.append(f"{name}'s policy is signed already")
    if problems:
        fail(f"{args.uki}: " + "; ".join(problems))
    what = "digests there, unsigned" if args.unsigned else "signed"
    print(f"pcrsign: {args.uki}: PCR 11 policy {what} for {len(PHASES)} phases, key {fingerprint(public_pem)[:16]}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("measure", help="the values PCR 11 is expected to hold, one per phase")
    p.add_argument("uki")
    p = sub.add_parser("sign", help="sign the policy into the .pcrsig section")
    p.add_argument("--key", required=True, metavar="FILE", help="the PCR key's private half, PEM")
    p.add_argument("--output", metavar="FILE", help="write the signed UKI here, not in place")
    p.add_argument("uki")
    p = sub.add_parser("check", help="the key in .pcrpkey signed every phase's policy")
    p.add_argument("--unsigned", action="store_true", help="the digests are there, not signed yet")
    p.add_argument("uki")
    args = parser.parse_args()
    {"measure": cmd_measure, "sign": cmd_sign, "check": cmd_check}[args.command](args)


# --- As systemd-measure, for ukify --------------------------------------------------------------------------------

def measure_main(argv):
    parser = argparse.ArgumentParser(prog="systemd-measure", description="scripts/pcrsign.py standing in for "
                                     "systemd-measure: calculate, policy-digest and sign, over the sections given")
    parser.add_argument("verb", choices=("calculate", "policy-digest", "sign"))
    for name in SECTIONS:
        parser.add_argument(f"--{name[1:]}", metavar="FILE")
    parser.add_argument("--bank", action="append", default=[])
    parser.add_argument("--phase", action="append", default=[])
    parser.add_argument("--private-key", metavar="FILE")
    parser.add_argument("--public-key", metavar="FILE")
    parser.add_argument("--json", choices=("pretty", "short", "off"), default="off")
    args = parser.parse_args(argv)
    if args.bank not in ([], ["sha256"]):
        fail(f"only the sha256 bank is supported, not {', '.join(args.bank)}")
    # Normalized as systemd-measure does: empty words go, so --phase=: is the stub's measurement alone.
    phases = [":".join(w for w in p.split(":") if w) for p in args.phase] or list(PHASES)
    sections = {}
    for name in SECTIONS:
        path = getattr(args, name[1:])
        if path:
            with open(path, "rb") as f:
                sections[name] = f.read()
    if not sections.get(".linux"):
        fail("--linux= must be specified")
    if args.verb == "calculate":
        if args.json == "off":
            for phase in phases:
                print(f"{PCR}:sha256={measure(sections, phase).hex()}")
        else:
            bank = [{**({"phase": phase} if phase else {}), "pcr": PCR, "hash": measure(sections, phase).hex()}
                    for phase in phases]
            print(json.dumps({"sha256": bank}, separators=(",", ":")))
        return
    if args.public_key:
        with open(args.public_key, "rb") as f:
            public_pem = f.read()
    elif args.verb == "sign" and args.private_key:
        public_pem = public_key(args.private_key)
    else:
        fail("--public-key= must be specified")
    if args.verb == "sign" and not args.private_key:
        fail("--private-key= must be specified")
    print(json.dumps(entries(sections, public_pem, args.private_key if args.verb == "sign" else None, phases),
                     separators=(",", ":")))


if __name__ == "__main__":
    if os.path.basename(sys.argv[0]) == "systemd-measure":
        measure_main(sys.argv[1:])
    else:
        main()
