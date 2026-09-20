#!/usr/bin/env bash
# Run only on verified G2-002. Does not attach a GPU or modify host drivers.
# IMAGE_NAME may be used for a side-by-side candidate; production is never
# overwritten by this script.
set -euo pipefail
test "$(hostname)" = G2-002
test "$(id -u)" = 0
root=/var/lib/1cat-rental
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
image_name="${IMAGE_NAME:-gaudi-ubuntu24.04-candidate.qcow2}"
target=$root/images/$image_name
manifest=$root/images/${image_name%.qcow2}.manifest.json
test ! -e "$target"
# QEMU runs as libvirt-qemu:kvm.  Keep the image directory private from other
# users while preserving the execute bit that QEMU needs to reach a backing
# image.  A root:root 0750 directory makes every qcow2 child unreadable to QEMU.
install -d -o root -g kvm -m 0750 "$root/images"
stage=$(mktemp -d "$root/images/build-XXXXXXXX")
source_image="${SOURCE_IMAGE:-/home/xinyi/kvm-images/noble-server-cloudimg-amd64.img}"
if [ -n "${SOURCE_IMAGE:-}" ]; then
  qemu-img convert -O qcow2 "$source_image" "$stage/system.qcow2"
  qemu-img resize "$stage/system.qcow2" 50G
else
  qemu-img create -f qcow2 "$stage/system.qcow2" 50G
  virt-resize --expand /dev/sda1 "$source_image" "$stage/system.qcow2"
fi
install -d "$stage/debs"
if [ -n "${OFFLINE_PACKAGE_DIR:-}" ]; then
  find "$OFFLINE_PACKAGE_DIR" -maxdepth 1 -type f -name '*.deb' -exec cp -- '{}' "$stage/debs/" \;
fi
for package in habanalabs-container-runtime habanalabs-dkms habanalabs-firmware habanalabs-firmware-tools habanalabs-rdma-core habanalabs-thunk habanalabs-tools habanalabs-qual habanalabs-perf-test; do
  find /var/cache/apt/archives -maxdepth 1 -name "${package}_1.24.1-482_*.deb" -exec cp -- '{}' "$stage/debs/" \;
done
test "$(find "$stage/debs" -name '*.deb' | wc -l)" -ge 9
if [ -n "${OFFLINE_PACKAGE_DIR:-}" ]; then
  virt-customize -a "$stage/system.qcow2" --no-network --copy-in "$stage/debs:/tmp" \
    --run-command 'cp -f /tmp/debs/*.deb /var/cache/apt/archives/; dpkg --unpack /tmp/debs/*.deb || true; DEBIAN_FRONTEND=noninteractive apt-get --no-download --no-remove --fix-broken -y install; dpkg --configure -a' \
    --run-command 'install -d -m 0755 /etc/docker; printf "%s\n" "{\"runtimes\":{\"habana\":{\"path\":\"/usr/bin/habana-container-runtime\",\"runtimeArgs\":[]}}}" > /etc/docker/daemon.json; systemctl enable docker || true; systemctl enable containerd || true' \
    --run-command 'printf "GRUB_CMDLINE_LINUX=\"iommu=1 intel_iommu=on pci=nocrs,realloc,assign-busses mmio_size=32G\"\n" > /etc/default/grub.d/99-gaudi.cfg; update-grub' \
    --run-command 'mkdir -p /etc/systemd/system/multi-user.target.wants && ln -sf /lib/systemd/system/qemu-guest-agent.service /etc/systemd/system/multi-user.target.wants/qemu-guest-agent.service && systemctl enable ssh; cloud-init clean --logs --machine-id' \
    --run-command 'rm -f /etc/ssh/ssh_host_* /root/.ssh/authorized_keys; rm -rf /var/lib/cloud/instances /tmp/debs; apt-get clean'
else
  virt-customize -a "$stage/system.qcow2" --network --copy-in "$stage/debs:/tmp" \
    --run-command 'apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y qemu-guest-agent openssh-server cloud-init cloud-guest-utils build-essential dkms linux-headers-6.8.0-139-generic linux-modules-extra-6.8.0-139-generic python3-pip python3-venv docker.io ca-certificates curl git && apt-get install -y /tmp/debs/*.deb' \
    --run-command 'install -d -m 0755 /etc/docker; printf "%s\n" "{\"runtimes\":{\"habana\":{\"path\":\"/usr/bin/habana-container-runtime\",\"runtimeArgs\":[]}}}" > /etc/docker/daemon.json; systemctl enable docker || true; systemctl enable containerd || true' \
    --run-command 'printf "GRUB_CMDLINE_LINUX=\"iommu=1 intel_iommu=on pci=nocrs,realloc,assign-busses mmio_size=32G\"\n" > /etc/default/grub.d/99-gaudi.cfg; update-grub' \
    --run-command 'mkdir -p /etc/systemd/system/multi-user.target.wants && ln -sf /lib/systemd/system/qemu-guest-agent.service /etc/systemd/system/multi-user.target.wants/qemu-guest-agent.service && systemctl enable ssh; cloud-init clean --logs --machine-id' \
    --run-command 'rm -f /etc/ssh/ssh_host_* /root/.ssh/authorized_keys; rm -rf /var/lib/cloud/instances /tmp/debs; apt-get clean'
fi
virt-customize -a "$stage/system.qcow2" --no-network \
  --mkdir /shared \
  --upload "$script_dir/shared-storage/1cat-mount-shared:/usr/local/sbin/1cat-mount-shared" \
  --chmod 0755:/usr/local/sbin/1cat-mount-shared \
  --upload "$script_dir/shared-storage/1cat-shared-storage.service:/etc/systemd/system/1cat-shared-storage.service" \
  --chmod 0644:/etc/systemd/system/1cat-shared-storage.service \
  --run-command 'systemctl enable 1cat-shared-storage.service'
qemu-img check "$stage/system.qcow2"
install -m 0440 -o libvirt-qemu -g kvm "$stage/system.qcow2" "$target"
printf '%s\n' '{"validated":false,"candidate":true,"driver":"1.24.1-482","base":"production-image-copy","checks":["python3-pip","python3-venv","docker-cli","habanalabs-container-runtime","habanalabs-qual","render-membership","hl-smi-operational","hbm-98304-mb","ssh-banner"]}' > "$manifest"
chown root:root "$manifest"
chmod 0640 "$manifest"
printf '%s\n' "Candidate image prepared at $target; NOT ENABLED. Validate a single-card guest before switching production."
