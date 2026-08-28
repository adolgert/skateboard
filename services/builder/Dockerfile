# Builder: NVIDIA HPC SDK (nvfortran + compute-sanitizer + nsys) plus a thin HTTP
# shim. Build context is the repository root, so the trusted harness that lives
# beside the service code is copied in with it.
FROM nvcr.io/nvidia/nvhpc:25.9-devel-cuda13.0-ubuntu24.04

# nvfortran / compute-sanitizer / nsys are already on PATH in this image.
# It ships no python, so add a minimal one for the service shim. make, cmake,
# and fpm are here because a code's build is its own: the harness runs the
# tree's makefile, and that makefile may be a few lines driving cmake or fpm.
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-pip make cmake curl \
 && rm -rf /var/lib/apt/lists/*
# pytest, hypothesis and numpy are here for the property stage: a code's own
# module of invariants is a pytest file, run by this interpreter against the
# replay binary. Pinned by major version like the service's own dependencies.
RUN pip3 install --break-system-packages --no-cache-dir \
      "fastapi==0.115.*" "uvicorn[standard]==0.30.*" "pydantic==2.*" \
      "pytest==8.*" "hypothesis==6.*" "numpy==2.*"

# fpm ships as one static binary. Pinned by version and checked by digest: the
# builder runs it on submitted code, so what lands here is not whatever the
# release page holds today.
ARG FPM_VERSION=0.12.0
ARG FPM_SHA256=576fc54fb829263b32d0e288535a2a3edc9d0478dffec84ba70445446f6d6abf
RUN curl -fsSL -o /usr/local/bin/fpm \
      "https://github.com/fortran-lang/fpm/releases/download/v${FPM_VERSION}/fpm-${FPM_VERSION}-linux-x86_64-gcc-12" \
 && echo "${FPM_SHA256}  /usr/local/bin/fpm" | sha256sum -c - \
 && chmod +x /usr/local/bin/fpm

RUN mkdir -p /opt/harness /work

# The trusted, baked harness -- NOT from the agent's payload. fc-shim is the
# compiler a submitted makefile is handed; npy_io.f90 is the capture format's
# Fortran side, which a code's own replay driver compiles against.
# harness_properties.py is the third: the library a code's property module
# imports, which turns "run the region on these inputs" into an invocation of
# the replay binary. It is baked rather than sent with the code, so what that
# phrase means cannot be redefined by the thing under test.
COPY services/builder/harness/npy_io.f90 services/builder/harness/fc-shim \
     services/builder/harness/harness_properties.py /opt/harness/
RUN chmod +x /opt/harness/fc-shim
# Service code (the build contract and the gate command lines live here).
COPY services/builder /app/services/builder

# The comparator, which is the same file the oracle is built around and the
# same one the gateway compares a program's own outputs with
# (equivalent/capture/compare.py). The mutation stage scores every mutant with
# it, so what it means for a mutant to have been noticed is what it means for
# a port to have been noticed. Copied rather than installed because this image
# installs no Python package of this project.
COPY equivalent/capture/compare.py /app/services/builder/compare.py

WORKDIR /app
EXPOSE 9090
CMD ["uvicorn", "services.builder.app:app", "--host", "0.0.0.0", "--port", "9090"]
