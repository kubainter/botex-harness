# Installation

## Prompt-only

Copy `skills/step-beyond/` into a host's skill directory or inject
`templates/core-injection.txt` as standing instructions. Label this mode
`prompt-only`: it guides reasoning but does not enforce persistence, permission,
shared state, or evidence.

## Runtime-backed

Requires Node.js 20 or newer:

```bash
npm install
npm run validate
```

Wire the host through `@step-beyond/adapter-reference`, then provide user-model
persistence with `@step-beyond/runtime-store`. See `docs/runtime.md` for concrete
examples.

An installation is runtime-backed only after capability detection, local policy
tests, and storage reload checks pass. It is verified only after repeated
adapter-specific behavioral evals. Re-running setup must update in place and must
not overwrite user memory or audit history.
