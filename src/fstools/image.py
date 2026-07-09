import os
import sys
import tarfile
import tomllib
import shutil
import subprocess
import click
from typing import List, Optional
from fstools.pkg import resolve_package_dependencies, load_all_manifests, collect_deps

HOST_DEPENDENCIES = ["mkfs.vfat", "mmd", "mcopy", "mksquashfs", "xorriso", "dd"]
EXTRA_PACKAGES = ["fish"]

MOTD = """
----
Freeside OS

Welcome back to the grid, Cowboy.
To carve out the local storage fabric and flash the system into
your deck, execute the provisioning terminal:

   $ freeside-install


"""

USRMERGE_SYMLINKS = {
    "bin":       "usr/bin",
    "sbin":      "usr/bin",
    "lib":       "usr/lib",
    "lib64":     "usr/lib",
    "usr/lib64": "lib",
    "usr/sbin":  "bin",
}

RUNTIME_DIRECTORIES = [
    "tmp", "proc", "sys", "dev", "run", "var", "etc", "root", "home", "boot"
]

BOOT_OPTIONS = (
    "console=ttyS0,115200 "
    "earlyprintk=serial,ttyS0,115200 "
    "systemd.volatile=overlay"
)

KERNEL_PKG = "linux-kernel"
KERNEL_MEMBER = "usr/lib/freeside/linux/kernel/vmlinuz"

INITRAMFS_PKG = "base-initramfs"
INITRAMFS_MEMBER = "usr/lib/freeside/initramfs.cpio"

BOOTLOADER_PKG = "systemd"
BOOTLOADER_MEMBER = "usr/lib/systemd/boot/efi/systemd-bootx64.efi"

EFI_IMAGE_SIZE_MB = 70
EFI_FAT_TYPE = "16"
EFI_LABEL = "FSEFIBOOT"
ISO_VOLUME_LABEL = "FREESIDE_LIVE"

# ==============================================================================
# Helper routines
# ==============================================================================

def check_host_dependencies():
    missing = [cmd for cmd in HOST_DEPENDENCIES if shutil.which(cmd) is None]
    if missing:
        click.secho("[-] ERROR: Missing required host tools:", err=True, fg="red")
        for cmd in missing:
            click.echo(f"    - {cmd}", err=True)
        click.echo("\nInstall them with your package manager, e.g.:", err=True)
        click.echo("  pacman -S dosfstools mtools squashfs-tools libisoburn", err=True)
        sys.exit(1)


def resolve_packages(packages_dir: str, groups: List[str]) -> List[str]:
    manifests = load_all_manifests(packages_dir)
    target_pkgs = {
        name for name, data in manifests.items()
        if data.package.group in groups
    }

    for pkg in EXTRA_PACKAGES:
        if pkg in manifests:
            target_pkgs.add(pkg)
        else:
            print(f"[-] WARNING: EXTRA_PACKAGES member '{pkg}' not found in manifests, skipping")

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


def find_package_tarball(packages_dir: str, tarballs_dir: str, pkg_name: str) -> Optional[str]:
    manifest_path = os.path.join(packages_dir, pkg_name, "package.manifest")
    if not os.path.isfile(manifest_path):
        return None

    with open(manifest_path, "rb") as f:
        data = tomllib.load(f)

    version = str(data.get("package", {}).get("version", ""))
    if not version:
        return None

    tarball_name = f"{pkg_name}-{version}-1.tar.gz"
    tarball_path = os.path.join(tarballs_dir, tarball_name)

    if os.path.isfile(tarball_path):
        return tarball_path
    return None


def extract_package_tarball(tarball_path: str, dest_dir: str):
    with tarfile.open(tarball_path, "r:gz") as tar:
        for member in tar.getmembers():
            name = member.name.lstrip("./")
            if not name or name.startswith("meta/") or name == "meta":
                continue

            target_path = os.path.join(dest_dir, name)

            if member.isdir():
                if os.path.islink(target_path) and os.path.isdir(target_path):
                    continue
                os.makedirs(target_path, exist_ok=True)

            elif member.issym():
                os.makedirs(os.path.dirname(target_path), exist_ok=True)
                if os.path.islink(target_path) or os.path.exists(target_path):
                    os.remove(target_path)
                os.symlink(member.linkname, target_path)

            elif member.islnk():
                link_target = os.path.join(dest_dir, member.linkname.lstrip("./"))
                os.makedirs(os.path.dirname(target_path), exist_ok=True)
                if os.path.islink(target_path) or os.path.exists(target_path):
                    os.remove(target_path)
                os.link(link_target, target_path)

            elif member.isreg():
                os.makedirs(os.path.dirname(target_path), exist_ok=True)
                if os.path.islink(target_path) or os.path.exists(target_path):
                    os.remove(target_path)
                fileobj = tar.extractfile(member)
                if fileobj:
                    with open(target_path, "wb") as out:
                        shutil.copyfileobj(fileobj, out)
                    os.chmod(target_path, member.mode & 0o7777)


def extract_file_from_tarball(tarball_path: str, member_path: str, output_path: str) -> bool:
    with tarfile.open(tarball_path, "r:gz") as tar:
        for candidate in [member_path, "./" + member_path]:
            try:
                member = tar.getmember(candidate)
            except KeyError:
                continue

            fileobj = tar.extractfile(member)
            if fileobj:
                os.makedirs(os.path.dirname(output_path), exist_ok=True)
                with open(output_path, "wb") as out:
                    shutil.copyfileobj(fileobj, out)
                os.chmod(output_path, member.mode & 0o7777)
                return True
    return False


def create_usrmerge_layout(staging_dir: str):
    print("[*] Creating UsrMerge directory layout...")
    os.makedirs(os.path.join(staging_dir, "usr/bin"), exist_ok=True)
    os.makedirs(os.path.join(staging_dir, "usr/lib"), exist_ok=True)

    for link, target in USRMERGE_SYMLINKS.items():
        link_path = os.path.join(staging_dir, link)
        os.makedirs(os.path.dirname(link_path), exist_ok=True)
        if os.path.islink(link_path) or os.path.exists(link_path):
            if os.path.isdir(link_path) and not os.path.islink(link_path):
                shutil.rmtree(link_path)
            else:
                os.remove(link_path)
        os.symlink(target, link_path)


def create_runtime_directories(staging_dir: str):
    print("[*] Creating runtime directories...")
    for d in RUNTIME_DIRECTORIES:
        os.makedirs(os.path.join(staging_dir, d), exist_ok=True)
    os.chmod(os.path.join(staging_dir, "tmp"), 0o1777)


def fix_musl_dynamic_linker(staging_dir: str):
    linker_path = os.path.join(staging_dir, "usr/lib/ld-musl-x86_64.so.1")
    libc_path = os.path.join(staging_dir, "usr/lib/libc.so")

    if not os.path.exists(libc_path):
        print("[-] WARNING: libc.so not found in staging! Dynamic binaries will not execute.")
        return

    if os.path.islink(linker_path):
        current_target = os.readlink(linker_path)
        if current_target == "libc.so":
            print("[*] musl dynamic linker already points to relative libc.so")
            return

    print("[*] Fixing musl dynamic linker -> libc.so (relative symlink)")
    if os.path.islink(linker_path) or os.path.exists(linker_path):
        os.remove(linker_path)
    os.symlink("libc.so", linker_path)


def fix_permissions(staging_dir: str):
    print("[*] Normalizing filesystem permissions...")
    for root, dirs, _files in os.walk(staging_dir):
        for d in dirs:
            dpath = os.path.join(root, d)
            if not os.path.islink(dpath):
                os.chmod(dpath, 0o755)

    usr_bin = os.path.join(staging_dir, "usr/bin")
    if os.path.isdir(usr_bin):
        for f in os.listdir(usr_bin):
            fpath = os.path.join(usr_bin, f)
            if os.path.isfile(fpath) and not os.path.islink(fpath):
                os.chmod(fpath, 0o755)

    usr_lib = os.path.join(staging_dir, "usr/lib")
    if os.path.isdir(usr_lib):
        for root, _dirs, files in os.walk(usr_lib):
            for f in files:
                if ".so" in f:
                    fpath = os.path.join(root, f)
                    if os.path.isfile(fpath) and not os.path.islink(fpath):
                        os.chmod(fpath, 0o755)


def _process_sysusers(staging_dir: str, target_etc: str):
    sysusers_dir = os.path.join(staging_dir, "usr/lib/sysusers.d")
    if not os.path.isdir(sysusers_dir):
        print("[-] WARNING: /usr/lib/sysusers.d/ not found, skipping sysusers")
        return

    passwd_path = os.path.join(target_etc, "passwd")
    group_path = os.path.join(target_etc, "group")
    shadow_path = os.path.join(target_etc, "shadow")

    existing_users = set()
    existing_groups = set()

    if os.path.isfile(passwd_path):
        with open(passwd_path) as f:
            for line in f:
                if line.strip() and not line.startswith("#"):
                    existing_users.add(line.split(":")[0])

    if os.path.isfile(group_path):
        with open(group_path) as f:
            for line in f:
                if line.strip() and not line.startswith("#"):
                    existing_groups.add(line.split(":")[0])

    next_uid = 900
    next_gid = 900
    used_uids = set()
    used_gids = set()

    if os.path.isfile(passwd_path):
        with open(passwd_path) as f:
            for line in f:
                parts = line.strip().split(":")
                if len(parts) >= 3 and parts[2].isdigit():
                    used_uids.add(int(parts[2]))

    if os.path.isfile(group_path):
        with open(group_path) as f:
            for line in f:
                parts = line.strip().split(":")
                if len(parts) >= 3 and parts[2].isdigit():
                    used_gids.add(int(parts[2]))

    def alloc_uid():
        nonlocal next_uid
        while next_uid in used_uids:
            next_uid += 1
        uid = next_uid
        used_uids.add(uid)
        next_uid += 1
        return uid

    def alloc_gid():
        nonlocal next_gid
        while next_gid in used_gids:
            next_gid += 1
        gid = next_gid
        used_gids.add(gid)
        next_gid += 1
        return gid

    new_users = []
    new_groups = []
    new_shadows = []

    print("[*] Processing sysusers.d configs...")
    conf_files = sorted(f for f in os.listdir(sysusers_dir) if f.endswith(".conf"))
    for conf_name in conf_files:
        conf_path = os.path.join(sysusers_dir, conf_name)
        with open(conf_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = line.split()
                if len(parts) < 3:
                    continue

                entry_type = parts[0]
                name = parts[1]
                id_field = parts[2] if len(parts) > 2 else "-"
                gecos = parts[3].strip('"') if len(parts) > 3 else ""
                home = parts[4] if len(parts) > 4 else "-"

                if entry_type == "g" and name not in existing_groups:
                    if id_field != "-" and id_field.isdigit():
                        gid = int(id_field)
                        used_gids.add(gid)
                    else:
                        gid = alloc_gid()
                    new_groups.append(f"{name}:x:{gid}:")
                    existing_groups.add(name)

                elif entry_type == "u" and name not in existing_users:
                    if ":" in id_field:
                        uid_s, gid_s = id_field.split(":", 1)
                        uid = int(uid_s) if uid_s.isdigit() else alloc_uid()
                        gid = int(gid_s) if gid_s.isdigit() else uid
                    elif id_field != "-" and id_field.isdigit():
                        uid = int(id_field)
                        gid = uid
                    else:
                        uid = alloc_uid()
                        gid = uid

                    used_uids.add(uid)
                    used_gids.add(gid)

                    if name not in existing_groups:
                        new_groups.append(f"{name}:x:{gid}:")
                        existing_groups.add(name)

                    home_dir = home if home != "-" else "/"
                    shell = "/usr/bin/nologin"
                    new_users.append(f"{name}:x:{uid}:{gid}:{gecos}:{home_dir}:{shell}")
                    new_shadows.append(f"{name}:!:19000:0:99999:7:::")
                    existing_users.add(name)

    if new_groups:
        with open(group_path, "a") as f:
            for entry in new_groups:
                f.write(entry + "\n")
        print(f"    Added {len(new_groups)} groups: " + ", ".join(e.split(":")[0] for e in new_groups))

    if new_users:
        with open(passwd_path, "a") as f:
            for entry in new_users:
                f.write(entry + "\n")
        with open(shadow_path, "a") as f:
            for entry in new_shadows:
                f.write(entry + "\n")
        print(f"    Added {len(new_users)} users: " + ", ".join(e.split(":")[0] for e in new_users))


def _add_live_user(staging_dir: str, target_etc: str):
    passwd_path = os.path.join(target_etc, "passwd")
    group_path = os.path.join(target_etc, "group")
    shadow_path = os.path.join(target_etc, "shadow")

    print("[*] Adding live user 'case'...")

    if os.path.exists(os.path.join(staging_dir, "usr/bin/fish")):
        shell = "/usr/bin/fish"
    else:
        shell = "/usr/bin/bash"
        print("    (fish not found, using bash)")

    with open(passwd_path, "a") as f:
        f.write(f"case:x:1000:1000:Freeside Live User:/home/case:{shell}\n")

    with open(shadow_path, "a") as f:
        f.write("case::19000:0:99999:7:::\n")

    with open(group_path, "a") as f:
        f.write("case:x:1000:\n")

    with open(group_path) as f:
        lines = f.readlines()
    with open(group_path, "w") as f:
        for line in lines:
            if line.startswith("wheel:"):
                line = line.rstrip("\n")
                if line.endswith(":"):
                    line += "case\n"
                else:
                    line += ",case\n"
            f.write(line)

    home_dir = os.path.join(staging_dir, "home/case")
    os.makedirs(home_dir, exist_ok=True)
    os.chown(home_dir, 1000, 1000)

    shells_path = os.path.join(target_etc, "shells")
    if os.path.isfile(shells_path):
        with open(shells_path) as f:
            shells_content = f.read()
        if shell not in shells_content:
            with open(shells_path, "a") as f:
                f.write(shell + "\n")

    print(f"    case:1000 shell={shell} groups=case,wheel")

    # Autologin Dropin
    print("[*] Configuring serial console auto-login for 'case'...")
    dropin_dir = os.path.join(target_etc, "systemd/system/serial-getty@ttyS0.service.d")
    os.makedirs(dropin_dir, exist_ok=True)
    dropin_path = os.path.join(dropin_dir, "autologin.conf")
    with open(dropin_path, "w") as f:
        f.write("[Service]\n")
        f.write("ExecStart=\n")
        f.write(f"ExecStart=-{shell} --login\n")
        f.write("User=case\n")
        f.write("Group=case\n")
        f.write("Environment=HOME=/home/case\n")
        f.write("Environment=TERM=linux\n")
        f.write(f"Environment=SHELL={shell}\n")
        f.write("WorkingDirectory=/home/case\n")
        f.write("StandardInput=tty\n")
        f.write("StandardOutput=tty\n")
        f.write("StandardError=tty\n")
        f.write("TTYPath=/dev/ttyS0\n")
        f.write("TTYReset=yes\n")
        f.write("UtmpIdentifier=ttyS0\n")

    # Nopasswd polkit escalate
    print("[*] Configuring passwordless privilege escalation for wheel...")
    polkit_dir = os.path.join(staging_dir, "etc/polkit-1/rules.d")
    os.makedirs(polkit_dir, exist_ok=True)
    polkit_path = os.path.join(polkit_dir, "50-wheel-nopasswd.rules")
    with open(polkit_path, "w") as f:
        f.write('polkit.addRule(function(action, subject) {\n')
        f.write('    if (subject.isInGroup("wheel")) {\n')
        f.write('        return polkit.Result.YES;\n')
        f.write('    }\n')
        f.write('});\n')


def _configure_live_shell_env(staging_dir: str, target_etc: str):
    print("[*] Configuring live-only environment (hostname, motd, shell profile)...")

    with open(os.path.join(target_etc, "hostname"), "w") as f:
        f.write("freeside-live\n")

    with open(os.path.join(target_etc, "motd"), "w") as f:
        f.write(MOTD)

    # profile
    profile_path = os.path.join(target_etc, "profile")
    profile_content = ""
    if os.path.isfile(profile_path):
        with open(profile_path) as f:
            profile_content = f.read()

    profile_additions = """
# Live-only prompt and motd configuration
if [ -n "$BASH_VERSION" ]; then
    PS1='\\[\\e[01;32m\\]\\u@\\h\\[\\e[00m\\]:\\[\\e[01;34m\\]\\w\\[\\e[00m\\] \\$ '
fi

if [ -f /etc/motd ] && [ -n "$PS1" ]; then
    cat /etc/motd
fi
"""
    if profile_additions not in profile_content:
        with open(profile_path, "a") as f:
            f.write(profile_additions)

    # config.fish
    fish_config_dir = os.path.join(target_etc, "fish")
    os.makedirs(fish_config_dir, exist_ok=True)
    fish_config_path = os.path.join(fish_config_dir, "config.fish")

    fish_additions = """
# Live-only fish prompt and motd configuration
if status is-interactive
    # Show MOTD
    if test -f /etc/motd
        cat /etc/motd
    end

    # Colored fish prompt matching user@host:~ $
    function fish_prompt
        set -l color_user (set_color green)
        set -l color_host (set_color green)
        set -l color_cwd (set_color blue)
        set -l color_normal (set_color normal)

        echo -n -s "$color_user" $USER "$color_normal" @ "$color_host" (prompt_hostname) "$color_normal" : "$color_cwd" (prompt_pwd) "$color_normal" ' $ '
    end
end
"""
    fish_content = ""
    if os.path.isfile(fish_config_path):
        with open(fish_config_path) as f:
            fish_content = f.read()

    if fish_additions not in fish_content:
        with open(fish_config_path, "a") as f:
            f.write(fish_additions)


def provision_etc(staging_dir: str):
    stock_etc = os.path.join(staging_dir, "usr/share/freeside/etc")
    target_etc = os.path.join(staging_dir, "etc")

    if not os.path.isdir(stock_etc):
        print("[-] WARNING: /usr/share/freeside/etc/ not found in staging, skipping /etc provisioning")
        return

    print("[*] Provisioning /etc from stock defaults...")
    os.makedirs(target_etc, exist_ok=True)

    copied = 0
    for item in os.listdir(stock_etc):
        src = os.path.join(stock_etc, item)
        dst = os.path.join(target_etc, item)

        if os.path.isfile(src):
            shutil.copy2(src, dst)
            copied += 1
        elif os.path.isdir(src):
            if os.path.exists(dst):
                shutil.rmtree(dst)
            shutil.copytree(src, dst, symlinks=True)
            copied += 1

    print(f"[+] Provisioned {copied} items into /etc")

    # Mask etc.mount
    print("[*] Masking etc.mount (incompatible with live volatile overlay)...")
    mask_dir = os.path.join(target_etc, "systemd/system")
    os.makedirs(mask_dir, exist_ok=True)
    for unit in ["etc.mount", "systemd-firstboot.service"]:
        unit_path = os.path.join(mask_dir, unit)
        if not os.path.exists(unit_path):
            os.symlink("/dev/null", unit_path)

    # Dbus
    sockets_wants = os.path.join(target_etc, "systemd/system/sockets.target.wants")
    os.makedirs(sockets_wants, exist_ok=True)
    dbus_socket_src = "/usr/lib/systemd/system/dbus.socket"
    dbus_socket_link = os.path.join(sockets_wants, "dbus.socket")
    if not os.path.exists(dbus_socket_link):
        os.symlink(dbus_socket_src, dbus_socket_link)

    _process_sysusers(staging_dir, target_etc)
    _add_live_user(staging_dir, target_etc)
    _configure_live_shell_env(staging_dir, target_etc)


def stage_package_cache(staging_dir: str, packages_dir: str, tarballs_dir: str, build_dir: str, resolved_packages: List[str]):
    print("[*] Staging package cache for offline installation...")
    cache_dir = os.path.join(staging_dir, "var/cache/freeside")
    pkg_cache = os.path.join(cache_dir, "packages")
    os.makedirs(pkg_cache, exist_ok=True)

    sandbox_tgz = os.path.join(build_dir, "sandbox-root.tgz")
    if os.path.isfile(sandbox_tgz):
        shutil.copy2(sandbox_tgz, os.path.join(cache_dir, "sandbox-root.tgz"))
        print(f"    Cached sandbox-root.tgz")
    else:
        print("[-] WARNING: sandbox-root.tgz not found, skipping cache")

    cached = 0
    for pkg_name in resolved_packages:
        tarball = find_package_tarball(packages_dir, tarballs_dir, pkg_name)
        if tarball:
            shutil.copy2(tarball, pkg_cache)
            cached += 1
    print(f"    Cached {cached}/{len(resolved_packages)} package tarballs")


def create_offline_archives(packages_dir: str, tarballs_dir: str, build_dir: str, sq_staging: str):
    print("[*] Creating offline system archives (tar.zst) for installer...")
    images_dir = os.path.join(sq_staging, "usr/lib/freeside/images")
    os.makedirs(images_dir, exist_ok=True)

    for group in ["base", "system"]:
        archive_name = f"freeside-{group}-v1.tar.zst"
        archive_path = os.path.join(images_dir, archive_name)
        print(f"    Building {archive_name}...")

        # Resolve packages for this specific group
        resolved = resolve_packages(packages_dir, [group])

        # Stage files in a temporary directory
        group_staging = os.path.join(build_dir, f"staging-{group}")
        if os.path.exists(group_staging):
            shutil.rmtree(group_staging)
        os.makedirs(group_staging)

        try:
            # Extract all packages for this group
            for pkg_name in resolved:
                tarball = find_package_tarball(packages_dir, tarballs_dir, pkg_name)
                if tarball:
                    extract_package_tarball(tarball, group_staging)
            
            # Apply layout fixes similar to the live rootfs
            create_usrmerge_layout(group_staging)
            create_runtime_directories(group_staging)
            fix_permissions(group_staging)
            fix_musl_dynamic_linker(group_staging)

            # Tar the staging directory with zstd compression
            subprocess.run([
                "tar",
                "--zstd",
                "-cf",
                archive_path,
                "-C",
                group_staging,
                "."
            ], check=True)
            print(f"    Created {archive_name} ({os.path.getsize(archive_path):,} bytes)")
        finally:
            if os.path.exists(group_staging):
                shutil.rmtree(group_staging)


def build_squashfs_rootfs(packages_dir: str, tarballs_dir: str, build_dir: str, staging_dir: str, groups: List[str]) -> str:
    squashfs_output = os.path.join(staging_dir, "rootfs.squashfs")

    print("=================================================================")
    print("       Freeside OS: SquashFS Root Filesystem Assembly            ")
    print("=================================================================")

    sq_staging = os.path.join(build_dir, "squashfs_staging")
    if os.path.exists(sq_staging):
        shutil.rmtree(sq_staging)
    os.makedirs(sq_staging)

    try:
        create_usrmerge_layout(sq_staging)

        resolved = resolve_packages(packages_dir, groups)
        print(f"[*] Resolved {len(resolved)} packages for groups: {', '.join(groups)}")

        extracted = 0
        missing = []
        for pkg_name in resolved:
            tarball = find_package_tarball(packages_dir, tarballs_dir, pkg_name)
            if tarball:
                print(f"    Extracting {pkg_name}...")
                extract_package_tarball(tarball, sq_staging)
                extracted += 1
            else:
                print(f"[-] WARNING: Tarball not found for {pkg_name}")
                missing.append(pkg_name)

        print(f"[*] Extracted {extracted}/{len(resolved)} packages" + (f" ({len(missing)} missing)" if missing else ""))

        create_runtime_directories(sq_staging)
        provision_etc(sq_staging)
        stage_package_cache(sq_staging, packages_dir, tarballs_dir, build_dir, resolved)
        create_offline_archives(packages_dir, tarballs_dir, build_dir, sq_staging)

        # Installer
        installer_src = os.path.join(build_dir, "freeside-install")
        if os.path.isfile(installer_src):
            print("[*] Including installer in the live image...")
            installer_dest = os.path.join(sq_staging, "usr/bin/freeside-install")
            shutil.copy2(installer_src, installer_dest)
            os.chmod(installer_dest, 0o755)
        else:
            raise FileNotFoundError(
                f"Installer binary not found at {installer_src}. Ensure 'just live::build-installer' has been run."
            )

        fix_permissions(sq_staging)
        fix_musl_dynamic_linker(sq_staging)

        # Verification
        libc = os.path.join(sq_staging, "usr/lib/libc.so")
        linker = os.path.join(sq_staging, "usr/lib/ld-musl-x86_64.so.1")
        if not os.path.exists(libc):
            raise RuntimeError("FATAL: libc.so is missing from the rootfs!")
        if os.path.islink(linker):
            target = os.readlink(linker)
            print(f"[+] Verified: ld-musl-x86_64.so.1 -> {target}")
        print(f"[+] Verified: libc.so present ({os.path.getsize(libc)} bytes)")

        print("[*] Compressing SquashFS image (zstd)...")
        subprocess.run(["mksquashfs", sq_staging, squashfs_output, "-comp", "zstd"], check=True)

    finally:
        if os.path.exists(sq_staging):
            shutil.rmtree(sq_staging)

    return squashfs_output


def write_loader_configs(iso_staging: str):
    print("[*] Writing systemd-boot loader configurations...")
    loader_dir = os.path.join(iso_staging, "loader")
    entries_dir = os.path.join(loader_dir, "entries")
    os.makedirs(entries_dir, exist_ok=True)

    with open(os.path.join(loader_dir, "loader.conf"), "w") as f:
        f.write("default live\n")
        f.write("timeout 3\n")

    with open(os.path.join(entries_dir, "live.conf"), "w") as f:
        f.write("title   Freeside OS Live Installer\n")
        f.write("linux   /vmlinuz\n")
        f.write("initrd  /initramfs.cpio.gz\n")
        f.write(f"options {BOOT_OPTIONS}\n")


def stage_boot_files(iso_staging: str, packages_dir: str, tarballs_dir: str):
    print("[*] Staging boot-critical files from package tarballs...")
    boot_files = [
        (KERNEL_PKG,     KERNEL_MEMBER,     os.path.join(iso_staging, "vmlinuz")),
        (INITRAMFS_PKG,  INITRAMFS_MEMBER,  os.path.join(iso_staging, "initramfs.cpio.gz")),
        (BOOTLOADER_PKG, BOOTLOADER_MEMBER, os.path.join(iso_staging, "EFI/BOOT/BOOTX64.EFI")),
    ]

    for pkg_name, member_path, output_path in boot_files:
        tarball = find_package_tarball(packages_dir, tarballs_dir, pkg_name)
        if not tarball:
            raise FileNotFoundError(f"Required package tarball not found: {pkg_name}. Ensure it has been compiled first.")

        if not extract_file_from_tarball(tarball, member_path, output_path):
            raise FileNotFoundError(f"Required file '{member_path}' not found inside {os.path.basename(tarball)}")
        size = os.path.getsize(output_path)
        print(f"    {pkg_name}: {os.path.basename(output_path)} ({size:,} bytes)")


def build_efi_image(iso_staging: str):
    print("[*] Assembling UEFI FAT boot image (efiboot.img)...")
    efiboot = os.path.join(iso_staging, "EFI/boot/efiboot.img")
    os.makedirs(os.path.dirname(efiboot), exist_ok=True)

    subprocess.run(["dd", "if=/dev/zero", f"of={efiboot}", "bs=1M", f"count={EFI_IMAGE_SIZE_MB}"], check=True, capture_output=True)
    subprocess.run(["mkfs.vfat", f"-F", EFI_FAT_TYPE, "-n", EFI_LABEL, efiboot], check=True, capture_output=True)

    def mmd(path):
        subprocess.run(["mmd", "-i", efiboot, f"::{path}"], check=True, capture_output=True)

    def mcopy(src, dst):
        subprocess.run(["mcopy", "-i", efiboot, src, f"::{dst}"], check=True, capture_output=True)

    mmd("/EFI")
    mmd("/EFI/BOOT")
    mcopy(os.path.join(iso_staging, "EFI/BOOT/BOOTX64.EFI"), "/EFI/BOOT/BOOTX64.EFI")
    mmd("/loader")
    mmd("/loader/entries")
    mcopy(os.path.join(iso_staging, "loader/loader.conf"), "/loader/loader.conf")
    mcopy(os.path.join(iso_staging, "loader/entries/live.conf"), "/loader/entries/live.conf")
    mcopy(os.path.join(iso_staging, "vmlinuz"), "/vmlinuz")
    mcopy(os.path.join(iso_staging, "initramfs.cpio.gz"), "/initramfs.cpio.gz")


def build_iso(iso_staging: str, output_path: str):
    print("[*] Compiling final ISO image...")
    subprocess.run([
        "xorriso",
        "-as", "mkisofs",
        "-iso-level", "3",
        "-R", "-J", "-joliet-long",
        "-volid", ISO_VOLUME_LABEL,
        "-e", "EFI/boot/efiboot.img",
        "-no-emul-boot",
        "-isohybrid-gpt-basdat",
        "-output", output_path,
        iso_staging
    ], check=True)
    size_mb = os.path.getsize(output_path) / (1024 * 1024)
    print(f"[+] ISO image written: {output_path} ({size_mb:.1f} MB)")

# ==============================================================================
# CLI Commands
# ==============================================================================

@click.group(name="image")
def image_group():
    """Freeside OS Live ISO Image builder subcommands."""
    pass


@image_group.command(name="build")
@click.option("--packages-dir", help="Path to packages source directory")
@click.option("--build-dir", help="Path to build root directory")
@click.option("--output", help="Output path for the final ISO image")
@click.option("--groups", multiple=True, default=["base", "system"], help="Package groups to include (default: base system)")
@click.pass_context
def build_image_cmd(ctx, packages_dir, build_dir, output, groups):
    """Assembles a bootable UEFI live ISO image from pre-compiled Freeside packages."""
    cfg = ctx.obj["CONFIG"]
    
    p_dir = os.path.abspath(packages_dir) if packages_dir else cfg.packages_dir
    b_dir = os.path.abspath(build_dir) if build_dir else cfg.build_dir
    out_path = os.path.abspath(output) if output else os.path.join(b_dir, "freeside-live.iso")
    t_dir = os.path.join(b_dir, "packages")

    print("=================================================================")
    print("         Freeside OS: Live ISO Image Builder (fsimage)           ")
    print("=================================================================")
    print(f"  Packages Dir:  {p_dir}")
    print(f"  Build Dir:     {b_dir}")
    print(f"  Tarballs Dir:  {t_dir}")
    print(f"  Output:        {out_path}")
    print(f"  Groups:        {', '.join(groups)}")
    print("=================================================================")

    check_host_dependencies()

    if not os.path.isdir(p_dir):
        click.secho(f"[-] ERROR: Packages directory not found: {p_dir}", err=True, fg="red")
        sys.exit(1)
    if not os.path.isdir(t_dir):
        click.secho(f"[-] ERROR: Tarballs directory not found: {t_dir}", err=True, fg="red")
        sys.exit(1)

    iso_staging = os.path.join(b_dir, "iso_staging")
    if os.path.exists(iso_staging):
        shutil.rmtree(iso_staging)
    os.makedirs(iso_staging)
    os.makedirs(os.path.join(iso_staging, "EFI/boot"), exist_ok=True)
    os.makedirs(os.path.join(iso_staging, "EFI/BOOT"), exist_ok=True)

    try:
        stage_boot_files(iso_staging, p_dir, t_dir)
        write_loader_configs(iso_staging)
        build_efi_image(iso_staging)
        build_squashfs_rootfs(p_dir, t_dir, b_dir, iso_staging, list(groups))
        build_iso(iso_staging, out_path)
    finally:
        if os.path.exists(iso_staging):
            shutil.rmtree(iso_staging)

    print("=================================================================")
    print("[+] Live ISO image assembled successfully!")
    print(f"    {out_path}")
    print("=================================================================")
