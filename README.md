# DX Exporter — Cinema 4D → DirectX .

A production Cinema 4D plugin that exports scene hierarchies to **DirectX ASCII `.x`** for the Tiltan Model Viewer / AXE simulation pipeline. It handles the parts a generic exporter doesn't: a strict parent/child node hierarchy, LOD and damage-state naming conventions, per-object multi-material assignment, MeshNormals vertex format, and a coordinate-system conversion that keeps multi-level rigs (turrets, launchers, helper nulls) spatially coherent.



> **Note on this repository.** This is a portfolio piece. The code is shared to demonstrate plugin architecture, the Cinema 4D Python API in anger, and the kind of hard-won, format-specific problem solving that real pipeline tools require. It is not a polished, fully-supported product release.

---

## What it does

The exporter walks a Cinema 4D object tree and emits a `.x` file whose structure a target real-time viewer can load directly, with:

- **Hierarchy-faithful frames.** Every object becomes a `Frame` with a local `FrameTransformMatrix`. Parent/child relationships, nulls, and helper objects are preserved so rigged sub-assemblies keep their pivots and relative placement.
- **LOD and damage-state awareness.** Objects named `LOD_High` / `LOD_Medium` / `LOD_Low` / `LOD_XLow` and `Dmg0` / `Dmg1` are recognized and mapped to the naming the viewer expects.
- **Per-object mesh extraction.** Geometry is read from polygon objects (and from the cache of generators/deformers where present), including normals, UVs, and optional vertex color / tangent / bitangent.
- **Multi-material support.** Per-polygon material assignment via selection tags is resolved into the exported material blocks, with texture-map copying alongside the `.x`.
- **Coordinate-system conversion.** A configurable axis mapping plus a similarity transform on each frame matrix keeps the whole hierarchy consistent (see *Engineering highlights*).
- **MeshNormals geometry format**, meters units, P/N splitter-safe output — the specific profile the Tiltan Viewer expects.
- **In-editor validation** of the hierarchy before export, surfacing problems instead of producing a broken file.

## Why use it

A vanilla `.x` export gets geometry out but routinely breaks on the things that matter for a real-time asset: the rig flattens, LOD/damage variants lose their naming, pivots drift, or the model lands rotated or mis-scaled in the target engine. This exporter is built around those failure modes specifically, so an artist can model in Cinema 4D using a clean naming convention and get an asset that drops into the viewer **standing on its wheels, correctly scaled, with its turret and launcher still pivoting around the right points.**


---

## Engineering highlights

These are the parts worth reading the code for.

### 1. Hierarchy-coherent coordinate conversion

`.x` reconstructs each object's world transform by chaining local matrices up the tree (`M_world_child = M_local_child · M_world_parent`). That means an axis conversion can't just be applied to translations — it has to be a **change of basis applied consistently to every node**, or per-level errors accumulate and children drift.

The exporter converts each frame matrix as a similarity transform `S · M · S` (where `S` is the axis-permutation matrix and is its own inverse). Applied to every node, each parent's trailing `S` cancels the child's leading `S`, so the tree stays coherent — *and* any real object rotation converts correctly, not just translations.

```python
def _tiltan_axis_vec_tuple(x, y, z):
    """Verified against the viewer's bounding-box readout and exported mesh
    extents: the target uses the same Y-up convention as Cinema 4D, so the
    correct mapping here is identity. The frame matrix conversion S*M*S then
    collapses to identity too, keeping the hierarchy coherent."""
    return x, y, z
```

The journey to that one-line function is the interesting part: an earlier version swapped the basis rows of each matrix independently (not a similarity transform), which looked "almost right" for un-rotated objects but tipped rigged sub-assemblies out of place. The fix was diagnosed by measuring the exported file's bounding box against the viewer's own readout, not by guessing.

### 2. Generator / deformer cache extraction

Parametric objects (cloners, sweeps, subdivision surfaces) have no polygons of their own. The exporter resolves the renderable mesh from the object's cache tree before extracting vertices:

```python
def _find_polygon_cache(obj):
    # Walks deform cache -> cache -> child caches to find the real
    # polygon output for a generator/deformer, so parametric objects
    # export as baked geometry instead of empty frames.
    ...
```

### 3. Convention-driven naming

LOD and damage states are derived from object names rather than manual tagging, so the artist's scene organization *is* the export configuration:

```python
def _tiltan_lod_suffix(name):
    low = str(name).lower()
    if low.endswith("_high"):   return "High"
    if low.endswith("_medium"): return "Medium"
    if low.endswith("_low"):    return "Low"
    if low.endswith("_xlow"):   return "XLow"
    ...
```

### 4. Robust texture resolution

Texture paths are resolved across Cinema 4D's shader graph, asset-relative paths, and absolute paths, then copied next to the `.x` with the related PBR maps, so the exported asset is self-contained.

---

## Installation

1. Copy `c4d_dx_exporter.pyp` into your Cinema 4D `plugins` folder
   (e.g. `…/Maxon/Maxon Cinema 4D 2026_XXXX/plugins/DXExporter/`).
2. Restart Cinema 4D.
3. The exporter appears under **Extensions › DX Exporter by EAE**.

> Keep exactly **one** copy of the `.pyp` in the plugins tree. A duplicate file (even one renamed with a prefix) registers the same plugin ID twice and Cinema 4D will report an ID collision.

## Usage

1. Organize the scene with the expected hierarchy and `LOD_*` / `Dmg*` naming.
2. Open the exporter, choose the **Scene Root** (or *Export selected hierarchy only*).
3. Pick vertex-data options (Normals + Texture coordinates are required; vertex color / tangent / bitangent optional).
4. **Validate** to check the hierarchy, then **Export .X**.

## Compatibility

- Cinema 4D 2026 (Python API).
- Output profile: DirectX `.x` ASCII, MeshNormals, meters, P/N splitter-safe.

## Tech

Cinema 4D Python SDK · `GeDialog` UI with custom `GeUserArea` widgets · matrix math for hierarchy-safe coordinate conversion · DirectX `.x` ASCII writer.

---

*Author: EAE. Shared as a portfolio demonstration of Cinema 4D pipeline plugin development.*
