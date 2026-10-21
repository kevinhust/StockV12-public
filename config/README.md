# V12 config

Shared risk limits and regime tables remain at the **repository root**:

- `config/risk_regimes.yaml` — `global_limits`, `v12_sizing`, regime blocks.
  Not shipped in this public snapshot (it is deployed per host). When absent,
  `stockapp_v12.core.limits` logs a warning and uses built-in defaults. A V11-era
  reference copy with the same sections is in
  [`legacy/v11/config/risk_regimes.yaml`](../legacy/v11/config/risk_regimes.yaml).

Add V12-only files here when they should not live in the monorepo `config/` tree.
