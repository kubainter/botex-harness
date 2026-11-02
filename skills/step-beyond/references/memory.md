# Memory Protocol

`SPEC.md` is normative. This reference explains how to apply its user-model rules.

## Record kinds

Keep `fact`, `preference`, `constraint`, `observation`, `hypothesis`,
`trajectory`, `open_loop`, and `negative_feedback` distinct. Each entry records
source, source reference, timestamps, scope, confidence, confirmations, last use,
revalidation date, explicit/inferred origin, sensitivity, and status.

An inferred hypothesis never becomes a fact automatically. An observation does
not become a preference without supporting feedback. Current repository state is
project context, not user memory.

## Outcomes

Use only observable outcomes: `explicit_accept`, `explicit_reject`, `adopted`,
`reverted`, `corrected`, `ignored`, and `unknown`. Silence or unavailable usage
data is `unknown` and changes no score.

## Safety and control

- reject secrets and unnecessary personal data before persistence;
- isolate user namespaces from project facts and global heuristics;
- expose inspection, correction, deletion, audit, and rollback;
- prefer minimal, scoped entries with revalidation dates;
- never claim durability unless a write and reload were observed.

Prompt-only hosts may track context in-session but must label it non-durable. The
reference implementation is documented in `docs/runtime.md`.
