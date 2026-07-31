# Agent-runner: the untrusted component. Stateless, read-only rootfs at runtime.
FROM python:3.12-slim

RUN pip install --no-cache-dir \
      "fastapi==0.115.*" "uvicorn[standard]==0.30.*" "pydantic==2.*" \
      "anthropic>=0.69" "requests==2.*"

WORKDIR /app
COPY app.py /app/
COPY adapters /app/adapters/

EXPOSE 8080
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
