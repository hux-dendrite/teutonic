FROM rclone/rclone:1.74.2 AS rclone

FROM python:3.11-slim

COPY --from=rclone /usr/local/bin/rclone /usr/local/bin/rclone

RUN pip install --no-cache-dir \
    "bittensor>=10.3,<11" \
    "psycopg[binary]>=3.2,<3.3" \
    "httpx>=0.27,<0.29" \
    "numpy>=1.26,<3" \
    "boto3>=1.34,<2" \
    "PyNaCl>=1.5,<2" \
    "jsonschema>=4.22,<5"

WORKDIR /app
ENV PYTHONPATH=/app

CMD ["sh", "-c", "python -m unittest discover -s tests/unit -v && python -m unittest discover -s tests/integration -v"]
