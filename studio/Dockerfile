# 3D studio at scale: one image for the MCP server (api.py) and the workers (worker.py),
# with headless Blender 5.2, the Redis client, the Prometheus client (metrics, module 3),
# Pillow and the DejaVu Sans font (marking of the renders, module 4); module 5: load_asset and
# environment (render.py), list_assets (api.py); the assets live in a separate image (assets/);
# module 6: boto3, storage.py (the storage team's S3 storage), package.py (scene cleaned and
# packed by a fresh Blender), read_scene.py (reading a downloaded scene, on the workstation).
#   docker build -t studio3d:5 studio
FROM python:3.12-slim

ARG BLENDER_VERSION=5.2.0
ARG BLENDER_SERIES=5.2
# download.blender.org refuses automated downloads (403): official mirror, checksum verified.
ARG BLENDER_MIRROR=https://ftp.nluug.nl/pub/graphics/blender/release
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl xz-utils \
      libx11-6 libxi6 libxxf86vm1 libxfixes3 libxrender1 libxkbcommon0 libsm6 libice6 \
      libgl1 libegl1 libglib2.0-0 libgomp1 \
  && cd /tmp \
  && curl -fsSLO "${BLENDER_MIRROR}/Blender${BLENDER_SERIES}/blender-${BLENDER_VERSION}-linux-x64.tar.xz" \
  && curl -fsSL "${BLENDER_MIRROR}/Blender${BLENDER_SERIES}/blender-${BLENDER_VERSION}.sha256" | grep linux-x64.tar.xz | sha256sum -c - \
  && tar -xJf "blender-${BLENDER_VERSION}-linux-x64.tar.xz" -C /opt \
  && rm "blender-${BLENDER_VERSION}-linux-x64.tar.xz" \
  && mv /opt/blender-${BLENDER_VERSION}-linux-x64 /opt/blender \
  && apt-get purge -y curl xz-utils && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

# Marking of the renders (module 4): font of the visible banner, same rendering everywhere.
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "mcp==1.20.0" "pydantic==2.11.7" "redis==5.2.1" "prometheus-client==0.26.0" "pillow==12.3.0" "boto3==1.43.111"

RUN useradd --create-home studio
WORKDIR /app
COPY common.py api.py worker.py render.py metrics.py marking.py storage.py package.py read_scene.py ./
USER studio
ENV BLENDER=/opt/blender/blender PYTHONUNBUFFERED=1
EXPOSE 8000 9101
# The manifest picks the role: "python api.py" (MCP server) or "python worker.py" (worker).
CMD ["python", "api.py"]
