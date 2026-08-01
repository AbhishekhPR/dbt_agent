# Semantic refund fallback

Declared contracts, invariants, and KPI definitions take precedence over SQL
fallback analysis. The owning declared semantic engine records its own finding
when that evidence changes.

When declared semantic context is unavailable, Relium can apply a deliberately narrow
fallback that detects removal of refund/adjustment subtraction from
net/gross business expressions. Its internal finding owner is
`semantic_refund_fallback` and its evidence comes from trusted immutable base
and head manifests.

This fallback is not arbitrary SQL semantic equivalence. It does not prove that
two queries are generally equivalent, and it must not be presented as a general
SQL reasoning engine. It only recognizes the scoped adjustment-removal pattern;
safe rewrites that retain the subtraction are negative controls.
