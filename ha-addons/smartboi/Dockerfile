FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# Installs the smartboi package straight from this repository's git history,
# sidestepping the Home Assistant add-on build-context limitation (a
# Dockerfile can only COPY files from its own add-on folder, not the rest
# of the repo). See TradingBot's Dockerfile for the same pattern.
#
# Pinned to a specific commit SHA, not a branch name -- `pip install
# git+URL@branch` is cached by Docker based on the literal Dockerfile text,
# not by what's actually at the remote branch tip, so leaving this as a
# branch name means "Rebuild" can silently keep reusing an old cached
# layer. Bumping the SHA below is what forces a fresh fetch.
#
# IMPORTANT: after pushing new commits you intend to deploy, update
# SMARTBOI_COMMIT to the new commit SHA, then use the add-on's "Rebuild"
# button (not just "Restart") in the Home Assistant UI.
ARG SMARTBOI_COMMIT=6ab990606afe5ac3aa1884e0d52db85e0c255f0b
RUN pip install --no-cache-dir \
    "smartboi @ git+https://github.com/Stullee/SmartBoi.git@${SMARTBOI_COMMIT}"

# Mirrors config.yaml's version -- bump both together on every release.
ARG SMARTBOI_VERSION=0.67.0
ENV SMARTBOI_VERSION=${SMARTBOI_VERSION}
ENV SMARTBOI_COMMIT=${SMARTBOI_COMMIT}

WORKDIR /app
COPY _addon_options.py /app/_addon_options.py
COPY addon_entrypoint.py /app/addon_entrypoint.py

CMD ["python", "addon_entrypoint.py"]
