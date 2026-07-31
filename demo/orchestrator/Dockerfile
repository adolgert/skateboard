# Orchestrator: trusted core. Build context is the demo/ directory so it can bake
# in the pristine work-tree (the seed) and the visible input dataset.
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir "requests==2.*"

# pristine ch04 work-tree (the seed the agent ports) and the visible inputs
COPY work /seed
COPY orchestrator/datasets /datasets
COPY orchestrator/orchestrator.py /app/orchestrator.py

WORKDIR /app
CMD ["python", "orchestrator.py"]
