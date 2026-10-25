import json
from pathlib import Path

from mirrorbridge.airplay.fairplay import FairPlay, REPLY_MESSAGES
from mirrorbridge.airplay.playfair import playfair_decrypt

VECTORS = json.loads((Path(__file__).parent / "data" / "playfair_vectors.json").read_text())


def test_playfair_matches_reference_vectors():
    # Vectors were produced by the original playfair C implementation.
    assert len(VECTORS) >= 40
    for v in VECTORS:
        key = playfair_decrypt(bytes.fromhex(v["message3"]), bytes.fromhex(v["ekey"]))
        assert key.hex() == v["aeskey"]


def test_fp_setup_handshake():
    fp = FairPlay()
    first = bytes([0x46, 0x50, 0x4C, 0x59, 0x03, 0x01, 0x01, 0, 0, 0, 0, 0x04, 0x02, 0x00, 0x02, 0xBB])
    assert fp.setup(first) == REPLY_MESSAGES[2]
    assert not fp.ready
    v = VECTORS[0]
    message3 = bytes.fromhex(v["message3"])
    reply = fp.setup(message3)
    assert len(reply) == 32
    assert reply[:12] == bytes.fromhex("46504c590301040000000014")
    assert reply[12:] == message3[144:]
    assert fp.decrypt_key(bytes.fromhex(v["ekey"])).hex() == v["aeskey"]
