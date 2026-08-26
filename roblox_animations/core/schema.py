"""Shared type schemas for the parsed-data flow (rbxm -> meta -> import).

The TypedDicts below describe dicts that cross module boundaries.  Keeping
the shapes in one place catches the key-drift class of bug (the weapon-grip
payload growing new fields in one consumer and not the others) at type-check
time instead of at import time.
"""
from typing import Any, Dict, List, Optional, TypedDict

# 12-component CFrame: position xyz + row-major 3x3 rotation.
CF12 = List[float]


class WeaponGrip(TypedDict):
    """One grip stamped by the Studio plugin's RBXM weapon export."""

    root: str
    bone: str
    jointType: str
    jointName: Optional[str]
    connectionC0: CF12
    connectionC1: CF12


class PartAuxEntry(TypedDict, total=False):
    """A supported part from an .rbxm/.rbxl parse (consumed by creation.py).

    total=False because legacy exporters and parser branches populate
    different subsets; index access still type-checks the key names.
    """

    idx: int
    inst_ref: int
    parent_ref: Optional[int]
    name: str
    class_name: str
    dims_fp: List[float]
    vol_fp: float
    part_size: List[float]
    part_cf: CF12
    is_primary_part: bool
    mesh_class: str
    mesh_id: str
    mesh_size: List[float]
    has_skinning: bool
    shape: str
    color: Any
    transparency: float
    reflectance: float
    texture_id: str
    face_decal: Dict[str, Any]
    surface_appearance: Dict[str, Any]
    texture_instance: Dict[str, Any]
    texture_instances: List[Dict[str, Any]]
    surface_appearances: List[Dict[str, Any]]
    decals: List[Dict[str, Any]]
    wrap_layer: Dict[str, Any]
    wrap_target: Dict[str, Any]
    union_mesh: Dict[str, Any]
    character_mesh: Dict[str, Any]
    model_tag: str
    _use_2022_materials: bool
    scale_type: str
    hd_limb_scale: Dict[str, float]


class WeaponImportPayload(TypedDict):
    """Payload stashed in operators.import_ops._pending_weapon_import["data"].

    Lives here so both import flows (OBJ and rbxm) and the apply operator
    agree on the shape; `schema` carries the version int.
    """

    schema: int
    meta_loaded: Dict[str, Any]
    rig_part_obj_names: List[str]
