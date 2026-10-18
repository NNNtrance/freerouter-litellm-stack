# FreeRouter model registrar, packaged for the litellm-proxy stack.
#
# Upstream ships no Dockerfile: its own compose.yml runs the refresher inside
# the LiteLLM image and bind-mounts ./freerouter, ./providers and ./state. This
# image is the equivalent for the model-sync service, which must talk to a
# *pre-existing* external LiteLLM gateway instead of bringing its own up.
#
# Build context is this directory (./freerouter), so every COPY below is
# relative to the FreeRouter checkout root.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

WORKDIR /app

# The three runtime deps pinned by pyproject.toml. Kept explicit (no uv.lock /
# pyproject install) so the image does not need the dev group or a build backend.
RUN pip install --no-cache-dir \
      "pydantic>=2.11,<3" \
      "PyYAML>=6.0,<7" \
      "typing-extensions>=4.14,<5"

# The package plus the provider catalog. providers/*.yaml is what turns a
# platform's free tier into routable models, so it is baked in on purpose:
# after `freerouter_pull` in compose.sh, `docker compose build model-sync`
# is the step that actually picks up new providers.
COPY freerouter/ ./freerouter/
COPY providers/ ./providers/
# Pool policy defaults; compose bind-mounts ./freerouter/policy over it so edits
# apply without a rebuild.
COPY policy/ ./policy/

# Health state (which models probed OK, quarantine, backoff) must survive a
# container restart, otherwise every rebuild re-probes the whole catalog.
# compose.sh bind-mounts ./freerouter/state over this path.
RUN mkdir -p /app/state

# `daemon` = discovery + probe + reconcile loop on FREEROUTER_REFRESH_INTERVAL.
# Overridden to `refresh` for a one-shot run.
ENTRYPOINT ["python", "-m", "freerouter"]
CMD ["daemon"]
