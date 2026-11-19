#!/usr/bin/env python3
"""Tooling of the program-buffer workflow (see README "Program buffer proposals").

Prepares a Solana program upgrade for an SPL Governance vote: resolves the build
settings of the commit, reads what is deployed, plans the funding, writes the IDL
buffer, hands both buffers to the governance, and renders the proposal payloads
and the verification instructions.

Standard library only, except the commands that sign (write-idl-buffer, handoff,
close-buffers, sweep), which need the `cryptography` package for ed25519.
The program buffer itself is written by `solana program write-buffer`.
"""

from __future__ import annotations

import argparse
import base64
import difflib
import hashlib
import json
import os
import shlex
import struct
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass
from pathlib import Path

SYSTEM_PROGRAM = "11111111111111111111111111111111"
COMPUTE_BUDGET = "ComputeBudget111111111111111111111111111111"
BPF_LOADER_UPGRADEABLE = "BPFLoaderUpgradeab1e11111111111111111111111"
PROGRAM_METADATA = "ProgM6JCCvbYkfKqJYHePx4xxSUSqJp7rh8Lyv7nk7S"
SPL_GOVERNANCE = "GovMaiHfpVPw8BAM1mbdzgmSZYDw2tdP32J2fapoQoYs"
SYSVAR_RENT = "SysvarRent111111111111111111111111111111111"
SYSVAR_CLOCK = "SysvarC1ock11111111111111111111111111111111"

# BPF upgradeable loader account headers
BPF_BUFFER_HEADER = 37  # u32 tag, Option<Pubkey> authority
BPF_PROGRAMDATA_HEADER = 45  # u32 tag, u64 slot, Option<Pubkey> authority
# Program Metadata: buffer and metadata accounts share a 96-byte header
PM_HEADER = 96
PM_IDL_SEED = b"idl"
# Program Metadata enums
PM_ENCODING_UTF8 = 1
PM_COMPRESSION_ZLIB = 2
PM_FORMAT_JSON = 1
PM_DATA_SOURCE_DIRECT = 0
PM_DISC_BUFFER = 1
PM_DISC_METADATA = 2
PM_COMPRESSION_NAMES = {0: "none", 1: "gzip", 2: "zlib"}

# Growing a program's data account. While this feature gate is inactive (mainnet, late 2026)
# only ExtendProgram exists: permissionless, refused in inner instructions, so a governance
# cannot run it and the proposer extends before the vote. Once active, only ExtendProgramChecked
# exists, signed by the upgrade authority, so the proposal extends.
FEATURE_EXTEND_PROGRAM_CHECKED = "2oMRZEDWT2tqtYMofhmmfQ8SsjqUFzT6sYXppQDavxwz"
FEATURE_PROGRAM = "Feature111111111111111111111111111111111111"
# ExtendProgram grows by at least this much, unless it grows the account to the maximum size.
LOADER_MIN_EXTEND = 10_240
# An inner instruction grows an account by at most this much per top-level instruction, so it
# bounds what one governance-executed proposal transaction may add to any account.
CPI_GROWTH_LIMIT = 10_240
MAX_ACCOUNT_SIZE = 10 * 1024 * 1024

# A Program Metadata write in a legacy transaction carries ~1000 bytes; keep a margin.
PM_WRITE_CHUNK = 900
# `solana program write-buffer` sends ~1 KiB of the program per transaction.
BPF_WRITE_CHUNK = 1000
LAMPORTS_PER_SIGNATURE = 5000
COMPUTE_UNIT_LIMIT = 200_000
# Covers the cleanup/sweep transfers and a retried write or two.
FEE_MARGIN_LAMPORTS = 10_000_000
LAMPORTS_PER_SOL = 1_000_000_000


class Failure(Exception):
    """An error the workflow reports as an annotation and fails on."""


# ---------------------------------------------------------------- base58, PDAs

_B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"


def b58encode(data: bytes) -> str:
    n = int.from_bytes(data, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58[r] + out
    return "1" * (len(data) - len(data.lstrip(b"\0"))) + out


def b58decode(text: str) -> bytes:
    n = 0
    for c in text:
        idx = _B58.find(c)
        if idx < 0:
            raise ValueError(f"invalid base58 character {c!r} in {text!r}")
        n = n * 58 + idx
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    return b"\0" * (len(text) - len(text.lstrip("1"))) + raw


def pubkey_bytes(key: str) -> bytes:
    raw = b58decode(key)
    if len(raw) != 32:
        raise Failure(f"{key!r} is not a 32-byte public key")
    return raw


def is_pubkey(key: str) -> bool:
    try:
        return len(b58decode(key)) == 32
    except ValueError:
        return False


_P = 2**255 - 19
_D = -121665 * pow(121666, _P - 2, _P) % _P


def _on_curve(point: bytes) -> bool:
    """Whether 32 bytes decompress to an ed25519 point (PDAs must not)."""
    y = int.from_bytes(point, "little") & ((1 << 255) - 1)
    if y >= _P:
        return False
    u = (y * y - 1) % _P
    v = (_D * y * y + 1) % _P
    x2 = u * pow(v, _P - 2, _P) % _P
    if x2 == 0:
        return True
    return pow(x2, (_P - 1) // 2, _P) == 1


def find_pda(seeds: list[bytes], program: str) -> str:
    prog = pubkey_bytes(program)
    for bump in range(255, -1, -1):
        h = hashlib.sha256(b"".join(seeds) + bytes([bump]) + prog + b"ProgramDerivedAddress").digest()
        if not _on_curve(h):
            return b58encode(h)
    raise Failure(f"no PDA for seeds {seeds!r}")


def programdata_address(program: str) -> str:
    return find_pda([pubkey_bytes(program)], BPF_LOADER_UPGRADEABLE)


def native_treasury_address(governance: str, governance_program: str = SPL_GOVERNANCE) -> str:
    return find_pda([b"native-treasury", pubkey_bytes(governance)], governance_program)


def pm_seed(seed: bytes) -> bytes:
    if len(seed) > 16:
        raise Failure("a Program Metadata seed is at most 16 bytes")
    return seed.ljust(16, b"\0")


def canonical_metadata_address(program: str, seed: bytes = PM_IDL_SEED) -> str:
    return find_pda([pubkey_bytes(program), pm_seed(seed)], PROGRAM_METADATA)


def executable_hash(code: bytes) -> str:
    """The hash solana-verify prints: sha256 of the bytecode without trailing zeros."""
    return hashlib.sha256(code.rstrip(b"\0")).hexdigest()


def sol(lamports: int) -> str:
    return f"{lamports / LAMPORTS_PER_SOL:.9f}".rstrip("0").rstrip(".")


# ------------------------------------------------------------------------ RPC


class Rpc:
    def __init__(self, url: str):
        self.url = url

    def call(self, method: str, params: list | None = None, attempts: int = 6):
        body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}).encode()
        delay = 1.0
        for attempt in range(attempts):
            try:
                req = urllib.request.Request(self.url, body, {"content-type": "application/json"})
                with urllib.request.urlopen(req, timeout=60) as resp:
                    reply = json.load(resp)
            except (urllib.error.URLError, TimeoutError, ConnectionError, json.JSONDecodeError) as e:
                if attempt == attempts - 1:
                    raise Failure(f"RPC {method} failed: {e}") from e
                time.sleep(delay)
                delay *= 2
                continue
            if "error" in reply:
                err = reply["error"]
                # 429 and node-behind errors are transient
                if attempt < attempts - 1 and err.get("code") in (-32005, -32014, -32016, 429):
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise RpcError(method, err)
            return reply["result"]
        raise Failure(f"RPC {method} failed")

    def account(self, address: str, commitment: str = "confirmed") -> dict | None:
        value = self.call("getAccountInfo", [address, {"encoding": "base64", "commitment": commitment}])["value"]
        if value is None:
            return None
        return {
            "owner": value["owner"],
            "lamports": value["lamports"],
            "data": base64.b64decode(value["data"][0]),
            "executable": value["executable"],
        }

    def balance(self, address: str) -> int:
        return self.call("getBalance", [address, {"commitment": "confirmed"}])["value"]

    def rent(self, size: int) -> int:
        return self.call("getMinimumBalanceForRentExemption", [size])

    def program_accounts(self, program: str, filters: list) -> list[dict]:
        result = self.call(
            "getProgramAccounts",
            [program, {"encoding": "base64", "commitment": "confirmed", "filters": filters,
                       "dataSlice": {"offset": 0, "length": 0}}],
        )
        return [{"pubkey": a["pubkey"], "lamports": a["account"]["lamports"]} for a in result]


class RpcError(Failure):
    def __init__(self, method: str, error: dict):
        self.error = error
        logs = (error.get("data") or {}).get("logs") if isinstance(error.get("data"), dict) else None
        detail = f"RPC {method}: {error.get('message')}"
        if logs:
            detail += "\n  " + "\n  ".join(logs)
        super().__init__(detail)


# --------------------------------------------------------------- instructions


@dataclass(frozen=True)
class Meta:
    pubkey: str
    signer: bool = False
    writable: bool = False


@dataclass(frozen=True)
class Ix:
    program: str
    accounts: tuple[Meta, ...]
    data: bytes
    label: str = ""


def ro(key: str) -> Meta:
    return Meta(key)


def rw(key: str) -> Meta:
    return Meta(key, writable=True)


def signer(key: str, writable: bool = False) -> Meta:
    return Meta(key, signer=True, writable=writable)


def ix_transfer(source: str, destination: str, lamports: int) -> Ix:
    return Ix(SYSTEM_PROGRAM, (signer(source, True), rw(destination)), struct.pack("<IQ", 2, lamports),
              f"System transfer {sol(lamports)} SOL to {destination}")


def ix_create_account(payer: str, new: str, lamports: int, space: int, owner: str) -> Ix:
    data = struct.pack("<IQQ", 0, lamports, space) + pubkey_bytes(owner)
    return Ix(SYSTEM_PROGRAM, (signer(payer, True), signer(new, True)), data, f"System create account {new}")


def ix_compute_unit_limit(units: int) -> Ix:
    return Ix(COMPUTE_BUDGET, (), struct.pack("<BI", 2, units))


def ix_compute_unit_price(micro_lamports: int) -> Ix:
    return Ix(COMPUTE_BUDGET, (), struct.pack("<BQ", 3, micro_lamports))


# BPF upgradeable loader (UpgradeableLoaderInstruction, bincode u32 tag)


def ix_bpf_upgrade(program: str, buffer: str, spill: str, authority: str) -> Ix:
    accounts = (rw(programdata_address(program)), rw(program), rw(buffer), rw(spill), ro(SYSVAR_RENT),
                ro(SYSVAR_CLOCK), signer(authority))
    return Ix(BPF_LOADER_UPGRADEABLE, accounts, struct.pack("<I", 3),
              f"BPF loader Upgrade {program} from buffer {buffer}, buffer lamports to {spill}")


def ix_bpf_set_buffer_authority(buffer: str, authority: str, new_authority: str) -> Ix:
    return Ix(BPF_LOADER_UPGRADEABLE, (rw(buffer), signer(authority), ro(new_authority)), struct.pack("<I", 4),
              f"BPF loader SetAuthority of buffer {buffer} to {new_authority}")


def ix_bpf_close_buffer(buffer: str, recipient: str, authority: str) -> Ix:
    return Ix(BPF_LOADER_UPGRADEABLE, (rw(buffer), rw(recipient), signer(authority)), struct.pack("<I", 5),
              f"BPF loader Close buffer {buffer}, lamports to {recipient}")


def _payer_metas(payer: str | None) -> tuple[Meta, ...]:
    """The optional accounts that pay rent the program data account does not hold yet."""
    return (ro(SYSTEM_PROGRAM), signer(payer, True)) if payer else ()


def ix_bpf_extend(program: str, additional: int, payer: str | None = None) -> Ix:
    """ExtendProgram: permissionless, top-level only."""
    accounts = (rw(programdata_address(program)), rw(program), *_payer_metas(payer))
    return Ix(BPF_LOADER_UPGRADEABLE, accounts, struct.pack("<II", 6, additional),
              f"BPF loader ExtendProgram {program} by {additional} bytes")


def ix_bpf_extend_checked(program: str, authority: str, additional: int, payer: str | None = None) -> Ix:
    """ExtendProgramChecked: signed by the upgrade authority, also as an inner instruction."""
    accounts = (rw(programdata_address(program)), rw(program), signer(authority), *_payer_metas(payer))
    return Ix(BPF_LOADER_UPGRADEABLE, accounts, struct.pack("<II", 9, additional),
              f"BPF loader ExtendProgramChecked {program} by {additional} bytes")


# Program Metadata (u8 discriminator). An absent optional account is passed as the
# program's own address, as the generated client does.


def _pm_opt(key: str | None) -> Meta:
    return ro(key or PROGRAM_METADATA)


def ix_pm_write(buffer: str, authority: str, offset: int, data: bytes | None = None,
                source_buffer: str | None = None) -> Ix:
    payload = struct.pack("<BI", 0, offset) + (data or b"")
    what = f"from buffer {source_buffer}" if source_buffer else f"{len(data or b'')} bytes at {offset}"
    return Ix(PROGRAM_METADATA, (rw(buffer), signer(authority), _pm_opt(source_buffer)), payload,
              f"Program Metadata Write {what} into {buffer}")


def ix_pm_initialize(metadata: str, authority: str, program: str, programdata: str, seed: bytes) -> Ix:
    """Initializes metadata whose data was written into the account beforehand."""
    data = bytes([1]) + pm_seed(seed) + bytes(
        [PM_ENCODING_UTF8, PM_COMPRESSION_ZLIB, PM_FORMAT_JSON, PM_DATA_SOURCE_DIRECT])
    accounts = (rw(metadata), signer(authority), ro(program), ro(programdata), _pm_opt(None))
    return Ix(PROGRAM_METADATA, accounts, data, f"Program Metadata Initialize {metadata} (seed {seed.decode()})")


def ix_pm_set_authority(account: str, authority: str, new_authority: str | None) -> Ix:
    data = bytes([2]) + (bytes([1]) + pubkey_bytes(new_authority) if new_authority else bytes([0]))
    accounts = (rw(account), signer(authority), _pm_opt(None), _pm_opt(None))
    return Ix(PROGRAM_METADATA, accounts, data, f"Program Metadata SetAuthority of {account} to {new_authority}")


def ix_pm_set_data(metadata: str, authority: str, buffer: str, program: str, programdata: str) -> Ix:
    data = bytes([3, PM_ENCODING_UTF8, PM_COMPRESSION_ZLIB, PM_FORMAT_JSON, PM_DATA_SOURCE_DIRECT])
    accounts = (rw(metadata), signer(authority), rw(buffer), ro(program), ro(programdata))
    return Ix(PROGRAM_METADATA, accounts, data, f"Program Metadata SetData of {metadata} from buffer {buffer}")


def ix_pm_trim(account: str, authority: str, program: str, programdata: str, destination: str) -> Ix:
    accounts = (rw(account), signer(authority), ro(program), ro(programdata), rw(destination), ro(SYSVAR_RENT))
    return Ix(PROGRAM_METADATA, accounts, bytes([5]), f"Program Metadata Trim {account}, lamports to {destination}")


def ix_pm_close(account: str, authority: str, destination: str, program: str | None = None,
                programdata: str | None = None) -> Ix:
    accounts = (rw(account), signer(authority), _pm_opt(program), _pm_opt(programdata), rw(destination))
    return Ix(PROGRAM_METADATA, accounts, bytes([6]), f"Program Metadata Close {account}, lamports to {destination}")


def ix_pm_allocate(account: str, authority: str, program: str | None = None, programdata: str | None = None,
                   seed: bytes | None = None) -> Ix:
    data = bytes([7]) + (pm_seed(seed) if seed is not None else b"")
    accounts = (rw(account), signer(authority), _pm_opt(program), _pm_opt(programdata), ro(SYSTEM_PROGRAM))
    return Ix(PROGRAM_METADATA, accounts, data, f"Program Metadata Allocate {account}")


def ix_pm_extend(account: str, authority: str, length: int, program: str | None = None,
                 programdata: str | None = None) -> Ix:
    accounts = (rw(account), signer(authority), _pm_opt(program), _pm_opt(programdata))
    return Ix(PROGRAM_METADATA, accounts, struct.pack("<BH", 8, length),
              f"Program Metadata Extend {account} by {length} bytes")


def pm_extend_chunks(length: int) -> list[int]:
    return [min(CPI_GROWTH_LIMIT, length - off) for off in range(0, length, CPI_GROWTH_LIMIT)]


# ------------------------------------------------- SPL Governance InstructionData


def encode_instruction_data(ix: Ix) -> str:
    """Borsh InstructionData, the base64 a Realms custom instruction and spl-governance-cli take."""
    out = bytearray(pubkey_bytes(ix.program))
    out += struct.pack("<I", len(ix.accounts))
    for m in ix.accounts:
        out += pubkey_bytes(m.pubkey) + bytes([int(m.signer), int(m.writable)])
    out += struct.pack("<I", len(ix.data)) + ix.data
    return base64.b64encode(bytes(out)).decode()


def decode_instruction_data(raw: bytes, offset: int = 0) -> tuple[Ix, int]:
    program = b58encode(raw[offset:offset + 32])
    offset += 32
    (count,) = struct.unpack_from("<I", raw, offset)
    offset += 4
    metas = []
    for _ in range(count):
        metas.append(Meta(b58encode(raw[offset:offset + 32]), bool(raw[offset + 32]), bool(raw[offset + 33])))
        offset += 34
    (size,) = struct.unpack_from("<I", raw, offset)
    offset += 4
    data = raw[offset:offset + size]
    return Ix(program, tuple(metas), data), offset + size


def decode_proposal_transaction(raw: bytes) -> list[Ix]:
    """ProposalTransactionV2: type, proposal, option u8, index u16, hold-up u32, Vec<InstructionData>."""
    offset = 1 + 32 + 1 + 2 + 4
    (count,) = struct.unpack_from("<I", raw, offset)
    offset += 4
    ixs = []
    for _ in range(count):
        ix, offset = decode_instruction_data(raw, offset)
        ixs.append(ix)
    return ixs


# ---------------------------------------------------------------- transactions


def _compact_u16(n: int) -> bytes:
    out = bytearray()
    while True:
        byte = n & 0x7F
        n >>= 7
        if n:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def compile_message(payer: str, ixs: list[Ix], blockhash: str) -> tuple[bytes, list[str]]:
    """Legacy message; returns its bytes and the required signer order."""
    flags: dict[str, list[bool]] = {payer: [True, True]}
    order = [payer]

    def add(key: str, is_signer: bool, is_writable: bool):
        if key not in flags:
            flags[key] = [is_signer, is_writable]
            order.append(key)
        else:
            flags[key][0] |= is_signer
            flags[key][1] |= is_writable

    for ix in ixs:
        for m in ix.accounts:
            add(m.pubkey, m.signer, m.writable)
        add(ix.program, False, False)

    def category(key: str) -> int:
        s, w = flags[key]
        return 0 if s and w else 1 if s else 2 if w else 3

    keys = [payer] + sorted(order[1:], key=category)  # stable: keeps first-seen order within a category
    index = {k: i for i, k in enumerate(keys)}
    signers = [k for k in keys if flags[k][0]]
    readonly_signed = sum(1 for k in signers if not flags[k][1])
    readonly_unsigned = sum(1 for k in keys if not flags[k][0] and not flags[k][1])

    msg = bytearray([len(signers), readonly_signed, readonly_unsigned])
    msg += _compact_u16(len(keys)) + b"".join(pubkey_bytes(k) for k in keys)
    msg += pubkey_bytes(blockhash)
    msg += _compact_u16(len(ixs))
    for ix in ixs:
        msg.append(index[ix.program])
        msg += _compact_u16(len(ix.accounts)) + bytes(index[m.pubkey] for m in ix.accounts)
        msg += _compact_u16(len(ix.data)) + ix.data
    return bytes(msg), signers


class Keypair:
    def __init__(self, secret64: bytes):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

        if len(secret64) != 64:
            raise Failure("a keypair file holds 64 bytes")
        self._key = Ed25519PrivateKey.from_private_bytes(secret64[:32])
        public = self._key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        if public != secret64[32:]:
            raise Failure("keypair file: the public half does not match the secret")
        self.pubkey = b58encode(public)
        self.secret64 = secret64

    @classmethod
    def load(cls, path: str) -> Keypair:
        try:
            values = json.loads(Path(path).read_text())
        except (OSError, json.JSONDecodeError) as e:
            raise Failure(f"cannot read keypair {path}: {e}") from e
        return cls(bytes(values))

    @classmethod
    def generate(cls) -> Keypair:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

        key = Ed25519PrivateKey.generate()
        seed = key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
        return cls(seed + key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))

    def save(self, path: str):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(list(self.secret64), f)

    def sign(self, message: bytes) -> bytes:
        return self._key.sign(message)


def send_and_confirm(rpc: Rpc, payer: Keypair, ixs: list[Ix], extra_signers: tuple[Keypair, ...] = (),
                     cu_price: int = 0, label: str = "", attempts: int = 5, compute_budget: bool = True) -> str:
    """Sends a transaction until it is confirmed; safe to retry because every caller's
    instructions either are idempotent or fail harmlessly when already applied.
    Without `compute_budget` the fee is exactly one signature per signer."""
    budget = [ix_compute_unit_limit(COMPUTE_UNIT_LIMIT)] if compute_budget else []
    if cu_price and compute_budget:
        budget.append(ix_compute_unit_price(cu_price))
    all_ixs = budget + ixs
    keypairs = {kp.pubkey: kp for kp in (payer, *extra_signers)}
    sent: list[str] = []
    for attempt in range(attempts):
        latest = rpc.call("getLatestBlockhash", [{"commitment": "confirmed"}])["value"]
        message, signer_keys = compile_message(payer.pubkey, all_ixs, latest["blockhash"])
        missing = [k for k in signer_keys if k not in keypairs]
        if missing:
            raise Failure(f"{label}: no keypair for signer(s) {missing}")
        signatures = [keypairs[k].sign(message) for k in signer_keys]
        tx = _compact_u16(len(signatures)) + b"".join(signatures) + message
        if len(tx) > 1232:
            raise Failure(f"{label}: transaction of {len(tx)} bytes exceeds 1232")
        signature = b58encode(signatures[0])
        sent.append(signature)
        encoded = base64.b64encode(tx).decode()
        try:
            rpc.call("sendTransaction", [encoded, {"encoding": "base64", "preflightCommitment": "confirmed",
                                                  "maxRetries": 0}])
        except RpcError as e:
            # an RPC node behind the one that served the blockhash
            if "Blockhash not found" in str(e) and attempt < attempts - 1:
                log(f"{label}: blockhash not found yet, retrying")
                sent.pop()
                time.sleep(2)
                continue
            raise
        deadline_height = latest["lastValidBlockHeight"]
        while True:
            time.sleep(1.5)
            statuses = rpc.call("getSignatureStatuses", [sent, {"searchTransactionHistory": False}])["value"]
            for sig, status in zip(sent, statuses, strict=True):
                if status is None:
                    continue
                if status.get("err"):
                    raise Failure(f"{label}: transaction {sig} failed: {status['err']}")
                if status.get("confirmationStatus") in ("confirmed", "finalized"):
                    log(f"{label}: confirmed {sig}")
                    return sig
            height = rpc.call("getBlockHeight", [{"commitment": "confirmed"}])
            if height > deadline_height:
                log(f"{label}: blockhash expired (attempt {attempt + 1}/{attempts}), resending")
                break
            try:
                rpc.call("sendTransaction", [encoded, {"encoding": "base64", "skipPreflight": True,
                                                      "maxRetries": 0}], attempts=2)
            except Failure:
                pass
    raise Failure(f"{label}: not confirmed after {attempts} attempts (signatures {sent})")


# ------------------------------------------------------------- on-chain state


def log(message: str):
    print(message, file=sys.stderr, flush=True)


def read_program(rpc: Rpc, program: str) -> dict:
    account = rpc.account(program)
    if account is None:
        raise Failure(f"program {program} does not exist")
    if account["owner"] != BPF_LOADER_UPGRADEABLE or struct.unpack_from("<I", account["data"])[0] != 2:
        raise Failure(f"{program} is not a program of the BPF upgradeable loader")
    programdata = b58encode(account["data"][4:36])
    pd = rpc.account(programdata)
    if pd is None or struct.unpack_from("<I", pd["data"])[0] != 3:
        raise Failure(f"programdata {programdata} of {program} is missing or not ProgramData")
    data = pd["data"]
    (slot,) = struct.unpack_from("<Q", data, 4)
    authority = b58encode(data[13:45]) if data[12] == 1 else None
    code = data[BPF_PROGRAMDATA_HEADER:]
    return {
        "program": program,
        "programdata": programdata,
        "authority": authority,
        "last_deploy_slot": slot,
        "capacity": len(code),
        "programdata_size": len(data),
        "programdata_lamports": pd["lamports"],
        "hash": executable_hash(code),
    }


def feature_active(rpc: Rpc, feature: str) -> bool:
    account = rpc.account(feature)
    return bool(account and account["owner"] == FEATURE_PROGRAM and account["data"][:1] == b"\x01")


def extension_plan(rpc: Rpc, program: dict, so_len: int, checked: bool) -> dict | None:
    """How the program data grows to hold `so_len` bytes of code, or None when it already does."""
    growth = so_len - program["capacity"]
    if growth <= 0:
        return None
    if checked:
        # one ExtendProgramChecked per proposal transaction: a CPI grows an account by at most
        # CPI_GROWTH_LIMIT, the minimum is LOADER_MIN_EXTEND, and a program extends once per block
        mode, step = "proposal", CPI_GROWTH_LIMIT
        additional = -(-growth // step) * step
    else:
        mode, step = "proposer", None
        additional = max(growth, LOADER_MIN_EXTEND)
    new_size = program["programdata_size"] + additional
    if new_size > MAX_ACCOUNT_SIZE:
        if program["programdata_size"] + growth > MAX_ACCOUNT_SIZE:
            raise Failure(f"{so_len} bytes of code exceed the {MAX_ACCOUNT_SIZE}-byte account limit")
        # growing to the maximum size is exempt from the minimum
        additional = MAX_ACCOUNT_SIZE - program["programdata_size"]
        new_size = MAX_ACCOUNT_SIZE
    steps = [additional] if step is None else [min(step, additional - off) for off in range(0, additional, step)]
    return {
        "mode": mode,
        "growth": growth,
        "bytes": additional,
        "steps": steps,
        "new_capacity": new_size - BPF_PROGRAMDATA_HEADER,
        # what the account does not hold yet; old programs often hold more than today's rent
        "rent": max(0, rpc.rent(new_size) - program["programdata_lamports"]),
    }


def read_bpf_buffer(rpc: Rpc, buffer: str) -> dict | None:
    account = rpc.account(buffer)
    if account is None:
        return None
    data = account["data"]
    if account["owner"] != BPF_LOADER_UPGRADEABLE or struct.unpack_from("<I", data)[0] != 1:
        raise Failure(f"{buffer} is not a BPF loader buffer")
    return {
        "authority": b58encode(data[5:37]) if data[4] == 1 else None,
        "hash": executable_hash(data[BPF_BUFFER_HEADER:]),
        "lamports": account["lamports"],
    }


def read_pm_account(rpc: Rpc, address: str) -> dict | None:
    account = rpc.account(address)
    if account is None:
        return None
    if account["owner"] != PROGRAM_METADATA:
        raise Failure(f"{address} is not owned by the Program Metadata program")
    data = account["data"]
    out = {"lamports": account["lamports"], "size": len(data), "discriminator": data[0]}
    if data[0] == PM_DISC_BUFFER:
        out["authority"] = None if data[33:65] == bytes(32) else b58encode(data[33:65])
        out["data"] = data[PM_HEADER:]
    elif data[0] == PM_DISC_METADATA:
        (length,) = struct.unpack_from("<I", data, 87)
        out.update({
            "authority": None if data[33:65] == bytes(32) else b58encode(data[33:65]),
            "mutable": bool(data[65]),
            "canonical": bool(data[66]),
            "seed": data[67:83].rstrip(b"\0").decode(errors="replace"),
            "encoding": data[83], "compression": data[84], "format": data[85], "data_source": data[86],
            "data": data[PM_HEADER:PM_HEADER + length],
        })
    return out


def unpack_pm_content(entry: dict) -> bytes:
    if entry["compression"] == PM_COMPRESSION_ZLIB:
        return zlib.decompress(entry["data"])
    if entry["compression"] == 1:
        return zlib.decompress(entry["data"], 16 + zlib.MAX_WBITS)
    return entry["data"]


def read_governance(rpc: Rpc, governance: str) -> dict:
    account = rpc.account(governance)
    if account is None:
        raise Failure(f"governance {governance} does not exist")
    data = account["data"]
    realm = b58encode(data[1:33])
    info = {"governance": governance, "governance_program": account["owner"], "realm": realm,
            "native_treasury": native_treasury_address(governance, account["owner"])}
    realm_account = rpc.account(realm)
    if realm_account is not None:
        rd = realm_account["data"]
        # RealmV2: type, community mint, config {u8, u8, [u8;6], u64, enum(u8,u64), Option<Pubkey>}
        offset = 1 + 32 + 1 + 1 + 6 + 8 + 1 + 8
        info["council_mint"] = b58encode(rd[offset + 1:offset + 33]) if rd[offset] == 1 else None
    return info


# ----------------------------------------------------------------- utilities


def write_json(path: str, value):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(value, indent=2) + "\n")


def read_json(path: str):
    return json.loads(Path(path).read_text())


def github_output(**values):
    target = os.environ.get("GITHUB_OUTPUT")
    lines = [f"{k.replace('_', '-')}={v}" for k, v in values.items()]
    if target:
        with open(target, "a") as f:
            f.write("\n".join(lines) + "\n")
    for line in lines:
        log(line)


def pack_idl(idl_bytes: bytes) -> bytes:
    return zlib.compress(idl_bytes, 9)


def canonical_json(raw: bytes) -> str:
    return hashlib.sha256(json.dumps(json.loads(raw), sort_keys=True, separators=(",", ":")).encode()).hexdigest()


# -------------------------------------------------------------------- settings

SETTING_KEYS = ("library-name", "base-image", "solana-verify-version", "cargo-args", "idl", "anchor-image",
                "idl-committed")


def cmd_settings(args):
    """Merges the settings file at the built commit over the caller's defaults."""
    workspace = Path(args.workspace)
    settings_path = workspace / args.file
    from_file = {}
    if settings_path.is_file():
        from_file = tomllib.loads(settings_path.read_text())
        unknown = sorted(set(from_file) - set(SETTING_KEYS))
        if unknown:
            raise Failure(f"{args.file}: unknown key(s) {unknown}, expected {list(SETTING_KEYS)}")
        log(f"Settings from {args.file} at the built commit: {from_file}")
    else:
        log(f"{args.file} is not in the built commit; using the caller's values")

    defaults = {k: getattr(args, k.replace("-", "_")) or "" for k in SETTING_KEYS}
    resolved = {k: str(from_file.get(k, defaults[k])) for k in SETTING_KEYS}
    source = {k: ("file" if k in from_file else "caller" if defaults[k] else "derived") for k in SETTING_KEYS}

    commit = subprocess.run(["git", "-C", str(workspace), "rev-parse", "HEAD"], check=True,
                            capture_output=True, text=True).stdout.strip()

    if not resolved["library-name"]:
        raise Failure("library-name is not set: add it to the settings file or the caller's inputs")

    if not resolved["base-image"]:
        lock = tomllib.loads((workspace / "Cargo.lock").read_text())
        versions = sorted({p["version"] for p in lock.get("package", []) if p.get("name") == "solana-program"})
        if len(versions) != 1:
            raise Failure(f"Cargo.lock pins solana-program {versions or 'not at all'}; set base-image explicitly")
        resolved["base-image"] = f"solanafoundation/solana-verifiable-build:{versions[0]}"

    if not resolved["solana-verify-version"]:
        resolved["solana-verify-version"] = "0.4.15"

    idl = resolved["idl"] or "none"
    if idl == "anchor" and not resolved["anchor-image"]:
        anchor = tomllib.loads((workspace / "Anchor.toml").read_text()).get("toolchain", {}).get("anchor_version")
        if not anchor:
            raise Failure("idl = 'anchor' needs Anchor.toml [toolchain] anchor_version or anchor-image")
        resolved["anchor-image"] = f"solanafoundation/anchor:v{anchor}"
    resolved["idl"] = idl

    cargo_args = resolved["cargo-args"].replace("{commit}", commit).replace("{commit_short}", commit[:7])
    try:
        cargo_argv = shlex.split(cargo_args)
    except ValueError as e:
        raise Failure(f"cargo-args {cargo_args!r}: {e}") from e

    build_argv = ["solana-verify", "build", "--library-name", resolved["library-name"],
                  "--base-image", resolved["base-image"]]
    if cargo_argv:
        build_argv += ["--", *cargo_argv]
    out = {**resolved, "cargo-args": cargo_args, "cargo-argv": cargo_argv, "commit": commit,
           "build-argv": build_argv, "source": source}
    write_json(args.out, out)
    github_output(commit=commit, library_name=resolved["library-name"], base_image=resolved["base-image"],
                  solana_verify_version=resolved["solana-verify-version"], idl=idl,
                  anchor_image=resolved["anchor-image"])


def cmd_build(args):
    settings = read_json(args.settings)
    log("$ " + shlex.join(settings["build-argv"]))
    subprocess.run(settings["build-argv"], cwd=args.workspace, check=True)


def cmd_idl(args):
    settings = read_json(args.settings)
    workspace = Path(args.workspace).resolve()
    mode = settings["idl"]
    out = Path(args.out)
    if mode == "none":
        log("No IDL configured (idl = 'none')")
        return
    if mode == "anchor":
        argv = ["docker", "run", "--rm", "-v", f"{workspace}:/work", "-w", "/work", "--entrypoint", "anchor",
                settings["anchor-image"], "idl", "build", "-p", settings["library-name"],
                "-o", "/work/target/program-buffer-idl.json"]
        log("$ " + shlex.join(argv))
        subprocess.run(argv, check=True)
        raw = (workspace / "target" / "program-buffer-idl.json").read_bytes()
    else:
        path = workspace / mode
        if not path.is_file():
            raise Failure(f"idl = {mode!r}: no such file in the built commit")
        raw = path.read_bytes()
    idl = json.loads(raw)
    if idl.get("address") and idl["address"] != args.program:
        raise Failure(f"the IDL names program {idl['address']}, not {args.program}")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(raw)
    log(f"IDL {out}: {len(raw)} bytes, sha256 {hashlib.sha256(raw).hexdigest()}")


# ------------------------------------------------------------- IDL comparison

IDL_SECTIONS = ("instructions", "accounts", "types", "events", "errors", "constants")
DIFF_LINES_SHOWN = 300


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def idl_changes(old: dict, new: dict) -> dict:
    """What differs, by section and entry name, plus the other top-level keys that differ."""
    changes = {}
    for section in IDL_SECTIONS:
        before = {e.get("name"): e for e in old.get(section) or [] if isinstance(e, dict)}
        after = {e.get("name"): e for e in new.get(section) or [] if isinstance(e, dict)}
        entry = {
            "added": sorted(map(str, set(after) - set(before))),
            "removed": sorted(map(str, set(before) - set(after))),
            "changed": sorted(str(k) for k in set(before) & set(after)
                              if _canonical(before[k]) != _canonical(after[k])),
        }
        if any(entry.values()):
            changes[section] = {k: v for k, v in entry.items() if v}
    other = sorted(k for k in set(old) | set(new)
                   if k not in IDL_SECTIONS and _canonical(old.get(k)) != _canonical(new.get(k)))
    if other:
        changes["other keys"] = other
    return changes


def compare_idl(new_raw: bytes, old_raw: bytes, old_label: str, diff_path: Path) -> dict:
    new, old = json.loads(new_raw), json.loads(old_raw)
    if _canonical(new) == _canonical(old):
        return {"equal": True}
    pretty = lambda d: json.dumps(d, indent=2, sort_keys=True).splitlines()  # noqa: E731
    diff = list(difflib.unified_diff(pretty(old), pretty(new), old_label, "generated", lineterm="", n=2))
    diff_path.write_text("\n".join(diff) + "\n")
    return {"equal": False, "changes": idl_changes(old, new), "diff": diff_path.name, "diff_lines": len(diff)}


def cmd_idl_compare(args):
    """The generated IDL against the IDL the repository commits and the one on chain."""
    settings = read_json(args.settings)
    generated = Path(args.idl)
    out_dir = Path(args.out_dir)
    result = {}
    if not generated.is_file():
        log("no IDL generated, nothing to compare")
        write_json(str(out_dir / "idl-compare.json"), result)
        return
    raw = generated.read_bytes()
    committed = settings.get("idl-committed")
    if committed:
        path = Path(args.workspace) / committed
        if not path.is_file():
            result["committed"] = {"path": committed, "missing": True}
            annotate("warning", f"idl-committed {committed} is not in the built commit")
        else:
            result["committed"] = {"path": committed, **compare_idl(raw, path.read_bytes(), committed,
                                                                     out_dir / "idl-committed.diff")}
            if not result["committed"]["equal"]:
                annotate("warning", f"The committed IDL {committed} differs from the one generated from this "
                                    f"commit, which is what gets published: {result['committed']['changes']}")
    entry = read_pm_account(Rpc(args.rpc), canonical_metadata_address(args.program))
    if entry is None:
        result["onchain"] = {"missing": True}
    else:
        result["onchain"] = compare_idl(raw, unpack_pm_content(entry), "on chain", out_dir / "idl-onchain.diff")
    write_json(str(out_dir / "idl-compare.json"), result)
    log(json.dumps(result, indent=2))


def annotate(level: str, message: str):
    print(f"::{level} title=IDL::{message}" if os.environ.get("GITHUB_ACTIONS") else f"{level}: {message}",
          file=sys.stderr)


def idl_comparison_section(compare: dict, out_dir: Path) -> list[str]:
    lines = []

    def describe(title: str, result: dict) -> list[str]:
        if result.get("equal"):
            return [f"- {title}: identical."]
        out = [f"- {title}: **differs**."]
        for section, entry in result.get("changes", {}).items():
            if section == "other keys":
                out.append(f"  - other keys: {', '.join(f'`{k}`' for k in entry)}")
            else:
                out.append(f"  - {section}: " + "; ".join(f"{kind} {', '.join(f'`{n}`' for n in names)}"
                                                        for kind, names in entry.items()))
        diff_file = out_dir / result.get("diff", "")
        if result.get("diff") and diff_file.is_file():
            diff = diff_file.read_text().splitlines()
            shown = diff[:DIFF_LINES_SHOWN]
            more = (f"\n... {len(diff) - DIFF_LINES_SHOWN} more lines in `{result['diff']}` of the artifact"
                    if len(diff) > DIFF_LINES_SHOWN else "")
            out += ["", "  <details><summary>diff</summary>", "", "  ```diff",
                    *("  " + line for line in shown), "  ```" + more, "", "  </details>"]
        return out

    committed = compare.get("committed")
    if committed:
        if committed.get("missing"):
            lines.append(f"- Committed `{committed['path']}`: not in the built commit.")
        else:
            lines += describe(f"Committed `{committed['path']}` against the generated IDL, which is what gets "
                              "published", committed)
    onchain = compare.get("onchain")
    if onchain:
        lines += (["- On chain: no IDL yet; the proposal creates it."] if onchain.get("missing")
                  else describe("On chain against the generated IDL, what the proposal changes", onchain))
    return ["## IDL", "", *lines, ""] if lines else []


# ---------------------------------------------------------------- read / plan


def cmd_onchain(args):
    rpc = Rpc(args.rpc)
    info = read_program(rpc, args.program)
    if args.governance:
        info["governance"] = read_governance(rpc, args.governance)
        treasury = info["governance"]["native_treasury"]
        if info["authority"] not in (treasury, args.governance):
            raise Failure(f"upgrade authority of {args.program} is {info['authority']}, neither governance "
                          f"{args.governance} nor its native treasury {treasury}")
        info["signer"] = info["authority"]
    elif args.upgrade_authority:
        if info["authority"] != args.upgrade_authority:
            raise Failure(f"upgrade authority is {info['authority']}, expected {args.upgrade_authority}")
        info["signer"] = info["authority"]
    else:
        raise Failure("--governance or --upgrade-authority is required")
    info["extend_program_checked"] = feature_active(rpc, FEATURE_EXTEND_PROGRAM_CHECKED)
    metadata = canonical_metadata_address(args.program)
    entry = read_pm_account(rpc, metadata)
    info["idl"] = {"address": metadata, "exists": entry is not None}
    if entry is not None:
        content = unpack_pm_content(entry)
        info["idl"].update({"size": entry["size"], "data_length": len(entry["data"]),
                            "lamports": entry["lamports"],
                            "authority": entry["authority"], "mutable": entry["mutable"],
                            "canonical_json_sha256": canonical_json(content),
                            "compression": PM_COMPRESSION_NAMES.get(entry["compression"], "?")})
    write_json(args.out, info)
    log(json.dumps(info, indent=2))


def cmd_plan(args):
    """Funding needed by the write job, from the built artifacts and the chain."""
    rpc = Rpc(args.rpc)
    onchain = read_json(args.onchain)
    code = Path(args.so).read_bytes()
    built_hash = executable_hash(code)
    costs = {"program_buffer_rent": rpc.rent(BPF_BUFFER_HEADER + len(code))}
    priority = args.compute_unit_price * COMPUTE_UNIT_LIMIT // 1_000_000
    per_tx = LAMPORTS_PER_SIGNATURE + priority
    txs = -(-len(code) // BPF_WRITE_CHUNK) + 2
    extend = extension_plan(rpc, onchain, len(code), onchain.get("extend_program_checked", False))
    if extend:
        # stays with the program as its rent: spent, not refundable, also when the vote fails
        costs["program_extend_rent"] = extend["rent"]
        txs += 1
    idl = None
    if args.idl and Path(args.idl).is_file():
        raw = Path(args.idl).read_bytes()
        packed = pack_idl(raw)
        idl = {"bytes": len(raw), "packed_bytes": len(packed), "sha256": hashlib.sha256(raw).hexdigest(),
               "canonical_json_sha256": canonical_json(raw), "metadata": onchain["idl"]["address"],
               "mode": "update" if onchain["idl"]["exists"] else "create"}
        if onchain["idl"]["exists"] and idl["canonical_json_sha256"] == onchain["idl"]["canonical_json_sha256"]:
            idl["mode"] = "unchanged"
        if idl["mode"] != "unchanged":
            costs["idl_buffer_rent"] = rpc.rent(PM_HEADER + len(packed))
            # rent of the metadata account the account does not hold yet; it stays there
            held = onchain["idl"].get("lamports", 0) if idl["mode"] == "update" else rpc.balance(idl["metadata"])
            rent = max(0, rpc.rent(PM_HEADER + len(packed)) - held)
            if rent:
                costs["idl_metadata_rent" if idl["mode"] == "create" else "idl_metadata_extra_rent"] = rent
            txs += 3 + -(-len(packed) // PM_WRITE_CHUNK)
    txs += 1  # handoff
    costs["fees_estimate"] = txs * per_tx + FEE_MARGIN_LAMPORTS
    required = sum(costs.values())
    refundable = costs["program_buffer_rent"] + costs.get("idl_buffer_rent", 0)
    plan = {
        "program": onchain["program"],
        "built_hash": built_hash,
        "deployed_hash": onchain["hash"],
        "so_bytes": len(code),
        "capacity": onchain["capacity"],
        "costs": costs,
        "required_lamports": required,
        "refundable_lamports": refundable,
        "spent_lamports": required - refundable,
        "compute_unit_price": args.compute_unit_price,
        "extend": extend,
        "idl": idl,
        "proposer": args.proposer,
        "refund_to": args.refund_to,
    }
    if args.proposer:
        plan["proposer_balance"] = rpc.balance(args.proposer)
    write_json(args.out, plan)
    log(json.dumps(plan, indent=2))
    github_output(required_sol=sol(required), built_hash=built_hash,
                  upgrade_needed=str(built_hash != onchain["hash"]).lower())


# ---------------------------------------------------------------- write steps


def cmd_check_funds(args):
    rpc = Rpc(args.rpc)
    plan = read_json(args.plan)
    balance = rpc.balance(args.proposer)
    need = plan["required_lamports"]
    log(f"proposer {args.proposer}: balance {sol(balance)} SOL, needs {sol(need)} SOL")
    if balance < need:
        raise Failure(f"the proposer {args.proposer} holds {sol(balance)} SOL, the run needs {sol(need)} SOL. "
                      "This run now returns what the proposer holds to refund-to; fund the full amount "
                      "and start a new run")


def cmd_verify_buffer(args):
    rpc = Rpc(args.rpc)
    expected = executable_hash(Path(args.so).read_bytes())
    buffer = read_bpf_buffer(rpc, args.buffer)
    if buffer is None:
        raise Failure(f"buffer {args.buffer} does not exist")
    if buffer["hash"] != expected:
        raise Failure(f"buffer {args.buffer} hashes to {buffer['hash']}, the build to {expected}")
    if buffer["authority"] != args.authority:
        raise Failure(f"buffer {args.buffer} authority is {buffer['authority']}, expected {args.authority}")
    log(f"buffer {args.buffer}: hash {buffer['hash']} matches the build, authority {buffer['authority']}")


def cmd_write_idl_buffer(args):
    rpc = Rpc(args.rpc)
    payer = Keypair.load(args.keypair)
    packed = pack_idl(Path(args.idl).read_bytes())
    buffer = Keypair.generate()
    buffer.save(args.buffer_keypair)
    size = PM_HEADER + len(packed)
    rent = rpc.rent(size)
    log(f"IDL buffer {buffer.pubkey}: {len(packed)} bytes packed, rent {sol(rent)} SOL")
    send_and_confirm(rpc, payer, [
        ix_create_account(payer.pubkey, buffer.pubkey, rent, size, PROGRAM_METADATA),
        ix_pm_allocate(buffer.pubkey, buffer.pubkey),
        ix_pm_set_authority(buffer.pubkey, buffer.pubkey, payer.pubkey),
    ], (buffer,), args.compute_unit_price, "create IDL buffer")
    for offset in range(0, len(packed), PM_WRITE_CHUNK):
        send_and_confirm(rpc, payer, [ix_pm_write(buffer.pubkey, payer.pubkey, offset,
                                                  packed[offset:offset + PM_WRITE_CHUNK])],
                         (), args.compute_unit_price, f"IDL write at {offset}")
    check_idl_buffer(rpc, buffer.pubkey, packed, payer.pubkey)
    Path(args.out).write_text(buffer.pubkey + "\n")
    github_output(idl_buffer=buffer.pubkey)


def cmd_extend(args):
    """Proposer mode: grows the program data with the permissionless ExtendProgram, before the vote."""
    rpc = Rpc(args.rpc)
    payer = Keypair.load(args.keypair)
    plan = read_json(args.plan)
    if not plan.get("extend") or plan["extend"]["mode"] != "proposer":
        log("the program data needs no extension by the proposer")
        return
    # from the chain again: anyone may have extended it since the build
    current = read_program(rpc, plan["program"])
    extend = extension_plan(rpc, current, plan["so_bytes"], checked=False)
    if extend is None:
        log(f"program data already holds {current['capacity']} bytes")
        return
    ix = ix_bpf_extend(plan["program"], extend["bytes"], payer.pubkey)
    signature = send_and_confirm(rpc, payer, [ix], (), args.compute_unit_price,
                                 f"extend the program data by {extend['bytes']} bytes")
    after = read_program(rpc, plan["program"])
    if after["capacity"] < plan["so_bytes"] or after["hash"] != current["hash"]:
        raise Failure(f"after the extension the program reads {after}")
    summary(f"Extended the program data of `{plan['program']}` by {extend['bytes']} bytes to "
            f"{after['capacity']} ({signature}); {sol(extend['rent'])} SOL of rent stays with the program.")


def check_idl_buffer(rpc: Rpc, address: str, packed: bytes, authority: str):
    entry = read_pm_account(rpc, address)
    if entry is None or entry["discriminator"] != PM_DISC_BUFFER:
        raise Failure(f"IDL buffer {address} is missing or not a Program Metadata buffer")
    if entry["data"] != packed:
        raise Failure(f"IDL buffer {address} content differs from the built IDL")
    if entry["authority"] != authority:
        raise Failure(f"IDL buffer {address} authority is {entry['authority']}, expected {authority}")
    log(f"IDL buffer {address}: content matches, authority {authority}")


def cmd_handoff(args):
    """One transaction: both buffers to the upgrade authority, the rent the proposal's
    instructions need (IDL account, program data extension) prefunded."""
    rpc = Rpc(args.rpc)
    payer = Keypair.load(args.keypair)
    plan = read_json(args.plan)
    onchain = read_json(args.onchain)
    authority = onchain["signer"]
    ixs = [ix_bpf_set_buffer_authority(args.program_buffer, payer.pubkey, authority)]
    if plan.get("extend") and plan["extend"]["mode"] == "proposal":
        current = read_program(rpc, plan["program"])
        extend = extension_plan(rpc, current, plan["so_bytes"], checked=True)
        if extend and extend["rent"]:
            ixs.append(ix_transfer(payer.pubkey, current["programdata"], extend["rent"]))
    idl = plan.get("idl")
    if args.idl_buffer and idl and idl["mode"] != "unchanged":
        ixs.append(ix_pm_set_authority(args.idl_buffer, payer.pubkey, authority))
        prefund = max(0, rpc.rent(PM_HEADER + idl["packed_bytes"]) - rpc.balance(idl["metadata"]))
        if prefund:
            ixs.append(ix_transfer(payer.pubkey, idl["metadata"], prefund))
    send_and_confirm(rpc, payer, ixs, (), args.compute_unit_price, "hand buffers to the upgrade authority")
    buffer = read_bpf_buffer(rpc, args.program_buffer)
    if buffer is None or buffer["authority"] != authority or buffer["hash"] != plan["built_hash"]:
        raise Failure(f"after the handoff buffer {args.program_buffer} reads {buffer}")
    if args.idl_buffer and idl and idl["mode"] != "unchanged":
        check_idl_buffer(rpc, args.idl_buffer, pack_idl(Path(args.idl).read_bytes()), authority)
    log(f"handed off to {authority}")


def find_buffers(rpc: Rpc, authority: str) -> list[dict]:
    """BPF loader and Program Metadata buffers whose authority is `authority`."""
    auth = pubkey_bytes(authority)
    bpf = rpc.program_accounts(BPF_LOADER_UPGRADEABLE, [
        {"memcmp": {"offset": 0, "bytes": b58encode(struct.pack("<I", 1))}},
        {"memcmp": {"offset": 4, "bytes": b58encode(b"\x01" + auth)}},
    ])
    pm = rpc.program_accounts(PROGRAM_METADATA, [
        {"memcmp": {"offset": 0, "bytes": b58encode(bytes([PM_DISC_BUFFER]))}},
        {"memcmp": {"offset": 33, "bytes": authority}},
    ])
    return [{**a, "kind": "bpf"} for a in bpf] + [{**a, "kind": "program-metadata"} for a in pm]


def cmd_close_buffers(args):
    rpc = Rpc(args.rpc)
    payer = Keypair.load(args.keypair)
    buffers = find_buffers(rpc, payer.pubkey)
    if not buffers:
        log(f"no buffers with authority {payer.pubkey}")
    for b in buffers:
        ix = (ix_bpf_close_buffer(b["pubkey"], args.refund_to, payer.pubkey) if b["kind"] == "bpf"
              else ix_pm_close(b["pubkey"], payer.pubkey, args.refund_to))
        send_and_confirm(rpc, payer, [ix], (), args.compute_unit_price,
                         f"close {b['kind']} buffer {b['pubkey']} ({sol(b['lamports'])} SOL)")
    summary(f"Closed {len(buffers)} buffer(s) of `{payer.pubkey}`, rent to `{args.refund_to}`: "
            + (", ".join(f"`{b['pubkey']}` ({b['kind']}, {sol(b['lamports'])} SOL)" for b in buffers) or "none"))


def cmd_sweep(args):
    """Sends the proposer's whole balance to refund-to, so the key holds nothing at rest."""
    rpc = Rpc(args.rpc)
    payer = Keypair.load(args.keypair)
    balance = rpc.balance(payer.pubkey)
    amount = balance - LAMPORTS_PER_SIGNATURE
    if amount <= 0:
        log(f"proposer {payer.pubkey} holds {sol(balance)} SOL, nothing to sweep")
        return
    # no compute budget instructions, so the fee is exactly one signature; a retry after the
    # transfer landed fails on the then-empty balance instead of paying twice
    signature = send_and_confirm(rpc, payer, [ix_transfer(payer.pubkey, args.refund_to, amount)],
                                 label="sweep", compute_budget=False)
    summary(f"Swept {sol(amount)} SOL from proposer `{payer.pubkey}` to `{args.refund_to}` ({signature})")


def summary(markdown: str):
    log(markdown)
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with open(target, "a") as f:
            f.write(markdown + "\n\n")


# ------------------------------------------------------------------- proposal


def proposal_transactions(plan: dict, onchain: dict, program_buffer: str, idl_buffer: str | None,
                          refund_to: str) -> tuple[list[list[Ix]], list[list[Ix]]]:
    """The proposal's transactions, and the abort proposal that closes the buffers instead."""
    # The governance executes a proposal transaction as one top-level instruction whose inner
    # instructions may grow any account by CPI_GROWTH_LIMIT bytes in total, so every growth is
    # split into transactions of at most that. Sizing transactions come first: one executed out of
    # order fails without effect, and nothing that needs the size runs before it.
    program = onchain["program"]
    programdata = onchain["programdata"]
    authority = onchain["signer"]
    transactions: list[list[Ix]] = []
    extend = plan.get("extend")
    if extend and extend["mode"] == "proposal":
        # its rent was prefunded at the handoff; a program extends once per block, so these
        # run one transaction at a time, which the governance does anyway
        transactions += [[ix_bpf_extend_checked(program, authority, n)] for n in extend["steps"]]
    transactions.append([ix_bpf_upgrade(program, program_buffer, refund_to, authority)])
    abort = [ix_bpf_close_buffer(program_buffer, refund_to, authority)]
    idl = plan.get("idl")
    if idl_buffer and idl and idl["mode"] != "unchanged":
        metadata = idl["metadata"]
        target = PM_HEADER + idl["packed_bytes"]
        if idl["mode"] == "create":
            # allocate creates the PDA with its header, the extends make room for the data
            first = min(target, CPI_GROWTH_LIMIT) - PM_HEADER
            sizing = [[ix_pm_allocate(metadata, authority, program, programdata, PM_IDL_SEED)]
                      + ([ix_pm_extend(metadata, authority, first, program, programdata)] if first else [])]
            sizing += [[ix_pm_extend(metadata, authority, n, program, programdata)]
                       for n in pm_extend_chunks(target - PM_HEADER - first)]
            # initialize must never run before its write: one transaction
            data = [ix_pm_write(metadata, authority, 0, source_buffer=idl_buffer),
                    ix_pm_initialize(metadata, authority, program, programdata, PM_IDL_SEED),
                    ix_pm_close(idl_buffer, authority, refund_to)]
        else:
            grow = target - onchain["idl"]["size"]
            # set-data grows the account itself when that fits one transaction
            sizing = ([[ix_pm_extend(metadata, authority, n, program, programdata)] for n in pm_extend_chunks(grow)]
                      if grow > CPI_GROWTH_LIMIT else [])
            data = [ix_pm_set_data(metadata, authority, idl_buffer, program, programdata),
                    ix_pm_close(idl_buffer, authority, refund_to)]
            if grow < 0:
                data.append(ix_pm_trim(metadata, authority, program, programdata, refund_to))
        transactions += sizing + [data]
        abort.append(ix_pm_close(idl_buffer, authority, refund_to))
    return transactions, [abort]


def yaml_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def manifest(governance: str, name: str, description: str, transactions: list[list[Ix]]) -> str:
    lines = [
        "# spl-governance-cli proposal create-batch <this file>",
        "proposals:",
        f"  - governance: {governance}",
        f"    name: {yaml_quote(name)}",
        f"    description: {yaml_quote(description)}",
        "    side: council",
        "    # check the inserted transactions on Realms, then sign off to start the vote",
        "    signOff: false",
        "    transactions:",
    ]
    for group in transactions:
        lines.append("      - instructions:")
        for ix in group:
            lines.append(f"          # {ix.label}")
            lines.append(f"          - {yaml_quote(encode_instruction_data(ix))}")
    return "\n".join(lines) + "\n"


def cmd_proposal(args):
    plan = read_json(args.plan)
    onchain = read_json(args.onchain)
    settings = read_json(args.settings)
    if not is_pubkey(args.refund_to):
        raise Failure(f"refund-to {args.refund_to!r} is not a public key")
    upgrade, abort = proposal_transactions(plan, onchain, args.program_buffer, args.idl_buffer, args.refund_to)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    governance = onchain.get("governance", {}).get("governance", args.governance or onchain["signer"])
    short = settings["commit"][:7]
    name = f"Upgrade {settings['library-name']} to {args.repository}@{short}"
    description = (f"{args.repository} commit {settings['commit']}, executable hash {plan['built_hash']}, "
                   f"buffer {args.program_buffer}. Verification: {args.run_url}")
    (out / "proposal.yaml").write_text(manifest(governance, name, description, upgrade))
    (out / "abort.yaml").write_text(manifest(governance, f"Close the {settings['library-name']} buffers of {short}",
                                             f"Returns the rent of the buffers proposed by {args.run_url}", abort))
    payloads = {
        "upgrade": [[{"label": ix.label, "base64": encode_instruction_data(ix)} for ix in g] for g in upgrade],
        "abort": [[{"label": ix.label, "base64": encode_instruction_data(ix)} for ix in g] for g in abort],
    }
    write_json(str(out / "payloads.json"), payloads)
    log(f"wrote {out}/proposal.yaml, abort.yaml, payloads.json")


# --------------------------------------------------------------------- report


def verification_section(settings: dict, plan: dict, args, program_buffer: str | None,
                         idl_buffer: str | None, authority: str) -> list[str]:
    repo_url = f"https://github.com/{args.repository}"
    name = settings["library-name"]
    build_cmd = shlex.join(settings["build-argv"])
    rpc = "https://api.mainnet-beta.solana.com"
    lines = [
        "## Verify it yourself",
        "",
        "Rebuild from a clean clone and compare the hashes. Nothing below depends on this workflow.",
        "",
        "Prerequisites, macOS (zsh):",
        "",
        "```zsh",
        "# Docker Desktop: https://docs.docker.com/desktop/setup/install/mac-install/",
        "# Rust: curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh",
        f"cargo install solana-verify --version {settings['solana-verify-version']} --locked",
        "# Apple Silicon: the build image is linux/amd64, emulated (slower, same bytes)",
        "export DOCKER_DEFAULT_PLATFORM=linux/amd64",
        "```",
        "",
        "Prerequisites, Ubuntu/Debian (bash):",
        "",
        "```bash",
        "sudo apt-get install -y docker.io git pkg-config libudev-dev build-essential",
        "sudo usermod -aG docker \"$USER\"   # then log in again",
        "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh",
        f"cargo install solana-verify --version {settings['solana-verify-version']} --locked",
        "```",
        "",
        "Build and compare (the same in zsh and bash):",
        "",
        "```bash",
        f"git clone {repo_url} && cd {args.repository.split('/')[-1]}",
        f"git checkout {settings['commit']}",
        build_cmd,
        f"solana-verify get-executable-hash target/deploy/{name}.so",
        f"# expect {plan['built_hash']}",
    ]
    if program_buffer:
        lines += [
            f"solana-verify get-buffer-hash -u {rpc} {program_buffer}",
            f"# expect {plan['built_hash']}",
        ]
    lines += [
        f"solana-verify get-program-hash -u {rpc} {plan['program']}",
        f"# currently deployed: {plan['deployed_hash']}",
        "```",
    ]
    if program_buffer:
        lines += [
            "",
            "With the Solana CLI ([install](https://docs.anza.xyz/cli/install)), check that only the governance can "
            "use the buffer:",
            "",
            "```bash",
            f"solana program show -u {rpc} {program_buffer}",
            f"# Authority: {authority}",
            "```",
        ]
    idl = plan.get("idl")
    if idl:
        if settings["idl"] == "anchor":
            regen = (f"docker run --rm -v \"$PWD\":/work -w /work --entrypoint anchor {settings['anchor-image']} "
                     f"idl build -p {name} -o /work/idl.json")
        else:
            regen = f"cp {settings['idl']} idl.json"
        lines += [
            "",
            f"The IDL (`{idl['canonical_json_sha256']}` is the sha256 of its key-sorted compact JSON):",
            "",
            "```bash",
            regen,
            "python3 -c 'import hashlib,json; print(hashlib.sha256(json.dumps(json.load(open(\"idl.json\")),"
            "sort_keys=True,separators=(\",\",\":\")).encode()).hexdigest())'",
        ]
        if idl_buffer:
            lines += [
                f"solana account -u {rpc} {idl_buffer} --output-file idl-buffer.bin",
                "python3 -c 'import hashlib,json,zlib; d=json.loads(zlib.decompress(open(\"idl-buffer.bin\",\"rb\")"
                ".read()[96:])); print(hashlib.sha256(json.dumps(d,sort_keys=True,separators=(\",\",\":\"))"
                ".encode()).hexdigest())'",
            ]
        lines += [f"# expect {idl['canonical_json_sha256']}", "```"]
    return lines


def cmd_report(args):
    settings = read_json(args.settings)
    plan = read_json(args.plan)
    onchain = read_json(args.onchain)
    program_buffer = args.program_buffer or None
    idl_buffer = args.idl_buffer or None
    authority = onchain["signer"]
    gov = onchain.get("governance", {})
    built, deployed = plan["built_hash"], plan["deployed_hash"]
    lines = [
        f"# {settings['library-name']} at `{settings['commit'][:7]}`",
        "",
        f"Program `{plan['program']}`, built from [{args.repository}@{settings['commit'][:7]}]"
        f"(https://github.com/{args.repository}/commit/{settings['commit']}).",
        "",
        "| | Executable hash |",
        "| --- | --- |",
        f"| Built here | `{built}` |",
    ]
    if program_buffer:
        lines.append(f"| Buffer `{program_buffer}` (read back) | `{built}` |")
    lines += [
        f"| Deployed (last deploy at slot {onchain['last_deploy_slot']}) | `{deployed}` |",
        "",
        ("**The built commit is what is deployed.**" if built == deployed
         else "The built commit differs from what is deployed."),
        "",
        "| Setting | Value | From |",
        "| --- | --- | --- |",
    ]
    for key in SETTING_KEYS:
        if settings.get(key):
            lines.append(f"| {key} | `{settings[key]}` | {settings.get('source', {}).get(key, '')} |")
    lines += ["", f"Upgrade authority `{authority}`"
              + (f": native treasury of governance `{gov['governance']}` (realm `{gov['realm']}`)" if gov else ""), ""]
    extend = plan.get("extend")
    if extend:
        grows = (f"The build is {plan['so_bytes']} bytes, {extend['growth']} more than the program data holds "
                 f"({plan['capacity']}), so it grows by {extend['bytes']} bytes to {extend['new_capacity']}; "
                 f"the {sol(extend['rent'])} SOL of rent this needs stays with the program.")
        if extend["mode"] == "proposer":
            lines += [grows + " On this cluster only the permissionless ExtendProgram exists and the runtime "
                      "refuses it inside a governance transaction, so the proposer extends the program data "
                      "before the vote. That is irreversible but harmless: the deployed code stays as it is.", ""]
        else:
            lines += [grows + f" The proposal does it in {len(extend['steps'])} ExtendProgramChecked transactions "
                      "before the upgrade (a program extends once per block); the proposer prefunds the rent, "
                      "which stays in the program data also when the vote fails.", ""]
    if plan.get("idl"):
        idl = plan["idl"]
        lines += [f"IDL: {idl['mode']} the canonical Program Metadata account `{idl['metadata']}` "
                  f"({idl['bytes']} bytes, {idl['packed_bytes']} zlib).", ""]
    if args.idl_compare and Path(args.idl_compare).is_file():
        lines += idl_comparison_section(read_json(args.idl_compare), Path(args.idl_compare).parent)
    if args.stage == "build":
        if args.dry_run:
            lines += ["**Dry run**: nothing was written. Run again with dry-run off to propose this build.", ""]
        elif plan.get("proposer"):
            costs = plan["costs"]
            rows = "\n".join(f"| {k.replace('_', ' ')} | {sol(v)} |" for k, v in costs.items())
            lines += [
                "## Funding",
                "",
                f"Send **{sol(plan['required_lamports'])} SOL** to the proposer `{plan['proposer']}` "
                f"(it holds {sol(plan.get('proposer_balance', 0))} SOL now), then approve the `write` job.",
                "",
                "| Cost | SOL |",
                "| --- | --- |",
                rows,
                "",
                f"{sol(plan['refundable_lamports'])} SOL returns to `{plan['refund_to']}` when the upgrade executes "
                "(or when the abort proposal closes the buffers); the IDL account keeps its rent. "
                "What the run does not spend is swept back to it at the end of the run, and a failed run "
                "first closes the buffers it still holds. To cancel instead, reject the approval.",
                "",
            ]
    if args.stage == "proposal":
        out = Path(args.out_dir)
        payloads = read_json(str(out / "payloads.json"))
        lines += [
            "## Proposal",
            "",
            f"Governance `{gov.get('governance', '')}`, council side. The manifests are in the "
            "`program-buffer-proposal` artifact of this run.",
            "",
            "With [spl-governance-cli](https://github.com/marinade-finance/spl-governance-cli) 2.x "
            "(`develop` until released; `pnpm install && pnpm build`, then `pnpm cli`), "
            "signing with a Ledger:",
            "",
            "```bash",
            "pnpm cli -u https://api.mainnet-beta.solana.com -k usb://ledger proposal create-batch proposal.yaml",
            "pnpm cli -u https://api.mainnet-beta.solana.com proposal show <PROPOSAL>",
            "pnpm cli -u https://api.mainnet-beta.solana.com -k usb://ledger proposal sign-off <PROPOSAL>",
            "```",
            "",
            "Each transaction below executes its instructions atomically; execute them in order "
            "(one run too early fails without effect). A single-instruction transaction can also be added in the "
            "[Realms UI](https://app.realms.today/dao/" + gov.get("realm", "") + ") as an "
            "*Execute Custom Instruction* with its base64; insert the multi-instruction ones with the CLI, "
            "which keeps them in one transaction (the IDL initialize must not run before its write).",
            "",
        ]
        for i, group in enumerate(payloads["upgrade"], 1):
            lines.append(f"Transaction {i}:")
            lines.append("")
            for entry in group:
                lines += [f"- {entry['label']}", "", f"  ```text\n  {entry['base64']}\n  ```", ""]
        lines += [
            "### If the vote fails",
            "",
            "The buffers now belong to the governance. `abort.yaml` (same artifact) proposes closing them, "
            "returning their rent to `" + args.refund_to + "`:",
            "",
        ]
        for group in payloads["abort"]:
            for entry in group:
                lines += [f"- {entry['label']}", "", f"  ```text\n  {entry['base64']}\n  ```", ""]
    lines += verification_section(settings, plan, args, program_buffer, idl_buffer, authority)
    text = "\n".join(lines) + "\n"
    if args.out:
        Path(args.out).write_text(text)
    summary(text)


# ----------------------------------------------------------------------- main


def cmd_decode(args):
    """Prints the instructions of InstructionData base64 values or a proposal transaction account."""
    if args.account:
        account = Rpc(args.rpc).account(args.account)
        ixs = decode_proposal_transaction(account["data"])
    else:
        ixs = [decode_instruction_data(base64.b64decode(v))[0] for v in args.base64]
    for ix in ixs:
        print(json.dumps({"program": ix.program, "data": ix.data.hex(),
                          "accounts": [[m.pubkey, "s" if m.signer else "-", "w" if m.writable else "-"]
                                       for m in ix.accounts]}))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="command", required=True)

    def command(name, fn, *, rpc=False, keypair=False):
        c = sub.add_parser(name)
        c.set_defaults(fn=fn)
        if rpc:
            c.add_argument("--rpc", required=True)
        if keypair:
            c.add_argument("--keypair", required=True)
        return c

    c = command("settings", cmd_settings)
    c.add_argument("--workspace", required=True)
    c.add_argument("--file", required=True)
    c.add_argument("--out", required=True)
    for key in SETTING_KEYS:
        c.add_argument(f"--{key}", default="")

    c = command("build", cmd_build)
    c.add_argument("--workspace", required=True)
    c.add_argument("--settings", required=True)

    c = command("idl", cmd_idl)
    c.add_argument("--workspace", required=True)
    c.add_argument("--settings", required=True)
    c.add_argument("--program", required=True)
    c.add_argument("--out", required=True)

    c = command("idl-compare", cmd_idl_compare, rpc=True)
    c.add_argument("--workspace", required=True)
    c.add_argument("--settings", required=True)
    c.add_argument("--program", required=True)
    c.add_argument("--idl", required=True)
    c.add_argument("--out-dir", required=True)

    c = command("onchain", cmd_onchain, rpc=True)
    c.add_argument("--program", required=True)
    c.add_argument("--governance", default="")
    c.add_argument("--upgrade-authority", default="", help="instead of --governance, for a local validator")
    c.add_argument("--out", required=True)

    c = command("plan", cmd_plan, rpc=True)
    c.add_argument("--onchain", required=True)
    c.add_argument("--so", required=True)
    c.add_argument("--idl", default="")
    c.add_argument("--proposer", default="")
    c.add_argument("--refund-to", default="")
    c.add_argument("--compute-unit-price", type=int, default=0)
    c.add_argument("--out", required=True)

    c = command("check-funds", cmd_check_funds, rpc=True)
    c.add_argument("--plan", required=True)
    c.add_argument("--proposer", required=True)

    c = command("verify-buffer", cmd_verify_buffer, rpc=True)
    c.add_argument("--buffer", required=True)
    c.add_argument("--so", required=True)
    c.add_argument("--authority", required=True)

    c = command("write-idl-buffer", cmd_write_idl_buffer, rpc=True, keypair=True)
    c.add_argument("--idl", required=True)
    c.add_argument("--buffer-keypair", required=True)
    c.add_argument("--compute-unit-price", type=int, default=0)
    c.add_argument("--out", required=True)

    c = command("extend", cmd_extend, rpc=True, keypair=True)
    c.add_argument("--plan", required=True)
    c.add_argument("--compute-unit-price", type=int, default=0)

    c = command("handoff", cmd_handoff, rpc=True, keypair=True)
    c.add_argument("--plan", required=True)
    c.add_argument("--onchain", required=True)
    c.add_argument("--program-buffer", required=True)
    c.add_argument("--idl-buffer", default="")
    c.add_argument("--idl", default="")
    c.add_argument("--compute-unit-price", type=int, default=0)

    c = command("close-buffers", cmd_close_buffers, rpc=True, keypair=True)
    c.add_argument("--refund-to", required=True)
    c.add_argument("--compute-unit-price", type=int, default=0)

    c = command("sweep", cmd_sweep, rpc=True, keypair=True)
    c.add_argument("--refund-to", required=True)

    c = command("proposal", cmd_proposal)
    c.add_argument("--plan", required=True)
    c.add_argument("--onchain", required=True)
    c.add_argument("--settings", required=True)
    c.add_argument("--program-buffer", required=True)
    c.add_argument("--idl-buffer", default="")
    c.add_argument("--refund-to", required=True)
    c.add_argument("--governance", default="")
    c.add_argument("--repository", required=True)
    c.add_argument("--run-url", required=True)
    c.add_argument("--out-dir", required=True)

    c = command("report", cmd_report)
    c.add_argument("--stage", choices=("build", "proposal"), required=True)
    c.add_argument("--idl-compare", default="")
    c.add_argument("--dry-run", action="store_true")
    c.add_argument("--settings", required=True)
    c.add_argument("--plan", required=True)
    c.add_argument("--onchain", required=True)
    c.add_argument("--program-buffer", default="")
    c.add_argument("--idl-buffer", default="")
    c.add_argument("--refund-to", default="")
    c.add_argument("--repository", required=True)
    c.add_argument("--out-dir", default="")
    c.add_argument("--out", default="")

    c = command("decode", cmd_decode)
    c.add_argument("--rpc", default="https://api.mainnet-beta.solana.com")
    c.add_argument("--account", default="", help="a ProposalTransaction account to decode")
    c.add_argument("base64", nargs="*")

    args = p.parse_args(argv)
    for key in ("refund_to",):
        value = getattr(args, key, "")
        if value and not is_pubkey(value):
            p.error(f"--{key.replace('_', '-')} {value!r} is not a public key")
    try:
        args.fn(args)
    except Failure as e:
        print(f"::error::{e}" if os.environ.get("GITHUB_ACTIONS") else f"error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
