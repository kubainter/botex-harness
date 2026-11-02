# 🔎 Environment Scan — Ground Truth Before You Build

> **"Memory tells you what the user wants. The environment tells you what's actually true right now. Read both before you act."**

The v4 `CONTEXT` stage combines attributable user context with current project
facts. `references/memory.md` covers the stored side; this file covers the live
project. Neither requires the other. A new session can scan a repo without user
memory, and a memory-rich session still checks project facts because stored
context can become stale.

---

## 1. What to Scan (priority order, budget-capped)

Same "detect what's available, use the first useful thing" spirit as Memory's store detection — except here every layer is usually available at once, so read the ones relevant to the request, not all of them:

```
1. MANIFESTS/LOCKFILES   — package.json, pyproject.toml, Cargo.toml, go.mod,
                            Gemfile, requirements.txt, composer.json
                            → stack, versions, tooling already chosen
                            (test framework, linter, formatter, scripts)
2. VCS HISTORY           — git log --oneline -20, git log -- <path>, blame on
                            a file you're about to touch
                            → active areas, recent direction, why code looks
                            the way it does, who/what churns
3. DIRECTORY STRUCTURE   — top 2-3 levels, monorepo markers, where
                            tests/docs/config live
                            → where a new file belongs
4. NEIGHBORING CODE      — 2-3 files of the same kind you're about to write
                            → naming, formatting, error handling, import
                            order, test co-location — match it, don't
                            invent a new style
5. CONFIG/CI/LINT        — .eslintrc, .prettierrc, tsconfig.json,
                            .github/workflows, Makefile, pre-commit hooks
                            → the quality bar already enforced; match it,
                            don't fight it
6. PROJECT DOCS          — README.md, CLAUDE.md, AGENTS.md, CONTRIBUTING.md,
                            docs/
                            → conventions the project already wrote down,
                            install/run/test commands, explicit house rules
```

Read what the request touches. "Fix this typo" needs none of this. "Add an endpoint" needs 1, 4, and 5 at minimum.

---

## 2. How Findings Feed the Pipeline

| Finding | Feeds | Effect |
|---|---|---|
| Test framework installed, no tests directory yet | INITIATIVE | Candidate only; score value, permission, cost, and verifiability before acting |
| Manifest has a deploy/build script | INTENT | The definition of done may include the project's existing build path |
| Neighboring files use named exports, no default export | BUILD | Match the established baseline |
| CI runs `lint --max-warnings 0` | VERIFY | Lint must actually pass, not just "look clean" |
| README says "run `npm test` before committing" | VERIFY | Run that exact command — don't invent your own |
| git log shows a recent commit fixing a race condition in this file | INTENT | Treat the area as fragile when defining done and verification scope |
| User model says `stack: Next.js`, package.json says Astro | CONTEXT, then LEARN | Use current project evidence and record the correction only from an observable outcome |

This is what makes the CODE domain's `CONTEXT` line real instead of
aspirational. Domain guidance describes what to inspect; this file describes how
to obtain current, attributable project evidence.

---

## 3. Budget & NEVER

```
BUDGET:   a few targeted reads/greps for THIS request — not a full-repo crawl.
          Once per session; cache findings in working context. Refresh only
          if the request moves to an unrelated part of the repo.
READ-ONLY: a scan never writes, stages, or commits anything.

NEVER:
  - Open .env, secrets.*, credentials.*, *.pem, id_rsa, .aws/, or anything
    whose name signals sensitive material. Skip it, note "not inspected."
  - Treat scan output as permission to change things beyond the request. It
    informs INTENT and INITIATIVE; permission classification still runs first.
  - Rewrite existing conventions "while you're in there." Matching a
    convention is not an invitation to refactor it.
  - Write scan findings into the User Pattern File. That file is for
    learned preferences — re-derivable
    facts about the current repo state don't belong there; re-scan instead
    of caching them as if they were durable user memory.
  - Over-scan a trivial request. Same proportionality rule as VERIFY: effort
    matches the ask.
```

**Degrades honestly:** with no filesystem or project-read capability, `CONTEXT`
uses only the sources actually available and records the missing project source.
The implementation remains prompt-only and must not claim it inspected a repo.

---

## 4. Precedence — Environment vs. Everything Else

```
EXPLICIT INSTRUCTION  >  USER MEMORY  >  ENVIRONMENT (ground truth)  >  AGENT SELF-NOTES  >  DOMAIN DEFAULTS
```

Environment and memory answer different questions, so most of the time they don't conflict at all:

```
ENVIRONMENT = what IS true right now, re-derived every relevant session
MEMORY      = what the USER wants, learned and persisted over time
```

- A user-model record says `stack: Next.js`; this repo's `package.json` says Astro. Current project evidence controls the build, while any durable correction remains attributable and auditable through `LEARN`.
- Memory has a `Banned: web: +cookie-banner` entry; the environment happens to already contain a cookie-banner component from a previous contributor. The ban still holds — the environment doesn't get a vote on a user preference. Don't add a *new* one; matching an *existing* file the user didn't ask you to touch is a separate question from proactively adding more of it.
- The user model is silent on this domain — project context fills what it can, and domain guidance supplies candidates rather than assumed facts.
- User explicitly says "use Redux here" while every other file in the repo uses Zustand — explicit instruction wins, this once. Note the friction in the delivery (one line), don't silently "correct" their choice back to match the repo.

---

## 5. Where This Matters Most

Every domain benefits, but scanning is most load-bearing in **CODE**, **WEB**,
**DATA**, and **TECHNICAL**, where conventions and stack are directly checkable
project facts. See those trees' `CONTEXT` lines.

---

## 6. Why This Matters

| Without environment scan | With environment scan |
|---|---|
| Guesses a stack or convention | Reads the manifest/lockfile, knows for certain |
| Introduces a new naming or formatting style | Matches the file it's about to sit next to |
| Ships code that fails CI on the first push | Matches the lint/CI bar already in force |
| Memory says one stack, repo says another — ships wrong | Environment catches the drift, corrects it, flags Profile for update |
| "Onboard me to this codebase" → a generic paragraph | A tour backed by an actually-read README, tree, and git log |

The Memory Protocol makes the agent remember *the user*. This protocol makes it *look before it builds* — the two together are what turns "one step ahead" from a slogan into something the agent can actually back up.
