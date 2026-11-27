"""Explicit private-release launcher. Not used unless service selects this file."""

import importlib.util
import os
import sys
from pathlib import Path


def install(zcode, candidate, builder, spec):
    original = zcode.router_platform

    def qualified(env, config, **kwargs):
        # Callback builder's base must remain installed adapter, not recurse.
        candidate.router_platform = original
        platform, qualifier, driver, custody = builder.build(candidate, env, config, spec, **kwargs)
        return platform

    zcode.router_platform = qualified


def main(argv):
    if len(argv) != 1:
        raise ValueError("qualification-launch-arguments")
    here = Path(__file__).resolve()
    scripts = here.parents[2]
    sys.path.insert(0, str(scripts))
    from simple_tools import wake_service, zcode_wake
    from simple_tools.native_candidate import native_qualification_builder
    from simple_tools.native_candidate.native_custody import read_private

    spec = read_private(Path(argv[0]))
    if (
        not Path(argv[0]).is_absolute()
        or os.getuid() == 0
        or Path(argv[0]).resolve() != Path(argv[0]).absolute()
    ):
        raise ValueError("qualification-launch-spec")
    # Package context gives existing adapter registry/settings helpers, while its
    # only native effect is the explicitly supplied tested driver.
    name = "simple_tools._qualified_zcode_adapter"
    module_spec = importlib.util.spec_from_file_location(
        name, here.with_name("candidate-zcode_wake.py")
    )
    candidate = importlib.util.module_from_spec(module_spec)
    sys.modules[name] = candidate
    module_spec.loader.exec_module(candidate)
    install(zcode_wake, candidate, native_qualification_builder, spec)
    return wake_service.main(["serve", "--bind", "127.0.0.1", "--port", "8794"])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
