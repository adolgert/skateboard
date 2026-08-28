# Oracle: trusted, sealed comparator. Read-only rootfs at runtime (see compose).
# Build context is the repository root, so one code's captures can be copied in.
FROM python:3.12-slim

# Deliberately minimal, and deliberately NOT the `equivalent` package: this
# service reads capture files and a manifest with numpy and yaml alone, so
# nothing the rest of the project changes can change what it decides.
RUN pip install --no-cache-dir fastapi==0.115.* "uvicorn[standard]==0.30.*" numpy==2.* pydantic==2.* PyYAML

WORKDIR /app
COPY services/oracle /app/services/oracle

# Which code this oracle answers for. One image holds one code's answers, so
# switching codes is a rebuild, which is also a human decision.
ARG CODE=tsunami

# Reference answers, the tolerance policy, and the code's manifest are BAKED
# IN. They are part of the image, not a mounted volume, so no other container
# can reach them and changing them requires rebuilding this image (a git
# commit + human sign-off). The manifest is here because the oracle refuses to
# start unless every output the code declares has a tolerance band.
COPY programs/${CODE}/captures /captures
COPY programs/${CODE}/tolerances.json /tolerances.json
COPY programs/${CODE}/manifest.yaml /manifest.yaml

EXPOSE 7070
CMD ["uvicorn", "services.oracle.serve:app", "--host", "0.0.0.0", "--port", "7070"]
