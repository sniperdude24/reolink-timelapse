"""convert_chunk skips non-key decoding only when it keeps keyframes only."""

import re
import subprocess
from pathlib import Path

import pytest

from reolink_timelapse import chunks


@pytest.fixture
def captured(monkeypatch, tmp_path):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        open(cmd[-1], "wb").close()  # the tmp output convert_chunk renames
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(chunks, "check_ffmpeg", lambda: "ffmpeg")
    monkeypatch.setattr(chunks.subprocess, "run", fake_run)
    return calls


def _convert(tmp_path, interval, **kwargs):
    chunk = tmp_path / "chunk_0001.ts"
    chunk.write_bytes(b"")
    chunks.convert_chunk(chunk, tmp_path / "segments", interval=interval,
                         output_fps=30, **kwargs)


def _skips_non_key(cmd):
    return any(cmd[i:i + 2] == ["-skip_frame", "nokey"] for i in range(len(cmd) - 1))


def test_keyframe_interval_skips_non_key_frames(tmp_path, captured):
    _convert(tmp_path, interval=30)
    assert _skips_non_key(captured[0])
    # An input option: it must come before the -i it applies to.
    cmd = captured[0]
    assert cmd.index("-skip_frame") < cmd.index("-i")


def test_threshold_interval_skips_non_key_frames(tmp_path, captured):
    _convert(tmp_path, interval=chunks.KEYFRAME_SNAP_MIN_INTERVAL)
    assert _skips_non_key(captured[0])


def test_short_interval_decodes_every_frame(tmp_path, captured):
    _convert(tmp_path, interval=1)
    assert not _skips_non_key(captured[0])


def test_skip_combines_with_hardware_decode(tmp_path, captured):
    _convert(tmp_path, interval=30, hw_decoder="hwaccel:drm")
    cmd = captured[0]
    assert _skips_non_key(cmd)
    assert cmd[cmd.index("-hwaccel") + 1] == "drm"


def test_vaapi_keeps_frames_on_gpu_until_after_select(tmp_path, captured, monkeypatch):
    monkeypatch.setattr(chunks, "intel_render_node", lambda: "/dev/dri/renderD129")
    _convert(tmp_path, interval=1, hw_decoder="vaapi", scale_width=1920)
    cmd = captured[0]
    assert cmd[cmd.index("-init_hw_device") + 1] == "vaapi=va:/dev/dri/renderD129"
    assert cmd[cmd.index("-hwaccel_output_format") + 1] == "vaapi"
    # Split on the graph's separators, not the escaped \, inside select.
    graph = re.split(r"(?<!\\),", cmd[cmd.index("-vf") + 1])
    names = [g.split("=")[0] for g in graph]
    # select on GPU surfaces, scale there, download, then the CPU graph --
    # and no second (software) scale.
    assert names[:4] == ["select", "scale_vaapi", "hwdownload", "format"]
    assert "w=1920" in graph[1] and "scale" not in names[4:]
    assert names.index("deflicker") > names.index("hwdownload")


def test_vaapi_without_scaling_still_normalises_on_gpu(tmp_path, captured, monkeypatch):
    monkeypatch.setattr(chunks, "intel_render_node", lambda: None)
    _convert(tmp_path, interval=1, hw_decoder="vaapi")
    cmd = captured[0]
    assert cmd[cmd.index("-init_hw_device") + 1] == "vaapi=va:/dev/dri/renderD128"
    assert "scale_vaapi=format=nv12" in cmd[cmd.index("-vf") + 1]


class _FakeConvert:
    """Stands in for convert_chunk: fails whenever `fails(hw, primer)`."""

    def __init__(self, fails):
        self.fails, self.calls = fails, []

    def __call__(self, chunk, segments_dir, *, hw_decoder, primer, **kwargs):
        self.calls.append((chunk.name, hw_decoder, primer is not None))
        if self.fails(chunk.name, hw_decoder, primer):
            raise RuntimeError("boom")
        out = Path(segments_dir) / f"{chunk.stem}_tl.mp4"
        out.write_bytes(b"")
        return out


def _renderer(tmp_path, monkeypatch, fails, names):
    chunks_dir = tmp_path / "chunks"
    chunks_dir.mkdir()
    for n in names:
        (chunks_dir / n).write_bytes(b"")
    seg = tmp_path / "seg"
    seg.mkdir()
    fake = _FakeConvert(fails)
    monkeypatch.setattr(chunks, "convert_chunk", fake)
    r = chunks.ChunkRenderer(chunks_dir, seg, interval=1, output_fps=60,
                             hw_decoder="vaapi", log=lambda m: None)
    return r, fake


def test_hw_retried_without_primer_before_software(tmp_path, monkeypatch):
    # The first chunk is fine; the second fails only when primed (a damaged
    # tail in the first) -- the GPU must still convert it.
    r, fake = _renderer(tmp_path, monkeypatch,
                        lambda name, hw, primer: name == "b.ts" and primer is not None,
                        ["a.ts", "b.ts"])
    assert r.process(None, include_newest=True) == 2
    assert fake.calls == [("a.ts", "vaapi", False), ("b.ts", "vaapi", True),
                          ("b.ts", "vaapi", False)]
    assert r.hw_decoder == "vaapi" and r.hw_failures == 0


def test_isolated_hw_failure_keeps_hw_decoder(tmp_path, monkeypatch):
    r, fake = _renderer(tmp_path, monkeypatch,
                        lambda name, hw, primer: name == "a.ts" and hw is not None,
                        ["a.ts", "b.ts"])
    assert r.process(None, include_newest=True) == 2
    assert fake.calls[1] == ("a.ts", None, False)  # software redo
    assert fake.calls[2][:2] == ("b.ts", "vaapi")  # hardware again next chunk
    assert r.hw_decoder == "vaapi" and r.hw_failures == 0


def test_hw_dropped_after_limit_consecutive_failures(tmp_path, monkeypatch):
    names = [f"{i}.ts" for i in range(chunks.HW_FAILURE_LIMIT + 1)]
    r, fake = _renderer(tmp_path, monkeypatch, lambda name, hw, primer: hw is not None, names)
    assert r.process(None, include_newest=True) == len(names)
    assert r.hw_decoder is None
    # The chunk after the limit goes straight to software.
    assert fake.calls[-1] == (names[-1], None, True)
