from ai_monitor.core.config import load_config, Config, DEFAULTS


def test_load_config_uses_defaults_when_missing(tmp_path):
    cfg = load_config(tmp_path / "missing.toml")
    assert cfg.claude.enabled is True
    assert cfg.claude.plan_cap_5h == DEFAULTS["agents"]["claude"]["plan_cap_5h"]
    assert cfg.codex.enabled is True


def test_load_config_overrides_partial(tmp_path):
    f = tmp_path / "c.toml"
    f.write_text("""
[agents.claude]
plan_cap_5h = 999_999

[notifications]
enabled = false
""")
    cfg = load_config(f)
    assert cfg.claude.plan_cap_5h == 999_999
    assert cfg.notifications.enabled is False
    # Untouched fields keep defaults.
    assert cfg.claude.remote_refresh_s == DEFAULTS["agents"]["claude"]["remote_refresh_s"]


def test_load_config_malformed_falls_back_to_defaults(tmp_path):
    f = tmp_path / "broken.toml"
    f.write_text("this is = not valid TOML }")
    cfg = load_config(f)
    assert cfg.claude.enabled is True  # didn't crash


def test_load_config_ignores_unknown_keys(tmp_path):
    """Unknown TOML keys should not crash the loader."""
    f = tmp_path / "c.toml"
    f.write_text("""
[agents.claude]
plan_cap_5h = 5_000_000
future_setting = "something"

[agents.codex]
unknown_key = 42
""")
    cfg = load_config(f)
    # Should not raise; known fields populated, unknown silently ignored.
    assert cfg.claude.plan_cap_5h == 5_000_000
