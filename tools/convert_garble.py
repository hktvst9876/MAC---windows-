"""Mechanically transliterate playfair's garble() from C into Python.

Usage: python tools/convert_garble.py hand_garble.c > garble.py

Rules (emulating C semantics):
 * bufferN[...] stores are truncated to 8 bits (unsigned char)
 * stores to the unsigned int locals are truncated to 32 bits
 * '/' becomes '//' (every division in garble() has a non-negative dividend)
"""
import re, sys

src = open(sys.argv[1]).read()
body = src[src.index("void garble("):]
body = body[body.index("{") + 1: body.rindex("}")]
body = re.sub(r"//[^\n]*", "", body)
body = re.sub(r"printf\([^;]*\);", "", body)
body = re.sub(r"unsigned int [^;]*;", "", body)
stmts = [s.strip() for s in body.split(";")]
names = {"buffer0": "b0", "buffer1": "b1", "buffer2": "b2", "buffer3": "b3", "buffer4": "b4"}
out = []
for s in stmts:
    if not s:
        continue
    s = " ".join(s.split())
    for k, v in names.items():
        s = s.replace(k, v)
    s = s.replace("/", "//")
    m = re.match(r"^(b\d\[[^\]]+\]|[A-Z]\w*|tmp\d?)\s*([-+^]?=)\s*(.*)$", s)
    if not m:
        raise SystemExit("cannot parse: " + s)
    lhs, op, rhs = m.groups()
    mask = "0xFF" if lhs.startswith("b") else "M32"
    if op == "=":
        out.append("%s = (%s) & %s" % (lhs, rhs, mask))
    else:
        out.append("%s = (%s %s (%s)) & %s" % (lhs, lhs, op[0], rhs, mask))
print("def garble(b0, b1, b2, b3, b4):")
print('    """In-place port of hand_garble.c garble(); arguments are bytearrays."""')
for line in out:
    print("    " + line)
