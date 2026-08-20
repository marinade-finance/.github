# Verifiable-build rollout

Every Marinade Solana program repo should build its bytecode the same way, through
one shared reusable workflow, and every build should be checked against what is
actually deployed on chain. This directory holds everything a repo owner needs to
get there.

The shared workflows live in this repo:

| Workflow | Purpose | When it runs |
| --- | --- | --- |
| [`.github/workflows/verifiable-build.yml`](../.github/workflows/verifiable-build.yml) | Reproducible build, hash table, artifact upload, **and comparison against deployed bytecode** | Release tags and manual dispatch |
| [`.github/workflows/verify-deployments.yml`](../.github/workflows/verify-deployments.yml) | Drift detection across every cluster in `Anchor.toml` | Weekly schedule |

Neither has ever executed. Zero callers exist org-wide today, which is the whole
reason for this rollout.

## Why the shared workflow changed

`verifiable-build.yml` as it stood could not be adopted by all eight repos, and
`verify-deployments.yml` could report success without verifying anything. Both
were fixed:

- **The build image is now an input.** `solana-verify` picks its Docker image from
  the `solana-program` version in `Cargo.lock` — not from `Anchor.toml`'s
  `solana_version`, which disagrees with the lockfile in four of these repos. The
  workflow now resolves that version itself, logs the image, and passes
  `--base-image` explicitly, so the choice is visible and a repo can override it.
- **A missing image now fails comprehensibly.** Two repos pin `solana-program`
  1.15.2, for which no image is published. Previously that surfaced as an opaque
  `docker pull` 404 mid-build. Now a manifest check runs first and names the fix.
  Registry-missing and infrastructure failures are reported as different things,
  so nobody edits `Cargo.lock` to chase a full runner disk.
- **The build is compared against the chain.** Building only proves the source
  compiles. `verifiable-build.yml` now reads program IDs from
  `[programs.mainnet]` (or the `program-ids` input), fetches the deployed hash,
  and fails on a mismatch.
- **"Nothing to verify" is now a failure, not a pass.** This was the most
  misleading behaviour in the old `verify-deployments.yml`: a repo with no
  `[programs.mainnet]` section exited 0 with a warning, making *verified* and
  *never checked* indistinguishable in the Actions UI. Unreadable on-chain hashes
  and declared-but-unbuilt programs now also count as unverified.

## Order of operations

Do these in order. Steps 1 and 2 are per repo and independent of other repos.

1. **Apply the `Anchor.toml` patch** for your repo, if the table below says you
   need one. `git apply` from the repo root:
   ```sh
   git apply /path/to/.github/rollout/anchor-toml/<repo>.patch
   ```
   Do this **first**. The workflow fails by design when there is no
   `[programs.mainnet]` table, so adopting the caller before the patch produces a
   red build with the message `No program IDs to verify against`.
2. **Copy the caller stub** [`verifiable-build.yml`](verifiable-build.yml) to
   `.github/workflows/verifiable-build.yml` in your repo, adjusting inputs per the
   table below. It needs no secrets and no `secrets: inherit`.
3. **Run it once by hand** — Actions → Verifiable Build → Run workflow — and
   confirm the summary shows `✅ match` for your program. This is the step that
   proves the rollout; nothing before it has been executed in CI.
4. **Optionally add** [`verify-deployments.yml`](verify-deployments.yml) for
   weekly drift detection, once step 3 is green.
5. **Retire the repo's ad-hoc build CI.** `marinade-config`'s
   `build-program.yml`, `distributor`'s `deploy.bash`, and
   `solana-randomness-registry`'s existing `verifiable-build.yml` + `verify.bash`
   all duplicate what the shared workflow now does. Remove them only after step 3
   passes, so there is never a window with no build coverage.

## Per-repo adoption table

`base-image` empty means the shared workflow derives it from `Cargo.lock`; the
resolved value is shown so you can confirm it against the run log. All eight
resolutions below were produced by running the workflow's actual resolution step
against each repo's real `Cargo.lock`.

Two caveats found by reviewing the adoption PRs against the repos themselves, both
of which make a run fail rather than mislead:

- **A derived image is not always a usable one.** `marinade-config` must override to
  `4.0.3`: the SBF platform-tools in the 2.x and 3.x images ship Rust 1.84, which
  cannot parse the 27 edition-2024 crates its lockfile pulls in, and its recorded
  hash was produced with 4.0.3. Check a repo's own build CI before trusting the
  derived value.
- **`library-name` is required for every multi-member workspace.** Without it
  `solana-verify` builds from the workspace root, cargo resolves every member, and
  the build dies on the first crate that is not a Solana program — a CLI, a client,
  a test harness. Only a single-crate repo can leave it empty.

| Repo | `anchor-workspace` | `library-name` | `base-image` | `solana-verify-version` | `[programs.mainnet]` | Program name → ID |
| --- | --- | --- | --- | --- | --- | --- |
| `validator-bonds` | `.` | `validator_bonds` | *(empty)* → `…:2.3.0` | default `0.4.15` | already correct | `validator_bonds` → `vBoNdEvzMrSai7is21XgVYik65mqtaKXuSdMBJ1xkW4` |
| `marinade-config` | `.` | `marinade_config` | **`…:4.0.3`** (override) | **`0.4.11`** — produced the recorded hash | already correct | `marinade_config` → `MCfgVNYfk5NmQQuyoM7BoUPoqpNtdkBQ1273AJVQk4x` |
| `distributor` | `.` | `merkle_distributor` | *(empty)* → `…:2.3.0` | default `0.4.15` | already correct | `merkle_distributor` → `meRdrpyDCAbQxunjZSLmJ78GxQcn4fJUvqU93GoHZr1` |
| `native-staking` | `.` | `marinade_native_proxy` | *(empty)* → `…:2.1.11` | default `0.4.15` | **add** — [patch](anchor-toml/native-staking.patch) | `marinade_native_proxy` → `mnspJQyF1KdDEs5c6YJPocYdY1esBgVQFufM2dY9oDk` |
| `solana-randomness-registry` | `.` | `randomness_registry` | *(empty)* → `…:2.2.1` | **`0.4.4`** — was pinned by the workflow this replaces | **add** — [patch](anchor-toml/solana-randomness-registry.patch) | `randomness_registry` → `rand4M2SB9tZmTz37p9yEhXC6LtWnBJhBRXkjX2YmNH` |
| `atomic-swap-contract` | `.` | `atomic_swap` | *(empty)* → `…:2.3.0` | default `0.4.15` | **add** — [patch](anchor-toml/atomic-swap-contract.patch) | `atomic_swap` → `AtoMXWZvgxPLRzG2s1KsN9UKFREXD15j6PuP9aMXth5` |
| `liquid-staking-program` | — | `marinade_finance` | **no image exists** | — | **add** — [patch](anchor-toml/liquid-staking-program.patch) | `marinade_finance` → `MarBmsSgKXdrN1egZf5sqe1TMai9K1rChYNDJgjq7aD` |
| `directed-stake` | — | `directed_stake` | **no image exists** | — | already correct | `directed_stake` → `dstK1PDHNoKN9MdmftRzsEbXP5T1FTBiQBm1Ee3meVd` |

`liquid-staking-program` and `directed-stake` **do not adopt in this rollout** —
see [below](#liquid-staking-program-and-directed-stake). Their `Anchor.toml`
patch is still worth landing, because it is correct and it is a prerequisite for
whenever they do adopt.

The program *name* in each row is the crate's `[lib] name`, which is also the
`target/deploy/<name>.so` file stem. The workflow matches `Anchor.toml` keys to
built artifacts by that name, and fails loudly if a declared name has no
artifact. All eight already agree, so no repo needs a rename.

### Evidence for every program ID

No ID here was taken from internal docs. Internal docs contradict each other —
the native-staking proxy appears under three different addresses and
validator-bonds under two — so the chain was treated as the only authority.

Each ID was confirmed three ways:

1. **It is the ID the repo itself declares.** `declare_id!()` in the program
   source matches `Anchor.toml` in all eight repos, with no disagreement.
2. **The account is a live upgradeable program on mainnet.** `getAccountInfo`
   against `api.mainnet-beta.solana.com` returns `executable: true` and
   `owner: BPFLoaderUpgradeab1e11111111111111111111111` for all eight, each with
   a `programData` address.
3. **Its deployed bytecode hashes to the expected value.** Each `programData`
   account was fetched, its 45-byte header stripped, trailing zeros trimmed, and
   the remainder SHA-256'd — reproducing the independently measured baseline
   hashes exactly, byte length and digest.

| Program ID | Bytes | sha256 (deployed) | Last upgrade slot | Upgrade authority |
| --- | --- | --- | --- | --- |
| `vBoNdEvzMrSai7is21XgVYik65mqtaKXuSdMBJ1xkW4` | 1113689 | `5c7e0fa636f2f839…` | 410576659 | `6YAju4nd4t7kyuHV6NvVpMepMk11DgWyYjKVJUak2EEm` |
| `MCfgVNYfk5NmQQuyoM7BoUPoqpNtdkBQ1273AJVQk4x` | 231457 | `6214250989b21969…` | 434478711 | `upXkU8TNv4UqPzad6XBmGzdvmo4T3pvYFv7pNe1MKGQ` |
| `meRdrpyDCAbQxunjZSLmJ78GxQcn4fJUvqU93GoHZr1` | 351561 | `d5be8412a0c4cbc9…` | 397804420 | `upXkU8TNv4UqPzad6XBmGzdvmo4T3pvYFv7pNe1MKGQ` |
| `mnspJQyF1KdDEs5c6YJPocYdY1esBgVQFufM2dY9oDk` | 423689 | `9176889dbe7b55bc…` | 333813058 | `6YAju4nd4t7kyuHV6NvVpMepMk11DgWyYjKVJUak2EEm` |
| `rand4M2SB9tZmTz37p9yEhXC6LtWnBJhBRXkjX2YmNH` | 308561 | `bba57ed49ea12327…` | 416011845 | `upXkU8TNv4UqPzad6XBmGzdvmo4T3pvYFv7pNe1MKGQ` |
| `AtoMXWZvgxPLRzG2s1KsN9UKFREXD15j6PuP9aMXth5` | 341633 | `4a7d2df167037018…` | 377174652 | `6YAju4nd4t7kyuHV6NvVpMepMk11DgWyYjKVJUak2EEm` |
| `MarBmsSgKXdrN1egZf5sqe1TMai9K1rChYNDJgjq7aD` | 1254137 | `db89efca1670f853…` | 433290841 | `551FBXSXdhcRDDkdcb3ThDRg84Mwe5Zs6YjJ1EEoyzBp` |
| `dstK1PDHNoKN9MdmftRzsEbXP5T1FTBiQBm1Ee3meVd` | 289073 | `d1cd8d4f26b753dc…` | 246472200 | `2aQP7NGhktKR92EsHKSoRzcw5FfcZ8oBWgyoGdB3ouww` |

The three distinct upgrade authorities are corroborating rather than primary
evidence: they cluster the eight programs into recognisable Marinade-controlled
groups, with no unexpected outside authority.

Two IDs from the internal inventory were checked and are **not deployed** on
mainnet: `vote-aggregator`, and the `MarinadeUnstake111…` placeholder. Neither
belongs in any `[programs.mainnet]` table, and neither repo appears above.

One thing worth flagging to the `atomic-swap-contract` owner: that repo declared
only `[programs.devnet]`, but the *same* address is live on mainnet with 341633
bytes of bytecode. The patch adds a `[programs.mainnet]` entry with that same ID.
Confirm this is intended before merging — it is the one row where the repo's own
config gave no hint that a mainnet deployment existed.

## What breaks if a repo skips this

Nothing breaks in the sense of a build going red — which is exactly the problem.

- **Skip the `Anchor.toml` patch, keep the old workflow:** the repo keeps
  reporting green while verifying nothing. `verify-deployments.yml` keys off
  `[programs.mainnet]`; with no such section it checked zero programs and exited
  0. That green check is the failure this rollout exists to remove.
- **Skip the patch, adopt the new caller:** the run fails immediately with
  `No program IDs to verify against`, naming the patch. Loud and cheap to fix —
  this is the intended behaviour, not a regression.
- **Skip adoption entirely:** the repo has no reproducible build path. Its
  bytecode cannot be independently tied to its source, so nobody outside the
  deploying engineer can confirm what is running. For `marinade-config`,
  `distributor`, and `solana-randomness-registry` the hand-rolled build CI also
  stays as a second, divergent definition of "the build".
- **Adopt with `verify-onchain: false`:** you get a reproducible artifact and a
  hash, and no statement at all about the deployment. Legitimate as a temporary
  step, misleading if left indefinitely.

## `liquid-staking-program` and `directed-stake`

**Recommendation: do not adopt yet. Realign the toolchain forward, then adopt.**

Both repos pin `solana-program` 1.15.2 in `Cargo.lock`, and there is no
`solanafoundation/solana-verifiable-build:1.15.2` image. Confirmed against the
registry: tags exist for 4.0.3, 2.3.1, 2.3.0, 2.2.17, 2.2.1, 2.1.11 and 1.14.29,
but not 1.15.2. There is therefore no verifiable-build path for these two repos
today, and the shared workflow now says so explicitly rather than failing
obscurely.

Three options were considered.

**Option A — realign the lockfile and Anchor version forward (recommended).**
Move to Anchor 0.31.x and a `solana-program` 2.x that has a published image, then
redeploy, then adopt the workflow with `verify-onchain: true`.

- The forward images are *hermetic*, which the legacy ones are not: the 2.1.11
  image ships `platform-tools v1.43` baked into the image. Verified by inspecting
  the pulled image.
- It ends with these two repos on the same toolchain and the same workflow as the
  other six, with no per-repo special case to maintain.
- Honest cost: the built hash will **not** match today's deployed bytecode, so
  verification only becomes meaningful from the next deploy onward. Anchor
  0.27 → 0.31 is a breaking upgrade requiring source changes.
- Sequence it `directed-stake` first: 289 KB, last upgraded at slot 246472200,
  far lower risk than mSOL. Do mSOL second, as a reviewed change — it is the
  flagship program and holds real value.

**Option B — pin an older `solana-verify` against the declared 1.14.29 image
(not recommended).** Two independent findings argue against it:

- The 1.14.29 image's `cargo-build-sbf` (1.14.29, cargo 1.68.0) **rejects
  `--config` outright**: `error: Found argument '--config' which wasn't expected`.
  Reproduced directly in the pulled image.
- More seriously, that image is **not hermetic**. It has no toolchain baked in
  and downloads `solana-sbf-tools-linux.tar.bz2` from a 2023 `solana-labs/bpf-tools`
  GitHub release *at build time*. Observed directly: the build attempt failed
  trying to fetch that URL. Pinning a "reproducible" build to a three-year-old
  release artifact, unpinned by digest and outside Marinade's control, is a
  weaker guarantee than the one it is meant to provide.
- It also would not necessarily reproduce the deployed bytecode anyway: the
  lockfile says 1.15.2, not 1.14.29.

**Option C — publish a 1.15.2 image ourselves (fallback only).** This is the only
option that *could* reproduce the currently deployed bytecode, because it matches
the lockfile. Consider it only if verifying the existing mSOL deployment is a hard
audit or compliance requirement. It is unproven — a 1.15.x-era image would
download its toolchain at build time just as 1.14.29 does — and it makes Marinade
responsible for maintaining a build image indefinitely. Time-box any attempt.

Landing the `Anchor.toml` patch for `liquid-staking-program` is still worth doing
now: the ID is chain-confirmed, and the missing `[programs.mainnet]` section is a
prerequisite for whichever option is chosen.

## What has been validated, and what has not

Validated locally, against the real repo files:

- Both shared workflows parse as YAML; every embedded shell block passes
  `bash -n` and every embedded Python heredoc compiles.
- The image-resolution step, run as-is against all eight `Cargo.lock` files,
  produces the images in the table above — including correctly resolving the two
  repos to the nonexistent 1.15.2 tag.
- The image pull step fails with the "no such image tag" message for 1.15.2 and
  succeeds for 2.2.1.
- The program-ID resolution step, run against all eight real `Anchor.toml` files,
  finds IDs in exactly three repos plus `directed-stake`, and returns empty for
  the five that need the patch — the case that is now a hard failure.
- All four patches pass `git apply --check` against pristine copies, and the
  patched files parse as TOML and yield the expected chain-confirmed IDs.
- The `program-ids` override parses valid input and rejects malformed entries.
- All eight program IDs confirmed live, upgradeable, and hash-matching on mainnet.

**Not validated, and only a real CI run can settle it:** no reproducible build
was executed end to end. This sandbox cannot reach crates.io or the Solana
platform-tools releases from inside a build container, so every `solana-verify
build` attempt fails on network before compiling — an environmental limit, not a
workflow defect. `solana-verify get-program-hash` is also unusable here: its RPC
client does not trust the sandbox's TLS-intercepting proxy, so it reports
`AccountNotFound` for programs that plain `curl` confirms exist. The on-chain
hashes above were therefore computed directly from the RPC rather than through
`solana-verify`.

So the following remain unproven until a `workflow_dispatch` run in CI:

- that `solana-verify build` succeeds inside each repo under its resolved image;
- that `solana-verify get-program-hash` returns the same digests computed here;
- most importantly, **whether each repo's build actually matches its deployed
  bytecode** — a mismatch on first run is a real possibility and would mean the
  deployment predates the current `main`, not that the workflow is broken.

Prove it per repo with the shared workflow's own manual trigger, cheapest repo
first:

```sh
# 1. Land the caller stub (and the Anchor.toml patch, if needed) on a branch.
# 2. Dispatch against that branch — no tag or release needed:
gh workflow run verifiable-build.yml --repo marinade-finance/marinade-config --ref <branch>
gh run watch --repo marinade-finance/marinade-config

# 3. Read the run summary: it prints the resolved image, the built hash, the
#    deployed hash, and a match/mismatch verdict per program.
```

Suggested order — smallest bytecode first, so a toolchain problem surfaces on the
cheapest build: `marinade-config` (231 KB), `solana-randomness-registry` (309 KB),
`atomic-swap-contract` (342 KB), `distributor` (352 KB), `native-staking`
(424 KB), `validator-bonds` (1.11 MB).
