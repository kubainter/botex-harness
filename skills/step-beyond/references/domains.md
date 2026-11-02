# 🔬 Domain Decision Trees (Token-Optimized)

> Match the request to a domain and use the tree as derived guidance. Each tree:
> **TRIGGER** → **CONTEXT** → **BUILD** → **INITIATIVE CANDIDATES** → **VERIFY**
> → **NEVER**. Candidates still require v4 permission classification and mode scoring.
>
> Confirmed user constraints filter these defaults. Project facts come from the
> live environment, not user memory. See `references/memory.md` and
> `references/environment-scan.md`.

---

## IMAGE

```
TRIGGER: image|photo|graphic|generate|render|visual|picture
CONTEXT: brand palette, constraints, platform, and observed accepted crops
BUILD: context, depth, light, brand colors — never a purposeless void
INITIATIVE CANDIDATES: alternate crop or required platform format
NEXT-SIGNAL CANDIDATES: Stories/Reels cut when the trajectory supports it
VERIFY: inspect output — hands, text-in-image, artifacts, ratio, requested elements
NEVER: void background, waxy skin, gibberish text, wrong finger count shipped
```

## WEB

```
TRIGGER: page|landing|website|site|build|create.*site
CONTEXT: stack, brand colors/fonts, hosting, and observed preferences
         — stack/hosting from environment scan if memory hasn't seen this repo yet
BUILD: responsive, real fonts, semantic HTML, no design slop
INITIATIVE CANDIDATES: contact, privacy, favicon, OG image, or 404 as request-specific candidates
NEXT-SIGNAL CANDIDATES: dark mode, mobile menu, or sitemap when evidence supports the need
VERIFY: open it, click every link, submit forms, 375px viewport, console clean
NEVER: single file, no meta, generic fonts, missing favicon, "#" links, lorem ipsum
```

## CONTENT

```
TRIGGER: post|content|carousel|write|article|newsletter|copy
CONTEXT: language, tone, constraints, platform, and observed accepted hook styles
BUILD: active voice, supported specifics, no filler, natural rhythm
INITIATIVE CANDIDATES: hook alternatives, CTA options, or a visual brief
NEXT-SIGNAL CANDIDATES: a next-post idea when a series is observed
VERIFY: slop scan (hard), every number sourced, read-aloud rhythm check
NEVER: "in today's", "let's dive", "game-changer", passive voice, em-dash chains
```

## CODE

```
TRIGGER: code|function|component|class|api|endpoint|script|program
CONTEXT: stack, conventions, test framework, and observed preferences
         — pull stack/conventions from environment scan when memory is silent
BUILD: types, error handling, relevant edge cases, input validation
INITIATIVE CANDIDATES: tests, documentation, or type exports when they advance this result
NEXT-SIGNAL CANDIDATES: integration example, Storybook story, or deployment config when justified
VERIFY: run it, run tests, feed it the edge case you claim to handle
NEVER: bare functions, no types, silent failures, hardcoded secrets, dead code
```

## RESEARCH

```
TRIGGER: research|find|analyze|investigate|competitor|market|look into
CONTEXT: industry, prior use of research, and preferred output format
BUILD: sources cited, supported quantitative data, no fabricated claims
INITIATIVE CANDIDATES: recommendations, competitive gaps, or priority matrix
NEXT-SIGNAL CANDIDATES: executive summary or pitch-ready slide when the audience needs it
VERIFY: every claim → a source you actually consulted; numbers cross-checked
NEVER: unsourced claims, vague insights, "more research needed"
```

## EMAIL

```
TRIGGER: email|mailing|outreach|newsletter|campaign|draft
CONTEXT: sender voice, audience, observed subject preferences, language
BUILD: personal tone, no spam triggers, clear identity
INITIATIVE CANDIDATES: subject variants, follow-up template, or preview text
NEXT-SIGNAL CANDIDATES: experiment plan or send-time analysis when evidence is available
VERIFY: render check, links resolve, variables filled, spam-trigger scan
NEVER: "hope this finds you well", all-caps subjects, spam words
```

## TECHNICAL

```
TRIGGER: cron|automation|script|workflow|deploy|pipeline|CI|CD
CONTEXT: infrastructure, alerting stack, and observed preferences
         — infra/config detected via environment scan (CI/deploy files) when silent
BUILD: errors, retries, validation, idempotency where required
INITIATIVE CANDIDATES: monitoring, logging, health check, or documentation
NEXT-SIGNAL CANDIDATES: alert webhook, dashboard, or runbook when justified
VERIFY: run once end-to-end; kill mid-run — does it recover? logs written?
NEVER: silent failures, no retry, hardcoded config, missing logs
```

## VIDEO

```
TRIGGER: video|reel|tiktok|shorts|clip|recording|edit
CONTEXT: platform, brand intro/outro, caption language, observed formats
BUILD: correct aspect, compression, clean audio
INITIATIVE CANDIDATES: thumbnail, captions, or chapters
NEXT-SIGNAL CANDIDATES: teaser cut or GIF preview when distribution calls for it
VERIFY: play it — first 3s, audio sync, captions match speech, file size sane
NEVER: wrong ratio, missing thumbnail, uncompressed output
```

## AUDIO

```
TRIGGER: audio|podcast|voice|TTS|text-to-speech|voiceover|narration
CONTEXT: preferred voice, language, distribution channel
BUILD: correct voice, language match, clean output
INITIATIVE CANDIDATES: transcript, show notes, or intro/outro markers
NEXT-SIGNAL CANDIDATES: audiogram when a social distribution need is observed
VERIFY: listen — pronunciation of names/brands, no clipping, correct language
NEVER: wrong language model, uncompressed for chat platforms
```

## DATA

```
TRIGGER: data|chart|graph|visualiz|dashboard|analyze.*data|plot
CONTEXT: BI tools, brand palette, audience, and export preferences
         — data source/schema from environment scan (files, pipeline config) when unknown
BUILD: labeled axes, correct scale, legend, accessible colors
INITIATIVE CANDIDATES: alternate chart, export, or summary statistics
NEXT-SIGNAL CANDIDATES: interactive dashboard or slide-ready version when justified
VERIFY: numbers sum/reconcile with source, scale honest, chart type fits claim
NEVER: misleading scale, missing labels, inaccessible palette, truncated y-axis
```

## SOCIAL

```
TRIGGER: social|IG|FB|Instagram|Facebook|Twitter|LinkedIn|TikTok|post on
CONTEXT: platforms, observed hashtag preferences, timing evidence, and constraints
BUILD: platform-correct ratio, alt text, readable text size
INITIATIVE CANDIDATES: caption, hashtags, posting-time analysis, or alt text
NEXT-SIGNAL CANDIDATES: Stories, Reels, or cross-platform variant when justified
VERIFY: preview at platform size — text readable on mobile, ratio exact
NEVER: wrong aspect ratio, missing alt text, illegible text
```

---

## No Match?

When no tree matches, use the canonical lifecycle directly. Build intent
hypotheses from attributable context, classify permission before scoring optional
candidates, and verify the result the way the user will exercise it. A new domain
tree requires repeated observed need; one unfamiliar request is not enough to
promote a guess into durable guidance.
