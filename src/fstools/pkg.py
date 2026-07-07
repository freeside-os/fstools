import os
import sys
import re
import shutil
import subprocess
import urllib.request
import urllib.error
import textwrap
import tomllib
import ssl
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Set
from datetime import datetime
import json
import click
from fstools.common import compute_sha256

VALID_GROUPS = {
    "base", 
    "builder", 
    "system", 
    "linux",
    "server", 
    "desktop", 
    "extra",
    "live-image"
}

# ==============================================================================
# Typed Manifest Dataclasses
# ==============================================================================

@dataclass
class ChecksumInfo:
    algorithm: str
    value: str

@dataclass
class SourceInfo:
    url: Optional[str] = None
    file: Optional[str] = None
    git: Optional[str] = None
    ref: str = "HEAD"
    checksum: Optional[ChecksumInfo] = None

@dataclass
class BuildInfo:
    dependencies: List[str] = field(default_factory=list)
    environment: Dict[str, str] = field(default_factory=dict)

@dataclass
class PackageInfo:
    name: str
    version: str
    description: str
    dependencies: List[str] = field(default_factory=list)
    group: Optional[str] = None

@dataclass
class PackageManifest:
    package: PackageInfo
    sources: List[SourceInfo] = field(default_factory=list)
    build: BuildInfo = field(default_factory=BuildInfo)

# ==============================================================================
# Helper Functions
# ==============================================================================

def load_manifest(manifest_path: str) -> PackageManifest:
    """Loads and parses a package.manifest file into structured dataclasses."""
    with open(manifest_path, "rb") as f:
        data = tomllib.load(f)
    
    pkg_data = data.get("package", {})
    package = PackageInfo(
        name=pkg_data.get("name"),
        version=str(pkg_data.get("version")),
        description=pkg_data.get("description", ""),
        dependencies=pkg_data.get("dependencies", []),
        group=pkg_data.get("group")
    )
    
    sources = []
    for src in data.get("sources", []):
        checksum = None
        if "checksum" in src:
            checksum = ChecksumInfo(
                algorithm=src["checksum"].get("algorithm"),
                value=src["checksum"].get("value")
            )
        sources.append(SourceInfo(
            url=src.get("url"),
            file=src.get("file"),
            git=src.get("git"),
            ref=src.get("ref", "HEAD"),
            checksum=checksum
        ))
        
    build_data = data.get("build", {})
    env_block = build_data.get("environment", build_data.get("env", {}))
    build = BuildInfo(
        dependencies=build_data.get("dependencies", []),
        environment={k: str(v) for k, v in env_block.items()}
    )
    
    return PackageManifest(package=package, sources=sources, build=build)


def load_all_manifests(packages_dir: str) -> Dict[str, PackageManifest]:
    """Loads all package manifests from the packages directory."""
    manifests = {}
    if not os.path.isdir(packages_dir):
        return manifests
    for entry in sorted(os.listdir(packages_dir)):
        manifest_path = os.path.join(packages_dir, entry, "package.manifest")
        if not os.path.isfile(manifest_path):
            continue
        manifests[entry] = load_manifest(manifest_path)
    return manifests


def load_all_manifests_safe(packages_dir: str) -> Dict[str, PackageManifest]:
    """Loads all package manifests, ignoring files that fail to parse."""
    manifests = {}
    if not os.path.isdir(packages_dir):
        return manifests
    for entry in sorted(os.listdir(packages_dir)):
        manifest_path = os.path.join(packages_dir, entry, "package.manifest")
        if not os.path.isfile(manifest_path):
            continue
        try:
            manifests[entry] = load_manifest(manifest_path)
        except Exception:
            pass
    return manifests


def create_readme_file(pkg_dir: str, pkg_name: str, version: str, upstream_ref: str, source_url: str, checksum: str):
    readme_path = os.path.join(pkg_dir, "README.md")
    readme_content = textwrap.dedent(f"""\
        # {pkg_name}
        
        Maintainer reference documentation for package version upgrades and security updates.
        
        | Field | Value |
        | :--- | :--- |
        | **Package Name** | {pkg_name} |
        | **Version** | {version} |
        | **Upstream Reference** | {upstream_ref} |
        | **Source URL** | {source_url} |
        | **Source Checksum (SHA256)** | {checksum} |
        
        ## Upgrade Notes
        <!-- Wintermute or maintainers will add valuable upgrade notes below -->
    """)
    with open(readme_path, "w", encoding="utf-8") as f:
        f.write(readme_content)
    print(f"Created file: {os.path.relpath(readme_path)}")

# ==============================================================================
# Operational Logic: RESOLVE
# ==============================================================================

def collect_deps(name: str, manifests: Dict[str, PackageManifest], visited: Set[str]):
    """Recursively collects transitive dependencies."""
    if name in visited or name not in manifests:
        return
    visited.add(name)
    data = manifests[name]
    runtime_deps = data.package.dependencies
    build_deps = data.build.dependencies
    for dep in set(runtime_deps + build_deps):
        collect_deps(dep, manifests, visited)


def resolve_dependencies(target_groups: Set[str], packages_dir: str) -> List[str]:
    """Returns a topologically sorted list of packages belonging to targeted groups."""
    manifests = load_all_manifests(packages_dir)
    
    target_pkgs = {
        name for name, data in manifests.items()
        if data.package.group in target_groups
    }

    all_needed = set()
    for name in target_pkgs:
        collect_deps(name, manifests, all_needed)

    in_degree = {name: 0 for name in all_needed}
    adj = {name: [] for name in all_needed}

    for name in all_needed:
        data = manifests[name]
        runtime_deps = data.package.dependencies
        build_deps = data.build.dependencies
        for dep in set(runtime_deps + build_deps):
            if dep in all_needed:
                adj[dep].append(name)
                in_degree[name] += 1

    from collections import deque
    queue = deque(sorted(n for n in all_needed if in_degree[n] == 0))
    ordered = []
    while queue:
        node = queue.popleft()
        ordered.append(node)
        for neighbour in sorted(adj[node]):
            in_degree[neighbour] -= 1
            if in_degree[neighbour] == 0:
                queue.append(neighbour)

    if len(ordered) != len(all_needed):
        raise Exception("Dependency cycle detected among packages")

    return ordered


def resolve_package_dependencies(pkg_name: str, packages_dir: str) -> List[str]:
    """Returns a topologically sorted list of packages needed to build pkg_name (including pkg_name itself)."""
    manifests = load_all_manifests(packages_dir)
    
    if pkg_name not in manifests:
        raise Exception(f"Package {pkg_name} not found in manifests")
        
    all_needed = set()
    collect_deps(pkg_name, manifests, all_needed)

    in_degree = {name: 0 for name in all_needed}
    adj = {name: [] for name in all_needed}

    for name in all_needed:
        data = manifests[name]
        runtime_deps = data.package.dependencies
        build_deps = data.build.dependencies
        for dep in set(runtime_deps + build_deps):
            if dep in all_needed:
                adj[dep].append(name)
                in_degree[name] += 1

    from collections import deque
    queue = deque(sorted(n for n in all_needed if in_degree[n] == 0))
    ordered = []
    while queue:
        node = queue.popleft()
        ordered.append(node)
        for neighbour in sorted(adj[node]):
            in_degree[neighbour] -= 1
            if in_degree[neighbour] == 0:
                queue.append(neighbour)

    if len(ordered) != len(all_needed):
        raise Exception("Dependency cycle detected among packages")

    return ordered


def resolve_group_dependencies(group_name: str, packages_dir: str) -> List[str]:
    """Returns a topologically sorted list of packages needed to build a group (including all transitive dependencies)."""
    manifests = load_all_manifests(packages_dir)
    
    target_pkgs = {
        name for name, data in manifests.items()
        if data.package.group == group_name
    }

    all_needed = set()
    for name in target_pkgs:
        collect_deps(name, manifests, all_needed)

    in_degree = {name: 0 for name in all_needed}
    adj = {name: [] for name in all_needed}

    for name in all_needed:
        data = manifests[name]
        runtime_deps = data.package.dependencies
        build_deps = data.build.dependencies
        for dep in set(runtime_deps + build_deps):
            if dep in all_needed:
                adj[dep].append(name)
                in_degree[name] += 1

    from collections import deque
    queue = deque(sorted(n for n in all_needed if in_degree[n] == 0))
    ordered = []
    while queue:
        node = queue.popleft()
        ordered.append(node)
        for neighbour in sorted(adj[node]):
            in_degree[neighbour] -= 1
            if in_degree[neighbour] == 0:
                queue.append(neighbour)

    if len(ordered) != len(all_needed):
        raise Exception("Dependency cycle detected among packages")

    return ordered

# ==============================================================================
# Operational Logic: VERIFY
# ==============================================================================

def check_dependency_cycle(pkg_name: str, manifests: dict) -> Optional[str]:
    """Checks if there is a dependency cycle starting from pkg_name."""
    if pkg_name not in manifests:
        return None
    
    visited = {}  # name -> state: 0=unvisited, 1=visiting, 2=visited
    path = []
    
    def dfs(name):
        if name not in manifests:
            return None
        visited[name] = 1
        path.append(name)
        
        data = manifests[name]
        deps = []
        if hasattr(data, 'package') and data.package:
            pkg_deps = getattr(data.package, 'dependencies', [])
            if isinstance(pkg_deps, list):
                for d in pkg_deps:
                    if isinstance(d, str):
                        deps.append(d)
            elif isinstance(pkg_deps, str):
                deps.append(pkg_deps)
                
        if hasattr(data, 'build') and data.build:
            build_deps = getattr(data.build, 'dependencies', [])
            if isinstance(build_deps, list):
                for d in build_deps:
                    if isinstance(d, str):
                        deps.append(d)
            elif isinstance(build_deps, str):
                deps.append(build_deps)
                
        for dep in sorted(set(deps)):
            if visited.get(dep) == 1:
                cycle_path = path[path.index(dep):] + [dep]
                return " -> ".join(cycle_path)
            elif visited.get(dep) != 2:
                cycle_str = dfs(dep)
                if cycle_str:
                    return cycle_str
                    
        path.pop()
        visited[name] = 2
        return None
        
    return dfs(pkg_name)


def verify_package(pkg_name: str, all_pkg_dirs: set, manifests: dict, packages_dir: str) -> List[str]:
    """Validates a package directory structure and manifests, returning a list of error strings."""
    errors = []
    pkg_dir = os.path.join(packages_dir, pkg_name)
    
    if not os.path.isdir(pkg_dir):
        return [f"Package directory '{pkg_dir}' does not exist."]
        
    manifest_path = os.path.join(pkg_dir, "package.manifest")
    justfile_path = os.path.join(pkg_dir, "package.justfile")
    
    if not os.path.isfile(manifest_path):
        errors.append("package.manifest is missing.")
    if not os.path.isfile(justfile_path):
        errors.append("package.justfile is missing.")
        
    if not os.path.isfile(manifest_path):
        return errors

    data = None
    try:
        with open(manifest_path, "rb") as f:
            data = tomllib.load(f)
    except Exception as e:
        errors.append(f"package.manifest is not valid TOML: {e}")
        return errors

    misplaced_keys = ["CFLAGS", "CXXFLAGS", "LDFLAGS", "CONFIGURE_ARGS", "MAKE_FLAGS"]
    for key in misplaced_keys:
        if key in data:
            errors.append(f"Build environment variable '{key}' must be placed under [build.environment] or [build.env], not in the root table.")
    
    pkg_data = data.get("package")
    if pkg_data is None:
        errors.append("Missing [package] table.")
    elif not isinstance(pkg_data, dict):
        errors.append("[package] must be a table.")
    else:
        name = pkg_data.get("name")
        if not name:
            errors.append("[package].name is missing or empty.")
        elif not isinstance(name, str):
            errors.append("[package].name must be a string.")
        elif name != pkg_name:
            errors.append(f"[package].name '{name}' does not match the directory name '{pkg_name}'.")
            
        version = pkg_data.get("version")
        if version is None or str(version).strip() == "":
            errors.append("[package].version is missing or empty.")
            
        if "description" not in pkg_data:
            errors.append("[package].description is missing.")
        elif not isinstance(pkg_data.get("description"), str):
            errors.append("[package].description must be a string.")
            
        group = pkg_data.get("group")
        if group is None:
            errors.append("[package].group is missing.")
        elif not isinstance(group, str):
            errors.append("[package].group must be a string.")
        elif group not in VALID_GROUPS:
            errors.append(f"[package].group '{group}' is invalid. Allowed groups are: {', '.join(sorted(VALID_GROUPS))}")
            
        deps = pkg_data.get("dependencies")
        if deps is not None:
            if not isinstance(deps, list):
                errors.append("[package].dependencies must be a list of strings.")
            else:
                for dep in deps:
                    if not isinstance(dep, str):
                        errors.append(f"Dependency '{dep}' in [package].dependencies must be a string.")
                    elif dep not in all_pkg_dirs:
                        errors.append(f"Runtime dependency '{dep}' does not exist under '{packages_dir}'.")
                        
    if isinstance(pkg_data, dict):
        for key in misplaced_keys:
            if key in pkg_data:
                errors.append(f"Build environment variable '{key}' must be placed under [build.environment] or [build.env], not in [package] table.")

    build_data = data.get("build")
    if build_data is not None:
        if not isinstance(build_data, dict):
            errors.append("[build] must be a table.")
        else:
            build_deps = build_data.get("dependencies")
            if build_deps is not None:
                if not isinstance(build_deps, list):
                    errors.append("[build].dependencies must be a list of strings.")
                else:
                    for dep in build_deps:
                        if not isinstance(dep, str):
                            errors.append(f"Dependency '{dep}' in [build].dependencies must be a string.")
                        elif dep not in all_pkg_dirs:
                            errors.append(f"Build dependency '{dep}' does not exist under '{packages_dir}'.")
            
            for key in misplaced_keys:
                if key in build_data:
                    errors.append(f"Build environment variable '{key}' must be placed under [build.environment] or [build.env], not in [build] table.")

    sources = data.get("sources")
    if sources is not None:
        if not isinstance(sources, list):
            errors.append("'sources' must be an array of tables.")
        else:
            for idx, src in enumerate(sources):
                if not isinstance(src, dict):
                    errors.append(f"Source at index {idx} must be a table.")
                    continue
                
                keys = [k for k in ["url", "file", "git"] if k in src]
                if len(keys) != 1:
                    errors.append(f"Source at index {idx} must specify exactly one of 'url', 'file', or 'git' (found: {', '.join(keys) if keys else 'none'}).")
                    continue
                
                src_type = keys[0]
                if src_type in ["url", "file"]:
                    checksum = src.get("checksum")
                    if checksum is None:
                        errors.append(f"Source at index {idx} ({src_type}) is missing a 'checksum' table.")
                    elif not isinstance(checksum, dict):
                        errors.append(f"Source at index {idx} has invalid 'checksum' (must be a table).")
                    else:
                        algo = checksum.get("algorithm")
                        val = checksum.get("value")
                        if algo != "sha256":
                            errors.append(f"Source at index {idx} has invalid checksum algorithm '{algo}' (only 'sha256' is supported).")
                        if not val:
                            errors.append(f"Source at index {idx} has missing or empty checksum value.")
                        elif not (isinstance(val, str) and len(val) == 64 and all(c in "0123456789abcdefABCDEF" for c in val)):
                            errors.append(f"Source at index {idx} has invalid SHA256 checksum value format.")
                    
                    if src_type == "file":
                        filename = src.get("file")
                        if isinstance(filename, str):
                            local_file_path = os.path.join(pkg_dir, filename)
                            if not os.path.isfile(local_file_path):
                                errors.append(f"Local file source '{filename}' does not exist at '{local_file_path}'.")
                
                elif src_type == "git":
                    if "checksum" in src:
                        errors.append(f"Source at index {idx} (git) should not have a 'checksum' table.")
                    ref = src.get("ref")
                    if ref is not None and not isinstance(ref, str):
                        errors.append(f"Source at index {idx} (git) has invalid 'ref' (must be a string).")

    if os.path.isfile(justfile_path):
        try:
            with open(justfile_path, "r", encoding="utf-8") as f:
                just_content = f.read()
            
            build_match = re.search(r"^build\b[^:]*:", just_content, re.MULTILINE)
            package_match = re.search(r"^package\b[^:]*:", just_content, re.MULTILINE)
            
            if not build_match:
                errors.append("package.justfile is missing the 'build' target.")
            if not package_match:
                errors.append("package.justfile is missing the 'package' target.")
        except Exception as e:
            errors.append(f"Failed to read package.justfile: {e}")

    cycle_str = check_dependency_cycle(pkg_name, manifests)
    if cycle_str:
        errors.append(f"Dependency cycle detected: {cycle_str}")

    return errors

# ==============================================================================
# Operational Logic: BUILD
# ==============================================================================

def generate_files_ledger(staging_dir: str) -> str:
    """Generates the meta/files.toml file registry list."""
    entries = []
    for root, _, files in os.walk(staging_dir):
        if os.path.relpath(root, staging_dir).startswith("meta"):
            continue
        for file in files:
            full_path = os.path.join(root, file)
            if os.path.islink(full_path):
                continue
            if not os.path.isfile(full_path):
                continue
            
            rel_path = os.path.relpath(full_path, staging_dir)
            path_str = f"./{rel_path}"
            sha256 = compute_sha256(full_path)
            stat_info = os.stat(full_path)
            size = stat_info.st_size
            mode = f"{stat_info.st_mode & 0o7777:04o}"
            uid = stat_info.st_uid
            gid = stat_info.st_gid
            
            entries.append(f"[[files]]\npath = \"{path_str}\"\nsha256 = \"{sha256}\"\nsize = {size}\nmode = \"{mode}\"\nuid = {uid}\ngid = {gid}\n")
    return "\n".join(entries)


def install_package(tarball_path: str, prefix: str = ""):
    """Installs a compiled package tarball into the container root /."""
    print(f"{prefix}Installing {os.path.basename(tarball_path)}")
    subprocess.run(["tar", "-xzf", tarball_path, "--exclude=meta/*", "-C", "/"], check=True)
    
    usr_sbin = "/usr/sbin"
    if os.path.exists(usr_sbin) and not os.path.islink(usr_sbin):
        usr_bin = "/usr/bin"
        for name in os.listdir(usr_sbin):
            src = os.path.join(usr_sbin, name)
            dst = os.path.join(usr_bin, name)
            if os.path.exists(dst):
                if os.path.islink(dst) or os.path.isfile(dst):
                    os.remove(dst)
                else:
                    shutil.rmtree(dst)
            shutil.move(src, dst)
        shutil.rmtree(usr_sbin)
        os.symlink("bin", usr_sbin)


def fetch_source_url(url: str, dest_dir: str, checksum_algo: Optional[str] = None, checksum_val: Optional[str] = None, prefix: str = "", skip_ssl: bool = False):
    """Downloads source from a URL and verifies checksum."""
    filename = url.split('/')[-1].split('?')[0]
    dest_path = os.path.join(dest_dir, filename)
    print(f"{prefix}  Downloading {url}...")
    
    if skip_ssl:
        print(f"{prefix} Disabling SSL check") 
        import ssl
        ctx = ssl._create_unverified_context()
        with urllib.request.urlopen(url, context=ctx) as response, open(dest_path, 'wb') as out_file:
            shutil.copyfileobj(response, out_file)
    else:
        urllib.request.urlretrieve(url, dest_path)
    
    if checksum_val and checksum_algo == "sha256":
        actual = compute_sha256(dest_path)
        if actual != checksum_val:
            os.remove(dest_path)
            raise Exception(f"Integrity check failed for {filename}! Expected SHA256: {checksum_val}, Got: {actual}")
        print(f"{prefix}  Checksum OK: {filename}")


def build_package_impl(pkg_name: str, packages_dir: str, build_dir: str, output_dir: str, keep_all_logs: bool = False, keep_sandbox: bool = False, current_idx: int = 1, total_count: int = 1):
    """Builds and packages a single package inside the container."""
    progress = f"[{current_idx}/{total_count}]"
    pkg_name_fmt = f"[{pkg_name}]"
    prefix = f"{progress:<10}{pkg_name_fmt:<20}"
    pkg_dir = os.path.join(packages_dir, pkg_name)
    manifest_path = os.path.join(pkg_dir, "package.manifest")
    
    if not os.path.isfile(manifest_path):
        raise Exception(f"Manifest not found for package {pkg_name} at {manifest_path}")
        
    manifest = load_manifest(manifest_path)
    pkg_version = manifest.package.version
    pkg_description = manifest.package.description
    pkg_group = manifest.package.group or ""
    pkg_dependencies = " ".join(manifest.package.dependencies)
    
    tarball_name = f"{pkg_name}-{pkg_version}-1.tar.gz"
    tarball_path = os.path.join(output_dir, tarball_name)
    
    if os.path.isfile(tarball_path):
        print(f"{prefix}Compiling... skipped")
        install_package(tarball_path, prefix)
        return True

    print(f"{prefix}Compiling {pkg_name}-{pkg_version} (group: {pkg_group})...")

    ws = os.path.join(build_dir, f"workspace/{pkg_name}-{pkg_version}")
    src_dir = f"{ws}/src"
    dest_dir = f"{ws}/dest"
    
    if os.path.exists(ws) and not keep_sandbox:
        shutil.rmtree(ws)
    os.makedirs(src_dir, exist_ok=True)
    os.makedirs(dest_dir, exist_ok=True)
    
    print(f"{prefix}Fetching sources...")
    for source in manifest.sources:
        if source.url:
            algo = source.checksum.algorithm if source.checksum else None
            val = source.checksum.value if source.checksum else None
            from fstools.common import load_config
            config = load_config()
            skip_ssl = pkg_name in config.skip_ssl_packages
            fetch_source_url(source.url, src_dir, algo, val, prefix, skip_ssl=skip_ssl)
        elif source.file:
            src_file_path = os.path.join(pkg_dir, source.file)
            if not os.path.exists(src_file_path):
                raise Exception(f"Local source file not found at {src_file_path}")
            print(f"{prefix}  Copying local file: {source.file}")
            shutil.copy(src_file_path, os.path.join(src_dir, source.file))
        elif source.git:
            git_url = source.git
            ref = source.ref
            print(f"{prefix}  Cloning git repo: {git_url} (ref: {ref})")
            subprocess.run(["git", "clone", "--depth=1", git_url, os.path.join(src_dir, pkg_name)], check=True)
            if ref != "HEAD":
                subprocess.run(["git", "-C", os.path.join(src_dir, pkg_name), "checkout", ref], check=True)

    shutil.copy(os.path.join(pkg_dir, "package.justfile"), os.path.join(ws, "package.justfile"))

    env = os.environ.copy()
    env["PKG_NAME"] = pkg_name
    env["PKG_VERSION"] = pkg_version
    env["PKG_DESCRIPTION"] = pkg_description
    env["PKG_DEPENDENCIES"] = pkg_dependencies
    env["PKG_GROUP"] = pkg_group
    env["DESTDIR"] = dest_dir
    
    for k, v in manifest.build.environment.items():
        env[k] = str(v)

    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = os.path.join(build_dir, f"{pkg_name}-{ts}.log")
    print(f"{prefix}Logging build output to {log_path}")

    build_success = False
    try:
        with open(log_path, "w", encoding="utf-8") as log_file:
            print(f"{prefix}Running: just build")
            subprocess.run(
                ["just", "-f", os.path.join(ws, "package.justfile"), "-d", src_dir, "build"], 
                env=env, 
                stdout=log_file,
                stderr=subprocess.STDOUT,
                check=True
            )
            print(f"{prefix}Running: just package")
            subprocess.run(
                ["just", "-f", os.path.join(ws, "package.justfile"), "-d", src_dir, "package"], 
                env=env, 
                stdout=log_file,
                stderr=subprocess.STDOUT,
                check=True
            )
        build_success = True
    except subprocess.CalledProcessError as e:
        print(f"{prefix}Build failed! See log at {log_path}", file=sys.stderr)
        raise e
    finally:
        if build_success and not keep_all_logs:
            try:
                os.remove(log_path)
            except OSError:
                pass

    staging_dir = os.path.join(ws, "staging")
    os.makedirs(os.path.join(staging_dir, "meta"), exist_ok=True)
    shutil.copy(manifest_path, os.path.join(staging_dir, "meta/package.manifest"))
    
    dest_usr = os.path.join(dest_dir, "usr")
    if os.path.exists(dest_usr):
        shutil.copytree(dest_usr, os.path.join(staging_dir, "usr"), symlinks=True)
        
    for entry in os.listdir(dest_dir):
        if entry == "usr":
            continue
        entry_path = os.path.join(dest_dir, entry)
        if os.path.isdir(entry_path):
            shutil.copytree(entry_path, os.path.join(staging_dir, entry), symlinks=True)
        else:
            shutil.copy(entry_path, os.path.join(staging_dir, entry))

    print(f"{prefix}Generating files.toml ledger...")
    ledger_content = generate_files_ledger(staging_dir)
    with open(os.path.join(staging_dir, "meta/files.toml"), "w", encoding="utf-8") as f:
        f.write(ledger_content)

    os.makedirs(output_dir, exist_ok=True)
    print(f"{prefix}Creating tarball: {tarball_name}")
    subprocess.run(["tar", "-czf", tarball_path, "-C", staging_dir, "."], check=True)

    install_package(tarball_path, prefix)

    if not keep_sandbox:
        shutil.rmtree(ws)
    print(f"{prefix}Compiling... done")
    return True

# ==============================================================================
# CLI Commands Definition
# ==============================================================================

@click.group(name="pkg")
def pkg_group():
    """Freeside Package Manager (fspack) subcommands."""
    pass


@pkg_group.command(name="resolve")
@click.option("--pkg", help="Resolve dependencies for a single package")
@click.option("--group", help="Resolve dependencies for a package group")
@click.argument("groups", nargs=-1)
@click.pass_context
def resolve_cmd(ctx, pkg, group, groups):
    """Resolve topologically sorted package order."""
    cfg = ctx.obj["CONFIG"]
    try:
        if pkg:
            ordered = resolve_package_dependencies(pkg, cfg.packages_dir)
        elif group:
            ordered = resolve_group_dependencies(group, cfg.packages_dir)
        elif groups:
            ordered = resolve_dependencies(set(groups), cfg.packages_dir)
        else:
            raise click.UsageError("Specify either --pkg <pkg_name>, --group <group_name>, or positional group names")
        for p in ordered:
            click.echo(p)
    except Exception as e:
        click.secho(f"Error: {e}", err=True, fg="red")
        sys.exit(1)


@pkg_group.command(name="convert")
@click.argument("pkgname")
def convert_cmd(pkgname):
    """Convert an Arch Linux PKGBUILD to Freeside format."""
    pkgname = pkgname.strip()
    url = f"https://gitlab.archlinux.org/archlinux/packaging/packages/{pkgname}/-/raw/main/PKGBUILD"
    try:
        print(f"Fetching PKGBUILD from {url}...")
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req) as response:
            if response.status != 200:
                raise Exception(f"Unable to retrieve PKGBUILD. [HTTP Status Code: {response.status}]")
            pkgbuild_content = response.read().decode('utf-8')
            
        os.makedirs(pkgname, exist_ok=True)
        print(f"Created directory: ./{pkgname}/")
        
        with open(os.path.join(pkgname, "PKGBUILD"), 'w', encoding='utf-8') as f:
            f.write(pkgbuild_content)
        
        # Parse logic
        from fstools.pkg_parser import generate_freeside_package
        manifest, justfile, pkgver, source_url, checksum = generate_freeside_package(pkgbuild_content)
        with open(os.path.join(pkgname, "package.manifest"), 'w', encoding='utf-8') as f:
            f.write(manifest)
        with open(os.path.join(pkgname, "package.justfile"), 'w', encoding='utf-8') as f:
            f.write(justfile)
            
        create_readme_file(pkgname, pkgname, pkgver, url, source_url, checksum)
        click.echo("Conversion successful!")
    except Exception as e:
        click.secho(f"Error: {e}", err=True, fg="red")
        sys.exit(1)


@pkg_group.command(name="build")
@click.option("--pkg", help="Build a single package")
@click.option("--group", help="Build a package group")
@click.option("--with-deps", is_flag=True, help="Build package dependencies too")
@click.option("--keep-all-logs", is_flag=True, help="Keep build log files even on success")
@click.option("--keep-sandbox", is_flag=True, help="Keep the build sandbox and workspace directories")
@click.pass_context
def build_cmd(ctx, pkg, group, with_deps, keep_all_logs, keep_sandbox):
    """Build package(s) inside the container sandbox."""
    if os.getuid() != 0:
        click.secho("Error: build command must be run with root/sudo privileges.", err=True, fg="red")
        sys.exit(1)

    if group and with_deps:
        click.secho("Error: --with-deps is only supported with --pkg", err=True, fg="red")
        sys.exit(1)

    cfg = ctx.obj["CONFIG"]
    
    if pkg:
        try:
            if with_deps:
                print(f"Resolving build order for package: {pkg} and its dependencies...")
                ordered_packages = resolve_package_dependencies(pkg, cfg.packages_dir)
                print(f"Found {len(ordered_packages)} packages to build: {ordered_packages}")
                total = len(ordered_packages)
                for idx, p in enumerate(ordered_packages, 1):
                    build_package_impl(p, cfg.packages_dir, cfg.build_dir, cfg.output_dir,
                                       keep_all_logs=keep_all_logs, keep_sandbox=keep_sandbox,
                                       current_idx=idx, total_count=total)
                print(f"\nBuild Complete for: {pkg} and dependencies ✓")
            else:
                build_package_impl(pkg, cfg.packages_dir, cfg.build_dir, cfg.output_dir,
                                   keep_all_logs=keep_all_logs, keep_sandbox=keep_sandbox,
                                   current_idx=1, total_count=1)
        except Exception as e:
            click.secho(f"Build Failed: {e}", err=True, fg="red")
            sys.exit(1)
    elif group:
        try:
            print(f"Resolving build order for group: {group}...")
            ordered_packages = resolve_dependencies({group}, cfg.packages_dir)
            print(f"Found {len(ordered_packages)} packages in group: {ordered_packages}")
            total = len(ordered_packages)
            for idx, p in enumerate(ordered_packages, 1):
                build_package_impl(p, cfg.packages_dir, cfg.build_dir, cfg.output_dir,
                                   keep_all_logs=keep_all_logs, keep_sandbox=keep_sandbox,
                                   current_idx=idx, total_count=total)
            print(f"\nGroup Build Complete for: {group} ✓")
        except Exception as e:
            click.secho(f"Group Build Failed: {e}", err=True, fg="red")
            sys.exit(1)
    else:
        click.secho("Error: Specify either --pkg <pkg_name> or --group <group_name>", err=True, fg="red")
        sys.exit(1)


@pkg_group.command(name="info")
@click.argument("pkgname")
@click.option("--simple", is_flag=True, help="Override/supplement global --simple for raw info scripting")
@click.pass_context
def info_cmd(ctx, pkgname, simple):
    """Get metadata info for a package."""
    cfg = ctx.obj["CONFIG"]
    simple_mode = simple or ctx.obj.get("SIMPLE", False)
    pkg_dir = os.path.join(cfg.packages_dir, pkgname)
    manifest_path = os.path.join(pkg_dir, "package.manifest")
    
    if not os.path.isfile(manifest_path):
        click.secho(f"Error: Manifest not found for package {pkgname}", err=True, fg="red")
        sys.exit(1)
        
    try:
        manifest = load_manifest(manifest_path)
        if simple_mode:
            click.echo(manifest.package.name)
            click.echo(manifest.package.version)
            click.echo(manifest.package.group or "")
            click.echo(pkg_dir)
            click.echo(manifest.package.description)
            click.echo(" ".join(manifest.package.dependencies))
            
            for src in manifest.sources:
                src_dict = {}
                if src.url: src_dict["url"] = src.url
                if src.file: src_dict["file"] = src.file
                if src.git: src_dict["git"] = src.git
                if src.ref != "HEAD": src_dict["ref"] = src.ref
                if src.checksum:
                    src_dict["checksum"] = {
                        "algorithm": src.checksum.algorithm,
                        "value": src.checksum.value
                    }
                click.echo("SOURCE:" + json.dumps(src_dict))
                
            for k, v in manifest.build.environment.items():
                click.echo(f"ENV:{k}={v}")
        else:
            click.echo(f"Package:      {manifest.package.name}")
            click.echo(f"Version:      {manifest.package.version}")
            click.echo(f"Group:        {manifest.package.group or 'None'}")
            click.echo(f"Path:         {pkg_dir}")
            click.echo(f"Description:  {manifest.package.description}")
            click.echo(f"Dependencies: {', '.join(manifest.package.dependencies) if manifest.package.dependencies else 'None'}")
            
            if manifest.sources:
                click.echo("\nSources:")
                for src in manifest.sources:
                    if src.url:
                        checksum_str = f" [{src.checksum.algorithm}: {src.checksum.value}]" if src.checksum else ""
                        click.echo(f"  - {src.url}{checksum_str}")
                    elif src.file:
                        click.echo(f"  - Local File: {src.file}")
                    elif src.git:
                        click.echo(f"  - Git Repo: {src.git} (ref: {src.ref})")
            
            env_vars = {}
            env_vars["PKG_NAME"] = manifest.package.name
            env_vars["PKG_VERSION"] = manifest.package.version
            env_vars["PKG_DESCRIPTION"] = manifest.package.description
            env_vars["PKG_GROUP"] = manifest.package.group or ""
            env_vars["PKG_DEPENDENCIES"] = " ".join(manifest.package.dependencies)
            
            for k, v in manifest.build.environment.items():
                env_vars[k] = str(v)
                
            if env_vars:
                click.echo("\nEnvironment Variables:")
                for k, v in sorted(env_vars.items()):
                    click.echo(f"  - {k} = {v}")
    except Exception as e:
        click.secho(f"Error: {e}", err=True, fg="red")
        sys.exit(1)


@pkg_group.command(name="create")
@click.argument("pkgname")
@click.option("--version", default="1.0.0", help="Version of the package (default: 1.0.0)")
@click.option("--description", default="", help="Description of the package")
@click.option("--group", default="extra", help="Group of the package (default: extra)")
@click.pass_context
def create_cmd(ctx, pkgname, version, description, group):
    """Create a new package skeleton."""
    cfg = ctx.obj["CONFIG"]
    pkg_dir = os.path.join(cfg.packages_dir, pkgname)
    
    if os.path.exists(pkg_dir):
        click.secho(f"Error: Directory for package '{pkgname}' already exists at {pkg_dir}.", err=True, fg="red")
        sys.exit(1)
        
    try:
        os.makedirs(pkg_dir)
        
        manifest_path = os.path.join(pkg_dir, "package.manifest")
        manifest_content = textwrap.dedent(f"""\
            [package]
            name = "{pkgname}"
            version = "{version}"
            description = "{description or f"Template package description for {pkgname}"}"
            dependencies = []
            group = "{group}"

            [[sources]]
            url = "https://example.com/{pkgname}-{version}.tar.gz"
            checksum = {{ algorithm = "sha256", value = "" }}

            [build]
            dependencies = []

            [build.environment]
            CONFIGURE_ARGS = "--prefix=/usr"
        """)
        with open(manifest_path, "w", encoding="utf-8") as f:
            f.write(manifest_content)
            
        justfile_path = os.path.join(pkg_dir, "package.justfile")
        justfile_content = textwrap.dedent("""\
            build:
                tar -xf $PKG_NAME-$PKG_VERSION.tar.gz
                cd $PKG_NAME-$PKG_VERSION && ./configure $CONFIGURE_ARGS && make -j$(nproc)

            package:
                cd $PKG_NAME-$PKG_VERSION && make DESTDIR="$DESTDIR" install
                find "$DESTDIR" -type d -exec chmod 755 {} +
                if [ -d "$DESTDIR/usr/lib" ]; then find "$DESTDIR/usr/lib" -name "*.so*" -exec chmod 755 {} + || true; fi
        """)
        with open(justfile_path, "w", encoding="utf-8") as f:
            f.write(justfile_content)
            
        create_readme_file(pkg_dir, pkgname, version, "None", f"https://example.com/{pkgname}-{version}.tar.gz", "")
            
        click.echo(f"Successfully created package skeleton for '{pkgname}' at {pkg_dir} ✓")
    except Exception as e:
        click.secho(f"Error creating package skeleton: {e}", err=True, fg="red")
        sys.exit(1)


@pkg_group.command(name="list")
@click.option("--group", help="List all packages in a given group")
@click.option("--groups", is_flag=True, help="List only the names of the groups")
@click.pass_context
def list_cmd(ctx, group, groups):
    """List package groups and packages."""
    cfg = ctx.obj["CONFIG"]
    try:
        manifests = load_all_manifests(cfg.packages_dir)
        
        g_map = {}
        for name, manifest in manifests.items():
            g = manifest.package.group
            if g:
                g_map.setdefault(g, []).append(name)
        
        if group:
            if group not in g_map:
                click.secho(f"Error: Group '{group}' not found", err=True, fg="red")
                sys.exit(1)
            for p in sorted(g_map[group]):
                version = manifests[p].package.version
                click.echo(f"{p:<25} ({version})")
        elif groups:
            for g in sorted(g_map.keys()):
                click.echo(g)
        else:
            for g in sorted(g_map.keys()):
                click.echo(f"{g}:")
                for p in sorted(g_map[g]):
                    version = manifests[p].package.version
                    click.echo(f"  {p:<25} ({version})")
    except Exception as e:
        click.secho(f"Error: {e}", err=True, fg="red")
        sys.exit(1)


@pkg_group.command(name="verify")
@click.argument("pkgname", required=False)
@click.option("--all", is_flag=True, help="Verify all packages in the repository")
@click.pass_context
def verify_cmd(ctx, pkgname, all):
    """Verify package validity."""
    if pkgname and all:
        click.secho("Error: Cannot specify both a package name and --all", err=True, fg="red")
        sys.exit(1)
    if not pkgname and not all:
        click.secho("Error: Specify either a package name or --all", err=True, fg="red")
        sys.exit(1)
        
    cfg = ctx.obj["CONFIG"]
    
    if all:
        pkg_names = []
        if os.path.isdir(cfg.packages_dir):
            for entry in sorted(os.listdir(cfg.packages_dir)):
                if os.path.isdir(os.path.join(cfg.packages_dir, entry)) and not entry.startswith("."):
                    pkg_names.append(entry)
        if not pkg_names:
            click.secho("No packages found to verify.", err=True, fg="red")
            sys.exit(0)
    else:
        pkg_names = [pkgname]
        
    manifests = load_all_manifests_safe(cfg.packages_dir)
    
    all_pkg_dirs = set()
    if os.path.isdir(cfg.packages_dir):
        for entry in os.listdir(cfg.packages_dir):
            if os.path.isdir(os.path.join(cfg.packages_dir, entry)) and not entry.startswith("."):
                all_pkg_dirs.add(entry)
                
    invalid_packages = 0
    total_packages = len(pkg_names)
    
    for p in pkg_names:
        errors = verify_package(p, all_pkg_dirs, manifests, cfg.packages_dir)
        if errors:
            invalid_packages += 1
            click.echo(f"✗ [{p}] Package is invalid:")
            for err in errors:
                click.echo(f"  - {err}")
        else:
            click.echo(f"✓ [{p}] Package is valid.")
            
    if invalid_packages > 0:
        click.secho(f"\nVerification failed: {invalid_packages}/{total_packages} package(s) are invalid.", err=True, fg="red")
        sys.exit(1)
    else:
        click.echo(f"\nVerification successful: {total_packages}/{total_packages} package(s) are valid.")
        sys.exit(0)
