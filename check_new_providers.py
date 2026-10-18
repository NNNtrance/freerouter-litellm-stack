#!/usr/bin/env python3
"""
Provider watch: weekly status message via the n8n Telegram webhook.

Since 2026-09-26 this reports on **FreeRouter**, not the retired
freellm.net -> sync_models.py pipeline. The catalog is read straight out of
`freerouter/providers/*.yaml`, so the report automatically tracks whatever
upstream ships (their bot commits new providers daily) instead of a hardcoded
table that drifts. Key health comes from probing each provider's own
`discovery.url` with the auth style that provider declares.

Two sections beyond the old report:
  * 🆕 new providers  = ids in the FreeRouter catalog we had not seen before
  * 📦 pool           = what is actually registered in LiteLLM right now,
                        read from /model/info, plus FreeRouter's own health
                        state (which is a real chat test, unlike a /models GET)

Key VALUES never leave this machine; only names and statuses are messaged.

Runs automatically every Saturday 19:00 (see
~/Library/LaunchAgents/com.litellm.provider-watch.plist).

  python3 check_new_providers.py            # scheduled run (notify on news/gaps)
  python3 check_new_providers.py --dry-run  # print message, change nothing
  python3 check_new_providers.py --send     # send the full report unconditionally
  python3 check_new_providers.py --force "text"  # send an arbitrary test message
  python3 check_new_providers.py --reset     # re-seed baseline, notify nothing

Env: N8N_NOTIFY_WEBHOOK_URL (required to send).
State: ./data/provider_watch.json  (seen provider ids + last check)
Reads: ./freerouter/providers/*.yaml, ./freerouter/state/health.json, ./.env
"""

import concurrent.futures
import json
import logging
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("provider-watch")

ROOT = Path(__file__).parent
STATE_PATH = ROOT / "data" / "provider_watch.json"
PROVIDERS_DIR = ROOT / "freerouter" / "providers"
HEALTH_PATH = ROOT / "freerouter" / "state" / "health.json"
GATEWAY_URL = "http://localhost:4000"

WEBHOOK_URL = os.environ.get("N8N_NOTIFY_WEBHOOK_URL", "")
HEADERS = {"User-Agent": "litellm-proxy/provider-watch"}

# FreeRouter expects these names, but .env stores the upstream ones. docker-compose.yml
# bridges them (ZHIPU_API_KEY=${ZAI_API_KEY}, HUGGINGFACE_API_KEY=${HF_TOKEN}), so a
# report keyed on the FreeRouter name has to look the .env name up.
DOTENV_ALIASES = {
    "ZHIPU_API_KEY": "ZAI_API_KEY",
    "HUGGINGFACE_API_KEY": "HF_TOKEN",
}

# Only these statuses are routable upstream; paused/retired/watch providers are
# reported separately instead of being nagged about as "missing keys".
ROUTABLE_STATUSES = {"active"}

# The Parrot webhook's Telegram node parses the text as Markdown even though
# parse_mode is not in the node's parameters (proven empirically, see
# md_safe below). Two consequences, both of which bit us:
#   * `_AI_` renders as italics, so LITELLM_MASTER_KEY arrives as
#     LITELLMMASTERKEY -- which is why old reports looked mangled.
#   * a line with an ODD number of underscores is unbalanced emphasis and
#     Telegram rejects the whole send with 400 "Bad request - please check
#     your parameters". `• B.AI (B_AI_API_KEY)` has three and killed the
#     first run of this report.
# Wrapping identifiers in backticks makes them code spans, which Markdown
# leaves alone: underscores survive *and* the emphasis stays balanced.
_MD_TOKEN = re.compile(r"(?<![`\w])[A-Za-z0-9_]*_[A-Za-z0-9_]*(?![`\w])")


def env_label(var):
    """Render an env var name so Markdown cannot mangle or choke on it."""
    return f"`{var}`" if var and var != "-" else var


def md_safe(text):
    """Last-resort guard: neutralise any identifier left with unbalanced underscores.

    md_safe is idempotent and only touches tokens that actually contain an
    underscore, so it cannot mangle prose. It exists so a future provider whose
    credential name has an odd underscore count cannot silently break the send
    again -- the failure is invisible from here, because the webhook acks 200
    before Telegram is called and only n8n's Error Flow notices.
    """
    out = []
    for line in text.splitlines():
        if "_" in line and line.count("_") % 2 == 1:
            line = _MD_TOKEN.sub(lambda m: f"`{m.group(0)}`", line)
        out.append(line)
    return "\n".join(out)


# ── inputs ──────────────────────────────────────────────────────────────────
def load_dotenv():
    """KEY=VALUE pairs from .env (values stay local, never messaged)."""
    env = {}
    try:
        for line in (ROOT / ".env").read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    except Exception as e:  # noqa: BLE001
        log.warning(f"Could not read .env: {e}")
    return env


def dotenv_name(fr_name):
    """The .env key that actually backs a FreeRouter credential name."""
    return DOTENV_ALIASES.get(fr_name, fr_name)


def _scalar(text, key):
    m = re.search(rf"^{key}:\s*(.+?)\s*$", text, re.M)
    return m.group(1).strip().strip("\"'") if m else None


def _block(text, key):
    """The indented body of a top-level mapping key."""
    m = re.search(rf"^{key}:\n((?:[ \t]+\S.*\n?)*)", text, re.M)
    return m.group(1) if m else ""


def parse_provider(path):
    """Pull the handful of fields we need out of one provider YAML.

    Hand-rolled rather than PyYAML: the host python3 has no yaml module and these
    files are machine-generated with a fixed shape, so this keeps the script
    dependency-free (as it was before) and immune to a broken local venv.
    """
    text = path.read_text()

    discovery = {}
    for line in _block(text, "discovery").splitlines():
        m = re.match(r"\s+([A-Za-z_]+):\s*(.+?)\s*$", line)
        if m:
            discovery[m.group(1)] = m.group(2).strip().strip("\"'")

    extras_block = _block(text, "extra_credentials")
    extras = re.findall(r"^\s+-\s+([A-Z][A-Z0-9_]+)\s*$", extras_block, re.M)

    limits = {}
    for line in _block(text, "limits").splitlines():
        m = re.match(r"\s+([A-Za-z_]+):\s*(.+?)\s*$", line)
        if m:
            limits[m.group(1)] = m.group(2).strip().strip("\"'")

    return {
        "id": _scalar(text, "id"),
        "name": _scalar(text, "name") or _scalar(text, "id"),
        "status": _scalar(text, "status") or "active",
        "tier": _scalar(text, "tier") or "",
        "credential": _scalar(text, "credential"),
        "extra_credentials": extras,
        "max_models": _scalar(text, "max_models"),
        "rpm": limits.get("rpm"),
        "mode": discovery.get("mode", "static"),
        "url": discovery.get("url"),
        "auth": discovery.get("auth", "bearer"),
    }


def load_providers():
    if not PROVIDERS_DIR.is_dir():
        raise RuntimeError(f"{PROVIDERS_DIR} not found - is the FreeRouter checkout present?")
    out = []
    for f in sorted(PROVIDERS_DIR.glob("*.yaml")):
        p = parse_provider(f)
        if p["id"]:
            out.append(p)
    return out


def load_health():
    try:
        return json.loads(HEALTH_PATH.read_text())
    except Exception:  # noqa: BLE001
        return {}


def gateway_deployments(env):
    """What LiteLLM actually has registered, via the admin API.

    This is the ground truth for the pool section: /model/info reports the
    model_name each deployment is reachable under plus the freerouter_provider
    tag, which is how we can tell a genuinely merged alias (one name, several
    providers) from two separate routes that merely look similar.
    """
    key = env.get("LITELLM_MASTER_KEY", "")
    if not key:
        return None
    req = urllib.request.Request(
        f"{GATEWAY_URL}/model/info",
        headers={**HEADERS, "Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read())
    except Exception as e:  # noqa: BLE001
        log.warning(f"/model/info unavailable ({e}); pool section will be partial")
        return None
    rows = []
    for entry in data.get("data", []):
        info = entry.get("model_info") or {}
        if not str(info.get("id", "")).startswith("fr-"):
            continue
        rows.append(
            {
                "name": entry.get("model_name"),
                "provider": info.get("freerouter_provider"),
                "rpm": (entry.get("litellm_params") or {}).get("rpm"),
            }
        )
    return rows


# ── probing ─────────────────────────────────────────────────────────────────
def _fill_url(url, env, provider):
    """Substitute ${VAR} from .env using the provider's extra credentials."""
    for name in provider["extra_credentials"]:
        val = env.get(dotenv_name(name), "")
        if not val:
            return None  # required extra credential is blank -> treat as missing
        url = url.replace("${" + name + "}", val)
    return url


def probe(provider, env):
    """Classify one provider. Returns (status, detail)."""
    cred = provider["credential"]
    if cred:
        key = env.get(dotenv_name(cred), "")
        if not key:
            return "missing", None
    else:
        key = ""

    if not provider["url"]:
        # static model list: there is no catalog endpoint to hit. FreeRouter's own
        # chat probes are the real evidence, so defer to them.
        return "no-endpoint", None

    url = _fill_url(provider["url"], env, provider)
    if url is None:
        return "missing", None

    auth = provider["auth"]
    if auth == "none" or not key:
        # Open catalog: reachable says nothing about whether our key works.
        return _get(url, None, None), "public"
    if auth == "query_key":
        sep = "&" if "?" in url else "?"
        return _get(f"{url}{sep}key={urllib.parse.quote(key)}", None, None), None
    return _get(url, f"Bearer {key}", None), None


def _get(url, authorization, extra, timeout=15):
    headers = dict(HEADERS)
    if authorization:
        headers["Authorization"] = authorization
    if extra:
        headers.update(extra)
    try:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return "ok" if 200 <= r.status < 300 else "unreachable"
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return "invalid"
        if e.code == 429:
            return "rate_limited"
        return "unreachable"
    except Exception:  # noqa: BLE001
        return "unreachable"


def key_health(providers, env):
    """{provider_id: (display_name, dotenv_var, status, note)}"""
    result = {}

    def one(p):
        status, note = probe(p, env)
        var = dotenv_name(p["credential"]) if p["credential"] else "-"
        return p["id"], (p["name"], var, status, note)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        for pid, row in ex.map(one, providers):
            result[pid] = row
    return result


def enrich_from_health(health_rows, health_state):
    """Let FreeRouter's own chat probes override the catalog probe.

    A 200 from a /models endpoint only proves the catalog is readable. Cerebras
    answers 200 while every chat call 402s, and NVIDIA NIM's catalog is public so
    a 200 says nothing about our key having any provisioned endpoint. FreeRouter
    already sends a real chat completion to every model each cycle and records the
    outcome, so that is both the stronger signal and already paid for.
    """
    models = (health_state or {}).get("models") or {}
    errors = (health_state or {}).get("provider_errors") or {}
    for pid, (name, var, status, note) in health_rows.items():
        if status == "missing":
            continue  # no key, so there is nothing to chat-test
        err = errors.get(pid, "")
        if re.search(r"401|403|invalid api key|unauthor|authenticat", err, re.I):
            health_rows[pid] = (name, var, "invalid", None)
            continue
        mine = [m for m in models.values() if m.get("provider") == pid]
        if not mine:
            continue
        healthy = [m for m in mine if m.get("status") == "healthy"]
        failed = [m for m in mine if m.get("status") == "quarantined"]
        untested = [m for m in mine if m.get("status") == "unknown"]
        if healthy:
            note_txt = f"chat-probed {len(healthy)}/{len(mine)}"
            # keep a 401/429 verdict: a working key still rate-limits
            keep = status if status in ("invalid", "rate_limited") else "ok"
            health_rows[pid] = (name, var, keep, note_txt)
        elif len(failed) >= 2:
            # Catalog said fine, chat says otherwise. Trust chat.
            detail = f"0/{len(mine)} models pass chat probes"
            if re.search(r"402|payment|billing|credit", err, re.I) or any(
                "402" in (m.get("last_error") or "") for m in failed
            ):
                detail += " (402 billing)"
            health_rows[pid] = (name, var, "unreachable", detail)
        elif untested and not failed:
            # Discovered but never chat-probed (FreeRouter spends a per-cycle probe
            # budget). Reporting this as "reachable" would overstate what we know.
            health_rows[pid] = (name, var, "public", f"{len(untested)} models, none probed yet")
        elif err:
            health_rows[pid] = (name, var, "unreachable", "discovery error")


# ── state ───────────────────────────────────────────────────────────────────
def load_state():
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:  # noqa: BLE001
        return {}


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True))


def notify(text):
    # Same pattern as ~/poke/check_disk_space.sh: message as ?message= query param
    # into the proven Parrot webhook (do NOT point this at a hand-built workflow —
    # n8n 2.x kept executing a stale definition when we tried).
    if not WEBHOOK_URL:
        log.error("N8N_NOTIFY_WEBHOOK_URL not set — cannot notify.")
        return False
    url = f"{WEBHOOK_URL}?message={urllib.parse.quote(text)}"
    req = urllib.request.Request(url, data=b"", headers=HEADERS, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            log.info(f"Webhook responded {r.status}")
            return 200 <= r.status < 300
    except Exception as e:  # noqa: BLE001
        log.error(f"Webhook POST failed: {e}")
        return False


# ── report ──────────────────────────────────────────────────────────────────
def build_message(new_ids, health_rows, providers, deployments, health_state):
    active = [p for p in providers if p["status"] in ROUTABLE_STATUSES]
    other = [p for p in providers if p["status"] not in ROUTABLE_STATUSES]

    lines = ["📡 Weekly LLM-provider status (FreeRouter)"]

    if new_ids:
        lines.append("\n🆕 New in the FreeRouter catalog:")
        for pid in sorted(new_ids):
            p = next((x for x in providers if x["id"] == pid), None)
            tier = f" [{p['tier']}]" if p and p["tier"] else ""
            lines.append(f"• {pid}{tier} — add its key to .env and to model-sync in docker-compose.yml")
    else:
        lines.append("\n🆕 No new providers.")

    def collect(want):
        return sorted(
            (row[0], row[1], row[3])
            for pid, row in health_rows.items()
            if row[2] == want
            and pid in {p["id"] for p in active}
        )

    missing = collect("missing")
    invalid = collect("invalid")
    ok = collect("ok")
    degraded = collect("unreachable")
    limited = collect("rate_limited")
    unprobeable = collect("no-endpoint")
    public = collect("public")

    if missing:
        lines.append("\n🔑 Missing keys (blank in .env):")
        for name, var, _ in missing:
            lines.append(f"• {name} ({env_label(var)})")
    if invalid:
        lines.append("\n⚠️ Invalid keys (401 — rotate):")
        for name, var, _ in invalid:
            lines.append(f"• {name} ({env_label(var)})")
    if not missing and not invalid:
        lines.append("\n🔑 All configured keys present and valid.")
    if degraded:
        lines.append("\n🟠 Configured but failing:")
        for name, var, note in degraded:
            lines.append(f"• {name} ({env_label(var)}) — {note or 'unreachable'}")
    if public:
        lines.append("\n🔓 Reachable, but not proven working:")
        for name, var, note in public:
            lines.append(f"• {name} — {note or 'open catalog, key untested'}")
    if unprobeable:
        lines.append(f"\nℹ️ Static model lists, judged by FreeRouter probes: "
                     f"{', '.join(n for n, _, _ in unprobeable)}")
    if limited:
        lines.append(f"⏳ Rate limited (fine): {', '.join(n for n, _, _ in limited)}")

    reach = [n for n, _, _ in ok] + [n for n, _, _ in public] + [n for n, _, _ in limited]
    reach += [n for n, _, _ in unprobeable]
    # carry the chat-probe ratio through, it is the part that actually matters
    ratios = {}
    for name, _var, note in list(ok) + list(unprobeable) + list(public):
        m = re.match(r"chat-probed (\d+)/(\d+)", note or "")
        if m:
            ratios[name] = f"{m.group(1)}/{m.group(2)}"
    shown = sorted({f"{n} ({ratios[n]})" if n in ratios else n for n in reach})
    lines.append(f"\n✅ Reachable: {', '.join(shown) if shown else 'none'}.")
    lines.append("(ratios = models that actually pass a real chat probe)")

    if deployments:
        by_prov = {}
        for d in deployments:
            by_prov.setdefault(d["provider"] or "?", set()).add(d["name"])
        merged = {}
        for d in deployments:
            merged.setdefault(d["name"], set()).add(d["provider"])
        cross = sorted(n for n, provs in merged.items() if len(provs) > 1)
        models = (health_state or {}).get("models") or {}
        healthy = sum(1 for m in models.values() if m.get("status") == "healthy")
        quarantined = sum(1 for m in models.values() if m.get("status") == "quarantined")
        lines.append("\n📦 Pool in LiteLLM")
        lines.append(
            f"• {len(deployments)} deployments / {len(merged)} names across "
            f"{len(by_prov)} providers: {', '.join(sorted(by_prov))}"
        )
        lines.append(f"• FreeRouter health: {healthy} healthy, {quarantined} quarantined")
        if cross:
            lines.append(f"• Merged across providers: {', '.join(cross)}")
        else:
            lines.append("• Merged across providers: none yet")
    else:
        lines.append("\n📦 Pool in LiteLLM: gateway unreachable, skipped.")

    if other:
        detail = ", ".join(f"{p['id']} ({p['status']})" for p in sorted(other, key=lambda x: x["id"]))
        lines.append(f"\n🚫 Not routable upstream: {detail}")

    lint = pool_lint_brief()
    if lint:
        lines.append(f"\n🧹 {lint}")
        lines.append("(details: python3 pool_lint.py — fixes per POOL_GUIDE.md)")

    return md_safe("\n".join(lines))


def pool_lint_brief():
    """One-line tidiness summary from pool_lint.py (merge suggestions, thin roles, ...)."""
    import subprocess

    try:
        done = subprocess.run(
            [sys.executable, str(Path(__file__).with_name("pool_lint.py")), "--brief"],
            capture_output=True, text=True, timeout=300, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return done.stdout.strip().splitlines()[-1] if done.stdout.strip() else ""


# ── main ────────────────────────────────────────────────────────────────────
def main(argv):
    dry_run = "--dry-run" in argv
    if "--force" in argv:
        i = argv.index("--force")
        text = argv[i + 1] if len(argv) > i + 1 else "Provider-watch test message ✅"
        ok = notify(text)
        sys.exit(0 if ok else 1)

    env = load_dotenv()
    try:
        providers = load_providers()
    except Exception as e:  # noqa: BLE001
        log.error(f"Could not read the FreeRouter catalog: {e}")
        sys.exit(2)
    log.info(f"FreeRouter catalog lists {len(providers)} providers")

    state = load_state()
    if "--reset" in argv:
        state = {"catalog_seen": sorted(p["id"] for p in providers),
                 "last_check": datetime.now(timezone.utc).isoformat()}
        if not dry_run:
            save_state(state)
        log.info(f"Baseline re-seeded with {len(providers)} providers")
        return

    seen = set(state.get("catalog_seen") or [])
    if not seen:
        # First run on the new state shape: seed silently, notify nothing, so the
        # switchover from the freellm.net catalog does not fire a false "new providers".
        state = {"catalog_seen": sorted(p["id"] for p in providers),
                 "last_check": datetime.now(timezone.utc).isoformat()}
        if not dry_run:
            save_state(state)
        log.info(f"First run: baseline seeded with {len(providers)} providers, no notification")
        return

    new = {p["id"] for p in providers} - seen
    gone = seen - {p["id"] for p in providers}
    if gone:
        log.info(f"No longer in the catalog: {sorted(gone)}")

    health_state = load_health()
    rows = key_health(providers, env)
    enrich_from_health(rows, health_state)
    deployments = gateway_deployments(env)

    log.info(
        "keys: %d ok, %d missing, %d invalid, %d public, %d degraded",
        sum(1 for r in rows.values() if r[2] == "ok"),
        sum(1 for r in rows.values() if r[2] == "missing"),
        sum(1 for r in rows.values() if r[2] == "invalid"),
        sum(1 for r in rows.values() if r[2] == "public"),
        sum(1 for r in rows.values() if r[2] == "unreachable"),
    )

    force_send = "--send" in argv
    gaps = [r for r in rows.values() if r[2] in ("missing", "invalid", "unreachable")]
    if not new and not gaps and not force_send:
        log.info("Nothing new, keys all good — staying silent.")
        state["last_check"] = datetime.now(timezone.utc).isoformat()
        if not dry_run:
            save_state(state)
        return

    text = build_message(new, rows, providers, deployments, health_state)
    if dry_run:
        print(text)
        return
    if notify(text):
        state["catalog_seen"] = sorted(p["id"] for p in providers)
        state["last_check"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
    else:
        sys.exit(1)  # keep old state so the next run retries


if __name__ == "__main__":
    main(sys.argv[1:])
