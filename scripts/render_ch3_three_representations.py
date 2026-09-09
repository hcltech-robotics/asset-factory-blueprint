#!/usr/bin/env python3
"""Render the source views used by Theory Figure 3.1.

Run with Blender 5.2 or later:

    blender --background --factory-startup \
      --python scripts/render_ch3_three_representations.py -- \
      --source-usd /path/to/A23DMOD_USD.usd \
      --output-dir docs/assets/theory

The source package must retain its ``textures`` directory beside the USD file.
The script imports the authored mesh, wires all four supplied 2K maps, and uses
one fixed camera and lighting rig for the visual, collision and mass-model
views. It writes transparent PNGs and no intermediate ``.blend`` file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

import bpy
from mathutils import Vector


TEXTURE_FILES = {
    "albedo": "A23DMAT_001_Albedo.jpg",
    "normal": "A23DMAT_001_Normal.jpg",
    "roughness": "A23DMAT_001_Roughness.jpg",
    "specular": "A23DMAT_001_Specular Level.jpg",
}

OUTPUT_FILES = {
    "visual": "ch3-crate-visual.png",
    "collision": "ch3-crate-collision.png",
    "mass": "ch3-crate-mass.png",
}


def script_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-usd", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--width", type=int, default=900)
    parser.add_argument("--height", type=int, default=720)
    argv = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    args = parser.parse_args(argv)
    if args.width < 320 or args.height < 320:
        parser.error("render dimensions must be at least 320 pixels")
    return args


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def require_sources(usd_path: Path) -> dict[str, Path]:
    usd_path = usd_path.expanduser().resolve()
    if not usd_path.is_file():
        raise FileNotFoundError(f"USD source not found: {usd_path}")
    texture_dir = usd_path.parent / "textures"
    textures = {name: texture_dir / filename for name, filename in TEXTURE_FILES.items()}
    missing = [str(path) for path in textures.values() if not path.is_file()]
    if missing:
        raise FileNotFoundError("missing source textures: " + ", ".join(missing))
    return textures


def set_socket(node: bpy.types.Node, name: str, value: object) -> None:
    socket = node.inputs.get(name)
    if socket is not None:
        socket.default_value = value


def new_material(name: str) -> tuple[bpy.types.Material, bpy.types.NodeTree, bpy.types.Node]:
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    tree = material.node_tree
    assert tree is not None
    tree.nodes.clear()
    output = tree.nodes.new("ShaderNodeOutputMaterial")
    output.location = (620, 0)
    return material, tree, output


def visual_material(textures: dict[str, Path]) -> bpy.types.Material:
    material, tree, output = new_material("Ch3 visual surface")
    shader = tree.nodes.new("ShaderNodeBsdfPrincipled")
    shader.location = (340, 0)
    set_socket(shader, "Metallic", 0.0)
    set_socket(shader, "Roughness", 0.55)
    tree.links.new(shader.outputs["BSDF"], output.inputs["Surface"])

    texcoord = tree.nodes.new("ShaderNodeTexCoord")
    texcoord.location = (-920, 0)

    albedo = tree.nodes.new("ShaderNodeTexImage")
    albedo.label = "Supplied 2K albedo"
    albedo.location = (-650, 250)
    albedo.image = bpy.data.images.load(str(textures["albedo"]), check_existing=True)
    albedo.image.colorspace_settings.name = "sRGB"
    tree.links.new(texcoord.outputs["UV"], albedo.inputs["Vector"])
    tree.links.new(albedo.outputs["Color"], shader.inputs["Base Color"])

    roughness = tree.nodes.new("ShaderNodeTexImage")
    roughness.label = "Supplied 2K roughness"
    roughness.location = (-650, 30)
    roughness.image = bpy.data.images.load(str(textures["roughness"]), check_existing=True)
    roughness.image.colorspace_settings.name = "Non-Color"
    tree.links.new(texcoord.outputs["UV"], roughness.inputs["Vector"])
    tree.links.new(roughness.outputs["Color"], shader.inputs["Roughness"])

    normal_texture = tree.nodes.new("ShaderNodeTexImage")
    normal_texture.label = "Supplied 2K tangent-space normal"
    normal_texture.location = (-650, -200)
    normal_texture.image = bpy.data.images.load(str(textures["normal"]), check_existing=True)
    normal_texture.image.colorspace_settings.name = "Non-Color"
    tree.links.new(texcoord.outputs["UV"], normal_texture.inputs["Vector"])
    normal_map = tree.nodes.new("ShaderNodeNormalMap")
    normal_map.location = (40, -190)
    normal_map.inputs["Strength"].default_value = 0.72
    tree.links.new(normal_texture.outputs["Color"], normal_map.inputs["Color"])
    tree.links.new(normal_map.outputs["Normal"], shader.inputs["Normal"])

    specular = tree.nodes.new("ShaderNodeTexImage")
    specular.label = "Supplied 2K specular level"
    specular.location = (-650, -420)
    specular.image = bpy.data.images.load(str(textures["specular"]), check_existing=True)
    specular.image.colorspace_settings.name = "Non-Color"
    tree.links.new(texcoord.outputs["UV"], specular.inputs["Vector"])
    specular_input = shader.inputs.get("Specular IOR Level")
    if specular_input is None:
        raise RuntimeError("the active Principled BSDF has no Specular IOR Level input")
    tree.links.new(specular.outputs["Color"], specular_input)
    return material


def technical_material(
    name: str,
    base_colour: tuple[float, float, float, float],
    metallic: float,
    roughness: float,
    emission_colour: tuple[float, float, float, float] | None = None,
    emission_strength: float = 0.0,
) -> bpy.types.Material:
    material, tree, output = new_material(name)
    shader = tree.nodes.new("ShaderNodeBsdfPrincipled")
    shader.location = (300, 0)
    set_socket(shader, "Base Color", base_colour)
    set_socket(shader, "Metallic", metallic)
    set_socket(shader, "Roughness", roughness)
    if emission_colour is not None:
        set_socket(shader, "Emission Color", emission_colour)
        set_socket(shader, "Emission Strength", emission_strength)
    tree.links.new(shader.outputs["BSDF"], output.inputs["Surface"])
    return material


def emission_material(
    name: str,
    colour: tuple[float, float, float, float],
    strength: float,
) -> bpy.types.Material:
    material, tree, output = new_material(name)
    emission = tree.nodes.new("ShaderNodeEmission")
    emission.location = (300, 0)
    emission.inputs["Color"].default_value = colour
    emission.inputs["Strength"].default_value = strength
    tree.links.new(emission.outputs["Emission"], output.inputs["Surface"])
    return material


def assign_material(obj: bpy.types.Object, material: bpy.types.Material) -> None:
    obj.data.materials.clear()
    obj.data.materials.append(material)


def duplicate_mesh(source: bpy.types.Object, name: str) -> bpy.types.Object:
    duplicate = source.copy()
    duplicate.data = source.data.copy()
    duplicate.name = name
    bpy.context.collection.objects.link(duplicate)
    duplicate.animation_data_clear()
    return duplicate


def edge_curve(
    source: bpy.types.Object,
    name: str,
    material: bpy.types.Material,
    bevel_depth: float,
) -> bpy.types.Object:
    """Build a stable renderable curve from the source mesh's actual edges."""
    curve = bpy.data.curves.new(name, type="CURVE")
    curve.dimensions = "3D"
    curve.resolution_u = 1
    curve.bevel_depth = bevel_depth
    curve.bevel_resolution = 0
    curve.resolution_v = 0
    curve.materials.append(material)
    vertices = source.data.vertices
    for edge in source.data.edges:
        spline = curve.splines.new("POLY")
        spline.points.add(1)
        for point, vertex_index in zip(spline.points, edge.vertices):
            coordinate = vertices[vertex_index].co
            point.co = (coordinate.x, coordinate.y, coordinate.z, 1.0)
    obj = bpy.data.objects.new(name, curve)
    bpy.context.collection.objects.link(obj)
    obj.matrix_world = source.matrix_world.copy()
    return obj


def apply_modifier(obj: bpy.types.Object, modifier: bpy.types.Modifier) -> None:
    bpy.ops.object.select_all(action="DESELECT")
    obj.select_set(True)
    bpy.context.view_layer.objects.active = obj
    result = bpy.ops.object.modifier_apply(modifier=modifier.name)
    if "FINISHED" not in result:
        raise RuntimeError(f"failed to apply {modifier.name} to {obj.name}")


def look_at(obj: bpy.types.Object, target: Vector) -> None:
    obj.rotation_euler = (target - obj.location).to_track_quat("-Z", "Y").to_euler()


def add_area_light(
    name: str,
    location: tuple[float, float, float],
    energy: float,
    size: float,
    colour: tuple[float, float, float],
    target: Vector,
) -> bpy.types.Object:
    data = bpy.data.lights.new(name=name, type="AREA")
    data.energy = energy
    data.shape = "DISK"
    data.size = size
    data.color = colour
    obj = bpy.data.objects.new(name, data)
    bpy.context.collection.objects.link(obj)
    obj.location = location
    look_at(obj, target)
    return obj


def configure_scene(width: int, height: int, centre: Vector) -> bpy.types.Object:
    scene = bpy.context.scene
    scene.render.engine = "BLENDER_EEVEE"
    scene.render.resolution_x = width
    scene.render.resolution_y = height
    scene.render.resolution_percentage = 100
    scene.render.film_transparent = True
    scene.render.image_settings.file_format = "PNG"
    scene.render.image_settings.color_mode = "RGBA"
    scene.render.image_settings.color_depth = "8"
    scene.render.image_settings.compression = 58
    scene.render.use_file_extension = True

    scene.view_settings.look = "AgX - Medium High Contrast"
    scene.view_settings.exposure = -0.50

    world = bpy.data.worlds.new("Ch3 transparent world")
    world.use_nodes = True
    background = world.node_tree.nodes.get("Background")
    assert background is not None
    background.inputs["Color"].default_value = (0.018, 0.024, 0.040, 1.0)
    background.inputs["Strength"].default_value = 0.20
    scene.world = world

    camera_data = bpy.data.cameras.new("Ch3 camera")
    camera_data.type = "ORTHO"
    camera_data.ortho_scale = 0.88
    camera_data.lens = 70
    camera = bpy.data.objects.new("Ch3 camera", camera_data)
    bpy.context.collection.objects.link(camera)
    camera.location = (1.04, -1.18, 0.78)
    look_at(camera, centre + Vector((0.0, 0.0, 0.015)))
    scene.camera = camera

    add_area_light(
        "Ch3 key",
        (1.35, -1.65, 1.95),
        410.0,
        1.1,
        (1.0, 0.80, 0.66),
        centre,
    )
    add_area_light(
        "Ch3 fill",
        (-1.55, -0.65, 1.15),
        265.0,
        1.35,
        (0.52, 0.72, 1.0),
        centre,
    )
    add_area_light(
        "Ch3 rim",
        (-0.10, 1.70, 1.55),
        340.0,
        1.0,
        (0.62, 0.88, 1.0),
        centre,
    )
    return camera


def render_view(
    output_path: Path,
    visible: tuple[bpy.types.Object, ...],
    renderables: tuple[bpy.types.Object, ...],
) -> None:
    visible_set = set(visible)
    for obj in renderables:
        obj.hide_render = obj not in visible_set
    bpy.context.scene.render.filepath = str(output_path)
    bpy.ops.render.render(write_still=True)
    if not output_path.is_file():
        raise RuntimeError(f"render was not written: {output_path}")


def main() -> None:
    args = script_arguments()
    usd_path = args.source_usd.expanduser().resolve()
    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    textures = require_sources(usd_path)

    bpy.ops.wm.read_factory_settings(use_empty=True)
    result = bpy.ops.wm.usd_import(filepath=str(usd_path))
    if "FINISHED" not in result:
        raise RuntimeError(f"USD import failed: {usd_path}")
    meshes = [obj for obj in bpy.context.scene.objects if obj.type == "MESH"]
    if len(meshes) != 1:
        raise RuntimeError(f"expected one mesh in source USD, found {len(meshes)}")
    visual = meshes[0]
    visual.name = "Ch3 visual source"

    world_corners = [visual.matrix_world @ Vector(corner) for corner in visual.bound_box]
    minimum = Vector(tuple(min(point[i] for point in world_corners) for i in range(3)))
    maximum = Vector(tuple(max(point[i] for point in world_corners) for i in range(3)))
    centre = (minimum + maximum) * 0.5
    configure_scene(args.width, args.height, centre)

    assign_material(visual, visual_material(textures))

    collision = duplicate_mesh(visual, "Ch3 mesh-derived collider")
    decimate = collision.modifiers.new("Deterministic collision reduction", "DECIMATE")
    decimate.decimate_type = "COLLAPSE"
    decimate.ratio = 0.58
    decimate.use_collapse_triangulate = True
    apply_modifier(collision, decimate)
    triangulate = collision.modifiers.new("Triangulated collision surface", "TRIANGULATE")
    triangulate.quad_method = "SHORTEST_DIAGONAL"
    triangulate.ngon_method = "BEAUTY"
    apply_modifier(collision, triangulate)
    assign_material(
        collision,
        technical_material(
            "Ch3 collider fill",
            (0.018, 0.30, 0.235, 1.0),
            0.25,
            0.30,
            (0.018, 0.16, 0.13, 1.0),
            0.18,
        ),
    )
    collision_wire = edge_curve(
        collision,
        "Ch3 collider topology",
        emission_material("Ch3 collider edges", (0.08, 0.98, 0.72, 1.0), 2.4),
        0.0015,
    )

    mass = duplicate_mesh(visual, "Ch3 analytical mass geometry")
    assign_material(
        mass,
        technical_material(
            "Ch3 mass geometry",
            (0.19, 0.055, 0.58, 1.0),
            0.34,
            0.27,
            (0.08, 0.015, 0.28, 1.0),
            0.12,
        ),
    )
    mass_wire = edge_curve(
        mass,
        "Ch3 mass geometry edges",
        emission_material("Ch3 mass edges", (0.62, 0.34, 1.0, 1.0), 1.35),
        0.00075,
    )

    renderables = (visual, collision, collision_wire, mass, mass_wire)
    render_view(output_dir / OUTPUT_FILES["visual"], (visual,), renderables)
    render_view(
        output_dir / OUTPUT_FILES["collision"],
        (collision, collision_wire),
        renderables,
    )
    render_view(output_dir / OUTPUT_FILES["mass"], (mass, mass_wire), renderables)

    report = {
        "blender": bpy.app.version_string,
        "source": {"name": usd_path.name, "sha256": sha256(usd_path)},
        "textures": {name: {"name": path.name, "sha256": sha256(path)} for name, path in sorted(textures.items())},
        "source_mesh": {
            "vertices": len(visual.data.vertices),
            "polygons": len(visual.data.polygons),
            "bounds_metres": {
                "minimum": list(minimum),
                "maximum": list(maximum),
            },
        },
        "collision_mesh": {
            "vertices": len(collision.data.vertices),
            "polygons": len(collision.data.polygons),
            "method": "Blender deterministic collapse decimation at ratio 0.58, then triangulation",
        },
        "outputs": {
            name: {
                "name": filename,
                "sha256": sha256(output_dir / filename),
            }
            for name, filename in OUTPUT_FILES.items()
        },
        "render": {
            "engine": bpy.context.scene.render.engine,
            "width": args.width,
            "height": args.height,
            "transparent": bpy.context.scene.render.film_transparent,
        },
    }
    print("CH3_RENDER_REPORT=" + json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
