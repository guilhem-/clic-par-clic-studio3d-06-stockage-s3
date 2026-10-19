"""Reads a Blender scene downloaded from the studio (module s3d06), without running anything it contains.

On the workstation, in a container without network or asset library:
    docker run --rm --network none -v "$PWD:/scene:ro" studio3d:5 /opt/blender/blender -b --factory-startup \\
      --disable-autoexec /scene/living-room-1.blend --python /app/read_scene.py
Prints: file, Blender version, objects, world, packed images, scripts, marking.
"""
from pathlib import Path

import bpy

f = Path(bpy.data.filepath)
scene = bpy.context.scene
objects = [o for o in scene.objects if o.type in ("MESH", "EMPTY")]
roots = sorted({o.get("studio_asset") for o in scene.objects if o.get("studio_asset")})
images = [im for im in bpy.data.images if im.source in ("FILE", "SEQUENCE", "TILED")]
missing = [im.name for im in images if not im.packed_file]
drivers = sum(1 for b in (*bpy.data.objects, *bpy.data.materials, *bpy.data.worlds, *bpy.data.node_groups, *bpy.data.scenes)
              for a in [getattr(b, "animation_data", None)] if a for d in a.drivers if d.driver.type == "SCRIPTED")
mark = scene.get("ai_studio3d")

print(f"File         : {f.name} ({f.stat().st_size / 1e6:.1f} MB), Blender {'.'.join(map(str, bpy.data.version))}")
print(f"Objects      : {len(objects)} including assets: {', '.join(roots) or 'none'}")
print(f"World        : {scene.world.name if scene.world else 'none'}")
print(f"Images       : {len(images) - len(missing)}/{len(images)} packed{' (missing: ' + ', '.join(missing) + ')' if missing else ''}")
print(f"Scripts      : {len(bpy.data.texts)} text, {drivers} Python expression")
if mark:
    print(f"Marking      : {mark['digitalSourceType']}, {mark['generator']}")
    print(f"               {mark['id']}, {mark['date']}")
else:
    print("Marking      : none")
