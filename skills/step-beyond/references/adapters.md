# Adapter Contract

Adapters map host capabilities without changing `SPEC.md` semantics.

The runtime capability surface is:

```text
read_context
read_project
execute_local
request_permission
publish_external
read_user_model
write_user_model
record_evidence
```

Detect capabilities once and cache the result. Missing capabilities return an
explicit degraded result. Local execution and external publication are separate;
publication requires a permission reference.

Host labels:

- `planned`: documentation only;
- `prompt-only`: portable instruction tested, no runtime enforcement;
- `runtime-backed`: adapter connected to the controller;
- `verified`: repeated adapter-specific behavioral tests passed.

Do not list a host as verified from package readiness, a documentation example,
or a single model run.

## When the host already has memory, verification, or skills

Do not stack two policy systems simply because both are available. Assign one
owner to each responsibility during capability detection and cache that decision
for the session.

| Responsibility | Owner | Host facility supplies |
|---|---|---|
| Behavioral semantics | `SPEC.md` | Instruction transport only |
| Current project facts | `read_project` / live files | Search, file reads, diagnostics |
| User-model record | One selected store | Persistence transport when its guarantees are known |
| Permission decision | Step Beyond controller | Host confirmation UI or permission token |
| Claim evidence | One verification ledger | Test, browser, diagnostic, or tool observation |
| Skill loading | Host skill mechanism | Discovery and progressive loading |

The invariant is **one owner per record**. A project rule is not copied into user
memory. A host memory item is not mirrored into the Step Beyond store unless an
explicit migration preserves source, scope, timestamps, sensitivity, and audit
history. When the host store cannot expose correction, deletion, provenance, or
sensitivity controls, use it only as prompt context and describe the mode
honestly; do not call it runtime-backed Step Beyond persistence.

Verification follows the same rule: keep **one verification record per material claim**.
If a host already ran a test or produced an editor diagnostic, import
that observation into the ledger with its scope. Do not rerun an identical check
to simulate independence. Run another check only when it covers a different
claim, environment, or failure mode. A fresh-context reviewer may review the
artifact, but its conclusions still map into the same claim ledger.

Host instructions, project rules, and installed skills can narrow behavior but
cannot silently change `SPEC.md` semantics. Resolve contradictions using the
host's normal instruction precedence, record the resulting degradation, and do
not claim conformance when a higher-priority instruction prevents it.

### Claude Code

At startup, detect the capabilities actually exposed in the current Claude Code
session; do not infer them from the host name alone.

- Treat `CLAUDE.md` and repository instructions as project or host instructions,
  not learned user preferences.
- If native or MCP memory is selected as the user-model store, require observable
  provenance, correction, deletion, and sensitivity behavior. Otherwise keep it
  as read-only context or use the Step Beyond store.
- Route shell results, test output, browser observations, and reviewer findings
  into the single verification ledger. If Claude Code already ran the relevant
  test, the Step Beyond `VERIFY` stage audits and scopes that evidence instead of
  running a duplicate test by default.
- A Task/subagent capability, when present, is execution transport. The parent
  controller retains permission, initiative state, delivery, and user-model
  ownership.

### Cursor

Detect the editor rules, diagnostics, terminal, and agent/tool surface available
in the current Cursor configuration; capabilities vary by version and workspace.

- Treat project rules and indexed code as instructions and project context, not
  durable evidence of a user preference.
- Editor diagnostics may become verification evidence only with file, scope, and
  observed status attached. A clean editor view does not prove runtime behavior.
- If Cursor or an extension exposes memory, select either that store or the Step
  Beyond store as the user-model owner. Do not dual-write.
- Cursor's agent or skill mechanism may load the portable instructions, but it
  does not make prompt-only permission or persistence guarantees enforceable.
- When a terminal test already covers a claim, the Step Beyond `VERIFY` stage
  records and audits it; it does not add a second ceremonial verification pass.

### Degradation examples

| Available host capability | Binding | Honest mode/result |
|---|---|---|
| Rules and chat context only | `read_context`; no enforced store or executor | `prompt-only` |
| Local execution, no auditable user store | project reads and execution; memory disabled or session-only | `runtime-backed` only for the connected policy operations, with persistence named as unavailable |
| Auditable store and executor, no publication operation | all local operations; publication unavailable | runtime-backed local behavior with explicit publication degradation |
| Repeated adapter-specific behavioral evidence | same bindings plus recorded benchmark scope | `verified` only for the measured adapter/host/model scope |
