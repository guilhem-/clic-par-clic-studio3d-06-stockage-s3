# Hostile test scene (module s3d06, test T5): a "Register" text and a driver with a Python expression.
import bpy
t = bpy.data.texts.new("trap.py")
t.write("print('this script must never reach the user')")
t.use_module = True
bpy.ops.mesh.primitive_cube_add(size=1, location=(0, 0, 0.5))
o = bpy.context.object
f = o.driver_add("location", 0)
f.driver.type = "SCRIPTED"
f.driver.expression = "__import__('os').getcwd() and 0"
