# Spec: Tier-ROI Model Selector

**Version:** 1.0
**Date:** 2026-06-07
Status: Draft; current Phase 2.5 scope is a manually invoked one-shot command.
Priority: Phase 2.5, after configurable routing (implemented) and real cloud
tiers are operational.
Depends on: versioned quality scores, a price snapshot schema validated against
the provider's documented API, and decisions in section 16.

-----

## 1. Purpose

A manually invoked command that **deterministically** selects the highest-ROI
OpenRouter model for configured cloud routing tiers and publishes a routing
table the runtime can consume. Same inputs produce identical output, with an
auditable explanation for every selection. Nightly automation is a later
promotion step after the one-shot command has operated reliably for several
weeks.

“ROI” here means **quality per effective dollar** within a tier’s quality band. The band guarantees the quality floor; the job maximizes value inside it. See §5 for the exact metric and default policy.

-----

## 2. Goals and non-goals

**Goals**

- Pick exactly one model per tier, deterministically, from a captured OpenRouter snapshot.
- Never emit an empty or broken tier (always fall back to the prior selection).
- Avoid day-to-day churn (“flapping”) via incumbency hysteresis.
- Produce a complete audit trail: what was chosen, what lost, and why it did or didn’t change.
- Be safe to re-run (idempotent) and safe to read mid-write (atomic publish).

**Non-goals**

- Live, per-request routing. This job sets *defaults*; runtime routing/cascading is a separate component.
- LLM-in-the-loop judgement. Selection is pure arithmetic over a pinned snapshot.
- Replacing Glassrail's evals. The quality score is an external input, not something this job measures.

### Current integration constraint

Glassrail exposes runtime tiers `0..3`, with local-first tiers `0–1` and
cloud-oriented tiers `2–3`. This command selects only tiers `2–3`; tiers `0–1`
remain outside its routing-table output. Section 6 and the sample configuration
use that same scope.

-----

## 3. Definitions

|Term                      |Meaning                                                                                                                                                                         |
|--------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------|
|**Candidate**             |A `(model_id, provider_id, mode)` row. A model with reasoning/non-reasoning or effort variants contributes one candidate per variant, each with its own quality score and price.|
|**Tier**                  |A contiguous, non-overlapping quality-index band (§6). Every eligible candidate falls in at most one selected tier.                                                            |
|**Quality (Q)**           |A numeric intelligence/capability score for a candidate (default source: Artificial Analysis Intelligence Index — see §4.2 caveat).                                             |
|**Effective cost (C_eff)**|Blended $/1M tokens including the credit-purchase fee (§5.2).                                                                                                                   |
|**ROI score**             |`Q^α / C_eff^β` (§5.1).                                                                                                                                                         |
|**Incumbent**             |The model currently selected for a tier (from the prior published routing table).                                                                                               |
|**Snapshot**              |The frozen, hashed input dataset a run scores against.                                                                                                                          |

-----

## 4. Inputs and data sources

The job reads four inputs. Fast-moving data (prices) is fetched nightly; slow-moving data (quality) is pinned and updated on a separate cadence. This separation is deliberate — it improves both determinism and honesty about data velocity.

### 4.1 Price + metadata — OpenRouter Models API (fetched nightly)

Authoritative for everything that changes daily:

- `model_id`, `provider_id`, list `price_in` / `price_out` per 1M tokens
- `context_length`, `max_output`
- `modalities` (text / image / video / audio / pdf)
- `status` (current / preview / deprecated)
- `provider_count` (for resilience filtering)
- promo end-date / list-vs-promo price, **if exposed** (see §7 price-basis rule)

> Map the provider response into the snapshot schema during implementation and
> record the response-schema version used for each snapshot. The selector must
> not assume undocumented field names or pricing semantics.

### 4.2 Quality scores — pinned table (updated weekly–biweekly)

A version-controlled file `quality_scores.yaml` mapping `(model_id, mode) → index`. Rationale and an honest caveat:

- Capability indices move far slower than prices, so a pinned table is both sufficient and more deterministic than re-pulling nightly.
- Treat the source as pluggable. An automated source may be used only after its access method and license are established; otherwise, maintain `quality_scores.yaml` on a documented cadence.
- Any candidate with a missing/null score is **ineligible** (§7), never silently scored as zero.

### 4.3 Prior routing table (read for hysteresis)

The last published `routing_table.json`. Provides the incumbent per tier so the job can apply hysteresis (§8.3) and fall back on empty pools (§9).

### 4.4 Configuration (frozen, hashed per run)

`config.yaml` (schema in §12). Versioned; the run records its `config_hash`.

-----

## 5. The ROI metric

### 5.1 Score

For each candidate:

```
ROI = round( (Q ** alpha) / (C_eff ** beta), 6 )
```

- `Q` — quality index (§4.2)
- `C_eff` — effective blended cost (§5.2)
- `alpha`, `beta` — tunable exponents, **default `alpha = 1`, `beta = 1`** → pure quality-per-effective-dollar.

**Default policy:** With `alpha = beta = 1`, the metric is cost-leaning. Inside a tier band it will often pick the least-expensive in-band candidate, because the band already enforces the quality floor. This is the intended “highest ROI = best value above a quality bar” behavior.

- To bias toward the **top** of each band (quality matters more than price), raise `alpha` or lower `beta`.
- Pure **max-quality regardless of cost** is not ROI: set `beta = 0` or sort by `Q`. Treat it as a separate configured objective.

`Q` is treated as if linear in value, which it is not strictly (the index is ordinal-ish). For a transparent, deterministic heuristic this is acceptable; the exponents are the lever if the default ranking does not fit the deployment workload.

### 5.2 Effective blended cost

```
blended = w_in * price_in + w_out * price_out
C_eff   = round( blended * (1 + credit_fee), 6 )
```

- `w_in`, `w_out` — workload blend weights. Set these from measured deployment telemetry, because ROI is only meaningful relative to the workload's input:output shape. Use the configured fallback when measurement is unavailable. Weights may be set globally and **overridden per tier** (§12) — e.g. classification tiers are output-light, coding tiers are output-heavy.
- `credit_fee` — a configured multiplier for billing overhead. Set it from the billing arrangement in effect for the recorded snapshot; do not encode a provider fee as a timeless default.

Use **list price by default**, not promotional price — see the price-basis rule in §7.

-----

## 6. Tier definitions

Contiguous, non-overlapping index bands across the command's selected cloud tiers. **All bounds are configurable.** Tiers `0–1` are intentionally absent because this command does not select their local-first routing entries.

|Tier|Index band `[lo, hi)`|Role of the cloud selection                                  |
|----|---------------------|--------------------------------------------------------------|
|2   |`[47, 53)`           |General cloud reasoning, synthesis, and implementation work  |
|3   |`[53, ∞)`            |Higher-capability cloud reasoning and multimodal work         |

Notes:

- Bands are left-closed, right-open. A score of exactly `47` is Tier 2.
- Because each `(model, mode)` is a separate candidate, one model can legitimately serve more than one selected tier through different modes.

-----

## 7. Eligibility filters

A candidate is **eligible for a tier** only if it passes **all** of the following, evaluated against the pinned snapshot. Filtering happens *before* scoring.

1. **Quality present** — `Q` is a finite numeric value (else excluded and logged).
1. **In band** — `tier.lo <= Q < tier.hi`.
1. **Status** — `status == "current"`. Exclude `deprecated` always; exclude `preview` unless `allow_preview: true`.
1. **Modality** — candidate’s `modalities ⊇ tier.required_modalities` (e.g. a vision tier excludes text-only models).
1. **Context** — `context_length >= tier.min_context`.
1. **Price ceiling** — `C_eff <= tier.max_cost_eff` (guards against a data error or promo-expiry selecting something wildly expensive).
1. **Resilience** — `provider_count >= global.min_providers` (avoids single-provider single points of failure).
1. **License** — if `tier.require_open_weights: true`, candidate must be open-weight (useful for future self-hosting parity).
1. **Provider policy** — `provider_id` is in `global.provider_allowlist` (if set) and not in `global.provider_blocklist`. Use this for data-residency / vendor-policy constraints.
1. **Price-basis rule (promo handling)** — price for scoring is the **list price**, *unless* a promo is active **and** its `promo_end` is after the **next** scheduled run. Rationale: never select a model on a promo price that will lapse before the next recompute, because the router would then run on an unexpectedly higher rate between runs. Deterministic given the snapshot + the known next-run timestamp.

-----

## 8. Selection algorithm (deterministic)

Reference implementation. This is the normative definition of the behavior; an implementation in any language must produce identical selections.

```python
from dataclasses import dataclass
from math import isfinite

@dataclass(frozen=True)
class Candidate:
    model_id: str
    provider_id: str
    mode: str
    quality: float | None   # Q; null means the snapshot has no usable score
    price_in: float         # $/1M, scoring basis (post price-basis rule)
    price_out: float        # $/1M, scoring basis
    context_length: int
    modalities: frozenset
    status: str
    provider_count: int
    open_weights: bool

@dataclass(frozen=True)
class ScoredCandidate:
    candidate: Candidate
    quality: float
    cost_eff: float
    roi: float

# ---- 8.1 cost + score (rounded for float-stable comparisons) ----

def effective_cost(price_in, price_out, w_in, w_out, fee):
    blended = w_in * price_in + w_out * price_out
    return round(blended * (1.0 + fee), 6)

def roi_score(quality, cost_eff, alpha, beta):
    assert cost_eff > 0, "cost_eff must be > 0"
    return round((quality ** alpha) / (cost_eff ** beta), 6)

# ---- 8.2 eligibility (see §7) ----

def base_eligible(c, tier, g):
    quality = c.quality
    return (
        quality is not None
        and isfinite(quality)
        and tier.lo <= quality < tier.hi
        and (c.status == "current" or (g.allow_preview and c.status == "preview"))
        and c.modalities >= tier.required_modalities
        and c.context_length >= tier.min_context
        and c.provider_count >= g.min_providers
        and (not tier.require_open_weights or c.open_weights)
        and (not g.provider_allowlist or c.provider_id in g.provider_allowlist)
        and c.provider_id not in g.provider_blocklist
        # Price-basis (promo) rule is applied upstream when constructing price_in/out.
    )

def score(c, tier, g):
    # Call only after base_eligible; the assertion narrows the nullable input.
    assert c.quality is not None and isfinite(c.quality)
    w_in, w_out = tier.blend or g.blend
    cost_eff = effective_cost(c.price_in, c.price_out, w_in, w_out, g.credit_fee)
    return ScoredCandidate(
        candidate=c,
        quality=c.quality,
        cost_eff=cost_eff,
        roi=roi_score(c.quality, cost_eff, g.alpha, g.beta),
    )

# ---- 8.3 per-tier selection with deterministic tiebreak + hysteresis ----

def select_for_tier(candidates, tier, g, prior, next_run_ts):
    pool = [score(c, tier, g) for c in candidates if base_eligible(c, tier, g)]
    pool = [c for c in pool if c.cost_eff <= tier.max_cost_eff]

    if not pool:
        return prior, "kept_prior__empty_pool"   # never emit an empty tier (§9)

    # TOTAL ORDER → unique winner, fully reproducible:
    #   1) higher ROI   2) higher quality   3) lower cost
    #   4) model_id asc 5) provider_id asc  6) mode asc
    pool.sort(key=lambda c: (-c.roi, -c.quality, c.cost_eff,
                             c.candidate.model_id, c.candidate.provider_id,
                             c.candidate.mode))
    challenger = pool[0]

    # hysteresis: incumbent stays unless beaten by a margin (§8.4)
    if prior is not None:
        incumbent = next((c for c in pool
                          if c.candidate.model_id == prior.model_id
                          and c.candidate.provider_id == prior.provider_id
                          and c.candidate.mode == prior.mode), None)
        if incumbent is not None and challenger is not incumbent:
            if challenger.roi < incumbent.roi * (1.0 + g.hysteresis):
                return incumbent, "kept_incumbent__within_hysteresis"

    return challenger, ("unchanged" if (prior and challenger.candidate.model_id == prior.model_id
                                        and challenger.candidate.provider_id == prior.provider_id
                                        and challenger.candidate.mode == prior.mode)
                        else "selected_challenger")
```

### 8.4 Hysteresis (anti-flapping)

A pure nightly recompute flaps: a one-cent price move or a one-point index revision flips the winner, churning routing and defeating prompt-cache locality. The incumbent therefore **stays selected unless a challenger’s ROI exceeds the incumbent’s by at least `hysteresis`** (default **`0.05` = 5%**). Still fully deterministic — it depends only on the persisted prior selection, the new snapshot, and the threshold.

Optional add-ons (off by default): a per-night **change budget** (max K tiers may change), and a per-tier **cooldown** (a tier may not change more than once per N days).

-----

## 9. Guardrails and sanity checks

Applied around the deterministic core. Any tripped guard keeps the prior selection for that tier and raises an alert (§11) — the job degrades safely, it never publishes a broken tier.

- **Empty pool** → keep incumbent, alert. (Handled in §8.3.)
- **Missing incumbent on empty pool** (first run, or incumbent now ineligible and no candidates) → leave tier `null`, alert at error level, and the router falls back to the next tier down per its own policy.
- **Price anomaly** → if a *selected* model’s `C_eff` moved more than `price_spike_pct` (default 50%) vs the previous snapshot, keep incumbent and flag for human review (likely a promo expiry or a data error).
- **Pool collapse** → if a tier’s eligible pool size drops below `min_pool_warn` (default 2), warn (selection still proceeds).
- **Config invalid** → fail the whole run before any write; alert; leave the live routing table untouched.

**Optional liveness gate (off by default):** after deterministic selection, fire one cheap smoke request at each newly-selected model; if it errors or times out, **veto the promotion** and keep the incumbent. Note the trade-off: a live smoke test introduces a liveness dependency, so the *promotion* is no longer reproducible from data alone. Keep the smoke result in the snapshot/log so a run remains explainable. Recommended only if provider flakiness has bitten you.

-----

## 10. Determinism guarantees

The job MUST satisfy these invariants; each has an enforcement mechanism:

|Invariant                                         |Enforcement                                                                                                                                                   |
|--------------------------------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------|
|Same snapshot + same config → identical selections|Fetch first, persist + hash the snapshot, **score only from the snapshot** — no network calls inside scoring.                                                 |
|No float-comparison nondeterminism                |All money and ROI values `round(..., 6)` before any comparison or sort.                                                                                       |
|Unique winner, never order-dependent              |Total-order sort key ending in `(model_id, provider_id, mode)`, which is globally unique. Stable sort.                                                        |
|Reproducible from the record                      |Every run logs `snapshot_hash` + `config_hash`; snapshots retained `snapshot_retention_days` (default 30).                                                    |
|Hysteresis is reproducible                        |Reads the persisted prior routing table; no hidden state.                                                                                                     |
|Re-running is safe                                |Idempotent: a re-run on the same snapshot/config yields byte-identical outputs (modulo the `generated_at` timestamp, which is excluded from the content hash).|

-----

## 11. Outputs

### 11.1 `routing_table.json` (consumed by the router)

Published atomically (write to a temp file, then `rename`) so the router never reads a partial file.

```json
{
  "schema_version": 1,
  "generated_at": "2026-06-07T03:17:04Z",
  "snapshot_hash": "sha256:…",
  "config_hash": "sha256:…",
  "tiers": {
    "2": {
      "model_id": "example/model-a",
      "provider_id": "example-provider",
      "mode": "standard",
      "price_in": 0.10,
      "price_out": 0.20,
      "cost_eff": 0.13,
      "quality": 47,
      "roi": 361.54,
      "context_length": 262144,
      "decision": "selected_challenger",
      "runner_up": { "model_id": "example/model-b", "roi": 265.44 }
    }
  }
}
```

### 11.2 `selection_log.jsonl` (append-only audit)

One line per `(run, tier)`: timestamp, tier, winner, runner-up, both ROI scores, `decision` reason (`selected_challenger` / `unchanged` / `kept_incumbent__within_hysteresis` / `kept_prior__empty_pool` / guard trips), `snapshot_hash`, `config_hash`. This record makes a later tier-change investigation reproducible.

### 11.3 `snapshot_<date>.json`

The raw fetched + pinned inputs for the run, hashed, retained per `snapshot_retention_days`. Enables exact replay.

### 11.4 Alerts

Routed to Slack / email / PagerDuty by severity:

- **info** — a tier changed (include old → new, both ROI, decision reason).
- **warn** — pool collapse, smoke-test veto, crypto/BYOK fee mismatch.
- **error** — fetch failure, empty tier with no incumbent, invalid config, price anomaly on a selected model.

-----

## 12. Configuration schema (`config.yaml`)

Worked example using reference anchors. Everything here is hashed into `config_hash`.

```yaml
version: 1

# ROI metric (§5)
alpha: 1.0          # quality exponent
beta:  1.0          # cost exponent (0 => max-quality, ignores price)
credit_fee: 0.0     # Set from the billing arrangement recorded for the snapshot.

blend:              # global default workload blend (set from deployment telemetry)
  w_in: 3.0
  w_out: 1.0

# anti-flapping (§8.4)
hysteresis: 0.05            # challenger must beat incumbent ROI by >=5%
change_budget_per_run: null # e.g. 2 to cap nightly changes; null = unlimited
tier_cooldown_days: 0

# guardrails (§9)
price_spike_pct: 0.50
min_pool_warn: 2
snapshot_retention_days: 30
allow_preview: false
enable_smoke_test: false

# resilience / policy (§7)
min_providers: 2
provider_allowlist: []      # empty = allow all
provider_blocklist: []

# tiers (§6) — bounds, modality, context, ceilings; blend overridable per tier
tiers:
  "2":
    lo: 47
    hi: 53
    min_context: 256000
    required_modalities: ["text"]
    max_cost_eff: 3.00
    require_open_weights: false
  "3":
    lo: 53
    hi: 100000       # Open-ended top selected band.
    min_context: 256000
    required_modalities: ["text"]
    max_cost_eff: 60.00
    blend: { w_in: 2.0, w_out: 1.0 }   # output-heavier (coding/agentic)
```

To add a **multimodal** variant of any tier, clone it with `required_modalities: ["text","image"]` (and a separate routing-table key) so the job picks the best vision-capable model independently of the text-only pick.

-----

## 13. One-shot operation and future scheduling

- **Phase 2.5:** run manually; do not install a scheduler.
- **Future Phase 4 cron:** `17 3 * * *` (nightly, 03:17 UTC), only after several
  weeks of inspected one-shot runs. Schedule after a documented provider price
  update window when one is observed.
- **Single-flight lock:** acquire a lock (file/Redis) on start; abort if held — never run two recomputes concurrently.
- **Fetch resilience:** per-source timeout (default 30s), 3 retries with exponential backoff; on persistent failure, **do not publish** — keep the live table, exit non-zero, alert error.
- **Atomic publish:** temp-write + `rename` for `routing_table.json` (§11.1).
- **Idempotent:** re-running on the same snapshot/config is a no-op on content.
- **Exit codes:** `0` success (changed or unchanged); `2` published with warnings; `3` no publish / kept prior due to failure; `4` invalid config. These can feed a deployment health monitor.
- **Manual replay:** a `--snapshot <path>` flag scores an old snapshot for audit/debugging without fetching.

-----

## 14. Failure modes

|Condition                                     |Behavior                                             |Alert|
|----------------------------------------------|-----------------------------------------------------|-----|
|Price/meta fetch fails                        |No publish; keep live table                          |error|
|Quality source unavailable                    |No publish (can’t score); keep live table            |error|
|Partial data (some candidates missing fields) |Drop incomplete candidates; proceed if pool non-empty|warn |
|Tier pool empty, incumbent eligible elsewhere |Keep incumbent                                       |error|
|Tier pool empty, no incumbent                 |Leave tier `null`; router falls back down a tier     |error|
|Selected model price spiked >`price_spike_pct`|Keep incumbent; flag for review                      |error|
|Promo expired before next run                 |Price-basis rule already used list price; no surprise|—    |
|Invalid config                                |Fail before any write                                |error|
|Smoke test fails (if enabled)                 |Veto promotion; keep incumbent                       |warn |

-----

## 15. Worked example (Tier 2, synthetic)

This example uses invented identifiers and prices; it is not a provider price
snapshot or a recommendation. With `alpha = beta = 1`, global blend `3:1`, and
`credit_fee = 0`, `C_eff = (3·in + 1·out)/4`.

|Candidate (mode)             |in / out $/1M|Q |C_eff|ROI = Q / C_eff |
|-----------------------------|-------------|--|-----|----------------|
|**example/model-a (standard)**|0.10 / 0.20 |47|0.125|**376** ← winner|
|example/model-b (standard)  |0.14 / 0.28  |49|0.175|280             |
|example/model-c (premium)   |0.44 / 0.88  |52|0.550|95              |
|example/model-d (standard)  |0.33 / 1.98  |50|0.743|67              |
|example/model-e (standard)  |0.98 / 3.08  |51|1.505|34              |

**Winner: `example/model-a`** — the lowest-cost candidate that clears the Tier 2 quality band wins under the configured objective.

Two things this example makes concrete:

1. **The metric is cost-leaning by default.** The lowest-index in-band model wins because the band already cleared the quality bar. Raising `alpha` shifts weight toward higher-index candidates, making a higher-cost choice an explicit retune rather than an accident.
1. **The promo rule matters.** A candidate must be scored at list price unless its promotion outlives the next run, preventing a selection from depending on a price that expires before recomputation.

-----

## 16. Open questions to confirm before build

1. **ROI objective** — keep value-optimizing default (`alpha=beta=1`), or bias toward band-top quality (raise `alpha`)? (§5.1)
1. **Quality source** — is programmatic Artificial Analysis access available, or should `quality_scores.yaml` be maintained by hand? (§4.2)
1. **Blend weights** — use the measured input:output ratio (global and/or per tier), or default to `3:1`? (§5.2)
1. **Provider/residency policy** — any providers to allow/blocklist (relevant given how many top-value models are Chinese-origin)? (§7.9)
1. **Preview models** — include OpenRouter preview/beta listings, or current-only? (§7.3)
1. **Liveness gate** — enable the post-selection smoke test, accepting the determinism trade-off? (§9)
