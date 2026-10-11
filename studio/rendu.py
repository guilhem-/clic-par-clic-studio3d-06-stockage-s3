"""Exécuté par Blender sans interface : blender -b --factory-startup --python rendu.py -- <dossier>.

Vide la scène, exécute scene.py (écrit par le modèle), ajoute au besoin caméra et lumière,
rend en Cycles sur processeur (1280 x 720, débruitage) et exporte le modèle 3D (GLB) sans le
décor. Écrit resultat.json ({"erreur": …} en cas d'échec).

Module s3d05 : le code du modèle dispose de deux fonctions de la bibliothèque d'assets (dossier
ASSETS, monté en lecture seule) : charger_asset("nom") et environnement("nom"). Sans bibliothèque,
le rendu se comporte comme avant.
Module s3d06 : si SCENE_BLEND=1, la scène complète est aussi enregistrée telle quelle (brut.blend) ;
le worker la fait nettoyer, marquer et empaqueter par un Blender neuf (empaqueter.py).
"""
import json
import os
import sys
import textwrap
import traceback
from pathlib import Path

import bpy
from mathutils import Vector

dossier = Path(sys.argv[sys.argv.index("--") + 1])
resultat = dossier / "resultat.json"


def ecrire(**info):
    resultat.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")


def cadrer(cam, objets):
    """Place la caméra pour voir tous les objets (vue de trois quarts, légèrement plongeante)."""
    points = [o.matrix_world @ v.co for o in objets for v in o.data.vertices]
    if not points:
        return
    bas = Vector((min(p.x for p in points), min(p.y for p in points), min(p.z for p in points)))
    haut = Vector((max(p.x for p in points), max(p.y for p in points), max(p.z for p in points)))
    centre = (bas + haut) / 2
    direction = Vector((1.0, -1.3, 0.8)).normalized()
    rotation = (-direction).to_track_quat("-Z", "Y")
    cam.rotation_euler = rotation.to_euler()
    # Recul juste suffisant pour que chaque sommet entre dans le cadre (16:9), avec une marge.
    tx = 18 / cam.data.lens                      # demi-largeur du capteur (36 mm) / focale
    ty = tx * 720 / 1280
    inverse = rotation.inverted()
    locaux = [inverse @ (p - centre) for p in points]  # repère de la caméra : elle regarde vers -z
    sx = sy = 0.0                                # décalage de la caméra dans son plan, pour centrer
    for _ in range(4):
        recul = max(max(z + abs(x - sx) / tx, z + abs(y - sy) / ty) for x, y, z in locaux) * 1.1
        us = [(x - sx) / (recul - z) for x, y, z in locaux]
        vs = [(y - sy) / (recul - z) for x, y, z in locaux]
        sx += (max(us) + min(us)) / 2 * recul
        sy += (max(vs) + min(vs)) / 2 * recul
    cam.location = centre + rotation @ Vector((sx, sy, recul))


# ---------------------------------------------------------------- Bibliothèque d'assets (module s3d05)

ASSETS = Path(os.environ.get("ASSETS", "/assets"))
_fiche = ASSETS / "catalogue.json"
CATALOGUE = json.loads(_fiche.read_text(encoding="utf-8")) if _fiche.exists() else {"modeles": {}, "environnements": {}}
ASSETS_UTILISES: list[str] = []  # la trace : rangée avec la demande, puis dans le marquage
_modeles_charges: dict = {}
_environnement = False


def charger_asset(nom: str, position=(0, 0, 0), rotation_z: float = 0.0, echelle: float = 1.0, decor: bool = False):
    """Ajoute à la scène une copie d'un objet de la bibliothèque, posé au sol ; renvoie son objet racine."""
    fiche = CATALOGUE["modeles"].get(nom)
    if fiche is None:  # le message revient au modèle, qui corrige et resoumet
        raise ValueError(f"asset inconnu : {nom!r}. Noms valides : {', '.join(sorted(CATALOGUE['modeles'])) or 'aucun'}")
    if nom not in _modeles_charges:  # une seule lecture du fichier, même pour dix chaises
        chemin = ASSETS / fiche["fichier"]  # construit par le studio, jamais par le modèle
        # Ajouté (link=False), pas lié : une copie que le modèle peut déplacer et recolorer.
        with bpy.data.libraries.load(str(chemin), link=False, assets_only=True) as (source, cible):
            cible.collections = [fiche["collection"]]
        _modeles_charges[nom] = cible.collections[0]
    modele = _modeles_charges[nom]
    premiere = nom not in ASSETS_UTILISES  # première fois : les objets ajoutés eux-mêmes
    copies = {o: o if premiere else o.copy() for o in modele.all_objects}  # ensuite : copies, maillages partagés
    for original, copie in copies.items():
        if not premiere:  # la copie d'un enfant suit la copie de son parent
            copie.parent = copies.get(original.parent)
        if decor:  # rendu, mais pas exporté dans le modèle 3D
            copie.name = f"Decor_{original.name}"
        bpy.context.scene.collection.objects.link(copie)
    racine = next(c for o, c in copies.items() if o.parent is None)
    racine.location = position
    racine.rotation_euler.z = rotation_z
    racine.scale = (echelle,) * 3
    racine["asset_studio"] = nom  # repère pour l'export et la trace
    ASSETS_UTILISES.append(nom)
    return racine


def environnement(nom: str, intensite: float = 1.0, rotation_z: float = 0.0, fond_visible: bool = True):
    """Éclaire la scène par une image HDRI de la bibliothèque : une photo panoramique d'un vrai lieu."""
    global _environnement
    fiche = CATALOGUE["environnements"].get(nom)
    if fiche is None:
        raise ValueError(f"environnement inconnu : {nom!r}. Noms valides : {', '.join(sorted(CATALOGUE['environnements'])) or 'aucun'}")
    monde = bpy.data.worlds.new(f"Monde {nom}")
    bpy.context.scene.world = monde
    monde.use_nodes = True
    arbre = monde.node_tree
    arbre.nodes.clear()
    coord, rotation = arbre.nodes.new("ShaderNodeTexCoord"), arbre.nodes.new("ShaderNodeMapping")
    texture, fond = arbre.nodes.new("ShaderNodeTexEnvironment"), arbre.nodes.new("ShaderNodeBackground")
    sortie = arbre.nodes.new("ShaderNodeOutputWorld")
    texture.image = bpy.data.images.load(str(ASSETS / fiche["fichier"]), check_existing=True)
    rotation.inputs["Rotation"].default_value[2] = rotation_z
    fond.inputs["Strength"].default_value = intensite
    arbre.links.new(coord.outputs["Generated"], rotation.inputs["Vector"])
    arbre.links.new(rotation.outputs["Vector"], texture.inputs["Vector"])
    arbre.links.new(texture.outputs["Color"], fond.inputs["Color"])
    if fond_visible:
        arbre.links.new(fond.outputs["Background"], sortie.inputs["Surface"])
    else:  # la caméra voit un gris neutre ; la scène reste éclairée par le HDRI
        gris, melange = arbre.nodes.new("ShaderNodeBackground"), arbre.nodes.new("ShaderNodeMixShader")
        chemin = arbre.nodes.new("ShaderNodeLightPath")
        gris.inputs["Color"].default_value = (0.8, 0.82, 0.85, 1)
        arbre.links.new(chemin.outputs["Is Camera Ray"], melange.inputs["Fac"])
        arbre.links.new(fond.outputs["Background"], melange.inputs[1])
        arbre.links.new(gris.outputs["Background"], melange.inputs[2])
        arbre.links.new(melange.outputs["Shader"], sortie.inputs["Surface"])
    _environnement = True
    ASSETS_UTILISES.append(nom)


# ---------------------------------------------------------------- Rendu

try:
    for o in list(bpy.data.objects):
        bpy.data.objects.remove(o, do_unlink=True)
    # Tolère une marque BOM et une indentation commune (code recopié d'un bloc indenté par le modèle).
    code = textwrap.dedent((dossier / "scene.py").read_text(encoding="utf-8-sig"))
    # Les deux fonctions de la bibliothèque sont simplement placées dans l'espace du code du modèle.
    exec(compile(code, "scene.py", "exec"), {"bpy": bpy, "__name__": "__scene__",
                                            "charger_asset": charger_asset, "environnement": environnement})

    scene = bpy.context.scene
    bpy.context.view_layer.update()  # positions à jour (objets déplacés par leur racine) avant le cadrage
    maillages = [o for o in scene.objects if o.type == "MESH"]
    if not maillages:
        raise RuntimeError("la scène ne contient aucun objet")
    if scene.camera is None:
        cam = bpy.data.objects.new("Camera", bpy.data.cameras.new("Camera"))
        scene.collection.objects.link(cam)
        scene.camera = cam
        cadrer(cam, [o for o in maillages if not o.name.startswith("Decor")] or maillages)
    if not _environnement and not any(o.type == "LIGHT" for o in scene.objects):
        soleil = bpy.data.objects.new("Soleil", bpy.data.lights.new("Soleil", "SUN"))
        soleil.data.energy = 3
        soleil.rotation_euler = (0.8, 0.2, 0.6)
        scene.collection.objects.link(soleil)

    # Lumière d'ambiance de base (fond gris clair) : une scène peu éclairée par le modèle reste lisible.
    # Avec un HDRI de la bibliothèque, c'est lui qui éclaire : on n'y touche pas.
    if not _environnement:
        if scene.world is None:
            scene.world = bpy.data.worlds.new("Monde")
        scene.world.use_nodes = True
        fond = next((n for n in scene.world.node_tree.nodes if n.type == "BACKGROUND"), None)
        if fond is not None:
            fond.inputs[0].default_value = (0.8, 0.82, 0.85, 1)
            fond.inputs[1].default_value = max(fond.inputs[1].default_value, 0.7)

    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = 48
    scene.cycles.use_denoising = True
    scene.render.resolution_x, scene.render.resolution_y, scene.render.resolution_percentage = 1280, 720, 100
    scene.render.image_settings.file_format = "PNG"
    scene.render.filepath = str(dossier / "rendu.png")
    bpy.ops.render.render(write_still=True)

    # Export du sujet : maillages hors décor, et racines des assets (pour garder leur hiérarchie).
    bpy.ops.object.select_all(action="DESELECT")
    for o in scene.objects:
        o.select_set((o.type == "MESH" and not o.name.startswith("Decor"))
                     or (o.type == "EMPTY" and "asset_studio" in o and not any(e.name.startswith("Decor") for e in o.children)))
    bpy.ops.export_scene.gltf(filepath=str(dossier / "modele.glb"), export_format="GLB", use_selection=True,
                              export_image_format="WEBP")  # textures des assets : un GLB plus léger
    if os.environ.get("SCENE_BLEND") == "1":  # module s3d06 : la scène entière, décor compris, encore brute
        bpy.ops.wm.save_as_mainfile(filepath=str(dossier / "brut.blend"), copy=True)
    ecrire(ok=True, objets=len(maillages), assets=sorted(set(ASSETS_UTILISES)), bibliotheque=CATALOGUE.get("version"))
except Exception:
    ecrire(erreur=traceback.format_exc()[-1500:])
