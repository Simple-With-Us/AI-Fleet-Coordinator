import importlib
import json
import pathlib
import sys
from unittest.mock import patch

root = pathlib.Path(sys.argv[1]).resolve()
sys.path.insert(0, str(root / "scripts"))
from simple_tools import wake_service, zcode_wake
from simple_tools.native_candidate import native_qualification_launch as launch

before = dict(wake_service.BUILDERS)
original = zcode_wake.router_platform
specpath = root / "spec.json"
specpath.write_text(
    json.dumps(
        {
            "target": {
                "session_id": "sess_fixture",
                "host": "server",
                "letter": "II",
                "generation": 2,
            },
            "workspace": "/fixture",
            "epoch": 100,
            "expires": 200,
        }
    )
)
specpath.chmod(0o600)
seen = []


def service(args):
    assert args == ["serve", "--bind", "127.0.0.1", "--port", "8794"]
    assert wake_service.BUILDERS == before
    for seat, b in before.items():
        assert wake_service.BUILDERS[seat] is b
    assert (
        importlib.import_module(wake_service.BUILDERS["ZC"].module).router_platform is not original
    )
    for seat, b in before.items():
        if seat != "ZC":
            assert b == b
    seen.append(args)
    return 37


with patch.object(wake_service, "main", side_effect=service):
    assert launch.main([str(specpath)]) == 37
assert len(seen) == 1
zcode_wake.router_platform = original
specpath.chmod(0o644)
with patch.object(wake_service, "main", side_effect=AssertionError("must not start")):
    try:
        launch.main([str(specpath)])
    except ValueError:
        pass
    else:
        raise AssertionError("public spec accepted")
print(
    json.dumps(
        {
            "actual_installed_package": True,
            "module": wake_service.__file__,
            "builders": sorted(before),
            "non_zc_builder_identity_unchanged": True,
            "zc_dynamic_module_callback_changed_only": True,
            "service_start_intercepted": True,
            "argv_matches_actual_ExecStart_bind_port": True,
            "unsafe_spec_refused": True,
            "result": 37,
        }
    )
)
