"""SSH トンネル / Tailscale 構築デプロイ。

構築手順書: docs/procedures/02-ssh-tunnel.md（Pyinfra対応）
"""

from pyinfra.operations import brew, files, server
from pyinfra import host

# Tailscale インストール
brew.casks(casks=["tailscale"], present=True)

# SSH鍵の配置（双方向）
files.put(
    src="keys/taka-ma-cluster",
    dest="~/.ssh/taka-ma-cluster",
    mode="600",
)
files.put(
    src="keys/taka-ma-cluster.pub",
    dest="~/.ssh/taka-ma-cluster.pub",
    mode="644",
)

# SSH config の配置（マシンごとに異なるテンプレート）
files.template(
    src=f"templates/ssh_config_{host.data.role}.j2",
    dest="~/.ssh/config",
    mode="600",
)

# sshd_config.d ディレクトリ作成
files.directory(path="/etc/ssh/sshd_config.d", present=True, sudo=True)

# Include ディレクティブの確認・追加
server.shell(
    commands=[
        'grep -q "Include /etc/ssh/sshd_config.d" /etc/ssh/sshd_config || '
        'echo "Include /etc/ssh/sshd_config.d/*.conf" | sudo tee -a /etc/ssh/sshd_config',
    ],
)

# クラスター専用SSH設定（Include方式）
files.template(
    src="templates/taka-ma-cluster-sshd.conf.j2",
    dest="/etc/ssh/sshd_config.d/taka-ma-cluster.conf",
    sudo=True,
)

# sshd 再起動
server.shell(
    commands=["sudo launchctl kickstart -kp system/com.openssh.sshd"],
)

# 静的IP設定（10GbE直結用）
server.shell(
    commands=[
        "networksetup -setmanual '{{ interface_name }}' {{ static_ip }} 255.255.255.252 ''"
    ],
)
