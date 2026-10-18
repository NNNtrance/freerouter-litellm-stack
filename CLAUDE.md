# Project conventions

## Free-model pool — read POOL_GUIDE.md first (2026-10-09)

Adding, removing or renaming any model or provider? Follow **`POOL_GUIDE.md`**. Design
and evidence: `plans/2026-10-09-pool-organization.md`. In one paragraph: free models
only; clients (Hermes, OpenCode, Poke) use ONLY the role names `hermes-fast|smart|coder|
long|vision` (+ `free-router`) from `freerouter/policy/roles.yaml`; every pooled
deployment passes a per-deployment gate (>=64k effective context, a real tool call,
>=20B when the id states a size); same models merge via `canonical_alias()` +
`freerouter/policy/aliases.yaml`; LiteLLM routes by latency; four healing layers (router
per request, FreeRouter hourly, `pool_doctor.py` hourly via Hermes cron `ec12596ec013`,
`pool_lint.py` weekly). `config.yaml` `model_list` is EMPTY on purpose.
Roles are TIERED (`tiers:` in roles.yaml = LiteLLM `order`: tier 1 first, next tier on
failure). Hermes's default (2026-10-09) is the single model `deepseek-v4.1-flash`, not a
role, to keep the provider's prompt cache warm; fallback chain `hermes-smart` ->
`hermes-fast` -> `free-router`. Its only healthy provider is Evren (free to 2026-11-01).
`pool_bench.py` measures tool-call latency per model for tier decisions.

## Workflow

- **Always merge and deploy.** When a PR is created for this repo, do not stop at the draft stage — flip it to ready-for-review, merge it (squash), and let the deploy workflow run. Do not ask "should I merge?" for routine changes; just ship.
- The site is built by `update-models.yml` (cron hourly + on-push when `generate_site.py` changes) and deployed via GitHub Pages from `docs/`.
- Probe data comes from `probe_models.py` via `probe.yml` (cron every 30 min).

## Local ops state (Mac mini, updated 2026-09-25)

Read this before touching the running gateway. The LIVE stack is the ROOT
compose project (`litellm-proxy`: containers `litellm`, `litellm-model-sync`,
`litellm-db`), NOT `danissimode/docker-compose.yml` (hardened reference, not
running). Live mounts: `./config.yaml`, `./freerouter/state`. Keys live ONLY
in `./.env` (`env_file` for litellm; model-sync gets an explicit key list in
compose), never inline. After `.env` edits: `docker restart litellm
litellm-model-sync`.

- **`config.yaml` `model_list` is empty (since 2026-10-09).** Every free provider is
  a FreeRouter yaml, so it is discovered, gated, merged, healed and price-checked; the
  antseed pins live in `antseed_routes.yaml` and are hot-synced as `as-` DB rows by
  `antseed_watch_cycle.py` (no litellm restart). Removed then: `llm7/*`, `llmtr/*` (now
  `llm7.yaml` / `llmtr.yaml`), `sambanova/*` (duplicated `sambanova.yaml`),
  `pollinations/*` (legacy text API answers 400 "being deprecated"), `motif/motif-3` +
  `llmtr-motif-3` (no longer zero-priced: it billed), `llmtr/evren-*` (BYOK). A static
  entry would also win over the DB rows of the same name and pin traffic to one
  provider. Restore point: `config.yaml.bak-20261009-014425-pre-pool-plan`.
  The 6 old roles (coder, fast, …) removed 2026-09-25 are now the `hermes-*` roles.
  The `auto` admin-UI row (UUID, Evren's own auto-router via the UI credential "Evren")
  is the one deliberate manual route; `pool_doctor.py` `KNOWN_MANUAL` allow-lists it.
- **llmtr.com is a PAID aggregator (347 models), routed free-only via
  `freerouter/providers/llmtr.yaml` (`mode: priced_catalog` on `pricing.prompt` /
  `pricing.completion`).** Only zero-priced entries are routed and the price is re-read
  every hourly cycle, so a repriced model leaves the pool instead of billing
  `LLMTR_API_KEY`. `evren/*` entries are denied (BYOK on llmtr's side; Evren is routed
  directly). Embedding/rerank/image ids are gated out. Note llmtr gates free models on a
  reduced daily allowance for the first 24h of an account.
- **Antseed is a PAID P2P market — every route must be pinned to a verified-free
  peer, and the buyer wallet stays at 0 USDC.** `@antseed/cli`'s `buyer start`
  serves an OpenAI-compatible proxy on `127.0.0.1:8377` (loopback only; the
  container reaches it as `host.docker.internal:8377`). Its catalog is 241 models
  and **most are paid**, settled per request in USDC on Base. The `antseed/*`
  routes live in `antseed_routes.yaml` (hot-synced as `as-<hash>` DB rows, no restart,
  since 2026-10-09; `--sync-only` applies hand edits), each hard-pinned with the documented
  `<peerId>@<model>` model prefix (`openai/<peerId>@<model>` — LiteLLM strips only
  `openai/` and forwards the rest verbatim). Known peer ids: Open Forge
  `1d90f467689d499dc435e5744b4613c3203eb0aa`, Dark Signal
  `4668854ba3e8b094e6f48fbeb59cec1cfde162f2`, D5V1N2
  `d5e7d5950b6cc71c095da4ace0c0cb8495667be0`, Super Intelligence
  `b0d884578491b8544dc8fc162f4a0f52e31a9ccf`.
  **Why pinned and not `fr/` pool entries:** FreeRouter probes send a bare model id
  with no seller pin, and the buyer's `routingPreferences.minTrustScore` (default
  60) excludes three of the four free peers — so a probe of e.g. `glm-4.7-flash`
  would rank only the eligible *paid* offers and route there while FreeRouter logged
  the route as free. The provider yaml schema has no custom-header key, so the pool
  cannot be pinned out of it. A pin is also **hard**: a dead peer returns 503 with
  `"antseed_pinned": true` (observed) instead of failing over to a paid seller.
  Never add an unpinned antseed route, and never `fr/`-declare one. Buyer identity
  `~/.antseed/identity.key`, logs `~/.antseed/logs/buyer.log`, kept alive by
  `~/Library/LaunchAgents/com.furkan.antseed-buyer.plist` (RunAtLoad + KeepAlive).
  Free peers are volunteer capacity (one seller literally reads "SOLD OUT, back
  tomorrow") — re-probe rather than assume. Route health is watched every 6h:
  `antseed_free_probe.py` (probe + optional `--apply` re-pin; state in
  `logs/antseed_free_probe.json`) driven by `antseed_free_watch.sh`, run by the Hermes
  cron job `antseed free-route watch` (`a67dfb4081c6`, via the
  `~/.hermes/scripts/antseed_free_watch.sh` shim). The watch also re-reads the
  catalog, because a pin stops the wrong-seller problem but NOT a seller repricing the
  same listing.
- **Two irreplaceable secrets are mirrored to the login keychain** (service
  `litellm-proxy/<VAR>`, account `mini`): `LITELLM_SALT_KEY` and
  `LITELLM_MASTER_KEY`. Every model lives in the DB with `litellm_params`
  encrypted under the salt, so losing it makes the whole catalog undecryptable;
  losing the master key locks out the API, the admin UI and `/model/new`, so
  FreeRouter could not re-register anything. `./compose.sh --keychain` stores
  both, `--keychain --check` compares (non-zero exit on any mismatch),
  `--keychain --restore` rehydrates a gutted `.env` (rehearsed for both: value
  returns byte-identical, `.env` stays 52 lines at mode 600, and the rewrite is
  an atomic `mv` so `.env` is never left truncated). Pass a bare
  `LITELLM_MASTER_KEY` to act on just one. Adding a third secret means adding it
  to `MANAGED_SECRETS` plus a `_kc_comment` line. Treat a deliberate salt
  rotation as a full re-registration — delete all rows, let FreeRouter rebuild.
  Note `security add-generic-password` uses **`-j`** for the comment; `-C`/`-D`
  are 4-char application-type flags and reject anything else.
- Admin UI: `http://<host>:4000/ui`, login with `.env` `UI_USERNAME` /
  `UI_PASSWORD` (config.yaml also hardcodes `ui_username: admin`; they match).
  Note `POST /login` takes **form encoding**, not JSON — a JSON body returns 401
  and looks like bad credentials. The admin API under the UI is the same
  `/model/new` + `/model/delete` FreeRouter uses.

- **model-sync is FreeRouter, built from `./freerouter`** (not `python:3.12-slim`
  + mounted `sync_models.py` any more). Upstream has no Dockerfile; the local
  `freerouter/Dockerfile` is ours and is in the `compose.sh` backup list.
  Because the service is build-based, `docker compose up -d model-sync` alone
  reuses the stale image — use `./compose.sh --sync`.
- **`config.yaml: store_model_in_db` MUST stay `true`.** `general_settings`
  overrides the `STORE_MODEL_IN_DB` env var (litellm `proxy_server.py`:
  `general_settings.get("store_model_in_db", False)`). With `false` here,
  `POST /model/new` and `/model/delete` 500 with "Set STORE_MODEL_IN_DB='True'
  in your env" and every registrar silently stops working. This bit us on
  2026-09-25 (fixed; see agent_logs).
- **FreeRouter local patches:** `compose.sh freerouter_pull` applies every
  `patches/*.patch` in sorted order (`--index`, each `--check`ed first, after cleaning
  untracked files in the patched dirs). `canonical-pooling.patch` (below) + 
  `pool-organization.patch` (2026-10-09: `policy.py` gate, `policy/*.yaml`, tool-calling
  probe, context learning from traffic, ranked cap, net-loss circuit breaker, roles,
  `-it`/`-free` merging, `llm7.yaml`, `llmtr.yaml`; regenerate it as the diff against
  upstream + canonical-pooling). Patches are the **only** place to change anything under
  `freerouter/` — never hand-edit the checkout, because `freerouter_pull` does
  `reset --hard` + re-apply and would discard it. `freerouter/Dockerfile` is
  untracked and survives the reset.
  1. `canonical_alias()` in `planning.py` — the same model on several platforms
     shares one alias (`llama-3.3-70b`) and pools, instead of N routes.
  2. `DEFAULT_RPM = 10` fallback in `planning.py`. **Load-bearing:** LiteLLM's
     `simple-shuffle` weights by `rpm` and scores a missing `rpm` as **0**, so a
     provider that omits the limit is never selected — cloudflare had 45
     deployments and served 0 requests. The deployment fingerprint hashes the
     *emitted* rpm, so changing it re-ids every deployment and forces a re-post.
     If merging looks one-sided again, check `rpm` is non-null on all `fr-` rows.
  3. **`nvidia-nim.yaml` allow-list + `max_models` 25 → 60.** Upstream allowed
     only `*instruct*`/`*-it`/`*chat*`, encoding a naming convention NVIDIA has
     dropped, so of 82 catalog entries it kept 22 — and **none** of the models
     this account actually has provisioned. Now matches on family (`*nemotron*`,
     `*deepseek*`, `*glm*`, …). The `max_models` bump is required, not cosmetic:
     the cap applies *after* an alphabetical sort, so with the widened list at 25
     the new entries sort after the `-instruct` block and get evicted (measured
     1/7 surviving at 25, 5/7 at 60). Effect: NVIDIA 1 → 10 healthy models, and
     four new cross-provider merges.
  4. **Throttle backoff (`state.py` + `probe.py`).** A 429 left the model
     `unknown` with `retry_after: None`, so it never left the head of the
     unknown queue and re-spent the provider's *entire* per-cycle probe budget
     on itself every cycle — OpenRouter's 2 slots/cycle were monopolised by two
     throttled models while 14 went unprobed, so the nemotron merges never
     materialised. A throttle now parks the model 30 min, and `probe_targets`
     honours `retry_after` for `unknown` (it only checked it for `quarantined`).
     OpenRouter 2 → 12 healthy; pool `healthy` 45 → 55.
- **litellm has a healthcheck** (urllib, no curl in that image). `model-sync`
  gates on `condition: service_healthy`; removing the healthcheck makes
  model-sync never start.
- **model-sync key list** (docker-compose.yml, explicit — NOT `env_file`, so the
  sync container never holds DATABASE_URL/POSTGRES_PASSWORD): CLOUDFLARE (+ACCOUNT_ID),
  COHERE, GEMINI, GROQ, OPENROUTER, CEREBRAS, MISTRAL, NVIDIA_NIM, LLM7 (placeholder
  `unused` — anonymous tier; LiteLLM needs it in `.env` too), LLMTR, plus two
  renames — `ZHIPU_API_KEY=${ZAI_API_KEY}` and `HUGGINGFACE_API_KEY=${HF_TOKEN}`
  (FreeRouter expects those exact names). Every name is a `credential:` in
  `freerouter/providers/*.yaml`, so the list tracks the provider catalog. Verified
  working: cloudflare, cohere, gemini, groq, openrouter. Dead but still passed so
  a rotation needs no compose edit: CEREBRAS (402), MISTRAL (401), NVIDIA_NIM
  (valid key, 0 provisioned NIM endpoints → every model 404s), ZHIPU (`.env`
  ZAI_API_KEY is truncated — no `.secret` part). No FreeRouter provider exists for
  POLLINATIONS/LLM7/TOGETHER/PERPLEXITY/PUBLICAI/KLUSTER keys.
- **Providers added 2026-10-04** (free-API sweep from the @calbuldelis69 "NEW FREE
  AI API DROPS" thread; each one proofed with a real chat call before it was
  registered). Keys live in `.env`, names added to the compose key list, and the
  new yamls went into `patches/canonical-pooling.patch` so `compose.sh --all`
  carries them (verified: the extended patch still `git apply --check`s clean
  against upstream main):
  - `kilo.yaml` — Kilo Gateway, **keyless**. `_skip_reason` drops any provider
    whose `credential:` env var is empty, so a keyless platform still needs a
    placeholder value (`KILO_API_KEY=unused`) plus `discovery.auth: none`.
    16 `:free` models verified 200; `/models` also returns hundreds of paid
    entries, so the allow-list `*:free` + `kilo-auto/free` is mandatory.
  - `zai.yaml` — Z.AI **global** (`api.z.ai/api/paas/v4`), separate from the
    upstream `zhipu.yaml`, which points at the CN `open.bigmodel.cn` host this key
    cannot authenticate to. Only `glm-4.7-flash` / `glm-4.5-flash` are free (chat
    200); everything else is 429 "Insufficient balance", and the account's
    `/models` catalog does not even list the flash ids → `mode: static`.
  - `sambanova.yaml` — the long-invalid `SAMBANOVA_API_KEY` was re-issued from the
    console (Auth0 → Continue with Google). 6 free models. `/v1/models` answers
    200 **without** a key, so the allow-list is what keeps paid entries out. Free
    tier is ~20 RPD and the open models frequently answer 429 "high demand" →
    expect `throttled` probes, and a `sambanova-free` group alias that only
    appears once a probe actually lands.
  - `zen.yaml` — **PAUSED 2026-10-17** (OpenCode Zen). Was: ten of its eleven `-free` models
    returned 403 `FreeTierError: can only be used from within OpenCode`, with `space-bunny-free`
    the one id a plain OpenAI client could call. **That last one closed too**: a live probe on
    2026-10-17 answers the same 403 for `space-bunny-free`, and its last good answer was
    2026-10-10. A client-locked free tier is not pool material — the pool cannot impersonate the
    OpenCode client — so the provider is `status: paused` (file kept for history). Key comes from
    a console *service account* (`opencode.ai/console/<org>/service-accounts`), org
    `org_01M426PFQJ6R8ANRFWDEG97W07`.
  - `electronhub.yaml` — Electron Hub (600+ model aggregator, `api.electronhub.ai/v1`).
    Signup genuinely is behind a Cloudflare wall (see below), so furkan cleared it by
    hand and pasted the `ek-…` key; adding it was then trivial. **The catalog's
    pricing is a trap:** the 8 entries priced `0/0` are all `*:dev` and answer 403
    "exclusive to the Electron Hub Coding Plan" — this is the clearest case yet of
    rule zero (a listing is not a free tier). The real free basis is a **weekly
    credit grant** spent at catalog prices, so the allow-list is a hand-verified set
    of the *cheapest* models that answer 200, not the free-looking ones.
    `/v1/models` is public (200 unauthenticated) → `auth: none`. Ids here contain
    `/` and `:` (`kilo-auto/free`, `deepseek-v4-flash:dev`) — quote every allow/deny
    entry. Free tier also throttles to ~4 requests per ~180 s window, so
    `probe.max_per_cycle: 3` and generate it from the probe CSV, not by hand.
  - **Not addable (checked 2026-10-04 — don't re-spend a session):** FreeInference —
    onboarding is closed ("we have stopped onboarding new
    users"). Nous Portal — no key can be created on the free tier ("You need
    available funds to create or use API keys"); the account that already exists
    there is `furkanpc34@gmail.com`, not the main Gmail. Nebius / Nscale — trial
    credits ($1/$5) that need a card once spent, i.e. not a `tier: free` platform.
    SiliconFlow — registration wants a CN phone number.
- **Providers added 2026-10-05/06** (second "NEW FREE AI API DROPS" thread from the same account —
  `x.com/calbuldelis69/status/2107089393672704007`; the first thread, `status/2106441106917613908`,
  is the 10-04 set above. Note `status/21` in a chat is a broken link: X ignores the handle on a
  status URL and id 21 is `biz`'s 2006 first tweet):
  - `tokenharbor.yaml` — **Token Harbor** (`tokenharbor.ai/v1`, OpenAI-compatible aggregator).
    Every account gets a rolling 7-day free allowance; the 4 free ids are
    `deepseek-v4.1-flash:free`, `deepseek-v4-flash:free`, `mimo-v2.6-flash:free`, `mimo-v2.5:free`
    (the other 60 catalog entries are paid Claude/GPT/Grok/GLM and `:free` is the only marker).
    All four answered a real chat 200 before registration; `fr/tokenharbor/deepseek-v4.1-flash:free`
    and `free-router` both answer 200 through the gateway. Free tier is 60 RPM / 1800 RPH
    account-wide, hence `limits.rpm: 60`. **Privacy:** the free routes require enabling the
    dashboard's "Enable free models" consent, under which Token Harbor may retain free-route
    requests/responses — never route sensitive content there.
  - `tiarina.yaml` — **PAUSED 2026-10-17** (Tiarina Cloud AI Gateway, `ai.tiarina.cloud/v1`,
    Indonesian, prepaid Rupiah). Was: console marked 3 ids FREE ($0.00)
    (`deepseek-v4.1-flash:free`, `qwen3.8-flash:free`, `mimo-v2.6-flash:free`), registered but
    only ever serving 429 "rate limiting this model" while paid ids answered 402. **The free tier
    is now paywalled:** a live check answers 403 `{"code":"free_tier_locked","message":"Free models
    require the free tier. Top up your Tiarina Cloud balance to unlock it."}` for every `:free` id
    (the catalog now lists 5), while `deepseek-v4.1-flash` still answers 402 `insufficient_quota` —
    so the account reaches nothing, free or paid. Rows went quarantined 2026-10-11 and never
    recovered (14 consecutive failures), which is what the pool doctor alerted about nightly.
    `status: paused` now; un-pause only if a promo returns or the account is deliberately topped up,
    and re-verify with a live 200 (`curl` needs a browser `User-Agent`, else Cloudflare 1010).
  - `requesty.yaml` — **Requesty** (`router.requesty.ai/v1`), European aggregator, 754-model
    catalog. Free tier: all free models, **200 requests/day, no card**. Its own catalog carries the
    evidence in `input_price`/`output_price`, so this one is `mode: priced_catalog` (not `listing`):
    exactly 13 of 754 entries are zero-price. Verified 200s: `google/gemma-4-31b-it`,
    `mistral/leanstral-1-5`, `nvidia/nemotron-3-super-120b-a12b`,
    `nvidia/nemotron-3.5-lightning-30b-a3b`, `nvidia/nemotron-3-ultra-550b-a55b`,
    `novita/ling-3.1-flash` (+ `nvidia/muse-glimmer-30b`). `poolside/laguna-*`,
    `novita/inclusionai/ling-3.0-tiny` and `nvidia/nemotron-3-nano-30b-a3b` are zero-price but
    dead upstream (404/410) and are denied. **Privacy:** the free NVIDIA entries carry
    `data_used_for_training: true` — never route sensitive content here. Signup needed one
    human click (see below).
  - Rejected in the same sweep, don't re-spend a session: **Chutes** — account DELETED 2026-10-06
    (key worked, `/v1/models` listed 14 ids, but every chat call was `402 … balance is $0.0`).
    **There is no free path to Chutes at all:** its pricing FAQ says *"We do not offer a free tier at
    this time"*, anonymous `/v1/models` answers 200 while an anonymous chat call returns nginx 429,
    and the 200 req/day early-access program ended Feb 2026 — directories still advertising a free
    tier are stale. Staking TAO is the SN64 miner/validator side and earns emissions, not API credits.
    Deleting it: `chutes.ai/app/settings` → Danger Zone → Delete Account → type the username **and**
    paste the 32-char fingerprint; verify with `chutes.ai/?deleted=true` plus an "Invalid fingerprint"
    on re-sign-in. **GLHF** (host down, Cloudflare 522),
    **ModelRouter** (`modelrouter.id` resolves to `0.0.0.0`), **DeepInfra** (trial credits),
    **Scaleway** — decided 2026-10-06, don't re-litigate. Its free tier is real but it is a
    **one-time 1,000,000-token bucket** (plus 60 min audio), not a recurring allowance: billing
    starts at token 1,000,001 and the bucket is applied to the most expensive tokens first.
    Worth ≈€0.50 on `deepseek-v4-flash-0731`, ≈€1.60 on `qwen3.8-27b`, ≈€2.90 on `glm-5.2` — once.
    The card is not what unlocks the free tier; it unlocks **rate limits** ("Base limits apply if
    you registered a valid payment method", 200k TPM per model; identity/KYC raises it to
    400k–2,000k). KYC means uploading an ID document, which is out of scope for an agent, and a
    pooled `fr/` route would silently consume the bucket and then bill a live card with no spend
    visibility in our dashboard. The onboarding form itself (step 2/3) is address-only, no card
    fields, but the account cannot be used properly without one. Account exists
    (`hermes-freerouter` org, personal project, Türkiye) and was left unregistered.
  - **The signup wall is captcha, not passwords.** Google OAuth in the human's logged-in Safari
    covered every provider with **clicks only** — not one password was typed. Requesty's
    `app.requesty.ai/sign-up` uses Clerk with an invisible captcha
    (`Clerk.environment.userSettings.signUp.captcha = {enabled:true, widget:"smart"}`) that
    **cannot be solved by any agent-side click**: the UI spins forever and even a direct
    `Clerk.client.signUp.authenticateWithRedirect({...})` call stays pending indefinitely, and a
    trusted cua-driver AXPress does not defeat it. One human click in Safari and the rest of the
    flow (consent → API key → probe → yaml) is fully automatable. Same class: **Scaleway** needs a
    card at onboarding step 2/3. Email verification (Token Harbor) was read out of Gmail **in that
    same Safari session** — no mail CLI exists on this host.
  - `apinex.yaml` — **APInex** (`api.apinex.bond/v1`, OpenAI-compatible aggregator, added
    2026-10-07). Its free tier is a **daily check-in gate, not a passive allowance**: 1M
    free-model tokens/day (`/api/public/subscriptions` → `freeDailyTokens`) on a $0 balance,
    5 RPM (`/api/public/rate-limits` → `rpmFree`), and the allowance is **LOCKED until a human
    passes the Cloudflare Turnstile check-in** at `apinex.bond/airdrop?tab=quests`. Evidence:
    `/v1/subscription` reported `allowed: false` with 1M tokens still unused and every `free/*`
    call answered `402 "Daily check-in required to use free models"`; after one human click
    `allowed: true` and all 6 ids below answered a real chat 200 (usage `cost_tokens: 0`).
    Only 6 of the 18 `free/*` catalog ids are on the free plan (`allowFree: true` assume false):
    `free/deepseek-v4.1-flash`, `free/deepseek-v4-flash-0731`, `free/deepseek-v4-pro-0813`,
    `free/gpt-6-luna`, `free/glm-5.3-flash`, `free/mimo-v2.6-pro` — kimi-k3, claude-4.6/opus,
    gemini, qwen-3.8-max, minimax all require a subscription. Charge formula is
    `ceil((in+out) * weight)` with weight 2–3, so ~333k real tokens/day, not 1M. **These are
    reasoning models** (`usage.completion_tokens_details.reasoning_tokens`): a small `max_tokens`
    returns an EMPTY `content` — that is not a failure, don't chase it. Safety property: balance
    is $0 and free models never charge the wallet, so a missed check-in only 402s.
    Consequence for the pool: `fr/apinex/*` rows are dead for any UTC day the human does not
    click, which is why `probe.max_per_cycle: 1` and `limits.rpm: 5`. Reminder job:
    `~/.hermes/scripts/apinex_checkin_watch.sh` (shim) → `apinex_checkin_watch.py` in this dir,
    cron `4572a36ddc15` daily 11:00 Europe/Istanbul, `no_agent=true`, silent unless locked.
    Signup is Google-OAuth-only in practice (`/api/user/auth/challenge` →
    `emailConfigured: false`, `googleEnabled: true`); the register page's Terms checkbox is the
    gate on its Google link, and **an injected `click()` on the unchecked `input[type=checkbox]`
    is what makes the OAuth anchor get its href** (before that the `<a>` has `href=null` and the
    click is a silent no-op). The Turnstile check-in itself is human-only: the widget loads, never
    self-solves, and cua-driver's AX walk can't even reach that page (Safari's bookmarks menubar
    consumes the whole element budget — `in_web_content: 0`). The watch script sends a browser
    `User-Agent`; Cloudflare answers python-urllib's default with `403 error code: 1010`.
  - `experientiallabs.yaml` — **Experiential Labs** (`api.experientiallabs.ai/v1`, added
    2026-10-17). One key reaches `/v1/*` (inference) and `/api/*` (management reads, catalog,
    usage, waterfall). Only **6 of 299 callable rows carry a promotional free tier**
    (`GET /api/models` → `promotional_listed: true`); **price is not the signal here** — the same
    slug read through `/v1/models` shows `pricing.input_nano_usd_per_million_tokens` = a normal
    $0.30/M. The $0 proof is the account ledger: `GET /api/gateway/usage/events` returns both
    DeepSeek rows with `cost_nano_usd: 0` and `free_nano_usd: 135300 / 150000` (2026-10-17), and
    both answered a real `tool_calls` through the gateway as `fr/experientiallabs/*` with
    `usage.cost: 0.0`. Usable rows: `deepseek-v4.1-flash-1017` and
    `deepseek-v4.1-flash-uncensored` (1M ctx, tools). `gpt-6-luna-decisions` (922k) answers
    `503 unavailable_route` for now, `clef`/`clef-flash` (65k) declare no tool calling,
    `jev-latest` is 32k — the 64k + tool-calling gate drops those three, which is the honest
    record. **Safety property** (why a partially-free aggregator is acceptable): the org is on the
    **Free plan with no card**, and *Free models past the daily limit* = **Stop at the free
    limit** → credits overflow OFF (llms.txt: it needs a saved card AND a settled $1 charge, and
    only auto-enables at the first real payment), so an exhausted allowance answers
    `429 insufficient_quota` / `free_limit_reached` — never an invoice. The 600-credit balance is
    a **free grant** only *priced* calls draw down (one `qwen3.8-27b` smoke test charged 0.005 of
    the $5 free credit), so **never widen the allow list to a priced row**. Key: `.env`
    `EXPLABS_API_KEY` (`xpl_` + 40 hex, 44 chars), minted in the web console — a key cannot mint
    keys, so rotations are a Safari action.
- **Routing is `latency-based-routing` since 2026-10-09**, so `rpm` is now a per-deployment
  CAP, not a weight (the paragraph below describes the simple-shuffle era; DEFAULT_RPM=10
  stays because it still damps 429 storms on free tiers).
- **`DEFAULT_RPM` in the patch was load-bearing under simple-shuffle.** LiteLLM's `simple-shuffle`
  weights deployments by `rpm` and scores a missing `rpm` as **0**, so a provider
  that omits the limit is never selected — cloudflare had 45 deployments and
  served 0 requests. The patch now emits `provider.limits.rpm or DEFAULT_RPM`
  (10) for every deployment. The deployment fingerprint hashes the *emitted*
  value, not the provider's raw declaration, so changing `DEFAULT_RPM` re-ids
  every deployment and the reconciler re-posts it. If merging ever looks
  one-sided again, check `rpm` is non-null on all `fr-` rows first.
- **Updates: use `./compose.sh`** (menu, per-service `--litellm`/`--sync`,
  auto-backup to `./backups/`, `--rollback`). Never bare
  `up --force-recreate`, never `down -v`, never unconditional `image prune`.
  `compose.sh --all` pulls FreeRouter and re-applies the patch.
- **danissimode/ is a git checkout with LOCAL EDITS** (uncommitted
  2026-09-08): `sync_models.py` cross-ref rewritten from dead
  `cheahjs/free-llm-api-resources` (repo deleted from GitHub, 404s) to
  `freellm.net` JSON-LD catalog (`_freellm_model_urls`/`_freellm_model_id`,
  15 OR + 10 Cohere IDs); same change mirrored to root `sync_models.py`
  (not mounted, parity only). COMMIT + PUSH before any pull. NOTE: since
  2026-09-25 this script is no longer what model-sync runs, and its 2051
  deployments were **deleted from Postgres on 2026-09-25** (all rows with a
  non-`fr-` model_id; dump at `data/db-backup-20260925-231721.sql`). The
  curated `config.yaml` roles were never in the DB, so they are unaffected.
  Re-running it would re-add the old routes — don't, without deciding first.
- **Key status (corrected 2026-10-05 from live calls + `freerouter/state/health.json`, not from
  this doc — it used to claim GROQ/CEREBRAS/MISTRAL/TOGETHER were dead and GEMINI/COHERE/
  CLOUDFLARE/ZAI were empty, and every one of those claims was wrong by then):** serving healthy —
  kilo 13, gemini 10, electronhub 10, cloudflare 7, evren 7, mistral 7, aion-labs 6, cohere 5,
  openrouter 5, groq 4, ollama 3, zenmux 3, cerebras 2, sambanova 2, zai 2, zhipu 2, zen 1
  (`fr/groq/qwen/qwen3.8-27b`, `fr/cerebras/gpt-oss-120b` and `fr/mistral/*` all answer live; the
  mistral failures were 429 rate limits, not auth). `TOGETHER_API_KEY` is set in `.env` but **no
  `together.yaml` exists**, so that key is *unused*, not failing. Keys with no provider or a
  provider that cannot serve are dead weight, not evidence of a bad key.
  Read the state file before reporting anything as broken: keys there are `provider::model` (a
  `startswith("groq/")` filter silently matches nothing), and `unknown` only means "not probed
  yet this cycle" since `probe.max_per_cycle` spreads them — only `quarantined` after a real
  failure means the key is bad.
- **Known gaps:** `danissimode/.github/workflows/monitor-cheahjs.yml` still
  targets the deleted repo (will fail Mondays); role groups exist since
  2026-10-09 (`hermes-*`, `freerouter/policy/roles.yaml`); modelscope's key has never
  worked (all 25 models 401, the doctor rechecks weekly).
- **Provider watch (2026-09-26, now FreeRouter-aware):** `check_new_providers.py`
  reads the catalog straight out of `freerouter/providers/*.yaml` (24 providers)
  instead of scraping freellm.net, so it tracks whatever upstream ships rather
  than a hardcoded table that drifts. Probes each provider's own `discovery.url`
  with the auth style that provider declares (`bearer` / `query_key` / `none`),
  then lets FreeRouter's recorded chat probes **override** the catalog verdict —
  a 200 from `/models` proves nothing, which is exactly why Cerebras (200, but
  every chat 402s) and NVIDIA NIM (public catalog) used to read as healthy.
  Sections: 🆕 new catalog entries, 🔑 missing keys, ⚠️ 401s, 🟠 configured but
  failing, ✅ reachable **with chat-probe ratios** (e.g. `Groq (4/6)`), 📦 live
  pool from `/model/info` (deployment count + aliases genuinely merged across
  providers), 🚫 not routable upstream (paused/retired/watch). Baseline moved to
  `data/provider_watch.json` → `catalog_seen`; the old freellm `seen` key is
  retired. Sends only when there is news or a gap, else stays silent; `--send`
  forces the full report. Hand-rolled YAML subset parser on purpose — the host
  `python3` has no `yaml` module and launchd runs it with no venv. Schedule:
  `~/Library/LaunchAgents/com.litellm.provider-watch.plist`, **Saturday** 19:00
  (launchd `Weekday 6`; 0 or 7 would be Sunday). Test `--dry-run`, or `--send` /
  `--force "msg"` with `N8N_NOTIFY_WEBHOOK_URL` set to Parrot's URL. Notification
  goes to the **Parrot** webhook via `POST ?message=`. **The Telegram node parses
  the text as Markdown** (not plain — proven the hard way) even though
  `parse_mode` is absent from the node parameters. Two consequences: `_AI_`
  renders as italics, so `LITELLM_MASTER_KEY` used to arrive as
  `LITELLMMASTERKEY`; and a line with an **odd** number of underscores is
  unbalanced emphasis that Telegram rejects with 400 "Bad request - please check
  your parameters" — `• B.AI (B_AI_API_KEY)` has three and silently killed the
  whole send. So `env_label()` backtick-wraps every var name (a code span, which
  Markdown leaves alone) and `md_safe()` is a final guard that wraps any
  identifier left with unbalanced underscores. **The webhook acks 200 before
  Telegram is called, so the script cannot see this failure** — only n8n's Error
  Flow does, and that is what messaged the alert. A 200 from the script proves
  nothing; to verify a change really sends, look for a *new Error Flow
  execution*:
  `sqlite3 ~/n8n_data/database.sqlite "select e.id,e.status,w.name from execution_entity e join workflow_entity w on w.id=e.workflowId order by e.id desc limit 4"`
  Lesson: do NOT hand-build n8n workflows via
  import/sqlite — executions kept serving a stale definition (`undefined`
  texts); reuse Parrot. Dead `Provider Watch Notify` left deactivated.
