# Oracle: trusted, sealed comparator. Read-only rootfs at runtime (see compose).
# Build context is the repository root, so one code's captures can be copied in.
FROM python:3.12-slim

RUN pip install --no-cache-dir fastapi==0.115.* "uvicorn[standard]==0.30.*" numpy==2.* pydantic==2.*

WORKDIR /app
COPY services/oracle/app.py services/oracle/compare.py /app/

# Which code this oracle answers for. One image holds one code's answers, so
# switching codes is a rebuild, which is also a human decision.
ARG CODE=tsunami

# Reference answers and the tolerance policy are BAKED IN. They are part of the
# image, not a mounted volume, so no other container can reach them and changing
# them requires rebuilding this image (a git commit + human sign-off).
COPY programs/${CODE}/captures /captures
COPY programs/${CODE}/tolerances.json /tolerances.json

EXPOSE 7070
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7070"]
