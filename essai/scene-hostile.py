# Scène d'essai hostile (module s3d06, essai T5) : un texte « Register » et un pilote à expression Python.
import bpy
t = bpy.data.texts.new("piege.py")
t.write("print('ce script ne doit jamais partir chez l utilisateur')")
t.use_module = True
bpy.ops.mesh.primitive_cube_add(size=1, location=(0, 0, 0.5))
o = bpy.context.object
f = o.driver_add("location", 0)
f.driver.type = "SCRIPTED"
f.driver.expression = "__import__('os').getcwd() and 0"
