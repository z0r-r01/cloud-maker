#!/usr/bin/env bash
set -euo pipefail

URI='qemu:///system'
IMAGE_DIR='/var/lib/libvirt/images/cloud-maker'
BASE_IMAGE="$IMAGE_DIR/ubuntu-24.04-base.img"

IMAGE_NAME='noble-server-cloudimg-amd64.img'
IMAGE_URL="https://cloud-images.ubuntu.com/noble/current/$IMAGE_NAME"

# Проверяем архитектуру и KVM
if [[ "$(uname -m)" != 'x86_64' ]]; then
    echo 'Ошибка: необходим Linux x86_64.'
    exit 1
fi

if [[ ! -c /dev/kvm ]]; then
    echo 'Ошибка: /dev/kvm недоступен.'
    exit 1
fi

# 1. Устанавливаем зависимости
echo '[1/5] Устанавливаем пакеты...'

sudo apt-get update

sudo apt-get install -y \
    qemu-kvm qemu-utils \
    libvirt-daemon-system libvirt-daemon-config-network \
    libvirt-clients virtinst cloud-image-utils \
    python3-libvirt python3-venv curl

# 2. Запускаем libvirt
echo '[2/5] Запускаем libvirt...'

sudo systemctl enable --now libvirtd
sudo virsh -c "$URI" list --all >/dev/null

# 3. Настраиваем виртуальную сеть
echo '[3/5] Проверяем сеть default...'

if ! sudo virsh -c "$URI" net-info default >/dev/null 2>&1; then
    NETWORK_XML='/usr/share/libvirt/networks/default.xml'

    if [[ ! -f "$NETWORK_XML" ]]; then
        echo 'Ошибка: конфигурация сети default не найдена.'
        exit 1
    fi

    sudo virsh -c "$URI" net-define "$NETWORK_XML"
fi

if ! sudo virsh -c "$URI" net-list --name | grep -Fxq 'default'; then
    sudo virsh -c "$URI" net-start default
fi

sudo virsh -c "$URI" net-autostart default

# 4. Подготавливаем базовый образ Ubuntu
echo '[4/5] Проверяем образ Ubuntu...'

sudo install -d -m 0755 "$IMAGE_DIR"

if sudo test -s "$BASE_IMAGE"; then
    echo 'Образ уже существует. Загрузка не требуется.'
else
    TEMP_DIR="$(mktemp -d)"
    trap 'rm -rf "$TEMP_DIR"' EXIT

    curl -fL --retry 3 \
        -o "$TEMP_DIR/$IMAGE_NAME" \
        "$IMAGE_URL"

    curl -fL --retry 3 \
        -o "$TEMP_DIR/SHA256SUMS" \
        'https://cloud-images.ubuntu.com/noble/current/SHA256SUMS'

    (
        cd "$TEMP_DIR"
        grep ' \*noble-server-cloudimg-amd64.img$' SHA256SUMS |
            sha256sum -c -
    )

    sudo install -m 0644 \
        "$TEMP_DIR/$IMAGE_NAME" "$BASE_IMAGE"
fi

sudo qemu-img info "$BASE_IMAGE" >/dev/null

# 5. Подготавливаем SSH-ключ
echo '[5/5] Проверяем SSH-ключ...'

KEY="$HOME/.ssh/cloud-maker-test"

mkdir -p "$HOME/.ssh"
chmod 700 "$HOME/.ssh"

if [[ ! -e "$KEY" && ! -e "$KEY.pub" ]]; then
    ssh-keygen -q -t ed25519 -f "$KEY" -N ''
elif [[ ! -f "$KEY.pub" ]]; then
    echo "Ошибка: отсутствует публичный ключ $KEY.pub"
    exit 1
fi

echo 'Установка Cloud Maker успешно завершена'
