import struct
import time
from fractions import Fraction

import av
import numpy as np

from mirrorbridge.airplay.audio import AudioStream, seq_diff
from mirrorbridge.airplay.mirror import avcc_to_annexb, parse_h264_config, parse_h265_config
from mirrorbridge.media.audio import AudioDecoder, db_to_gain
from mirrorbridge.media.recorder import Recorder
from mirrorbridge.media.video import fit_size, frame_to_png
from mirrorbridge.testing.fake_sender import make_test_frame


def _encode_aac():
    enc = av.CodecContext.create("aac", "w")
    enc.sample_rate = 44100
    enc.layout = "stereo"
    enc.format = "fltp"
    enc.open()
    n = enc.frame_size
    packets = []
    for start in range(0, 44100, n):
        t = (np.arange(n) + start) / 44100.0
        tone = (0.3 * np.sin(2 * np.pi * 440 * t)).astype(np.float32)
        frame = av.AudioFrame.from_ndarray(np.stack([tone, tone]), format="fltp", layout="stereo")
        frame.sample_rate = 44100
        frame.pts = start
        packets += [bytes(p) for p in enc.encode(frame)]
    return packets


def test_aac_lc_decoder():
    dec = AudioDecoder("aac")
    total = 0
    for p in _encode_aac():
        pcm = dec.decode(p)
        if pcm is not None:
            assert pcm.dtype == np.int16 and pcm.shape[1] == 2
            total += len(pcm)
    assert total > 40000


def _alac_verbatim_frame(left, right):
    """An uncompressed ("escape") ALAC stereo frame without an explicit size."""
    bits = "001" + "0000" + "0" * 12 + "0" + "00" + "1"  # CPE, tag, unused, has_size, extra, escape
    for l, r in zip(left, right):
        bits += format(int(l) & 0xFFFF, "016b") + format(int(r) & 0xFFFF, "016b")
    bits += "111"  # end element
    bits += "0" * (-len(bits) % 8)
    return int(bits, 2).to_bytes(len(bits) // 8, "big")


def test_alac_decoder_with_airplay_cookie():
    # AirPlay never sends the ALAC configuration; the receiver assumes the
    # standard 352-sample, 16-bit stereo, 44.1 kHz "magic cookie".
    dec = AudioDecoder("alac")
    left = (np.sin(np.arange(352) / 7) * 12000).astype(np.int16)
    right = (np.cos(np.arange(352) / 5) * 9000).astype(np.int16)
    pcm = dec.decode(_alac_verbatim_frame(left, right))
    assert pcm.shape == (352, 2)
    assert np.array_equal(pcm[:, 0], left) and np.array_equal(pcm[:, 1], right)


def test_volume_mapping():
    assert db_to_gain(-144) == 0.0
    assert db_to_gain(0) == 1.0
    assert abs(db_to_gain(-20) - 0.1) < 1e-9


def test_fit_size():
    assert fit_size(1920, 1080, 960, 960) == (960, 540)
    w, h = fit_size(1179, 2556, 800, 600)
    assert h == 600 and w % 2 == 0 and w <= 800


def test_h264_config_and_annexb():
    sps, pps = b"\x67\x42\x00\x1f\xaa\xbb", b"\x68\xce\x3c\x80"
    payload = bytes([1, 0x42, 0, 0x1F, 0xFF, 0xE1]) + struct.pack(">H", len(sps)) + sps + b"\x01" \
        + struct.pack(">H", len(pps)) + pps
    assert parse_h264_config(payload) == b"\x00\x00\x00\x01" + sps + b"\x00\x00\x00\x01" + pps
    data = struct.pack(">I", 3) + b"\x65\x01\x02" + struct.pack(">I", 2) + b"\x41\x09"
    annexb, headers = avcc_to_annexb(data)
    assert annexb == b"\x00\x00\x00\x01\x65\x01\x02\x00\x00\x00\x01\x41\x09"
    assert headers == [0x65, 0x41]
    assert avcc_to_annexb(b"\x00\x00\x00\x09\x65") == (None, None)  # length overruns
    assert avcc_to_annexb(struct.pack(">I", 1) + b"\x85") == (None, None)  # forbidden bit


def test_h265_config():
    vps, sps, pps = b"\x40\x01\x0c", b"\x42\x01\x01\x01", b"\x44\x01\xc0"
    payload = bytearray(0x75)
    payload[4:8] = b"hvc1"
    for marker, unit in ((0xA0, vps), (0xA1, sps), (0xA2, pps)):
        payload += bytes([marker, 0x00, 0x01]) + struct.pack(">H", len(unit)) + unit
    sc = b"\x00\x00\x00\x01"
    assert parse_h265_config(bytes(payload)) == sc + vps + sc + sps + sc + pps


class _Collect:
    def __init__(self):
        self.frames = []

    def audio_frame(self, payload, ts):
        self.frames.append((payload, ts))

    def audio_flush(self):
        pass


def _rtp(seq, payload):
    return struct.pack(">BBHII", 0x80, 0x60, seq & 0xFFFF, seq * 480, 0) + payload


def test_rtp_reorder_dedup_and_gap():
    h = _Collect()
    a = AudioStream(bytes(16), bytes(16), 8, 480, h, "127.0.0.1")
    a.decrypt = lambda packet: packet[12:]
    for seq in (100, 101, 101, 103, 102, 102, 104):
        a._enqueue(_rtp(seq, b"x%d" % seq))
    assert [p for p, _ in h.frames] == [b"x100", b"x101", b"x102", b"x103", b"x104"]
    # A lost packet is skipped once the reorder window is exceeded.
    for seq in range(106, 106 + 20):
        a._enqueue(_rtp(seq, b"y"))
    assert len(h.frames) == 5 + 20
    assert a.lost == 1
    # Sequence numbers wrap around.
    assert seq_diff(2, 65534) == 4


def test_recorder_letterboxes_rotation(tmp_path):
    landscape = make_test_frame(320, 180, 0, 30).reformat(format="yuv420p")
    portrait = make_test_frame(180, 320, 0, 30).reformat(format="yuv420p")
    frames = [landscape]
    rec = Recorder(tmp_path / "out.mp4", lambda: frames[-1], fps=20)
    rec.start()
    t0 = time.monotonic()
    while time.monotonic() - t0 < 1.2:
        pcm = (np.sin(np.arange(441) / 5) * 5000).astype(np.int16)
        rec.feed_audio(np.stack([pcm, pcm], axis=1))
        if time.monotonic() - t0 > 0.6:
            frames.append(portrait)
        time.sleep(0.01)
    rec.stop()
    assert rec.error is None
    c = av.open(str(tmp_path / "out.mp4"))
    v = c.streams.video[0]
    assert (v.codec_context.width, v.codec_context.height) == (320, 180)
    assert len(c.streams.audio) == 1
    assert sum(1 for _ in c.decode(video=0)) >= 18


def test_png_screenshot():
    png = frame_to_png(make_test_frame(64, 48, 0, 30))
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
