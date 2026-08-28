# The gateway: the reference monitor. It holds the git repository, the ledger,
# and the analyzer, and it is the only service the agent's session can reach.
# Build context is the repository root, because the image needs the package and
# the analyzer script side by side in the same layout they have in the checkout.
FROM python:3.12-slim

# git builds every submitted tree with plumbing commands; curl is what the
# container healthcheck uses to ask /healthz.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY pyproject.toml /src/pyproject.toml
COPY equivalent /src/equivalent
COPY tools/regionharness /src/tools/regionharness

# Installed in place, on purpose. The analyzer is run as a subprocess whose
# working directory is the project root the package resolves from its own
# file location, and whose command line is the relative path
# tools/regionharness/check_sese.py. A copied-into-site-packages install would
# resolve that root to a directory where the analyzer is not present.
RUN pip install --no-cache-dir -e /src

# The strategy files live at their own mount point so that an operator can
# mount a different set over them without rebuilding this image.
RUN mkdir -p /strategies \
 && cp /src/equivalent/strategy/files/*.yaml /strategies/

EXPOSE 8000
CMD ["equivalent-gateway"]
