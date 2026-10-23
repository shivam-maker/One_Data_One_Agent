# One User, One Agent: API, replay and tests in one image.
# The data pack is NOT copied into the image (it is personal data); it is mounted at /data/pack at run time.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src \
    OUA_PACK=/data/pack \
    OUA_STATE_DB=/app/api_state/state.db

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY src/ src/
COPY tests/ tests/
COPY pyproject.toml README.md ARCHITECTURE.md KNOWN_LIMITATIONS.md AI_COLLABORATION.md ./

RUN useradd --create-home app && mkdir -p /app/sample_run /app/api_state && chown -R app /app
USER app

EXPOSE 8000

ENTRYPOINT ["python", "-m", "oua"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
