# Oracle: trusted, sealed comparator. Read-only rootfs at runtime (see compose).
# Build context is the repository root, so one code's captures can be copied in.
FROM python:3.12-slim

# Deliberately minimal, and deliberately NOT the `equivalent` package: this
# service reads capture files and a manifest with numpy and yaml alone, so
# nothing the rest of the project changes can change what it decides.
RUN pip install --no-cache-dir fastapi==0.115.* "uvicorn[standard]==0.30.*" numpy==2.* pydantic==2.* PyYAML

WORKDIR /app
COPY services/oracle /app/services/oracle

# The comparator, which is the same file the gateway uses to compare a
# port's whole-program outputs (equivalent/capture/compare.py). It is
# copied rather than installed because this image installs nothing of the
# package -- one file of pure numpy is what this service needs, and there
# is one of it, so the two sides cannot drift into two definitions of
# "the same answer".
COPY equivalent/capture/compare.py /app/services/oracle/compare.py

# Which code this oracle answers for. One image holds one code's answers, so
# switching codes is a rebuild, which is also a human decision.
ARG CODE=tsunami

# The code's whole directory is BAKED IN: its manifest, its captures, and
# the source tree that holds its tolerance policy. It is part of the
# image, not a mounted volume, so no other container can reach it and
# changing it requires rebuilding this image (a git commit + human
# sign-off). The manifest is what says which outputs have to have a
# tolerance band before this service will answer anything.
#
# The whole directory rather than three named paths, because a code that
# has not been brought in yet has only some of them, and this image has
# to build for that code too: the oracle starts anyway and reports that
# it is not ready.
COPY programs/${CODE} /program

EXPOSE 7070
CMD ["uvicorn", "services.oracle.serve:app", "--host", "0.0.0.0", "--port", "7070"]
