# Held Asset Materiality

Trader v3 remains fail-closed when a governed holding lacks fresh Research
valuation. A second, read-only exact-contract lookup may waive that block only
when it proves the holding is economically immaterial.

- The dust boundary is 5 USDC.
- The highest positive USD price found across exact Base contract pairs with an
  approved USDC, USDbC, or WETH quote is multiplied by 1.25.
- Only a holding whose balance times that conservative price is strictly below
  5 USDC is excluded from portfolio value, valuation coverage, and execution
  selection and counted as quarantined.
- Missing, malformed, unavailable, or boundary-and-above evidence remains
  blocking.
- Dust evidence is never used as a trading signal and has no signing path.

The reader batches at most 30 contracts and caches positive and negative
results for ten minutes to bound provider traffic. The pre-existing sub-gwei
residue floor remains an earlier guard for mechanically negligible swap
rounding.
