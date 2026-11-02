# AI Slop Index

Slop is content that imitates a familiar output without carrying the information,
function, or intent that output needs. Use this reference during `VERIFY`. Match
the artifact to a domain, record each observed hit, repair the smallest affected
scope, and scan the repair once more.

A pattern is evidence only when the detection rule matches the artifact. Do not
reject a choice because it appears on this list if the brief, brand, codebase, or
data supports it.

## 1. Text

| Pattern | Detect | Repair |
|---|---|---|
| Stock opener | Search for phrases such as “in today's fast-paced world”, “let's dive in”, “it's important to note”, “look no further”, or “hope this email finds you well”. | Start with the first fact, decision, or request-specific sentence. |
| Empty business verb | Find `leverage`, `unlock`, `elevate`, `supercharge`, `revolutionize`, or `game-changer`; test whether the sentence names an action and result. | Name who does what and what changes. |
| Interchangeable copy | Replace the company or product name with a competitor. If the paragraph still fits, it lacks request-specific evidence. | Add real service, audience, constraint, example, or differentiator from the brief. |
| Repeated triplet | Find consecutive three-item slogans or lists whose members overlap in meaning. | Keep the strongest distinct items and make each concrete. |
| Metronomic paragraphs | Compare adjacent paragraph and sentence shapes. Flag repeated length and identical cadence that do not reflect the argument. | Merge, split, or vary structure according to the content. |
| Emphasis inflation | Count headings, bold spans, emoji, and all-caps text that compete for attention. Flag emphasis with no priority distinction. | Keep emphasis only on decisions, warnings, or scan anchors. |
| Decorative list | Read bullets as prose. If sequence and grouping add no meaning, the list is formatting noise. | Convert it to a direct sentence or group items by a real category. |
| Restated conclusion | Compare the last paragraph with the introduction. Flag claims that add no decision, evidence, or next action. | End on the last new piece of information. |
| Hedge stack | Find several uncertainty terms around one claim, such as `may`, `might`, `possibly`, and `arguably`. | State the supported scope or label the claim unverified. |
| Unsupported number | Trace each number to supplied data, calculation, or a consulted source. | Cite the evidence, show the calculation, or remove the number. |

## 2. Code

| Pattern | Detect | Repair |
|---|---|---|
| Narration comment | Compare each comment with the next statement. Flag comments that repeat the syntax without explaining a constraint. | Delete it or document why the code must behave that way. |
| Dead abstraction | Find interfaces, factories, providers, or wrappers with one caller and one implementation that add no policy boundary. | Inline the layer or name the extension requirement that justifies it. |
| Catch and ignore | Search empty catch blocks, swallowed rejections, and broad handlers that discard the original error. | Handle the expected condition or propagate an error with context. |
| Decorative typing | Search `any`, unchecked casts, or generic types that erase information at a boundary. | Use the domain type, validate unknown input, and narrow it. |
| Debug residue | Search console probes, temporary flags, commented-out code, unused imports, and scratch files. | Remove residue and keep reproducible diagnostics in tests or documented tooling. |
| TODO as completion | Search TODO, FIXME, placeholder throws, and stub returns in code described as finished. | Implement the required path or report the artifact as incomplete. |
| Copy-paste family | Compare sibling functions or branches that differ only by literals or one call. | Extract the shared behavior when it improves the existing codebase pattern. |
| Defensive theater | Trace guards against the actual type and call graph. Flag checks for states that cannot occur while real boundary validation is absent. | Validate real external boundaries and remove impossible-state noise. |
| Embedded secret | Scan committed text for credentials, private keys, tokens, and provider-specific secret formats. | Remove the secret, rotate it when exposed, and use the project's secret mechanism. |
| Framework cosplay | Compare infrastructure cost with the requested scale and existing stack. Flag a new framework that solves no demonstrated constraint. | Use the smallest existing project primitive that satisfies the requirement. |

## 3. Web / Design

| Pattern | Detect | Repair |
|---|---|---|
| Generic gradient hero | Compare the hero with the brief. Flag a stock gradient, centered slogan, and generic buttons with no brand or product evidence. | Build the hierarchy from the actual offer, brand system, and primary action. |
| Unmotivated glass effect | Find blur, transparency, and layered cards used across unrelated components without a hierarchy purpose. | Use solid surfaces or reserve the effect for a justified layer. |
| Typography default | Inspect font choice, scale, line length, and hierarchy. Flag a default font with indistinguishable levels. | Use the existing brand or project type system and define clear roles. |
| Emoji iconography | Find emoji used as product or business interface icons where the design system has another visual language. | Use accessible text, the existing icon set, or a purpose-built asset. |
| Repeated card reflex | Compare cards for identical icon-title-body structure and interchangeable content. | Choose a layout that matches the information relationships. |
| Fake social proof | Trace testimonials, logos, customer counts, awards, and ratings to supplied evidence. | Remove them or mark supplied placeholders so nobody can mistake them for claims. |
| Placeholder residue | Search lorem ipsum, “Your Company”, dummy contacts, broken images, empty hrefs, and `#` links. | Replace with supplied content, a labelled placeholder, or a complete state. |
| Dead navigation | Exercise every visible link and control. Flag targets that do not exist or controls with no response. | Build the target, remove the control, or label an unavailable action. |
| Contrast failure | Measure text and control contrast against the project's stated accessibility target. | Adjust foreground, background, weight, or size and remeasure. |
| Stock metaphor | Compare imagery with the product and audience. Flag generic handshakes, rockets, and lightbulbs that communicate no specific concept. | Use product evidence, real context, or a relevant illustration brief. |

## 4. Image

| Pattern | Detect | Repair |
|---|---|---|
| Context-free subject | Inspect whether the background, surface, light, and crop support the requested purpose. | Add a setting or composition tied to the brief. |
| Waxy skin | Inspect faces at useful resolution for uniform plastic texture and missing pores or material detail. | Regenerate or retouch with natural texture and restrained smoothing. |
| Hand anomaly | Count visible fingers and inspect joints, grip, occlusion, and object contact. | Regenerate or retouch the affected hand; crop only when the composition still works. |
| Broken embedded text | Read every sign, label, screen, and package mark at output size. | Replace text in a design tool, regenerate without text, or remove the element. |
| HDR oversaturation | Inspect clipped highlights, crushed shadows, halos, and uniform cyan-orange grading. | Reduce local contrast and saturation; restore a coherent light source. |
| Isometric default | Compare the chosen perspective with the brief. Flag isometric 3D used without a spatial or explanatory need. | Use the viewpoint that best shows the subject or process. |
| Corporate Memphis default | Flag flat generic characters with exaggerated limbs when the brief gives no reason for that style. | Apply the actual brand illustration system or a context-specific visual direction. |

## 5. Data / Chart

| Pattern | Detect | Repair |
|---|---|---|
| Misleading baseline | Inspect axis bounds and compare the visual difference with the numeric difference. | Use an honest baseline or annotate the break and reason. |
| Chart-type mismatch | Match the analytical question to the encoding. Flag pies with many categories, lines for unordered categories, or area used without meaningful totals. | Choose the chart that represents comparison, trend, distribution, or relationship directly. |
| Missing label | Check title, axes, units, legend, time range, source, and filter state. | Add every label required to interpret the value without guessing. |
| Rainbow palette | Compare color count with semantic groups. Flag unrelated colors that encode no meaning. | Use a restrained accessible palette with consistent semantic roles. |
| False precision | Compare displayed decimals with sample size, measurement resolution, and uncertainty. | Round to the precision supported by the source and state uncertainty when known. |
| Dashboard sprawl | Map every chart to a decision or monitoring question. Flag panels with no distinct question. | Keep the views that answer the user's decisions and move detail behind drill-down or an appendix. |
| Unsupported aggregation | Recalculate totals, rates, denominators, and filters from the source. | Correct the transformation and document the calculation. |

## 6. Scan procedure

1. Identify the artifact domains.
2. Apply each relevant detection rule to the actual artifact.
3. Record the pattern, location, and observation. A pattern name without an
   observation is not a finding.
4. Repair the smallest scope that removes the evidence while preserving the
   user's intent and project conventions.
5. Repeat the relevant checks once and attach the result to the verification
   ledger. Report any remaining scope as `partially_verified` or `unverified`.

Teams can add project-specific patterns when an observed miss supports them.
Store the evidence and scope with the detector; do not turn one person's taste
into a global rule.
