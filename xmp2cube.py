#!/usr/bin/env python3
"""xmp2cube - convert Adobe Camera Raw / Lightroom "Look" presets (.xmp) into
standard .cube 3D LUT files (e.g. for DxO PhotoLab).

The color look in these presets lives in an embedded RGB table (crs:RGBTable /
crs:Table_<hash>). That table is a 3D LUT serialized with Adobe's dng_big_table
format: a zlib-compressed binary blob, text-encoded with a Z85-like base85
variant. This tool decodes it and writes a .cube file.

The decoded LUT declares its own primaries + transfer function (read from the
table). Two output modes:

  native : write the table verbatim, in its own primaries + transfer function.
           Lossless. In DxO, set the LUT color space to match (Rec.2020 for the
           v3.1 tables). Note: ProPhoto/1.8 (v3.0) has no DxO equivalent, so use
           srgb mode for those.

  srgb   : color-manage the look into a standard sRGB pipeline (input & output
           in sRGB primaries + sRGB gamma) by resampling, converting from the
           table's declared source space. In DxO, set the LUT color space to
           sRGB. Note: colors outside the sRGB gamut are clipped.
"""

import argparse
import glob
import os
import re
import struct
import sys
import zlib

# --------------------------------------------------------------------------
# 1. Base85 text decoding (dng_big_table::DecodeFromString)
# --------------------------------------------------------------------------

# kDecodeTable[96], indexed by (ord(char) - 32). 0xFF = ignore.
_KDECODE = [
    0xFF, 0x44, 0xFF, 0x54, 0x53, 0x52, 0xFF, 0x49, 0x4B, 0x4C, 0x46, 0x41, 0xFF, 0x3F, 0x3E, 0x45,
    0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08, 0x09,
    0x40, 0xFF, 0xFF, 0x42, 0xFF, 0x47, 0x51,
    0x24, 0x25, 0x26, 0x27, 0x28, 0x29, 0x2A, 0x2B, 0x2C, 0x2D,
    0x2E, 0x2F, 0x30, 0x31, 0x32, 0x33, 0x34, 0x35, 0x36, 0x37,
    0x38, 0x39, 0x3A, 0x3B, 0x3C, 0x3D,
    0x4D, 0xFF, 0x4E, 0x43, 0xFF, 0x48,
    0x0A, 0x0B, 0x0C, 0x0D, 0x0E, 0x0F, 0x10, 0x11, 0x12, 0x13,
    0x14, 0x15, 0x16, 0x17, 0x18, 0x19, 0x1A, 0x1B, 0x1C, 0x1D,
    0x1E, 0x1F, 0x20, 0x21, 0x22, 0x23,
    0x4F, 0x4A, 0x50, 0xFF, 0xFF,
]
assert len(_KDECODE) == 96


def decode_base85(text):
    """Decode the Adobe XMP base85 text into the compressed binary block."""
    out = bytearray()
    phase = 0
    value = 0
    for ch in text:
        e = ord(ch)
        if e < 32 or e > 127:
            continue
        d = _KDECODE[e - 32]
        if d > 85:
            continue
        phase += 1
        if phase == 1:
            value = d
        elif phase == 2:
            value += d * 85
        elif phase == 3:
            value += d * 85 ** 2
        elif phase == 4:
            value += d * 85 ** 3
        else:
            value += d * 85 ** 4
            out += struct.pack("<I", value & 0xFFFFFFFF)
            phase = 0
    if phase > 1:  # trailing partial group of `phase` chars -> phase-1 bytes
        out += struct.pack("<I", value & 0xFFFFFFFF)[:phase - 1]
    return bytes(out)


def decompress_block(compressed):
    """First 4 bytes (LE) = uncompressed size; remainder is a zlib stream."""
    if len(compressed) < 5:
        raise ValueError("compressed block too short")
    uncompressed_size = struct.unpack_from("<I", compressed, 0)[0]
    raw = zlib.decompress(compressed[4:])
    if len(raw) != uncompressed_size:
        raise ValueError(
            "size mismatch: header says %d, got %d" % (uncompressed_size, len(raw))
        )
    return raw


# --------------------------------------------------------------------------
# 2. Parse the dng_rgb_table binary structure
# --------------------------------------------------------------------------

_BTT_RGBTABLE = 1
_RGB_TABLE_VERSION = 1
PRIMARIES = ["sRGB", "Adobe", "ProPhoto", "P3", "Rec2020"]
GAMMA = ["Linear", "sRGB", "1.8", "2.2", "Rec2020"]
GAMUT = ["clip", "extend"]


class RGBTable:
    def __init__(self, divisions, samples, primaries, gamma, gamut,
                 min_amount, max_amount):
        self.divisions = divisions          # N (grid points per axis)
        self.samples = samples              # flat list of (r,g,b), uint16, dng order
        self.primaries = primaries          # index into PRIMARIES
        self.gamma = gamma                  # index into GAMMA
        self.gamut = gamut                  # index into GAMUT
        self.min_amount = min_amount
        self.max_amount = max_amount

    def sample_norm(self, ri, gi, bi):
        """Return the (r,g,b) output in [0,1] at integer grid indices."""
        n = self.divisions
        r, g, b = self.samples[(ri * n + gi) * n + bi]
        return (r / 65535.0, g / 65535.0, b / 65535.0)


def parse_rgb_table(raw):
    off = 0

    def u32():
        nonlocal off
        v = struct.unpack_from("<I", raw, off)[0]
        off += 4
        return v

    def u16():
        nonlocal off
        v = struct.unpack_from("<H", raw, off)[0]
        off += 2
        return v

    def f64():
        nonlocal off
        v = struct.unpack_from("<d", raw, off)[0]
        off += 8
        return v

    if u32() != _BTT_RGBTABLE:
        raise ValueError("not an RGB table")
    if u32() != _RGB_TABLE_VERSION:
        raise ValueError("unknown RGB table version")
    dimensions = u32()
    divisions = u32()
    if dimensions != 3:
        raise ValueError("only 3D tables supported (got %dD)" % dimensions)

    n = divisions
    nop = [((i * 0xFFFF + (n >> 1)) // (n - 1)) for i in range(n)]

    samples = [None] * (n * n * n)
    idx = 0
    for ri in range(n):
        for gi in range(n):
            for bi in range(n):
                r = (u16() + nop[ri]) & 0xFFFF
                g = (u16() + nop[gi]) & 0xFFFF
                b = (u16() + nop[bi]) & 0xFFFF
                samples[idx] = (r, g, b)
                idx += 1

    primaries = u32()
    gamma = u32()
    gamut = u32()
    min_amount = f64()
    max_amount = f64()

    return RGBTable(divisions, samples, primaries, gamma, gamut,
                    min_amount, max_amount)


def load_table_from_xmp(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        data = f.read()
    m = re.search(r'crs:Table_[0-9a-fA-F]+="([^"]*)"', data)
    if not m:
        raise ValueError("no crs:Table_ attribute found in %s" % path)
    return parse_rgb_table(decompress_block(decode_base85(m.group(1))))


# --------------------------------------------------------------------------
# 3. Color management (only needed for srgb mode)
# --------------------------------------------------------------------------

def _mat_inv(m):
    a, b, c = m[0]
    d, e, f = m[1]
    g, h, i = m[2]
    det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)
    inv = 1.0 / det
    return [
        [(e * i - f * h) * inv, (c * h - b * i) * inv, (b * f - c * e) * inv],
        [(f * g - d * i) * inv, (a * i - c * g) * inv, (c * d - a * f) * inv],
        [(d * h - e * g) * inv, (b * g - a * h) * inv, (a * e - b * d) * inv],
    ]


def _mat_mul_mat(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)]
            for i in range(3)]


def _mat_vec(m, v):
    return (m[0][0] * v[0] + m[0][1] * v[1] + m[0][2] * v[2],
            m[1][0] * v[0] + m[1][1] * v[1] + m[1][2] * v[2],
            m[2][0] * v[0] + m[2][1] * v[1] + m[2][2] * v[2])


# --- transfer functions: encoded <-> linear --------------------------------

def srgb_to_linear(v):
    return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4


def linear_to_srgb(v):
    v = 0.0 if v < 0.0 else (1.0 if v > 1.0 else v)
    return v * 12.92 if v <= 0.0031308 else 1.055 * v ** (1 / 2.4) - 0.055


_R2020_A = 1.09929682680944
_R2020_B = 0.018053968510807


def rec2020_to_linear(v):
    if v < 4.5 * _R2020_B:
        return v / 4.5
    return ((v + (_R2020_A - 1)) / _R2020_A) ** (1 / 0.45)


def linear_to_rec2020(v):
    v = 0.0 if v < 0.0 else (1.0 if v > 1.0 else v)
    if v < _R2020_B:
        return 4.5 * v
    return _R2020_A * v ** 0.45 - (_R2020_A - 1)


def _power_to_linear(g):
    return lambda v: (v if v > 0.0 else 0.0) ** g


def _linear_to_power(g):
    def f(v):
        v = 0.0 if v < 0.0 else (1.0 if v > 1.0 else v)
        return v ** (1.0 / g)
    return f


# (encoded->linear, linear->encoded) keyed by GAMMA name.
_TRANSFER = {
    "Linear":  (lambda v: v if v > 0.0 else 0.0,
                lambda v: 0.0 if v < 0.0 else (1.0 if v > 1.0 else v)),
    "sRGB":    (srgb_to_linear, linear_to_srgb),
    "1.8":     (_power_to_linear(1.8), _linear_to_power(1.8)),
    "2.2":     (_power_to_linear(2.2), _linear_to_power(2.2)),
    "Rec2020": (rec2020_to_linear, linear_to_rec2020),
}


# --- primaries -> XYZ in a common D65 connection space ----------------------

# (R, G, B chromaticities, white point) per PRIMARIES name.
_PRIMARIES_XY = {
    "sRGB":     ((0.6400, 0.3300), (0.3000, 0.6000), (0.1500, 0.0600), "D65"),
    "Adobe":    ((0.6400, 0.3300), (0.2100, 0.7100), (0.1500, 0.0600), "D65"),
    "ProPhoto": ((0.734699, 0.265301), (0.159597, 0.840403),
                 (0.036598, 0.000105), "D50"),
    "P3":       ((0.6800, 0.3200), (0.2650, 0.6900), (0.1500, 0.0600), "D65"),
    "Rec2020":  ((0.7080, 0.2920), (0.1700, 0.7970), (0.1310, 0.0460), "D65"),
}
_WHITE_XY = {"D65": (0.31270, 0.32900), "D50": (0.34567, 0.35850)}

_BRADFORD = [
    [0.8951, 0.2664, -0.1614],
    [-0.7502, 1.7135, 0.0367],
    [0.0389, -0.0685, 1.0296],
]


def _xy_to_xyz(x, y):
    return (x / y, 1.0, (1.0 - x - y) / y)


def _bradford(src_white, dst_white):
    s = _mat_vec(_BRADFORD, src_white)
    d = _mat_vec(_BRADFORD, dst_white)
    scale = [[d[0] / s[0], 0.0, 0.0],
             [0.0, d[1] / s[1], 0.0],
             [0.0, 0.0, d[2] / s[2]]]
    return _mat_mul_mat(_mat_inv(_BRADFORD), _mat_mul_mat(scale, _BRADFORD))


def _rgb_to_xyz_d65(name):
    """linear-RGB(name) -> XYZ, chromatically adapted to a common D65 white."""
    rxy, gxy, bxy, wname = _PRIMARIES_XY[name]
    white = _xy_to_xyz(*_WHITE_XY[wname])
    cols = [_xy_to_xyz(*rxy), _xy_to_xyz(*gxy), _xy_to_xyz(*bxy)]
    m = [[cols[j][i] for j in range(3)] for i in range(3)]
    s = _mat_vec(_mat_inv(m), white)
    m = [[m[i][j] * s[j] for j in range(3)] for i in range(3)]
    if wname != "D65":
        m = _mat_mul_mat(_bradford(white, _xy_to_xyz(*_WHITE_XY["D65"])), m)
    return m


def _primaries_matrix(src_name, dst_name):
    """linear-RGB(src) -> linear-RGB(dst), via the D65 connection space."""
    return _mat_mul_mat(_mat_inv(_rgb_to_xyz_d65(dst_name)),
                        _rgb_to_xyz_d65(src_name))


def _trilinear(table, cr, cg, cb):
    """Sample table (RGBTable) at continuous [0,1] coords, trilinearly."""
    n = table.divisions
    m = n - 1

    def axis(c):
        x = c * m
        if x <= 0:
            return 0, 0, 0.0
        if x >= m:
            return m, m, 0.0
        lo = int(x)
        return lo, lo + 1, x - lo

    r0, r1, fr = axis(cr)
    g0, g1, fg = axis(cg)
    b0, b1, fb = axis(cb)
    s = table.sample_norm
    out = [0.0, 0.0, 0.0]
    for dr, wr in ((r0, 1 - fr), (r1, fr)):
        if wr == 0.0:
            continue
        for dg, wg in ((g0, 1 - fg), (g1, fg)):
            if wg == 0.0:
                continue
            for db, wb in ((b0, 1 - fb), (b1, fb)):
                if wb == 0.0:
                    continue
                w = wr * wg * wb
                v = s(dr, dg, db)
                out[0] += w * v[0]
                out[1] += w * v[1]
                out[2] += w * v[2]
    return out


# --------------------------------------------------------------------------
# 4. .cube writers
# --------------------------------------------------------------------------

def _cube_header(fh, title, size, notes):
    fh.write("# Generated by xmp2cube\n")
    for line in notes:
        fh.write("# %s\n" % line)
    if title:
        fh.write('TITLE "%s"\n' % title.replace('"', "'"))
    fh.write("LUT_3D_SIZE %d\n" % size)
    fh.write("DOMAIN_MIN 0.0 0.0 0.0\n")
    fh.write("DOMAIN_MAX 1.0 1.0 1.0\n")


def _dxo_native_hint(prim, gamma):
    if prim == "Rec2020" and gamma == "Rec2020":
        return "In DxO: set the LUT color space to Rec.2020 (~DxO Wide Gamut)."
    if prim == "sRGB" and gamma == "sRGB":
        return "In DxO: set the LUT color space to sRGB."
    return ("In DxO there is no exact match for %s primaries + %s gamma; use "
            "srgb mode instead (-m srgb)." % (prim, gamma))


def write_cube_native(table, out_path, title):
    """Verbatim 3D LUT, in the table's own primaries + transfer function."""
    n = table.divisions
    prim, gamma = PRIMARIES[table.primaries], GAMMA[table.gamma]
    notes = [
        "Color space: %s primaries, %s transfer function (verbatim table)."
        % (prim, gamma),
        _dxo_native_hint(prim, gamma),
        "Look represented at 100% strength (RGBTable amount = 1.0).",
    ]
    with open(out_path, "w", encoding="ascii", newline="\n") as fh:
        _cube_header(fh, title, n, notes)
        # .cube ordering: red varies fastest.
        for bi in range(n):
            for gi in range(n):
                for ri in range(n):
                    r, g, b = table.sample_norm(ri, gi, bi)
                    fh.write("%.6f %.6f %.6f\n" % (r, g, b))


def write_cube_srgb(table, out_path, title, size):
    """Color-managed look baked into an sRGB-in / sRGB-out LUT.

    The source space (primaries + transfer function) is read from the table
    itself, so ProPhoto/1.8, Rec.2020/Rec.2020, etc. are all handled.
    """
    n = size
    m = n - 1
    src_prim, src_gamma = PRIMARIES[table.primaries], GAMMA[table.gamma]
    src_to_lin, lin_to_src = _TRANSFER[src_gamma]
    srgb_to_src = _primaries_matrix("sRGB", src_prim)
    src_to_srgb = _primaries_matrix(src_prim, "sRGB")
    notes = [
        "Color space: sRGB primaries + sRGB gamma (in and out).",
        "In DxO: set the imported LUT color space to sRGB.",
        "Baked from a %s/%s-gamma look; out-of-sRGB-gamut colors are clipped."
        % (src_prim, src_gamma),
        "Look represented at 100% strength (RGBTable amount = 1.0).",
    ]
    with open(out_path, "w", encoding="ascii", newline="\n") as fh:
        _cube_header(fh, title, n, notes)
        for bi in range(n):
            cb = bi / m
            for gi in range(n):
                cg = gi / m
                for ri in range(n):
                    cr = ri / m
                    # sRGB-encoded input -> linear sRGB
                    lin = (srgb_to_linear(cr),
                           srgb_to_linear(cg),
                           srgb_to_linear(cb))
                    # linear sRGB -> linear source primaries
                    ls = _mat_vec(srgb_to_src, lin)
                    # linear source -> source-encoded (table input space)
                    e = (lin_to_src(ls[0]), lin_to_src(ls[1]), lin_to_src(ls[2]))
                    # sample the look
                    o = _trilinear(table, e[0], e[1], e[2])
                    # source-encoded output -> linear source
                    ol = (src_to_lin(o[0]), src_to_lin(o[1]), src_to_lin(o[2]))
                    # linear source -> linear sRGB
                    os = _mat_vec(src_to_srgb, ol)
                    # linear sRGB -> sRGB-encoded output
                    fh.write("%.6f %.6f %.6f\n" % (
                        linear_to_srgb(os[0]),
                        linear_to_srgb(os[1]),
                        linear_to_srgb(os[2])))


# --------------------------------------------------------------------------
# 5. CLI
# --------------------------------------------------------------------------

def convert_one(in_path, out_path, mode, size):
    table = load_table_from_xmp(in_path)
    title = os.path.splitext(os.path.basename(in_path))[0]
    if mode == "native":
        write_cube_native(table, out_path, title)
    else:
        write_cube_srgb(table, out_path, title, size)
    return table


def main(argv=None):
    p = argparse.ArgumentParser(
        description="Convert Adobe Camera Raw .xmp Look presets to .cube LUTs.")
    p.add_argument("inputs", nargs="*", default=None,
                   help="Input .xmp files or directories (default: current dir).")
    p.add_argument("-o", "--outdir", default="cube",
                   help="Output directory (default: ./cube).")
    p.add_argument("-m", "--mode", choices=["native", "srgb"], default="native",
                   help="native = verbatim Rec.2020 LUT (lossless); "
                        "srgb = color-managed sRGB LUT (default: native).")
    p.add_argument("-s", "--size", type=int, default=33,
                   help="Grid size for srgb (resampled) mode (default: 33).")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args(argv)

    inputs = args.inputs or ["."]
    files = []
    for item in inputs:
        if os.path.isdir(item):
            files.extend(sorted(glob.glob(os.path.join(item, "*.xmp"))))
        else:
            files.append(item)
    if not files:
        p.error("no .xmp files found")

    os.makedirs(args.outdir, exist_ok=True)
    ok = 0
    for fp in files:
        base = os.path.splitext(os.path.basename(fp))[0]
        out_path = os.path.join(args.outdir, base + ".cube")
        try:
            t = convert_one(fp, out_path, args.mode, args.size)
        except Exception as e:
            print("FAIL %s: %s" % (fp, e), file=sys.stderr)
            continue
        ok += 1
        if args.verbose:
            print("%s -> %s  [%dx%dx%d, %s/%s]" % (
                os.path.basename(fp), out_path, t.divisions, t.divisions,
                t.divisions, PRIMARIES[t.primaries], GAMMA[t.gamma]))
        else:
            print("wrote %s" % out_path)
    print("\n%d/%d converted (mode=%s) -> %s" %
          (ok, len(files), args.mode, args.outdir))
    return 0 if ok == len(files) else 1


if __name__ == "__main__":
    sys.exit(main())
