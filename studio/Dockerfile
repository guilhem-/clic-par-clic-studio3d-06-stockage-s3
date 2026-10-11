# Studio 3D à l'échelle : une image pour le serveur MCP (api.py) et les workers (travailleur.py),
# avec Blender 5.2 sans interface, le client Redis, le client Prometheus (mesures, module 3),
# Pillow et la police DejaVu Sans (marquage des rendus, module 4) ; module 5 : charger_asset et
# environnement (rendu.py), lister_assets (api.py) ; les assets sont dans une image à part ;
# module 6 : boto3, stockage.py (stockage S3 de l'équipe stockage), empaqueter.py (scène nettoyée et
# empaquetée par un Blender neuf), lire_scene.py (lecture d'une scène téléchargée, sur le poste).
#   docker build -t studio3d:5 studio
FROM python:3.12-slim

ARG BLENDER_VERSION=5.2.0
ARG BLENDER_SERIE=5.2
# download.blender.org refuse les téléchargements automatisés (403) : miroir officiel, empreinte vérifiée.
ARG BLENDER_MIROIR=https://ftp.nluug.nl/pub/graphics/blender/release
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl xz-utils \
      libx11-6 libxi6 libxxf86vm1 libxfixes3 libxrender1 libxkbcommon0 libsm6 libice6 \
      libgl1 libegl1 libglib2.0-0 libgomp1 \
  && cd /tmp \
  && curl -fsSLO "${BLENDER_MIROIR}/Blender${BLENDER_SERIE}/blender-${BLENDER_VERSION}-linux-x64.tar.xz" \
  && curl -fsSL "${BLENDER_MIROIR}/Blender${BLENDER_SERIE}/blender-${BLENDER_VERSION}.sha256" | grep linux-x64.tar.xz | sha256sum -c - \
  && tar -xJf "blender-${BLENDER_VERSION}-linux-x64.tar.xz" -C /opt \
  && rm "blender-${BLENDER_VERSION}-linux-x64.tar.xz" \
  && mv /opt/blender-${BLENDER_VERSION}-linux-x64 /opt/blender \
  && apt-get purge -y curl xz-utils && apt-get autoremove -y && rm -rf /var/lib/apt/lists/*

# Marquage des rendus (module 4) : police du bandeau visible, même rendu partout.
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir "mcp==1.20.0" "pydantic==2.11.7" "redis==5.2.1" "prometheus-client==0.26.0" "pillow==12.3.0" "boto3==1.43.111"

RUN useradd --create-home studio
WORKDIR /app
COPY commun.py api.py travailleur.py rendu.py metriques.py marquage.py stockage.py empaqueter.py lire_scene.py ./
USER studio
ENV BLENDER=/opt/blender/blender PYTHONUNBUFFERED=1
EXPOSE 8000 9101
# Le manifeste choisit le rôle : « python api.py » (serveur MCP) ou « python travailleur.py » (worker).
CMD ["python", "api.py"]
