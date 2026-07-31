# Builder: NVIDIA HPC SDK (nvfortran + compute-sanitizer + nsys) plus a thin HTTP
# shim. Build context is the demo/ directory so we can copy the trusted capture
# harness in alongside the service code.
FROM nvcr.io/nvidia/nvhpc:25.9-devel-cuda13.0-ubuntu24.04

# nvfortran / compute-sanitizer / nsys are already on PATH in this image.
# It ships no python, so add a minimal one for the service shim.
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-pip \
 && rm -rf /var/lib/apt/lists/*
RUN pip3 install --break-system-packages --no-cache-dir \
      "fastapi==0.115.*" "uvicorn[standard]==0.30.*" "pydantic==2.*"

RUN mkdir -p /opt/harness /work

# Trusted, baked replay harness -- NOT from the agent's payload.
COPY capture/mod_capture.f90 capture/replay.f90 /opt/harness/
# Service code (all gate command lines live in stages.py).
COPY builder/app.py builder/stages.py /app/

WORKDIR /app
EXPOSE 9090
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "9090"]
