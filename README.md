# GitHub org wide setup

Shared GitHub Actions workflows and configuration for Marinade repositories.

## Static analysis

A reusable workflow that runs static analysis on Rust and Anchor codebases. It auto-detects what to run based on repo contents:

| Tool | Purpose | Runs when |
| --- | --- | --- |
| **clippy** | Rust linter (idioms, perf, common bugs, anti-patterns) | Any `Cargo.toml` present |
| **cargo-deny** | Supply chain: RustSec CVE advisories, license allowlist, banned/yanked crates, duplicates | Any `Cargo.toml` present |
| **Sec3 X-Ray** | Solana dataflow analyzer: signer/owner checks, PDA seed reuse, arbitrary CPI, account substitution, lamport math overflow | `Anchor.toml` present |
| **solana-lints** | Trail of Bits Dylint-based Anchor pattern lints (insecure init, bump seed canonicalization, audit-derived antipatterns) | `Anchor.toml` present |

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
      rust-workspace: ./rust             # rust lives in subdir
      anchor-programs-path: ./on-chain/programs
      run-solana: 'false'                # force-disable even if Anchor.toml exists
```

### Inputs

All inputs are optional.

| Input | Default | Purpose |
| --- | --- | --- |
| `run-rust` | `auto` | Force on/off: `'true'` / `'false'` / `'auto'` |
| `run-solana` | `auto` | Force on/off: `'true'` / `'false'` / `'auto'` |
| `rust-workspace` | `.` | Path to Rust workspace root |
| `anchor-programs-path` | `./programs` | Path Sec3 X-Ray scans |
| `rust-toolchain` | `stable` | Toolchain for clippy |
| `solana-lints-toolchain` | `nightly-2025-01-09` | Nightly for solana-lints; must match upstream `crytic/solana-lints` `rust-toolchain` |
| `clippy-deny-warnings` | `true` | Set `false` during initial cleanup |
| `xray-version` | `v0.0.6` | Sec3 X-Ray release tag to install |
| `solana-lints-ref` | (pinned SHA) | `crytic/solana-lints` git ref to build lints from; bump deliberately together with `solana-lints-toolchain` |
| `deny-config` | `…/main/deny.toml` | URL of the cargo-deny config to fetch. Override to pin policy to a tag/SHA |

### Shared config

`cargo-deny` uses the [`deny.toml`](./deny.toml) at the root of this repo, fetched at workflow runtime via the `deny-config` URL input. By default it points at `main`, so a policy change here takes effect immediately for every consumer — useful for security advisory updates, but it does mean the workflow's pinned ref alone (e.g. `@v1`) does not fully pin behavior. To pin policy too, override `deny-config`:

```yaml
with:
  deny-config: https://raw.githubusercontent.com/marinade-finance/.github/v1/deny.toml
```

To propose a change to the shared policy (license allowlist, advisory ignore list, etc.), open a PR against this repo.

### Local-dev mirror

To catch issues before pushing, add a `Makefile` target in your repo:

```makefile
.PHONY: check
check:
	cargo clippy --all-targets --all-features -- -D warnings
	cargo deny --config <(curl -fsSL https://raw.githubusercontent.com/marinade-finance/.github/main/deny.toml) check
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

## Other workflows

- [`trivy-scan.yml`](./.github/workflows/trivy-scan.yml) — filesystem vulnerability scan run on this repo.
- [`verify-dependabot.yml`](./.github/workflows/verify-dependabot.yml) — validates `dependabot.yml` config.
