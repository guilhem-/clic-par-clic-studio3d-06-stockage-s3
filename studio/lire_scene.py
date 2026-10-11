"""Lit une scène Blender téléchargée depuis le studio (module s3d06), sans rien exécuter de ce qu'elle contient.

Sur le poste, dans un conteneur sans réseau ni bibliothèque d'assets :
    docker run --rm --network none -v "$PWD:/scene:ro" studio3d:5 /opt/blender/blender -b --factory-startup \\
      --disable-autoexec /scene/salon-1.blend --python /app/lire_scene.py
Affiche : fichier, version de Blender, objets, monde, images empaquetées, scripts, marquage.
"""
from pathlib import Path

import bpy

f = Path(bpy.data.filepath)
scene = bpy.context.scene
objets = [o for o in scene.objects if o.type in ("MESH", "EMPTY")]
racines = sorted({o.get("asset_studio") for o in scene.objects if o.get("asset_studio")})
images = [im for im in bpy.data.images if im.source in ("FILE", "SEQUENCE", "TILED")]
manquantes = [im.name for im in images if not im.packed_file]
pilotes = sum(1 for b in (*bpy.data.objects, *bpy.data.materials, *bpy.data.worlds, *bpy.data.node_groups, *bpy.data.scenes)
              for a in [getattr(b, "animation_data", None)] if a for d in a.drivers if d.driver.type == "SCRIPTED")
marque = scene.get("ai_studio3d")

print(f"Fichier      : {f.name} ({f.stat().st_size / 1e6:.1f} Mo), Blender {'.'.join(map(str, bpy.data.version))}")
print(f"Objets       : {len(objets)} dont assets : {', '.join(racines) or 'aucun'}")
print(f"Monde        : {scene.world.name if scene.world else 'aucun'}")
print(f"Images       : {len(images) - len(manquantes)}/{len(images)} empaquetées{' (manquantes : ' + ', '.join(manquantes) + ')' if manquantes else ''}")
print(f"Scripts      : {len(bpy.data.texts)} texte, {pilotes} expression Python")
if marque:
    print(f"Marquage     : {marque['digitalSourceType']}, {marque['generateur']}")
    print(f"               {marque['identifiant']}, {marque['date']}")
else:
    print("Marquage     : absent")
