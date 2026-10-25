"""Extract the playfair lookup tables from omg_hax.h / omg_hax.c into a Python module.

Usage: python tools/extract_playfair_tables.py omg_hax.h omg_hax.c > mirrorbridge/airplay/playfair/tables.py
"""
import re, sys, zlib, base64, struct

hdr = open(sys.argv[1]).read()
src = open(sys.argv[2]).read()
text = hdr + "\n" + src
# strip comments
text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
text = re.sub(r"//[^\n]*", "", text)

pat = re.compile(r"(unsigned char|unsigned long|uint32_t|char)\s+(\w+)\s*((?:\[\w*\])+)\s*=\s*\{(.*?)\};", re.S)
tables = {}
for m in pat.finditer(text):
    ctype, name, dims, body = m.groups()
    nums = re.findall(r"0[xX][0-9a-fA-F]+|\d+", body)
    vals = [int(n, 0) for n in nums]
    tables[name] = (ctype, vals)

wanted = ["message_key", "message_iv", "z_key", "x_key", "t_key", "table_s1", "table_s2", "table_s3",
          "table_s4", "table_s5", "table_s6", "table_s7", "table_s8", "table_s9", "table_s10",
          "sap_iv", "sap_key_material", "index_mangle", "initial_session_key", "static_source_1",
          "static_source_2", "default_sap"]
out = []
for name in wanted:
    ctype, vals = tables[name]
    width = 1 if ctype in ("unsigned char", "char") else 4
    for v in vals:
        assert v < (1 << (8 * width)), (name, hex(v))
    print(name, ctype, len(vals), file=sys.stderr)
    out.append((name, width, vals))

blob = b""
index = []
for name, width, vals in out:
    data = bytes(vals) if width == 1 else struct.pack("<%dI" % len(vals), *vals)
    index.append((name, width, len(blob), len(data)))
    blob += data

enc = base64.b64encode(zlib.compress(blob, 9)).decode()
lines = [enc[i:i + 76] for i in range(0, len(enc), 76)]
print('"""Lookup tables for the FairPlay (SAP) key decryption.')
print()
print("Generated from the playfair sources (omg_hax.h/omg_hax.c, GPL-3.0) bundled with")
print('UxPlay/RPiPlay. Do not edit by hand."""')
print()
print("import base64")
print("import struct")
print("import zlib")
print()
print("_BLOB = zlib.decompress(base64.b64decode(")
for l in lines:
    print('    "%s"' % l)
print("))")
print()
print("# name: (element width in bytes, offset, length in bytes)")
print("_INDEX = {")
for name, width, off, ln in index:
    print('    "%s": (%d, %d, %d),' % (name, width, off, ln))
print("}")
print()
print()
print("def _load(name):")
print("    width, off, ln = _INDEX[name]")
print("    data = _BLOB[off:off + ln]")
print("    if width == 1:")
print("        return data")
print('    return struct.unpack("<%dI" % (ln // 4), data)')
print()
print()
for name, width, off, ln in index:
    print("%s = _load(\"%s\")" % (name.upper(), name))
