# The agent's container: untrusted. It has the HPC compilers and a GPU so a
# person's session can try things locally, and it can reach the model provider.
# Nothing it does here is evidence -- only what the gateway checks is. Build
# context is the repository root, because the image needs the extension source
# and the isolation check, which live in different directories of the checkout.
FROM nvcr.io/nvidia/nvhpc:25.9-devel-cuda13.0-ubuntu24.04

ARG NODE_MAJOR=22
ARG PI_VERSION=0.84.3
# The base image already has an account at uid 1000. The agent account takes
# that uid so that files it writes into the mounted working copy belong to the
# person who started the deployment rather than to root.
ARG AGENT_UID=1000

# The base image ships neither curl nor node; the isolation check needs curl.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl git gnupg \
 && curl -fsSL "https://deb.nodesource.com/setup_${NODE_MAJOR}.x" | bash - \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/*

RUN npm install -g "@earendil-works/pi-coding-agent@${PI_VERSION}"

# The extension is loaded as TypeScript; there is no build step. Only its
# runtime dependency is installed -- the agent packages it imports types from
# are needed by the type checker, not by the running session.
WORKDIR /opt/pi-extension
COPY pi-extension/package.json pi-extension/package-lock.json ./
RUN npm ci --omit=dev
COPY pi-extension/src ./src

COPY deploy/isolation_check.sh /opt/isolation_check.sh
RUN chmod +x /opt/isolation_check.sh

RUN userdel --remove ubuntu 2>/dev/null || true \
 && useradd --create-home --uid "${AGENT_UID}" --shell /bin/bash agent \
 && mkdir -p /working /sessions /home/agent/.pi \
 && chown agent:agent /working /sessions /home/agent/.pi

USER agent
ENV HOME=/home/agent
ENV EQUIVALENT_GATEWAY_URL=http://gateway:8000
ENV EQUIVALENT_REGION=ch04:step
WORKDIR /working

# The session files land on the mounted directory so they can be read back
# beside the gateway's request log; the command line wins over both the
# environment variable and settings.json, so this is the one place that says it.
CMD ["pi", "--extension", "/opt/pi-extension/src/extension.ts", "--session-dir", "/sessions"]
