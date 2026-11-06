import libvirt
from pydantic import BaseModel, Field
from fastapi import FastAPI, HTTPException

from app.vm_manager import (
    VMNotFound,
    VMConflict,
    list_vms,
    get_vm,
    start_vm,
    stop_vm,
    create_vm,
    delete_vm,
)


app = FastAPI(
    title="Cloud Maker",
    description="Mini cloud platform based on QEMU/KVM",
    version="0.2.0",
)

class VMCreateRequest(BaseModel):
    name: str
    cpus: int = Field(default=1, ge=1, le=4)
    memory_mb: int = Field(default=1024, ge=512, le=4096)
    disk_gb: int = Field(default=10, ge=5, le=30)

    hostname: str | None = Field(
        default=None,
        max_length=63,
    )

    ssh_public_key: str | None = Field(
        default=None,
        max_length=4096,
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

    except ValueError as exc:
        raise HTTPException(
            status_code=400,
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

@app.post("/vms", status_code=201)
def create_vm_endpoint(request: VMCreateRequest):
    return execute(
        create_vm,
        request.name,
        request.cpus,
        request.memory_mb,
        request.disk_gb,
	request.hostname,
	request.ssh_public_key
    )


@app.delete("/vms/{name}")
def delete_vm_endpoint(name: str):
    return execute(delete_vm, name)
