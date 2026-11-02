# Adaptive Initiative

Initiative advances the user's actual goal without broadening authority.
Permission classification always runs first.

For each optional candidate, identify expected value, confidence, reversibility,
verifiability, historical accuracy, cost, risk, and a request-specific rationale.

```text
score = value * confidence * reversibility * verifiability
        * historical_accuracy / max(cost + risk, epsilon)
```

`fast`, `standard`, and `exploratory` modes adjust the threshold. `strict` mode
disables every optional action and unsolicited proposal. No score can bypass an
`ASK` or `FORBIDDEN` decision.

Good initiative closes a concrete gap or likely failure mode. Generic additions
that could be attached to any task are omitted. Large, public, expensive,
irreversible, credential-related, or security-sensitive moves require permission
even when valuable.
