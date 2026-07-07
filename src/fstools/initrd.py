import os
import sys
import tarfile
import gzip
import shutil
import stat
import click
from pathlib import Path
from typing import Optional

# ==============================================================================
# Constants
# ==============================================================================

LIVE_MOUNT_CONTENT = """[Unit]
Description=Mount Live Boot Medium
DefaultDependencies=no
After=udevadm-trigger-service.service

[Mount]
What=/dev/disk/by-label/FREESIDE_LIVE
Where=/run/initramfs/live
Type=iso9660
Options=ro,nofail
"""

SYSROOT_MOUNT_CONTENT = """[Unit]
Description=Mount Live Root Filesystem
DefaultDependencies=no
Requires=run-initramfs-live.mount
After=run-initramfs-live.mount

[Mount]
What=/run/initramfs/live/rootfs.squashfs
Where=/sysroot
Type=squashfs
Options=ro,loop
"""

REQUIRED_BOOT_PACKAGES = [
    "musl",
    "base-files",
    "systemd",
    "cryptsetup",
    "btrfs-progs",
    "util-linux",
    "kmod",
    "bash",
    "libcap",
    "openssl",
    "xz",
    "zstd",
    "zlib",
    "json-c"
]

PAYLOAD_EXCLUDE_FILTERS = [
    "/include/", "/share/man/", "/share/doc/", "/lib/pkgconfig/"
]

VFS_SKELETON_DIRECTORIES = [
    "proc", "sys", "run", "sysroot", "etc", "dev",
    "usr/bin", "usr/lib", "usr/share",
    "var", "var/log", "var/lib", "var/lib/udev",
    "dev/mqueue", "dev/hugepages",
    "sys/kernel/debug", "sys/kernel/config", "sys/kernel/tracing",
    "sys/fs/fuse/connections"
]

VFS_STICKY_DIRECTORIES = ["tmp", "var/tmp"]

VFS_COMPATIBILITY_SYMLINKS = {
    "bin": "usr/bin",
    "sbin": "usr/bin",
    "lib": "usr/lib",
    "lib64": "usr/lib",
    "usr/sbin": "bin",
    "etc/passwd": "../usr/share/freeside/etc/passwd",
    "etc/group": "../usr/share/freeside/etc/group",
    "etc/systemd/system/systemd-vconsole-setup.service": "/dev/null",
    "init": "usr/lib/systemd/systemd"
}

OOM_OVERRIDE_SERVICES = [
    "systemd-journald.service",
    "systemd-udevd.service"
]
OOM_OVERRIDE_CONTENT = "[Service]\nOOMScoreAdjust=\n"

# ==============================================================================
# CpioWriter Utility
# ==============================================================================

class CpioWriter:
    def __init__(self, output_file):
        self.output = output_file
        self.inode_counter = 1

    def _write_padded(self, header_data, body_data=b"", body_len=0, alignment=4):
        self.output.write(header_data)
        header_len = len(header_data)
        header_rem = header_len % alignment
        if header_rem != 0:
            self.output.write(b"\x00" * (alignment - header_rem))
            
        if body_len > 0:
            self.output.write(body_data)
            body_rem = body_len % alignment
            if body_rem != 0:
                self.output.write(b"\x00" * (alignment - body_rem))

    def write_record(self, name, mode, uid=0, gid=0, filesize=0, rdev_major=0, rdev_minor=0, data=b""):
        name_bytes = name.lstrip("/").encode("utf-8")
        namesize = len(name_bytes) + 1  # Includes trailing null byte

        header_meta = (
            b"070701" +
            f"{self.inode_counter:08x}".encode() +
            f"{mode:08x}".encode() +
            f"{uid:08x}".encode() +
            f"{gid:08x}".encode() +
            b"00000001" +  # nlink
            b"00000000" +  # mtime
            f"{filesize:08x}".encode() +
            b"00000003" +  # devmajor
            b"00000001" +  # devminor
            f"{rdev_major:08x}".encode() +
            f"{rdev_minor:08x}".encode() +
            f"{namesize:08x}".encode() +
            b"00000000"    # check
        )

        self.inode_counter += 1
        header_data = header_meta + name_bytes + b"\x00"
        self._write_padded(header_data, body_data=data, body_len=filesize, alignment=4)

    def write_directory(self, name, mode=0o755):
        self.write_record(name, mode=stat.S_IFDIR | mode)

    def write_symlink(self, name, target):
        target_bytes = target.encode("utf-8")
        self.write_record(
            name=name,
            mode=stat.S_IFLNK | 0o777,
            filesize=len(target_bytes),
            data=target_bytes
        )

    def write_file(self, name, mode, data):
        self.write_record(
            name=name,
            mode=stat.S_IFREG | mode,
            filesize=len(data),
            data=data
        )

    def write_device_node(self, name, mode, major, minor):
        self.write_record(
            name=name,
            mode=mode,
            rdev_major=major,
            rdev_minor=minor
        )

    def write_trailer(self):
        self.write_record("TRAILER!!!", mode=0, filesize=0)

# ==============================================================================
# Helper routines
# ==============================================================================

def find_package_archive(packages_dir: str, package_name: str) -> Optional[Path]:
    p_dir = Path(packages_dir)
    if not p_dir.exists():
        return None
    
    matches = list(p_dir.glob(f"{package_name}-[0-9]*.tar.gz")) + list(p_dir.glob(f"{package_name}-[0-9]*.tgz"))
    if not matches:
        matches = list(p_dir.glob(f"{package_name}-*.tar.gz")) + list(p_dir.glob(f"{package_name}-*.tgz"))
        
    if not matches:
        return None
    return max(matches, key=lambda p: p.stat().st_mtime)


def extract_early_boot_payload(archive_path: Path, dest_dir: str):
    print(f"[*] Unpacking early boot files from {archive_path.name}...")
    with tarfile.open(archive_path, "r:gz") as tar:
        for member in tar.getmembers():
            if any(p in member.name for p in PAYLOAD_EXCLUDE_FILTERS):
                continue
            tar.extract(member, path=dest_dir)


def create_freeside_vfs_skeleton(staging_dir: str):
    print("[*] Creating Freeside VFS directory hierarchy...")
    for d in VFS_SKELETON_DIRECTORIES:
        os.makedirs(os.path.join(staging_dir, d), exist_ok=True)

    for tmp_dir in VFS_STICKY_DIRECTORIES:
        tmp_path = os.path.join(staging_dir, tmp_dir)
        os.makedirs(tmp_path, exist_ok=True)
        os.chmod(tmp_path, 0o1777)


def inject_boot_symlinks(staging_dir: str):
    print("[*] Injecting systemd un-merged /usr boot paths...")
    for link, target in VFS_COMPATIBILITY_SYMLINKS.items():
        link_path = os.path.join(staging_dir, link)
        os.makedirs(os.path.dirname(link_path), exist_ok=True)
        if os.path.exists(link_path) or os.path.islink(link_path):
            if os.path.isdir(link_path) and not os.path.islink(link_path):
                shutil.rmtree(link_path)
            else:
                os.remove(link_path)
        os.symlink(target, link_path)

    etc_dir = os.path.join(staging_dir, "etc")
    os.makedirs(etc_dir, exist_ok=True)
    
    with open(os.path.join(etc_dir, "initrd-release"), "w") as f:
        f.write("# Freeside OS Initrd Signaling Profile\n")

    os_rel = os.path.join(etc_dir, "os-release")
    if not os.path.exists(os_rel):
        with open(os_rel, "w") as f:
            f.write("NAME=\"Freeside OS\"\nID=freeside\nVERSION_ID=0.1.0\n")

    for svc in OOM_OVERRIDE_SERVICES:
        svc_dir = os.path.join(etc_dir, "systemd/system", f"{svc}.d")
        os.makedirs(svc_dir, exist_ok=True)
        with open(os.path.join(svc_dir, "override.conf"), "w") as f:
            f.write(OOM_OVERRIDE_CONTENT)

    systemd_system_dir = os.path.join(etc_dir, "systemd/system")
    os.makedirs(systemd_system_dir, exist_ok=True)
    
    with open(os.path.join(systemd_system_dir, "run-initramfs-live.mount"), "w") as f:
        f.write(LIVE_MOUNT_CONTENT)
        
    with open(os.path.join(systemd_system_dir, "sysroot.mount"), "w") as f:
        f.write(SYSROOT_MOUNT_CONTENT)
        
    wants_dir = os.path.join(systemd_system_dir, "initrd.target.wants")
    os.makedirs(wants_dir, exist_ok=True)
    
    symlink_path = os.path.join(wants_dir, "sysroot.mount")
    if os.path.exists(symlink_path) or os.path.islink(symlink_path):
        os.remove(symlink_path)
    os.symlink("../sysroot.mount", symlink_path)


def build_cpio_archive(staging_dir: str, output_path: str):
    print("[*] Packaging staging workspace into compressed newc CPIO archive...")
    with gzip.open(output_path, "wb", compresslevel=9) as gz_out:
        cpio = CpioWriter(gz_out)

        # Secure System Device Nodes
        cpio.write_device_node("dev/console", stat.S_IFCHR | 0o600, 5, 1)
        cpio.write_device_node("dev/null", stat.S_IFCHR | 0o666, 1, 3)
        cpio.write_directory("dev", mode=0o755)

        for root, dirs, files in os.walk(staging_dir):
            for d in dirs:
                full_path = os.path.join(root, d)
                rel_path = os.path.relpath(full_path, staging_dir)
                if rel_path in ["dev/console", "dev/null", "dev"]:
                    continue

                if os.path.islink(full_path):
                    cpio.write_symlink(rel_path, os.readlink(full_path))
                else:
                    dir_stat = os.stat(full_path)
                    cpio.write_directory(rel_path, mode=(dir_stat.st_mode & 0o7777))

            for f in files:
                full_path = os.path.join(root, f)
                rel_path = os.path.relpath(full_path, staging_dir)

                if os.path.islink(full_path):
                    cpio.write_symlink(rel_path, os.readlink(full_path))
                else:
                    file_stat = os.stat(full_path)
                    with open(full_path, "rb") as bin_file:
                        file_data = bin_file.read()
                    cpio.write_file(rel_path, mode=(file_stat.st_mode & 0o777), data=file_data)

        cpio.write_trailer()


def list_cpio_archive(filepath: str):
    print("=================================================================")
    print(f"[*] Listing archive: {filepath}")
    print("=================================================================")
    try:
        with gzip.open(filepath, "rb") as f:
            data = f.read()
    except Exception as e:
        click.secho(f"[-] ERROR: Failed to open/decompress archive: {e}", err=True, fg="red")
        sys.exit(1)

    offset = 0
    while offset < len(data):
        if offset + 110 > len(data):
            click.secho(f"[-] ERROR: Truncated header at offset {offset}", err=True, fg="red")
            break

        header = data[offset:offset+110]
        if not header.startswith(b"070701"):
            click.secho(f"[-] ERROR: Invalid magic at offset {offset}: {header[:6]}", err=True, fg="red")
            break

        try:
            mode = int(header[14:22], 16)
            filesize = int(header[54:62], 16)
            namesize = int(header[94:102], 16)
        except ValueError as e:
            click.secho(f"[-] ERROR: Corrupt header fields at offset {offset}: {e}", err=True, fg="red")
            break

        offset += 110

        if offset + namesize > len(data):
            click.secho(f"[-] ERROR: Truncated filename at offset {offset}", err=True, fg="red")
            break

        name_bytes = data[offset:offset+namesize]
        name = name_bytes.split(b"\x00")[0].decode("utf-8", errors="replace")

        name_pad = (4 - (110 + namesize) % 4) % 4
        offset += namesize + name_pad

        if offset + filesize > len(data):
            click.secho(f"[-] ERROR: Truncated file data at offset {offset}", err=True, fg="red")
            break

        filedata = data[offset:offset+filesize]
        file_pad = (4 - filesize % 4) % 4
        offset += filesize + file_pad

        if name == "TRAILER!!!":
            break

        if stat.S_ISLNK(mode):
            target = filedata.decode("utf-8", errors="replace")
            click.echo(f"l {name} -> {target}")
        elif stat.S_ISREG(mode):
            perms = oct(mode & 0o777)[2:]
            click.echo(f"- {name:<60} (size={filesize:<8}, perms={perms})")
        elif stat.S_ISDIR(mode):
            perms = oct(mode & 0o777)[2:]
            click.echo(f"d {name:<60} (perms={perms})")
        elif stat.S_ISCHR(mode):
            click.echo(f"c {name:<60}")
        elif stat.S_ISBLK(mode):
            click.echo(f"b {name:<60}")
        else:
            click.echo(f"? {name:<60} (mode={oct(mode)})")
    print("=================================================================")

# ==============================================================================
# CLI Commands
# ==============================================================================

@click.group(name="initrd")
def initrd_group():
    """Freeside OS Hermetic Initramfs builder subcommands."""
    pass


@initrd_group.command(name="build")
@click.option("--packages-dir", help="Directory containing pre-compiled packages")
@click.option("--output", help="Path where compressed initramfs archive should be built")
@click.option("--work-dir", help="Temporary directory for staging system")
@click.pass_context
def build_initrd_cmd(ctx, packages_dir, output, work_dir):
    """Assemble Freeside OS initramfs base image."""
    cfg = ctx.obj["CONFIG"]
    
    p_dir = os.path.abspath(packages_dir) if packages_dir else cfg.output_dir
    staging_dir = os.path.abspath(work_dir) if work_dir else os.path.join(cfg.build_dir, "initramfs-staging")
    out_path = os.path.abspath(output) if output else os.path.join(cfg.build_dir, "initramfs-base.cpio.gz")

    print("=================================================================")
    print("           Freeside OS: Initramfs Packaging Toolchain            ")
    print("=================================================================")
    print(f"[*] Package Dir:  {p_dir}")
    print(f"[*] Staging Dir:  {staging_dir}")
    print(f"[*] Output Arch:  {out_path}")

    if os.path.exists(staging_dir):
        shutil.rmtree(staging_dir)
    os.makedirs(staging_dir, exist_ok=True)

    try:
        create_freeside_vfs_skeleton(staging_dir)

        for pkg in REQUIRED_BOOT_PACKAGES:
            archive = find_package_archive(p_dir, pkg)
            if not archive:
                click.secho(f"[-] ERROR: Required boot dependency '{pkg}' was not found inside packages directory!", err=True, fg="red")
                sys.exit(1)
            extract_early_boot_payload(archive, staging_dir)

        inject_boot_symlinks(staging_dir)
        build_cpio_archive(staging_dir, out_path)
    finally:
        if os.path.exists(staging_dir):
            shutil.rmtree(staging_dir)

    print("[+] Success! Byte-reproducible initramfs base image written successfully.")
    print(f"    Target Location: {out_path}")
    print("=================================================================")


@initrd_group.command(name="list")
@click.argument("filepath")
def list_initrd_cmd(filepath):
    """Parses and lists the contents of a compressed newc CPIO archive."""
    list_cpio_archive(filepath)
