import re
import libvirt


LIBVIRT_URI = "qemu:///system"

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
