#!/usr/bin/env bash
set -euo pipefail

# Параметры виртуальной машины
NAME="${1:-}"
CPUS="${2:-1}"
MEMORY="${3:-1024}"

# Пути
IMAGE_DIR="/var/lib/libvirt/images/cloud-maker"
BASE_IMAGE="$IMAGE_DIR/ubuntu-24.04-base.img"
VM_DISK="$IMAGE_DIR/$NAME.qcow2"

SSH_KEY="$HOME/.ssh/cloud-maker-test.pub"

# Проверяем параметры
if [[ ! "$NAME" =~ ^cm-[a-z0-9-]+$ ]]; then
    echo "Ошибка: имя должно начинаться с cm-"
    exit 1
fi

if [[ ! "$CPUS" =~ ^[1-9][0-9]*$ ]] ||
   [[ ! "$MEMORY" =~ ^[1-9][0-9]*$ ]]; then
    echo "Ошибка: CPU и RAM должны быть положительными числами"
    exit 1
fi

# Проверяем необходимые файлы
if ! sudo test -f "$BASE_IMAGE"; then
    echo "Ошибка: базовый образ Ubuntu не найден"
    exit 1
fi

if [[ ! -f "$SSH_KEY" ]]; then
    echo "Ошибка: публичный SSH-ключ не найден"
    exit 1
fi

# Проверяем, существует ли уже такая ВМ
if sudo virsh -c qemu:///system dominfo "$NAME" \
    >/dev/null 2>&1; then
    echo "Ошибка: ВМ $NAME уже существует"
    exit 1
fi

if sudo test -e "$VM_DISK"; then
    echo "Ошибка: диск $VM_DISK уже существует"
    exit 1
fi

echo "Создаём виртуальную машину: $NAME"

# Копируем базовый образ
sudo cp "$BASE_IMAGE" "$VM_DISK"

# Увеличиваем диск до 10 ГБ
sudo qemu-img resize "$VM_DISK" 10G

# Создаём временную конфигурацию cloud-init
TEMP_DIR=$(mktemp -d)
trap 'rm -rf "$TEMP_DIR"' EXIT

PUBLIC_KEY=$(cat "$SSH_KEY")

cat > "$TEMP_DIR/user-data" <<EOF
#cloud-config
hostname: $NAME
users:
  - default
ssh_authorized_keys:
  - $PUBLIC_KEY
ssh_pwauth: false
EOF

# Запускаем виртуальную машину
sudo virt-install \
    --connect qemu:///system \
    --name "$NAME" \
    --memory "$MEMORY" \
    --vcpus "$CPUS" \
    --osinfo generic \
    --import \
    --disk path="$VM_DISK",format=qcow2,bus=virtio \
    --network network=default,model=virtio \
    --graphics none \
    --console pty,target_type=serial \
    --cloud-init user-data="$TEMP_DIR/user-data" \
    --noautoconsole

echo "Виртуальная машина $NAME успешно создана"
