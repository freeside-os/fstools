import os
import sys
import hashlib
from dataclasses import dataclass
import tomllib

@dataclass
class FstoolsConfig:
    packages_dir: str
    build_dir: str
    output_dir: str
    skip_ssl_packages: list[str]

def load_config() -> FstoolsConfig:
    # Defaults
    packages_dir = "packages"
    build_dir = "build"
    output_dir = "build/packages"
    skip_ssl_packages = ["ca-certificates"]

    # 1. Load from /etc/freeside/fstools.toml if present
    config_file = "/etc/freeside/fstools.toml"
    if os.path.exists(config_file):
        try:
            with open(config_file, "rb") as f:
                data = tomllib.load(f)
                if "packages_dir" in data:
                    packages_dir = data["packages_dir"]
                if "build_dir" in data:
                    build_dir = data["build_dir"]
                if "output_dir" in data:
                    output_dir = data["output_dir"]
                if "skip_ssl_packages" in data:
                    skip_ssl_packages = data["skip_ssl_packages"]
        except Exception as e:
            print(f"Warning: Failed to load {config_file}: {e}", file=sys.stderr)

    # 2. Environment Variable Overrides
    if "FSTOOLS_PACKAGES_DIR" in os.environ:
        packages_dir = os.environ["FSTOOLS_PACKAGES_DIR"]
    elif "STRAYLIGHT_PACKAGES_ROOT" in os.environ:
        packages_dir = os.environ["STRAYLIGHT_PACKAGES_ROOT"]

    if "FSTOOLS_BUILD_DIR" in os.environ:
        build_dir = os.environ["FSTOOLS_BUILD_DIR"]
    elif "STRAYLIGHT_BUILDER_ROOT" in os.environ:
        build_dir = os.environ["STRAYLIGHT_BUILDER_ROOT"]

    if "FSTOOLS_OUTPUT_DIR" in os.environ:
        output_dir = os.environ["FSTOOLS_OUTPUT_DIR"]
    elif "STRAYLIGHT_BUILDER_OUTPUT_ROOT" in os.environ:
        output_dir = os.environ["STRAYLIGHT_BUILDER_OUTPUT_ROOT"]

    if "FSTOOLS_SKIP_SSL_PACKAGES" in os.environ:
        skip_ssl_packages = [p.strip() for p in os.environ["FSTOOLS_SKIP_SSL_PACKAGES"].split(",") if p.strip()]

    return FstoolsConfig(
        packages_dir=os.path.abspath(packages_dir),
        build_dir=os.path.abspath(build_dir),
        output_dir=os.path.abspath(output_dir),
        skip_ssl_packages=skip_ssl_packages
    )

def compute_sha256(path: str) -> str:
    """Computes the SHA256 checksum of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
