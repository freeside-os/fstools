# Freeside Developer and Builder Tools (fstools) targets
set dotenv-load := false

proj_dir := source_directory()
root_dir := proj_dir + "/.."
root_build := root_dir + "/build"
packages_dir := root_dir + "/packages"

# Compile the fstools host PEX zipapp and sync with the package spec manifest
build:
    @echo "=== [fstools] Syncing fstools Virtual Environment ==="
    uv sync --python 3 --reinstall-package freeside-fstools && uv pip install --reinstall-package freeside-fstools -e .

    @echo "=== [fstools] Building fstools Python ZipApp ==="
    mkdir -p "{{root_build}}"
    sudo chown "$(id -u):$(id -g)" "{{root_build}}" 2>/dev/null || true
    uvx pex \
        --venv-repository .venv \
        -c fstools \
        freeside-fstools \
        --python-shebang "/usr/bin/env python3" \
        -o "{{root_build}}/fstools"
    @echo "=== [fstools] Copying fstools to package directory ==="
    cp "{{root_build}}/fstools" "{{packages_dir}}/fstools/fstools"
    @echo "=== [fstools] Updating packages/fstools/package.manifest ==="
    @echo "New checksum: {{ sha256_file(packages_dir + "/fstools/fstools") }}"
    sed -i -E 's/value = "[a-f0-9]{64}"/value = "{{ sha256_file(packages_dir + "/fstools/fstools") }}"/g' "{{packages_dir}}/fstools/package.manifest"
    @echo "=== [fstools] Removing stale package tarballs ==="
    rm -f "{{root_build}}/packages/fstools-"*.tar.gz
    @echo "=== [fstools] Build completed successfully ==="

# Run fstools unit and integration tests
test:
    @echo "=== [fstools] Running fstools Tests ==="
    uv run --all-groups pytest

# Execute fstools locally in development mode
run *args="":
    uv run fstools {{args}}

# Clean fstools virtualenv and build artifacts
clean:
    @echo "=== [fstools] Cleaning virtualenv and build outputs ==="
    rm -rf .venv .pytest_cache dist
    rm -f "{{root_build}}/fstools"
