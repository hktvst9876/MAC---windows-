"""Pure-Python port of the "playfair" FairPlay (SAP v1) key decryption.

AirPlay senders encrypt the per-session AES key ("ekey") with Apple's FairPlay
scheme. The reverse-engineered "playfair" C implementation (by EstebanKubata,
distributed with RPiPlay and UxPlay under the GPL-3.0) recovers it. This module
is a line-by-line port of that code. C integer semantics are emulated
explicitly: stores to ``unsigned char`` buffers are masked to 8 bits and stores
to ``uint32_t``/``unsigned int`` values are masked to 32 bits.

The port is verified against the original C implementation with recorded test
vectors (see ``tests/test_playfair.py``).
"""

import math

from .tables import (
    DEFAULT_SAP,
    INDEX_MANGLE,
    INITIAL_SESSION_KEY,
    MESSAGE_IV,
    MESSAGE_KEY,
    STATIC_SOURCE_1,
    STATIC_SOURCE_2,
    T_KEY,
    TABLE_S1,
    TABLE_S2,
    TABLE_S3,
    TABLE_S4,
    TABLE_S5,
    TABLE_S6,
    TABLE_S7,
    TABLE_S8,
    TABLE_S9,
    TABLE_S10,
    X_KEY,
    Z_KEY,
)

M32 = 0xFFFFFFFF


def _get32(buf, off):
    return buf[off] | (buf[off + 1] << 8) | (buf[off + 2] << 16) | (buf[off + 3] << 24)


def _put32(buf, off, value):
    buf[off] = value & 0xFF
    buf[off + 1] = (value >> 8) & 0xFF
    buf[off + 2] = (value >> 16) & 0xFF
    buf[off + 3] = (value >> 24) & 0xFF


# ---------------------------------------------------------------------------
# omg_hax.c
# ---------------------------------------------------------------------------

def _xor_key(data, key):
    return bytearray(data[i] ^ key[i % 16] for i in range(len(data)))


def _permute_block_1(block):
    s3 = TABLE_S3
    block[0] = s3[block[0]]
    block[4] = s3[0x400 + block[4]]
    block[8] = s3[0x800 + block[8]]
    block[12] = s3[0xC00 + block[12]]

    tmp = block[13]
    block[13] = s3[0x100 + block[9]]
    block[9] = s3[0xD00 + block[5]]
    block[5] = s3[0x900 + block[1]]
    block[1] = s3[0x500 + tmp]

    tmp = block[2]
    block[2] = s3[0xA00 + block[10]]
    block[10] = s3[0x200 + tmp]
    tmp = block[6]
    block[6] = s3[0xE00 + block[14]]
    block[14] = s3[0x600 + tmp]

    tmp = block[3]
    block[3] = s3[0xF00 + block[7]]
    block[7] = s3[0x300 + block[11]]
    block[11] = s3[0x700 + block[15]]
    block[15] = s3[0xB00 + tmp]


def _pt2(i):
    return ((71 * i) % 144) << 8


def _permute_block_2(block, rnd):
    s4 = TABLE_S4
    r = rnd * 16
    block[0] = s4[_pt2(r + 0) + block[0]]
    block[4] = s4[_pt2(r + 4) + block[4]]
    block[8] = s4[_pt2(r + 8) + block[8]]
    block[12] = s4[_pt2(r + 12) + block[12]]

    tmp = block[13]
    block[13] = s4[_pt2(r + 13) + block[9]]
    block[9] = s4[_pt2(r + 9) + block[5]]
    block[5] = s4[_pt2(r + 5) + block[1]]
    block[1] = s4[_pt2(r + 1) + tmp]

    tmp = block[2]
    block[2] = s4[_pt2(r + 2) + block[10]]
    block[10] = s4[_pt2(r + 10) + tmp]
    tmp = block[6]
    block[6] = s4[_pt2(r + 6) + block[14]]
    block[14] = s4[_pt2(r + 14) + tmp]

    tmp = block[3]
    block[3] = s4[_pt2(r + 3) + block[7]]
    block[7] = s4[_pt2(r + 7) + block[11]]
    block[11] = s4[_pt2(r + 11) + block[15]]
    block[15] = s4[_pt2(r + 15) + tmp]


def generate_key_schedule(key_material):
    """Return the 11x4 word key schedule for 16 bytes of key material."""
    buf = _xor_key(key_material[:16], T_KEY)
    schedule = [[0, 0, 0, 0] for _ in range(11)]
    ti = 0
    for rnd in range(11):
        schedule[rnd][0] = _get32(buf, 0)
        t1 = ((31 * ti) % 0x28) << 8
        t2 = ((31 * (ti + 1)) % 0x28) << 8
        t3 = ((31 * (ti + 2)) % 0x28) << 8
        t4 = ((31 * (ti + 3)) % 0x28) << 8
        ti += 4
        buf[0] ^= TABLE_S1[t1 + buf[0x0D]] ^ INDEX_MANGLE[rnd]
        buf[1] ^= TABLE_S1[t2 + buf[0x0E]]
        buf[2] ^= TABLE_S1[t3 + buf[0x0F]]
        buf[3] ^= TABLE_S1[t4 + buf[0x0C]]
        schedule[rnd][1] = _get32(buf, 4)
        _put32(buf, 4, _get32(buf, 4) ^ _get32(buf, 0))
        schedule[rnd][2] = _get32(buf, 8)
        _put32(buf, 8, _get32(buf, 8) ^ _get32(buf, 4))
        schedule[rnd][3] = _get32(buf, 12)
        _put32(buf, 12, _get32(buf, 12) ^ _get32(buf, 8))
    return schedule


def cycle(block, schedule):
    """Decrypt one 16-byte block in place with the given key schedule."""
    s5, s6, s7, s8 = TABLE_S5, TABLE_S6, TABLE_S7, TABLE_S8
    for w in range(4):
        _put32(block, 4 * w, _get32(block, 4 * w) ^ schedule[10][w])
    _permute_block_1(block)
    for rnd in range(9):
        key = schedule[9 - rnd]
        k0, k1, k2, k3 = key
        ab = (s5[block[3] ^ (k0 >> 24) & 0xFF]
              ^ s6[block[2] ^ (k0 >> 16) & 0xFF]
              ^ s8[block[0] ^ k0 & 0xFF]
              ^ s7[block[1] ^ (k0 >> 8) & 0xFF])
        _put32(block, 0, ab)
        ab = (s5[block[7] ^ (k1 >> 24) & 0xFF]
              ^ s6[block[6] ^ (k1 >> 16) & 0xFF]
              ^ s7[block[5] ^ (k1 >> 8) & 0xFF]
              ^ s8[block[4] ^ k1 & 0xFF])
        _put32(block, 4, ab)
        _put32(block, 8, s5[block[11] ^ (k2 >> 24) & 0xFF]
               ^ s6[block[10] ^ (k2 >> 16) & 0xFF]
               ^ s7[block[9] ^ (k2 >> 8) & 0xFF]
               ^ s8[block[8] ^ k2 & 0xFF])
        _put32(block, 12, s5[block[15] ^ (k3 >> 24) & 0xFF]
               ^ s6[block[14] ^ (k3 >> 16) & 0xFF]
               ^ s7[block[13] ^ (k3 >> 8) & 0xFF]
               ^ s8[block[12] ^ k3 & 0xFF])
        _permute_block_2(block, 8 - rnd)
    for w in range(4):
        _put32(block, 4 * w, _get32(block, 4 * w) ^ schedule[0][w])


def _mt(i):
    return ((97 * i) % 144) << 8


def decrypt_message(message_in):
    """Decrypt the 128-byte payload of the 164-byte fp-setup message."""
    mode = message_in[12]
    if mode not in (0, 1, 2, 3):
        raise ValueError("unsupported FairPlay mode %d" % mode)
    s2, s9, s10 = TABLE_S2, TABLE_S9, TABLE_S10
    mk = MESSAGE_KEY
    m = mode * 144
    out = bytearray(128)
    for i in range(8):
        if mode == 3:
            start = 0x80 - 0x10 * i
        else:
            start = 0x10 * (i + 1)
        buf = bytearray(message_in[start:start + 16])
        for j in range(9):
            base = 0x80 - 0x10 * j
            buf[0x0] = s2[_mt(base + 0x0) + buf[0x0]] ^ mk[m + base + 0x0]
            buf[0x4] = s2[_mt(base + 0x4) + buf[0x4]] ^ mk[m + base + 0x4]
            buf[0x8] = s2[_mt(base + 0x8) + buf[0x8]] ^ mk[m + base + 0x8]
            buf[0xC] = s2[_mt(base + 0xC) + buf[0xC]] ^ mk[m + base + 0xC]

            tmp = buf[0x0D]
            buf[0xD] = s2[_mt(base + 0xD) + buf[0x9]] ^ mk[m + base + 0xD]
            buf[0x9] = s2[_mt(base + 0x9) + buf[0x5]] ^ mk[m + base + 0x9]
            buf[0x5] = s2[_mt(base + 0x5) + buf[0x1]] ^ mk[m + base + 0x5]
            buf[0x1] = s2[_mt(base + 0x1) + tmp] ^ mk[m + base + 0x1]

            tmp = buf[0x02]
            buf[0x2] = s2[_mt(base + 0x2) + buf[0xA]] ^ mk[m + base + 0x2]
            buf[0xA] = s2[_mt(base + 0xA) + tmp] ^ mk[m + base + 0xA]
            tmp = buf[0x06]
            buf[0x6] = s2[_mt(base + 0x6) + buf[0xE]] ^ mk[m + base + 0x6]
            buf[0xE] = s2[_mt(base + 0xE) + tmp] ^ mk[m + base + 0xE]

            tmp = buf[0x3]
            buf[0x3] = s2[_mt(base + 0x3) + buf[0x7]] ^ mk[m + base + 0x3]
            buf[0x7] = s2[_mt(base + 0x7) + buf[0xB]] ^ mk[m + base + 0x7]
            buf[0xB] = s2[_mt(base + 0xB) + buf[0xF]] ^ mk[m + base + 0xB]
            buf[0xF] = s2[_mt(base + 0xF) + tmp] ^ mk[m + base + 0xF]

            for w in range(4):
                o = 4 * w
                _put32(buf, o, s9[buf[o]] ^ s9[0x100 + buf[o + 1]]
                       ^ s9[0x200 + buf[o + 2]] ^ s9[0x300 + buf[o + 3]])

        buf[0x0] = s10[(0x0 << 8) + buf[0x0]]
        buf[0x4] = s10[(0x4 << 8) + buf[0x4]]
        buf[0x8] = s10[(0x8 << 8) + buf[0x8]]
        buf[0xC] = s10[(0xC << 8) + buf[0xC]]

        tmp = buf[0x0D]
        buf[0xD] = s10[(0xD << 8) + buf[0x9]]
        buf[0x9] = s10[(0x9 << 8) + buf[0x5]]
        buf[0x5] = s10[(0x5 << 8) + buf[0x1]]
        buf[0x1] = s10[(0x1 << 8) + tmp]

        tmp = buf[0x02]
        buf[0x2] = s10[(0x2 << 8) + buf[0xA]]
        buf[0xA] = s10[(0xA << 8) + tmp]
        tmp = buf[0x06]
        buf[0x6] = s10[(0x6 << 8) + buf[0xE]]
        buf[0xE] = s10[(0xE << 8) + tmp]

        tmp = buf[0x3]
        buf[0x3] = s10[(0x3 << 8) + buf[0x7]]
        buf[0x7] = s10[(0x7 << 8) + buf[0xB]]
        buf[0xB] = s10[(0xB << 8) + buf[0xF]]
        buf[0xF] = s10[(0xF << 8) + tmp]

        if mode == 3:
            dst = 0x70 - 0x10 * i
            prev = message_in[dst:dst + 16] if i < 7 else MESSAGE_IV[16 * mode:16 * mode + 16]
        else:
            dst = 0x10 * i
            prev = message_in[dst:dst + 16] if i > 0 else MESSAGE_IV[16 * mode:16 * mode + 16]
        for k in range(16):
            out[dst + k] = buf[k] ^ prev[k]
    return out


def generate_session_key(old_sap, message_in):
    decrypted = decrypt_message(message_in)
    new_sap = bytearray(320)
    new_sap[0x000:0x011] = STATIC_SOURCE_1
    new_sap[0x011:0x091] = decrypted
    new_sap[0x091:0x111] = old_sap[0x80:0x100]
    new_sap[0x111:0x140] = STATIC_SOURCE_2
    session_key = bytearray(INITIAL_SESSION_KEY)
    for rnd in range(5):
        base = new_sap[rnd * 64:rnd * 64 + 64]
        md5 = modified_md5(base, session_key)
        sap_hash(base, session_key)
        for i in range(0, 16, 4):
            _put32(session_key, i, (_get32(session_key, i) + _get32(md5, i)) & M32)
    for i in range(0, 16, 4):
        session_key[i:i + 4] = session_key[i:i + 4][::-1]
    for i in range(16):
        session_key[i] ^= 121
    return session_key


# ---------------------------------------------------------------------------
# modified_md5.c
# ---------------------------------------------------------------------------

_SHIFT = (7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22, 7, 12, 17, 22,
          5, 9, 14, 20, 5, 9, 14, 20, 5, 9, 14, 20, 5, 9, 14, 20,
          4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23, 4, 11, 16, 23,
          6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21, 6, 10, 15, 21)

_MD5_K = tuple(int((1 << 32) * abs(math.sin(i + 1))) & M32 for i in range(64))


def _rol32(value, count):
    value &= M32
    return ((value << count) & M32) | (value >> (32 - count))


def _swap_words(buf, a, b):
    a *= 4
    b *= 4
    buf[a:a + 4], buf[b:b + 4] = buf[b:b + 4], buf[a:a + 4]


def modified_md5(original_block, key_in):
    block = bytearray(original_block[:64])
    kw = [_get32(key_in, 4 * i) for i in range(4)]
    a, b, c, d = kw
    for i in range(64):
        if i < 16:
            j = i
        elif i < 32:
            j = (5 * i + 1) % 16
        elif i < 48:
            j = (3 * i + 5) % 16
        else:
            j = 7 * i % 16
        inp = (block[4 * j] << 24) | (block[4 * j + 1] << 16) | (block[4 * j + 2] << 8) | block[4 * j + 3]
        z = (a + inp + _MD5_K[i]) & M32
        if i < 16:
            f = (b & c) | (~b & d)
        elif i < 32:
            f = (b & d) | (c & ~d)
        elif i < 48:
            f = b ^ c ^ d
        else:
            f = c ^ (b | (~d & M32))
        z = _rol32(z + (f & M32), _SHIFT[i])
        z = (z + b) & M32
        a, b, c, d = d, z, b, c
        if i == 31:
            _swap_words(block, a & 15, b & 15)
            _swap_words(block, c & 15, d & 15)
            _swap_words(block, (a >> 4) & 15, (b >> 4) & 15)
            _swap_words(block, (a >> 8) & 15, (b >> 8) & 15)
            _swap_words(block, (a >> 12) & 15, (b >> 12) & 15)
    out = bytearray(16)
    _put32(out, 0, kw[0] + a)
    _put32(out, 4, kw[1] + b)
    _put32(out, 8, kw[2] + c)
    _put32(out, 12, kw[3] + d)
    return out


# ---------------------------------------------------------------------------
# sap_hash.c / hand_garble.c
# ---------------------------------------------------------------------------

def rol8(value, count):
    value &= 0xFF
    return ((value << count) & 0xFF) | (value >> (8 - count))


def rol8x(value, count):
    value &= 0xFF
    return (value << count) | (value >> (8 - count))


def weird_ror8(value, count):
    if count == 0:
        return 0
    value &= 0xFF
    return ((value >> count) & 0xFF) | (value << (8 - count))


def weird_rol8(value, count):
    if count == 0:
        return 0
    value &= 0xFF
    return ((value << count) & 0xFF) | (value >> (8 - count))


def weird_rol32(value, count):
    if count == 0:
        return 0
    value &= 0xFF
    return (value << count) ^ (value >> (8 - count))


def garble(b0, b1, b2, b3, b4):
    """In-place port of hand_garble.c garble(); arguments are bytearrays.

    Generated mechanically from the C source by tools/convert_garble.py.
    """
    b2[12] = (0x14 + (((b1[64] & 92) | ((b1[99] // 3) & 35)) & b4[rol8x(b4[(b1[206] % 21)],4) % 21])) & 0xFF
    b1[4] = ((b1[99] // 5) * (b1[99] // 5) * 2) & 0xFF
    b2[34] = (0xb8) & 0xFF
    b1[153] = (b1[153] ^ ((b2[b1[203] % 35] * b2[b1[203] % 35] * b1[190]))) & 0xFF
    b0[3] = (b0[3] - ((((b4[b1[205] % 21]>>1) & 80) | 0xe6440))) & 0xFF
    b0[16] = (0x93) & 0xFF
    b0[13] = (0x62) & 0xFF
    b1[33] = (b1[33] - ((b4[b1[36] % 21] & 0xf6))) & 0xFF
    tmp2 = (b2[b1[67] % 35]) & M32
    b2[12] = (0x07) & 0xFF
    tmp = (b0[b1[181] % 20]) & M32
    b1[2] = (b1[2] - (3136)) & 0xFF
    b0[19] = (b4[b1[58] % 21]) & 0xFF
    b3[0] = (92 - b2[b1[32] % 35]) & 0xFF
    b3[4] = (b2[b1[15] % 35] + 0x9e) & 0xFF
    b1[34] = (b1[34] + ((b4[((b2[b1[15] % 35] + 0x9e) & 0xff) % 21] // 5))) & 0xFF
    b0[19] = (b0[19] + (0xfffffee6 - ((b0[b3[4] % 20]>>1) & 102))) & 0xFF
    b1[15] = ((3*(((b1[72] >> (b4[b1[190] % 21] & 7)) ^ (b1[72] << ((7 - (b4[b1[190] % 21]-1)&7)))) - (3*b4[b1[126] % 21]))) ^ b1[15]) & 0xFF
    b0[15] = (b0[15] ^ (b2[b1[181] % 35] * b2[b1[181] % 35] * b2[b1[181] % 35])) & 0xFF
    b2[4] = (b2[4] ^ (b1[202]//3)) & 0xFF
    A = (92 - b0[b3[0] % 20]) & M32
    E = ((A & 0xc6) | (~b1[105] & 0xc6) | (A & (~b1[105]))) & M32
    b2[1] = (b2[1] + ((E*E*E))) & 0xFF
    b0[19] = (b0[19] ^ (((224 | (b4[b1[92] % 21] & 27)) * b2[b1[41] % 35]) // 3)) & 0xFF
    b1[140] = (b1[140] + (weird_ror8(92, b1[5] & 7))) & 0xFF
    b2[12] = (b2[12] + (((((~b1[4]) ^ b2[b1[12] % 35]) | b1[182]) & 192) | (((~b1[4]) ^ b2[b1[12] % 35]) & b1[182]))) & 0xFF
    b1[36] = (b1[36] + (125)) & 0xFF
    b1[124] = (rol8x((((74 & b1[138]) | ((74 | b1[138]) & b0[15])) & b0[b1[43] % 20]) | (((74 & b1[138]) | ((74 | b1[138]) & b0[15]) | b0[b1[43] % 20]) & 95), 4)) & 0xFF
    b3[8] = (((((b0[b3[4] % 20] & 95)) & ((b4[b1[68] % 21] & 46) << 1)) | 16) ^ 92) & 0xFF
    A = (b1[177] + b4[b1[79] % 21]) & M32
    D = ((((A >> 1) | ((3 * b1[148]) // 5)) & b2[1]) | ((A >> 1) & ((3 * b1[148])//5))) & M32
    b3[12] = (((-34 - D))) & 0xFF
    A = (8 - ((b2[22] & 7))) & M32
    B = ((b1[33] >> (A & 7))) & M32
    C = (b1[33] << (b2[22] & 7)) & M32
    b2[16] = (b2[16] + (((b2[b3[0] % 35] & 159) | b0[b3[4] % 20] | 8) - ((B^C) | 128))) & 0xFF
    b0[14] = (b0[14] ^ (b2[b3[12] % 35])) & 0xFF
    A = (weird_rol8(b4[b0[b1[201] % 20] % 21], ((b2[b1[112] % 35] << 1) & 7))) & M32
    D = ((b0[b1[208] % 20] & 131) | (b0[b1[164] % 20] & 124)) & M32
    b1[19] = (b1[19] + ((A & (D//5)) | ((A | (D//5)) & 37))) & 0xFF
    b2[8] = (weird_ror8(140, ((b4[b1[45] % 21] + 92) * (b4[b1[45] % 21] + 92)) & 7)) & 0xFF
    b1[190] = (56) & 0xFF
    b2[8] = (b2[8] ^ (b3[0])) & 0xFF
    b1[53] = (~((b0[b1[83] % 20] | 204)//5)) & 0xFF
    b0[13] = (b0[13] + (b0[b1[41] % 20])) & 0xFF
    b0[10] = (((b2[b3[0] % 35] & b1[2]) | ((b2[b3[0] % 35] | b1[2]) & b3[12])) // 15) & 0xFF
    A = ((((56 | (b4[b1[2] % 21] & 68)) | b2[b3[8] % 35]) & 42) | (((b4[b1[2] % 21] & 68) | 56) & b2[b3[8] % 35])) & M32
    b3[16] = ((A*A) + 110) & 0xFF
    b3[20] = (202 - b3[16]) & 0xFF
    b3[24] = (b1[151]) & 0xFF
    b2[13] = (b2[13] ^ (b4[b3[0] % 21])) & 0xFF
    B = (((b2[b1[179] % 35] - 38) & 177) | (b3[12] & 177)) & M32
    C = (((b2[b1[179] % 35] - 38)) & b3[12]) & M32
    b3[28] = (30 + ((B | C) * (B | C))) & 0xFF
    b3[32] = (b3[28] + 62) & 0xFF
    A = (((b3[20] + (b3[0] & 74)) | ~b4[b3[0] % 21]) & 121) & M32
    B = (((b3[20] + (b3[0] & 74)) & ~b4[b3[0] % 21])) & M32
    tmp3 = ((A|B)) & M32
    C = (((((A|B) ^ 0xffffffa6) | b3[0]) & 4) | (((A|B) ^ 0xffffffa6) & b3[0])) & M32
    b1[47] = ((b2[b1[89] % 35] + C) ^ b1[47]) & 0xFF
    b3[36] = (((rol8((tmp & 179) + 68, 2) & b0[3]) | (tmp2 & ~b0[3])) - 15) & 0xFF
    b1[123] = (b1[123] ^ (221)) & 0xFF
    A = (((b4[b3[0] % 21]) // 3) - b2[b3[4] % 35]) & M32
    C = ((((b3[0] & 163) + 92) & 246) | (b3[0] & 92)) & M32
    E = (((C | b3[24]) & 54) | (C & b3[24])) & M32
    b3[40] = (A - E) & 0xFF
    b3[44] = (tmp3 ^ 81 ^ (((b3[0] >> 1) & 101) + 26)) & 0xFF
    b3[48] = (b2[b3[4] % 35] & 27) & 0xFF
    b3[52] = (27) & 0xFF
    b3[56] = (199) & 0xFF
    b3[64] = (b3[4] + (((((((b3[40] | b3[24]) & 177) | (b3[40] & b3[24])) & ((((b4[b3[0] % 20] & 177) | 176)) | ((b4[b3[0] % 21]) & ~3))) | ((((b3[40] & b3[24]) | ((b3[40] | b3[24]) & 177)) & 199) | ((((b4[b3[0] % 21] & 1) + 176) | (b4[b3[0] % 21] & ~3)) & b3[56]))) & (~b3[52])) | b3[48])) & 0xFF
    b2[33] = (b2[33] ^ (b1[26])) & 0xFF
    b1[106] = (b1[106] ^ (b3[20] ^ 133)) & 0xFF
    b2[30] = (((b3[64] // 3) - (275 | (b3[0] & 247))) ^ b0[b1[122] % 20]) & 0xFF
    b1[22] = ((b2[b1[90] % 35] & 95) | 68) & 0xFF
    A = ((b4[b3[36] % 21] & 184) | (b2[b3[44] % 35] & ~184)) & M32
    b2[18] = (b2[18] + (((A*A*A) >> 1))) & 0xFF
    b2[5] = (b2[5] - (b4[b1[92] % 21])) & 0xFF
    A = ((((b1[41] & ~24)|(b2[b1[183] % 35] & 24)) & (b3[16] + 53)) | (b3[20] & b2[b3[20] % 35])) & M32
    B = ((b1[17] & (~b3[44])) | (b0[b1[59] % 20] & b3[44])) & M32
    b2[18] = (b2[18] ^ ((A*B))) & 0xFF
    A = (weird_ror8(b1[11], b2[b1[28] % 35] & 7) & 7) & M32
    B = ((((b0[b1[93] % 20] & ~b0[14]) | (b0[14] & 150)) & ~28) | (b1[7] & 28)) & M32
    b2[22] = ((((((B | weird_rol8(b2[b3[0] % 35], A)) & b2[33]) | (B & weird_rol8(b2[b3[0] % 35], A))) + 74) & 0xff)) & 0xFF
    A = (b4[(b0[b1[39] % 20] ^ 217) % 21]) & M32
    b0[15] = (b0[15] - (((((b3[20] | b3[0]) & 214) | (b3[20] & b3[0])) & A) | ((((b3[20] | b3[0]) & 214) | (b3[20] & b3[0]) | A) & b3[32]))) & 0xFF
    B = ((((b2[b1[57] % 35] & b0[b3[64] % 20]) | ((b0[b3[64] % 20] | b2[b1[57] % 35]) & 95) | (b3[64] & 45) | 82) & 32)) & M32
    C = (((b2[b1[57] % 35] & b0[b3[64] % 20]) | ((b2[b1[57] % 35] | b0[b3[64] % 20]) & 95)) & ((b3[64] & 45) | 82)) & M32
    D = (((((b3[0]//3) - (b3[64]|b1[22]))) ^ (b3[28] + 62) ^ ((B|C)))) & M32
    T = (b0[(D & 0xff) % 20]) & M32
    b3[68] = ((b0[b1[99] % 20] * b0[b1[99] % 20] * b0[b1[99] % 20] * b0[b1[99] % 20]) | b2[b3[64] % 35]) & 0xFF
    U = (b0[b1[50] % 20]) & M32
    W = (b2[b1[138] % 35]) & M32
    X = (b4[b1[39] % 21]) & M32
    Y = (b0[b1[4] % 20]) & M32
    Z = (b4[b1[202] % 21]) & M32
    V = (b0[b1[151] % 20]) & M32
    S = (b2[b1[14] % 35]) & M32
    R = (b0[b1[145] % 20]) & M32
    A = ((b2[b3[68] % 35] & b0[b1[209] % 20]) | ((b2[b3[68] % 35] | b0[b1[209] % 20]) & 24)) & M32
    B = (weird_rol8(b4[b1[127] % 21], b2[b3[68] % 35] & 7)) & M32
    C = ((A & b0[10]) | (B & ~b0[10])) & M32
    D = (7 ^ (b4[b2[b3[36] % 35] % 21] << 1)) & M32
    b3[72] = ((C & 71) | (D & ~71)) & 0xFF
    b2[2] = (b2[2] + ((((b0[b3[20] % 20] << 1) & 159) | (b4[b1[190] % 21] & ~159)) & ((((b4[b3[64] % 21] & 110) | (b0[b1[25] % 20] & ~110)) & ~150) | (b1[25] & 150)))) & 0xFF
    b2[14] = (b2[14] - (((b2[b3[20] % 35] & (b3[72] ^ b2[b1[100] % 35])) & ~34) | (b1[97] & 34))) & 0xFF
    b0[17] = (115) & 0xFF
    b1[23] = (b1[23] ^ (((((((b4[b1[17] % 21] | b0[b3[20] % 20]) & b3[72]) | (b4[b1[17] % 21] & b0[b3[20] % 20])) & (b1[50]//3)) | ((((b4[b1[17] % 21] | b0[b3[20] % 20]) & b3[72]) | (b4[b1[17] % 21] & b0[b3[20] % 20]) | (b1[50] // 3)) & 246)) << 1))) & 0xFF
    b0[13] = (((((((b0[b3[40] % 20] | b1[10]) & 82) | (b0[b3[40] % 20] & b1[10])) & 209) | ((b0[b1[39] % 20] << 1) & 46)) >> 1)) & 0xFF
    b2[33] = (b2[33] - (b1[113] & 9)) & 0xFF
    b2[28] = (b2[28] - (((((2 | (b1[110] & 222)) >> 1) & ~223) | (b3[20] & 223)))) & 0xFF
    J = (weird_rol8((V | Z), (U & 7))) & M32
    A = ((b2[16] & T) | (W & (~b2[16]))) & M32
    B = ((b1[33] & 17) | (X & ~17)) & M32
    E = (((Y | ((A+B) // 5)) & 147) | (Y & ((A+B) // 5))) & M32
    M = ((b3[40] & b4[((b3[8] + J + E) & 0xff) % 21]) | ((b3[40] | b4[((b3[8] + J + E) & 0xff) % 21]) & b2[23])) & M32
    b0[15] = ((((b4[b3[20] % 21] - 48) & (~b1[184])) | ((b4[b3[20] % 21] - 48) & 189) | (189 & ~b1[184])) & (M*M*M)) & 0xFF
    b2[22] = (b2[22] + (b1[183])) & 0xFF
    b3[76] = ((3 * b4[b1[1] % 21]) ^ b3[0]) & 0xFF
    A = (b2[((b3[8] + (J + E)) & 0xff) % 35]) & M32
    F = ((((b4[b1[178] % 21] & A) | ((b4[b1[178] % 21] | A) & 209)) * b0[b1[13] % 20]) * (b4[b1[26] % 21] >> 1)) & M32
    G = ((F + 0x733ffff9) * 198 - (((F + 0x733ffff9) * 396 + 212) & 212) + 85) & M32
    b3[80] = (b3[36] + (G ^ 148) + ((G ^ 107) << 1) - 127) & 0xFF
    b3[84] = (((b2[b3[64] % 35]) & 245) | (b2[b3[20] % 35] & 10)) & 0xFF
    A = (b0[b3[68] % 20] | 81) & M32
    b2[18] = (b2[18] - (((A*A*A) & ~b0[15]) | ((b3[80] // 15) & b0[15]))) & 0xFF
    b3[88] = (b3[8] + J + E - b0[b1[160] % 20] + (b4[b0[((b3[8] + J + E) & 255) % 20] % 21] // 3)) & 0xFF
    B = (((R ^ b3[72]) & ~198) | ((S * S) & 198)) & M32
    F = ((b4[b1[69] % 21] & b1[172]) | ((b4[b1[69] % 21] | b1[172] ) & ((b3[12] - B) + 77))) & M32
    b0[16] = (147 - ((b3[72] & ((F & 251) | 1)) | (((F & 250) | b3[72]) & 198))) & 0xFF
    C = ((b4[b1[168] % 21] & b0[b1[29] % 20] & 7) | ((b4[b1[168] % 21] | b0[b1[29] % 20]) & 6)) & M32
    F = ((b4[b1[155] % 21] & b1[105]) | ((b4[b1[155] % 21] | b1[105]) & 141)) & M32
    b0[3] = (b0[3] - (b4[weird_rol32(F, C) % 21])) & 0xFF
    b1[5] = (weird_ror8(b0[12], ((b0[b1[61] % 20] // 5) & 7)) ^ (((~b2[b3[84] % 35]) & 0xffffffff) // 5)) & 0xFF
    b1[198] = (b1[198] + (b1[3])) & 0xFF
    A = ((162 | b2[b3[64] % 35])) & M32
    b1[164] = (b1[164] + (((A*A)//5))) & 0xFF
    G = (weird_ror8(139, (b3[80] & 7))) & M32
    C = (((b4[b3[64] % 21] * b4[b3[64] % 21] * b4[b3[64] % 21]) & 95) | (b0[b3[40] % 20] & ~95)) & M32
    b3[92] = ((G & 12) | (b0[b3[20] % 20] & 12) | (G & b0[b3[20] % 20]) | C) & 0xFF
    b2[12] = (b2[12] + (((b1[103] & 32) | (b3[92] & ((b1[103] | 60))) | 16)//3)) & 0xFF
    b3[96] = (b1[143]) & 0xFF
    b3[100] = (27) & 0xFF
    b3[104] = ((((b3[40] & ~b2[8]) | (b1[35] & b2[8])) & b3[64]) ^ 119) & 0xFF
    b3[108] = (238 & ((((b3[40] & ~b2[8]) | (b1[35] & b2[8])) & b3[64]) << 1)) & 0xFF
    b3[112] = ((~b3[64] & (b3[84] // 3)) ^ 49) & 0xFF
    b3[116] = (98 & ((~b3[64] & (b3[84] // 3)) << 1)) & 0xFF
    A = ((b1[35] & b2[8]) | (b3[40] & ~b2[8])) & M32
    B = ((A & b3[64]) | (((b3[84] // 3) & ~b3[64]))) & M32
    b1[143] = (b3[96] - ((B & (86 + ((b1[172] & 64) >> 1))) | (((((b1[172] & 65) >> 1) ^ 86) | ((~b3[64] & (b3[84] // 3)) | (((b3[40] & ~b2[8]) | (b1[35] & b2[8])) & b3[64]))) & b3[100]))) & 0xFF
    b2[29] = (162) & 0xFF
    A = (((((b4[b3[88] % 21]) & 160) | (b0[b1[125] % 20] & 95)) >> 1)) & M32
    B = (b2[b1[149] % 35] ^ (b1[43] * b1[43])) & M32
    b0[15] = (b0[15] + ((B&A) | ((A|B) & 115))) & 0xFF
    b3[120] = (b3[64] - b0[b3[40] % 20]) & 0xFF
    b1[95] = (b4[b3[20] % 21]) & 0xFF
    A = (weird_ror8(b2[b3[80] % 35], (b2[b1[17] % 35] * b2[b1[17] % 35] * b2[b1[17] % 35]) & 7)) & M32
    b0[7] = (b0[7] - ((A*A))) & 0xFF
    b2[8] = (b2[8] - b1[184] + (b4[b1[202] % 21] * b4[b1[202] % 21] * b4[b1[202] % 21])) & 0xFF
    b0[16] = ((b2[b1[102] % 35] << 1) & 132) & 0xFF
    b3[124] = ((b4[b3[40] % 21] >> 1) ^ b3[68]) & 0xFF
    b0[7] = (b0[7] - ((b0[b1[191] % 20] - (((b4[b1[80] % 21] << 1) & ~177) | (b4[b4[b3[88] % 21] % 21] & 177))))) & 0xFF
    b0[6] = (b0[b1[119] % 20]) & 0xFF
    A = ((b4[b1[190] % 21] & ~209) | (b1[118] & 209)) & M32
    B = (b0[b3[120] % 20] * b0[b3[120] % 20]) & M32
    b0[12] = ((b0[b3[84] % 20] ^ (b2[b1[71] % 35] + b2[b1[15] % 35])) & ((A & B) | ((A | B) & 27))) & 0xFF
    B = ((b1[32] & b2[b3[88] % 35]) | ((b1[32] | b2[b3[88] % 35]) & 23)) & M32
    D = ((((b4[b1[57] % 21] * 231) & 169) | (B & 86))) & M32
    F = ((((b0[b1[82] % 20] & ~29) | (b4[b3[124] % 21] & 29)) & 190) | (b4[(D//5) % 21] & ~190)) & M32
    H = (b0[b3[40] % 20] * b0[b3[40] % 20] * b0[b3[40] % 20]) & M32
    K = ((H & b1[82]) | (H & 92) | (b1[82] & 92)) & M32
    b3[128] = (((F & K) | ((F | K) & 192)) ^ (D//5)) & 0xFF
    b2[25] = (b2[25] ^ (((b0[b3[120] % 20] << 1) * b1[5]) - (weird_rol8(b3[76], (b4[b3[124] % 21] & 7)) & (b3[20] + 110)))) & 0xFF


_I0_INDEX = (18, 22, 23, 0, 5, 19, 32, 31, 10, 21, 30)


def sap_hash(block_in, key_out):
    """Hash 64 bytes of block_in into the 16-byte bytearray key_out."""
    buffer0 = bytearray((0x96, 0x5F, 0xC6, 0x53, 0xF8, 0x46, 0xCC, 0x18, 0xDF, 0xBE, 0xB2, 0xF8,
                         0x38, 0xD7, 0xEC, 0x22, 0x03, 0xD1, 0x20, 0x8F))
    buffer2 = bytearray((0x43, 0x54, 0x62, 0x7A, 0x18, 0xC3, 0xD6, 0xB3, 0x9A, 0x56, 0xF6, 0x1C,
                         0x14, 0x3F, 0x0C, 0x1D, 0x3B, 0x36, 0x83, 0xB1, 0x39, 0x51, 0x4A, 0xAA,
                         0x09, 0x3E, 0xFE, 0x44, 0xAF, 0xDE, 0xC3, 0x20, 0x9D, 0x42, 0x3A))
    buffer3 = bytearray(132)
    buffer4 = bytearray((0xED, 0x25, 0xD1, 0xBB, 0xBC, 0x27, 0x9F, 0x02, 0xA2, 0xA9, 0x11, 0x00,
                         0x0C, 0xB3, 0x52, 0xC0, 0xBD, 0xE3, 0x1B, 0x49, 0xC7))
    # Byte i is the (3 - i % 4)th byte of little-endian word (i % 64) / 4.
    buffer1 = bytearray(block_in[((i % 64) & ~3) + 3 - (i % 4)] for i in range(210))
    for i in range(840):
        x = buffer1[((i - 155) & M32) % 210]
        y = buffer1[((i - 57) & M32) % 210]
        z = buffer1[((i - 13) & M32) % 210]
        w = buffer1[i % 210]
        buffer1[i % 210] = (rol8(y, 5) + (rol8(z, 3) ^ w) - rol8(x, 7)) & 0xFF

    garble(buffer0, buffer1, buffer2, buffer3, buffer4)

    for i in range(16):
        key_out[i] = 0xE1
    for i in range(11):
        if i == 3:
            key_out[i] = 0x3D
        else:
            key_out[i] = (key_out[i] + buffer3[_I0_INDEX[i] * 4]) & 0xFF
    for i in range(20):
        key_out[i % 16] ^= buffer0[i]
    for i in range(35):
        key_out[i % 16] ^= buffer2[i]
    for i in range(210):
        key_out[i % 16] ^= buffer1[i]
    for _ in range(16):
        for i in range(16):
            x = key_out[((i - 7) & M32) % 16]
            y = key_out[i % 16]
            z = key_out[((i - 37) & M32) % 16]
            w = key_out[((i - 177) & M32) % 16]
            key_out[i] = rol8(x, 1) ^ y ^ rol8(z, 6) ^ rol8(w, 5)


# ---------------------------------------------------------------------------
# playfair.c
# ---------------------------------------------------------------------------

def playfair_decrypt(message3, cipher_text):
    """Recover the 16-byte AES key.

    message3: the 164-byte second fp-setup request from the sender.
    cipher_text: the 72-byte "ekey" from the SETUP request.
    """
    if len(message3) != 164 or len(cipher_text) != 72:
        raise ValueError("playfair_decrypt needs a 164-byte message and a 72-byte key")
    chunk1 = cipher_text[16:32]
    chunk2 = cipher_text[56:72]
    sap_key = generate_session_key(DEFAULT_SAP, message3)
    schedule = generate_key_schedule(sap_key)
    block = _xor_key(chunk2, Z_KEY)
    cycle(block, schedule)
    key = bytearray(block[i] ^ chunk1[i] for i in range(16))
    key = _xor_key(key, X_KEY)
    key = _xor_key(key, Z_KEY)
    return bytes(key)
