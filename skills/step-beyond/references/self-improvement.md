# Auditable Learning

Keep four streams separate:

- user-specific preferences;
- global heuristic evaluation;
- verification-gap learning;
- slop-pattern learning.

Use observable outcomes only. `unknown` is neutral; silence is not automatic
failure. A user's preference never updates another user's profile or leaks into a
global record.

Global heuristic changes require multiple eligible events. Each revision records
the previous version, proposed version, reason events, timestamp, and status. It
must be auditable and reversible. New releases should keep global revisions
proposal-only until poisoning resistance and rollback behavior are measured.

Learning never changes permission classes, broadens strict scope, or converts an
unverified claim into evidence.
