import libvirt

from fastapi import FastAPI, HTTPException

from app.vm_manager import (
    VMNotFound,
    VMConflict,
    list_vms,
    get_vm,
    start_vm,
    stop_vm,
)


app = FastAPI(
    title="Cloud Maker",
    description="Mini cloud platform based on QEMU/KVM",
    version="0.2.0",
)


def execute(action, *args):
    try:
        return action(*args)

    except VMNotFound as exc:
        raise HTTPException(
            status_code=404,
            detail=str(exc),
        ) from exc

    except VMConflict as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc),
        ) from exc

    except (libvirt.libvirtError, RuntimeError) as exc:
        raise HTTPException(
            status_code=503,
            detail="Virtualization service error",
        ) from exc


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/vms")
def get_vms():
    return execute(list_vms)


@app.get("/vms/{name}")
def get_vm_by_name(name: str):
    return execute(get_vm, name)


@app.post("/vms/{name}/start")
def start_vm_endpoint(name: str):
    return execute(start_vm, name)


@app.post("/vms/{name}/stop", status_code=202)
def stop_vm_endpoint(name: str):
    return execute(stop_vm, name)
