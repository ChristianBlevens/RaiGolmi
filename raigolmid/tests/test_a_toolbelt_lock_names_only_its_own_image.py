"""A `toolbelt.lock` arrives with a downloaded toolbelt and any tab can write one, and its image
starts as root in a view: it is used only when it names what the packages resolve to."""
import pytest

from raigolmid.definitions import Toolbelt
from raigolmid.toolbelts import Closure, ToolbeltError, ToolbeltResolver

PACKAGES = ("libcap", "python3", "coreutils", "util-linux")


def _locked(tmp_path, image: str, method: str = "nixery") -> tuple[ToolbeltResolver, Toolbelt]:
    toolbelt = Toolbelt(id="t", name="t", directory=tmp_path, supports=(), capabilities=(),
                        packages=PACKAGES)
    resolver = ToolbeltResolver()
    resolver.write_lock(toolbelt, Closure(image=image, method=method, packages=PACKAGES,
                                          package_digest=toolbelt.package_digest))
    return resolver, toolbelt


@pytest.mark.parametrize("image", [
    "nixery.dev/shell/libcap/python3/coreutils/util-linux",
    "nixery.dev/shell/libcap/python3/coreutils/util-linux@sha256:" + "a" * 64,
])
def test_a_lock_naming_its_packages_is_used(tmp_path, image):
    resolver, toolbelt = _locked(tmp_path, image)
    assert resolver.resolve(toolbelt).image == image


@pytest.mark.parametrize("image,method", [
    ("docker.io/someone/anything:latest", "nixery"),
    ("nixery.dev/shell/libcap/python3/coreutils/util-linux/extra", "nixery"),
    ("docker.io/someone/anything:latest", "flake"),
])
def test_a_lock_naming_another_image_is_refused(tmp_path, image, method):
    resolver, toolbelt = _locked(tmp_path, image, method)
    with pytest.raises(ToolbeltError, match="not what its packages resolve to"):
        resolver.resolve(toolbelt)
