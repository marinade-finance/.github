# GitHub org wide setup

Shared GitHub Actions workflows and configuration for Marinade repositories.

## Static analysis

A reusable workflow that runs static analysis on Rust and Anchor codebases. It auto-detects what to run based on repo contents:

| Tool | Purpose | Runs when |
| --- | --- | --- |
| **clippy** | Rust linter (idioms, perf, common bugs, anti-patterns) | `<rust-workspace>/Cargo.toml` present (workspace root manifest) |
| **cargo-deny** | Supply chain: RustSec CVE advisories, license allowlist, banned/yanked crates, duplicates | `<rust-workspace>/Cargo.toml` present (workspace root manifest) |
| **Sec3 X-Ray** | Solana dataflow analyzer: signer/owner checks, PDA seed reuse, arbitrary CPI, account substitution, lamport math overflow | `<anchor-workspace>/Anchor.toml` present |
| **solana-lints** | otter-sec Dylint-based Anchor pattern lints (insecure init, bump seed canonicalization, audit-derived antipatterns) | `<anchor-workspace>/Anchor.toml` present |

Reproducible builds are **not** part of this workflow — see [Verifiable build](#verifiable-build-verifiable-buildyml) below for why, and how to enable them per repo.

Anchor detection is `Anchor.toml`-only — Cargo dependencies on `anchor-lang` / `anchor-client` are not used as a signal, since off-chain services that pull in Anchor crates for deserialization are not Anchor programs.

### Usage (default)

In a consuming repo, add `.github/workflows/static-analysis.yml`:

```yaml
name: Static Analysis
on:
  pull_request:
  push:
    branches: [main]
jobs:
  static-analysis:
    uses: marinade-finance/.github/.github/workflows/static-analysis.yml@main
```

That's it. The workflow handles detection and skips jobs that don't apply.

### Usage (with overrides)

```yaml
name: Static Analysis
on:
  pull_request:
  push:
    branches: [main]
jobs:
  static-analysis:
    uses: marinade-finance/.github/.github/workflows/static-analysis.yml@main
    with:
      clippy-deny-warnings: false        # non-blocking during initial cleanup
      rust-workspace: ./rust             # off-chain Rust crate
      anchor-workspace: ./on-chain       # Anchor.toml + programs/ live here
      run-solana: 'false'                # force-disable even if Anchor.toml exists
```

### Inputs

All inputs are optional.

| Input | Default | Purpose |
| --- | --- | --- |
| `run-rust` | `auto` | Force on/off: `'true'` / `'false'` / `'auto'` |
| `run-solana` | `auto` | Force on/off: `'true'` / `'false'` / `'auto'` |
| `rust-workspace` | `.` | Path to Rust workspace root (must contain `Cargo.toml` for clippy/cargo-deny to run) |
| `anchor-workspace` | `""` (= `rust-workspace`) | Path to Anchor workspace root (must contain `Anchor.toml`). Set when Anchor lives outside the Rust workspace |
| `anchor-programs-path` | `""` (= `programs`, resolved relative to `anchor-workspace`) | Path Sec3 X-Ray scans, resolved relative to `anchor-workspace`. Override only if your programs live somewhere other than `programs/` under the Anchor workspace |
| `rust-toolchain` | `stable` | Toolchain for clippy |
| `solana-lints-toolchain` | `nightly-2025-09-18` | Nightly for the dylint lints; must match the lints repo's own `rust-toolchain`. Forced via `RUSTUP_TOOLCHAIN` so a workspace pin can't override it |
| `clippy-deny-warnings` | `true` | Set `false` during initial cleanup |
| `xray-version` | `v0.0.6` | Sec3 X-Ray release tag to install |
| `xray-sha256` | `""` (skip) | SHA256 of the X-Ray linux-amd64 tarball. When set, the downloaded archive is verified against this checksum before extraction. Strongly recommended for supply-chain safety; leave empty to skip verification (a warning is logged) |
| `anchor-cli-version` | `0.31.1` | `anchor-cli` version installed for the sec3-xray job (X-Ray shells out to `anchor` for IDL extraction) |
| `solana-lints-repo` | `https://github.com/otter-sec/anchor-lints.git` | Git URL of the dylint lints to build, passed straight to `git clone`. otter-sec is actively maintained, Anchor-focused, and a single Cargo workspace; the legacy `crytic`/`trailofbits` `solana-lints` is dormant and stuck on `nightly-2025-01-09` (rustc 1.86), too old for workspaces whose deps need rustc 1.88+ |
| `solana-lints-ref` | (pinned SHA) | Git ref of `solana-lints-repo` to build lints from; must be compatible with `solana-lints-toolchain`, so bump the two together |
| `cargo-dylint-version` | `5.0.0` | `cargo-dylint` / `dylint-link` version installed for the solana-lints job. Must match the `dylint_linting` version the lints repo builds against — otter-sec/anchor-lints uses dylint 5.x |
| `deny-config` | `…/main/deny.toml` | URL of the cargo-deny config to fetch. Override to pin policy to a tag/SHA |

### Shared config

`cargo-deny` uses the [`deny.toml`](./deny.toml) at the root of this repo, fetched at workflow runtime via the `deny-config` URL input. By default it points at `main`, so a policy change here takes effect immediately for every consumer — useful for security advisory updates, but it does mean the workflow's pinned ref alone (e.g. `@v1`) does not fully pin behavior. To pin policy too, override `deny-config`:

```yaml
with:
  deny-config: https://raw.githubusercontent.com/marinade-finance/.github/v1/deny.toml
```

To propose a change to the shared policy (license allowlist, advisory ignore list, etc.), open a PR against this repo.

### Local-dev mirror

To catch issues before pushing, add a `Makefile` target in your repo. `make` defaults to `/bin/sh` on most systems, so the deny.toml is downloaded to a temp file rather than passed via process substitution:

```makefile
DENY_CONFIG_URL := https://raw.githubusercontent.com/marinade-finance/.github/main/deny.toml

.PHONY: check
check:
	cargo clippy --all-targets --all-features -- -D warnings
	@DENY_TMP=$$(mktemp) && \
	  curl -fsSL $(DENY_CONFIG_URL) -o $$DENY_TMP && \
	  cargo deny --config $$DENY_TMP check; \
	  rc=$$?; rm -f $$DENY_TMP; exit $$rc
```

### Versioning

While the workflow is unstable it lives at `@main`. Once stable, a `v1` tag will be cut and consumers should migrate from `@main` to `@v1` so unrelated changes to `main` cannot break every consumer at once.

Note that pinning the workflow ref alone does **not** pin `deny.toml` — see [Shared config](#shared-config) above for how to pin the cargo-deny policy as well.

### Dependabot

This repo's [`dependabot.yml`](./.github/dependabot.yml) updates pinned action SHAs monthly. Consuming repos should exclude `dtolnay/rust-toolchain` from Dependabot updates (it has only a `master` branch, no version tags), e.g.:

```yaml
ignore:
  - dependency-name: 'dtolnay/rust-toolchain'
```

### Branch protection

After rolling out, add the relevant jobs as required status checks in each consumer repo:

- All Rust repos: `clippy`, `cargo-deny`
- Anchor repos: also `sec3-xray`, `solana-lints`

Skipped jobs render as gray "skipped" rather than failures, so a TS-only repo with this workflow still goes green.

## Verifiable build (`verifiable-build.yml`)

A **separate** reusable workflow that produces the reproducible BPF build: a sha256 bytecode hash table in the run summary, plus a `verifiable-build-<sha>` artifact containing `target/deploy/*.so` (90 days by default). The build runs inside the Solana Foundation's `solanafoundation/solana-verifiable-build` Docker image, driven by Ellipsis Labs's `solana-verify build`, so the output is bit-for-bit reproducible.

It is deliberately **not** part of `static-analysis.yml`. Static analysis is attached through an org ruleset, and rulesets only trigger a required workflow on `pull_request` / `merge_group` — so a build gated on release tags or `workflow_dispatch` could never fire that way. Each program repo therefore commits its own caller at `.github/workflows/verifiable-build.yml`:

```yaml
name: Verifiable Build
on:
  workflow_dispatch:
  push:
    tags:
      - 'v*'
permissions:
  contents: read
concurrency:
  group: verifiable-build-${{ github.ref }}
  cancel-in-progress: false
jobs:
  verifiable-build:
    uses: marinade-finance/.github/.github/workflows/verifiable-build.yml@main
    with:
      library-name: 'my_program'
```

`concurrency` must be declared in the caller: a called workflow evaluates it in the caller's context, so a shared group name deadlocks it against the calling run.

| Input | Default | Purpose |
| --- | --- | --- |
| `library-name` | — | Crate to build. Required for any workspace whose members are not all SBF-buildable, or `solana-verify` builds the workspace root and fails on the first CLI or client crate |
| `base-image` | derived from `Cargo.lock` | Build image. Set it when the derived one cannot build the repo |
| `solana-verify-version` | `0.4.15` | A bump can change the bytecode hash |
| `cargo-args` | — | Appended after `--`. Needed by programs that bake build-time env into the binary |
| `verify-onchain` | `true` | Compare the built hash against deployed bytecode; fails on mismatch |
| `programs-section` | `mainnet` | Which `[programs.<cluster>]` table to read IDs from |
| `program-ids` | — | Override the IDs instead of reading `Anchor.toml` |
| `rpc-url` | `https://api.mainnet-beta.solana.com` | RPC endpoint for fetching deployed bytecode |
| `fail-on-mismatch` | `true` | Set false to report without failing |
| `anchor-workspace` | `.` | Anchor workspace root |
| `artifact-retention-days` | `90` | Retention for the uploaded `.so` |

### Per-repo values

Measured against each repo's real `Cargo.lock` and mainnet deployment.

| Repo | `library-name` | `base-image` | `solana-verify-version` | Tag trigger | On-chain verification |
| --- | --- | --- | --- | --- | --- |
| `validator-bonds` | `validator_bonds` | derived `2.3.0` | default | `contract-v*` | default (`[programs.mainnet]`) |
| `marinade-config` | `marinade_config` | **`4.0.3`** — 2.x/3.x images ship Rust 1.84, too old for its edition-2024 crates | **`0.4.11`** — produced the recorded hash | `v*` | default (`[programs.mainnet]`) |
| `distributor` | `merkle_distributor` | derived `2.3.0` | default | `v*` | default (`[programs.mainnet]`) |
| `native-staking` | `marinade_native_proxy` | derived `2.1.11` | default | `mainnet-*` | **`verify-onchain: false`** — Anchor.toml declares only `[programs.localnet]` |
| `solana-randomness-registry` | `randomness_registry` | derived `2.2.1` | **`0.4.4`** — pinned by the workflow it replaced | `v*` | **`verify-onchain: false`** — Anchor.toml declares only `[programs.localnet]` |
| `atomic-swap-contract` | `atomic_swap` | derived `2.3.0` | default | `v*` | **`programs-section: devnet`** — Anchor.toml declares no `[programs.mainnet]` |
| `liquid-staking-program` | `marinade_finance` | **none published** for its `1.15.2` lockfile | — | not adopting | — |
| `directed-stake` | `directed_stake` | **none published** for its `1.15.2` lockfile | — | not adopting | — |

`verify-onchain` defaults to true and fails rather than passing green when it can resolve no program IDs, so a repo whose `Anchor.toml` has no `[programs.mainnet]` must set one of the values above before the workflow can run. The error names which `[programs.*]` tables the repo does declare and which of the three remedies applies.

`solana-verify` picks its image from the `solana-program` version in `Cargo.lock`, not `Anchor.toml`'s `solana_version`; those disagree in four of these repos, so the workflow resolves it, logs it, and passes `--base-image` explicitly.

## Verify deployments (manual)

A separate manually-triggered reusable workflow (`verify-deployments.yml`) builds the program(s) verifiably and compares the resulting hash against every cluster declared in `Anchor.toml`'s `[programs.<cluster>]` sections. Results are rendered as a per-cluster status matrix in the run summary: ✅ match, ❌ mismatch, ❌ could not read deployed bytecode, ❌ no RPC URL for the cluster, ❌ no built artifact of that name, ⏭ skipped (localnet).

Add a thin wrapper in any Anchor consumer repo:

```yaml
name: Verify deployments
on:
  workflow_dispatch:
jobs:
  verify:
    uses: marinade-finance/.github/.github/workflows/verify-deployments.yml@main
    with:
      library-name: my-program   # required unless every workspace member is SBF-buildable
```

Then trigger it from the **Actions** tab. RPC URLs default to the public Solana endpoints (`https://api.mainnet-beta.solana.com` and the devnet/testnet equivalents) rather than CLI monikers, so resolution never depends on a Solana CLI config on the runner; override per-cluster via the `mainnet-rpc-url` / `devnet-rpc-url` / `testnet-rpc-url` inputs if you need a private RPC. `[programs.localnet]` entries are skipped (no deployment to compare against) — but a repo declaring *only* `[programs.localnet]` has nothing to verify at all, and `require-programs-section` defaults to true, so this job hard-fails there. Those repos want `require-programs-section: false`; see the On-chain verification column above.

`fail-on-mismatch` defaults to true, but only the clusters in `blocking-clusters` (default `mainnet mainnet-beta`) can fail the job. One local build cannot match two clusters holding different deployments, so a repo declaring the same program under both mainnet and devnet would otherwise be permanently red. Non-blocking clusters are still reported in the summary. Set `blocking-clusters: "*"` to gate on all of them, or `fail-on-mismatch: false` to observe drift without blocking anywhere.

## Other workflows

- [`trivy-scan.yml`](./.github/workflows/trivy-scan.yml) — filesystem vulnerability scan run on this repo.
- [`verify-dependabot.yml`](./.github/workflows/verify-dependabot.yml) — validates `dependabot.yml` config.
