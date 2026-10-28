"""Editable native seed projects for the V0.1 cases C01..C10.

Each seed is a typed `deep3d.spec/1` source with named parameters, Feature
relations and scene nodes. Acceptance numbers come from closed-form formulas of
the case dimensions (never from measuring generated geometry). Every seed also
declares a role binding: the registry's semantic roles (`stock.extrude`, ...)
expanded to exact Feature names. Bindings are written here, before any change
is executed, and are not derived from the invalidation code under test.
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from uuid import NAMESPACE_URL, uuid5

PRECISION = {
    "schema_version": "deep3d.precision/1",
    "working_tolerance_mm": 1e-05,
    "max_subshape_tolerance_mm": 0.001,
    "critical_dimension_error_mm": 0.001,
    "step_roundtrip_dimension_error_mm": 0.005,
    "volume_relative_error": 1e-06,
    "measurement_error_budget_mm": 0.0001,
}
PI = math.pi


def ident(*parts: str) -> str:
    return str(uuid5(NAMESPACE_URL, "deep3d-g3-seed/" + "/".join(parts)))


def L(value: float) -> dict:
    return {"kind": "literal", "value": {"kind": "length_mm", "value": value}}


def A(value: float) -> dict:
    return {"kind": "literal", "value": {"kind": "angle_rad", "value": value}}


@dataclass
class Builder:
    case: str
    name: str
    parameters: dict = field(default_factory=dict)
    features: dict = field(default_factory=dict)
    nodes: dict = field(default_factory=dict)
    roles: dict = field(default_factory=dict)
    acceptance: dict = field(default_factory=dict)

    def __post_init__(self):
        self.root = ident(self.case, "node", "world")
        self.nodes[self.root] = {"id": self.root, "name": "world", "parent": None, "feature": None}

    def fid(self, name: str) -> str:
        return ident(self.case, "feature", name)

    def nid(self, name: str) -> str:
        return ident(self.case, "node", name)

    def param(self, name: str, value: float) -> None:
        pid = ident(self.case, "parameter", name)
        self.parameters[pid] = {
            "id": pid,
            "name": name,
            "value": {"kind": "length_mm", "value": value},
        }

    def P(self, name: str, scale: float = 1) -> dict:
        return {
            "kind": "parameter",
            "parameter_id": ident(self.case, "parameter", name),
            "scale": scale,
        }

    def Lin(self, terms: list[tuple[str, float]], offset: float = 0) -> dict:
        return {
            "kind": "linear",
            "terms": [
                {"parameter_id": ident(self.case, "parameter", n), "scale": s} for n, s in terms
            ],
            "offset": {"kind": "length_mm", "value": offset},
        }

    def feature(self, name: str, op: str, role: str | None = None, **operation) -> str:
        fid = self.fid(name)
        self.features[fid] = {"id": fid, "name": name, "operation": {"op": op, **operation}}
        if role:
            self.roles.setdefault(role, []).append(name)
        return fid

    # Operation shorthands -------------------------------------------------
    def rect(self, name, w, h, role=None, cx=None, cy=None):
        return self.feature(
            name,
            "profile.rectangle@1",
            role,
            width=w,
            height=h,
            center_x=cx or L(0),
            center_y=cy or L(0),
        )

    def circle(self, name, r, role=None, cx=None, cy=None):
        return self.feature(
            name, "profile.circle@1", role, radius=r, center_x=cx or L(0), center_y=cy or L(0)
        )

    def extrude(self, name, profile, distance, role=None):
        return self.feature(name, "solid.extrude@1", role, profile=profile, distance=distance)

    def boolean(self, name, op, base, tool, role=None):
        return self.feature(name, f"solid.{op}@1", role, base=base, tool=tool)

    def move(self, name, shape, x, y, z, role=None, axis=(0, 0, 1), angle=0.0):
        return self.feature(
            name,
            "shape.transform@1",
            role,
            shape=shape,
            translation=[x, y, z],
            axis=list(axis),
            angle=A(angle),
        )

    def node(self, name: str, feature: str | None, parent: str | None = None) -> str:
        nid = self.nid(name)
        self.nodes[nid] = {
            "id": nid,
            "name": name,
            "parent": parent or self.root,
            "feature": feature,
        }
        return nid

    def expect(self, node: str, volume: float, bounds: list[float], solids: int = 1) -> None:
        self.acceptance.setdefault("nodes", {})[self.nid(node)] = {
            "solid_count": solids,
            "volume_mm3": volume,
            "bounds_mm": bounds,
        }

    def expect_assembly(self, volume: float, bounds: list[float], solids: int) -> None:
        self.acceptance["assembly"] = {
            "solid_count": solids,
            "volume_mm3": volume,
            "bounds_mm": bounds,
        }

    def project(self) -> dict:
        acceptance = {"schema_version": "deep3d.mechanical-acceptance/1", **self.acceptance}
        acceptance.setdefault("max_interference_volume_mm3", 0)
        return {
            "schema_version": "deep3d.spec/1",
            "project_id": ident(self.case, "project"),
            "name": self.name,
            "root_node_id": self.root,
            "nodes": self.nodes,
            "parameters": self.parameters,
            "features": self.features,
            "acceptance": acceptance,
            "precision": copy.deepcopy(PRECISION),
        }


# Closed-form acceptance -----------------------------------------------------


def c01_expect(b, w=80, d=50, t=8, hd=6, hx=25, hy=15, min_wall=None):
    """`min_wall` adds the wall requirement; the volume formula assumes separate holes."""
    volume = w * d * t - 4 * PI * (hd / 2) ** 2 * t
    b.expect("mounting_plate", volume, [-w / 2, -d / 2, 0, w / 2, d / 2, t])
    node = b.acceptance["nodes"][b.nid("mounting_plate")]
    if volume <= 0:
        del node["volume_mm3"]
    node["axial_holes"] = {
        "axis": "z",
        "diameter_mm": hd,
        "centers_mm": [[sx * hx, sy * hy] for sx, sy in [(-1, -1), (-1, 1), (1, -1), (1, 1)]],
        "through": True,
    }
    if min_wall is not None:
        node["axial_holes"]["min_wall_mm"] = min_wall


def c02_expect(b, d1=24, d2=16, l1=20, length=40, bore=6):
    volume = PI * ((d1 / 2) ** 2 * l1 + (d2 / 2) ** 2 * (length - l1) - (bore / 2) ** 2 * length)
    b.expect("stepped_shaft", volume, [-d1 / 2, -d1 / 2, 0, d1 / 2, d1 / 2, length])


def c03_expect(b, r=2.0):
    area = 40 * 6 + 6 * 24
    b.expect("l_bracket", area * 20 - 4 * (1 - PI / 4) * r * r * 20, [0, 0, 0, 40, 30, 20])


def c04_expect(b, c=1.0, hole=10):
    r = hole / 2
    chamfer = 2 * PI * (r + c / 3) * (c * c / 2)
    b.expect("chamfered_block", 40 * 40 * 10 - PI * r * r * 10 - chamfer, [-20, -20, 0, 20, 20, 10])


def c05_expect(b, count=8, bolt=8, pitch=70):
    volume = PI * 50**2 * 10 - PI * 10**2 * 10 - count * PI * (bolt / 2) ** 2 * 10
    b.expect("bolt_disk", volume, [-50, -50, 0, 50, 50, 10])
    b.acceptance["nodes"][b.nid("bolt_disk")]["axial_holes"] = {
        "axis": "z",
        "diameter_mm": bolt,
        "centers_mm": [
            [pitch / 2 * math.cos(2 * PI * k / count), pitch / 2 * math.sin(2 * PI * k / count)]
            for k in range(count)
        ],
        "through": True,
    }


def c06_expect(b, count=5, pitch=10, height=15):
    half = (count - 1) / 2 * pitch + 1
    width = max(30, half)
    b.expect(
        "heatsink", 60 * 40 * 5 + count * 2 * 40 * height, [-width, -20, 0, width, 20, 5 + height]
    )


def c07_expect(b, width=6, length=20):
    r = width / 2
    slot = (length - width) * width + PI * r * r
    b.expect("slot_plate", (40 * 20 - slot) * 6, [-20, -10, 0, 20, 10, 6])


def c08_expect(b, w=80):
    volume = w * 40 * 20 - 2 * (PI * 36 * 20 + PI * (100 - 36) * 5)
    b.expect("bearing_block", volume, [-w / 2, -20, 0, w / 2, 20, 20])


def c09_expect(b, gap=10, sx=12, sy=8, spacers=None):
    """`spacers` lists (node, x, y) occurrences; defaults to the four corners."""
    if spacers is None:
        spacers = C09_SPACERS(sx, sy)
    plate, t = 40 * 30 * 5, 5
    b.acceptance["nodes"] = {}
    b.expect("bottom_plate", plate, [-20, -15, 0, 20, 15, t])
    b.expect("top_plate", plate, [-20, -15, t + gap, 20, 15, 2 * t + gap])
    spacer = PI * 9 * gap
    for node, x, y in spacers:
        b.expect(node, spacer, [x - 3, y - 3, t, x + 3, y + 3, t + gap])
    b.expect_assembly(
        2 * plate + len(spacers) * spacer, [-20, -15, 0, 20, 15, 2 * t + gap], 2 + len(spacers)
    )


def C09_SPACERS(sx=12, sy=8):
    return [
        ("spacer_front_left", -sx, -sy),
        ("spacer_front_right", sx, -sy),
        ("spacer_back_left", -sx, sy),
        ("spacer_back_right", sx, sy),
    ]


def c10_expect(b, height=40, beam=90):
    b.acceptance["nodes"] = {}
    b.expect("base", 100 * 60 * 6, [-50, -30, 0, 50, 30, 6])
    b.expect("upright_left", 100 * height, [-40, -5, 6, -30, 5, 6 + height])
    b.expect("upright_right", 100 * height, [30, -5, 6, 40, 5, 6 + height])
    b.expect("beam", beam * 10 * 8, [-beam / 2, -5, 6 + height, beam / 2, 5, 14 + height])
    total = 100 * 60 * 6 + 2 * 100 * height + beam * 80
    b.expect_assembly(total, [-50, -30, 0, 50, 30, 14 + height], 4)


# Seeds -----------------------------------------------------------------------


def c01() -> Builder:
    b = Builder("C01", "Four-hole mounting plate")
    for name, value in [
        ("plate_width", 80),
        ("plate_depth", 50),
        ("plate_thickness", 8),
        ("hole_diameter", 6),
        ("hole_x", 25),
        ("hole_y", 15),
    ]:
        b.param(name, value)
    profile = b.rect("StockProfile", b.P("plate_width"), b.P("plate_depth"), "stock.profile")
    stock = b.extrude("StockBody", profile, b.P("plate_thickness"), "stock.extrude")
    tools = []
    for i, (sx, sy) in enumerate([(-1, -1), (-1, 1), (1, -1), (1, 1)], 1):
        hole = b.circle(
            f"HoleProfile{i}",
            b.P("hole_diameter", 0.5),
            "holes.profiles",
            b.P("hole_x", sx),
            b.P("hole_y", sy),
        )
        tools.append(b.extrude(f"HoleTool{i}", hole, b.P("plate_thickness"), "holes.tools"))
    tool = b.feature("HoleTools", "assembly.compound@1", "holes.tools", parts=tools)
    plate = b.boolean("MountingPlate", "cut", stock, tool, "holes.cut")
    b.node("mounting_plate", plate)
    c01_expect(b)
    return b


def c02() -> Builder:
    b = Builder("C02", "Revolved stepped shaft")
    for name, value in [
        ("large_section_diameter", 24),
        ("small_section_diameter", 16),
        ("large_section_length", 20),
        ("shaft_length", 40),
        ("bore_diameter", 6),
    ]:
        b.param(name, value)
    # Section in the XY plane (x = radius, y = axial), revolved about +Y,
    # then turned so the shaft axis is +Z.
    points = [
        [L(0), L(0)],
        [b.P("large_section_diameter", 0.5), L(0)],
        [b.P("large_section_diameter", 0.5), b.P("large_section_length")],
        [b.P("small_section_diameter", 0.5), b.P("large_section_length")],
        [b.P("small_section_diameter", 0.5), b.P("shaft_length")],
        [L(0), b.P("shaft_length")],
    ]
    section = b.feature("ShaftSection", "profile.polygon@1", "shaft.profile", points=points)
    revolved = b.feature(
        "ShaftRevolution",
        "solid.revolve@1",
        "shaft.revolve",
        profile=section,
        angle=A(2 * PI),
        axis_origin=[L(0), L(0), L(0)],
        axis_direction=[0, 1, 0],
    )
    shaft = b.move("ShaftAxisToZ", revolved, L(0), L(0), L(0), "shaft.revolve", (1, 0, 0), PI / 2)
    bore = b.circle("BoreProfile", b.P("bore_diameter", 0.5), "bore.profile")
    tool = b.extrude("BoreTool", bore, b.P("shaft_length"), "bore.extrude")
    result = b.boolean("BoredShaft", "cut", shaft, tool, "shaft.cut")
    b.node("stepped_shaft", result)
    c02_expect(b)
    return b


def c03() -> Builder:
    b = Builder("C03", "L section with selected free-end fillets")
    b.param("free_end_fillet_radius", 2)
    b.param("extrusion_length", 20)
    corners = [(0, 0), (40, 0), (40, 6), (6, 6), (6, 30), (0, 30)]
    section = b.feature(
        "AngleSection",
        "profile.polygon@1",
        "angle.profile",
        points=[[L(x), L(y)] for x, y in corners],
    )
    body = b.extrude("AngleBody", section, b.P("extrusion_length"), "angle.extrude")
    radius = b.P("free_end_fillet_radius")
    end_x = b.feature(
        "FreeEndFilletX",
        "solid.fillet@1",
        "free_ends.fillet",
        shape=body,
        radius=radius,
        selector={
            "kind": "line_parallel_in_plane",
            "direction": [0, 0, 1],
            "axis": "x",
            "coordinate": L(40),
            "expected_count": 2,
        },
    )
    end_y = b.feature(
        "FreeEndFilletY",
        "solid.fillet@1",
        "free_ends.fillet",
        shape=end_x,
        radius=radius,
        selector={
            "kind": "line_parallel_in_plane",
            "direction": [0, 0, 1],
            "axis": "y",
            "coordinate": L(30),
            "expected_count": 2,
        },
    )
    b.node("l_bracket", end_y)
    c03_expect(b)
    return b


def c04() -> Builder:
    b = Builder("C04", "Block with a hole-mouth chamfer")
    for name, value in [
        ("block_size", 40),
        ("block_height", 10),
        ("hole_diameter", 10),
        ("hole_mouth_chamfer", 1),
    ]:
        b.param(name, value)
    profile = b.rect("BlockProfile", b.P("block_size"), b.P("block_size"), "block.profile")
    block = b.extrude("BlockBody", profile, b.P("block_height"), "block.extrude")
    hole = b.circle("HoleProfile", b.P("hole_diameter", 0.5), "hole.cut")
    tool = b.extrude("HoleTool", hole, b.P("block_height"), "hole.cut")
    cut = b.boolean("BoredBlock", "cut", block, tool, "hole.cut")
    chamfered = b.feature(
        "HoleMouthChamfer",
        "solid.chamfer@1",
        "mouth.chamfer",
        shape=cut,
        distance=b.P("hole_mouth_chamfer"),
        selector={
            "kind": "circle",
            "radius": b.P("hole_diameter", 0.5),
            "axis": "z",
            "coordinate": b.P("block_height"),
            "expected_count": 1,
        },
    )
    b.node("chamfered_block", chamfered)
    c04_expect(b)
    return b


def c05() -> Builder:
    b = Builder("C05", "Circular bolt pattern disk")
    for name, value in [
        ("disk_diameter", 100),
        ("disk_thickness", 10),
        ("center_hole_diameter", 20),
        ("bolt_diameter", 8),
        ("bolt_pitch_diameter", 70),
    ]:
        b.param(name, value)
    disk = b.extrude(
        "DiskBody",
        b.circle("DiskProfile", b.P("disk_diameter", 0.5), "disk.profile"),
        b.P("disk_thickness"),
        "disk.extrude",
    )
    center = b.extrude(
        "CenterTool",
        b.circle("CenterProfile", b.P("center_hole_diameter", 0.5), "center.cut"),
        b.P("disk_thickness"),
        "center.cut",
    )
    centered = b.boolean("CenterCut", "cut", disk, center, "center.cut")
    for role in ("center.cut",):
        b.roles[role] = ["CenterProfile", "CenterTool", "CenterCut"]
    bolt = b.extrude(
        "BoltTool",
        b.circle("BoltProfile", b.P("bolt_diameter", 0.5), "bolt.profile"),
        b.P("disk_thickness"),
        "bolt.profile",
    )
    b.roles["bolt.seed"] = ["BoltProfile", "BoltTool"]
    seed = b.move(
        "BoltSeedPlacement",
        bolt,
        b.P("bolt_pitch_diameter", 0.5),
        L(0),
        L(0),
        "bolt.seed_placement",
    )
    pattern = b.feature(
        "BoltPattern",
        "shape.circular_pattern@1",
        "bolt.pattern",
        shape=seed,
        count=8,
        axis=[0, 0, 1],
        origin=[L(0), L(0), L(0)],
        step_angle=A(2 * PI / 8),
        fuse=False,
    )
    result = b.boolean("BoltCut", "cut", centered, pattern, "bolts.cut")
    b.node("bolt_disk", result)
    c05_expect(b)
    return b


def c06() -> Builder:
    b = Builder("C06", "Linear fin heatsink")
    for name, value in [
        ("base_width", 60),
        ("base_depth", 40),
        ("base_thickness", 5),
        ("fin_thickness", 2),
        ("fin_height", 15),
        ("fin_pitch", 10),
    ]:
        b.param(name, value)
    base = b.extrude(
        "BaseBody",
        b.rect("BaseProfile", b.P("base_width"), b.P("base_depth"), "base.profile"),
        b.P("base_thickness"),
        "base.extrude",
    )
    fin_profile = b.rect("FinProfile", b.P("fin_thickness"), b.P("base_depth"), "fin.profile")
    fin = b.extrude("FinBody", fin_profile, b.P("fin_height"), "fin.extrude")
    pattern = b.feature(
        "FinPattern",
        "shape.linear_pattern@1",
        "fin.pattern",
        shape=fin,
        count=5,
        step=[b.P("fin_pitch"), L(0), L(0)],
        fuse=False,
    )
    placed = b.move(
        "FinPlacement", pattern, b.P("fin_pitch", -2), L(0), b.P("base_thickness"), "fin.pattern"
    )
    result = b.boolean("Heatsink", "fuse", base, placed, "heatsink.fuse")
    b.node("heatsink", result)
    c06_expect(b)
    return b


def c07() -> Builder:
    b = Builder("C07", "Plate with a constrained obround slot")
    for name, value in [
        ("plate_width", 40),
        ("plate_depth", 20),
        ("plate_thickness", 6),
        ("slot_width", 6),
        ("slot_length", 20),
    ]:
        b.param(name, value)
    plate = b.extrude(
        "PlateBody",
        b.rect("PlateProfile", b.P("plate_width"), b.P("plate_depth"), "plate.profile"),
        b.P("plate_thickness"),
        "plate.extrude",
    )
    # Arc centers sit at +/-(length - width)/2; straight sides at y=+/-width/2.
    cx = b.Lin([("slot_length", 0.5), ("slot_width", -0.5)])
    ncx = b.Lin([("slot_length", -0.5), ("slot_width", 0.5)])
    hw, nhw = b.P("slot_width", 0.5), b.P("slot_width", -0.5)
    sketch = {
        "points": {
            "c1": [ncx, L(0)],
            "c2": [cx, L(0)],
            "a": [ncx, nhw],
            "b": [cx, nhw],
            "c": [cx, hw],
            "d": [ncx, hw],
        },
        "lines": {"bottom": ["a", "b"], "top": ["c", "d"]},
        "circles": {},
        "arcs": {
            "right": {"center": "c2", "start": "b", "end": "c", "ccw": True},
            "left": {"center": "c1", "start": "d", "end": "a", "ccw": True},
        },
        "constraints": [
            # Overall length is carried by the fixed, mirrored arc centers; with
            # both end widths, the four tangencies make the sides horizontal.
            {"id": "left_center", "kind": "fix", "point": "c1", "x": ncx, "y": L(0)},
            {"id": "centers_symmetric", "kind": "symmetric_h", "a": "c1", "b": "c2"},
            {"id": "right_tangent_bottom", "kind": "tangent", "a": "right", "b": "bottom"},
            {"id": "right_tangent_top", "kind": "tangent", "a": "right", "b": "top"},
            {"id": "left_tangent_top", "kind": "tangent", "a": "left", "b": "top"},
            {"id": "left_tangent_bottom", "kind": "tangent", "a": "left", "b": "bottom"},
            {
                "id": "slot_width",
                "kind": "distance",
                "a": "a",
                "b": "d",
                "value": b.P("slot_width"),
            },
            {
                "id": "slot_width_right",
                "kind": "distance",
                "a": "b",
                "b": "c",
                "value": b.P("slot_width"),
            },
        ],
        "loops": [
            [
                {"entity": "bottom", "reversed": False},
                {"entity": "right", "reversed": False},
                {"entity": "top", "reversed": False},
                {"entity": "left", "reversed": False},
            ]
        ],
    }
    slot = b.feature("SlotSketch", "profile.sketch@1", "slot.sketch", sketch=sketch)
    tool = b.extrude("SlotTool", slot, b.P("plate_thickness"), "slot.extrude")
    result = b.boolean("SlottedPlate", "cut", plate, tool, "slot.cut")
    b.node("slot_plate", result)
    c07_expect(b)
    return b


def c08() -> Builder:
    b = Builder("C08", "Bearing block with counterbored holes")
    for name, value in [
        ("block_width", 80),
        ("block_depth", 40),
        ("block_height", 20),
        ("bore_diameter", 12),
        ("counterbore_diameter", 20),
        ("counterbore_depth", 5),
    ]:
        b.param(name, value)
    current = b.extrude(
        "BlockBody",
        b.rect("BlockProfile", b.P("block_width"), b.P("block_depth"), "block.profile"),
        b.P("block_height"),
        "block.extrude",
    )
    bore = b.extrude(
        "BoreTool",
        b.circle("BoreProfile", b.P("bore_diameter", 0.5), "bore.profile"),
        b.P("block_height"),
        "bore.profile",
    )
    counter = b.extrude(
        "CounterboreTool",
        b.circle("CounterboreProfile", b.P("counterbore_diameter", 0.5), "counterbore.profile"),
        b.P("counterbore_depth"),
        "counterbore.profile",
    )
    counter_z = b.Lin([("block_height", 1), ("counterbore_depth", -1)])
    for side, sign in (("Left", -1), ("Right", 1)):
        # Hole center distance stays half the block width: x = +/- width/4.
        placed = b.move(
            f"Bore{side}", bore, b.P("block_width", 0.25 * sign), L(0), L(0), "bores.placement"
        )
        current = b.boolean(f"BoreCut{side}", "cut", current, placed, "bores.cut")
    for side, sign in (("Left", -1), ("Right", 1)):
        placed = b.move(
            f"Counterbore{side}",
            counter,
            b.P("block_width", 0.25 * sign),
            L(0),
            counter_z,
            "counterbores.placement",
        )
        current = b.boolean(f"CounterboreCut{side}", "cut", current, placed, "counterbores.cut")
    b.node("bearing_block", current)
    c08_expect(b)
    return b


def c09() -> Builder:
    b = Builder("C09", "Plate and spacer rigid stack")
    for name, value in [
        ("plate_width", 40),
        ("plate_depth", 30),
        ("plate_thickness", 5),
        ("spacer_radius", 3),
        ("plate_gap", 10),
        ("spacer_x_offset", 12),
        ("spacer_y_offset", 8),
    ]:
        b.param(name, value)
    group = b.node("plate_spacer_stack", None)
    plate = b.extrude(
        "PlateBody",
        b.rect("PlateProfile", b.P("plate_width"), b.P("plate_depth"), "plate.profile"),
        b.P("plate_thickness"),
        "plate.extrude",
    )
    bottom = b.move("BottomPlateInstance", plate, L(0), L(0), L(0), "bottom_plate.instance")
    top = b.move(
        "TopPlateInstance",
        plate,
        L(0),
        L(0),
        b.Lin([("plate_thickness", 1), ("plate_gap", 1)]),
        "top_plate.instance",
    )
    b.roles["plate.instances"] = ["BottomPlateInstance", "TopPlateInstance"]
    b.node("bottom_plate", bottom, group)
    b.node("top_plate", top, group)
    spacer = b.extrude(
        "SpacerBody",
        b.circle("SpacerProfile", b.P("spacer_radius"), "spacer.profile"),
        b.P("plate_gap"),
        "spacer.extrude",
    )
    for node, sx, sy in [
        ("spacer_front_left", -1, -1),
        ("spacer_front_right", 1, -1),
        ("spacer_back_left", -1, 1),
        ("spacer_back_right", 1, 1),
    ]:
        name = "".join(part.title() for part in node.split("_")) + "Instance"
        placed = b.move(
            name,
            spacer,
            b.P("spacer_x_offset", sx),
            b.P("spacer_y_offset", sy),
            b.P("plate_thickness"),
            "spacer.instances",
        )
        b.node(node, placed, group)
    b.roles["spacer.front_left_instance"] = ["SpacerFrontLeftInstance"]
    c09_expect(b)
    return b


def c10() -> Builder:
    b = Builder("C10", "Gantry with cross-component height reference")
    for name, value in [
        ("base_width", 100),
        ("base_depth", 60),
        ("base_thickness", 6),
        ("upright_size", 10),
        ("upright_height", 40),
        ("upright_x", 35),
        ("beam_length", 90),
        ("beam_depth", 10),
        ("beam_height", 8),
    ]:
        b.param(name, value)
    group = b.node("gantry", None)
    base = b.extrude(
        "BaseBody",
        b.rect("BaseProfile", b.P("base_width"), b.P("base_depth"), "base.profile"),
        b.P("base_thickness"),
        "base.extrude",
    )
    b.node("base", base, group)
    upright = b.extrude(
        "UprightBody",
        b.rect("UprightProfile", b.P("upright_size"), b.P("upright_size"), "upright.profile"),
        b.P("upright_height"),
        "upright.extrude",
    )
    for node, sign in (("upright_left", -1), ("upright_right", 1)):
        placed = b.move(
            node.title().replace("_", "") + "Instance",
            upright,
            b.P("upright_x", sign),
            L(0),
            b.P("base_thickness"),
            "upright.instances",
        )
        b.node(node, placed, group)
    beam = b.extrude(
        "BeamBody",
        b.rect("BeamProfile", b.P("beam_length"), b.P("beam_depth"), "beam.profile"),
        b.P("beam_height"),
        "beam.extrude",
    )
    # The beam rests on the uprights: z = base thickness + upright height.
    placed = b.move(
        "BeamInstance",
        beam,
        L(0),
        L(0),
        b.Lin([("base_thickness", 1), ("upright_height", 1)]),
        "beam.instance",
    )
    b.node("beam", placed, group)
    c10_expect(b)
    return b


SEEDS = {f.__name__.upper(): f for f in (c01, c02, c03, c04, c05, c06, c07, c08, c09, c10)}
