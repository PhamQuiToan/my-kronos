#!/usr/bin/env bash
# Deploy từ máy local tới Linux server qua SSH. Điền cấu hình dưới đây.
set -euo pipefail

SSH_HOST="${SSH_HOST:-}"                 # Ví dụ: 203.0.113.10 hoặc alias trong ~/.ssh/config
SSH_USER="${SSH_USER:-}"                 # Ví dụ: ubuntu; để trống nếu dùng SSH alias
SSH_PORT="${SSH_PORT:-22}"
SSH_KEY="${SSH_KEY:-}"                   # Ví dụ: /Users/you/.ssh/id_ed25519; trống = SSH mặc định
REMOTE_DIR="${REMOTE_DIR:-/opt/kronos}"   # Đường dẫn tuyệt đối, SSH user cần quyền ghi
PYTHON_BIN="${PYTHON_BIN:-python3}"       # Python >= 3.10 trên server
INSTALL_DEPS="${INSTALL_DEPS:-1}"         # 0 = chỉ đồng bộ source
POST_DEPLOY_COMMAND="${POST_DEPLOY_COMMAND:-}" # Ví dụ: sudo -n systemctl restart kronos-api

usage() {
    echo 'Cách dùng: bash scripts/deploy.sh [--dry-run]'
    echo 'Điền cấu hình SSH trong script hoặc truyền qua biến môi trường.'
}
DRY_RUN=0
case "${1:-}" in
    '') ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; exit 1 ;;
esac
[[ $# -le 1 ]] || { usage >&2; exit 1; }
[[ -n "$SSH_HOST" && "$SSH_HOST" != -* ]] || { echo 'Hãy điền SSH_HOST.' >&2; exit 1; }
[[ "$SSH_PORT" =~ ^[0-9]+$ ]] && (( SSH_PORT >= 1 && SSH_PORT <= 65535 )) || { echo 'SSH_PORT không hợp lệ.' >&2; exit 1; }
[[ "$REMOTE_DIR" =~ ^/[a-zA-Z0-9_./-]+$ && "$REMOTE_DIR" != / && "$REMOTE_DIR" != *..* ]] || { echo 'REMOTE_DIR phải là đường dẫn tuyệt đối, không có khoảng trắng hoặc dấu ..' >&2; exit 1; }
[[ "$INSTALL_DEPS" == 0 || "$INSTALL_DEPS" == 1 ]] || { echo 'INSTALL_DEPS phải là 0 hoặc 1.' >&2; exit 1; }
for command in ssh rsync; do
    command -v "$command" >/dev/null || { echo "Thiếu lệnh: $command" >&2; exit 1; }
done
SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SSH_ARGS=(-p "$SSH_PORT")
if [[ -n "$SSH_KEY" ]]; then
    [[ -f "$SSH_KEY" ]] || { echo "Không tìm thấy SSH_KEY: $SSH_KEY" >&2; exit 1; }
    SSH_ARGS+=(-i "$SSH_KEY")
fi
if [[ -n "$SSH_USER" ]]; then SSH_ARGS+=(-l "$SSH_USER"); fi
printf -v SSH_TRANSPORT '%q ' ssh "${SSH_ARGS[@]}"
RSYNC_ARGS=(-az --itemize-changes --protect-args
    --exclude='.git/' --exclude='.venv/' --exclude='venv/'
    --exclude='__pycache__/' --exclude='*.pyc' --exclude='.DS_Store'
    --exclude='.env' --exclude='.env.*' --exclude='.api_token*'
    --exclude='*.pem' --exclude='*.key' --exclude='deploy.sh'
    --exclude='logs/' --exclude='*.log' --exclude='forecast_result/'
    --exclude='prediction_results/' --exclude='future_prediction*.csv'
    --exclude='prediction_result.csv')

echo "Deploy $SOURCE_DIR -> $SSH_HOST:$REMOTE_DIR"
if (( DRY_RUN )); then
    echo 'Dry run: không tạo thư mục, cài dependencies hoặc chạy lệnh trên server.'
    RSYNC_ARGS+=(--dry-run)
else
    ssh "${SSH_ARGS[@]}" "$SSH_HOST" "mkdir -p -- '$REMOTE_DIR'"
fi
# Không dùng --delete: giữ dữ liệu và file riêng trên server.
rsync "${RSYNC_ARGS[@]}" -e "$SSH_TRANSPORT" "$SOURCE_DIR/" "$SSH_HOST:$REMOTE_DIR/"
if (( DRY_RUN )); then exit 0; fi

printf -v REMOTE_ARGS '%q ' "$REMOTE_DIR" "$PYTHON_BIN" "$INSTALL_DEPS" "$POST_DEPLOY_COMMAND"
ssh "${SSH_ARGS[@]}" "$SSH_HOST" "bash -s -- $REMOTE_ARGS" <<'REMOTE'
set -euo pipefail
cd "$1"
if [[ "$3" == 1 ]]; then
    "$2" -c 'import sys; assert sys.version_info >= (3, 10), "Cần Python >= 3.10"'
    if [[ ! -x .venv/bin/python ]]; then "$2" -m venv .venv; fi
    .venv/bin/python -m pip install -r requirements.txt -r webui/requirements.txt
    .venv/bin/python -m pip check
    .venv/bin/python -m webui.api_token
fi
if [[ -n "$4" ]]; then
    # Lệnh do người deploy cấu hình, chạy từ thư mục project.
    bash -e -o pipefail -c "$4"
fi
REMOTE
echo 'Deploy hoàn tất.'
