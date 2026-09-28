"""An upload carries the files its build reads and nothing else, because
anything more is noise that can leak the user's private content."""
from __future__ import annotations

from pathlib import Path

import pytest

from raigolmid import layerfiles
from raigolmid.definitions import load_body, load_face, load_toolbelt



def upload_files(layer):
    return layerfiles.upload_choice(layer)[0]

def _write(root: Path, files: dict[str, str]) -> None:
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)


def test_a_body_sends_its_dockerfile_and_what_it_copies_filtered_as_docker_filters(tmp_path):
    body = tmp_path / "bodies" / "web"
    _write(body, {
        "body.toml": 'id = "web"\ndockerfile = "Dockerfile"\ndescription = "a site"\n',
        "Dockerfile": "FROM busybox\nCOPY app/ /app/\nCOPY *.conf /etc/\n",
        ".dockerignore": "**/*.log\napp/secret\n!app/secret/keep.txt\n",
        "app/index.html": "", "app/debug.log": "", "app/deep/x.log": "",
        "app/secret/token": "", "app/secret/keep.txt": "",
        "server.conf": "", "notes.md": "private", "LAYER.md": "", ".git/config": "",
    })

    sent = set(upload_files(load_body(body)))

    assert sent == {"body.toml", "Dockerfile", ".dockerignore", "app/index.html",
                    "app/secret/keep.txt", "server.conf", "LAYER.md"}


def test_a_dockerfile_specific_ignore_file_replaces_the_contexts(tmp_path):
    body = tmp_path / "web"
    _write(body, {
        "body.toml": 'id = "web"\ndockerfile = "Dockerfile"\n',
        "Dockerfile": "FROM busybox\nCOPY . /app\n",
        "Dockerfile.dockerignore": "*.md\n", ".dockerignore": "*.txt\n",
        "a.txt": "", "b.md": "",
    })

    sent = set(upload_files(load_body(body)))

    assert "a.txt" in sent and "b.md" not in sent


def test_a_body_that_builds_from_the_users_project_is_not_uploaded(tmp_path):
    project = tmp_path / "project"
    _write(project, {"Dockerfile": "FROM busybox\n"})
    body = tmp_path / "web"
    _write(body, {"body.toml": f'id = "web"\ndockerfile = "Dockerfile"\n'
                               f'working_copy = "{project}"\n'})

    with pytest.raises(layerfiles.LayerFilesError, match="their work"):
        upload_files(load_body(body))


def test_a_face_sends_its_config_dirs_and_its_compositor_and_a_toolbelt_its_lock(tmp_path):
    faces = tmp_path / "faces"
    _write(faces, {
        "calm/face.toml": 'id = "calm"\n[desktop]\ncompositor = "sway"\nconfig_dir = "desktop/"\n',
        "calm/desktop/sway.conf": "", "calm/LAYER.md": "", "calm/scratch.txt": "",
        "_compositors/sway/Containerfile": "FROM fedora\nCOPY entry.sh /\n",
        "_compositors/sway/entry.sh": "", "_compositors/sway/README": "",
    })
    toolbelt = tmp_path / "tb"
    _write(toolbelt, {"toolbelt.toml": 'id = "tb"\npackages = ["bash"]\n',
                      "toolbelt.lock": "{}", "LAYER.md": ""})

    assert set(upload_files(load_face(faces / "calm"))) == {
        "face.toml", "desktop/sway.conf", "_compositors/sway/Containerfile",
        "_compositors/sway/entry.sh", "LAYER.md"}
    assert set(upload_files(load_toolbelt(toolbelt))) == {
        "toolbelt.toml", "toolbelt.lock", "LAYER.md"}
    files, required, _ = layerfiles.upload_choice(load_toolbelt(toolbelt))
    assert "LAYER.md" in files and "LAYER.md" not in required


def test_what_the_user_unticks_is_left_out_now_and_next_time_but_not_what_a_build_reads(tmp_path):
    """A folder or a file the user leaves out is remembered in the layer."""
    faces = tmp_path / "faces"
    _write(faces, {
        "calm/face.toml": 'id = "calm"\n[desktop]\ncompositor = "sway"\nconfig_dir = "desktop/"\n',
        "calm/desktop/sway.conf": "", "calm/desktop/private/diary.txt": "",
        "calm/desktop/private/keys.txt": "",
        "_compositors/sway/Containerfile": "FROM fedora\nCOPY entry.sh /\n",
        "_compositors/sway/entry.sh": "",
    })
    face = load_face(faces / "calm")
    files, required, excluded = layerfiles.upload_choice(face)
    assert required == {"face.toml", "_compositors/sway/Containerfile",
                        "_compositors/sway/entry.sh"} and excluded == set()

    layerfiles.choose(face, {"desktop/private/diary.txt", "desktop/private/keys.txt"})
    assert layerfiles.upload_choice(face)[2] == {"desktop/private/diary.txt",
                                                 "desktop/private/keys.txt"}
    assert layerfiles.UPLOAD_IGNORE not in files, "the user's choice itself is never sent"

    with pytest.raises(layerfiles.LayerFilesError, match="build reads"):
        layerfiles.choose(face, {"_compositors/sway/entry.sh"})
    layerfiles.choose(face, set())
    assert not (faces / "calm" / layerfiles.UPLOAD_IGNORE).exists()
