#!/bin/bash
set -euo pipefail

echo "=== Phase 0: Bootstrap ==="

# 1. Homebrew
if ! command -v brew &>/dev/null; then
    echo "Installing Homebrew..."
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile
    eval "$(/opt/homebrew/bin/brew shellenv)"
fi

# 2. Brewfile
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
echo "Installing packages from Brewfile..."
brew bundle --file="$REPO_ROOT/Brewfile"

# 3. Python 3.12 (Brewfile で入るが明示的に確認)
echo "Verifying Python 3.12..."
python3.12 --version

# 4. /opt 配下の所有権準備（macOS の /opt は root 所有のため、一度だけ sudo で用意）
#    これが無いと uv venv / pyinfra の files.directory が Permission denied で失敗する。
#    既に両ディレクトリが存在し実行ユーザー所有なら冪等にスキップする
#    （再実行・非対話シェルで sudo パスワード要求により停止しないため）。
if [ -d /opt/taka-ma ] && [ -d /opt/taka-ma-env ] \
   && [ -O /opt/taka-ma ] && [ -O /opt/taka-ma-env ]; then
    echo "/opt ownership already prepared (skip sudo)."
else
    echo "Preparing /opt ownership (sudo)..."
    sudo mkdir -p /opt/taka-ma /opt/taka-ma-env
    sudo chown -R "$(whoami)" /opt/taka-ma /opt/taka-ma-env
fi

# 5. Pyinfra 用の仮想環境
echo "Creating Pyinfra environment..."
uv venv /opt/taka-ma-env --python 3.12
/opt/taka-ma-env/bin/python -m ensurepip
ln -sf pip3 /opt/taka-ma-env/bin/pip
source /opt/taka-ma-env/bin/activate

# 5. Pyinfra (pip install)
echo "Installing Pyinfra..."
uv pip install pyinfra

echo "=== Phase 0 完了 ==="
echo "次のステップ: pyinfra -y @local pyinfra/deploys/common.py（構築手順書 docs/procedures/01-common-base.md Step 2）"
