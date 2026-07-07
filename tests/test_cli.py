import os
from click.testing import CliRunner
import pytest
from fstools.cli import main
from fstools.common import load_config, FstoolsConfig

def test_cli_help():
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "Freeside OS Developer and Builder Tools" in result.output
    assert "pkg" in result.output
    assert "image" in result.output
    assert "initrd" in result.output

def test_cli_pkg_help():
    runner = CliRunner()
    result = runner.invoke(main, ["pkg", "--help"])
    assert result.exit_code == 0
    assert "resolve" in result.output
    assert "build" in result.output
    assert "verify" in result.output

def test_cli_image_help():
    runner = CliRunner()
    result = runner.invoke(main, ["image", "--help"])
    assert result.exit_code == 0
    assert "build" in result.output

def test_cli_initrd_help():
    runner = CliRunner()
    result = runner.invoke(main, ["initrd", "--help"])
    assert result.exit_code == 0
    assert "build" in result.output
    assert "list" in result.output

def test_config_defaults(monkeypatch):
    # Clear any potential env vars
    monkeypatch.delenv("FSTOOLS_PACKAGES_DIR", raising=False)
    monkeypatch.delenv("STRAYLIGHT_PACKAGES_ROOT", raising=False)
    monkeypatch.delenv("FSTOOLS_BUILD_DIR", raising=False)
    monkeypatch.delenv("STRAYLIGHT_BUILDER_ROOT", raising=False)
    monkeypatch.delenv("FSTOOLS_OUTPUT_DIR", raising=False)
    monkeypatch.delenv("STRAYLIGHT_BUILDER_OUTPUT_ROOT", raising=False)
    
    # Mock existence of /etc/freeside/fstools.toml to return False
    monkeypatch.setattr(os.path, "exists", lambda path: False)
    
    cfg = load_config()
    assert os.path.basename(cfg.packages_dir) == "packages"
    assert os.path.basename(cfg.build_dir) == "build"
    assert os.path.basename(cfg.output_dir) == "packages" # since parent is build/

def test_config_env_overrides(monkeypatch):
    monkeypatch.setenv("FSTOOLS_PACKAGES_DIR", "/tmp/overridden_pkgs")
    monkeypatch.setenv("FSTOOLS_BUILD_DIR", "/tmp/overridden_build")
    monkeypatch.setenv("FSTOOLS_OUTPUT_DIR", "/tmp/overridden_output")
    
    cfg = load_config()
    assert cfg.packages_dir == "/tmp/overridden_pkgs"
    assert cfg.build_dir == "/tmp/overridden_build"
    assert cfg.output_dir == "/tmp/overridden_output"

def test_config_straylight_fallbacks(monkeypatch):
    monkeypatch.delenv("FSTOOLS_PACKAGES_DIR", raising=False)
    monkeypatch.delenv("FSTOOLS_BUILD_DIR", raising=False)
    monkeypatch.delenv("FSTOOLS_OUTPUT_DIR", raising=False)
    monkeypatch.setenv("STRAYLIGHT_PACKAGES_ROOT", "/tmp/sl_pkgs")
    monkeypatch.setenv("STRAYLIGHT_BUILDER_ROOT", "/tmp/sl_build")
    monkeypatch.setenv("STRAYLIGHT_BUILDER_OUTPUT_ROOT", "/tmp/sl_output")
    
    cfg = load_config()
    assert cfg.packages_dir == "/tmp/sl_pkgs"
    assert cfg.build_dir == "/tmp/sl_build"
    assert cfg.output_dir == "/tmp/sl_output"
