"""Tests for per-crew ACP interface selection (``acp_interfaces`` + ``acp_interface``).

Covers the three things that decide whether a crew runs locally or on kiro-cli:
the parse (which entries are refused), the resolution order, and the capability
memberships an external harness must NOT inherit.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import unittest.mock
from pathlib import Path

import pytest

from kiro_crew import platform_compat
from kiro_crew.acp.types import (
    ACP_BACKEND_EXTERNAL,
    ACP_BACKEND_KAS,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_ACP_RUNTIME,
    ACP_BACKENDS_INTERNAL_SANDBOX,
    ACP_BACKENDS_KIRO_IDENTITY_STORE,
    ACP_BACKENDS_KNOWN,
    ACP_BACKENDS_SELECTABLE,
    ACP_BACKENDS_SESSION_SHARING,
    ACP_BACKENDS_STEER,
    ACP_INTERFACE_DEFAULT,
    ACP_INTERFACE_KIRO_CLI,
)
from kiro_crew.config.loader import KiroCrewConfig, resolve_acp_interface


def _cfg(tmp_path: Path, data: dict) -> KiroCrewConfig:
    """Load *data* through the real config entry point.

    ``KiroCrewConfig.load()`` with ``config_path`` patched, not a direct
    dataclass construction: ``load()`` is what parses and normalizes an
    operator's config.json, so it is the only path that exercises the
    ``acp_interfaces`` parse guards these tests are about.
    """
    data.setdefault("workspaces", {"default": {"dir": "workspace"}})
    data.setdefault("memory_stores", {"default": {}})
    p = tmp_path / "config.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    with unittest.mock.patch("kiro_crew.config.loader.config_path", return_value=p):
        return KiroCrewConfig.load()


def _fake_backend(tmp_path: Path, name: str = "shim") -> str:
    """Create a file this platform will accept as runnable.

    The suffix is platform-dependent on purpose: Windows has no execute bit, so
    ``platform_compat.is_executable_file`` decides runnability there by
    extension. An extensionless fixture would be rejected on Windows and the
    positive assertions would fail for a reason that has nothing to do with this
    feature.
    """
    exe = tmp_path / (f"{name}.cmd" if platform_compat.IS_WINDOWS else name)
    exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    return str(exe)


# ── parse ──────────────────────────────────────────────────────────────────


def test_interface_without_command_is_dropped(tmp_path):
    cfg = _cfg(tmp_path, {"acp_interfaces": {"broken": {"description": "no command"}}})
    assert "broken" not in cfg.acp_interfaces


def test_builtin_name_cannot_be_shadowed(tmp_path):
    """A redefined kiro-cli would silently move every unconfigured crew."""
    cfg = _cfg(
        tmp_path,
        {"acp_interfaces": {ACP_INTERFACE_KIRO_CLI: {"command": _fake_backend(tmp_path)}}},
    )
    assert ACP_INTERFACE_KIRO_CLI not in cfg.acp_interfaces


def test_interface_parses_command_args_and_env(tmp_path):
    exe = _fake_backend(tmp_path)
    cfg = _cfg(
        tmp_path,
        {
            "acp_interfaces": {
                "local": {
                    "command": exe,
                    "args": ["acp", "--agent", "{agent}"],
                    "env": {"LMSTUDIO_MODEL": "qwen"},
                }
            }
        },
    )
    iface = cfg.acp_interfaces["local"]
    assert iface.command == exe
    assert iface.args == ["acp", "--agent", "{agent}"]
    assert iface.env == {"LMSTUDIO_MODEL": "qwen"}


def test_junk_types_do_not_break_the_load(tmp_path):
    """config.json is hand-editable and agent-writable."""
    cfg = _cfg(
        tmp_path,
        {
            "acp_interfaces": {
                "junk": {"command": _fake_backend(tmp_path), "args": "not-a-list", "env": 7}
            }
        },
    )
    assert cfg.acp_interfaces["junk"].args == []
    assert cfg.acp_interfaces["junk"].env == {}


# ── resolution ─────────────────────────────────────────────────────────────


def test_unconfigured_install_resolves_to_kiro_cli(tmp_path):
    cfg = _cfg(tmp_path, {"agents": {"default": {"kiro_agent": "kirocrew"}}})
    got = resolve_acp_interface(cfg, "default")
    assert got.name == ACP_INTERFACE_DEFAULT
    assert got.backend == ACP_BACKEND_KIRO
    assert got.command == []


def test_crew_interface_wins_over_the_legacy_backend(tmp_path):
    exe = _fake_backend(tmp_path)
    cfg = _cfg(
        tmp_path,
        {
            "acp_interfaces": {"local": {"command": exe}},
            "agent": {"acp_backend": "kas"},
            "agents": {
                "night": {"kiro_agent": "kirocrew", "acp_interface": "local"},
                "day": {"kiro_agent": "kirocrew"},
            },
        },
    )
    night = resolve_acp_interface(cfg, "night", kiro_agent="kirocrew")
    day = resolve_acp_interface(cfg, "day", kiro_agent="kirocrew")
    assert night.backend == ACP_BACKEND_EXTERNAL
    assert day.backend == ACP_BACKEND_KAS
    # The point of the feature: two crews, one gateway, different harnesses.
    assert night.name != day.name


def test_there_is_no_global_interface_tier(tmp_path):
    """``agent.acp_interface`` is deliberately absent, and must stay absent.

    It was in an earlier revision of this PR, was never parsed (so a configured
    value was silently discarded and overwritten on the next save), and had no
    named consumer: the mixed-fleet case is served by the per-crew key and the
    all-crews case by ``agent.acp_backend``. This pins the subtraction so it is
    not reintroduced as "symmetry".
    """
    import dataclasses

    from kiro_crew.config.loader import AgentConfig

    assert "acp_interface" not in {f.name for f in dataclasses.fields(AgentConfig)}

    # An operator who sets it anyway is not silently obeyed by a half-wired tier.
    cfg = _cfg(
        tmp_path,
        {"agent": {"acp_interface": "local"}, "agents": {"c": {"kiro_agent": "kirocrew"}}},
    )
    assert resolve_acp_interface(cfg, "c").name == ACP_INTERFACE_DEFAULT


def test_default_args_follow_the_kiro_convention(tmp_path):
    exe = _fake_backend(tmp_path)
    cfg = _cfg(
        tmp_path,
        {
            "acp_interfaces": {"local": {"command": exe}},
            "agents": {"c": {"kiro_agent": "my-agent", "acp_interface": "local"}},
        },
    )
    got = resolve_acp_interface(cfg, "c", kiro_agent="my-agent")
    assert got.command == [exe, "acp", "--agent", "my-agent"]


def test_placeholders_are_substituted(tmp_path):
    exe = _fake_backend(tmp_path)
    cfg = _cfg(
        tmp_path,
        {
            "acp_interfaces": {
                "local": {"command": exe, "args": ["run", "-a", "{agent}", "-m", "{model}"]}
            },
            "agents": {"c": {"kiro_agent": "kirocrew", "acp_interface": "local"}},
        },
    )
    got = resolve_acp_interface(cfg, "c", kiro_agent="kirocrew", model="qwen")
    assert got.command == [exe, "run", "-a", "kirocrew", "-m", "qwen"]


def test_unknown_placeholder_passes_through_rather_than_failing(tmp_path):
    exe = _fake_backend(tmp_path)
    cfg = _cfg(
        tmp_path,
        {
            "acp_interfaces": {"local": {"command": exe, "args": ["--x", "{nope}"]}},
            "agents": {"c": {"kiro_agent": "kirocrew", "acp_interface": "local"}},
        },
    )
    got = resolve_acp_interface(cfg, "c", kiro_agent="kirocrew")
    assert got.command == [exe, "--x", "{nope}"]


def test_unknown_interface_degrades_to_default(tmp_path):
    """A typo in one crew's binding must not take the gateway down."""
    cfg = _cfg(tmp_path, {"agents": {"c": {"kiro_agent": "kirocrew", "acp_interface": "typo"}}})
    got = resolve_acp_interface(cfg, "c")
    assert got.name == ACP_INTERFACE_DEFAULT
    assert got.backend == ACP_BACKEND_KIRO


def test_legacy_global_acp_backend_still_honoured(tmp_path):
    """An install that persisted kas keeps running kas without reconfiguration."""
    cfg = _cfg(tmp_path, {"agent": {"acp_backend": "kas"}, "agents": {"c": {}}})
    assert resolve_acp_interface(cfg, "c").backend == ACP_BACKEND_KAS


def test_interface_env_is_carried(tmp_path):
    exe = _fake_backend(tmp_path)
    cfg = _cfg(
        tmp_path,
        {
            "acp_interfaces": {"local": {"command": exe, "env": {"A": "1"}}},
            "agents": {"c": {"acp_interface": "local"}},
        },
    )
    assert resolve_acp_interface(cfg, "c").env == {"A": "1"}


def test_roundtrip_preserves_the_binding(tmp_path):
    exe = _fake_backend(tmp_path)
    cfg = _cfg(
        tmp_path,
        {
            "acp_interfaces": {"local": {"command": exe}},
            "agents": {"c": {"kiro_agent": "kirocrew", "acp_interface": "local"}},
        },
    )
    again = _cfg(tmp_path, cfg.to_dict())
    assert again.agents["c"].acp_interface == "local"
    assert again.acp_interfaces["local"].command == exe


# ── capability memberships ─────────────────────────────────────────────────


def test_external_is_known_but_not_directly_selectable():
    """It is reached by declaring an interface, not by typing a backend id."""
    assert ACP_BACKEND_EXTERNAL in ACP_BACKENDS_KNOWN
    assert ACP_BACKEND_EXTERNAL not in ACP_BACKENDS_SELECTABLE


@pytest.mark.parametrize(
    "capability",
    [
        ACP_BACKENDS_INTERNAL_SANDBOX,
        ACP_BACKENDS_SESSION_SHARING,
        ACP_BACKENDS_STEER,
        ACP_BACKENDS_ACP_RUNTIME,
        ACP_BACKENDS_KIRO_IDENTITY_STORE,
    ],
)
def test_external_claims_no_capability(capability):
    """An operator-supplied harness has demonstrated none of these.

    INTERNAL_SANDBOX is the one that matters most: membership makes wrap_argv
    SKIP Crew's seatbelt, so including external here would silently unconfine
    every external harness.
    """
    assert ACP_BACKEND_EXTERNAL not in capability


def test_harness_identity_is_tested_positively():
    """harness-parity H5: no site may spell a harness as the absence of others.

    The `is_external_backend` predicate exists so the two effort call sites do
    not read `not kiro and not claude` — a pair of negations that is correct
    with three backends and then silently captures the fourth. The CI gate
    (`scripts/check_harness_parity.py`) only inspects lines a change ADDS, so
    without this test a later refactor could reintroduce the negation on a line
    the gate no longer considers new.
    """
    import inspect

    from kiro_crew.providers.acp import AcpProvider

    for method in (AcpProvider.change_effort, AcpProvider._apply_initial_effort):
        src = inspect.getsource(method)
        assert "not self.is_claude_backend" not in src, method.__qualname__
        assert "not self.is_acp_runtime_backend and not" not in src, method.__qualname__


# ── provider guard ─────────────────────────────────────────────────────────


def test_provider_refuses_external_without_a_command():
    from kiro_crew.providers.acp import AcpProvider

    with pytest.raises(ValueError, match="requires acp_command"):
        AcpProvider(acp_backend=ACP_BACKEND_EXTERNAL, acp_interface="local")


def test_launchable_command_check(tmp_path):
    """The pre-spawn guard, including the cases that differ per platform.

    The negative cases are chosen to hold on BOTH platforms rather than assuming
    POSIX: an extensionless file fails the Windows rule (no runnable extension)
    and the POSIX rule (no execute bit), so one assertion covers both. The
    execute bit is only meaningful on POSIX, so the "executable but empty" case
    is asserted through the size check, which is platform-independent.
    """
    from kiro_crew.kiro_prerequisite import _acp_executable_is_runnable

    def runnable(p: str) -> bool:
        return _acp_executable_is_runnable(p, platform_name=sys.platform)

    exe = _fake_backend(tmp_path)
    assert runnable(exe)

    # Not runnable anywhere: no execute bit (POSIX), no runnable suffix (Windows).
    plain = tmp_path / "not-exec"
    plain.write_text("x", encoding="utf-8")
    assert not runnable(str(plain))

    assert not runnable(str(tmp_path / "missing"))
    assert not runnable(str(tmp_path))  # a directory is not a command

    # Zero bytes is refused even when the platform would call it runnable.
    empty = tmp_path / ("empty.cmd" if platform_compat.IS_WINDOWS else "empty")
    empty.write_text("", encoding="utf-8")
    empty.chmod(empty.stat().st_mode | stat.S_IXUSR)
    assert not runnable(str(empty))


def test_spawn_asks_the_prerequisite_predicate_not_a_third_spelling():
    """One rule, one spelling.

    "May ACP launch this file?" is already answered by
    ``kiro_prerequisite._acp_executable_is_runnable``. The external branch must
    call it rather than restate it, because three spellings of one predicate is
    how they drift apart — and the Windows half is the half that drifts silently
    (there is no execute bit there, so a hand-rolled ``os.access(X_OK)`` answers
    True for a text file and the guard stops guarding without failing).
    """
    import inspect

    from kiro_crew.acp import client

    assert not hasattr(
        client, "_is_launchable_command"
    ), "the local predicate is back; call kiro_prerequisite._acp_executable_is_runnable"
    src = inspect.getsource(client.AcpClient._spawn)
    assert "_acp_executable_is_runnable" in src


def test_external_command_must_be_absolute(tmp_path):
    """A relative command is refused at load, not resolved twice.

    Validation happens in this process and the spawn happens with the session
    work dir as cwd, and that work dir is workspace-controlled — so a relative
    command could name one file when checked and another when executed. Refusing
    at parse time is what makes "the path checked is the path run" true.
    """
    cfg = _cfg(tmp_path, {"acp_interfaces": {"rel": {"command": "bin/harness"}}})
    assert "rel" not in cfg.acp_interfaces


def test_resolved_command_is_absolute_and_canonical(tmp_path):
    """The argv carries a realpath, so nothing re-resolves it against a cwd."""
    real = tmp_path / "real"
    real.mkdir()
    exe = _fake_backend(real)
    link = tmp_path / "link"
    link.symlink_to(real)
    via_link = str(link / Path(exe).name)

    cfg = _cfg(
        tmp_path,
        {
            "acp_interfaces": {"local": {"command": via_link}},
            "agents": {"c": {"kiro_agent": "kirocrew", "acp_interface": "local"}},
        },
    )
    got = resolve_acp_interface(cfg, "c", kiro_agent="kirocrew")
    assert os.path.isabs(got.command[0])
    assert got.command[0] == os.path.realpath(via_link)


def test_subagent_inherits_the_command_with_the_backend():
    """The backend id and its argv are one fact and must travel together.

    Without the command, a subagent of an external-backed parent inherits the
    backend id and nothing to launch, and dies in provider construction.
    """
    from kiro_crew.session import SessionManager

    class _Client:
        backend = ACP_BACKEND_EXTERNAL
        _acp_command = ["/opt/shim", "acp", "--agent", "kirocrew"]
        _sandbox_mode = "auto"
        _extra_env = {"A": "1"}

    class _Provider:
        _client = _Client()

    class _Manager:
        """Minimal stand-in: the method only needs get_provider to resolve."""

        @staticmethod
        def get_provider(_key: str) -> _Provider:
            return _Provider()

    kwargs = SessionManager._parent_runtime_kwargs(_Manager(), "parent-session")
    assert kwargs["acp_backend"] == ACP_BACKEND_EXTERNAL
    assert kwargs["acp_command"] == ["/opt/shim", "acp", "--agent", "kirocrew"]
