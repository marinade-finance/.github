#!/usr/bin/env python3
"""End-to-end test of program_buffer.py on a local solana-test-validator.

A local keypair stands in for the governance native treasury: the proposal payloads
are decoded and executed signed by it, as SPL Governance would execute them.

Needs Docker, the `cryptography` package, the Program Metadata program binary
(dump it from mainnet: `solana program dump ProgM6JCCvbYkfKqJYHePx4xxSUSqJp7rh8Lyv7nk7S pm.so`)
and two builds of one program (the deployed and the proposed .so), plus its IDL:

    python3 tests/local_validator_test.py --pm-so pm.so --old-so old.so --new-so new.so --idl idl.json
"""

from __future__ import annotations

import argparse
import base64
import json
import os
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
    def __init__(self, image: str, work: Path, programs: list[tuple[str, Path, str]], pm_so: Path):
        self.name = f"pb-test-validator-{os.getpid()}"
        # Agave 3.x asserts io_uring support, which Docker's default seccomp profile blocks.
        args = ["docker", "run", "-d", "--rm", "--name", self.name, "--network", "host",
                "--security-opt", "seccomp=unconfined",
                "-v", f"{work}:/work", "--entrypoint", "solana-test-validator", image,
                "--ledger", "/tmp/ledger", "--reset", "--quiet",
                "--bpf-program", pb.PROGRAM_METADATA, f"/work/{pm_so.name}"]
        for address, so, authority in programs:
            args += ["--upgradeable-program", address, f"/work/{so.name}", authority]
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


def execute(rpc: pb.Rpc, authority: pb.Keypair, groups: list[list[dict]]):
    """What SPL Governance does: each group is one transaction signed by the treasury."""
    for i, group in enumerate(groups):
        ixs = [pb.decode_instruction_data(base64.b64decode(entry["base64"]))[0] for entry in group]
        pb.send_and_confirm(rpc, authority, ixs, label=f"governance executes transaction {i + 1}")


class Scenario:
    def __init__(self, work: Path, image: str, rpc: pb.Rpc, program: str, keys: dict[str, pb.Keypair]):
        self.work, self.image, self.rpc, self.program, self.keys = work, image, rpc, program, keys

    def path(self, name: str) -> str:
        return str(self.work / name)

    def write_program_buffer(self, so: str) -> str:
        buffer = pb.Keypair.generate()
        buffer.save(self.path("buffer-keypair.json"))
        out = run("docker", "run", "--rm", "--network", "host", "-v", f"{self.work}:/work",
                  "--entrypoint", "solana", self.image, "program", "write-buffer", f"/work/{Path(so).name}",
                  "--buffer", "/work/buffer-keypair.json", "-u", RPC, "-k", "/work/proposer.json",
                  "--use-rpc", "--max-sign-attempts", "50", capture=True)
        print(out)
        address = out.split("Buffer:")[1].split()[0]
        assert address == buffer.pubkey, (address, buffer.pubkey)
        return address

    def propose(self, so: str, idl: str | None, tag: str) -> dict:
        """The write job: plan, both buffers, handoff, payloads. Returns the artifacts."""
        p, t, r = (self.keys[k].pubkey for k in ("proposer", "treasury", "refund"))
        onchain, plan = self.path(f"{tag}-onchain.json"), self.path(f"{tag}-plan.json")
        tool("onchain", "--rpc", RPC, "--program", self.program, "--upgrade-authority", t, "--out", onchain)
        tool("plan", "--rpc", RPC, "--onchain", onchain, "--so", so, "--idl", idl or "", "--proposer", p,
             "--refund-to", r, "--out", plan)
        tool("check-funds", "--rpc", RPC, "--plan", plan, "--proposer", p)
        program_buffer = self.write_program_buffer(so)
        tool("verify-buffer", "--rpc", RPC, "--buffer", program_buffer, "--so", so, "--authority", p)
        plan_data = pb.read_json(plan)
        idl_buffer = ""
        if idl and plan_data["idl"]["mode"] != "unchanged":
            tool("write-idl-buffer", "--rpc", RPC, "--keypair", self.path("proposer.json"), "--idl", idl,
                 "--buffer-keypair", self.path("idl-buffer-keypair.json"), "--out", self.path("idl-buffer.txt"))
            idl_buffer = Path(self.path("idl-buffer.txt")).read_text().strip()
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

    def fund_proposer(self, plan_lamports: int):
        airdrop(self.rpc, self.keys["proposer"].pubkey, plan_lamports // pb.LAMPORTS_PER_SOL + 2)

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
            rent = self.rpc.rent(entry["size"])
            assert entry["lamports"] >= rent, (entry["lamports"], rent)


def big_idl(path: Path, entries: int, seed: str):
    """An IDL whose zlib form exceeds the 10 KiB single-instruction realloc limit."""
    import hashlib
    noise = [hashlib.sha256(f"{seed}{i}".encode()).hexdigest() for i in range(entries)]
    path.write_text(json.dumps({"address": "", "metadata": {"name": "big"}, "instructions": [],
                                "docs": noise}))
    print(f"{path.name}: {len(pb.pack_idl(path.read_bytes()))} bytes packed")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--pm-so", required=True)
    p.add_argument("--old-so", required=True)
    p.add_argument("--new-so", required=True)
    p.add_argument("--idl", required=True)
    p.add_argument("--image", default="solanafoundation/solana-verifiable-build:4.0.3")
    args = p.parse_args()

    work = Path(tempfile.mkdtemp(prefix="pb-test-"))
    os.chmod(work, 0o755)
    for name, src in (("pm.so", args.pm_so), ("old.so", args.old_so), ("new.so", args.new_so)):
        (work / name).write_bytes(Path(src).read_bytes())
    keys = {k: pb.Keypair.generate() for k in ("proposer", "treasury", "refund", "program_a", "program_b")}
    keys["proposer"].save(str(work / "proposer.json"))
    os.chmod(work / "proposer.json", 0o644)  # readable by the CLI container
    treasury = keys["treasury"].pubkey
    program_a, program_b = keys["program_a"].pubkey, keys["program_b"].pubkey
    validator = Validator(args.image, work, [(program_a, work / "old.so", treasury),
                                             (program_b, work / "old.so", treasury)], work / "pm.so")
    rpc = validator.rpc
    refund = keys["refund"].pubkey
    new_so, old_so = str(work / "new.so"), str(work / "old.so")
    try:
        airdrop(rpc, treasury, 2)

        # 1. program A: upgrade with a first, small IDL (create without extend)
        a = Scenario(work, args.image, rpc, program_a, keys)
        a.fund_proposer(10 * pb.LAMPORTS_PER_SOL)
        before = rpc.balance(refund)
        result = a.propose(new_so, args.idl, "a1")
        assert result["plan"]["idl"]["mode"] == "create"
        execute(rpc, keys["treasury"], result["payloads"]["upgrade"])
        a.check_upgraded(new_so, result, args.idl)
        refunded = rpc.balance(refund) - before
        spent = 12 * pb.LAMPORTS_PER_SOL - refunded
        print(f"scenario 1: refunded {pb.sol(refunded)} SOL, kept {pb.sol(spent)} SOL (IDL rent + fees)")
        assert spent < result["plan"]["costs"]["idl_metadata_rent"] + 50_000_000, spent

        # 2. program A: back to the old build with a smaller IDL (update + trim)
        small = work / "idl-small.json"
        idl = json.loads(Path(args.idl).read_text())
        idl["instructions"] = idl["instructions"][:1]
        small.write_text(json.dumps(idl))
        a.fund_proposer(10 * pb.LAMPORTS_PER_SOL)
        result = a.propose(old_so, str(small), "a2")
        assert result["plan"]["idl"]["mode"] == "update"
        execute(rpc, keys["treasury"], result["payloads"]["upgrade"])
        a.check_upgraded(old_so, result, str(small))

        # 3. program A: same IDL again is left alone, the vote fails, the abort proposal refunds
        a.fund_proposer(10 * pb.LAMPORTS_PER_SOL)
        result = a.propose(new_so, str(small), "a3")
        assert result["plan"]["idl"]["mode"] == "unchanged" and not result["idl_buffer"]
        before = rpc.balance(refund)
        execute(rpc, keys["treasury"], result["payloads"]["abort"])
        assert rpc.account(result["program_buffer"]) is None
        assert pb.read_program(rpc, program_a)["hash"] == pb.executable_hash(Path(old_so).read_bytes())
        assert rpc.balance(refund) - before == result["plan"]["costs"]["program_buffer_rent"]

        # 4. program B: a large IDL (create with extends), then a much larger one (update with extends)
        b = Scenario(work, args.image, rpc, program_b, keys)
        large, larger = work / "idl-large.json", work / "idl-larger.json"
        big_idl(large, 400, "a")
        big_idl(larger, 1300, "b")
        for idl_file, tag, mode in ((large, "b1", "create"), (larger, "b2", "update")):
            b.fund_proposer(12 * pb.LAMPORTS_PER_SOL)
            result = b.propose(new_so if tag == "b1" else old_so, str(idl_file), tag)
            assert result["plan"]["idl"]["mode"] == mode
            groups = result["payloads"]["upgrade"]
            print(f"{tag}: {[len(g) for g in groups]} instructions per proposal transaction")
            # executing the data transaction before the sizing ones fails without effect
            if mode == "create":
                try:
                    execute(rpc, keys["treasury"], [groups[-1]])
                    raise AssertionError("data transaction ran before allocation")
                except pb.Failure as e:
                    print(f"out-of-order data transaction refused as expected: {str(e)[:120]}")
            execute(rpc, keys["treasury"], groups)
            b.check_upgraded(new_so if tag == "b1" else old_so, result, str(idl_file))

        # 5. a run that fails after writing: close-buffers returns everything the proposer still owns
        a.fund_proposer(10 * pb.LAMPORTS_PER_SOL)
        a.write_program_buffer(new_so)
        tool("write-idl-buffer", "--rpc", RPC, "--keypair", str(work / "proposer.json"), "--idl", str(large),
             "--buffer-keypair", str(work / "idl-buffer-keypair.json"), "--out", str(work / "idl-buffer.txt"))
        assert len(pb.find_buffers(rpc, keys["proposer"].pubkey)) == 2
        tool("close-buffers", "--rpc", RPC, "--keypair", str(work / "proposer.json"), "--refund-to", refund)
        tool("sweep", "--rpc", RPC, "--keypair", str(work / "proposer.json"), "--refund-to", refund)
        assert not pb.find_buffers(rpc, keys["proposer"].pubkey)
        assert rpc.balance(keys["proposer"].pubkey) == 0

        print(f"\nALL SCENARIOS PASSED (work dir {work})")
    finally:
        validator.stop()


if __name__ == "__main__":
    main()
