#!/usr/bin/env python3
"""Однокомандная интеграционная проверка Cloud Maker.

Запускает собственный Uvicorn на localhost, создаёт одну уникальную ВМ,
проверяет API, cloud-init, квоты, веб-панель и рестарт приложения.
Другие виртуальные машины не останавливает и не удаляет.
"""

import json
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# При прямом запуске scripts/check_api.py Python не добавляет корень проекта в sys.path.
# Для импорта app.cloudinit включаем его явно.
sys.path.insert(0, str(ROOT))
IMAGE_DIR = Path("/var/lib/libvirt/images/cloud-maker/api")
SHUTDOWN_TIMEOUT = 180  # Гостевая ОС выключается асинхронно.
FIRST_BOOT_GRACE = 60  # Новая Ubuntu VM может не принять ACPI до конца загрузки.


class CheckFailure(Exception):
    pass


def require(condition, message):
    if not condition:
        raise CheckFailure(message)


def ok(message):
    print(f"[OK] {message}", flush=True)


def info(message):
    print(f"[INFO] {message}", flush=True)


def wait_until(predicate, timeout, message):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(2)
    raise CheckFailure(message)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class Server:
    def __init__(self, log_path):
        self.port = free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.log_path = log_path
        self.process = None
        self.log_file = None

    def start(self):
        self.log_file = self.log_path.open("ab")
        self.process = subprocess.Popen(
            [
                sys.executable, "-m", "uvicorn", "app.main:app",
                "--host", "127.0.0.1", "--port", str(self.port),
                "--no-access-log",
            ],
            cwd=ROOT,
            stdout=self.log_file,
            stderr=subprocess.STDOUT,
        )

        def ready():
            if self.process.poll() is not None:
                raise CheckFailure("Uvicorn завершился при запуске (см. лог ниже)")
            try:
                status, body = self.request("GET", "/health", timeout=2)
                return status == 200 and body.get("status") == "ok"
            except (OSError, ValueError, CheckFailure):
                return False

        wait_until(ready, 25, "Uvicorn не запустился за 25 секунд")

    def stop(self):
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.process = None
        if self.log_file is not None:
            self.log_file.close()
            self.log_file = None

    def restart(self):
        self.stop()
        self.start()

    def request(self, method, path, data=None, timeout=15):
        headers = {}
        payload = None
        if data is not None:
            payload = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(
            self.base_url + path, data=payload, headers=headers, method=method
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                status = response.status
                raw = response.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            raw = exc.read()
        except urllib.error.URLError as exc:
            raise CheckFailure(f"API недоступен ({method} {path}): {exc}") from exc
        if path == "/":
            return status, raw.decode("utf-8", errors="replace")
        try:
            return status, json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise CheckFailure(
                f"Некорректный JSON ({method} {path}, HTTP {status}): "
                f"{raw[:160]!r}"
            ) from exc

    def expect(self, method, path, expected_status, data=None, timeout=15):
        status, body = self.request(method, path, data, timeout)
        if status != expected_status:
            raise CheckFailure(
                f"{method} {path}: ожидали HTTP {expected_status}, "
                f"получили HTTP {status}: {body}"
            )
        return body


def make_test_key(tmpdir):
    if shutil.which("ssh-keygen") is None:
        raise CheckFailure("Нужен ssh-keygen (пакет openssh-client)")
    path = Path(tmpdir) / "check_ed25519"
    subprocess.run(
        ["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(path)],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    return " ".join(path.with_suffix(".pub").read_text(encoding="utf-8").split()[:2])


def wait_state(server, name, expected, timeout=SHUTDOWN_TIMEOUT):
    def has_state():
        vm = server.expect("GET", f"/vms/{name}", 200)
        return vm["state"] == expected
    wait_until(has_state, timeout, f"ВМ {name} не перешла в {expected}")


def graceful_stop(server, name, boot_grace=FIRST_BOOT_GRACE):
    """Ждём начальную загрузку ОС и повторяем ACPI-запрос при необходимости.

    HTTP 202 означает лишь отправку команды. Не подменяем эту проверку
    принудительным destroy(): гостевая ОС должна выключиться самостоятельно.
    """
    info(f"Даём гостевой ОС {boot_grace} с на начальную загрузку перед ACPI shutdown")
    time.sleep(boot_grace)
    server.expect("POST", f"/vms/{name}/stop", 202)
    deadline = time.monotonic() + SHUTDOWN_TIMEOUT
    last_retry = time.monotonic()
    last_state = "unknown"
    while time.monotonic() < deadline:
        last_state = server.expect("GET", f"/vms/{name}", 200)["state"]
        if last_state == "shut_off":
            return
        # При раннем shutdown ОС могла ещё не инициализировать ACPI.
        # Повторяем только когда домен всё ещё RUNNING, не трогаем другие ВМ.
        if last_state == "running" and time.monotonic() - last_retry >= 30:
            try:
                server.expect("POST", f"/vms/{name}/stop", 202)
                info(f"Повторный запрос ACPI shutdown для {name}")
            except CheckFailure as exc:
                info(f"Повторный shutdown не принят: {exc}")
            last_retry = time.monotonic()
        time.sleep(3)
    raise CheckFailure(
        f"Гость {name} не завершил работу по ACPI за {SHUTDOWN_TIMEOUT} с "
        f"(последнее состояние: {last_state}). Проверьте консоль ВМ и "
        "загрузку Ubuntu. Принудительный destroy() не засчитываем как "
        "успешное штатное завершение."
    )


def quota_snapshot(server):
    quota = server.expect("GET", "/quota", 200)
    for key in ("limits", "used", "available"):
        require(key in quota, f"В /quota отсутствует поле {key}")
    return quota


def cleanup_owned_vm(name):
    """Очистка исключительно нашей тестовой ВМ, безопасная при сбое libvirt."""
    can_remove_disks = False
    try:
        import libvirt
        conn = libvirt.open("qemu:///system")
        if conn is None:
            raise RuntimeError("Cannot connect to libvirt")
        try:
            vm = next(
                (domain for domain in conn.listAllDomains()
                 if domain.name() == name),
                None,
            )
            if vm is not None:
                if vm.isActive():
                    vm.destroy()
                vm.undefine()
            # На случай ошибки удаления проверяем отсутствие домена ещё раз.
            can_remove_disks = not any(
                domain.name() == name for domain in conn.listAllDomains()
            )
        finally:
            conn.close()
    except Exception as exc:
        info(f"Не удалось полностью очистить libvirt: {exc}")

    if not can_remove_disks:
        info(f"Диски {name} сохранены: удаление ВМ не подтверждено.")
        return

    for suffix in (".qcow2", "-seed.iso"):
        path = IMAGE_DIR / f"{name}{suffix}"
        try:
            path.unlink(missing_ok=True)
        except OSError as exc:
            info(f"Не удалось удалить {path}: {exc}")


def main():
    name = "cm-api-check-" + uuid.uuid4().hex[:12]
    hostname = "check-" + uuid.uuid4().hex[:10]
    success = False

    with tempfile.TemporaryDirectory(prefix="cloud-maker-check-") as temp:
        log_path = Path(temp) / "uvicorn.log"
        server = Server(log_path)
        try:
            info("Проверка Cloud Maker: без остановки существующих ВМ")
            ssh_pub = make_test_key(temp)
            server.start()
            ok("Запуск HTTP API и GET /health")

            webpage = server.expect("GET", "/", 200)
            require("Cloud Maker" in webpage and "<html" in webpage.lower(),
                    "Главная страница не содержит веб-интерфейс Cloud Maker")
            ok("Собственная веб-панель по адресу /")

            baseline_vms = server.expect("GET", "/vms", 200)
            require(isinstance(baseline_vms, list), "/vms не вернул список")
            baseline_names = {vm["name"] for vm in baseline_vms}
            before = quota_snapshot(server)
            require(before["available"]["cpus"] >= 1 and
                    before["available"]["memory_mb"] >= 512,
                    "Недостаточно свободной квоты для тестовой ВМ. "
                    "Освободите 1 vCPU и 512 MiB RAM.")
            ok("GET /vms и GET /quota")

            request = {
                "name": name,
                "cpus": 1,
                "memory_mb": 512,
                "disk_gb": 5,
                "hostname": hostname,
                "ssh_public_key": ssh_pub,
            }
            created = server.expect("POST", "/vms", 201, request, timeout=180)
            require(created.get("vm", {}).get("name") == name,
                    "POST /vms вернул неожиданную ВМ")
            ok("Создание и автоматический запуск тестовой ВМ")

            listed = server.expect("GET", "/vms", 200)
            require(any(vm["name"] == name for vm in listed),
                    "Созданная ВМ отсутствует в /vms")
            detail = server.expect("GET", f"/vms/{name}", 200)
            require(detail["state"] == "running", "Новая ВМ не запущена")
            require(detail["cpus"] == 1 and detail["memory_mb"] == 512,
                    "CPU/RAM ВМ не соответствуют запросу")
            ok("Список ВМ и получение конкретной ВМ")

            disk_path = IMAGE_DIR / f"{name}.qcow2"
            seed_path = IMAGE_DIR / f"{name}-seed.iso"
            require(disk_path.is_file() and disk_path.stat().st_size > 0,
                    "Диск qcow2 не создан")
            require(seed_path.is_file() and seed_path.stat().st_size > 0,
                    "ISO cloud-init не создан")
            # Файлы в /var/lib/libvirt/images могут принадлежать root/libvirt-qemu.
            # Пользователь с доступом к libvirt не обязан иметь право читать ISO.
            # Поэтому по возможности читаем реальный ISO, а при PermissionError
            # независимо проверяем генератор на ISO в нашем временном каталоге.
            try:
                seed_content = seed_path.read_bytes()
                checked_seed = "реальный ISO"
            except PermissionError:
                from app.cloudinit import create_cloud_init
                local_seed = Path(temp) / "check-cloud-init.iso"
                create_cloud_init(name, local_seed, hostname, ssh_pub)
                seed_content = local_seed.read_bytes()
                checked_seed = "временный ISO генератора (оригинал защищён правами)"
                info("Реальный cloud-init ISO недоступен на чтение: "
                     "проверяем генератор на временном ISO")

            require(b"#cloud-config" in seed_content and
                    hostname.encode() in seed_content and
                    ssh_pub.encode() in seed_content and
                    name.encode() in seed_content,
                    "ISO cloud-init не содержит заданные hostname/SSH-ключ/instance-id")
            ok(f"Диск qcow2 и cloud-init ({checked_seed}; hostname, SSH-ключ)")

            server.expect("POST", "/vms", 409, request, timeout=20)
            bad_name = dict(request, name="not-cloud-maker")
            server.expect("POST", "/vms", 400, bad_name, timeout=20)
            ok("Защита от дублирующих имён и неправильного имени")

            after_create = quota_snapshot(server)
            require(after_create["used"]["cpus"] == before["used"]["cpus"] + 1 and
                    after_create["used"]["memory_mb"] == before["used"]["memory_mb"] + 512,
                    "Квоты после создания ВМ рассчитаны неправильно")
            oversized = dict(request, name="cm-api-oversized-" + uuid.uuid4().hex[:10],
                             cpus=4, memory_mb=4096)
            server.expect("POST", "/vms", 409, oversized, timeout=20)
            require(not (IMAGE_DIR / f"{oversized['name']}.qcow2").exists(),
                    "При превышении квоты появился лишний диск")
            ok("Учёт ресурсов и запрет превышения CPU/RAM квот")

            graceful_stop(server, name)
            ok("Корректная остановка ВМ")

            server.expect("POST", f"/vms/{name}/start", 200)
            wait_state(server, name, "running", timeout=30)
            ok("Повторный запуск ВМ")

            server.restart()
            restored = server.expect("GET", f"/vms/{name}", 200)
            require(restored["state"] == "running", "Состояние ВМ после рестарта потеряно")
            after_restart = quota_snapshot(server)
            require(after_restart["used"] == after_create["used"],
                    "Квоты потеряны после рестарта API")
            ok("Восстановление состояния ВМ и квот после рестарта сервиса")

            graceful_stop(server, name)
            server.expect("DELETE", f"/vms/{name}", 200)
            status, _ = server.request("GET", f"/vms/{name}")
            require(status == 404, "После удаления ВМ остаётся доступна через API")
            require(not disk_path.exists() and not seed_path.exists(),
                    "После удаления остались диск или seed ISO")
            final_names = {vm["name"] for vm in server.expect("GET", "/vms", 200)}
            require(final_names == baseline_names,
                    "Список ВМ не вернулся к исходному состоянию")
            final_quota = quota_snapshot(server)
            require(final_quota["used"] == before["used"],
                    "Использованные квоты не вернулись к исходным")
            ok("Удаление тестовой ВМ и файлов, возврат исходных квот")

            success = True
            print("\n[PASS] Все проверки Cloud Maker выполнены успешно.", flush=True)
        except (CheckFailure, subprocess.CalledProcessError, OSError, KeyError,
                TypeError, ValueError, TimeoutError) as exc:
            print(f"\n[FAIL] {exc}", file=sys.stderr, flush=True)
        finally:
            server.stop()
            # На случай сбоя удаляем только собственный случайный домен.
            cleanup_owned_vm(name)
            if not success and log_path.exists():
                print("\n--- Последние строки лога Uvicorn ---", file=sys.stderr)
                print("\n".join(log_path.read_text(errors="replace").splitlines()[-35:]),
                      file=sys.stderr)
    return 0 if success else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[FAIL] Проверка прервана", file=sys.stderr)
        sys.exit(130)
