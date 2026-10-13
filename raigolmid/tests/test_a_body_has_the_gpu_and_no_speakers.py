"""A body renders on the GPU's render node, never its display node, and plays audio into
nothing (or a file) rather than onto the owner's speakers."""
from __future__ import annotations

from pathlib import Path

from raigolmid import compose
from raigolmid.definitions import Body


def _service(monkeypatch, dri: Path) -> dict:
    monkeypatch.setattr(compose, "DRI", dri)
    body = Body(id="game", name="game", directory=None, image="debian", dockerfile_name=None,
                build_target=None, context_name=None, working_copy=None, command=None,
                ports=(), runtime=None, shell=None, read_only=False, environment={})
    placement = compose.BodyPlacement(instance="game@tab-1", namespace_ref="container:a",
                                      working_copy=Path("/wc"), image="debian",
                                      user="1000:1000")
    (service,) = compose.render(body, placement, 1, "d")["services"].values()
    return service


def test_a_body_is_given_the_render_nodes_and_not_the_display_node(tmp_path, monkeypatch):
    dri = tmp_path / "dri"
    dri.mkdir()
    for node in ("card1", "renderD128", "renderD129"):
        (dri / node).touch()
    assert _service(monkeypatch, dri)["devices"] == [
        f"{dri}/renderD128:{dri}/renderD128:rw", f"{dri}/renderD129:{dri}/renderD129:rw"]


def test_a_machine_with_no_gpu_names_no_device(tmp_path, monkeypatch):
    assert "devices" not in _service(monkeypatch, tmp_path / "absent")


def test_a_bodys_default_audio_output_is_the_null_one(tmp_path, monkeypatch):
    service = _service(monkeypatch, tmp_path / "absent")
    assert f"{compose.BODY_ASOUND}:/etc/asound.conf:ro" in service["volumes"]
    conf = compose.BODY_ASOUND.read_text(encoding="utf-8")
    assert "slave.pcm { type null }" in conf and "RAIGOLMI_AUDIO_FILE" in conf
