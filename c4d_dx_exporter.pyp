"""
Cinema 4D 2026 - DX Exporter by EAE v0020
DX Exporter by EAE - AXE/3ds Max compatible Master Asset Hierarchy mode
Exports: Geometry, Normals, Tangents, UVs, VertexColor, Multi-Textures,
         Full hierarchy, Null/Dummy frames, Materials, Animation Clips, Bones
Format: ASCII .x (DirectX Text Format)
"""

import c4d
import os
import math
import struct
import tempfile
import traceback
import shutil
import re
from c4d import gui, plugins, documents, storage




def _document_unit_to_meter_scale(doc):
    """Best-effort C4D document-unit to meters factor.

    If the C4D API/constant is unavailable, returns 1.0 so export is not blocked.
    Intended result examples:
      meters      -> 1.0
      centimeters -> 0.01
      millimeters -> 0.001
    """
    try:
        usd = doc[c4d.DOCUMENT_DOCUNIT]
        scale, unit = usd.GetUnitScale()

        mapping = {}
        for const_name, factor in (
            ("DOCUMENT_UNIT_KM", 1000.0),
            ("DOCUMENT_UNIT_M", 1.0),
            ("DOCUMENT_UNIT_CM", 0.01),
            ("DOCUMENT_UNIT_MM", 0.001),
            ("DOCUMENT_UNIT_UM", 0.000001),
            ("DOCUMENT_UNIT_NM", 0.000000001),
            ("DOCUMENT_UNIT_MILE", 1609.344),
            ("DOCUMENT_UNIT_YARD", 0.9144),
            ("DOCUMENT_UNIT_FOOT", 0.3048),
            ("DOCUMENT_UNIT_INCH", 0.0254),
        ):
            if hasattr(c4d, const_name):
                mapping[getattr(c4d, const_name)] = factor

        return float(scale) * float(mapping.get(unit, 1.0))
    except Exception:
        return 1.0


def _tiltan_axis_vec_tuple(x, y, z):
    """C4D default Y-up -> Tiltan viewer axis.

    Empirically (verified against the Tiltan Model Viewer bounding-box readout
    and the exported mesh extents), the viewer uses the SAME Y-up convention as
    Cinema 4D: X = right/width, Y = up/height, Z = depth/length. No axis swap is
    required.

    The earlier (x, z, y) Y/Z swap was incorrect for this target: it moved the
    vehicle's length onto the viewer's vertical axis, tipping the model onto its
    end. Keeping the axes as-is leaves the model standing correctly on its
    wheels, and — because the frame conversion S*M*S then uses an identity S —
    the hierarchy (turret/rocket/AP helpers) also stays coherent.

    Mapping:
      outX = inX
      outY = inY
      outZ = inZ
    """
    return x, y, z


def _tiltan_axis_vec_c4d(v):
    try:
        x, y, z = _tiltan_axis_vec_tuple(v.x, v.y, v.z)
        return c4d.Vector(x, y, z)
    except Exception:
        return v


def _apply_tiltan_axis_to_matrix(m, settings):
    """Apply the Tiltan output axis mapping to a *local* frame matrix.

    The axis change (C4D Y-up/Z-front -> Tiltan Y-front/Z-up) is the Y/Z swap
    matrix S. For a hierarchy of LOCAL matrices that the .x loader recomposes as
    M_world_child = M_local_child * M_world_parent, the correct conversion of
    each local matrix is the change of basis (similarity transform):

        M' = S * M * S        (S is its own inverse for a pure Y/Z swap)

    Applied to every node this telescopes: each parent's trailing S cancels the
    child's leading S, so the whole tree transforms coherently AND any real
    object rotation is converted correctly.

    The previous version swapped the basis rows (v1/v2/v3) and the offset
    independently. That is NOT a similarity transform: it does not cancel
    through the parent chain, so children (turret/rocket) and helper nulls
    (AP_body lights/smoke) drifted. It only looked "close" for nodes that had
    no rotation; the moment an object is rotated it would break.
    """
    try:
        if not settings.get('tiltan_axis_zup_yfront', True):
            return m

        # Build S from the SAME axis mapping used for vertices, so frame and
        # mesh conversions can never diverge. S permutes basis vectors exactly
        # as _tiltan_axis_vec_tuple permutes a point's components.
        S = c4d.Matrix()
        S.off = c4d.Vector(0.0, 0.0, 0.0)
        S.v1 = _tiltan_axis_vec_c4d(c4d.Vector(1.0, 0.0, 0.0))
        S.v2 = _tiltan_axis_vec_c4d(c4d.Vector(0.0, 1.0, 0.0))
        S.v3 = _tiltan_axis_vec_c4d(c4d.Vector(0.0, 0.0, 1.0))

        # Change of basis: S * m * S realizes S·M·S^-1 (S is self-inverse for a
        # pure axis permutation; identity when no swap is configured).
        return S * m * S
    except Exception:
        return m


def _filepath_to_str(path):
    """Cinema 4D SaveDialog can return either a str or a Filename-like object depending on version."""
    if path is None:
        return ""
    if isinstance(path, str):
        return path
    for attr in ("GetString", "GetFileString"):
        fn = getattr(path, attr, None)
        if fn:
            try:
                value = fn()
                if value:
                    return str(value)
            except Exception:
                pass
    return str(path)


PLUGIN_ID = 1059002  # Register at developers.maxon.net

# ─── Dialog Control IDs ───────────────────────────────────────────────────────
# General
CHK_SCENE_ROOT       = 10001
CHK_MATERIAL         = 10002
CHK_BONE             = 10003
CHK_MESH             = 10004
CHK_DUMMY            = 10005
CHK_OTHER_NODES      = 10006
CHK_DUPLICATE_NAME   = 10007
CHK_VERTEX_DUP       = 10008
CHK_ONLY_ANIMSET     = 10009
CHK_INLINE           = 10010
CHK_BONE_MESH        = 10011

# Vertex Elements
CHK_NORMAL           = 10020
CHK_TANGENT          = 10021
CHK_BITANGENT        = 10022
CHK_VERTEXCOLOR      = 10023
CHK_TEXCOORDS        = 10024
CMB_GEOM_FORMAT      = 10025

# Textures
CHK_TEXTURES         = 10030
CHK_OVERWRITE        = 10031
CHK_MULTI_TEX        = 10032
CHK_CONVERT_TEX      = 10033
CMB_TEX_FORMAT       = 10034
CHK_POWER2           = 10035
CHK_REMOVE_DUPS      = 10036
CHK_CREATE_FX        = 10037
CHK_NORMALMAP        = 10038
EDT_PROC_TEX_W       = 10039
EDT_PROC_TEX_H       = 10040

# Animation Clips
CHK_ANIM_CLIPS       = 10050
CHK_SKIN             = 10051
CHK_WEIGHT_FIX       = 10052
EDT_WEIGHT_FIX_VAL   = 10053
LST_CLIPS            = 10054
BTN_ADD_CLIP         = 10055
BTN_INSERT_CLIP      = 10056
BTN_DEL_CLIP         = 10057
EDT_CLIP_NAME        = 10058
EDT_CLIP_START       = 10059
EDT_CLIP_END         = 10060
EDT_SAMPLE_RATE      = 10061
CHK_KEYFRAMES_ONLY   = 10062

# Timeline
RDO_TIMELINE_3DSMAX  = 10070
RDO_TIMELINE_CUSTOM  = 10071
EDT_TIMELINE_CUSTOM  = 10072

# Keys
RDO_MATRIX           = 10080
RDO_SRT              = 10081
EDT_OPTIMIZE         = 10082

# Standard Material
CHK_DIFFUSE_MAP_CLR  = 10090

# DirectX Shader Material
CHK_EFFECT_PARAMS    = 10091
CHK_FX               = 10092
CHK_FX_OVERWRITE     = 10093

# Coordinate System
EDT_XROT             = 10100
EDT_ZROT             = 10101
EDT_SCALE            = 10102
CHK_RIGHTHANDED      = 10103
CHK_WORLDSPACE       = 10104

# X File Format
CMB_XFILE_FORMAT     = 10110

# Export Scope
CHK_EXPORT_SELECTED  = 10130

# Buttons
BTN_OK               = 10120
BTN_CANCEL           = 10121
BTN_VALIDATE         = 10122
BTN_VALIDATE_DIAG    = 10123

# Tiltan compatibility
CHK_TILTAN_COMPAT    = 10131
CHK_TILTAN_AXIS      = 10132
CHK_CONVERT_METERS   = 10133

# Groups
GRP_GENERAL          = 11000
GRP_GENERAL_LEFT     = 11001
GRP_GENERAL_RIGHT    = 11002
GRP_VERTEX           = 11003
GRP_TEXTURES         = 11004
GRP_ANIM_MAIN        = 11005
GRP_ANIM_LEFT        = 11006
GRP_ANIM_RIGHT       = 11007
GRP_CLIPS_BTNS       = 11008
GRP_CLIP_EDIT        = 11009
GRP_SAMPLE           = 11010
GRP_TIMELINE         = 11011
GRP_KEYS             = 11012
GRP_STD_MAT          = 11013
GRP_DX_MAT           = 11014
GRP_COORD            = 11015
GRP_BOTTOM           = 11016
GRP_PROC_TEX         = 11017
GRP_WEIGHT           = 11018
GRP_CONVERT          = 11019
# Tabbed layout
GRP_TABS             = 11030
TAB_GEOMETRY         = 11031
TAB_MATERIALS        = 11032
TAB_ANIMATION        = 11033
TAB_COORD_OUTPUT     = 11034
TAB_DIAGNOSTICS      = 11035

# Tiltan logo (PNG, base64-embedded so the plugin needs no external file)
GADGET_LOGO          = 11040
_TILTAN_LOGO_B64 = (
    "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"
)


# ─────────────────────────────────────────────────────────────────────────────
# DirectX ASCII .X Writer
# ─────────────────────────────────────────────────────────────────────────────

class DXWriter:
    def __init__(self):
        self.lines = []

    def w(self, text=""):
        self.lines.append(text)

    def get_text(self):
        return "\n".join(self.lines)

    def write_header(self, geom_format="decldata"):
        # Match the working reference files exactly: declare ONLY the templates
        # actually used. Extra unused templates (MeshNormals/MeshTextureCoords/
        # FVFData) can make a strict loader like the model viewer reject the file.
        self.w("xof 0303txt 0032")
        self._write_templates(geom_format)

    def _write_templates(self, geom_format="decldata"):
        self.w("template ColorRGBA {")
        self.w(" <35ff44e0-6c7c-11cf-8f52-0040333594a3>")
        self.w(" FLOAT red;")
        self.w(" FLOAT green;")
        self.w(" FLOAT blue;")
        self.w(" FLOAT alpha;")
        self.w("}")
        self.w()
        self.w("template ColorRGB {")
        self.w(" <d3e16e81-7835-11cf-8f52-0040333594a3>")
        self.w(" FLOAT red;")
        self.w(" FLOAT green;")
        self.w(" FLOAT blue;")
        self.w("}")
        self.w()
        self.w("template Material {")
        self.w(" <3d82ab4d-62da-11cf-ab39-0020af71e433>")
        self.w(" ColorRGBA faceColor;")
        self.w(" FLOAT power;")
        self.w(" ColorRGB specularColor;")
        self.w(" ColorRGB emissiveColor;")
        self.w(" [...]")
        self.w("}")
        self.w()
        self.w("template TextureFilename {")
        self.w(" <a42790e1-7810-11cf-8f52-0040333594a3>")
        self.w(" STRING filename;")
        self.w("}")
        self.w()
        self.w("template Frame {")
        self.w(" <3d82ab46-62da-11cf-ab39-0020af71e433>")
        self.w(" [...]")
        self.w("}")
        self.w()
        self.w("template Matrix4x4 {")
        self.w(" <f6f23f45-7686-11cf-8f52-0040333594a3>")
        self.w(" array FLOAT matrix[16];")
        self.w("}")
        self.w()
        self.w("template FrameTransformMatrix {")
        self.w(" <f6f23f41-7686-11cf-8f52-0040333594a3>")
        self.w(" Matrix4x4 frameMatrix;")
        self.w("}")
        self.w()
        self.w("template Vector {")
        self.w(" <3d82ab5e-62da-11cf-ab39-0020af71e433>")
        self.w(" FLOAT x;")
        self.w(" FLOAT y;")
        self.w(" FLOAT z;")
        self.w("}")
        self.w()
        self.w("template MeshFace {")
        self.w(" <3d82ab5f-62da-11cf-8f52-0040333594a3>")
        self.w(" DWORD nFaceVertexIndices;")
        self.w(" array DWORD faceVertexIndices[nFaceVertexIndices];")
        self.w("}")
        self.w()
        self.w("template Mesh {")
        self.w(" <3d82ab44-62da-11cf-ab39-0020af71e433>")
        self.w(" DWORD nVertices;")
        self.w(" array Vector vertices[nVertices];")
        self.w(" DWORD nFaces;")
        self.w(" array MeshFace faces[nFaces];")
        self.w(" [...]")
        self.w("}")
        self.w()
        if geom_format == 'meshnormals':
            self.w("template MeshNormals {")
            self.w(" <f6f23f43-7686-11cf-8f52-0040333594a3>")
            self.w(" DWORD nNormals;")
            self.w(" array Vector normals[nNormals];")
            self.w(" DWORD nFaceNormals;")
            self.w(" array MeshFace faceNormals[nFaceNormals];")
            self.w("}")
            self.w()
        self.w("template MeshMaterialList {")
        self.w(" <f6f23f42-7686-11cf-8f52-0040333594a3>")
        self.w(" DWORD nMaterials;")
        self.w(" DWORD nFaceIndexes;")
        self.w(" array DWORD faceIndexes[nFaceIndexes];")
        self.w(" [Material <3d82ab4d-62da-11cf-ab39-0020af71e433>]")
        self.w("}")
        self.w()
        if geom_format == 'meshnormals':
            self.w("template Coords2d {")
            self.w(" <f6f23f44-7686-11cf-8f52-0040333594a3>")
            self.w(" FLOAT u;")
            self.w(" FLOAT v;")
            self.w("}")
            self.w()
            self.w("template MeshTextureCoords {")
            self.w(" <f6f23f40-7686-11cf-8f52-0040333594a3>")
            self.w(" DWORD nTextureCoords;")
            self.w(" array Coords2d textureCoords[nTextureCoords];")
            self.w("}")
            self.w()
        else:
            # DeclData path: declare only VertexElement + DeclData, exactly like
            # the working reference files (no unused MeshNormals/MeshTextureCoords/
            # FVFData templates, which a strict viewer may reject).
            self.w("template VertexElement {")
            self.w(" <f752461c-1e23-48f6-b9f8-8350850f336f>")
            self.w(" DWORD Type;")
            self.w(" DWORD Method;")
            self.w(" DWORD Usage;")
            self.w(" DWORD UsageIndex;")
            self.w("}")
            self.w()
            self.w("template DeclData {")
            self.w(" <bf22e553-292c-4781-9fea-62bd554bdd93>")
            self.w(" DWORD nElements;")
            self.w(" array VertexElement Elements[nElements];")
            self.w(" DWORD nDWords;")
            self.w(" array DWORD data[nDWords];")
            self.w("}")
            self.w()

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def safe_name(obj_or_str, duplicate_name=False, counter=[0]):
    """Return a DirectX .x-safe identifier while preserving pipeline names.

    Important: Master assets can legally contain repeated names under different
    parents, for example LOD_High, LOD_Medium, Dmg0 and Dmg1.  Therefore this
    function does NOT make names unique unless Duplicate Name is explicitly ON.

    MeshView is stricter than Deep/AXE and rejects identifiers containing
    characters such as #, spaces, dots, colons, brackets, etc.  Those characters
    are converted to underscores, but normal pipeline names remain unchanged.
    """
    try:
        if isinstance(obj_or_str, str):
            raw = obj_or_str
        else:
            raw = obj_or_str.GetName()
    except Exception:
        raw = "Object"

    raw = str(raw or "Object")
    out = []
    for ch in raw:
        # DirectX identifiers are safest as [A-Za-z_][A-Za-z0-9_]*.
        if ch.isalnum() or ch == "_":
            out.append(ch)
        else:
            out.append("_")
    n = "".join(out).strip("_")
    if not n:
        n = "Object"
    if n[0].isdigit():
        n = "_" + n
    # Preserve the exact case of pipeline names.
    # Tiltan Model Viewer / splitter is case-sensitive for Lod_High,
    # Lod_Medium, Lod_Low and AP_body.  Do not normalize Lod_* to LOD_*.
    if duplicate_name:
        counter[0] += 1
        n = f"{n}_{counter[0]:04d}"
    return n


def mat4x4_str(m):
    vals = [
        m.v1.x, m.v1.y, m.v1.z, 0.0,
        m.v2.x, m.v2.y, m.v2.z, 0.0,
        m.v3.x, m.v3.y, m.v3.z, 0.0,
        m.off.x, m.off.y, m.off.z, 1.0,
    ]
    return ", ".join(f"{v:.6f}" for v in vals)


def identity_mat_str():
    return ("1.000000, 0.000000, 0.000000, 0.000000,"
            "0.000000, 1.000000, 0.000000, 0.000000,"
            "0.000000, 0.000000, 1.000000, 0.000000,"
            "0.000000, 0.000000, 0.000000, 1.000000")


def _as_path_string(value):
    """Convert C4D Filename/string-like values to a normal filesystem path."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    for attr in ("GetString", "GetFileString"):
        fn = getattr(value, attr, None)
        if fn:
            try:
                v = fn()
                if v:
                    return str(v)
            except Exception:
                pass
    return str(value)


def _basename_any(path):
    p = _as_path_string(path).strip()
    if not p:
        return ""
    return os.path.basename(p.replace("\\", "/"))


def _find_bitmap_shader(shader):
    """Return the first bitmap shader under shader, including simple layer/fusion children."""
    if shader is None:
        return None
    try:
        if shader.GetType() == c4d.Xbitmap:
            return shader
    except Exception:
        pass

    stack = []
    try:
        child = shader.GetDown()
        while child:
            stack.append(child)
            child = child.GetNext()
    except Exception:
        pass

    seen = set()
    while stack:
        sh = stack.pop()
        sid = id(sh)
        if sid in seen:
            continue
        seen.add(sid)
        try:
            if sh.GetType() == c4d.Xbitmap:
                return sh
        except Exception:
            pass
        try:
            child = sh.GetDown()
            while child:
                stack.append(child)
                child = child.GetNext()
        except Exception:
            pass
    return None


def _material_bitmap_path(mat):
    """Return the diffuse bitmap path from a standard C4D material, if present."""
    if mat is None:
        return ""
    try:
        if not mat.GetChannelState(c4d.CHANNEL_COLOR):
            return ""
    except Exception:
        pass
    try:
        shader = mat[c4d.MATERIAL_COLOR_SHADER]
    except Exception:
        shader = None
    bitmap_shader = _find_bitmap_shader(shader)
    if not bitmap_shader:
        return ""
    try:
        return _as_path_string(bitmap_shader[c4d.BITMAPSHADER_FILENAME])
    except Exception:
        return ""


def _material_has_texture(mat):
    return bool(_material_bitmap_path(mat))


def _resolve_texture_source(tex_path, settings):
    """Resolve absolute and C4D relative texture paths."""
    raw = _as_path_string(tex_path).strip()
    if not raw:
        return ""
    raw_norm = raw.replace("\\", os.sep).replace("/", os.sep)
    base = os.path.basename(raw_norm)
    doc_dir = settings.get('doc_dir', '') or ''
    out_dir = settings.get('output_dir', '') or ''

    candidates = []
    if os.path.isabs(raw_norm):
        candidates.append(raw_norm)
    else:
        candidates.append(raw_norm)
        if doc_dir:
            candidates.append(os.path.join(doc_dir, raw_norm))
            candidates.append(os.path.join(doc_dir, base))
            candidates.append(os.path.join(doc_dir, 'tex', base))
        if out_dir:
            candidates.append(os.path.join(out_dir, raw_norm))
            candidates.append(os.path.join(out_dir, base))

    seen = set()
    for c in candidates:
        try:
            n = os.path.normpath(c)
        except Exception:
            continue
        if n in seen:
            continue
        seen.add(n)
        try:
            if os.path.isfile(n):
                return n
        except Exception:
            pass
    return ""


def _save_bitmap_with_c4d(src, dst, fmt):
    """Convert a bitmap through Cinema 4D's bitmap API.

    This is used when the UI checkbox 'Convert to' is enabled.  It is especially
    important for BMP support because simply copying a PNG and renaming it to
    .bmp will create an invalid texture file for external DirectX tools.
    """
    fmt = (fmt or "").lower().strip()
    filter_name = {
        'bmp': 'FILTER_BMP',
        'png': 'FILTER_PNG',
        'tga': 'FILTER_TGA',
        'jpg': 'FILTER_JPG',
        'jpeg': 'FILTER_JPG',
    }.get(fmt)
    if not filter_name:
        _log_export("Texture conversion unsupported format: %s" % fmt)
        return False
    try:
        filt = getattr(c4d, filter_name)
    except Exception:
        _log_export("Cinema 4D bitmap filter missing: %s" % filter_name)
        return False

    try:
        bmp = c4d.bitmaps.BaseBitmap()
        res = bmp.InitWith(src)
        # InitWith usually returns (result, ismovie), but older versions may differ.
        if isinstance(res, tuple):
            err = res[0]
        else:
            err = res
        if err != c4d.IMAGERESULT_OK:
            _log_export("Texture load failed for conversion: %s result=%s" % (src, str(err)))
            return False
        saved = bmp.Save(dst, filt)
        if saved != c4d.IMAGERESULT_OK:
            _log_export("Texture save failed: %s result=%s" % (dst, str(saved)))
            return False
        _log_export("Converted texture: %s -> %s" % (src, dst))
        return True
    except Exception as e:
        _log_export("Texture conversion failed: %s -> %s : %r" % (src, dst, e))
        return False



def _copy_related_texture_maps(main_tex_path, output_dir, settings=None):
    """Copy/convert external engine-side maps next to the exported .x.

    These maps are intentionally NOT written into TextureFilename blocks.  The
    renderer/splitter uses them later by naming convention.  Supported suffixes
    include damage, heat, IR, normal and specular maps, for example:

        Asset_Material.bmp
        Asset_Material_Dmg2.bmp
        Asset_Material_Heatmap.bmp
        Asset_Material_irMap.bmp
        Asset_Material_NormalMap.bmp
        Asset_Material_SpecularMap.bmp
    """
    settings = settings or {}
    copied = []
    try:
        src = _resolve_texture_source(main_tex_path, settings)
        if not src or not os.path.isfile(src):
            return copied
        src_dir = os.path.dirname(src)
        src_stem = os.path.splitext(os.path.basename(src))[0]
        if not src_dir or not src_stem:
            return copied
        keywords = ("dmg", "heatmap", "heat_map", "irmap", "ir_map", "normalmap", "normal_map", "specularmap", "specular_map")
        image_exts = ('.bmp', '.png', '.tga', '.tif', '.tiff', '.jpg', '.jpeg')
        try:
            names = os.listdir(src_dir)
        except Exception:
            names = []
        for fn in names:
            low = fn.lower()
            if not low.endswith(image_exts):
                continue
            stem, _ext = os.path.splitext(fn)
            if stem == src_stem:
                continue
            stem_low = stem.lower()
            # Prefer maps that share the exact main texture stem, but also allow
            # nearby maps that start with the same asset prefix before the first
            # material suffix.  This keeps SA65_* map sets together without
            # hardcoding body/turret/rocket names.
            same_main = stem_low.startswith(src_stem.lower() + "_")
            same_asset = False
            asset_prefix = src_stem.split('_')[0].lower() if '_' in src_stem else ''
            if asset_prefix:
                same_asset = stem_low.startswith(asset_prefix + "_")
            if not (same_main or same_asset):
                continue
            if not any(k in stem_low for k in keywords):
                continue
            full = os.path.join(src_dir, fn)
            out_name = _copy_texture_next_to_x(full, output_dir, None, overwrite=settings.get('overwrite', False), settings=settings)
            if out_name:
                copied.append(out_name)
                _log_export("Copied related external map (not written to .x): %s" % out_name)
    except Exception as e:
        _log_export("Related texture map copy failed: %r" % (e,))
    return copied

def _copy_texture_next_to_x(tex_path, output_dir, output_name=None, overwrite=False, settings=None):
    """Copy or convert a texture next to the exported .x file.

    MeshView/AXE resolves TextureFilename best when the bitmap is next to the .x
    file.  When Convert to BMP/PNG/TGA is requested, this performs a real bitmap
    conversion instead of only changing the filename extension.
    """
    try:
        if not tex_path or not output_dir:
            return None
        settings = settings or {}
        src = _resolve_texture_source(tex_path, settings)
        if not src:
            _log_export("Texture source not found: %s" % str(tex_path))
            return None

        src_base = _basename_any(src)
        if not src_base:
            return None
        src_stem, src_ext = os.path.splitext(src_base)
        requested_fmt = str(settings.get('tex_format', '') or '').lower().strip()
        convert_requested = bool(settings.get('convert_tex', False) and requested_fmt)

        dst_name = output_name or src_base
        if convert_requested:
            dst_name = src_stem + "." + requested_fmt

        try:
            os.makedirs(output_dir, exist_ok=True)
        except Exception:
            pass

        dst = os.path.join(output_dir, dst_name)
        needs_write = overwrite or (not os.path.isfile(dst))

        if convert_requested:
            if needs_write:
                ok = _save_bitmap_with_c4d(src, dst, requested_fmt)
                if ok:
                    return dst_name
                # Do not lie in TextureFilename. If conversion failed, copy the original
                # with its real extension so the external viewer still has a valid file.
                fallback_name = src_base
                fallback_dst = os.path.join(output_dir, fallback_name)
                if os.path.abspath(src) != os.path.abspath(fallback_dst):
                    if overwrite or not os.path.isfile(fallback_dst):
                        shutil.copy2(src, fallback_dst)
                        _log_export("Copied original texture fallback: %s -> %s" % (src, fallback_dst))
                return fallback_name
            return dst_name

        try:
            if os.path.abspath(src) != os.path.abspath(dst):
                if needs_write:
                    shutil.copy2(src, dst)
                    _log_export("Copied texture: %s -> %s" % (src, dst))
        except Exception as e:
            _log_export("Texture copy failed: %s -> %s : %r" % (src, dst, e))
        return dst_name
    except Exception as e:
        _log_export("Texture copy/convert failed: %r" % (e,))
        return None


def _max_style_cube_uvs_24():
    """Original AXE/Max fallback: the whole bitmap is repeated on every cube face."""
    face = [(1.0, 2.0), (0.0, 1.0), (1.0, 1.0), (0.0, 2.0)]
    return face * 6


def _cube_atlas_uvs_24():
    """Six-face cube atlas UVs.

    v14 repeated the full texture on every face.  That loads in MeshView, but when
    the source bitmap is a six-panel facade strip the result looks stretched and
    unclear.  This layout keeps the 3ds Max/AXE per-face corner order, but assigns
    one sixth of the U range to each cube face.
    """
    uvs = []
    for face_index in range(6):
        u0 = float(face_index) / 6.0
        u1 = float(face_index + 1) / 6.0
        uvs.extend([
            (u1, 2.0),
            (u0, 1.0),
            (u1, 1.0),
            (u0, 2.0),
        ])
    return uvs


def _cube_atlas_face_uv(face_index, local_i, face_count=6):
    try:
        face_count = max(1, int(face_count))
        face_index = max(0, int(face_index)) % face_count
        u0 = float(face_index) / float(face_count)
        u1 = float(face_index + 1) / float(face_count)
    except Exception:
        u0, u1 = 0.0, 1.0
    face = [(u1, 2.0), (u0, 1.0), (u1, 1.0), (u0, 2.0)]
    try:
        return face[int(local_i)]
    except Exception:
        return (0.0, 0.0)


def _is_all_zero_uvs(uvs_list):
    if not uvs_list:
        return True
    for uvs in uvs_list:
        for u, v in uvs:
            if abs(float(u)) > 1.0e-8 or abs(float(v)) > 1.0e-8:
                return False
    return True


def hpb_to_quat(h, p, b):
    cy, sy = math.cos(h * 0.5), math.sin(h * 0.5)
    cp, sp = math.cos(p * 0.5), math.sin(p * 0.5)
    cr, sr = math.cos(b * 0.5), math.sin(b * 0.5)
    qw = cr*cp*cy + sr*sp*sy
    qx = cr*sp*cy + sr*cp*sy
    qy = cr*cp*sy - sr*sp*cy
    qz = sr*cp*cy - cr*sp*sy
    return qw, qx, qy, qz



def _safe_export_scale(settings):
    try:
        s = float(settings.get('scale', 1.0))
    except Exception:
        s = 1.0
    if abs(s) < 1.0e-8:
        s = 1.0

    try:
        if settings.get('convert_units_to_meters', True):
            s *= float(settings.get('unit_to_meter_scale', 1.0))
    except Exception:
        pass
    return s



def _apply_global_scale_to_matrix_translation(m, settings):
    """Scale only the translation component of a C4D Matrix.

    Mesh vertices are scaled separately in extract_mesh(). Scaling only m.off
    keeps the hierarchy proportional without double-scaling local rotations or
    object basis vectors.
    """
    try:
        s = _safe_export_scale(settings)
        if abs(s - 1.0) < 1.0e-8:
            return m
        mm = c4d.Matrix(m)
        mm.off.x *= s
        mm.off.y *= s
        mm.off.z *= s
        return mm
    except Exception:
        return m


def _effective_coord_settings(settings):
    """Coordinate conversion used for mesh vertices.

    Tiltan compatibility preserves the P/N hierarchy and local matrices.
    The only safe visible transform option is a uniform global scale.

    To scale a hierarchy correctly, we scale both:
      1. mesh-local vertex coordinates here
      2. FrameTransformMatrix translations in write_frame_recursive

    Axis rotations / handedness conversion are intentionally disabled in
    Tiltan mode because they can desync mesh geometry from dummy/attach frames.
    """
    if settings.get('preserve_full_hierarchy', False) or settings.get('asset_mode', False):
        return 0.0, 0.0, _safe_export_scale(settings), False
    return (
        settings.get('xrot', 0.0),
        settings.get('zrot', 0.0),
        _safe_export_scale(settings),
        settings.get('right_handed', False),
    )


def apply_coord_system(v, xrot_deg, zrot_deg, scale, right_handed, tiltan_axis_zup_yfront=False):
    x, y, z = v
    x *= scale
    y *= scale
    z *= scale

    if tiltan_axis_zup_yfront:
        return _tiltan_axis_vec_tuple(x, y, z)

    if right_handed:
        z = -z
    xr = math.radians(xrot_deg)
    zr = math.radians(zrot_deg)
    # X rotation
    ny = y * math.cos(xr) - z * math.sin(xr)
    nz = y * math.sin(xr) + z * math.cos(xr)
    y, z = ny, nz
    # Z rotation
    nx = x * math.cos(zr) - y * math.sin(zr)
    ny = x * math.sin(zr) + y * math.cos(zr)
    x, y = nx, ny
    return x, y, z





def _find_polygon_in_cache_tree(op):
    """Return first PolygonObject inside a cache/deform-cache tree.

    Important: this function is for C4D generated cache trees only. It may recurse
    into cache children, but it must not be used to walk the live Object Manager
    hierarchy of a normal Null, otherwise parent Nulls/Dummies would accidentally
    export their first child's mesh and duplicate/flatten the asset.
    """
    if op is None:
        return None
    try:
        if op.GetType() == c4d.Opolygon:
            return op
    except Exception:
        return None
    try:
        dc = op.GetDeformCache()
        hit = _find_polygon_in_cache_tree(dc)
        if hit:
            return hit
    except Exception:
        pass
    try:
        c = op.GetCache()
        hit = _find_polygon_in_cache_tree(c)
        if hit:
            return hit
    except Exception:
        pass
    try:
        child = op.GetDown()
        while child:
            hit = _find_polygon_in_cache_tree(child)
            if hit:
                return hit
            child = child.GetNext()
    except Exception:
        pass
    return None


def _find_polygon_cache(obj):
    """Return the polygon output of obj itself, never of its Object Manager children."""
    if obj is None:
        return None
    try:
        if obj.GetType() == c4d.Opolygon:
            return obj
    except Exception:
        return None

    # Do not recurse into obj.GetDown() here. Child traversal belongs to the
    # hierarchy exporter. For generator/primitive/deformer objects, use only C4D
    # evaluated cache/deform-cache output.
    try:
        dc = obj.GetDeformCache()
        hit = _find_polygon_in_cache_tree(dc)
        if hit:
            return hit
    except Exception:
        pass
    try:
        c = obj.GetCache()
        hit = _find_polygon_in_cache_tree(c)
        if hit:
            return hit
    except Exception:
        pass
    return None


def _has_polygon_output(obj):
    return _find_polygon_cache(obj) is not None

# ─────────────────────────────────────────────────────────────────────────────
# Mesh Extractor — full vertex splitting
# ─────────────────────────────────────────────────────────────────────────────

def extract_mesh(obj, settings, tag_source=None):
    """
    Returns dict: verts, normals, tangents, uvs_list (per UV channel),
                  vcols, faces, mat_per_face, materials, orig_indices
    """
    if obj.GetType() != c4d.Opolygon:
        return None

    tag_source = tag_source or obj

    points  = obj.GetAllPoints()
    polys   = obj.GetAllPolygons()
    if not points or not polys:
        return None

    xrot, zrot, scale, rh = _effective_coord_settings(settings)

    # ── Phong Normals ────────────────────────────────────────────────────────
    phong_normals = None
    if settings.get('normals', True):
        try:
            phong_normals = obj.CreatePhongNormals()
        except Exception:
            pass

    # Caches/generators can store UV tags on the evaluated polygon cache while
    # the visible object stores the material tag.  Read tags from both sources
    # without duplicating them.
    tag_sources = []
    for src in (tag_source, obj):
        if src is not None and src not in tag_sources:
            tag_sources.append(src)

    # ── UV channels ─────────────────────────────────────────────────────────
    uv_tags = []
    multi = settings.get('multi_tex', False)
    for src in tag_sources:
        try:
            tags = src.GetTags()
        except Exception:
            tags = []
        for tag in tags:
            if tag.GetType() == c4d.Tuvw and tag not in uv_tags:
                uv_tags.append(tag)
                if not multi:
                    break
        if uv_tags and not multi:
            break

    # ── Vertex Colors ────────────────────────────────────────────────────────
    vcol_tag = None
    if settings.get('vertexcolor', False):
        for src in tag_sources:
            try:
                vcol_tag = src.GetTag(c4d.Tvertexcolor)
            except Exception:
                vcol_tag = None
            if vcol_tag:
                break

    # ── Materials per face ───────────────────────────────────────────────────
    mat_per_face = [0] * len(polys)
    mat_list = []
    mat_name_map = {}

    for src in tag_sources:
        try:
            tags = src.GetTags()
        except Exception:
            tags = []
        for tag in tags:
            if tag.GetType() == c4d.Ttexture:
                mat = tag.GetMaterial()
                if mat is None:
                    continue
                mat_id = id(mat)
                if mat_id not in mat_name_map:
                    mat_name_map[mat_id] = len(mat_list)
                    mat_list.append(mat)
                midx = mat_name_map[mat_id]
                sel_name = tag[c4d.TEXTURETAG_RESTRICTION]
                if sel_name:
                    # Restriction selections can live on the visible source object
                    # or on the evaluated mesh cache.  Try both.
                    for sel_src in tag_sources:
                        t = sel_src.GetFirstTag() if sel_src else None
                        while t:
                            if t.GetType() == c4d.Tpolygonselection and t.GetName() == sel_name:
                                bs = t.GetBaseSelect()
                                for fi in range(len(polys)):
                                    if bs.IsSelected(fi):
                                        mat_per_face[fi] = midx
                                t = None
                                break
                            t = t.GetNext()
                else:
                    for fi in range(len(polys)):
                        mat_per_face[fi] = midx

    if not mat_list:
        mat_list = [None]

    # ── Split vertices ───────────────────────────────────────────────────────
    # Some C4D generated/cubic projections do not bake a useful UVW tag into the
    # generator cache.  MeshView then receives all UVs as 0,0 and shows a flat gray
    # material.  When the object has a bitmap material but no useful UV channel,
    # generate the same simple per-face cube UV layout as the Max .x file.
    fallback_uv = False
    try:
        fallback_uv = settings.get('texcoords', True) and (not uv_tags) and any(_material_has_texture(m) for m in mat_list)
    except Exception:
        fallback_uv = settings.get('texcoords', True) and (not uv_tags)

    split_verts   = []
    split_normals = []
    split_uvs     = [[] for _ in uv_tags]
    if fallback_uv:
        split_uvs = [[]]
    split_vcols   = []
    orig_indices  = []
    face_list     = []
    key_map       = {}

    POLY_VMAP = [0, 1, 2, 3]
    fallback_face_uv = [(1.0, 2.0), (0.0, 1.0), (1.0, 1.0), (0.0, 2.0)]
    fallback_is_cube_atlas = fallback_uv and len(polys) == 6

    for fi, poly in enumerate(polys):
        corner_ids = [poly.a, poly.b, poly.c, poly.d]
        is_tri     = (poly.c == poly.d)
        triangles  = [(0,1,2)] if is_tri else [(0,1,2), (0,2,3)]

        for tri in triangles:
            face_verts = []
            for local_i in tri:
                vi = corner_ids[local_i]

                # Normal
                nx, ny, nz = 0.0, 1.0, 0.0
                if phong_normals:
                    ni = fi * 4 + local_i
                    if ni < len(phong_normals):
                        n = phong_normals[ni]
                        nx, ny, nz = n.x, n.y, n.z

                # UVs
                uvs = []
                if fallback_uv:
                    if fallback_is_cube_atlas:
                        uvs.append(_cube_atlas_face_uv(fi, local_i, 6))
                    else:
                        uvs.append(fallback_face_uv[local_i])
                else:
                    for uvi, utag in enumerate(uv_tags):
                        try:
                            # C4D 2026 is more reliable with GetSlow() for UVWTag.
                            # Some builds return valid-looking data addresses but 0,0
                            # coordinates through the older static GetUVW path.
                            try:
                                uv = utag.GetSlow(fi)
                            except Exception:
                                addr = utag.GetDataAddressR()
                                uv = c4d.UVWTag.GetUVW(addr, fi)
                            uv_corners = [uv['a'], uv['b'], uv['c'], uv['d']]
                            u = uv_corners[local_i].x
                            v = uv_corners[local_i].y  # No V-flip: MeshView/AXE uses same UV convention as C4D
                            uvs.append((round(u, 5), round(v, 5)))
                        except Exception:
                            uvs.append((0.0, 0.0))

                # Vertex color
                vc = (1.0, 1.0, 1.0, 1.0)
                if vcol_tag:
                    try:
                        addr = vcol_tag.GetDataAddressR()
                        col  = c4d.VertexColorTag.GetPoint(addr, None, None, vi)
                        vc   = (col.x, col.y, col.z, 1.0)
                    except Exception:
                        pass

                uv_key = tuple(u for pair in uvs for u in pair)
                key = (vi, round(nx,4), round(ny,4), round(nz,4)) + uv_key

                if key not in key_map:
                    key_map[key] = len(split_verts)
                    p  = points[vi]
                    px, py, pz = apply_coord_system((p.x, p.y, p.z), xrot, zrot, scale, rh, settings.get('tiltan_axis_zup_yfront', True))
                    split_verts.append((px, py, pz))
                    nnx, nny, nnz = apply_coord_system((nx, ny, nz), xrot, zrot, 1.0, rh, settings.get('tiltan_axis_zup_yfront', True))
                    split_normals.append((nnx, nny, nnz))
                    for uvi, uv_pair in enumerate(uvs):
                        split_uvs[uvi].append(uv_pair)
                    split_vcols.append(vc)
                    orig_indices.append(vi)

                face_verts.append(key_map[key])
            face_list.append((face_verts, mat_per_face[fi]))

    try:
        if settings.get('texcoords', True) and len(split_verts) == 24 and _is_all_zero_uvs(split_uvs) and any(_material_has_texture(m) for m in mat_list):
            split_uvs = [_cube_atlas_uvs_24()]
    except Exception:
        pass

    return {
        'verts':       split_verts,
        'normals':     split_normals,
        'uvs_list':    split_uvs,
        'vcols':       split_vcols,
        'faces':       face_list,
        'materials':   mat_list,
        'orig_indices': orig_indices,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Write Functions
# ─────────────────────────────────────────────────────────────────────────────

def write_material_block(w, mat, settings, tex_format):
    if mat is None:
        w.w("Material DefaultMaterial {")
        w.w(" 1.000000;1.000000;1.000000;1.000000;;")
        w.w(" 100.000000;")
        w.w(" 0.000000;0.000000;0.000000;;")
        w.w(" 0.000000;0.000000;0.000000;;")
        w.w("}")
        w.w()
        return

    name = safe_name(mat)

    col   = c4d.Vector(0.8, 0.8, 0.8)
    spec  = c4d.Vector(1.0, 1.0, 1.0)
    emiss = c4d.Vector(0.0, 0.0, 0.0)
    power = 10.0
    alpha = 1.0

    try:
        if mat.GetChannelState(c4d.CHANNEL_COLOR):
            col = mat[c4d.MATERIAL_COLOR_COLOR]
    except Exception:
        pass
    # Legacy Tiltan viewer compatibility: old working exporter wrote white
    # specular color for textured materials.  Some splitter builds are fragile
    # with black specular values in global Material blocks.
    try:
        if mat.GetChannelState(c4d.CHANNEL_SPECULAR):
            power = mat[c4d.MATERIAL_SPECULAR_WIDTH] * 100.0
    except Exception:
        pass
    try:
        if mat.GetChannelState(c4d.CHANNEL_LUMINANCE):
            emiss = mat[c4d.MATERIAL_LUMINANCE_COLOR]
    except Exception:
        pass
    try:
        if mat.GetChannelState(c4d.CHANNEL_TRANSPARENCY):
            alpha = 1.0 - mat[c4d.MATERIAL_TRANSPARENCY_BRIGHTNESS]
    except Exception:
        pass

    # MeshView/AXE displays textured materials best with white diffuse.
    if _material_has_texture(mat) or settings.get('diffuse_map_color', False):
        col = c4d.Vector(1.0, 1.0, 1.0)

    w.w(f"Material {name} {{")
    w.w(f" {col.x:.6f}; {col.y:.6f}; {col.z:.6f}; {alpha:.6f};;")
    w.w(f" {power:.6f};")
    w.w(f" {spec.x:.6f}; {spec.y:.6f}; {spec.z:.6f};;")
    w.w(f" {emiss.x:.6f}; {emiss.y:.6f}; {emiss.z:.6f};;")

    if settings.get('textures', True):
        try:
            tex_path = _material_bitmap_path(mat)
            if tex_path:
                fname = _basename_any(tex_path)
                base, _ext = os.path.splitext(fname)
                if settings.get('convert_tex', False):
                    fname = base + "." + tex_format.lower()
                copied_name = _copy_texture_next_to_x(
                    tex_path,
                    settings.get('output_dir', ''),
                    fname,
                    overwrite=settings.get('overwrite', False),
                    settings=settings
                )
                if copied_name:
                    fname = copied_name
                # Only the Color/diffuse map is exported and referenced. All
                # other maps (NormalMap, SpecularMap, IRMap, LightMap, Dmg, ...)
                # are produced and placed in the model folder manually by the
                # user, so the plugin neither copies nor references them.
                w.w(" TextureFilename {")
                w.w(f"  \"{fname}\";")
                w.w(" }")
        except Exception:
            pass

    w.w("}")
    w.w()


def _float_to_dword(f):
    """Reinterpret a 32-bit float's bits as an unsigned DWORD (little-endian),
    matching the DeclData/FVFData packing used by AXE / 3ds Max .x files."""
    return struct.unpack('<I', struct.pack('<f', float(f)))[0]


def _select_geom_format(settings):
    """Decide which per-vertex geometry container to emit.

    The model-viewer's main .x file uses DeclData (verified against the
    working reference assets such as SA22.X), so 'auto' defaults to DeclData.
    An explicit override can be forced via settings['geom_format'].
    """
    forced = str(settings.get('geom_format', 'auto') or 'auto').lower().strip()
    if forced == 'fvfdata':
        # FVFData template is declared for engine compatibility, but the
        # reference assets emit geometry via DeclData; match that.
        return 'decldata'
    if forced in ('decldata', 'meshnormals'):
        return forced
    # Auto: Tiltan Model Viewer / the older working exporter expect classic
    # MeshNormals + MeshTextureCoords. DeclData can load as 0 polygons.
    return 'meshnormals'


def write_decldata_block(w, mesh_data, settings, ind=""):
    """Write per-vertex data packed as a DeclData block (normals, uvs,
    optional tangent/bitangent), exactly as AXE/3ds Max text .x files do."""
    verts    = mesh_data['verts']
    normals  = mesh_data['normals']
    uvs_list = mesh_data['uvs_list']
    nverts   = len(verts)

    want_n  = settings.get('normals', True) and bool(normals)
    want_t  = settings.get('tangent', False)
    want_b  = settings.get('bitangent', False)
    uvs     = uvs_list[0] if (settings.get('texcoords', True) and uvs_list and uvs_list[0]) else None

    # Build the element declaration list.
    # Type:  2 = FLOAT3, 1 = FLOAT2
    # Usage: 3 = NORMAL, 5 = TEXCOORD, 6 = TANGENT, 7 = BINORMAL
    elements = []
    if want_n:
        elements.append((2, 0, 3, 0))   # normal  (FLOAT3)
    if want_t:
        elements.append((2, 0, 6, 0))   # tangent (FLOAT3)
    if want_b:
        elements.append((2, 0, 7, 0))   # binormal(FLOAT3)
    if uvs:
        elements.append((1, 0, 5, 0))   # texcoord(FLOAT2)

    if not elements:
        return

    # Pack the raw DWORD stream, one vertex after another.
    dwords = []
    for vi in range(nverts):
        if want_n:
            nx, ny, nz = normals[vi]
            dwords += [_float_to_dword(nx), _float_to_dword(ny), _float_to_dword(nz)]
        if want_t:
            dwords += [_float_to_dword(0.0), _float_to_dword(0.0), _float_to_dword(0.0)]
        if want_b:
            dwords += [_float_to_dword(0.0), _float_to_dword(0.0), _float_to_dword(0.0)]
        if uvs:
            u, v = uvs[vi] if vi < len(uvs) else (0.0, 0.0)
            dwords += [_float_to_dword(u), _float_to_dword(v)]

    w.w(f"{ind} DeclData {{")
    w.w(f"{ind}  {len(elements)};")
    for ei, (t, m, usg, idx) in enumerate(elements):
        sep = "," if ei < len(elements) - 1 else ";"
        w.w(f"{ind}  {t};{m};{usg};{idx};{sep}")
    w.w(f"{ind}  {len(dwords)};")
    for di, d in enumerate(dwords):
        sep = "," if di < len(dwords) - 1 else ";"
        w.w(f"{ind}  {d}{sep}")
    w.w(f"{ind} }}")
    w.w()


def write_mesh_block(w, name, mesh_data, settings, inline=False, ind=""):
    verts    = mesh_data['verts']
    normals  = mesh_data['normals']
    uvs_list = mesh_data['uvs_list']
    vcols    = mesh_data['vcols']
    faces    = mesh_data['faces']
    mats     = mesh_data['materials']
    orig_idx = mesh_data['orig_indices']

    geom_format = _select_geom_format(settings)

    mesh_name = name
    w.w(f"{ind}Mesh {mesh_name} {{")

    # Vertices
    w.w(f"{ind} {len(verts)};")
    for i, (x, y, z) in enumerate(verts):
        sep = "," if i < len(verts)-1 else ";"
        w.w(f"{ind} {x:.6f}; {y:.6f}; {z:.6f};{sep}")
    w.w()

    # Faces
    w.w(f"{ind} {len(faces)};")
    for i, (tri, _) in enumerate(faces):
        sep = "," if i < len(faces)-1 else ";"
        w.w(f"{ind} 3; {tri[0]},{tri[1]},{tri[2]};{sep}")
    w.w()

    # MeshNormals (only in the classic MeshNormals format)
    if geom_format == 'meshnormals' and settings.get('normals', True) and normals:
        w.w(f"{ind} MeshNormals {{")
        w.w(f"{ind}  {len(normals)};")
        for i, (nx, ny, nz) in enumerate(normals):
            sep = "," if i < len(normals)-1 else ";"
            w.w(f"{ind}  {nx:.6f}; {ny:.6f}; {nz:.6f};{sep}")
        w.w()
        w.w(f"{ind}  {len(faces)};")
        for i, (tri, _) in enumerate(faces):
            sep = "," if i < len(faces)-1 else ";"
            w.w(f"{ind}  3; {tri[0]},{tri[1]},{tri[2]};{sep}")
        w.w(f"{ind} }}")
        w.w()

    # MeshMaterialList
    if settings.get('material', True):
        w.w(f"{ind} MeshMaterialList {{")
        w.w(f"{ind}  {len(mats)};")
        w.w(f"{ind}  {len(faces)};")
        for i, (_, midx) in enumerate(faces):
            sep = "," if i < len(faces)-1 else ";"
            w.w(f"{ind}  {midx}{sep}")
        w.w()
        if inline:
            # Inline materials inside mesh block
            for mat in mats:
                write_material_block(w, mat, settings, settings.get('tex_format','tga'))
        else:
            for mat in mats:
                mname = safe_name(mat) if mat else "DefaultMaterial"
                w.w(f"{ind}  {{ {mname} }}")
        w.w(f"{ind} }}")
        w.w()


    # DeclData (packed normals/uvs/tangents) - modern format
    if geom_format == 'decldata':
        write_decldata_block(w, mesh_data, settings, ind=ind)

    # MeshTextureCoords (only in the classic MeshNormals format)
    if geom_format == 'meshnormals' and settings.get('texcoords', True) and uvs_list:
        for uvi, uvs in enumerate(uvs_list):
            if not uvs:
                continue
            suffix = f"c{uvi+1}"
            w.w(f"{ind} MeshTextureCoords {suffix} {{")
            w.w(f"{ind}  {len(uvs)};")
            for i, (u, v) in enumerate(uvs):
                sep = "," if i < len(uvs)-1 else ";"
                w.w(f"{ind}  {u:.6f}; {v:.6f};{sep}")
            w.w(f"{ind} }}")
            w.w()

    # MeshVertexColors
    if settings.get('vertexcolor', False) and vcols:
        w.w(f"{ind} MeshVertexColors {{")
        w.w(f"{ind}  {len(vcols)};")
        for i, (r, g, b, a) in enumerate(vcols):
            sep = "," if i < len(vcols)-1 else ";"
            w.w(f"{ind}  {i}; {r:.6f}; {g:.6f}; {b:.6f}; {a:.6f};;{sep}")
        w.w(f"{ind} }}")
        w.w()

    # VertexDuplicationIndices
    if settings.get('vertex_dup', False):
        n_orig = len(set(orig_idx)) if orig_idx else len(verts)
        w.w(f"{ind} VertexDuplicationIndices {{")
        w.w(f"{ind}  {len(orig_idx)};")
        w.w(f"{ind}  {n_orig};")
        for i, oi in enumerate(orig_idx):
            sep = "," if i < len(orig_idx)-1 else ";"
            w.w(f"{ind}  {oi}{sep}")
        w.w(f"{ind} }}")
        w.w()

    w.w(f"{ind}}}")
    w.w()



def _collect_materials_for_object(obj):
    mats = []
    try:
        for tag in obj.GetTags():
            if tag.GetType() == c4d.Ttexture:
                mat = tag.GetMaterial()
                if mat and mat not in mats:
                    mats.append(mat)
    except Exception:
        pass
    return mats or [None]


def extract_cube_primitive_mesh(obj, settings):
    """AXE/MeshView-safe cube export for C4D parametric Cube objects."""
    try:
        if obj.GetType() != c4d.Ocube:
            return None
    except Exception:
        return None

    # C4D Cube size is stored as a Vector in PRIM_CUBE_LEN. Fallback to 200 like the default cube.
    try:
        size = obj[c4d.PRIM_CUBE_LEN]
        sx, sy, sz = float(size.x), float(size.y), float(size.z)
    except Exception:
        sx = sy = sz = 0.0
    if abs(sx) < 1.0e-8 or abs(sy) < 1.0e-8 or abs(sz) < 1.0e-8:
        try:
            rad = obj.GetRad()
            sx, sy, sz = abs(float(rad.x))*2.0, abs(float(rad.y))*2.0, abs(float(rad.z))*2.0
        except Exception:
            sx = sy = sz = 0.0
    if abs(sx) < 1.0e-8 or abs(sy) < 1.0e-8 or abs(sz) < 1.0e-8:
        sx = sy = sz = 200.0

    hx, hy, hz = sx * 0.5, sy * 0.5, sz * 0.5

    # Vertex order copied to be close to 3ds Max .X output: 4 split vertices per face.
    raw_verts = [
        ( hx, hy,-hz), (-hx, hy, hz), ( hx, hy, hz), (-hx, hy,-hz),  # top
        ( hx,-hy,-hz), ( hx, hy, hz), ( hx,-hy, hz), ( hx, hy,-hz),  # right
        (-hx,-hy,-hz), ( hx,-hy, hz), (-hx,-hy, hz), ( hx,-hy,-hz),  # bottom
        (-hx, hy,-hz), (-hx,-hy, hz), (-hx, hy, hz), (-hx,-hy,-hz),  # left
        ( hx, hy, hz), (-hx,-hy, hz), ( hx,-hy, hz), (-hx, hy, hz),  # front
        ( hx,-hy,-hz), (-hx, hy,-hz), ( hx, hy,-hz), (-hx,-hy,-hz),  # back
    ]
    normals = (
        [(0,1,0)]*4 + [(1,0,0)]*4 + [(0,-1,0)]*4 +
        [(-1,0,0)]*4 + [(0,0,1)]*4 + [(0,0,-1)]*4
    )
    faces = [
        ([0,1,2],0), ([1,0,3],0),
        ([4,5,6],0), ([5,4,7],0),
        ([8,9,10],0), ([9,8,11],0),
        ([12,13,14],0), ([13,12,15],0),
        ([16,17,18],0), ([17,16,19],0),
        ([20,21,22],0), ([21,20,23],0),
    ]

    xrot, zrot, scale, rh = _effective_coord_settings(settings)
    _tiltan_axis = settings.get('tiltan_axis_zup_yfront', True)
    verts = [apply_coord_system(v, xrot, zrot, scale, rh, _tiltan_axis) for v in raw_verts]
    normals = [apply_coord_system(n, xrot, zrot, 1.0, rh, _tiltan_axis) for n in normals]

    # Use a six-face atlas instead of repeating the whole bitmap on every face.
    # This matches C4D cube/facade strip previews much better in MeshView.
    uvs = _cube_atlas_uvs_24()

    return {
        'verts': verts,
        'normals': normals,
        'uvs_list': [uvs] if settings.get('texcoords', True) else [],
        'vcols': [(1.0,1.0,1.0,1.0)] * 24,
        'faces': faces,
        'materials': _collect_materials_for_object(obj),
        'orig_indices': list(range(24)),
    }



def _tiltan_is_lod_name(name):
    try:
        s = str(name).lower()
        return s in ("lod_high", "lod_medium", "lod_low", "lod_xlow")
    except Exception:
        return False


def _tiltan_lod_suffix(name):
    s = str(name)
    low = s.lower()
    if low.endswith("_high"):
        return "High"
    if low.endswith("_medium"):
        return "Medium"
    if low.endswith("_low"):
        return "Low"
    if low.endswith("_xlow"):
        return "XLow"
    return s.split("_")[-1] if "_" in s else s


def _tiltan_dmg_suffix(name):
    s = str(name)
    low = s.lower()
    if low == "dmg0":
        return ""
    if low == "dmg1":
        return "Dmg"
    if low.startswith("dmg"):
        return "Dmg" + s[3:]
    return s


def _tiltan_part_needs_unique_leaf_names(part_name):
    """Older Tiltan Model Viewer splitter can crash when a non-body part contains
    the same leaf mesh/frame name repeated under all LOD/Dmg branches.

    Body has historically worked with Body/BodyDmg repeated, so keep body names
    unchanged.  For other PxxNyy parts, make only the geometry leaf names unique
    by LOD/Dmg while preserving the structural P/N, Lod_* and Dmg* hierarchy.
    """
    try:
        s = str(part_name)
        low = s.lower()
        if not re.match(r"^p\d+n\d+_", low):
            return False
        if low.endswith("_body"):
            return False
        return True
    except Exception:
        return False


def _tiltan_legacy_leaf_name(obj, base_name):
    """Return a splitter-safe Frame/Mesh name for geometry leaves only.

    Example:
      P00N01_Turent / Lod_High / Dmg0 / Turent    -> Turent_High
      P00N01_Turent / Lod_High / Dmg1 / TurentDmg -> TurentDmg_High_Dmg

    The hierarchy frames themselves remain unchanged:
      P00N01_Turent / Lod_High / Dmg0
    """
    try:
        dmg_obj = obj.GetUp()
        lod_obj = dmg_obj.GetUp() if dmg_obj else None
        part_obj = lod_obj.GetUp() if lod_obj else None
        if not (dmg_obj and lod_obj and part_obj):
            return base_name

        dmg_name = safe_name(dmg_obj, False)
        lod_name = safe_name(lod_obj, False)
        part_name = safe_name(part_obj, False)

        if not _tiltan_is_lod_name(lod_name):
            return base_name
        if not str(dmg_name).lower().startswith("dmg"):
            return base_name
        if not _tiltan_part_needs_unique_leaf_names(part_name):
            return base_name

        lod_suffix = _tiltan_lod_suffix(lod_name)
        dmg_suffix = _tiltan_dmg_suffix(dmg_name)
        if dmg_suffix:
            return f"{base_name}_{lod_suffix}_{dmg_suffix}"
        return f"{base_name}_{lod_suffix}"
    except Exception:
        return base_name


def write_frame_recursive(w, obj, settings, doc, ind="", stats=None):
    """Write one C4D object as one DirectX Frame and recurse children.

    v19 is a master-asset exporter: hierarchy comes first. Every C4D object can
    become a Frame, even when it has no mesh. This preserves Max Dummy/Null
    attach points such as P00N00_body, P00N01_Turent, P01N02_rocket, Smoke01 and
    light01-light04.
    """
    if obj is None:
        return

    try:
        obj_type = obj.GetType()
    except Exception:
        obj_type = -1

    preserve = settings.get('preserve_full_hierarchy', True)

    direct_mesh = False
    try:
        direct_mesh = (obj_type == c4d.Opolygon or obj_type == getattr(c4d, 'Ocube', -999999) or _has_polygon_output(obj))
    except Exception:
        direct_mesh = False

    export_mesh  = settings.get('mesh', True) and direct_mesh
    export_bone  = settings.get('bone', True) and obj_type == c4d.Ojoint
    export_dummy = settings.get('dummy', True) and obj_type == c4d.Onull
    export_other = settings.get('other_nodes', False)

    # In master asset mode, write all objects as Frames. Mesh data is optional.
    # In classic mode, keep old AXE-style filtering.
    should_export = preserve or export_mesh or export_bone or export_dummy or export_other

    if not should_export:
        child = obj.GetDown()
        while child:
            write_frame_recursive(w, child, settings, doc, ind, stats)
            child = child.GetNext()
        return

    dup = settings.get('duplicate_name', False)
    name = safe_name(obj, dup)

    # Tiltan Model Viewer splitter compatibility:
    # keep structural hierarchy names as-is, but make geometry leaf names unique
    # inside non-body PxxNyy parts, e.g. Turent_High / TurentDmg_High_Dmg.
    try:
        if settings.get('tiltan_unique_part_leaf_names', True):
            name = _tiltan_legacy_leaf_name(obj, name)
    except Exception:
        pass

    # Master assets must keep local matrices. World-space baking breaks Dummy
    # positions and turret/rocket parenting. The World Space UI option is ignored
    # when preserve_full_hierarchy is enabled.
    #
    # Exception: Export Selected Hierarchy can start from an object whose parent is
    # not being exported. In that case, the selected root must use its world matrix
    # so it keeps the same scene position. Children still use local matrices.
    selected_root_world = bool(settings.get('export_selected', False) and
                               id(obj) in settings.get('_selected_root_ids', set()))
    use_world = selected_root_world or (settings.get('world_space', False) and not preserve and not settings.get('asset_preserve_local', True))
    try:
        m = obj.GetMg() if use_world else obj.GetMl()
    except Exception:
        m = c4d.Matrix()

    # Tiltan mode: Global Scale must scale hierarchy offsets too.
    # Mesh vertices are scaled in extract_mesh(); frame translation is scaled here.
    if settings.get('preserve_full_hierarchy', False) or settings.get('asset_mode', False):
        m = _apply_global_scale_to_matrix_translation(m, settings)

    # Export coordinate target:
    # World Axis = X right, Y front, Z up.
    m = _apply_tiltan_axis_to_matrix(m, settings)

    if stats is not None:
        stats['frames'] = stats.get('frames', 0) + 1
        if obj_type == c4d.Onull:
            stats['nulls'] = stats.get('nulls', 0) + 1
        elif obj_type == c4d.Ojoint:
            stats['joints'] = stats.get('joints', 0) + 1

        # Generic asset-structure counters. These are only diagnostics; export
        # never depends on specific part names such as body/turret/rocket.
        # LOD and Dmg groups are preserved by hierarchy/name, with any number
        # of Dmg states supported (Dmg0, Dmg1, Dmg2, ...).
        try:
            lname = str(name).lower()
            if lname == 'lod_high':
                stats['lod_high'] = stats.get('lod_high', 0) + 1
            elif lname == 'lod_medium':
                stats['lod_medium'] = stats.get('lod_medium', 0) + 1
            elif lname == 'lod_low':
                stats['lod_low'] = stats.get('lod_low', 0) + 1
            if lname.startswith('dmg') and len(lname) > 3 and lname[3:].isdigit():
                stats.setdefault('damage_names', set()).add(name)
        except Exception:
            pass

    w.w(f"{ind}Frame {name} {{")
    w.w()
    w.w(f"{ind} FrameTransformMatrix {{")
    w.w(f"{ind}  {mat4x4_str(m)};;")
    w.w(f"{ind} }}")
    w.w()

    mesh_written = False
    if export_mesh:
        mesh_data = None
        if obj_type == getattr(c4d, 'Ocube', -1):
            mesh_data = extract_cube_primitive_mesh(obj, settings)
        if mesh_data is None:
            mesh_obj = obj if obj_type == c4d.Opolygon else _find_polygon_cache(obj)
            mesh_data = extract_mesh(mesh_obj, settings, tag_source=obj) if mesh_obj else None
        if mesh_data:
            inline = settings.get('inline', False)
            # For Max/splitter compatibility, Mesh name equals Frame name.
            write_mesh_block(w, name, mesh_data, settings, inline=inline, ind=ind+" ")
            mesh_written = True
            if stats is not None:
                stats['meshes'] = stats.get('meshes', 0) + 1
        else:
            if stats is not None:
                stats['mesh_failures'] = stats.get('mesh_failures', 0) + 1

    if stats is not None and not mesh_written:
        stats['empty_frames'] = stats.get('empty_frames', 0) + 1

    if settings.get('hierarchy', True):
        child = obj.GetDown()
        while child:
            write_frame_recursive(w, child, settings, doc, ind+" ", stats)
            child = child.GetNext()

    w.w(f"{ind}}}")
    w.w()


def _find_weight_tag(obj):
    """Return a Cinema 4D Weight/CAWeightTag without relying on removed c4d.Tskin."""
    if obj is None:
        return None

    # Newer Cinema 4D versions expose Weight tags as CAWeightTag/tcaweight,
    # while older exporter code often used c4d.Tskin.  C4D 2026 does not
    # define c4d.Tskin, so never access it directly.
    candidate_type_names = (
        'Tskin',
        'Tcaweight',
        'Tweights',
        'Tweight',
        'ID_CA_WEIGHT_TAG',
    )
    for name in candidate_type_names:
        tag_type = getattr(c4d, name, None)
        if tag_type is None:
            continue
        try:
            tag = obj.GetTag(tag_type)
            if tag:
                return tag
        except Exception:
            pass

    # Fallback: detect the tag by the methods the CAWeightTag exposes.
    try:
        for tag in obj.GetTags():
            if (hasattr(tag, 'GetJointCount') and
                hasattr(tag, 'GetJoint') and
                hasattr(tag, 'GetWeight')):
                return tag
    except Exception:
        pass
    return None


def write_skin_weights(w, obj, doc, settings):
    """Write SkinWeights blocks for skinned meshes. Safe on C4D 2026."""
    if obj.GetType() != c4d.Opolygon:
        return
    skin_tag = _find_weight_tag(obj)
    if not skin_tag:
        return

    try:
        casc = skin_tag.GetJointCount()
    except Exception:
        return

    weight_fix = settings.get('weight_fix_val', 1.0)

    for ji in range(casc):
        try:
            joint = skin_tag.GetJoint(ji, doc)
            if not joint:
                continue
            jname = safe_name(joint)
            n_pts = obj.GetPointCount()
            indices = []
            weights = []
            for vi in range(n_pts):
                try:
                    wt = skin_tag.GetWeight(ji, vi)
                except Exception:
                    wt = 0.0
                if wt > 0.0001:
                    indices.append(vi)
                    weights.append(min(wt, weight_fix))

            if not indices:
                continue

            # Offset matrix (inverse bind pose)
            try:
                bind_m = skin_tag.GetBindMatrix(ji)
                offset = ~bind_m
            except Exception:
                offset = c4d.Matrix()

            w.w(f" SkinWeights {{")
            w.w(f"  \"{jname}\";")
            w.w(f"  {len(indices)};")
            for i, vi in enumerate(indices):
                sep = "," if i < len(indices)-1 else ";"
                w.w(f"  {vi}{sep}")
            for i, wt in enumerate(weights):
                sep = "," if i < len(weights)-1 else ";"
                w.w(f"  {wt:.6f}{sep}")
            w.w(f"  {mat4x4_str(offset)};;")
            w.w(f" }}")
            w.w()
        except Exception:
            continue


def write_animation_clips(w, obj, clips, settings, doc):
    fps         = doc.GetFps()
    sample_rate = settings.get('sample_rate', 1)
    use_srt     = settings.get('use_srt', False)
    only_keys   = settings.get('keyframes_only', False)
    optimize    = max(1, settings.get('optimize', 3))

    # Collect animated objects
    animated = []
    def collect(o):
        if o.GetType() in (c4d.Opolygon, c4d.Ojoint, c4d.Onull):
            animated.append(o)
        c = o.GetDown()
        while c:
            collect(c)
            c = c.GetNext()
    collect(obj)

    orig_time = doc.GetTime()

    for clip in clips:
        cname  = safe_name(clip['name'])
        cf_s   = clip['start']
        cf_e   = clip['end']

        w.w(f"AnimationSet {cname} {{")
        w.w()

        for aobj in animated:
            oname = safe_name(aobj)
            w.w(f" Animation anim_{oname} {{")
            w.w(f"  {{ {oname} }}")
            w.w()

            frames = list(range(cf_s, cf_e + 1, max(1, sample_rate)))
            if cf_e not in frames:
                frames.append(cf_e)

            if use_srt:
                s_keys, r_keys, t_keys = [], [], []
                for frame in frames:
                    doc.SetTime(c4d.BaseTime(frame, fps))
                    c4d.EventAdd()
                    m  = aobj.GetMl()
                    sx = m.v1.GetLength()
                    sy = m.v2.GetLength()
                    sz = m.v3.GetLength()
                    hp = aobj.GetRelRot()
                    qw, qx, qy, qz = hpb_to_quat(hp.x, hp.y, hp.z)
                    px, py, pz = m.off.x, m.off.y, m.off.z
                    t = int(frame * (4800.0 / fps))
                    s_keys.append((t, sx, sy, sz))
                    r_keys.append((t, qw, qx, qy, qz))
                    t_keys.append((t, px, py, pz))

                # Scale (type 1)
                w.w(f"  AnimationKey {{  // Scale")
                w.w(f"   1; {len(s_keys)};")
                for i, (t, sx, sy, sz) in enumerate(s_keys):
                    sep = "," if i < len(s_keys)-1 else ";"
                    w.w(f"   {t}; 3; {sx:.6f},{sy:.6f},{sz:.6f};;{sep}")
                w.w(f"  }}")
                w.w()

                # Rotation (type 0 quaternion)
                w.w(f"  AnimationKey {{  // Rotation")
                w.w(f"   0; {len(r_keys)};")
                for i, (t, qw, qx, qy, qz) in enumerate(r_keys):
                    sep = "," if i < len(r_keys)-1 else ";"
                    w.w(f"   {t}; 4; {qw:.6f},{qx:.6f},{qy:.6f},{qz:.6f};;{sep}")
                w.w(f"  }}")
                w.w()

                # Position (type 2)
                w.w(f"  AnimationKey {{  // Position")
                w.w(f"   2; {len(t_keys)};")
                for i, (t, px, py, pz) in enumerate(t_keys):
                    sep = "," if i < len(t_keys)-1 else ";"
                    w.w(f"   {t}; 3; {px:.6f},{py:.6f},{pz:.6f};;{sep}")
                w.w(f"  }}")
                w.w()

            else:
                # Matrix keys (type 4)
                mat_keys = []
                for frame in frames:
                    doc.SetTime(c4d.BaseTime(frame, fps))
                    c4d.EventAdd()
                    m = aobj.GetMl()
                    t = int(frame * (4800.0 / fps))
                    mat_keys.append((t, mat4x4_str(m)))

                w.w(f"  AnimationKey {{  // Matrix")
                w.w(f"   4; {len(mat_keys)};")
                for i, (t, ms) in enumerate(mat_keys):
                    sep = "," if i < len(mat_keys)-1 else ";"
                    w.w(f"   {t}; 16; {ms};;{sep}")
                w.w(f"  }}")
                w.w()

            w.w(f" }}")
            w.w()

        w.w(f"}}")
        w.w()

    doc.SetTime(orig_time)
    c4d.EventAdd()


# ─────────────────────────────────────────────────────────────────────────────
# Main Export
# ─────────────────────────────────────────────────────────────────────────────

def _iter_objects(root):
    """Depth-first object iterator."""
    obj = root
    while obj:
        yield obj
        child = obj.GetDown()
        for c in _iter_objects(child):
            yield c
        obj = obj.GetNext()


def _iter_subtree(root):
    """Depth-first iterator for one selected object and its children only."""
    if root is None:
        return
    yield root
    child = root.GetDown()
    while child:
        for c in _iter_subtree(child):
            yield c
        child = child.GetNext()


def _is_descendant_of(obj, possible_parent):
    p = obj.GetUp() if obj else None
    while p:
        if p == possible_parent:
            return True
        p = p.GetUp()
    return False


def _all_scene_objects(doc):
    """Yield every object in the document hierarchy."""
    if doc is None:
        return
    obj = doc.GetFirstObject()
    while obj:
        for x in _iter_subtree(obj):
            yield x
        obj = obj.GetNext()


def _get_active_objects_robust(doc):
    """Return selected objects robustly across Cinema 4D versions/layout states."""
    selected = []
    if doc is None:
        return selected

    # Standard API path.
    for flag_name in ('GETACTIVEOBJECTFLAGS_SELECTIONORDER', 'GETACTIVEOBJECTFLAGS_NONE', 'GETACTIVEOBJECTFLAGS_CHILDREN'):
        try:
            flag = getattr(c4d, flag_name, 0)
            arr = doc.GetActiveObjects(flag)
            if arr:
                for obj in arr:
                    if obj and obj not in selected:
                        selected.append(obj)
                if selected:
                    return selected
        except Exception:
            pass

    # Single-active-object fallback.
    try:
        obj = doc.GetActiveObject()
        if obj:
            selected.append(obj)
            return selected
    except Exception:
        pass

    # Last resort: walk scene and check the active bit.  This covers cases where
    # Object Manager selection exists but GetActiveObjects returned an empty list.
    try:
        active_bit = getattr(c4d, 'BIT_ACTIVE', None)
        if active_bit is not None:
            for obj in _all_scene_objects(doc):
                try:
                    if obj.GetBit(active_bit):
                        selected.append(obj)
                except Exception:
                    pass
    except Exception:
        pass
    return selected


def _get_export_roots(doc, settings):
    """Return scene roots or selected top-level roots for selected hierarchy export.

    Export Selected means: selected object(s) plus all children.  If both a parent
    and its child are selected, only the parent is exported to avoid duplicates.
    """
    if not settings.get('export_selected', False):
        roots = []
        obj = doc.GetFirstObject()
        while obj:
            roots.append(obj)
            obj = obj.GetNext()
        return roots

    selected = _get_active_objects_robust(doc)
    if not selected:
        return []

    roots = []
    for obj in selected:
        if not any(_is_descendant_of(obj, other) for other in selected if other != obj):
            if obj not in roots:
                roots.append(obj)
    return roots


def _log_export(message):
    try:
        path = os.path.join(tempfile.gettempdir(), "c4d_dx_exporter_debug.log")
        with open(path, "a", encoding="utf-8") as f:
            f.write(str(message) + "\n")
    except Exception:
        pass


def do_export(filepath, settings, clips, doc):
    _log_export("--- export start v21 Tiltan legacy X compatibility ---")
    _log_export("filepath: " + str(filepath))
    try:
        settings['output_dir'] = os.path.dirname(os.path.abspath(_filepath_to_str(filepath)))
    except Exception:
        settings['output_dir'] = ''
    try:
        settings['doc_dir'] = doc.GetDocumentPath() or ''
    except Exception:
        settings['doc_dir'] = ''
    try:
        # Force Cinema 4D to evaluate generators/deformers so Cube/Sweep/Cloner caches exist.
        doc.ExecutePasses(None, True, True, True, c4d.BUILDFLAGS_NONE)
    except Exception as e:
        _log_export("ExecutePasses failed: " + repr(e))

    w = DXWriter()
    w.write_header(_select_geom_format(settings))

    inline   = settings.get('inline', False)
    mat_done = set()
    stats = {'frames': 0, 'meshes': 0, 'empty_frames': 0, 'nulls': 0, 'joints': 0, 'mesh_failures': 0, 'lod_high': 0, 'lod_medium': 0, 'lod_low': 0, 'damage_names': set()}
    roots = _get_export_roots(doc, settings)
    try:
        settings['_selected_root_ids'] = set(id(r) for r in roots) if settings.get('export_selected', False) else set()
    except Exception:
        settings['_selected_root_ids'] = set()
    if not roots:
        if settings.get('export_selected', False):
            return False, 'Export Selected Hierarchy is enabled, but no object is selected.'
        return False, 'No objects in the scene.'

    # Write global materials (unless inline).  DefaultMaterial is written once
    # so meshes without a material still contain a valid MeshMaterialList.
    if settings.get('material', True) and not inline:
        write_material_block(w, None, settings, settings.get('tex_format', 'tga'))
        for root in roots:
            for obj in _iter_subtree(root):
                for tag in obj.GetTags():
                    if tag.GetType() == c4d.Ttexture:
                        mat = tag.GetMaterial()
                        if mat and id(mat) not in mat_done:
                            mat_done.add(id(mat))
                            write_material_block(w, mat, settings,
                                                 settings.get('tex_format', 'tga'))

    # Scene root frame
    if settings.get('scene_root', True):
        w.w("Frame SceneRoot {")
        w.w()
        w.w(" FrameTransformMatrix {")
        w.w(f"  {identity_mat_str()};;")
        w.w(" }")
        w.w()
        ind = " "
    else:
        ind = ""

    # Export objects.  In selected mode, export each selected top-level object
    # with all of its children, preserving local matrices and duplicate group
    # names such as LOD_High/Dmg0 under different parents.
    for obj in roots:
        write_frame_recursive(w, obj, settings, doc, ind, stats)

    # Skin weights
    if settings.get('skin', False):
        for root in roots:
            for obj in _iter_subtree(root):
                if obj.GetType() == c4d.Opolygon:
                    write_skin_weights(w, obj, doc, settings)

    if settings.get('scene_root', True):
        w.w("}")
        w.w()

    # Animation
    if settings.get('anim_clips', True) and clips:
        root = roots[0] if roots else None
        if root:
            write_animation_clips(w, root, clips, settings, doc)

    if stats.get('frames', 0) == 0:
        _log_export("No frames exported.")
        return False, "No frames were exported. Make sure the document contains objects."
    if stats.get('meshes', 0) == 0:
        _log_export("Warning: no mesh exported, writing hierarchy-only .x. frames=%s" % stats.get('frames', 0))

    try:
        # Keep LF line endings and legacy whitespace. The Tiltan Model Viewer
        # splitter is old and its hierarchy/polygon scanner matches the working
        # reference .x layout: values are separated as "x; y; z;" and lines end
        # with LF, not forced CRLF.
        text = w.get_text().replace("\r\n", "\n").replace("\r", "\n") + "\n"
        with open(filepath, 'w', encoding='utf-8', newline='\n') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if not os.path.isfile(filepath) or os.path.getsize(filepath) <= 0:
            return False, "The .x file was not created or is empty."
        damage_names = sorted(list(stats.get('damage_names', set())))
        _log_export("Export OK: selected_mode=%s, %d bytes, frames=%d, meshes=%d, empty_frames=%d, nulls=%d, joints=%d, mesh_failures=%d, LOD_High=%d, LOD_Medium=%d, LOD_Low=%d, DamageGroups=%s" % (str(settings.get('export_selected', False)), os.path.getsize(filepath), stats.get('frames', 0), stats.get('meshes', 0), stats.get('empty_frames', 0), stats.get('nulls', 0), stats.get('joints', 0), stats.get('mesh_failures', 0), stats.get('lod_high', 0), stats.get('lod_medium', 0), stats.get('lod_low', 0), ','.join(damage_names)))
        mode = "Selected hierarchy" if settings.get('export_selected', False) else "Whole scene"
        return True, "Mode: %s\nExported %d mesh(es), %d frame(s), %d empty/null frame(s).\nLOD: High=%d, Medium=%d, Low=%d\nDamage groups: %s" % (mode, stats.get('meshes', 0), stats.get('frames', 0), stats.get('empty_frames', 0), stats.get('lod_high', 0), stats.get('lod_medium', 0), stats.get('lod_low', 0), (', '.join(damage_names) if damage_names else 'none'))
    except Exception as e:
        _log_export(traceback.format_exc())
        return False, str(e)


# ─────────────────────────────────────────────────────────────────────────────
# Dialog
# ─────────────────────────────────────────────────────────────────────────────

class _LogoArea(c4d.gui.GeUserArea):
    """Draws the embedded Tiltan logo bitmap. Falls back to text if the
    bitmap cannot be loaded, so the banner is never blank."""
    def __init__(self):
        super().__init__()
        self.bmp = None
        self._load()

    def _load(self):
        self.bmp = None
        # 1) Prefer a tiltan_logo.png sitting next to the plugin (easy to update).
        try:
            here = os.path.dirname(os.path.abspath(__file__))
            for name in ("tiltan_logo.png", "logo.png"):
                cand = os.path.join(here, name)
                if os.path.isfile(cand):
                    bmp = c4d.bitmaps.BaseBitmap()
                    res = bmp.InitWith(cand)
                    ok = (res[0] == c4d.IMAGERESULT_OK) if isinstance(res, (tuple, list)) else (res == c4d.IMAGERESULT_OK)
                    if ok and bmp.GetBw() > 0 and bmp.GetBh() > 0:
                        self.bmp = bmp
                        return
        except Exception:
            pass
        # 2) Fall back to the embedded base64 copy (works even if the PNG is missing).
        try:
            import base64
            raw = base64.b64decode(_TILTAN_LOGO_B64)
            tmp = os.path.join(tempfile.gettempdir(), "tiltan_logo_eae.png")
            with open(tmp, "wb") as f:
                f.write(raw)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except Exception:
                    pass
            bmp = c4d.bitmaps.BaseBitmap()
            res = bmp.InitWith(tmp)
            ok = (res[0] == c4d.IMAGERESULT_OK) if isinstance(res, (tuple, list)) else (res == c4d.IMAGERESULT_OK)
            if ok and bmp.GetBw() > 0 and bmp.GetBh() > 0:
                self.bmp = bmp
        except Exception:
            self.bmp = None

    def GetMinSize(self):
        return (150, 38)

    def DrawMsg(self, x1, y1, x2, y2, msg):
        self.OffScreenOn()
        w = self.GetWidth()
        h = self.GetHeight()

        # Always paint a white plate across the whole area first (so there is
        # never a black gap, and the logo art sits on its intended white bg).
        self.DrawSetPen(c4d.Vector(1.0, 1.0, 1.0))
        self.DrawRectangle(0, 0, w, h)

        if self.bmp:
            bw, bh = self.bmp.GetBw(), self.bmp.GetBh()
            scale = float(h) / float(bh) if bh else 1.0
            dw = max(1, int(bw * scale))
            dh = max(1, int(bh * scale))
            if dw > w:  # never overflow the gadget width
                dw = w
                scale = float(w) / float(bw)
                dh = max(1, int(bh * scale))
            dy = (h - dh) // 2
            self.DrawBitmap(self.bmp, 0, dy, dw, dh, 0, 0, bw, bh,
                            c4d.BMP_NORMAL)
        else:
            try:
                self.DrawSetTextCol(c4d.Vector(0.16, 0.27, 0.42), c4d.COLOR_TRANS)
                self.DrawSetFont(c4d.FONT_BOLD)
                txt = "TILTAN"
                th = self.DrawGetFontHeight()
                self.DrawText(txt, 6, max(0, (h - th) // 2))
            except Exception:
                pass


class _TiltanButton(c4d.gui.GeUserArea):
    """A custom-drawn button in Tiltan colors with hover/pressed states.

    on_click is a no-argument callback invoked when the button is released
    inside its own area.
    """
    # Tiltan brand palette
    BLUE      = c4d.Vector(0.161, 0.275, 0.420)   # #29466B
    BLUE_HOV  = c4d.Vector(0.220, 0.360, 0.540)
    BLUE_DN   = c4d.Vector(0.120, 0.210, 0.330)
    GREEN     = c4d.Vector(0.725, 0.835, 0.463)   # #B9D576
    TEXT      = c4d.Vector(1.0, 1.0, 1.0)

    def __init__(self, label, on_click, primary=True):
        super().__init__()
        self.label = label
        self.on_click = on_click
        self.primary = primary       # primary = solid blue; secondary = outline
        self.hover = False
        self.pressed = False

    def GetMinSize(self):
        return (96, 30)

    def DrawMsg(self, x1, y1, x2, y2, msg):
        self.OffScreenOn()
        w = self.GetWidth()
        h = self.GetHeight()

        # Dialog background behind the button so corners look clean.
        try:
            self.DrawSetPen(c4d.COLOR_BG)
        except Exception:
            self.DrawSetPen(c4d.Vector(0.18, 0.18, 0.18))
        self.DrawRectangle(0, 0, w, h)

        if self.primary:
            fill = self.BLUE_DN if self.pressed else (self.BLUE_HOV if self.hover else self.BLUE)
            self.DrawSetPen(fill)
            self.DrawRectangle(0, 0, w - 1, h - 1)
            # Tiltan green accent strip on the left edge.
            self.DrawSetPen(self.GREEN)
            self.DrawRectangle(0, 0, 3, h - 1)
            txt_col = self.TEXT
        else:
            # Secondary: subtle fill + blue border.
            base = c4d.Vector(0.26, 0.26, 0.26) if not self.hover else c4d.Vector(0.32, 0.32, 0.32)
            if self.pressed:
                base = c4d.Vector(0.22, 0.22, 0.22)
            self.DrawSetPen(base)
            self.DrawRectangle(0, 0, w - 1, h - 1)
            self.DrawSetPen(self.BLUE_HOV)
            self.DrawLine(0, 0, w - 1, 0)
            self.DrawLine(0, h - 1, w - 1, h - 1)
            self.DrawLine(0, 0, 0, h - 1)
            self.DrawLine(w - 1, 0, w - 1, h - 1)
            txt_col = c4d.Vector(0.90, 0.90, 0.90)

        try:
            self.DrawSetTextCol(txt_col, c4d.COLOR_TRANS)
            self.DrawSetFont(c4d.FONT_BOLD)
            tw = self.DrawGetTextWidth(self.label)
            th = self.DrawGetFontHeight()
            self.DrawText(self.label, max(6, (w - tw) // 2),
                          max(0, (h - th) // 2))
        except Exception:
            pass

    def InputEvent(self, msg):
        try:
            device = msg.GetInt32(c4d.BFM_INPUT_DEVICE)
            channel = msg.GetInt32(c4d.BFM_INPUT_CHANNEL)
        except Exception:
            return True
        if device == c4d.BFM_INPUT_MOUSE and channel == c4d.BFM_INPUT_MOUSELEFT:
            self.pressed = True
            self.Redraw()
            try:
                self.MouseDragStart(c4d.BFM_INPUT_MOUSELEFT, 0, 0,
                                    c4d.MOUSEDRAGFLAGS_DONTHIDEMOUSE)
                while True:
                    result = self.MouseDrag()
                    rc = result[0] if isinstance(result, (tuple, list)) else result
                    if rc != c4d.MOUSEDRAGRESULT_CONTINUE:
                        break
                self.MouseDragEnd()
            except Exception:
                pass
            self.pressed = False
            self.Redraw()
            if self.on_click:
                try:
                    self.on_click()
                except Exception:
                    pass
        return True


class DXExportDialog(gui.GeDialog):

    def __init__(self):
        self.clips = []
        self.sel_clip = -1
        self._logo = _LogoArea()
        self._btn_export = _TiltanButton("Export .X", self._on_export_click, primary=True)
        self._btn_cancel = _TiltanButton("Cancel", self._on_cancel_click, primary=False)
        self._btn_validate = _TiltanButton("Validate", self._on_validate_click, primary=False)

    def _on_export_click(self):
        self._run_export()

    def _on_cancel_click(self):
        self.Close()

    def _on_validate_click(self):
        self._validate_hierarchy()

    # ── Layout ────────────────────────────────────────────────────────────────
    def CreateLayout(self):
        self.SetTitle("DX Exporter by EAE v0020")

        # ── Header ───────────────────────────────────────────────────────────
        self.GroupBegin(0, c4d.BFH_SCALEFIT, 2, 0, "", 0)
        self.GroupBorderSpace(16, 10, 16, 8)

        self.GroupBegin(0, c4d.BFH_SCALEFIT, 2, 0, "", 0)
        self.AddUserArea(GADGET_LOGO, c4d.BFH_LEFT | c4d.BFV_TOP, 96, 38)
        self.AttachUserArea(self._logo, GADGET_LOGO)
        self.GroupBegin(0, c4d.BFH_SCALEFIT | c4d.BFV_CENTER, 1, 0, "", 0)
        self.AddStaticText(0, c4d.BFH_SCALEFIT, 0, 0, "DX Exporter by EAE v0020")
        self.AddStaticText(0, c4d.BFH_SCALEFIT, 0, 0, "Tiltan Viewer · P/N hierarchy · MeshNormals")
        self.GroupEnd()
        self.GroupEnd()

        self.GroupBegin(0, c4d.BFH_RIGHT, 3, 0, "", 0)
        self.AddUserArea(BTN_VALIDATE, c4d.BFH_RIGHT | c4d.BFV_TOP, 122, 28)
        self.AttachUserArea(self._btn_validate, BTN_VALIDATE)
        self.AddUserArea(BTN_OK,       c4d.BFH_RIGHT | c4d.BFV_TOP, 112, 28)
        self.AttachUserArea(self._btn_export, BTN_OK)
        self.AddUserArea(BTN_CANCEL,   c4d.BFH_RIGHT | c4d.BFV_TOP, 82, 28)
        self.AttachUserArea(self._btn_cancel, BTN_CANCEL)
        self.GroupEnd()

        self.GroupEnd()
        self.AddSeparatorH(0, c4d.BFH_SCALEFIT)

        # ── Compact status strip ─────────────────────────────────────────────
        self.GroupBegin(0, c4d.BFH_SCALEFIT, 1, 0, "", 0)
        self.GroupBorderSpace(16, 6, 16, 6)
        self.GroupBegin(0, c4d.BFH_SCALEFIT, 4, 0, "", 0)
        self.AddCheckbox(CHK_TILTAN_COMPAT, c4d.BFH_LEFT, 0, 0, "Tiltan Compatibility")
        self.AddStaticText(0, c4d.BFH_LEFT, 0, 0, "Axis: X Right / Y Front / Z Up")
        self.AddStaticText(0, c4d.BFH_LEFT, 0, 0, "Units: meters")
        self.AddStaticText(0, c4d.BFH_LEFT, 0, 0, "Format: MeshNormals")
        self.GroupEnd()
        self.GroupEnd()

        self.AddSeparatorH(0, c4d.BFH_SCALEFIT)

        # ── Tabs ─────────────────────────────────────────────────────────────
        self.TabGroupBegin(GRP_TABS, c4d.BFH_SCALEFIT | c4d.BFV_SCALEFIT, c4d.TAB_TABS)

        # =====================================================================
        # EXPORT
        # =====================================================================
        self.GroupBegin(TAB_GEOMETRY, c4d.BFH_SCALEFIT | c4d.BFV_TOP, 1, 0, "Export", 0)
        self.GroupBorderSpace(14, 10, 14, 10)

        self.GroupBegin(0, c4d.BFH_SCALEFIT, 2, 0, "", 0)

        self.GroupBegin(GRP_GENERAL, c4d.BFH_SCALEFIT, 1, 0, "Source", c4d.BFV_TOP)
        self.GroupBorder(c4d.BORDER_GROUP_IN)
        self.GroupBorderSpace(12, 8, 12, 9)
        self.GroupBegin(0, c4d.BFH_SCALEFIT, 2, 0, "", 0)
        self.AddCheckbox(CHK_SCENE_ROOT, c4d.BFH_LEFT, 0, 0, "Scene Root")
        self.AddEditText(0, c4d.BFH_SCALEFIT, 180, 0)
        self.GroupEnd()
        self.AddCheckbox(CHK_EXPORT_SELECTED, c4d.BFH_LEFT, 0, 0, "Export selected hierarchy only")
        self.AddStaticText(0, c4d.BFH_SCALEFIT, 0, 0, "Recommended: full root object.")
        self.GroupEnd()

        self.GroupBegin(0, c4d.BFH_SCALEFIT, 1, 0, "Structure", c4d.BFV_TOP)
        self.GroupBorder(c4d.BORDER_GROUP_IN)
        self.GroupBorderSpace(12, 8, 12, 9)
        self.AddCheckbox(CHK_MESH,        c4d.BFH_LEFT, 0, 0, "Meshes")
        self.AddCheckbox(CHK_DUMMY,       c4d.BFH_LEFT, 0, 0, "Dummies / Nulls")
        self.AddCheckbox(CHK_OTHER_NODES, c4d.BFH_LEFT, 0, 0, "Other node groups")
        self.AddCheckbox(CHK_BONE,        c4d.BFH_LEFT, 0, 0, "Bones")
        self.GroupEnd()

        self.GroupEnd()

        self.GroupBegin(GRP_VERTEX, c4d.BFH_SCALEFIT, 1, 0, "Vertex Data", c4d.BFV_TOP)
        self.GroupBorder(c4d.BORDER_GROUP_IN)
        self.GroupBorderSpace(12, 8, 12, 9)

        self.GroupBegin(0, c4d.BFH_SCALEFIT, 3, 0, "", 0)

        self.GroupBegin(0, c4d.BFH_SCALEFIT, 1, 0, "Required", 0)
        self.GroupBorder(c4d.BORDER_THIN_IN)
        self.GroupBorderSpace(10, 7, 10, 8)
        self.AddCheckbox(CHK_NORMAL,    c4d.BFH_LEFT, 0, 0, "Normals")
        self.AddCheckbox(CHK_TEXCOORDS, c4d.BFH_LEFT, 0, 0, "Texture coordinates")
        self.GroupEnd()

        self.GroupBegin(0, c4d.BFH_SCALEFIT, 1, 0, "Optional", 0)
        self.GroupBorder(c4d.BORDER_THIN_IN)
        self.GroupBorderSpace(10, 7, 10, 8)
        self.AddCheckbox(CHK_VERTEXCOLOR, c4d.BFH_LEFT, 0, 0, "Vertex color")
        self.AddCheckbox(CHK_TANGENT,     c4d.BFH_LEFT, 0, 0, "Tangent")
        self.AddCheckbox(CHK_BITANGENT,   c4d.BFH_LEFT, 0, 0, "Bitangent")
        self.GroupEnd()

        self.GroupBegin(0, c4d.BFH_SCALEFIT, 1, 0, "Format", 0)
        self.GroupBorder(c4d.BORDER_THIN_IN)
        self.GroupBorderSpace(10, 7, 10, 8)
        self.AddStaticText(0, c4d.BFH_SCALEFIT, 0, 0, "Geometry Format")
        self.AddStaticText(0, c4d.BFH_SCALEFIT, 0, 0, "MeshNormals")
        self.AddStaticText(0, c4d.BFH_SCALEFIT, 0, 0, "Locked by Tiltan profile")
        self.GroupEnd()

        self.GroupEnd()
        self.GroupEnd()

        self.GroupBegin(0, c4d.BFH_SCALEFIT, 1, 0, "Export Summary", c4d.BFV_TOP)
        self.GroupBorder(c4d.BORDER_GROUP_IN)
        self.GroupBorderSpace(12, 8, 12, 9)
        self.GroupBegin(0, c4d.BFH_SCALEFIT, 3, 0, "", 0)
        self.AddStaticText(0, c4d.BFH_SCALEFIT, 0, 0, "Profile: Tiltan Viewer")
        self.AddStaticText(0, c4d.BFH_SCALEFIT, 0, 0, "File: DirectX .X Text")
        self.AddStaticText(0, c4d.BFH_SCALEFIT, 0, 0, "Geometry: MeshNormals")
        self.AddStaticText(0, c4d.BFH_SCALEFIT, 0, 0, "Splitter: P/N safe")
        self.AddStaticText(0, c4d.BFH_SCALEFIT, 0, 0, "Axis: X-right / Y-front / Z-up")
        self.AddStaticText(0, c4d.BFH_SCALEFIT, 0, 0, "Animation: optional")
        self.GroupEnd()
        self.GroupEnd()

        self.GroupEnd()  # Export tab

        # =====================================================================
        # MATERIALS
        # =====================================================================
        self.GroupBegin(TAB_MATERIALS, c4d.BFH_SCALEFIT | c4d.BFV_TOP, 1, 0, "Materials", 0)
        self.GroupBorderSpace(14, 10, 14, 10)

        self.GroupBegin(0, c4d.BFH_SCALEFIT, 2, 0, "", 0)

        self.GroupBegin(0, c4d.BFH_SCALEFIT, 1, 0, "Material Export", c4d.BFV_TOP)
        self.GroupBorder(c4d.BORDER_GROUP_IN)
        self.GroupBorderSpace(12, 8, 12, 9)
        self.AddCheckbox(CHK_MATERIAL, c4d.BFH_LEFT, 0, 0, "Export materials")
        self.AddCheckbox(CHK_INLINE,   c4d.BFH_LEFT, 0, 0, "Inline materials")
        self.AddCheckbox(CHK_DIFFUSE_MAP_CLR, c4d.BFH_LEFT, 0, 0, "Use diffuse map color")
        self.GroupEnd()

        self.GroupBegin(GRP_TEXTURES, c4d.BFH_SCALEFIT, 1, 0, "Textures", c4d.BFV_TOP)
        self.GroupBorder(c4d.BORDER_GROUP_IN)
        self.GroupBorderSpace(12, 8, 12, 9)
        self.AddCheckbox(CHK_TEXTURES,  c4d.BFH_LEFT, 0, 0, "Copy color/diffuse textures")
        self.AddCheckbox(CHK_OVERWRITE, c4d.BFH_LEFT, 0, 0, "Overwrite existing textures")
        self.AddStaticText(0, c4d.BFH_SCALEFIT, 0, 0, "Texture conversion is hidden until fully verified.")
        self.GroupEnd()

        self.GroupEnd()
        self.GroupEnd()  # Materials tab

        # =====================================================================
        # ANIMATION
        # =====================================================================
        self.GroupBegin(TAB_ANIMATION, c4d.BFH_SCALEFIT | c4d.BFV_TOP, 1, 0, "Animation", 0)
        self.GroupBorderSpace(14, 10, 14, 10)

        self.GroupBegin(GRP_ANIM_MAIN, c4d.BFH_SCALEFIT, 1, 0, "Animation Export", c4d.BFV_TOP)
        self.GroupBorder(c4d.BORDER_GROUP_IN)
        self.GroupBorderSpace(12, 8, 12, 9)
        self.AddCheckbox(CHK_ANIM_CLIPS, c4d.BFH_LEFT, 0, 0, "Export animation")
        self.AddStaticText(0, c4d.BFH_SCALEFIT, 0, 0, "Off by default for static Tiltan Viewer assets.")
        self.GroupEnd()

        self.GroupBegin(0, c4d.BFH_SCALEFIT, 2, 0, "", 0)

        self.GroupBegin(0, c4d.BFH_SCALEFIT, 1, 0, "Clip Editor", c4d.BFV_TOP)
        self.GroupBorder(c4d.BORDER_GROUP_IN)
        self.GroupBorderSpace(12, 8, 12, 9)

        self.GroupBegin(GRP_CLIP_EDIT, c4d.BFH_SCALEFIT, 6, 0, "", 0)
        self.AddStaticText(0, c4d.BFH_LEFT, 48, 0, "Name")
        self.AddEditText(EDT_CLIP_NAME, c4d.BFH_SCALEFIT, 120, 0)
        self.AddStaticText(0, c4d.BFH_LEFT, 38, 0, "Start")
        self.AddEditNumberArrows(EDT_CLIP_START, c4d.BFH_LEFT, 60, 0)
        self.AddStaticText(0, c4d.BFH_LEFT, 30, 0, "End")
        self.AddEditNumberArrows(EDT_CLIP_END, c4d.BFH_LEFT, 60, 0)
        self.GroupEnd()

        self.GroupBegin(GRP_CLIPS_BTNS, c4d.BFH_LEFT, 3, 0, "", 0)
        self.AddButton(BTN_ADD_CLIP,    c4d.BFH_LEFT, 82, 24, "Add Clip")
        self.AddButton(BTN_INSERT_CLIP, c4d.BFH_LEFT, 72, 24, "Insert")
        self.AddButton(BTN_DEL_CLIP,    c4d.BFH_LEFT, 72, 24, "Delete")
        self.GroupEnd()

        self.AddMultiLineEditText(LST_CLIPS, c4d.BFH_SCALEFIT | c4d.BFV_SCALEFIT, 360, 88, 0)
        self.GroupEnd()

        self.GroupBegin(0, c4d.BFH_SCALEFIT, 1, 0, "Sampling / Skin", c4d.BFV_TOP)
        self.GroupBorder(c4d.BORDER_GROUP_IN)
        self.GroupBorderSpace(12, 8, 12, 9)

        self.GroupBegin(GRP_SAMPLE, c4d.BFH_LEFT, 2, 0, "", 0)
        self.AddStaticText(0, c4d.BFH_LEFT, 110, 0, "Sample rate")
        self.AddEditNumberArrows(EDT_SAMPLE_RATE, c4d.BFH_LEFT, 62, 0)
        self.AddStaticText(0, c4d.BFH_LEFT, 110, 0, "Optimize")
        self.AddEditNumberArrows(EDT_OPTIMIZE, c4d.BFH_LEFT, 62, 0)
        self.GroupEnd()

        self.AddCheckbox(CHK_KEYFRAMES_ONLY, c4d.BFH_LEFT, 0, 0, "Keyframes only")
        self.AddCheckbox(CHK_SKIN, c4d.BFH_LEFT, 0, 0, "Export skin / bones animation")

        self.GroupBegin(GRP_WEIGHT, c4d.BFH_LEFT, 3, 0, "", 0)
        self.AddCheckbox(CHK_WEIGHT_FIX, c4d.BFH_LEFT, 0, 0, "Weight fix")
        self.AddStaticText(0, c4d.BFH_LEFT, 40, 0, "Value")
        self.AddEditNumber(EDT_WEIGHT_FIX_VAL, c4d.BFH_LEFT, 70, 0)
        self.GroupEnd()

        self.GroupBegin(GRP_KEYS, c4d.BFH_LEFT, 2, 0, "", 0)
        self.AddRadioButton(RDO_MATRIX, c4d.BFH_LEFT, 0, 0, "Matrix keys")
        self.AddRadioButton(RDO_SRT,    c4d.BFH_LEFT, 0, 0, "SRT keys")
        self.GroupEnd()

        self.GroupEnd()
        self.GroupEnd()

        self.GroupEnd()  # Animation tab

        # =====================================================================
        # OUTPUT
        # =====================================================================
        self.GroupBegin(TAB_COORD_OUTPUT, c4d.BFH_SCALEFIT | c4d.BFV_TOP, 1, 0, "Output", 0)
        self.GroupBorderSpace(14, 10, 14, 10)

        self.GroupBegin(0, c4d.BFH_SCALEFIT, 2, 0, "", 0)

        self.GroupBegin(GRP_COORD, c4d.BFH_SCALEFIT, 1, 0, "Units & Scale", c4d.BFV_TOP)
        self.GroupBorder(c4d.BORDER_GROUP_IN)
        self.GroupBorderSpace(12, 8, 12, 9)
        self.GroupBegin(0, c4d.BFH_LEFT, 2, 0, "", 0)
        self.AddStaticText(0, c4d.BFH_LEFT, 120, 0, "Global scale")
        self.AddEditNumber(EDT_SCALE, c4d.BFH_LEFT, 82, 0)
        self.GroupEnd()
        self.AddCheckbox(CHK_CONVERT_METERS, c4d.BFH_LEFT, 0, 0, "Convert C4D document units to meters")
        self.AddStaticText(0, c4d.BFH_SCALEFIT, 0, 0, "Final size = Global scale × document-unit-to-meter factor.")
        self.GroupEnd()

        self.GroupBegin(0, c4d.BFH_SCALEFIT, 1, 0, "World Axis", c4d.BFV_TOP)
        self.GroupBorder(c4d.BORDER_GROUP_IN)
        self.GroupBorderSpace(12, 8, 12, 9)
        self.AddCheckbox(CHK_TILTAN_AXIS, c4d.BFH_LEFT, 0, 0, "Export as X-right, Y-front, Z-up")
        self.AddStaticText(0, c4d.BFH_SCALEFIT, 0, 0, "C4D mapping: X → X, Z → Y-front, Y → Z-up.")
        self.GroupEnd()

        self.GroupEnd()

        self.GroupBegin(0, c4d.BFH_SCALEFIT, 1, 0, "File Format", c4d.BFV_TOP)
        self.GroupBorder(c4d.BORDER_GROUP_IN)
        self.GroupBorderSpace(12, 8, 12, 9)
        self.AddStaticText(0, c4d.BFH_SCALEFIT, 0, 0, "DirectX .X text format only.")
        self.AddStaticText(0, c4d.BFH_SCALEFIT, 0, 0, "Binary, world-space baking and manual axis rotations are hidden to protect the Tiltan splitter.")
        self.GroupEnd()

        self.GroupEnd()  # Output tab

        # =====================================================================
        # DIAGNOSTICS
        # =====================================================================
        self.GroupBegin(TAB_DIAGNOSTICS, c4d.BFH_SCALEFIT | c4d.BFV_TOP, 1, 0, "Diagnostics", 0)
        self.GroupBorderSpace(14, 10, 14, 10)

        self.GroupBegin(0, c4d.BFH_SCALEFIT, 1, 0, "Model Check", c4d.BFV_TOP)
        self.GroupBorder(c4d.BORDER_GROUP_IN)
        self.GroupBorderSpace(12, 8, 12, 9)
        self.AddButton(BTN_VALIDATE_DIAG, c4d.BFH_LEFT, 160, 24, "Validate P/N Structure")
        self.AddStaticText(0, c4d.BFH_SCALEFIT, 0, 0, "Checks PxxNyy parts and Lod_High / Lod_Medium / Lod_Low structure.")
        self.GroupEnd()

        self.GroupBegin(0, c4d.BFH_SCALEFIT, 1, 0, "Export Profile", c4d.BFV_TOP)
        self.GroupBorder(c4d.BORDER_GROUP_IN)
        self.GroupBorderSpace(12, 8, 12, 9)
        self.GroupBegin(0, c4d.BFH_SCALEFIT, 2, 0, "", 0)
        self.AddStaticText(0, c4d.BFH_SCALEFIT, 0, 0, "Geometry")
        self.AddStaticText(0, c4d.BFH_SCALEFIT, 0, 0, "MeshNormals")
        self.AddStaticText(0, c4d.BFH_SCALEFIT, 0, 0, "Axis")
        self.AddStaticText(0, c4d.BFH_SCALEFIT, 0, 0, "X-right / Y-front / Z-up")
        self.AddStaticText(0, c4d.BFH_SCALEFIT, 0, 0, "Units")
        self.AddStaticText(0, c4d.BFH_SCALEFIT, 0, 0, "meters")
        self.AddStaticText(0, c4d.BFH_SCALEFIT, 0, 0, "Splitter")
        self.AddStaticText(0, c4d.BFH_SCALEFIT, 0, 0, "P/N safe")
        self.GroupEnd()
        self.GroupEnd()

        self.GroupEnd()  # Diagnostics tab

        self.GroupEnd()  # Tabs

        self.AddSeparatorH(0, c4d.BFH_SCALEFIT)
        self.GroupBegin(0, c4d.BFH_SCALEFIT, 1, 0, "", 0)
        self.GroupBorderSpace(14, 7, 14, 8)
        self.AddStaticText(0, c4d.BFH_SCALEFIT, 0, 0,
                           "Ready · Tiltan compatible · MeshNormals · meters · P/N splitter-safe · Animation optional")
        self.GroupEnd()
        return True


    def InitValues(self):
        """Default verified Tiltan export profile."""
        try:
            self.SetBool(CHK_TILTAN_COMPAT, True)
            self.SetBool(CHK_TILTAN_AXIS, True)
            self.SetBool(CHK_CONVERT_METERS, True)

            self.SetBool(CHK_SCENE_ROOT, True)
            self.SetBool(CHK_EXPORT_SELECTED, False)

            self.SetBool(CHK_MESH, True)
            self.SetBool(CHK_DUMMY, True)
            self.SetBool(CHK_OTHER_NODES, True)
            self.SetBool(CHK_BONE, True)

            self.SetBool(CHK_MATERIAL, True)
            self.SetBool(CHK_INLINE, False)
            self.SetBool(CHK_DIFFUSE_MAP_CLR, True)

            self.SetBool(CHK_NORMAL, True)
            self.SetBool(CHK_TEXCOORDS, True)
            self.SetBool(CHK_TANGENT, False)
            self.SetBool(CHK_BITANGENT, False)
            self.SetBool(CHK_VERTEXCOLOR, False)

            self.SetBool(CHK_TEXTURES, True)
            self.SetBool(CHK_OVERWRITE, False)

            self.SetLong(CMB_GEOM_FORMAT, 0)
            self.SetFloat(EDT_SCALE, 1.0)

            # Safe defaults for controls that are no longer exposed in the UI.
            self.SetBool(CHK_DUPLICATE_NAME, False)
            self.SetBool(CHK_VERTEX_DUP, False)
            self.SetBool(CHK_CONVERT_TEX, False)
            self.SetBool(CHK_POWER2, False)
            self.SetBool(CHK_MULTI_TEX, False)
            self.SetBool(CHK_REMOVE_DUPS, False)
            self.SetBool(CHK_CREATE_FX, False)
            self.SetBool(CHK_NORMALMAP, False)
            self.SetBool(CHK_ANIM_CLIPS, False)
            self.SetString(EDT_CLIP_NAME, "Clip0")
            self.SetLong(EDT_CLIP_START, 0)
            self.SetLong(EDT_CLIP_END, 60)
            self.SetBool(CHK_SKIN, False)
            self.SetBool(CHK_WEIGHT_FIX, False)
            self.SetBool(CHK_KEYFRAMES_ONLY, False)
            self.SetBool(CHK_FX, False)
            self.SetBool(CHK_EFFECT_PARAMS, False)
            self.SetBool(CHK_FX_OVERWRITE, False)
            self.SetLong(CMB_TEX_FORMAT, 0)
            self.SetLong(EDT_PROC_TEX_W, 1024)
            self.SetLong(EDT_PROC_TEX_H, 1024)
            self.SetLong(EDT_SAMPLE_RATE, 1)
            self.SetLong(EDT_OPTIMIZE, 1)
            self.SetFloat(EDT_WEIGHT_FIX_VAL, 0.0)
            self.SetBool(RDO_MATRIX, True)
            self.SetBool(RDO_SRT, False)

            self._update_enabled()
        except Exception:
            pass
        try:
            if self._logo:
                self._logo.LayoutChanged()
                self._logo.Redraw()
            for b in (self._btn_export, self._btn_cancel, self._btn_validate):
                b.LayoutChanged()
                b.Redraw()
        except Exception:
            pass
        return True


    def _update_enabled(self):
        """Keep controls active only when their parent option is enabled."""
        try:
            has_tex = self.GetBool(CHK_TEXTURES)
            self.Enable(CHK_OVERWRITE, has_tex)

            anim_on = self.GetBool(CHK_ANIM_CLIPS)
            for ctrl in (
                EDT_CLIP_NAME, EDT_CLIP_START, EDT_CLIP_END,
                BTN_ADD_CLIP, BTN_INSERT_CLIP, BTN_DEL_CLIP,
                LST_CLIPS, EDT_SAMPLE_RATE, EDT_OPTIMIZE,
                CHK_KEYFRAMES_ONLY, CHK_SKIN, CHK_WEIGHT_FIX,
                EDT_WEIGHT_FIX_VAL, RDO_MATRIX, RDO_SRT
            ):
                try:
                    self.Enable(ctrl, anim_on)
                except Exception:
                    pass

            weight_on = anim_on and self.GetBool(CHK_WEIGHT_FIX)
            try:
                self.Enable(EDT_WEIGHT_FIX_VAL, weight_on)
            except Exception:
                pass

            if self.GetBool(CHK_TILTAN_COMPAT):
                self.SetLong(CMB_GEOM_FORMAT, 0)
                self.SetBool(CHK_NORMAL, True)
                self.SetBool(CHK_TEXCOORDS, True)
                self.SetBool(CHK_TILTAN_AXIS, True)
                self.SetBool(CHK_CONVERT_METERS, True)
                self.Enable(CMB_GEOM_FORMAT, False)
            else:
                self.Enable(CMB_GEOM_FORMAT, True)
        except Exception:
            pass

    def _validate_hierarchy(self):
        """Lightweight Tiltan P/N hierarchy check before export."""
        doc = documents.GetActiveDocument()
        if not doc:
            gui.MessageDialog("No active document.")
            return

        root = doc.GetFirstObject()
        if not root:
            gui.MessageDialog("Scene is empty.")
            return

        parts = []
        warnings = []

        def walk(obj):
            while obj:
                name = obj.GetName()
                if re.match(r"^P\d+N\d+_", name, re.IGNORECASE):
                    parts.append(obj)
                    child_names = []
                    child = obj.GetDown()
                    while child:
                        child_names.append(child.GetName())
                        child = child.GetNext()

                    low_names = [n.lower() for n in child_names]
                    for lod in ("lod_high", "lod_medium", "lod_low"):
                        if lod not in low_names:
                            warnings.append(f"{name}: missing {lod}")

                down = obj.GetDown()
                if down:
                    walk(down)
                obj = obj.GetNext()

        try:
            walk(root)
            msg = []
            msg.append("Tiltan hierarchy check")
            msg.append("")
            msg.append(f"Root: {root.GetName()}")
            msg.append(f"P/N parts detected: {len(parts)}")
            if parts:
                msg.append("")
                msg.append("Parts:")
                for p in parts[:20]:
                    msg.append("  - " + p.GetName())
                if len(parts) > 20:
                    msg.append(f"  ... +{len(parts)-20} more")

            if warnings:
                msg.append("")
                msg.append("Warnings:")
                for w in warnings[:20]:
                    msg.append("  - " + w)
                if len(warnings) > 20:
                    msg.append(f"  ... +{len(warnings)-20} more")
            else:
                msg.append("")
                msg.append("No obvious P/N + Lod hierarchy problems found.")

            msg.append("")
            msg.append("Compatibility export: ON" if self.GetBool(CHK_TILTAN_COMPAT) else "Compatibility export: OFF")
            msg.append("World Axis: X-right, Y-front, Z-up" if self.GetBool(CHK_TILTAN_AXIS) else "World Axis conversion: OFF")
            try:
                factor = _document_unit_to_meter_scale(doc) if self.GetBool(CHK_CONVERT_METERS) else 1.0
                msg.append(f"Meter conversion factor: {factor:.6g}")
                msg.append(f"Final export scale: {self.GetFloat(EDT_SCALE) * factor:.6g}")
            except Exception:
                msg.append("Meter conversion factor: unavailable, using 1.0")
            gui.MessageDialog("\n".join(msg))
        except Exception as e:
            gui.MessageDialog(f"Hierarchy validation failed:\n{e}")

    def _refresh_list(self):
        """Refresh the animation clip summary field."""
        try:
            if not self.clips:
                self.SetString(LST_CLIPS, "No clips defined.")
                return

            lines = []
            for i, clip in enumerate(self.clips):
                marker = ">" if i == self.sel_clip else " "
                lines.append(f"{marker} {i:02d}  {clip.get('name','Clip')}    {clip.get('start',0)} - {clip.get('end',0)}")
            self.SetString(LST_CLIPS, "\n".join(lines))
        except Exception:
            pass


    # ── Commands ──────────────────────────────────────────────────────────────
    def Command(self, id, msg):
        if id == BTN_CANCEL:
            self.Close()
            return True

        if id == BTN_VALIDATE or id == BTN_VALIDATE_DIAG:
            self._validate_hierarchy()
            return True

        if id == CHK_TILTAN_COMPAT:
            if self.GetBool(CHK_TILTAN_COMPAT):
                self.SetLong(CMB_GEOM_FORMAT, 0)
                self.SetBool(CHK_NORMAL, True)
                self.SetBool(CHK_TEXCOORDS, True)
                self.SetBool(CHK_DUPLICATE_NAME, False)
                self.SetBool(CHK_VERTEX_DUP, False)

        # Keep dependent controls greyed/enabled as toggles change.
        self._update_enabled()

        if id == BTN_ADD_CLIP:
            name  = self.GetString(EDT_CLIP_NAME) or f"Clip{len(self.clips)}"
            start = int(self.GetLong(EDT_CLIP_START))
            end   = int(self.GetLong(EDT_CLIP_END))
            self.clips.append({'name': name, 'start': start, 'end': end})
            self.sel_clip = len(self.clips) - 1
            self._refresh_list()
            return True

        if id == BTN_INSERT_CLIP:
            name  = self.GetString(EDT_CLIP_NAME) or f"Clip{len(self.clips)}"
            start = int(self.GetLong(EDT_CLIP_START))
            end   = int(self.GetLong(EDT_CLIP_END))
            idx   = max(0, self.sel_clip)
            self.clips.insert(idx, {'name': name, 'start': start, 'end': end})
            self.sel_clip = idx
            self._refresh_list()
            return True

        if id == BTN_DEL_CLIP:
            if self.clips:
                if not (0 <= self.sel_clip < len(self.clips)):
                    self.sel_clip = len(self.clips) - 1
                self.clips.pop(self.sel_clip)
                self.sel_clip = max(0, self.sel_clip - 1)
                self._refresh_list()
            return True

        if id == LST_CLIPS:
            return True

        if id == BTN_OK:
            self._run_export()
            return True

        return True

    # ── Export ────────────────────────────────────────────────────────────────
    def _run_export(self):
        doc = documents.GetActiveDocument()
        if not doc:
            gui.MessageDialog("No active document!")
            return

        try:
            filepath = storage.SaveDialog(
                type=c4d.FILESELECTTYPE_ANYTHING,
                title="Export DirectX .X File",
                force_suffix="x"
            )
            filepath = _filepath_to_str(filepath)
            if not filepath:
                return
            filepath = filepath.strip().strip('"')
            if not filepath.lower().endswith('.x'):
                filepath += '.x'
            folder = os.path.dirname(filepath)
            if folder and not os.path.isdir(folder):
                os.makedirs(folder)
        except Exception as e:
            gui.MessageDialog(f"Save path failed:\n{e}")
            return

        tex_fmt_map = {0: 'tga', 1: 'dds', 2: 'png', 3: 'bmp'}
        geom_fmt_map = {0: 'auto', 1: 'meshnormals', 2: 'decldata', 3: 'fvfdata'}

        settings = {
            # General
            'scene_root':      self.GetBool(CHK_SCENE_ROOT),
            'material':        self.GetBool(CHK_MATERIAL),
            'bone':            self.GetBool(CHK_BONE),
            'mesh':            self.GetBool(CHK_MESH),
            'dummy':           self.GetBool(CHK_DUMMY),
            'other_nodes':     self.GetBool(CHK_OTHER_NODES),
            'export_selected': self.GetBool(CHK_EXPORT_SELECTED),
            'duplicate_name':  False,
            'vertex_dup':      False,
            'only_animset':    False,
            'inline':          self.GetBool(CHK_INLINE),
            'bone_mesh':       False,
            'hierarchy':       True,
            'preserve_full_hierarchy': True,
            'asset_preserve_local': True,
            'tiltan_unique_part_leaf_names': self.GetBool(CHK_TILTAN_COMPAT),
            # Vertex
            'normals':         self.GetBool(CHK_NORMAL),
            'tangent':         self.GetBool(CHK_TANGENT),
            'bitangent':       self.GetBool(CHK_BITANGENT),
            'geom_format':     geom_fmt_map.get(self.GetLong(CMB_GEOM_FORMAT), 'auto'),
            'vertexcolor':     self.GetBool(CHK_VERTEXCOLOR),
            'texcoords':       self.GetBool(CHK_TEXCOORDS),
            # Textures
            'textures':        self.GetBool(CHK_TEXTURES),
            'overwrite':       self.GetBool(CHK_OVERWRITE),
            'convert_tex':     False,
            'tex_format':      'tga',
            'power2':          False,
            'multi_tex':       False,
            'remove_dups':     False,
            'create_fx':       False,
            'normalmap':       False,
            'proc_tex_w':      1024,
            'proc_tex_h':      1024,
            # Animation
            'anim_clips':      self.GetBool(CHK_ANIM_CLIPS),
            'skin':            self.GetBool(CHK_SKIN),
            'weight_fix':      self.GetBool(CHK_WEIGHT_FIX),
            'weight_fix_val':  self.GetFloat(EDT_WEIGHT_FIX_VAL),
            'sample_rate':     max(1, int(self.GetLong(EDT_SAMPLE_RATE))),
            'keyframes_only':  self.GetBool(CHK_KEYFRAMES_ONLY),
            # Keys
            'use_srt':         self.GetBool(RDO_SRT),
            'optimize':        max(1, int(self.GetLong(EDT_OPTIMIZE))),
            # Standard mat
            'diffuse_map_color': self.GetBool(CHK_DIFFUSE_MAP_CLR),
            # DX shader
            'effect_params':   False,
            'fx':              False,
            'fx_overwrite':    False,
            # Output / Tiltan-safe global transform
            'xrot':            0.0,
            'zrot':            0.0,
            'scale':           self.GetFloat(EDT_SCALE),
            'convert_units_to_meters': self.GetBool(CHK_CONVERT_METERS),
            'unit_to_meter_scale': _document_unit_to_meter_scale(doc),
            'tiltan_axis_zup_yfront': self.GetBool(CHK_TILTAN_AXIS),
            'right_handed':    False,
            'world_space':     False,
        }

        clips = self.clips if settings['anim_clips'] else []

        ok, err = do_export(filepath, settings, clips, doc)
        if ok:
            gui.MessageDialog(f"Export complete!\n{err}\n{filepath}")
            self.Close()
        else:
            gui.MessageDialog(f"Export failed:\n{err}")


# ─────────────────────────────────────────────────────────────────────────────
# Plugin Registration
# ─────────────────────────────────────────────────────────────────────────────

class DXExporterCmd(plugins.CommandData):
    _dlg = None

    def Execute(self, doc):
        if DXExporterCmd._dlg is None:
            DXExporterCmd._dlg = DXExportDialog()
        DXExporterCmd._dlg.Open(
            dlgtype=c4d.DLG_TYPE_ASYNC,
            pluginid=PLUGIN_ID,
            defaultw=680,
            defaulth=600,
            xpos=-1,
            ypos=-1,
        )
        return True

    def GetState(self, doc):
        return c4d.CMD_ENABLED


if __name__ == "__main__":
    plugins.RegisterCommandPlugin(
        id=PLUGIN_ID,
        str="DX Exporter by EAE v0020",
        info=0,
        help="Export to DirectX ASCII .x - AXE compatible",
        dat=DXExporterCmd(),
        icon=None,
    )
