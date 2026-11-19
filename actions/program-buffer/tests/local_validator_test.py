#!/usr/bin/env python3
"""End-to-end test of program_buffer.py on a local solana-test-validator.

The proposal transactions are executed the way SPL Governance executes them: through CPI,
by tests/cpi-executor, signed by its PDA `[b"treasury"]`, which is the test programs' upgrade
authority. CPI matters: it refuses the permissionless ExtendProgram and limits how much one
transaction may grow an account.

Two runs: one with the feature set of mainnet in late 2026 (ExtendProgramChecked inactive, so
the proposer extends a growing program), one with every feature active (the proposal extends).

Needs Docker, the `cryptography` package, and:
- the Program Metadata program: `solana program dump ProgM6JCCvbYkfKqJYHePx4xxSUSqJp7rh8Lyv7nk7S pm.so`
- tests/cpi-executor built: `cargo-build-sbf --manifest-path tests/cpi-executor/Cargo.toml`
- two builds of one program (the deployed and the proposed .so) and its IDL

    python3 tests/local_validator_test.py --pm-so pm.so --executor-so cpi_executor.so \\
        --old-so old.so --new-so new.so --idl idl.json
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import struct
import subprocess
import sys
import tempfile
import time
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import program_buffer as pb  # noqa: E402

TOOL = str(HERE.parent / "program_buffer.py")
RPC = "http://127.0.0.1:8899"


def run(*argv: str, capture: bool = False) -> str:
    print("$", " ".join(argv), flush=True)
    result = subprocess.run(argv, check=True, text=True, capture_output=capture)
    return result.stdout if capture else ""


def tool(*argv: str):
    run(sys.executable, "-I", TOOL, *argv)


class Validator:
    def __init__(self, image: str, work: Path, programs: list[tuple[str, str, str]], bpf: list[tuple[str, str]],
                 deactivate: list[str]):
        self.name = f"pb-test-validator-{os.getpid()}"
        subprocess.run(["docker", "rm", "-f", self.name], capture_output=True)
        # Agave 3.x+ asserts io_uring support, which Docker's default seccomp profile blocks.
        args = ["docker", "run", "-d", "--rm", "--name", self.name, "--network", "host",
                "--security-opt", "seccomp=unconfined",
                "-v", f"{work}:/work", "--entrypoint", "solana-test-validator", image,
                "--ledger", "/tmp/ledger", "--reset", "--quiet"]
        for address, so in bpf:
            args += ["--bpf-program", address, f"/work/{so}"]
        for address, so, authority in programs:
            args += ["--upgradeable-program", address, f"/work/{so}", authority]
        for feature in deactivate:
            args += ["--deactivate-feature", feature]
        run(*args)
        self.rpc = pb.Rpc(RPC)
        for _ in range(90):
            try:
                if self.rpc.call("getHealth", attempts=1) == "ok":
                    return
            except pb.Failure:
                pass
            time.sleep(1)
        raise SystemExit("validator did not start")

    def stop(self):
        subprocess.run(["docker", "rm", "-f", self.name], capture_output=True)


def airdrop(rpc: pb.Rpc, key: str, sol: int):
    sig = rpc.call("requestAirdrop", [key, sol * pb.LAMPORTS_PER_SOL])
    for _ in range(60):
        status = rpc.call("getSignatureStatuses", [[sig]])["value"][0]
        if status and status.get("confirmationStatus") in ("confirmed", "finalized"):
            return
        time.sleep(0.5)
    raise SystemExit(f"airdrop to {key} not confirmed")


class Governance:
    """Executes proposal transactions like SPL Governance: each one a single top-level
    instruction of the executor, which invokes every instruction signed by the treasury PDA."""

    def __init__(self, rpc: pb.Rpc, executor: str, fee_payer: pb.Keypair):
        self.rpc, self.executor, self.fee_payer = rpc, executor, fee_payer
        self.treasury = pb.find_pda([b"treasury"], executor)

    def instruction(self, inner: list[pb.Ix]) -> pb.Ix:
        data = struct.pack("<I", len(inner)) + b"".join(
            base64.b64decode(pb.encode_instruction_data(ix)) for ix in inner)
        metas: dict[str, tuple[bool, bool]] = {}
        for ix in inner:
            for m in ix.accounts:
                s, w = metas.get(m.pubkey, (False, False))
                metas[m.pubkey] = (s or (m.signer and m.pubkey != self.treasury), w or m.writable)
            metas.setdefault(ix.program, (False, False))
        return pb.Ix(self.executor, tuple(pb.Meta(k, s, w) for k, (s, w) in metas.items()), data)

    def execute(self, groups: list[list[dict]]):
        for i, group in enumerate(groups):
            inner = [pb.decode_instruction_data(base64.b64decode(entry["base64"]))[0] for entry in group]
            for attempt in range(10):
                try:
                    pb.send_and_confirm(self.rpc, self.fee_payer, [self.instruction(inner)],
                                        label=f"governance executes transaction {i + 1}/{len(groups)}")
                    break
                except pb.Failure as e:
                    # a program extends once per block; whoever executes retries
                    if "extended in this block already" not in str(e) or attempt == 9:
                        raise
                    time.sleep(0.5)


class Scenario:
    def __init__(self, work: Path, image: str, rpc: pb.Rpc, program: str, keys: dict[str, pb.Keypair],
                 governance: Governance):
        self.work, self.image, self.rpc, self.program, self.keys = work, image, rpc, program, keys
        self.governance = governance

    def path(self, name: str) -> str:
        return str(self.work / name)

    def write_program_buffer(self, so: str) -> str:
        buffer = pb.Keypair.generate()
        buffer.save(self.path("buffer-keypair.json"))
        os.chmod(self.path("buffer-keypair.json"), 0o644)  # readable by the CLI container
        out = run("docker", "run", "--rm", "--network", "host", "-v", f"{self.work}:/work",
                  "--entrypoint", "solana", self.image, "program", "write-buffer", f"/work/{Path(so).name}",
                  "--buffer", "/work/buffer-keypair.json", "-u", RPC, "-k", "/work/proposer.json",
                  "--use-rpc", "--with-compute-unit-price", "1", "--max-sign-attempts", "50", capture=True)
        address = out.split("Buffer:")[1].split()[0]
        assert address == buffer.pubkey, (address, buffer.pubkey)
        return address

    def propose(self, so: str, idl: str | None, tag: str) -> dict:
        """The build's plan, then the write job step by step. Returns its artifacts."""
        p, r = self.keys["proposer"].pubkey, self.keys["refund"].pubkey
        t = self.governance.treasury
        onchain, plan = self.path(f"{tag}-onchain.json"), self.path(f"{tag}-plan.json")
        tool("onchain", "--rpc", RPC, "--program", self.program, "--upgrade-authority", t, "--out", onchain)
        tool("plan", "--rpc", RPC, "--onchain", onchain, "--so", so, "--idl", idl or "", "--proposer", p,
             "--refund-to", r, "--out", plan)
        plan_data = pb.read_json(plan)
        airdrop(self.rpc, p, plan_data["required_lamports"] // pb.LAMPORTS_PER_SOL + 1)
        tool("check-funds", "--rpc", RPC, "--plan", plan, "--proposer", p)
        program_buffer = self.write_program_buffer(so)
        tool("verify-buffer", "--rpc", RPC, "--buffer", program_buffer, "--so", so, "--authority", p)
        idl_buffer = ""
        if idl and plan_data["idl"]["mode"] != "unchanged":
            tool("write-idl-buffer", "--rpc", RPC, "--keypair", self.path("proposer.json"), "--idl", idl,
                 "--buffer-keypair", self.path("idl-buffer-keypair.json"), "--out", self.path("idl-buffer.txt"))
            idl_buffer = Path(self.path("idl-buffer.txt")).read_text().strip()
        tool("extend", "--rpc", RPC, "--keypair", self.path("proposer.json"), "--plan", plan)
        tool("handoff", "--rpc", RPC, "--keypair", self.path("proposer.json"), "--plan", plan, "--onchain", onchain,
             "--program-buffer", program_buffer, "--idl-buffer", idl_buffer, "--idl", idl or "")
        tool("verify-buffer", "--rpc", RPC, "--buffer", program_buffer, "--so", so, "--authority", t)
        settings = self.path("settings.json")
        pb.write_json(settings, {"commit": "0" * 40, "library-name": "test_program", "build-argv": ["true"],
                                 "solana-verify-version": "0.4.15", "idl": "idl.json", "source": {},
                                 "base-image": self.image})
        out_dir = self.path(f"{tag}-proposal")
        tool("proposal", "--plan", plan, "--onchain", onchain, "--settings", settings, "--program-buffer",
             program_buffer, "--idl-buffer", idl_buffer, "--refund-to", r, "--repository", "example/repo",
             "--run-url", "https://example.invalid/run", "--out-dir", out_dir)
        tool("report", "--stage", "proposal", "--settings", settings, "--plan", plan, "--onchain", onchain,
             "--program-buffer", program_buffer, "--idl-buffer", idl_buffer, "--refund-to", r,
             "--repository", "example/repo", "--out-dir", out_dir, "--out", self.path(f"{tag}-report.md"))
        tool("sweep", "--rpc", RPC, "--keypair", self.path("proposer.json"), "--refund-to", r)
        assert self.rpc.balance(p) == 0, "the sweep leaves the proposer empty"
        return {"plan": plan_data, "payloads": pb.read_json(f"{out_dir}/payloads.json"),
                "program_buffer": program_buffer, "idl_buffer": idl_buffer}

    def check_upgraded(self, so: str, result: dict, idl: str | None):
        info = pb.read_program(self.rpc, self.program)
        assert info["hash"] == pb.executable_hash(Path(so).read_bytes()), "program upgraded to the build"
        assert self.rpc.account(result["program_buffer"]) is None, "program buffer consumed"
        if idl and result["idl_buffer"]:
            assert self.rpc.account(result["idl_buffer"]) is None, "IDL buffer closed"
            entry = pb.read_pm_account(self.rpc, pb.canonical_metadata_address(self.program))
            assert entry and entry["canonical"] and entry["seed"] == "idl", entry
            assert entry["authority"] is None, "canonical metadata carries no authority"
            assert (entry["encoding"], entry["compression"], entry["format"]) == (1, 2, 1)
            assert json.loads(zlib.decompress(entry["data"])) == json.loads(Path(idl).read_bytes()), "IDL content"
            assert entry["size"] == pb.PM_HEADER + len(entry["data"]), "account sized to the data"
            assert entry["lamports"] >= self.rpc.rent(entry["size"]), "rent exempt"


def big_idl(path: Path, entries: int, seed: str):
    """An IDL whose zlib form exceeds what one proposal transaction may add to an account."""
    noise = [hashlib.sha256(f"{seed}{i}".encode()).hexdigest() for i in range(entries)]
    path.write_text(json.dumps({"address": "", "metadata": {"name": "big"}, "instructions": [], "docs": noise}))
    print(f"{path.name}: {len(pb.pack_idl(path.read_bytes()))} bytes packed")


def run_suite(args, mainnet_like: bool):
    work = Path(tempfile.mkdtemp(prefix="pb-test-"))
    os.chmod(work, 0o755)
    for name, src in (("pm.so", args.pm_so), ("old.so", args.old_so), ("new.so", args.new_so),
                      ("executor.so", args.executor_so)):
        (work / name).write_bytes(Path(src).read_bytes())
    keys = {k: pb.Keypair.generate() for k in ("proposer", "refund", "voter", "executor", "a", "b", "g")}
    keys["proposer"].save(str(work / "proposer.json"))
    os.chmod(work / "proposer.json", 0o644)  # readable by the CLI container
    executor = keys["executor"].pubkey
    treasury = pb.find_pda([b"treasury"], executor)
    program_a, program_b, program_g = keys["a"].pubkey, keys["b"].pubkey, keys["g"].pubkey
    validator = Validator(
        args.image, work,
        # g starts as the 47800-byte Program Metadata binary and is upgraded to the 351576-byte build
        [(program_a, "old.so", treasury), (program_b, "old.so", treasury), (program_g, "pm.so", treasury)],
        [(pb.PROGRAM_METADATA, "pm.so"), (executor, "executor.so")],
        [pb.FEATURE_EXTEND_PROGRAM_CHECKED] if mainnet_like else [])
    rpc = validator.rpc
    refund = keys["refund"].pubkey
    new_so, old_so = str(work / "new.so"), str(work / "old.so")
    label = "mainnet features" if mainnet_like else "all features"
    try:
        assert pb.feature_active(rpc, pb.FEATURE_EXTEND_PROGRAM_CHECKED) == (not mainnet_like)
        airdrop(rpc, keys["voter"].pubkey, 5)
        governance = Governance(rpc, executor, keys["voter"])

        # growth: the build outgrows the program data
        g = Scenario(work, args.image, rpc, program_g, keys, governance)
        before = pb.read_program(rpc, program_g)
        result = g.propose(new_so, None, "g1")
        extend = result["plan"]["extend"]
        assert extend and extend["mode"] == ("proposer" if mainnet_like else "proposal"), extend
        after_write = pb.read_program(rpc, program_g)
        if mainnet_like:
            assert after_write["capacity"] >= Path(new_so).stat().st_size, "proposer extended before the vote"
            assert after_write["hash"] == before["hash"], "the deployed code is untouched"
            assert len(result["payloads"]["upgrade"]) == 1, "the proposal is the upgrade alone"
        else:
            assert after_write["capacity"] == before["capacity"], "nothing extends before the vote"
            assert len(result["payloads"]["upgrade"]) == len(extend["steps"]) + 1
            # the upgrade executed before the extensions fails without effect
            try:
                governance.execute([result["payloads"]["upgrade"][-1]])
                raise AssertionError("upgrade ran before the extension")
            except pb.Failure as e:
                print(f"early upgrade refused as expected: {str(e).splitlines()[0][:100]}")
        governance.execute(result["payloads"]["upgrade"])
        g.check_upgraded(new_so, result, None)
        print(f"[{label}] growth: {extend['mode']} extended {extend['bytes']} bytes in "
              f"{len(extend['steps'])} step(s), rent {pb.sol(extend['rent'])} SOL")

        # program a: upgrade with a first, small IDL
        a = Scenario(work, args.image, rpc, program_a, keys, governance)
        result = a.propose(new_so, args.idl, "a1")
        assert result["plan"]["idl"]["mode"] == "create"
        governance.execute(result["payloads"]["upgrade"])
        a.check_upgraded(new_so, result, args.idl)

        # program a: back to the old build with a smaller IDL (update + trim)
        small = work / "idl-small.json"
        idl = json.loads(Path(args.idl).read_text())
        idl["instructions"] = idl["instructions"][:1]
        small.write_text(json.dumps(idl))
        result = a.propose(old_so, str(small), "a2")
        assert result["plan"]["idl"]["mode"] == "update"
        governance.execute(result["payloads"]["upgrade"])
        a.check_upgraded(old_so, result, str(small))

        # program a: same IDL again is left alone, the vote fails, the abort proposal refunds
        result = a.propose(new_so, str(small), "a3")
        assert result["plan"]["idl"]["mode"] == "unchanged" and not result["idl_buffer"]
        before = rpc.balance(refund)
        governance.execute(result["payloads"]["abort"])
        assert rpc.account(result["program_buffer"]) is None
        assert pb.read_program(rpc, program_a)["hash"] == pb.executable_hash(Path(old_so).read_bytes())
        assert rpc.balance(refund) - before == result["plan"]["costs"]["program_buffer_rent"]

        # program b: a large IDL (create with extends), then a much larger one (update with extends)
        b = Scenario(work, args.image, rpc, program_b, keys, governance)
        large, larger = work / "idl-large.json", work / "idl-larger.json"
        big_idl(large, 400, "a")
        big_idl(larger, 1300, "b")
        for idl_file, tag, mode, so in ((large, "b1", "create", new_so), (larger, "b2", "update", old_so)):
            result = b.propose(so, str(idl_file), tag)
            assert result["plan"]["idl"]["mode"] == mode
            groups = result["payloads"]["upgrade"]
            print(f"[{label}] {tag}: {[len(g) for g in groups]} instructions per proposal transaction")
            if mode == "create":
                # the data transaction executed before the sizing ones fails without effect
                try:
                    governance.execute([groups[-1]])
                    raise AssertionError("data transaction ran before allocation")
                except pb.Failure as e:
                    print(f"out-of-order data transaction refused as expected: {str(e).splitlines()[0][:100]}")
            governance.execute(groups)
            b.check_upgraded(so, result, str(idl_file))

        # a run that fails after writing: close-buffers returns everything the proposer still owns
        airdrop(rpc, keys["proposer"].pubkey, 5)
        a.write_program_buffer(new_so)
        tool("write-idl-buffer", "--rpc", RPC, "--keypair", str(work / "proposer.json"), "--idl", str(large),
             "--buffer-keypair", str(work / "idl-buffer-keypair.json"), "--out", str(work / "idl-buffer.txt"))
        assert len(pb.find_buffers(rpc, keys["proposer"].pubkey)) == 2
        tool("close-buffers", "--rpc", RPC, "--keypair", str(work / "proposer.json"), "--refund-to", refund)
        tool("sweep", "--rpc", RPC, "--keypair", str(work / "proposer.json"), "--refund-to", refund)
        assert not pb.find_buffers(rpc, keys["proposer"].pubkey)
        assert rpc.balance(keys["proposer"].pubkey) == 0

        print(f"\n[{label}] ALL SCENARIOS PASSED (work dir {work})\n")
    finally:
        validator.stop()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--pm-so", required=True)
    p.add_argument("--executor-so", required=True)
    p.add_argument("--old-so", required=True)
    p.add_argument("--new-so", required=True)
    p.add_argument("--idl", required=True)
    p.add_argument("--image", default="solanafoundation/solana-verifiable-build:4.0.3")
    p.add_argument("--only", choices=("mainnet", "all"), default=None)
    args = p.parse_args()
    if args.only != "all":
        run_suite(args, mainnet_like=True)
    if args.only != "mainnet":
        run_suite(args, mainnet_like=False)


if __name__ == "__main__":
    main()
