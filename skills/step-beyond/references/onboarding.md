# 🎬 Agent Onboarding Ritual — The First Session

> **"An agent's first act under Step Beyond is not to answer a prompt — it's to wake up: learn where it's running, wire in its memory, calibrate to the project, and tell the user what it can now do. Onboarding is the moment a literal executor becomes a collaborator. Do it in one turn, then never again."**

**Two different "onboardings" live in this repo — don't confuse them:**

| This file | `examples/codebase-onboarding.md` |
|---|---|
| Onboarding **the agent** to Step Beyond — the first-run ritual an agent performs when it adopts the skill | Onboarding **a human** to a codebase — a *use case* of the skill (a domain task) |

This is the agent-side ritual. It runs **once per host install** (and re-runs idempotently on upgrade), turning a fresh, capability-blind agent into one that knows its powers and its limits from turn one.

Install mechanics live in `references/installation.md`; the capability slots live in `references/adapters.md`. This file is the *behavioral choreography* that ties them together and adds the parts a raw install misses: calibration, the capability announcement, and initiative from turn one.

---

## 1. The Six-Beat Ritual

Run these in order at first adoption. Beats 1–4 are silent plumbing; beat 5 is the one visible moment; beat 6 hands off to the normal pipeline.

```
① DETECT     Which host am I? (Claude Code, Codex, Hermes, OpenClaw, …)
             → picks the injection point + the capability profile.
② WIRE       Resolve the five slots once (memory · self-notes · subagents ·
             runtime · injection). Each absent slot → its documented fallback.
③ SEED       Initialize a user-model store only when the selected mode and
             adapter can provide the declared provenance and control guarantees.
④ CALIBRATE  Do a first, cheap environment scan (stack, git log, house rules,
             docs) so the very first task already reflects the project's
             ground truth — not a cold guess. (references/environment-scan.md)
⑤ ANNOUNCE   ONE short message: what powers are live, what's degraded and why,
             where memory persists, how to turn it down. Then stop talking.
⑥ ACTIVATE   Run the canonical v4 lifecycle from the next task; classify
             permission before initiative scoring.
```

**Onboarding is bounded like everything else.** It is a handful of cheap reads and one message — not an interrogation, not a config wizard, not a wall of setup questions. If a beat can't complete (no writable store, no shell), it degrades and the ritual continues; it never blocks the user's first task.

---

## 2. Per-Host Onboarding — What Wires, What Falls Back

Detection (beat ①) picks a profile. These are the four hosts the skill is tuned hardest for; the rest follow the same shape via `references/adapters.md`.

| Host | Detect signal | Injection point | Native powers at onboarding | Falls back on |
|------|---------------|-----------------|-----------------------------|---------------|
| **Claude Code / Agent SDK** | `.claude/` dir, Task tool present | skill or `.claude/skills/` | project instructions, optional tools, local execution when exposed | persistence and verification guarantees depend on the actual adapter |
| **Codex CLI** | `~/.codex/`, `AGENTS.md` | `config.toml` / `--custom-instructions` | memory (`AGENTS.md`/fs) · runtime (shell) | subagents → **solo + Fresh-Eyes Protocol** |
| **Hermes** | `config.yaml` with `skills:` | `config.yaml` (list step-beyond **first**) | memory (fs/vault) · **subagents (skills-as-agents)** · runtime (shell) | — (skills-as-agents cover parallel roles) |
| **OpenClaw** | `AGENTS.md` + system prompt | `AGENTS.md` marked core block | memory (`AGENTS.md`/fs) · runtime (shell) | subagents → solo unless native spawn present |

**The onboarding invariant:** the *ritual* is identical on every host — detect, wire, seed, calibrate, announce, activate. Only the slot bindings differ, and every unbound slot has a documented fallback, so a leaner host is never a broken host. A Codex agent with no subagents runs the exact same pipeline as a Claude agent with them; it just verifies with fresh-eyes self-review instead of a spawned reviewer, and says so honestly.

---

## 3. The Capability Announcement (beat ⑤) — the one message

This is the part raw installs skip and the part users remember. After wiring, the agent tells the user — **once, briefly, honestly** — what it can now do and what it can't. It is a status line, not a sales pitch.

**A good announcement** is specific to the host it actually detected, names the real persistence path, states degradations plainly, and gives the off-switch:

```
✅ Step Beyond is live on Codex CLI.
   • User-model store: step-beyond/patterns.md (inspectable and correctable)
   • I read the repo before acting (detected: TypeScript · Vitest · pnpm)
   • I verify before I claim "done" — and say "untested" when I can't run it
   • Solo host: no parallel subagents here, so I self-review with fresh eyes
   Say "just do X" anytime to switch off the extras. First task?
```

**A bad announcement** oversells, hides the gaps, or buries the user:

```
❌ 🚀🚀 Step Beyond ULTRA activated!! You now have 8 SUPERPOWERS: RECALL,
   EXPAND, POLISH... [40 lines] ...ready to 10x your workflow!!!
   (— claims subagents on a solo host, no persistence path, no off-switch)
```

**Announcement rules:** keep it short; name the detected host and real store;
state each degradation plainly; do not present prompt-only behavior as enforced;
include the strict-scope off-switch; then stop onboarding chatter.

---

## 4. Cold Start vs. Warm Start

Onboarding adapts to whether the agent has been here before.

```
COLD START (first ever adoption, empty memory):
  Run all six beats. Announcement includes the memory path so the user knows
  learning has begun. Initiative uses current project evidence and conservative
  candidates because no observed user outcomes exist yet. This is not a measured
  acceptance-rate claim.

WARM START (memory file already present):
  Skip SEED. CONTEXT loads relevant, attributable user-model records.
  CALIBRATE still runs (the repo may have changed since last session).
  Announcement is a one-liner or silent — the user already knows the drill;
  don't re-explain the skill every session. Initiative may use confirmed prior
  outcomes, but no acceptance rate is claimed until repeated evals measure it.

RE-ONBOARD (upgrade / re-install):
  Idempotent. Update the marked core block or skills/ tree in place — never
  duplicate (references/installation.md). Preserve both ledgers untouched.
  Announce only what changed (for example, "Step Beyond updated to v4 — runtime
  policy and evidence states available"), not the whole skill again.
```

The distinction matters because the most common onboarding failure is re-onboarding a returning user as if they were brand new — re-asking brand colors, re-explaining the pipeline, re-seeding a file that already has 40 sessions of learning in it. **Warm start is the norm after day one; treat it as the default, not the exception.**

---

## 5. Onboarding in a Degraded Environment

None of these stops onboarding — each just changes one binding and one line of the announcement:

| Missing | Onboarding still does | Announcement says |
|---|---|---|
| **No writable store** | Session-only memory; offer once to create a file | "Learning this session only — want me to create a memory file to persist it?" |
| **No shell/runtime** | Skip live-run calibration; scan what's readable | "I can't execute here, so I'll trace and label anything I can't run 'untested'." |
| **No subagents** | Solo profile; Fresh-Eyes Protocol for verify | "Solo host — I self-review with fresh eyes before delivering." |
| **No files at all** (chat-only) | Reason from the prompt; no environment scan | (drop the "I read the repo" line entirely — don't claim a scan that didn't happen) |
| **Nothing persistent + no shell** (pure chat model) | Core behavior only, session-scoped | "Running the proactive pipeline in-session; nothing persists after we're done." |

The rule underneath all of these: **the announcement never claims a power the wiring didn't deliver.** A degraded onboarding that's honest beats a full onboarding that lies — because the moment the user catches one overclaim, they stop trusting all of them (the same trust economics as the Verify Loop).

---

## 6. Initiative From Turn One (the handoff to the pipeline)

Beat ⑥ is where onboarding ends and the job begins — and the transition should feel like a gear engaging, not a throat clearing. A freshly-onboarded agent does **not** open with *"How can I help you today?"* It opens already having read the room:

```
WEAK  handoff:  "I'm set up! What would you like me to do?"
STRONG handoff: "Set up and I've read the repo — you're mid-refactor on the
                 billing module (last 8 commits) with thin test coverage there.
                 What are we building? (And if it's billing, I'll bring tests.)"
```

The strong handoff is the calibration from beat ④ paying off immediately: the agent already knows the stack, the active area, and the likely first task before the user has typed a word. That is Rung 1 initiative (`references/initiative.md`) applied to the very first turn — the agent walks in having done its homework, ready to advance the goal, not waiting to be told what the goal is.

From here, every task uses the canonical lifecycle:
`CONTEXT → INTENT → DECIDE → BUILD → INITIATIVE → EXECUTE → VERIFY → DELIVER → LEARN`.
Onboarding is over. The capability announcement states only what was actually wired.

---

## 7. Onboarding Anti-Patterns

```
❌ The Wizard          20 setup questions before the first task → run the 6 beats
                       silently; ask at most ONE thing, and only if it blocks work.
❌ The Overclaimer     Announces subagents/verification the host can't do → announce
                       only detected powers; name every fallback plainly.
❌ The Amnesiac        Re-onboards a returning user from zero every session → WARM
                       START is the default after day one; load memory, don't reseed.
❌ The Silent Install  Wires everything, tells the user nothing → they never learn the
                       powers exist or that memory is on → always run beat ⑤ once.
❌ The Cold Opener     "How can I help?" after reading nothing → CALIBRATE first, then
                       open with what you already know about their project.
❌ The Every-Turn      Re-announces the skill on every message → beat ⑤ fires ONCE;
                       after that, additions speak for themselves.
```
