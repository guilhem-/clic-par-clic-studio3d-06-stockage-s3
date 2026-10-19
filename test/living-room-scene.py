# Test scene (module s3d06, off-camera tests): the library's armchair and vase, wooden lounge lighting.
import bpy
bpy.ops.mesh.primitive_plane_add(size=6)
bpy.context.object.name = "Decor floor"
load_asset("armchair", position=(0, 0, 0))
load_asset("ceramic_vase", position=(0.8, -0.3, 0))
environment("wooden_lounge")
