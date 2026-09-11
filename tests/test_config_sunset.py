"""The `sunset:` config section round-trips and coexists with the rest."""

import pytest
import yaml

from reolink_timelapse.config import Camera, Config, SunsetJob


def _config(tmp_path, text=None):
    path = tmp_path / "config.yaml"
    if text is not None:
        path.write_text(text, encoding="utf-8")
    return Config(path=path)


def test_absent_section_means_no_jobs(tmp_path):
    c = _config(tmp_path, "cameras: {}\nrecordings: {}\n")
    assert c.sunset_jobs == {}


def test_save_and_reload_round_trip(tmp_path):
    c = _config(tmp_path)
    c.put_camera(Camera(name="Backyard", ip="1.2.3.4", user="u", password="p"))
    c.live_sessions_keep_days = 7
    c.stream_auth_user, c.stream_auth_pin = "dave", "0424"
    job = SunsetJob(camera_name="Backyard", latitude=36.088749, longitude=-83.819199,
                    timezone="America/New_York", pre_minutes=45, post_minutes=75, keep_days=3)
    c.put_sunset(job)
    c.save()

    raw = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert raw["sunset"] == {"Backyard": {
        "latitude": 36.088749, "longitude": -83.819199, "timezone": "America/New_York",
        "pre_minutes": 45, "post_minutes": 75, "keep_days": 3}}
    assert raw["live"]["sessions_keep_days"] == 7
    assert raw["stream"]["auth_pin"] == "0424"

    again = _config(tmp_path)
    assert again.sunset_jobs == {"Backyard": job}
    assert again.get_sunset("Backyard").is_configured
    assert again.live_sessions_keep_days == 7
    assert again.stream_auth == ("dave", "0424")


def test_unknown_keys_are_ignored_and_defaults_fill_in(tmp_path):
    c = _config(tmp_path, (
        "cameras: {}\nrecordings: {}\n"
        "sunset:\n  Backyard:\n    latitude: 1.0\n    longitude: 2.0\n"
        "    timezone: UTC\n    foo: bar\n"))
    job = c.sunset_jobs["Backyard"]
    assert (job.pre_minutes, job.post_minutes, job.keep_days) == (60, 60, 7)
    assert not hasattr(job, "foo")


def test_half_written_section_does_not_break_loading(tmp_path):
    c = _config(tmp_path, "cameras: {}\nrecordings: {}\nsunset:\n  Backyard:\n")
    assert not c.sunset_jobs["Backyard"].is_configured


def test_get_sunset_missing_is_a_helpful_exit(tmp_path):
    c = _config(tmp_path)
    with pytest.raises(SystemExit, match="sunset-config --camera Nope"):
        c.get_sunset("Nope")


def test_remove_camera_blocked_while_sunset_job_exists(tmp_path):
    c = _config(tmp_path)
    c.put_camera(Camera(name="Backyard", ip="1.2.3.4", user="u", password="p"))
    c.put_sunset(SunsetJob(camera_name="Backyard", latitude=1, longitude=2, timezone="UTC"))
    with pytest.raises(SystemExit, match="sunset job"):
        c.remove_camera("Backyard")
    del c.sunset_jobs["Backyard"]
    c.remove_camera("Backyard")
    assert c.cameras == {}


def test_empty_jobs_write_no_section(tmp_path):
    c = _config(tmp_path)
    c.save()
    raw = yaml.safe_load((tmp_path / "config.yaml").read_text(encoding="utf-8"))
    assert "sunset" not in raw
