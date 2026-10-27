"""Named rigid instances and AP214/mm XCAF exchange, without feature construction."""

from __future__ import annotations

from pathlib import Path

from .contracts import CadError


def compound(shapes):
    from OCP.BRep import BRep_Builder
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Copy
    from OCP.TopoDS import TopoDS_Compound

    result = TopoDS_Compound()
    builder = BRep_Builder()
    builder.MakeCompound(result)
    for shape in shapes:
        # Each occurrence must remain an occurrence even at an identical placement.
        builder.Add(result, BRepBuilderAPI_Copy(shape, True, False).Shape())
    return result


def _document():
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDocStd import TDocStd_Document
    from OCP.XCAFDoc import XCAFDoc_DocumentTool

    document = TDocStd_Document(TCollection_ExtendedString("BinXCAF"))
    return document, XCAFDoc_DocumentTool.ShapeTool_s(document.Main())


def _name(label, value: str) -> None:
    from OCP.TCollection import TCollection_ExtendedString
    from OCP.TDataStd import TDataStd_Name

    TDataStd_Name.Set_s(label, TCollection_ExtendedString(value, True))


def _read_name(label) -> str:
    from OCP.TDataStd import TDataStd_Name

    value = TDataStd_Name()
    if label.FindAttribute(TDataStd_Name.GetID_s(), value):
        return value.Get().ToExtString()
    return ""


def part_source(feature_id: str, features: dict) -> str:
    while features[feature_id]["op"] == "shape.transform@1":
        feature_id = features[feature_id]["inputs"]["shape"]
    return feature_id


def _placement(feature_id: str, features: dict):
    from OCP.gp import gp_Ax1, gp_Dir, gp_Pnt, gp_Trsf, gp_Vec

    transform = gp_Trsf()
    while features[feature_id]["op"] == "shape.transform@1":
        feature = features[feature_id]
        p = feature["parameters"]
        step = gp_Trsf()
        step.SetRotation(
            gp_Ax1(gp_Pnt(0, 0, 0), gp_Dir(p["axis_x"], p["axis_y"], p["axis_z"])), p["angle_rad"]
        )
        step.SetTranslationPart(gp_Vec(p["dx_mm"], p["dy_mm"], p["dz_mm"]))
        transform.Multiply(step)
        feature_id = feature["inputs"]["shape"]
    return feature_id, transform


def assembly_manifest(plan: dict) -> dict:
    features = {f["id"]: f for f in plan["features"]}
    scene = plan["scene"]
    parts = {}
    instances = {}
    for node, feature in sorted(plan["outputs"].items()):
        part = part_source(feature, features)
        parts.setdefault(
            part, {"part_id": part, "name": features[part]["name"], "quantity": 0, "instances": []}
        )
        parts[part]["quantity"] += 1
        parts[part]["instances"].append(node)
        instances[node] = {
            "name": scene[node]["name"],
            "parent": scene[node]["parent"],
            "feature_id": feature,
            "part_id": part,
        }
    return {
        "schema_version": "deep3d.rigid-assembly/1",
        "project_id": plan["project_id"],
        "revision_id": plan["revision_id"],
        "scene": scene,
        "instances": instances,
        "bom": list(parts.values()),
    }


def export_assembly(plan: dict, shapes: dict, path: Path, tolerance: float) -> dict:
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Copy
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.Interface import Interface_Static
    from OCP.STEPCAFControl import STEPCAFControl_Writer
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer
    from OCP.TopLoc import TopLoc_Location

    from .kernel import _transfer_checks

    document, tool = _document()
    features = {f["id"]: f for f in plan["features"]}
    scene = plan["scene"]
    used = set()
    for node in plan["outputs"]:
        while node is not None:
            used.add(node)
            node = scene[node]["parent"]
    roots = [node for node in used if scene[node]["parent"] is None]
    if len(roots) != 1:
        raise CadError("ASSEMBLY_INVALID", "Assembly requires one rooted scene")
    labels, parts = {}, {}

    def group(node):
        if node in labels:
            return labels[node]
        definition = tool.AddShape(compound([]), False)
        _name(definition, "node:" + node + ":" + scene[node]["name"])
        labels[node] = definition
        parent = scene[node]["parent"]
        if parent is not None:
            reference = tool.AddComponent(group(parent), definition, TopLoc_Location())
            _name(reference, "node:" + node + ":" + scene[node]["name"])
        return definition

    for node in sorted(used):
        if node not in plan["outputs"]:
            group(node)
    for node, feature in sorted(plan["outputs"].items()):
        source, placement = _placement(feature, features)
        if source not in parts:
            parts[source] = tool.AddShape(
                BRepBuilderAPI_Copy(shapes[source], True, False).Shape(), False
            )
            _name(parts[source], "part:" + source + ":" + features[source]["name"])
        parent = scene[node]["parent"]
        reference = tool.AddComponent(group(parent), parts[source], TopLoc_Location(placement))
        _name(reference, "node:" + node + ":" + scene[node]["name"])
    tool.UpdateAssemblies()
    STEPControl_Writer()
    keys = ("write.step.schema", "write.step.unit")
    previous = {key: Interface_Static.CVal_s(key) for key in keys}
    try:
        for key, value in zip(keys, ("AP214IS", "MM")):
            if not Interface_Static.SetCVal_s(key, value):
                raise CadError("STEP_CONFIG", "Cannot set STEP assembly profile")
        writer = STEPCAFControl_Writer()
        writer.SetNameMode(True)
        writer.ChangeWriter().SetTolerance(tolerance)
        if not writer.Transfer(document, STEPControl_AsIs):
            raise CadError("STEP_TRANSFER_FAILED", "Cannot transfer assembly document")
        checks = _transfer_checks(writer.Writer().WS().TransferWriter().CheckList())
        if checks["errors"] or checks["warnings"] or writer.Write(str(path)) != IFSelect_RetDone:
            raise CadError("STEP_TRANSFER_FAILED", "Assembly export was not clean", checks)
    finally:
        for key, value in previous.items():
            Interface_Static.SetCVal_s(key, value)
    return {
        "schema": "AP214IS",
        "units": "mm",
        "assembly": True,
        "instances": len(plan["outputs"]),
        "part_definitions": len(parts),
    }


def read_assembly(path: Path) -> tuple[dict, dict]:
    from OCP.collections import Sequence_TCollection_AsciiString, Sequence_TDF_Label
    from OCP.IFSelect import IFSelect_RetDone
    from OCP.STEPCAFControl import STEPCAFControl_Reader
    from OCP.TDF import TDF_Label
    from OCP.TopLoc import TopLoc_Location
    from OCP.XCAFDoc import XCAFDoc_ShapeTool

    from .kernel import _transfer_checks

    document, tool = _document()
    reader = STEPCAFControl_Reader()
    reader.SetNameMode(True)
    if reader.ReadFile(str(path)) != IFSelect_RetDone:
        raise CadError("STEP_IMPORT_FAILED", "Cannot read STEP assembly")
    native_reader = reader.ChangeReader()
    lengths, angles, solid_angles = (Sequence_TCollection_AsciiString() for _ in range(3))
    native_reader.FileUnits(lengths, angles, solid_angles)
    units = [lengths.Value(i).ToCString().lower() for i in range(1, lengths.Length() + 1)]
    if not units or any(u not in {"millimetre", "millimeter", "mm"} for u in units):
        raise CadError("STEP_UNIT_UNSUPPORTED", "STEP assembly must explicitly use millimetres")
    native_reader.SetSystemLengthUnit(1.0)
    if not reader.Transfer(document):
        raise CadError("STEP_TRANSFER_FAILED", "Cannot import STEP assembly document")
    checks = _transfer_checks(native_reader.WS().TransferReader().LastCheckList())
    if checks["errors"] or checks["warnings"]:
        raise CadError("STEP_TRANSFER_FAILED", "Assembly import was not clean", checks)
    roots = Sequence_TDF_Label()
    tool.GetFreeShapes(roots)
    if roots.Length() != 1:
        raise CadError("ASSEMBLY_INVALID", "STEP requires exactly one assembly root")
    nodes, metadata = {}, {}

    def visit(label, parent_location, parent_node, depth):
        if depth > 64:
            raise CadError("ASSEMBLY_INVALID", "STEP hierarchy exceeds 64 levels")
        name = _read_name(label)
        definition = label
        local = TopLoc_Location()
        if XCAFDoc_ShapeTool.IsReference_s(label):
            definition = TDF_Label()
            if not XCAFDoc_ShapeTool.GetReferredShape_s(label, definition):
                raise CadError("ASSEMBLY_INVALID", "Broken STEP component reference")
            local = XCAFDoc_ShapeTool.GetLocation_s(label)
        location = parent_location.Multiplied(local)
        if not name.startswith("node:") or len(name.split(":", 2)) != 3:
            raise CadError("ASSEMBLY_NAMES_MISSING", "STEP component lost its stable node name")
        _, node, display_name = name.split(":", 2)
        if node in metadata:
            raise CadError("ASSEMBLY_INVALID", "Duplicate STEP node identity")
        metadata[node] = {"name": display_name, "parent": parent_node}
        children = Sequence_TDF_Label()
        XCAFDoc_ShapeTool.GetComponents_s(definition, children, False)
        if children.Length():
            for index in range(1, children.Length() + 1):
                visit(children.Value(index), location, node, depth + 1)
        else:
            part_name = _read_name(definition)
            if not part_name.startswith("part:"):
                raise CadError("ASSEMBLY_NAMES_MISSING", "STEP part identity is missing")
            metadata[node]["part_id"] = part_name.split(":", 2)[1]
            nodes[node] = XCAFDoc_ShapeTool.GetShape_s(definition).Moved(location)

    visit(roots.Value(1), TopLoc_Location(), None, 0)
    return nodes, metadata
