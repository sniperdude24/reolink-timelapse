"""Intel iGPU detection picks VAAPI only on Linux hosts with an i915/xe GPU."""

from reolink_timelapse import decode


def test_intel_linux_host_maps_to_vaapi(monkeypatch):
    monkeypatch.setattr(decode.sys, "platform", "linux")
    monkeypatch.setattr(decode.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(decode, "intel_render_node", lambda: "/dev/dri/renderD128")
    assert decode.decoder_map_for_platform() == {"h264": "vaapi", "hevc": "vaapi"}
    assert decode.hw_mechanism_name() == "Intel VAAPI"


def test_linux_without_intel_gpu_has_no_hw_family(monkeypatch):
    monkeypatch.setattr(decode.sys, "platform", "linux")
    monkeypatch.setattr(decode.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(decode, "intel_render_node", lambda: None)
    assert decode.decoder_map_for_platform() == {}


def test_vaapi_checked_against_hwaccels_listing(monkeypatch):
    monkeypatch.setattr(decode, "decoder_map_for_platform",
                        lambda: {"h264": "vaapi", "hevc": "vaapi"})
    seen = []

    class R:
        stdout = "Hardware acceleration methods:\nvdpau\nvaapi\n"

    def fake_run(cmd, **kwargs):
        seen.append(cmd[-1])
        return R()

    monkeypatch.setattr(decode.subprocess, "run", fake_run)
    assert decode.hw_decoders_available("ffmpeg") == {"vaapi"}
    assert seen == ["-hwaccels"]
