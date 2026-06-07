# xmp2cube

Convert Adobe Camera Raw / Lightroom **Look presets (`.xmp`)** into standard
**`.cube` 3D LUT** files, for use in DxO PhotoLab, DaVinci Resolve, OBS, or any
LUT-capable application.

Many ACR/Lightroom "Look" presets carry their color grade as an embedded 3D RGB
lookup table. `xmp2cube` extracts that table and writes it out as a portable
`.cube` LUT — no Adobe software required, no dependencies beyond the Python
standard library.

## How it works

A Look preset embeds its color table in a `crs:Table_<hash>` attribute. That
value is Adobe's `dng_big_table` serialization: a zlib-compressed binary blob,
text-encoded with a Z85-like base85 variant. `xmp2cube`:

1. **Decodes** the base85 text and inflates the zlib stream.
2. **Parses** the binary `dng_rgb_table` structure (a 3D RGB LUT, stored as
   uint16 deltas from a neutral identity ramp).
3. **Reads the table's own color space** — its primaries and transfer function
   are recorded in the table and used directly; nothing is assumed.
4. **Writes** a `.cube` file, either verbatim or color-managed into sRGB.

## Requirements

- Python 3 (standard library only — no `pip install` needed).

## Usage

```bash
# Convert every .xmp in the current directory (native mode, output to ./cube)
python xmp2cube.py

# Choose an output directory and mode
python xmp2cube.py -m srgb -o cube_srgb/

# Convert specific files, with details printed
python xmp2cube.py "My Look.xmp" "Another Look.xmp" -v

# sRGB mode at a custom grid resolution (default 33)
python xmp2cube.py -m srgb -s 65
```

| Option | Meaning |
|--------|---------|
| `inputs` | One or more `.xmp` files or directories (default: current directory). |
| `-o, --outdir` | Output directory (default: `./cube`). |
| `-m, --mode` | `native` or `srgb` (default: `native`). |
| `-s, --size` | Grid size for `srgb` (resampled) mode (default: 33). |
| `-v, --verbose` | Print each table's dimensions and color space. |

## Output modes

| Mode | Color space of the `.cube` | When to use |
|------|----------------------------|-------------|
| `native` | The table's own primaries + transfer function, written **verbatim**. | Lossless. Best when your application can be told the LUT's exact color space. |
| `srgb` | **sRGB primaries + sRGB gamma**, in and out. | Universal. The look is resampled from the table's declared space into a plain sRGB-in/sRGB-out LUT. Colors outside the sRGB gamut are clipped. |

`native` is lossless but only useful if your host lets you declare the LUT's
input/output color space to match the table (e.g. Rec.2020). `srgb` is the safe,
unambiguous choice for most workflows — feed it sRGB, get sRGB back.

### Matching the color space in your host

A `.cube` file carries raw numbers; it does **not** tell the application what
primaries or transfer function those numbers live in. You must set that to match
the `.cube` you exported, or tones will shift (for example, a gamma-1.8 table
interpreted as Rec.2020 will look too light). Each generated `.cube` includes
header comments naming its declared color space.

- **`srgb` mode** → set the LUT color space to **sRGB**. (Unambiguous,
  recommended.)
- **`native` mode** → set it to match the table's declared space (shown in
  `-v` output and in the `.cube` header). Some spaces — e.g. ProPhoto with a
  1.8 gamma — have no exact equivalent in a given host; use `srgb` mode for
  those.

## Scope and limitations

`xmp2cube` extracts the preset's **embedded 3D RGB LUT** — and only that. It is
deliberately narrow:

- **Only the embedded color table is converted.** Other preset adjustments
  (exposure, tone curve, HSL, masks, profile selection, etc.) are *not* baked
  in. If a preset's look depends heavily on settings outside the embedded table,
  the `.cube` will not fully reproduce it. Presets often also rely on a specific
  **camera/input profile** being applied first; the `.cube` represents the grade
  on top of whatever input you feed it.
- **3D RGB tables only.** The parser handles the standard `dng_rgb_table`
  (3 dimensions). 1D curves and HSV/hue-based Look tables are not supported.
- **The look is exported at full strength** (table amount = 1.0). The preset's
  min/max amount range is read but not applied.
- **Table discovery** looks for a `crs:Table_<hash>` attribute and uses the
  first one found. Presets that store the table differently, or carry several
  tables, may not convert as expected.
- **Color spaces** are limited to those Adobe defines: primaries
  `sRGB, Adobe RGB, ProPhoto, Display P3, Rec.2020` and transfer functions
  `Linear, sRGB, 1.8, 2.2, Rec.2020`.

## Project layout

```
xmp2cube.py    # the converter (single file, stdlib only)
README.md
```

## License

See repository for license details.
