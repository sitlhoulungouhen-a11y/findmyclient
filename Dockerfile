# Run on a VPS whose provider allows outbound port 25 (check: `nc -vz gmail-smtp-in.l.google.com 25`).
# docker build -t findmyclient . && docker run --rm -it -v $PWD/data:/data -p 127.0.0.1:8765:8765 findmyclient <command>
FROM python:3.13-slim
WORKDIR /app
COPY pyproject.toml README.md ./
COPY findmyclient ./findmyclient
RUN pip install --no-cache-dir .
ENV FMC_DB=/data/findmyclient.db
WORKDIR /data
VOLUME /data
ENTRYPOINT ["findmyclient"]
