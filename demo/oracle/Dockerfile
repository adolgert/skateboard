# Oracle: trusted, sealed comparator. Read-only rootfs at runtime (see compose).
FROM python:3.12-slim

RUN pip install --no-cache-dir fastapi==0.115.* "uvicorn[standard]==0.30.*" numpy==2.* pydantic==2.*

WORKDIR /app
COPY app.py compare.py /app/

# Reference answers and the tolerance policy are BAKED IN. They are part of the
# image, not a mounted volume, so no other container can reach them and changing
# them requires rebuilding this image (a git commit + human sign-off).
COPY captures /captures
COPY tolerances.json /tolerances.json

EXPOSE 7070
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7070"]
