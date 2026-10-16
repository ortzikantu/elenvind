#!/usr/bin/env bash
# 在 CI runner 上装一个支持 `http2 on;` 的 nginx（>= 1.25.1），并断言版本。
#
# 为什么需要它：GitHub 的 ubuntu-latest 自带发行版 nginx（24.04 上是 1.24），
# 而 `http2 on;` 是 1.25.1 起才有的指令 —— 用 runner 自带的版本跑测试，会得到
# `unknown directive "http2"`，那是环境太旧，不是配置写错。CI 与发布流水线的
# verify 任务都调用本脚本，保证两边环境一致。
#
# 失败即退出（set -e）：装不上或版本太旧都要**明确报错**，不能静默退回旧版本。

set -euo pipefail

sudo apt-get remove -y --purge nginx nginx-common nginx-core || true
sudo systemctl mask nginx 2>/dev/null || true

curl -fsSL https://nginx.org/keys/nginx_signing.key \
  | sudo gpg --dearmor -o /usr/share/keyrings/nginx-archive-keyring.gpg

# shellcheck disable=SC1091
. /etc/os-release
echo "deb [signed-by=/usr/share/keyrings/nginx-archive-keyring.gpg] http://nginx.org/packages/mainline/ubuntu ${UBUNTU_CODENAME:-$VERSION_CODENAME} nginx" \
  | sudo tee /etc/apt/sources.list.d/nginx.list >/dev/null

sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nginx

python3 - <<'PY'
import subprocess

result = subprocess.run(["nginx", "-v"], capture_output=True, text=True)
version = (result.stderr + result.stdout).split("nginx/")[1].split()[0]
parts = tuple(int(part) for part in version.split("."))
print(f"nginx {version}")
assert parts >= (1, 25, 1), (
    f"CI 的 nginx 太旧：{version} —— 本项目的示例配置面向支持 `http2 on;` 的环境，"
    "请确认 nginx.org 的包真的装上了（而不是回退到发行版版本）")
PY
