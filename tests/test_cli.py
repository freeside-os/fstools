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


def test_pkg_checksum_local_file(tmp_path):
    sample = tmp_path / "sample.tar.gz"
    sample.write_bytes(b"freeside-test-payload")
    runner = CliRunner()
    result = runner.invoke(main, ["pkg", "checksum", str(sample)])
    assert result.exit_code == 0
    digest = result.output.strip()
    assert len(digest) == 64
    assert all(c in "0123456789abcdef" for c in digest)


def test_pkg_logs_extracts_errors(tmp_path, monkeypatch):
    build_dir = tmp_path / "build"
    build_dir.mkdir()
    log_file = build_dir / "zlib-20261113-000001.log"
    log_file.write_text(
        "checking gcc... ok\n"
        "src/foo.c:12:5: error: implicit declaration of function 'bar'\n"
        "make: *** [Makefile:10: foo.o] Error 1\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("FSTOOLS_BUILD_DIR", str(build_dir))
    runner = CliRunner()
    result = runner.invoke(main, ["pkg", "logs", "zlib", "--tail", "10"])
    assert result.exit_code == 0
    assert "Extracted Error Signatures" in result.output
    assert "implicit declaration of function 'bar'" in result.output


def test_pkg_audit_json(tmp_path, monkeypatch):
    pkgs_dir = tmp_path / "packages"
    pkg_dir = pkgs_dir / "demo"
    pkg_dir.mkdir(parents=True)
    (pkg_dir / "package.manifest").write_text(
        '[package]\nname = "demo"\nversion = "1.2.3"\ndescription = "demo"\ngroup = "extra"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("FSTOOLS_PACKAGES_DIR", str(pkgs_dir))
    import fstools.pkg as pkg_mod
    monkeypatch.setattr(
        pkg_mod,
        "query_osv_vulnerabilities",
        lambda queries, timeout=10: {"results": [{"vulns": [{"id": "CVE-2026-0001"}]}]},
    )

    runner = CliRunner()
    result = runner.invoke(main, ["pkg", "audit", "demo", "--json"])
    assert result.exit_code == 0
    assert "CVE-2026-0001" in result.output
    assert '"package": "demo"' in result.output
