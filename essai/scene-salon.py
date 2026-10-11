# Scène d'essai (module s3d06, essais hors champ) : fauteuil et vase de la bibliothèque, éclairage salon bois.
import bpy
bpy.ops.mesh.primitive_plane_add(size=6)
bpy.context.object.name = "Decor sol"
charger_asset("fauteuil", position=(0, 0, 0))
charger_asset("vase_ceramique", position=(0.8, -0.3, 0))
environnement("salon_bois")
