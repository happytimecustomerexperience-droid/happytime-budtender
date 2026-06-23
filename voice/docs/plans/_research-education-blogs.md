# Happy Time — Education & Blog KB Research (Voice Stack)

> **Purpose:** Knowledge-base + agent-training source material for the Happy Time voice stack.
> Two parts: (1) Happy Time house education/blog content distilled for the KB, and (2) a clean
> canonical cannabis **weights / types / WA purchase-limits** reference table.
>
> **Generated:** 2026-06-22 · **Maintainer note:** re-verify the site-sourced bullets against the
> live pages when the Vercel checkpoint lifts (see "Provenance" below).

---

## ⚠️ Provenance & sourcing caveat (READ FIRST)

**The live site is currently UN-FETCHABLE by automated tools.** Every request to
`https://happytimeweed.com/*` returns **HTTP 429 with a "Vercel Security Checkpoint" challenge
page** (an Astro-rendered JS bot-wall) — both `WebFetch` and a direct `fetch()` with browser
headers get the same 31KB challenge HTML, not the real content. So I could not scrape the page
bodies directly.

What I *did* recover is **site-sourced via search-engine snippets** (Google/Bing have crawler
allow-listing the WAF respects) of the real Happy Time education pages. Those snippets quote the
actual page copy and are labeled **`[SITE]`** below. Everything labeled **`[GENERAL]`** is general
WA-cannabis / industry knowledge added to fill gaps — clearly separated so the KB owner knows
exactly what came from Happy Time's own pages vs. general domain knowledge.

**Confirmed live Happy Time education/blog URLs** (discovered, not fetched — feed these to a
browser-MCP or manual paste when building the KB for verbatim house copy):

| URL | Topic |
|---|---|
| `https://happytimeweed.com/education` | Education Center hub (Marijuana 101, dosing, products, medical) |
| `https://happytimeweed.com/education/edibles` | Edibles guide — dosing, onset, peer-reviewed |
| `https://happytimeweed.com/education/microdosing` | Microdosing guide (low-dose, 1–2.5 mg) |
| `https://happytimeweed.com/education/cannabis-strain-types` | Strain types (indica/sativa/hybrid) |
| `https://happytimeweed.com/education/cannabis-storage-guide` | Storage — flower/edibles/concentrates/carts |
| `https://happytimeweed.com/blog/how-to-use-disposable-vape` | Beginner disposable-vape guide |
| `https://happytimeweed.com/blog/best-dispensary-yakima-wa` | Brand/SEO blog |
| `https://happytimeweed.com/blog/recreational-marijuana-yakima-wa` | Rec-marijuana-in-Yakima blog |
| `https://happytimeweed.com/dispensary-faqs/` | FAQ page |
| `https://happytimeweed.com/strains/<slug>` | Per-strain effect/flavor pages (e.g. `terpee-slurpee`, `guava`) |
| `https://happytimeweed.com/brands` | Featured brands |
| `https://happytimeweed.com/yakima-menu` / `/mount-vernon` / `/pullman-menu` | Per-store menus (Dutchie-embedded) |

---

## 1. House identity & footprint `[SITE]`

- **Happy Time** is a **family-owned** Washington cannabis retailer.
- **Three stores:** **Yakima** (1315 N 1st St, Yakima, WA 98901), **Mount Vernon** (Skagit),
  **Pullman**. Voice agent should localize hours/pickup/menu to the caller's store.
- Online ordering is **pickup** (Dutchie-embedded menus per store).
- **Editorial house standard `[SITE]`:** "every regulatory and health claim in their guides cites a
  verifiable government or peer-reviewed source." → **Voice-agent house rule: never invent a dosing
  number or a medical claim; cite/anchor to the education page, and stay conservative.** This mirrors
  the budtender "Numbers-Guard" pattern — the agent surfaces facts, never originates a figure it
  can't back.

---

## 2. Edibles — dosing, onset, formats `[SITE]`

Distilled from `education/edibles`. **Highest-value training block** — edibles are where customers
most need guidance and where over-consumption risk is real.

**Beginner dose**
- **Start at 2.5 mg THC** for a first-time edibles customer. Practical instruction: **cut a 10 mg
  gummy into quarters, take one quarter.**
- **Wait 2 hours before re-dosing** (a hard rule — repeat it even when they "don't feel anything yet").

**Onset & duration**
- Edibles **onset in 30–90 minutes**, **last 4–8 hours** (much longer than inhaled).
- **Peak effects ≈ 3 hours after ingestion** (on average).

**Empty vs. full stomach**
- **Empty stomach** → hits **faster but more unpredictably**; peak can come on **suddenly**.
- **With food** → **slower onset, more gradual** curve.
- House recommendation: **eat a small meal before a first edibles experience** to make the curve
  easier to track.

**"Took too much" guidance `[SITE]`** — the edibles guide explicitly covers what to do if you
overconsume (stay calm, hydrate, rest; effects pass; CBD can blunt it). *(Pull verbatim text when
the site is reachable; keep the agent's tone reassuring and non-alarmist.)*

**Formats carried (all 3 stores)**
- **Gummies** — most common format, **5 mg or 10 mg per piece**.
- **Chocolates**, **baked goods**, **mints** (mints/lozenges = **fast onset, sublingual absorption**),
  **THC beverages** (**15–30 min onset — the fastest edible category**).

---

## 3. Microdosing `[SITE]`

Distilled from `education/microdosing`.

- **Definition:** consuming **sub-intoxicating doses (1–2.5 mg THC)** for functional use.
- **Start dose:** **2.5 mg THC** (¼ of a 10 mg gummy); **wait 2 h min** before re-dosing; **peak ≈ 3 h**.
- **Use cases the guide names:**
  - **Tolerance management** — low daily doses without ramping tolerance.
  - **Stepping down from heavy use** — e.g. 10–30 mg/day → functional doses.
  - **Anxiety modulation** — some users find **low-dose THC + CBD** better than high-dose THC.
  - **Sleep** — **2–5 mg THC + low-dose CBN** sometimes works without morning fog.
- **Pattern timeline:** benefits **compound over weeks**; patterns emerge after **2–4 weeks**.
- **Precautions (do-not):** re-dose too soon (still wait 2 h on edibles); stack with alcohol;
  start with high-THC strains.

---

## 4. THC vs CBD `[SITE]`

- **CBD is non-intoxicating;** effects are **calming / anti-anxiety** without the strong THC
  head/body high.
- **1:1 CBD:THC** (e.g. **5 mg CBD + 5 mg THC**) often **feels less intoxicating** — CBD softens THC.
- `[GENERAL]` Common ratios on WA shelves: **1:1** (balanced), **2:1 / 5:1 / 20:1** (CBD-leaning,
  progressively less head-high). **CBN** = the "sleepy" minor cannabinoid (pairs with THC for sleep).

---

## 5. Strain types, terpenes & effects `[SITE]`

From `education/cannabis-strain-types` + per-strain pages.

- **Strain-type labels (indica / sativa / hybrid) are a GENERAL industry classification.** Happy
  Time's stated position: **"the terpene profile and your own physiology shape the experience more
  than the label."** → **Voice-agent house rule: never over-promise that "indica = couch-lock."
  Ask about desired *effect*, then steer by terpene + reported effects, not just the type word.**
- **Effect vocabulary the site uses** (mirrors the budtender's effect engine — relaxed/uplifted/middle):
  - *Terpee Slurpee (Hybrid)* — **energetic, uplifted, focused, aroused, tingly**; **citrus, lemon-lime,
    orange, sweet**.
  - *Guava (Hybrid)* — **relaxed, happy, euphoric, uplifted, talkative**; **tropical, fruity, sweet, citrus**.
- `[GENERAL]` **Terpene → effect cheat-sheet** (matches `ranking.EFFECT_HINTS`):
  - **Myrcene, Linalool** → relaxed/sedating (indica-leaning).
  - **Limonene, Pinene** → uplifted/energizing (sativa-leaning); limonene = citrus/mood, pinene = alert.
  - **Caryophyllene** → calming/peppery (the only terpene that hits CB2 receptors).
  - **Terpinolene** → bright/heady. **Humulene** → grounding.

---

## 6. Concentrates, vapes & flower forms `[SITE]` + `[GENERAL]`

**Vape cartridges / disposables `[SITE]`**
- **Live-resin carts** are extracted from **fresh-frozen flower**, so the **terpene profile tracks
  the original strain closely**.
- Carts deliver **fast onset (same as flower) with none of the smoke** — discreet, on-the-go.
- A dedicated **"How to use a disposable vape"** beginner blog exists (`blog/how-to-use-disposable-vape`).

**Concentrate subtypes `[GENERAL]`** (mirror `ranking._SUBTYPE_KEYWORDS["concentrates"]` so KB and
recommender agree): **rosin / live rosin** (solventless, premium), **live resin / cured resin**,
**RSO / FECO** (full-extract, oral), **distillate** (high-THC, flavorless), **diamonds**, **sauce**,
**badder/batter/budder**, **shatter**, **crumble**, **sugar**, **wax**, **bubble hash / temple ball**,
**kief**, **applicator/syringe**.

**Flower forms `[GENERAL]`:** whole-bud, **smalls/popcorn** (smaller buds, cheaper), **shake**
(loose, cheapest), **pre-rolls** (single or multi-pack), **infused pre-rolls** ("diamond /
hash-hole / moon-rock"), **blunts**.

---

## 7. Storage `[SITE]`

From `education/cannabis-storage-guide` (flower / edibles / concentrates / carts).

- **UV degrades THC and terpenes** → **always store opaque / in a dark space.**
- `[GENERAL]` General rules to round out the KB: **cool, dark, airtight**; flower ideal ~**59–63%
  RH** (humidity packs); keep concentrates **cold**; store carts **upright**; **keep all products in
  child-resistant packaging, locked away from kids/pets.**

---

## 8. House style / canned-answer guidance for the voice agent

Synthesized from the site's tone + the budtender's `_why`/reason patterns. Use these as **system-prompt
behaviors**, not verbatim scripts:

- **Lead with the effect/occasion question**, not the product. ("What are you going for — relaxed,
  uplifted, something balanced?") — mirrors the questionnaire slots (`effect_desired`, `category`).
- **Be specific and conservative on dosing.** Always: *start low, wait 2 hours, don't re-dose early.*
  Never invent an mg number — anchor to 2.5 mg start / 5–10 mg standard piece.
- **Cite the source for any health/regulatory claim** ("our edibles guide explains…") — house standard.
- **Don't over-promise strain-type effects;** steer by terpene + reported effects.
- **Respect WA limits** (Section 10) and **21+ / valid-ID** gating in every transactional turn.
- **Pickup-only, per-store;** localize to the caller's store (Yakima / Mt Vernon / Pullman).
- **Persuasive but honest "why this" hooks** (from `ranking._why`): personal go-to brand → live deal/
  on-sale → the effect they asked for → genuinely high THC (≥25%) → real scarcity ("almost gone") →
  terpene/strain flavor. Never fabricate a deal or a stat.
- **One add-on, lighter & cheaper** (from `pairing.py`): suggest a single complementary impulse item
  (pre-roll/edible/beverage) at ~25% of the main item's price — never a second big purchase.

---

## 9. Canonical cannabis WEIGHTS & SIZES reference `[GENERAL]` (+ matches budtender code)

**Flower / concentrate weight ladder** (aligns with `ranking._GRAM_BUCKETS` / `_GRAM_HINTS` / `_SIZE_SYNONYMS`):

| Common name | Grams | Notes |
|---|---|---|
| Half-gram | **0.5 g** | common cart / single pre-roll |
| Gram | **1 g** | "a gram"; standard cart size |
| 2 grams | 2 g | |
| **Eighth** (⅛ oz) | **3.5 g** | the default flower unit customers shop by |
| 4 grams | 4 g | occasional "4g eighth-plus" deals |
| **Quarter** (¼ oz) | **7 g** | |
| 8 grams | 8 g | |
| 10 grams | 10 g | |
| **Half-ounce** (½ oz) | **14 g** | |
| **Ounce** (oz) | **28 g** | the WA flower purchase **cap** (1 oz) |

> Quick math: **1 oz = 28 g**, **½ oz = 14 g**, **¼ oz = 7 g**, **⅛ oz = 3.5 g**.

**Cartridge sizes:** **0.5 g** and **1 g** (most common). Disposables = all-in-one (battery + oil).

**Edible dosing units `[GENERAL]` + `[SITE]`:**

| Unit | Value | Notes |
|---|---|---|
| Microdose | **1–2.5 mg THC** | functional, sub-intoxicating `[SITE]` |
| Beginner start | **2.5 mg THC** | ¼ of a 10 mg piece `[SITE]` |
| Standard piece | **5 mg or 10 mg THC** | typical gummy `[SITE]` |
| **WA max single edible package** | **10 × 10 mg = 100 mg THC** | standard WA solid-edible pack |
| Onset | **30–90 min** (beverages 15–30 min) | peak ≈ 3 h `[SITE]` |

**Pre-roll sizes:** sold by **pack count** — **single, 5-pack, 10-pack**, etc. (the size axis the
budtender parses from the name). Per-joint weights commonly 0.5 g / 1 g.

---

## 10. Washington State purchase limits & legal gating `[GENERAL — WA law]`

**Per-transaction adult-use limits (21+), per WAC 314-55-095 / WSLCB:**

| Product form | Limit |
|---|---|
| **Useable cannabis (flower/bud)** | **1 ounce (28 g)** |
| **Concentrates** | **7 grams** |
| **Cannabis-infused edibles — solid** | **16 ounces** |
| **Cannabis-infused edibles — liquid** | **72 ounces** |

- **21+ only**, valid government-issued **photo ID required** at pickup. (WA has no separate medical
  dispensary system — medical patients buy at the same rec stores; a DOH-recognition card can raise
  certain limits, but the **voice agent should quote the standard adult-use limits above**.)
- Purchases are **tracked** so a customer cannot exceed limits in a transaction.
- `[GENERAL]` **DOH-Compliant ("DOH Approved") products** are the WA-Dept-of-Health-certified
  medical line (specific cannabinoid ratios, testing, labeling) — this maps to the budtender's
  `doh_only` filter and the "DOH Approved" category names in the live catalog. The voice agent can
  filter to DOH products on request.

---

## 11. KB-build TODO (for the owner)

1. **Get verbatim house copy:** when the Vercel checkpoint lifts (or via an authenticated/browser-MCP
   pull), capture full text of the 6 education pages + the disposable-vape blog + FAQ for exact
   wording, citations, and any "what to do if you took too much" script.
2. **Sync taxonomy with the recommender:** the KB's category/subtype/size vocabulary should stay
   identical to `happytime-budtender/budtender/ranking.py` (`CATEGORY_BY_SLOTKEY`, `_SUBTYPE_KEYWORDS`,
   `_GRAM_HINTS`) so the agent's spoken vocabulary and the suggestion API speak the same language.
3. **Pull per-strain effect/flavor pages** (`/strains/<slug>`) into the KB as structured
   effect→terpene→flavor rows to power spoken effect-based recommendations.

---

## Sources

- [Happy Time Education Center](https://happytimeweed.com/education)
- [Happy Time Edibles Guide](https://happytimeweed.com/education/edibles)
- [Happy Time Microdosing Guide](https://happytimeweed.com/education/microdosing)
- [Happy Time Strain Types](https://happytimeweed.com/education/cannabis-strain-types)
- [Happy Time Storage Guide](https://happytimeweed.com/education/cannabis-storage-guide)
- [Happy Time — How to Use a Disposable Vape](https://happytimeweed.com/blog/how-to-use-disposable-vape)
- [Happy Time FAQs](https://happytimeweed.com/dispensary-faqs/)
- [Happy Time — Best Dispensary Yakima](https://happytimeweed.com/blog/best-dispensary-yakima-wa)
- [WSLCB — Know the Law: Cannabis](https://lcb.wa.gov/education/know-the-law-cannabis)
- [WAC 314-55-095 (purchase limits)](https://app.leg.wa.gov/wac/default.aspx?cite=314-55-095)
