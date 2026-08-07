import os
import sys
import shutil
import tarfile
import subprocess
from pathlib import Path

import click

from fstools.initrd import find_package_archive
from fstools.pkg import resolve_dependencies


# ==============================================================================
# CLI Commands
# ==============================================================================

@click.group(name="sandbox")
def sandbox_group():
    """Freeside OS compiler sandbox assembly subcommands."""
    pass


@sandbox_group.command(name="build")
@click.option("--output", default=None, help="Output path for sandbox tarball (default: <build_dir>/sandbox-root.tgz)")
@click.pass_context
def build_sandbox_cmd(ctx, output):
    """Assemble compiler sandbox from pre-built base + builder package tarballs.

    Resolves all packages in the 'base' and 'builder' groups, validates that
    compiled tarballs exist for each, extracts them into a fresh staging
    directory, and archives the result as sandbox-root.tgz.

    This is a pure assembly step — no compilation occurs. If any required
    package tarball is missing, the command fails with a clear message.
    """
    cfg = ctx.obj["CONFIG"]
    output_path = os.path.abspath(output) if output else os.path.join(cfg.build_dir, "sandbox-root.tgz")

    # ── Step 1: Resolve base + builder package list ──────────────────────
    click.echo("=== Resolving sandbox package list (base + builder) ===")
    try:
        packages = resolve_dependencies({"base", "builder"}, cfg.packages_dir)
    except Exception as e:
        click.secho(f"Failed to resolve dependencies: {e}", fg="red", err=True)
        sys.exit(1)

    if not packages:
        click.secho("No packages resolved for base + builder groups.", fg="red", err=True)
        sys.exit(1)

    click.echo(f"  Resolved {len(packages)} packages")

    # ── Step 2: Locate and validate all tarballs ─────────────────────────
    missing = []
    tarballs = []
    for pkg in packages:
        tarball = find_package_archive(cfg.output_dir, pkg)
        if not tarball:
            missing.append(pkg)
        else:
            tarballs.append((pkg, tarball))

    if missing:
        click.secho(f"Missing pre-built tarballs for: {', '.join(missing)}", fg="red", err=True)
        click.echo("Run 'just pkg::build-group base' and 'just pkg::build-group builder' first.", err=True)
        sys.exit(1)

    # ── Step 3: Extract all tarballs into fresh staging directory ─────────
    sandbox_dir = os.path.join(cfg.build_dir, "sandbox")
    if os.path.exists(sandbox_dir):
        click.echo(f"  Removing existing sandbox directory: {sandbox_dir}")
        shutil.rmtree(sandbox_dir)
    os.makedirs(sandbox_dir)

    click.echo("=== Extracting packages into sandbox ===")
    for pkg, tarball in tarballs:
        click.echo(f"  [{pkg}] {tarball.name}")
        with tarfile.open(tarball, "r:gz") as tar:
            for member in tar.getmembers():
                # Skip package metadata — not part of the runtime rootfs
                if member.name == "meta" or member.name.startswith("meta/"):
                    continue
                tar.extract(member, path=sandbox_dir)

    # ── Step 3b: Create UsrMerge compatibility symlinks ────────────────────
    # Without these, dynamically linked binaries fail because the musl dynamic
    # linker at /lib/ld-musl-x86_64.so.1 can't be resolved.
    usrmerge_symlinks = {
        "bin": "usr/bin",
        "sbin": "usr/bin",
        "lib": "usr/lib",
        "lib64": "usr/lib",
    }
    click.echo("=== Creating UsrMerge compatibility symlinks ===")
    for link_name, target in usrmerge_symlinks.items():
        link_path = os.path.join(sandbox_dir, link_name)
        if os.path.exists(link_path) or os.path.islink(link_path):
            if os.path.isdir(link_path) and not os.path.islink(link_path):
                shutil.rmtree(link_path)
            else:
                os.remove(link_path)
        os.symlink(target, link_path)
        click.echo(f"  /{link_name} → {target}")

    # ── Step 4: Archive and cleanup ──────────────────────────────────────
    click.echo(f"=== Creating sandbox tarball: {output_path} ===")
    subprocess.run(
        ["tar", "-czf", output_path, "-C", sandbox_dir, "."],
        check=True
    )
    shutil.rmtree(sandbox_dir)

    size_mb = os.path.getsize(output_path) / (1024 * 1024)
    click.echo("================================================================")
    click.echo("Sandbox rootfs assembled successfully!")
    click.echo(f"  Tarball:   {output_path}")
    click.echo(f"  Size:      {size_mb:.1f} MB")
    click.echo(f"  Packages:  {len(tarballs)}")
    click.echo("================================================================")
