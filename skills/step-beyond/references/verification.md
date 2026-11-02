# Verification Ledger

Every material delivery claim maps to an actual check and evidence scope.

Record the claim, method, executed check, evidence, result, verified scope,
unchecked elements, environment blockers, and status:

- `verified`: stated scope directly checked and passed;
- `partially_verified`: useful checks passed, but named scope remains unchecked;
- `unverified`: no meaningful execution evidence supports the claim.

Do not delete useful work only because an external check is unavailable. Remove
or narrow the unsupported claim. A passing static check does not prove runtime
behavior; a passing local integration does not prove an unavailable external
service.

Verification effort should match consequence. Publication, security, migrations,
and destructive operations require stronger evidence and permission than a local
draft. Claim audit applies to the delivery message itself.
