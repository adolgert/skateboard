# The gateway: the reference monitor. It holds the git repository, the ledger,
# and the analyzer, and it is the only service the agent's session can reach.
# Build context is the repository root, because the image installs the package
# from the checkout.
FROM python:3.12-slim

# git builds every submitted tree with plumbing commands; curl is what the
# container healthcheck uses to ask /healthz.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git curl \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY pyproject.toml /src/pyproject.toml
COPY equivalent /src/equivalent

# The analyzer a strategy names is a module of this package, run as
# `python3 -m equivalent.analyzers.check_sese`, so a plain install puts it
# where the subprocess finds it whatever directory it starts in.
RUN pip install --no-cache-dir /src

# The strategy files live at their own mount point so that an operator can
# mount a different set over them without rebuilding this image.
RUN mkdir -p /strategies \
 && cp /src/equivalent/strategy/files/*.yaml /strategies/

EXPOSE 8000
CMD ["equivalent-gateway"]
