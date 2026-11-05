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


def list_vms():
    conn = libvirt.open(LIBVIRT_URI)

    if conn is None:
        raise RuntimeError("Cannot connect to libvirt")

    try:
        result = []

        for vm in conn.listAllDomains():
            name = vm.name()

            if not name.startswith("cm-"):
                continue

            info = vm.info()

            vm_data = {
                "name": name,
                "state": STATE_NAMES.get(info[0], "unknown"),
                "cpus": info[3],
                "memory_mb": info[1] // 1024,
            }

            result.append(vm_data)

        return sorted(result, key=lambda vm: vm["name"])

    finally:
        conn.close()
