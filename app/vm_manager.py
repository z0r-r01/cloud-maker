import re
import libvirt
import os
import shutil
import subprocess
import threading
import xml.etree.ElementTree as ET
from pathlib import Path
from app.cloudinit import create_cloud_init

LIBVIRT_URI = "qemu:///system"
MAX_TOTAL_CPUS = 4
MAX_TOTAL_MEMORY_MB = 4096

IMAGE_DIR = Path("/var/lib/libvirt/images/cloud-maker")
API_IMAGE_DIR = IMAGE_DIR / "api"
BASE_IMAGE = IMAGE_DIR / "ubuntu-24.04-base.img"

SSH_PUBLIC_KEY = Path.home() / ".ssh/cloud-maker-test.pub"

VM_LOCK = threading.Lock ()

STATE_NAMES = {
    libvirt.VIR_DOMAIN_NOSTATE: "unknown",
    libvirt.VIR_DOMAIN_RUNNING: "running",
    libvirt.VIR_DOMAIN_BLOCKED: "blocked",
    libvirt.VIR_DOMAIN_PAUSED: "paused",
    libvirt.VIR_DOMAIN_SHUTDOWN: "shutting_down",
    libvirt.VIR_DOMAIN_SHUTOFF: "shut_off",
    libvirt.VIR_DOMAIN_CRASHED: "crashed",
}


class VMNotFound(Exception):
    pass


class VMConflict(Exception):
    pass


def connect():
    conn = libvirt.open(LIBVIRT_URI)

    if conn is None:
        raise RuntimeError("Cannot connect to libvirt")

    return conn


def find_vm(conn, name):
    # Разрешаем управление только ВМ с именами нашего проекта
    if not re.fullmatch(r"cm-[a-z0-9-]+", name):
        raise VMNotFound(f"VM '{name}' not found")

    try:
        vm = conn.lookupByName(name)
    except libvirt.libvirtError as exc:
        if exc.get_error_code() == libvirt.VIR_ERR_NO_DOMAIN:
            raise VMNotFound(f"VM '{name}' not found") from exc
        raise

    if vm is None:
        raise VMNotFound(f"VM '{name}' not found")

    return vm


def vm_to_dict(vm):
    info = vm.info()

    return {
        "name": vm.name(),
        "state": STATE_NAMES.get(info[0], "unknown"),
        "cpus": info[3],
        "memory_mb": info[1] // 1024,
    }


def list_vms():
    conn = connect()

    try:
        result = []

        for vm in conn.listAllDomains():
            if vm.name().startswith("cm-"):
                result.append(vm_to_dict(vm))

        return sorted(result, key=lambda vm: vm["name"])

    finally:
        conn.close()


def get_vm(name):
    conn = connect()

    try:
        vm = find_vm(conn, name)
        return vm_to_dict(vm)

    finally:
        conn.close()


def start_vm(name):
    conn = connect()

    try:
        vm = find_vm(conn, name)

        if vm.isActive():
            raise VMConflict(f"VM '{name}' is already active")

        vm.create()

        return {
            "message": "VM started",
            "vm": vm_to_dict(vm),
        }

    finally:
        conn.close()


def stop_vm(name):
    conn = connect()

    try:
        vm = find_vm(conn, name)

        if not vm.isActive():
            raise VMConflict(f"VM '{name}' is already stopped")

        vm.shutdown()

        return {
            "message": "Shutdown requested",
            "name": name,
        }

    finally:
        conn.close()

def build_vm_xml(name, cpus, memory_mb, disk_path, seed_path):
    domain = ET.Element("domain", type="kvm")

    ET.SubElement(domain, "name").text = name

    ET.SubElement(
        domain,
        "memory",
        unit="MiB",
    ).text = str(memory_mb)

    ET.SubElement(
        domain,
        "vcpu",
    ).text = str(cpus)

    os_element = ET.SubElement(domain, "os")

    ET.SubElement(
        os_element,
        "type",
        arch="x86_64",
    ).text = "hvm"

    ET.SubElement(os_element, "boot", dev="hd")

    # Поддержка корректного выключения гостевой ОС
    features = ET.SubElement(domain, "features")
    ET.SubElement(features, "acpi")

    devices = ET.SubElement(domain, "devices")

    # Основной виртуальный диск
    disk = ET.SubElement(
        devices,
        "disk",
        type="file",
        device="disk",
    )

    ET.SubElement(
        disk,
        "driver",
        name="qemu",
        type="qcow2",
    )

    ET.SubElement(disk, "source", file=str(disk_path))
    ET.SubElement(disk, "target", dev="vda", bus="virtio")

    # Виртуальный CD-ROM с cloud-init
    cdrom = ET.SubElement(
        devices,
        "disk",
        type="file",
        device="cdrom",
    )

    ET.SubElement(
        cdrom,
        "driver",
        name="qemu",
        type="raw",
    )

    ET.SubElement(cdrom, "source", file=str(seed_path))
    ET.SubElement(cdrom, "target", dev="sda", bus="sata")
    ET.SubElement(cdrom, "readonly")

    # Сетевая карта
    interface = ET.SubElement(
        devices,
        "interface",
        type="network",
    )

    ET.SubElement(interface, "source", network="default")
    ET.SubElement(interface, "model", type="virtio")

    # Консоль
    serial = ET.SubElement(devices, "serial", type="pty")
    ET.SubElement(serial, "target", port="0")

    console = ET.SubElement(devices, "console", type="pty")
    ET.SubElement(
        console,
        "target",
        type="serial",
        port="0",
    )

    return ET.tostring(domain, encoding="unicode")

def create_vm(name, cpus, memory_mb, disk_gb, hostname=None, ssh_public_key=None):
    if not re.fullmatch(r"cm-api-[a-z0-9-]+", name):
        raise ValueError("VM name must start with cm-api-")

    if not 1 <= cpus <= 4:
        raise ValueError("CPU count must be between 1 and 4")

    if not 512 <= memory_mb <= 4096:
        raise ValueError("Memory must be between 512 and 4096 MB")

    if not 5 <= disk_gb <= 30:
        raise ValueError("Disk size must be between 5 and 30 GB")

    disk_path = API_IMAGE_DIR / f"{name}.qcow2"
    seed_path = API_IMAGE_DIR / f"{name}-seed.iso"

    if not BASE_IMAGE.is_file():
        raise RuntimeError("Base Ubuntu image not found")

    if ssh_public_key is None and not SSH_PUBLIC_KEY.is_file():
        raise RuntimeError("SSH public key not found")

    with VM_LOCK:
        conn = connect()
        vm = None

        try:
            try:
                existing = conn.lookupByName(name)
            except libvirt.libvirtError as exc:
                if exc.get_error_code() != libvirt.VIR_ERR_NO_DOMAIN:
                    raise
            else:
                raise VMConflict(f"VM '{name}' already exists")

            if disk_path.exists() or seed_path.exists():
                raise VMConflict("VM storage files already exist")
            
            # Проверяем суммарные квоты до создания диска
            quota = quota_snapshot(conn)

            new_total_cpus = quota["used"]["cpus"] + cpus

            new_total_memory = (
                quota["used"]["memory_mb"] + memory_mb
            )

            if (
                new_total_cpus > MAX_TOTAL_CPUS
                or new_total_memory > MAX_TOTAL_MEMORY_MB
            ):
                raise VMConflict(
                    "Resource quota exceeded. "
                    f"Available: {quota['available']['cpus']} vCPU, "
                    f"{quota['available']['memory_mb']} MiB RAM. "
                    f"Requested: {cpus} vCPU, {memory_mb} MiB RAM."
                )

            try:
                # Создаём диск из базового образа
                shutil.copyfile(BASE_IMAGE, disk_path)

                subprocess.run(
                    [
                        "qemu-img",
                        "resize",
                        str(disk_path),
                        f"{disk_gb}G",
                    ],
                    check=True,
                )

                os.chmod(disk_path, 0o660)

                # Создаём конфигурацию cloud-init
                create_cloud_init(name, seed_path, hostname, ssh_public_key,)

                # Формируем XML
                xml = build_vm_xml(
                    name,
                    cpus,
                    memory_mb,
                    disk_path,
                    seed_path,
                )

                # Регистрируем ВМ в libvirt
                vm = conn.defineXML(xml)

                if vm is None:
                    raise RuntimeError("Failed to define VM")

                # Запускаем виртуальную машину
                vm.create()

                return {
                    "message": "VM created successfully",
                    "vm": vm_to_dict(vm),
                }

            except Exception:
                # При ошибке убираем частично созданные ресурсы
                if vm is not None:
                    try:
                        if vm.isActive():
                            vm.destroy()

                        vm.undefine()
                    except libvirt.libvirtError:
                        pass

                disk_path.unlink(missing_ok=True)
                seed_path.unlink(missing_ok=True)
                raise

        finally:
            conn.close()

def delete_vm(name):
    if not re.fullmatch(r"cm-api-[a-z0-9-]+", name):
        raise VMNotFound(f"VM '{name}' not found")

    disk_path = API_IMAGE_DIR / f"{name}.qcow2"
    seed_path = API_IMAGE_DIR / f"{name}-seed.iso"

    with VM_LOCK:
        conn = connect()

        try:
            vm = find_vm(conn, name)

            if vm.isActive():
                raise VMConflict(
                    "Stop the VM before deleting it"
                )

            # Проверяем диски в конфигурации ВМ.
            # Не удаляем машину, если используются другие пути.
            xml = ET.fromstring(vm.XMLDesc(0))

            source_paths = set()

            for source in xml.findall("./devices/disk/source"):
                path = source.get("file")

                if path is not None:
                    source_paths.add(path)

            expected = {
                str(disk_path),
                str(seed_path),
            }

            if source_paths != expected:
                raise VMConflict(
                    "VM storage does not match Cloud Maker layout"
                )

            # Удаляем регистрацию ВМ из libvirt
            vm.undefine()

            # Удаляем принадлежащие ей файлы
            disk_path.unlink(missing_ok=True)
            seed_path.unlink(missing_ok=True)

            return {
                "message": "VM deleted successfully",
                "name": name,
            }

        finally:
            conn.close()

def quota_snapshot(conn):
    used_cpus = 0
    used_memory_mb = 0

    for vm in conn.listAllDomains():
        if not vm.name().startswith("cm-"):
            continue

        # Читаем сохранённую конфигурацию ВМ.
        # Она существует и для выключенных машин.
        xml = ET.fromstring(
            vm.XMLDesc(libvirt.VIR_DOMAIN_XML_INACTIVE)
        )

        vcpu_element = xml.find("vcpu")
        memory_element = xml.find("memory")

        if vcpu_element is None or memory_element is None:
            raise RuntimeError("VM resource configuration is missing")

        cpus = int(vcpu_element.text)

        memory_value = int(memory_element.text)
        unit = memory_element.get("unit", "KiB")

        if unit == "KiB":
            memory_mb = (memory_value + 1023) // 1024
        elif unit == "MiB":
            memory_mb = memory_value
        elif unit == "GiB":
            memory_mb = memory_value * 1024
        else:
            raise RuntimeError(f"Unsupported memory unit: {unit}")

        used_cpus += cpus
        used_memory_mb += memory_mb

    return {
        "limits": {
            "cpus": MAX_TOTAL_CPUS,
            "memory_mb": MAX_TOTAL_MEMORY_MB,
        },
        "used": {
            "cpus": used_cpus,
            "memory_mb": used_memory_mb,
        },
        "available": {
            "cpus": max(0, MAX_TOTAL_CPUS - used_cpus),
            "memory_mb": max(
                0,
                MAX_TOTAL_MEMORY_MB - used_memory_mb,
            ),
        },
    }


def get_quota():
    conn = connect()

    try:
        return quota_snapshot(conn)
    finally:
        conn.close()
