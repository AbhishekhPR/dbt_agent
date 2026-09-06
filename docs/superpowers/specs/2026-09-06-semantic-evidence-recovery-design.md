# Semantic Evidence Recovery Design

## Goal

Make a new review attempt recover SQL semantic evidence from the review's exact immutable base/head manifests when an older current attempt predates semantic-evidence persistence, without changing historical attempts or coupling code analysis to warehouse evidence.

## Evidence and root cause

Production review `gh-2d7269f587057aa260610b1c77fc2861` records the expected base/head SHAs and manifest hashes. Both manifest rows exist and both contain compiled SQL for `int_subscription_revenue`. Attempt 1 was created through the manifest-resume path before that path passed semantic evidence into `begin_review`; attempt 2 was later created by metadata recomputation and copied the resulting null. The current API and frontend therefore truthfully render the current attempt as unavailable.

## Data flow

Direct webhook and current manifest-resume reviews continue computing semantic evidence before the initial attempt is written. Metadata recomputation first carries an existing semantic document from the immediately previous attempt. Only when that value is null does it look up the same review's exact base/head manifest rows, verify both stored hashes against the immutable review binding, and run code analysis for the review's stored changed models. A successful evaluated document is written only to the new attempt. Any missing manifest, hash mismatch, missing model, or unreadable SQL leaves the new attempt unavailable.

Warehouse snapshots remain inputs only to production-metadata comparison and final evidence policy. They are not inputs to SQL semantic comparison.

## Compatibility and UI

No historical attempt is updated. The attempts API continues projecting semantic evidence from each attempt row, and the frontend continues selecting the row matching `review.attempt`. An evaluated document with zero changes remains an object with `status: evaluated`; null remains unavailable.

## Verification

Regression coverage exercises the exact LEFT JOIN plus nullable-side WHERE filter, a safe projection addition, the served direct webhook, manifest resume, metadata recomputation, immutable attempt history, current-attempt API projection, and the frontend's three semantic states. Focused suites run first, followed by the full backend and frontend suites.
