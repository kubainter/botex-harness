# 🤖 Subagent Orchestration — Scaling Step Beyond

> **When subagents exist, use them for two things: parallel building and unbiased verification. When they don't, run solo — the pipeline is the same.**

---

## 1. Detect Capability (once per session)

```
Claude Code / Agent SDK   → Task/Agent tool, custom subagents
CrewAI / AutoGen / LangGraph → crew members, group chat agents, graph nodes
OpenAI Agents SDK         → handoffs, agent-as-tool
Custom ReAct loop         → spawn-with-fresh-context, if implemented
None of the above         → SOLO MODE: full pipeline yourself +
                            Fresh-Eyes Protocol (references/verification.md §3)
```

Subagents are an **accelerator, not a requirement**. Every Step Beyond behavior works solo.

---

## 2. The Roles

| Role | Gets | Does | Returns |
|------|------|------|---------|
| **ORCHESTRATOR** (you) | Request + attributable context | CONTEXT, INTENT, DECIDE, permission, assembly, DELIVER, LEARN | One user-facing result |
| **BUILDER** | Authorized intent + project constraints | BUILD and authorized EXECUTE work | Working deliverable |
| **SPECIALIST** | One authorized independent action | Execute only that action | Working component + evidence |
| **VERIFIER** | Deliverable + checklists **only** | Verify Loop (all four checks) | Pass / itemized failures |
| **CRITIC** (big deliverables only) | Deliverable + "find what a demanding reviewer would flag" | Hostile pass | Weakest 3 points |

### The Verifier Firewall (the rule that matters most)

```
VERIFIER receives:  the deliverable, the ORIGINAL user request,
                    the domain verify table, the slop index
VERIFIER never receives:  the builder's reasoning, your plan,
                          your justifications, your excitement
```

A verifier that reads the builder's reasoning inherits the builder's blind spots. Fresh context **is** the feature — that's what a solo agent can't fake and the single biggest reason to spawn one.

---

## 3. Orchestration Pattern

```
                    ┌── SPECIALIST: authorized action A ──┐
USER ──▶ CONTEXT    │                                     │
         INTENT ──▶ DECIDE ──▶ BUILDER ───────────────────┼──▶ VERIFIER
                    │                                     │    VERIFY ledger
                    └── SPECIALIST: authorized action B ──┘    DELIVER → LEARN
```

**Rules:**

1. **Parallelize only independent authorized actions.** A task that depends on the base stays sequential.
2. **The initiative budget is global.** The orchestrator owns mode thresholds, permission classes, cost, and risk across all agents; subagents cannot create extra scope.
3. **The orchestrator owns delivery and learning.** One voice to the user and one auditable learning update; subagents do not write the user model.
4. **Verify failures route back to the builder** (max 2 fix cycles per Verify Loop), then re-verify. The verifier never fixes — fixing makes it an author, and authors can't verify.
5. **Critic is for big deliverables only** — full sites, long documents, multi-file changes. For a single function it's ceremony.

---

## 4. When to Spawn vs. Go Solo

```
SPAWN when:
  multiple independent actions     → specialists can reduce wall-clock time
  high review cost                 → fresh-context verifier adds independent evidence
  separable research questions     → parallel searchers, orchestrator synthesizes

SOLO when:
  single-file / single-artifact task   → spawn overhead > benefit
  tight latency (user waiting on chat) → subagent startup costs seconds
  platform bills per-agent             → respect the $ ceiling
```

Spawning has real cost: startup latency, token duplication (each agent re-reads context), coordination overhead. **A subagent that saves no wall-clock time and adds no fresh perspective is slop in agent form.**

---

## 5. Prompt Templates

**SPECIALIST** (one per authorized action):
```
Build exactly this, nothing more: {authorized action, e.g. "contact page: form
(name/email/message), validation, success state"}.
Constraints from user profile: {stack, brand, language}.
Match the conventions of: {base deliverable location}.
Verify your piece works before returning. Return: files + 1-line status.
Do NOT add extras — scope and initiative are owned by the orchestrator.
```

**VERIFIER**:
```
You are reviewing a deliverable you did not build. Original request: "{verbatim
user request}". Deliverable: {location/content}.
Run: (1) base check per this table: {domain verify row},
(2) every addition works, (3) slop scan per this list: {domain slop section},
(4) flag any claim in this draft delivery message not backed by observation:
"{draft message}".
Return: PASS, or an itemized list: what fails, where, how observed.
Do not fix anything. Do not praise anything.
```

---

## 6. Failure Modes

| Failure | Symptom | Prevention |
|---------|---------|-----------|
| Scope growth by committee | Subagents each add "just one" extra | Actions are authorized and specified by the orchestrator only |
| Verifier capture | Verifier saw the plan, rubber-stamps it | The Firewall — deliverable + checklists only |
| Delegation theater | 4 agents, task was one file | The spawn/solo table above |
| Frankenstein assembly | Parallel pieces with different conventions | Every EXTENDER gets the same Profile + base conventions |
| Lost budget tracking | Cost and risk are scored independently by each agent | Initiative state lives in one orchestrator-owned record |
