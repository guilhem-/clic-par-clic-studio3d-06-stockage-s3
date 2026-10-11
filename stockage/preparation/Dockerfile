# Préparation du stockage S3 (équipe stockage, module s3d06) : bucket, cycle de vie, politiques, comptes.
#   docker build -t stockage-preparation:1 services/stockage/preparation
# Python + boto3 (bucket, cycle de vie, essais) et le client rc de RustFS (comptes et politiques),
# téléchargé depuis sa publication GitHub, empreinte vérifiée.
FROM python:3.12-slim

ARG RC_VERSION=0.1.36
ARG RC_SHA256=4a8128911ccad4e7b481f26635a4cfd1ec064412210526e57ad2c748d356f3b7
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
  && cd /tmp \
  && curl -fsSLO "https://github.com/rustfs/cli/releases/download/v${RC_VERSION}/rustfs-cli-linux-amd64-v${RC_VERSION}.tar.gz" \
  && echo "${RC_SHA256}  rustfs-cli-linux-amd64-v${RC_VERSION}.tar.gz" | sha256sum -c - \
  && tar -xzf "rustfs-cli-linux-amd64-v${RC_VERSION}.tar.gz" \
  && install -m 0755 "$(find /tmp -type f -name rc | head -1)" /usr/local/bin/rc \
  && rm -rf /tmp/* && apt-get purge -y curl && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "boto3==1.43.111"
RUN useradd --create-home stockage
USER stockage
WORKDIR /app
COPY preparer.py ./
CMD ["python", "preparer.py"]
