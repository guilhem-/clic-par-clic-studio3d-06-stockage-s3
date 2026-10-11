"""Module s3d06 — la scène à livrer, préparée par un Blender neuf qui n'a jamais exécuté le code du modèle :
    blender -b --factory-startup --disable-autoexec brut.blend --python empaqueter.py -- <dossier>

Le code du modèle a construit la scène dans un autre Blender (rendu.py). Celui-ci ouvre le fichier sans
en lancer les scripts, le nettoie (rien ne doit s'exécuter chez l'utilisateur), y inscrit le marquage
(marquage.json, écrit par le worker), empaquette textures et éclairage, puis enregistre scene.blend,
compressé. Bilan dans empaquetage.json.
"""
import json
import sys
from pathlib import Path

import bpy

dossier = Path(sys.argv[sys.argv.index("--") + 1])
i = json.loads((dossier / "marquage.json").read_text(encoding="utf-8"))
bilan = {"textes": 0, "pilotes": 0}

# 1. Aucun script : ni texte (un texte « Register » s'exécuterait à l'ouverture, si l'utilisateur
#    autorise les scripts), ni pilote à expression Python.
for t in list(bpy.data.texts):
    bpy.data.texts.remove(t)
    bilan["textes"] += 1
for famille in ("objects", "meshes", "materials", "worlds", "node_groups", "scenes", "lights", "cameras", "shape_keys"):
    for bloc in getattr(bpy.data, famille):
        for porteur in (bloc, getattr(bloc, "node_tree", None)):
            ad = getattr(porteur, "animation_data", None)
            for pilote in list(ad.drivers if ad else []):
                if pilote.driver.type == "SCRIPTED":
                    ad.drivers.remove(pilote)
                    bilan["pilotes"] += 1

# 2. Tout en local : aucune bibliothèque liée (le poste de l'utilisateur ne l'a pas).
for famille in ("objects", "meshes", "materials", "collections", "node_groups", "images", "worlds"):
    for bloc in getattr(bpy.data, famille):
        if bloc.library:
            bloc.make_local()
for lib in list(bpy.data.libraries):
    bpy.data.libraries.remove(lib)

# 3. Le marquage du module 4, dans le fichier lui-même.
bpy.context.scene["ai_studio3d"] = {
    "digitalSourceType": "trainedAlgorithmicMedia", "generateur": "AI studio3D",
    "identifiant": f"urn:uuid:{i['identifiant']}", "date": i["date"],
    "assets": ", ".join(f"{a['nom']} ({a['source']}, {a['licence']})" for a in i.get("assets", [])),
}

# 4. Textures et éclairage HDRI dans le fichier : il s'ouvre partout, sans la bibliothèque.
bpy.ops.file.pack_all()
images = [im for im in bpy.data.images if im.source in ("FILE", "SEQUENCE", "TILED")]
bilan["images"] = len(images)
bilan["non_empaquetees"] = [im.name for im in images if not im.packed_file]
bpy.ops.wm.save_as_mainfile(filepath=str(dossier / "scene.blend"), compress=True, copy=True)
(dossier / "empaquetage.json").write_text(json.dumps(bilan, ensure_ascii=False), encoding="utf-8")
