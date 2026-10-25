import io
import math
from fractions import Fraction

import av
import numpy as np

from mirrorbridge.testing.fake_sender import make_test_frame


def make_mkv(seconds=2.0, width=320, height=240, fps=20) -> bytes:
    """A Matroska file with H.264 video and AAC audio, like a browser would send."""
    buf = io.BytesIO()
    out = av.open(buf, "w", format="matroska")
    vs = out.add_stream("libx264", rate=fps)
    vs.width, vs.height, vs.pix_fmt = width, height, "yuv420p"
    vs.codec_context.options = {"preset": "ultrafast", "tune": "zerolatency"}
    aus = out.add_stream("aac", rate=48000)
    aus.layout = "stereo"
    for i in range(int(seconds * fps)):
        frame = make_test_frame(width, height, i, fps).reformat(format="yuv420p")
        frame.pts = i
        frame.time_base = Fraction(1, fps)
        for pkt in vs.encode(frame):
            out.mux(pkt)
    samples = int(seconds * 48000)
    for start in range(0, samples, 1024):
        t = (np.arange(1024) + start) / 48000.0
        tone = (0.2 * np.sin(2 * math.pi * 330 * t)).astype(np.float32)
        frame = av.AudioFrame.from_ndarray(np.stack([tone, tone]), format="fltp", layout="stereo")
        frame.sample_rate = 48000
        frame.pts = start
        for pkt in aus.encode(frame):
            out.mux(pkt)
    for stream in (vs, aus):
        for pkt in stream.encode(None):
            out.mux(pkt)
    out.close()
    return buf.getvalue()


class StubController:
    """Just enough of core.controller.Controller for session objects."""

    def __init__(self, config):
        self.config = config
        self.sessions = {}
        self.ended = []

    def session_started(self, session):
        self.sessions[session.id] = session

    def session_ended(self, session):
        self.sessions.pop(session.id, None)
        self.ended.append(session)
