import libvirt

from fastapi import FastAPI, HTTPException

from app.vm_manager import list_vms


app = FastAPI(
    title="Cloud Maker",
    description="Mini cloud platform based on QEMU/KVM",
    version="0.1.0",
)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/vms")
def get_vms():
    try:
        return list_vms()

    except (libvirt.libvirtError, RuntimeError):
        raise HTTPException(
            status_code=503,
            detail="Libvirt is unavailable",
        )
