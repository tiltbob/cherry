#!/usr/bin/env python3
"""scripts/pcrsign.py computes, signs and checks the UKI's PCR 11 policy as systemd would.

Against PE binaries made here, of a few bytes per section, and keys made with openssl: the PCR 11 values are the
ones systemd-measure 255.4 computed for the same sections and phases (the vectors below; and those of a
systemd-measure on this host, when it has one), the PolicyPCR digests are systemd's own test vectors
(src/test/test-tpm2.c), a policy signed is one check accepts, and check catches an unsigned policy, a section
changed after signing, and a signature by another key. As systemd-measure, through a link of that name, it gives
ukify the same values, digests and signatures. Needs python3 and openssl.
"""

import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import pcrsign  # noqa: E402

SCRIPT = os.path.abspath(pcrsign.__file__)
HOST_MEASURE = "/usr/lib/systemd/systemd-measure"

# The sections of the vectors, and what systemd-measure 255.4 computed PCR 11 to be for them: as the stub leaves
# it, after sysinit, and after sysinit and ready.
VECTOR_SECTIONS = {".linux": b"kernel bytes\n", ".osrel": b"ID=cherry\n", ".cmdline": b"console=ttyS0 panic=10",
                   ".initrd": b"initrd!", ".uname": b"6.12.0\n",
                   ".sbat": b"sbat,1,SBAT Version,sbat,1,https://github.com/rhboot/shim/blob/main/SBAT.md\n",
                   ".pcrpkey": b"not a key, just the bytes of .pcrpkey\n"}
VECTOR_PCR11 = {"": "999d341bdc6395ae8bd87b3688683d481336521b6d54eb93b7f0cbe982ac41fa",
                "sysinit": "f333c2b5de304959ada553dde9660b2ed33e6d8ca6a6f6a1dc1fab6a3136ae30",
                "sysinit:ready": "d2e58886001b1168013dbe4634f60df7399e0c3fc49f2defc0706613930abf51"}
# systemd's test of tpm2_calculate_policy_pcr: PCRs 4, 7 and 8 at these values, the digest from zero, and once
# more from that digest.
POLICY_VALUES = {4: "368f85b3013041dfe203faaa364f00b07c5da7b1e5f1dbf2efb06fa6b9bd92de",
                 7: "aa1154c9e0a774854ccbed4c8ce7e9b906b3d700a1a8db1772d0341a62dbe51b",
                 8: "cfde439a2c06af3479ca6bdc60429b90553d65300c5cfcc40004a08c6b5ad81a"}
POLICY_DIGESTS = ("76532a0e16f7e6bf6b02918c11f75d99d729fab0cc81d0df2c4284a2c4fe6e05",
                  "97e64bcabb64c1fa4b726528644926c8029f5b4458b0575c98c04fe225629a0b")

cases = 0


def check(condition, message):
    if not condition:
        sys.exit(f"pcrsign: {message}")


def case():
    global cases
    cases += 1


def round_up(n, to):
    return (n + to - 1) // to * to


def make_pe(sections):
    """A PE32+ binary with these sections ([(name, contents)]), as ukify lays them out: the contents' size as
    the virtual size, the raw size rounded up to the file alignment."""
    align, count = 512, len(sections)
    headers = 0x40 + 4 + 20 + 240 + 40 * count
    dos = bytearray(0x40)
    dos[:2] = b"MZ"
    struct.pack_into("<I", dos, 0x3C, 0x40)
    coff = struct.pack("<HHIIIHH", 0x8664, count, 0, 0, 0, 240, 0x22)
    optional = bytearray(240)
    struct.pack_into("<H", optional, 0, 0x20B)
    table, body, offset, address = b"", b"", round_up(headers, align), 0x1000
    for name, data in sections:
        raw = round_up(len(data), align)
        table += struct.pack("<8sIIIIIIHHI", name.encode().ljust(8, b"\0"), len(data), address, raw, offset, 0, 0, 0,
                             0, 0x40000040)
        body += data + bytes(raw - len(data))
        offset += raw
        address += round_up(max(len(data), 1), 0x1000)
    return bytes(dos) + b"PE\0\0" + coff + bytes(optional) + table + bytes(round_up(headers, align) - headers) + body


def write(path, data):
    with open(path, "wb") as f:
        f.write(data)


def run(*args, ok=True):
    p = subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, text=True)
    check((p.returncode == 0) == ok, f"{' '.join(args)}: exit status {p.returncode}:\n{p.stdout}{p.stderr}")
    return p.stdout + p.stderr


def main():
    tmp = tempfile.mkdtemp(prefix="cherry-pcrsign-")
    try:
        tests(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print(f"pcrsign: {cases} cases passed")


def tests(tmp):
    # The measurement, on the vectors: as a dictionary of sections, as a PE binary, and on the command line.
    for phase, value in VECTOR_PCR11.items():
        check(pcrsign.measure(VECTOR_SECTIONS, phase).hex() == value, f"PCR 11 for the phase {phase!r}")
    case()
    vectors = os.path.join(tmp, "vectors.efi")
    write(vectors, make_pe(list(VECTOR_SECTIONS.items()) + [(".pcrsig", b"{}")]))
    data, uki = pcrsign.read_uki(vectors)
    check({k: bytes(v) for k, v in uki.items() if k != ".pcrsig"} == VECTOR_SECTIONS, f"the PE's sections: {uki}")
    check(run("measure", vectors) == "".join(f"{p or ':'} {v}\n" for p, v in VECTOR_PCR11.items()), "measure")
    case()
    # Sections the stub skips: an empty one, and .pcrsig, whatever it holds.
    check(pcrsign.measure({**VECTOR_SECTIONS, ".ucode": b"", ".pcrsig": b"x"}).hex() == VECTOR_PCR11[""],
          "an empty section or .pcrsig changed the measurement")
    case()
    # The PolicyPCR digest, on systemd's vectors, then from the result.
    values = {k: bytes.fromhex(v) for k, v in POLICY_VALUES.items()}
    digest = pcrsign.policy(values)
    check(digest.hex() == POLICY_DIGESTS[0], f"PolicyPCR digest {digest.hex()}")
    check(pcrsign.policy(values, digest).hex() == POLICY_DIGESTS[1], "PolicyPCR digest from a previous one")
    case()

    # As systemd-measure: the vectors' files, the phases as ukify or a hand gives them, and JSON as it prints it.
    measure = os.path.join(tmp, "systemd-measure")
    os.symlink(SCRIPT, measure)
    files = []
    for name, content in VECTOR_SECTIONS.items():
        write(os.path.join(tmp, name[1:]), content)
        files.append(f"--{name[1:]}={os.path.join(tmp, name[1:])}")
    expected = {"sha256": [{**({"phase": p} if p else {}), "pcr": 11, "hash": v} for p, v in VECTOR_PCR11.items()]}

    def measure_tool(*args, ok=True):
        p = subprocess.run([measure, *args], capture_output=True, text=True)
        check((p.returncode == 0) == ok, f"systemd-measure {' '.join(args)}: exit status {p.returncode}:\n"
                                         f"{p.stdout}{p.stderr}")
        return p.stdout

    out = measure_tool("calculate", *files, "--bank=sha256", "--phase=:", "--phase=sysinit", "--phase=sysinit:ready",
                       "--json=short")
    check(json.loads(out) == expected, f"calculate's JSON: {out}")
    out = measure_tool("calculate", "--json", "off", *files)
    check(out == "".join(f"11:sha256={v}\n" for v in VECTOR_PCR11.values()), f"calculate without phases: {out}")
    measure_tool("calculate", *files, "--bank=sha1", ok=False)
    measure_tool("calculate", *files[1:], ok=False)
    case()
    if os.access(HOST_MEASURE, os.X_OK):
        p = subprocess.run([HOST_MEASURE, "calculate", *files, "--bank=sha256", "--phase=:", "--phase=sysinit",
                            "--phase=sysinit:ready", "--json=short"], capture_output=True, text=True)
        if p.returncode == 0:
            check(json.loads(p.stdout) == expected, f"this host's systemd-measure differs: {p.stdout}")
            case()

    # A key, and a UKI carrying its public half, with the policy digests ukify --policy-digest leaves to sign.
    key, pub = os.path.join(tmp, "pcr.key"), os.path.join(tmp, "pcr.pem")
    subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", key],
                   check=True, capture_output=True)
    subprocess.run(["openssl", "pkey", "-in", key, "-pubout", "-out", pub], check=True, capture_output=True)
    with open(pub, "rb") as f:
        public_pem = f.read()
    sections = {**VECTOR_SECTIONS, ".pcrpkey": public_pem}
    digests = json.loads(measure_tool("policy-digest", *files[:-1], f"--pcrpkey={pub}", f"--public-key={pub}"))
    check([set(e) for e in digests["sha256"]] == [{"pcrs", "pkfp", "pol"}] * 3, f"policy-digest's entries: {digests}")
    check(digests["sha256"][0]["pkfp"] == pcrsign.fingerprint(public_pem)
          and digests["sha256"][1]["pol"] == pcrsign.policy({11: pcrsign.measure(sections, "sysinit")}).hex(),
          f"policy-digest's fingerprint or digests: {digests}")
    measure_tool("policy-digest", *files, ok=False)
    case()
    padded = json.dumps(digests) + " " * 1024 * 3
    uki_path = os.path.join(tmp, "cherry.efi")
    write(uki_path, make_pe(list(sections.items()) + [(".pcrsig", padded.encode())]))
    out = run("check", "--unsigned", uki_path)
    check("digests there, unsigned for 3 phases" in out, out)
    out = run("check", uki_path, ok=False)
    check("the stub's measurement alone's policy is not signed" in out, out)
    case()

    # Signed, by the right key only, the digests checked against the sections first; then check passes.
    other = os.path.join(tmp, "other.key")
    subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", other],
                   check=True, capture_output=True)
    out = run("sign", "--key", other, uki_path, ok=False)
    check("is not the private half of the key in" in out, out)
    stale = os.path.join(tmp, "stale.efi")
    write(stale, make_pe(list({**sections, ".cmdline": b"other"}.items()) + [(".pcrsig", padded.encode())]))
    out = run("sign", "--key", key, stale, ok=False)
    check("are not those of its sections" in out, out)
    signed_path = os.path.join(tmp, "signed.efi")
    out = run("sign", "--key", key, "--output", signed_path, uki_path)
    check("PCR 11 policy signed for 3 phases" in out and run("check", "--unsigned", uki_path), out)
    out = run("check", signed_path)
    check("PCR 11 policy signed for 3 phases" in out, out)
    out = run("check", "--unsigned", signed_path, ok=False)
    check("is signed already" in out, out)
    _, signed_uki = pcrsign.read_uki(signed_path)
    pcrsig = json.loads(bytes(signed_uki[".pcrsig"]).decode())
    check([set(e) for e in pcrsig["sha256"]] == [{"pcrs", "pkfp", "pol", "sig"}] * 3, f".pcrsig: {pcrsig}")
    check({k: v for k, v in signed_uki.items() if k != ".pcrsig"} == sections, "sign changed another section")
    case()
    # The same signatures as systemd-measure sign gives ukify, which derives the public key when not given.
    sig = json.loads(measure_tool("sign", *files[:-1], f"--pcrpkey={pub}", f"--private-key={key}"))
    check([e["pol"] for e in sig["sha256"]] == [e["pol"] for e in pcrsig["sha256"]]
          and all(pcrsign.signed(sig, public_pem, sections, phase) is True for phase in pcrsign.PHASES),
          f"systemd-measure sign: {sig}")
    measure_tool("sign", *files, ok=False)
    case()
    # Signed in place, twice over.
    shutil.copy(signed_path, uki_path)
    run("sign", "--key", key, uki_path)
    check(run("check", uki_path), "check after signing in place")
    case()

    # What check catches: a byte of a measured section changed, and signatures by another key.
    tampered = bytearray(data := open(signed_path, "rb").read())
    at = data.index(b"panic=10")
    tampered[at + 7] = ord("1")
    write(os.path.join(tmp, "tampered.efi"), tampered)
    out = run("check", os.path.join(tmp, "tampered.efi"), ok=False)
    check(out.count("no policy digest for") == 3, out)
    resigned = bytearray(data)
    pcrsign.write_pcrsig(resigned, json.dumps(pcrsign.entries(sections, pcrsign.public_key(other), other)))
    write(os.path.join(tmp, "resigned.efi"), resigned)
    out = run("check", os.path.join(tmp, "resigned.efi"), ok=False)
    check(out.count("no policy digest for") == 3, out)
    forged = json.loads(bytes(signed_uki[".pcrsig"]).decode())
    forged["sha256"][1]["sig"] = forged["sha256"][0]["sig"]
    swapped = bytearray(data)
    pcrsign.write_pcrsig(swapped, json.dumps(forged))
    write(os.path.join(tmp, "swapped.efi"), swapped)
    out = run("check", os.path.join(tmp, "swapped.efi"), ok=False)
    check("the signature over the phase sysinit's policy does not verify" in out, out)
    case()
    # No room, no section, not a PE binary.
    small = bytearray(make_pe(list(sections.items()) + [(".pcrsig", b"{}")]))
    pcrsign.write_pcrsig(small, json.dumps(digests, separators=(",", ":")))
    write(os.path.join(tmp, "small.efi"), small)
    out = run("sign", "--key", key, os.path.join(tmp, "small.efi"), ok=False)
    check("no room in .pcrsig" in out, out)
    write(os.path.join(tmp, "none.efi"), make_pe(list(sections.items())))
    out = run("sign", "--key", key, os.path.join(tmp, "none.efi"), ok=False)
    check("no .pcrsig section" in out, out)
    write(os.path.join(tmp, "text"), b"not a UKI")
    out = run("check", os.path.join(tmp, "text"), ok=False)
    check("not a PE binary" in out, out)
    case()


if __name__ == "__main__":
    main()
