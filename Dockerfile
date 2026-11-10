FROM python:3.13-alpine@sha256:7984cf56ceaddebe4fefeb75d64000b9f45d8b77b11a09b24524da6cdc3af0c8 AS assets
COPY build/ /build/
RUN python /build/fetch_assets.py /downloads

FROM python:3.13-alpine@sha256:7984cf56ceaddebe4fefeb75d64000b9f45d8b77b11a09b24524da6cdc3af0c8 AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TOKENIZERS_PARALLELISM=false RAYON_NUM_THREADS=1 PYTHONPATH=/app
COPY --from=assets /downloads/ /opt/spi-tokenizer/
COPY build/LICENSE-2.0.txt /opt/spi-tokenizer/LICENSE-2.0.txt
COPY build/MODEL-README.md /opt/spi-tokenizer/README.md
COPY build/legal/ /opt/spi-tokenizer/legal/
RUN python -m pip install --no-index --no-deps /opt/spi-tokenizer/*.whl && rm /opt/spi-tokenizer/*.whl
COPY build/library_manifest.py /tmp/library_manifest.py
RUN python /tmp/library_manifest.py && rm /tmp/library_manifest.py
COPY backend/ /app/
COPY frontend/ /ui/
RUN mkdir -p /app/spi && ln -s ../github_broker.py /app/spi/github_broker.py \
 && chmod 0555 /app /app/spi /ui /opt/spi-tokenizer \
 && chmod 0444 /app/*.py /ui/* /opt/spi-tokenizer/*.json /opt/spi-tokenizer/*.jinja
USER 10001:10001
RUN python -c 'from local_token_counter import LocalCounter; LocalCounter(); import jwt; from cryptography.hazmat.primitives.asymmetric import rsa'
WORKDIR /app
EXPOSE 8080
CMD ["python", "-B", "/app/service.py"]

# Node is an offline contract-test dependency, never part of the release image.
FROM runtime AS test
USER root
RUN apk add --no-cache nodejs
COPY backend/ /workspace/backend/
COPY frontend/ /workspace/frontend/
COPY integrations/ /workspace/integrations/
COPY tests/ /workspace/tests/
COPY .github/workflows/ci.yml /workspace/.github/workflows/ci.yml
COPY scripts/test_all.py /workspace/scripts/test_all.py
COPY scripts/harbor_ci_auth.py /workspace/scripts/harbor_ci_auth.py
RUN chmod -R a+rX /workspace
USER 10001:10001
WORKDIR /workspace
CMD ["python", "-B", "scripts/test_all.py"]

FROM runtime AS release
