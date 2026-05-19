# GitHub org wide setup

Shared GitHub Actions workflows and configuration for Marinade repositories.

## Static analysis

A reusable workflow that runs static analysis on Rust and Anchor codebases. It auto-detects what to run based on repo contents:

| Tool | Purpose | Runs when |
| --- | --- | --- |
| **clippy** | Rust linter (idioms, perf, common bugs, anti-patterns) | `<rust-workspace>/Cargo.toml` present (workspace root manifest) |
| **cargo-deny** | Supply chain: RustSec CVE advisories, license allowlist, banned/yanked crates, duplicates | `<rust-workspace>/Cargo.toml` present (workspace root manifest) |
| **Sec3 X-Ray** | Solana dataflow analyzer: signer/owner checks, PDA seed reuse, arbitrary CPI, account substitution, lamport math overflow | `<anchor-workspace>/Anchor.toml` present |
| **solana-lints** | Trail of Bits Dylint-based Anchor pattern lints (insecure init, bump seed canonicalization, audit-derived antipatterns) | `<anchor-workspace>/Anchor.toml` present |
| **solana-verify** | Reproducible build of all programs via Ellipsis Labs `solana-verifiable-build`; emits sha256 hashes to the run summary and uploads `target/deploy/*.so` as a workflow artifact | `<anchor-workspace>/Anchor.toml` present **and** push to the repo's default branch (or manual `workflow_dispatch`) |

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
| `solana-lints-toolchain` | `nightly-2025-01-09` | Nightly for solana-lints; must match upstream `crytic/solana-lints` `rust-toolchain` |
| `clippy-deny-warnings` | `true` | Set `false` during initial cleanup |
| `xray-version` | `v0.0.6` | Sec3 X-Ray release tag to install |
| `xray-sha256` | `""` (skip) | SHA256 of the X-Ray linux-amd64 tarball. When set, the downloaded archive is verified against this checksum before extraction. Strongly recommended for supply-chain safety; leave empty to skip verification (a warning is logged) |
| `anchor-cli-version` | `0.31.1` | `anchor-cli` version installed for the sec3-xray job (X-Ray shells out to `anchor` for IDL extraction) |
| `solana-lints-ref` | (pinned SHA) | `crytic/solana-lints` git ref to build lints from; bump deliberately together with `solana-lints-toolchain` |
| `deny-config` | `…/main/deny.toml` | URL of the cargo-deny config to fetch. Override to pin policy to a tag/SHA |
| `solana-verify-version` | `0.4.15` | Version of the `solana-verify` CLI used by the verifiable-build job |

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

The `verifiable-build` job is intentionally **not** a required check — it only runs on push to the default branch, so it never blocks PR merges.

### Verifiable build

For Anchor repos, `verifiable-build` runs:

- automatically on push to the default branch, and
- on demand on any branch when the consumer wrapper is triggered via `workflow_dispatch` (useful for ad-hoc release builds and for testing).

It produces a sha256 hash table for each compiled program in the run's job summary, and a workflow artifact (`verifiable-build-<sha>`) containing the `target/deploy/*.so` files, retained for 90 days.

The build runs inside Ellipsis Labs's `solana-verifiable-build` Docker image (invoked by `solana-verify build`), so the produced binary is bit-for-bit reproducible.

To enable manual triggering, add `workflow_dispatch:` to the consumer wrapper:

```yaml
on:
  pull_request:
  push:
    branches: [main]
  workflow_dispatch:        # enables manual verifiable-build runs on any branch
```

### Verify deployments (manual)

A separate manually-triggered reusable workflow (`verify-deployments.yml`) builds the program(s) verifiably and compares the resulting hash against every cluster declared in `Anchor.toml`'s `[programs.<cluster>]` sections. Results are rendered as a per-cluster status matrix in the run summary (✅ match / ❌ mismatch / ⏸ not deployed).

Add a thin wrapper in any Anchor consumer repo:

```yaml
name: Verify deployments
on:
  workflow_dispatch:
jobs:
  verify:
    uses: marinade-finance/.github/.github/workflows/verify-deployments.yml@main
```

Then trigger it from the **Actions** tab. RPC URLs default to public Solana cluster monikers (`mainnet-beta`, `devnet`, `testnet`); override per-cluster via the `mainnet-rpc-url` / `devnet-rpc-url` / `testnet-rpc-url` inputs if you need a private RPC. `[programs.localnet]` entries are skipped (no deployment to compare against).

The job does not fail on mismatch — by design, since the deployed program is often older than the default-branch HEAD.

## Other workflows

- [`trivy-scan.yml`](./.github/workflows/trivy-scan.yml) — filesystem vulnerability scan run on this repo.
- [`verify-dependabot.yml`](./.github/workflows/verify-dependabot.yml) — validates `dependabot.yml` config.
