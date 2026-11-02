#!/usr/bin/env bash
# Deploy từ máy local tới Linux server qua SSH. Nhập cấu hình khi chạy.
set -euo pipefail
DEPLOY_STAGE='kiểm tra cấu hình'
trap 'status=$?; echo "Deploy thất bại ở bước: $DEPLOY_STAGE (exit $status)." >&2; exit "$status"' ERR

SSH_HOST="${SSH_HOST:-45.77.252.226}"     # IP hoặc alias trong ~/.ssh/config
SSH_USER="${SSH_USER-root}"              # root là tên user, không phải biến $root
SSH_PORT="${SSH_PORT:-22}"
SSH_KEY="${SSH_KEY:-}"                   # Ví dụ: /Users/you/.ssh/id_ed25519; trống = SSH mặc định
REMOTE_DIR="${REMOTE_DIR:-/opt/kronos}"   # Đường dẫn tuyệt đối, SSH user cần quyền ghi
PYTHON_BIN="${PYTHON_BIN:-python3}"       # Python >= 3.10 trên server
INSTALL_DEPS="${INSTALL_DEPS:-1}"         # 0 = chỉ đồng bộ source
POST_DEPLOY_COMMAND="${POST_DEPLOY_COMMAND-systemctl restart kronos-api && systemctl is-active --quiet kronos-api}"

usage() {
    echo 'Cách dùng: bash scripts/deploy.sh [--dry-run] [--non-interactive]'
    echo 'Nhập cấu hình khi chạy trên terminal; Enter để giữ giá trị mặc định.'
    echo 'Dùng --non-interactive và biến môi trường khi chạy tự động.'
}
DRY_RUN=0
INTERACTIVE=1
for argument in "$@"; do
    case "$argument" in
        --dry-run) DRY_RUN=1 ;;
        --non-interactive) INTERACTIVE=0 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; exit 1 ;;
    esac
done

prompt_value() {
    local variable="$1" label="$2" answer
    read -r -p "$label [${!variable}]: " answer || {
        echo 'Không đọc được input; đã hủy deploy.' >&2
        exit 1
    }
    if [[ -n "$answer" ]]; then printf -v "$variable" '%s' "$answer"; fi
}
if [[ "$INTERACTIVE" == 1 && -t 0 ]]; then
    echo 'Nhập thông tin deploy. Enter để giữ giá trị trong ngoặc vuông.'
    prompt_value SSH_HOST 'SSH host / IP / alias (bắt buộc)'
    prompt_value SSH_USER 'SSH user (trống = user từ SSH config)'
    prompt_value SSH_PORT 'SSH port'
    prompt_value SSH_KEY 'Đường dẫn SSH private key (trống = SSH mặc định)'
    prompt_value REMOTE_DIR 'Thư mục deploy trên server'
fi
# read không tự mở rộng ~ như shell khi gán trực tiếp.
if [[ "$SSH_KEY" == '~/'* ]]; then SSH_KEY="$HOME/${SSH_KEY:2}"; fi
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
# REMOTE_DIR đã giới hạn ký tự an toàn; hỗ trợ cả rsync cũ trên macOS.
RSYNC_ARGS=(-az --itemize-changes
    --exclude='learning_state/'
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
    DEPLOY_STAGE='kết nối SSH và tạo thư mục đích (kiểm tra quyền ghi)'
    ssh "${SSH_ARGS[@]}" "$SSH_HOST" "mkdir -p -- '$REMOTE_DIR'"
fi
# Không dùng --delete: giữ dữ liệu và file riêng trên server.
DEPLOY_STAGE='đồng bộ source bằng rsync'
rsync "${RSYNC_ARGS[@]}" -e "$SSH_TRANSPORT" "$SOURCE_DIR/" "$SSH_HOST:$REMOTE_DIR/"
if (( DRY_RUN )); then exit 0; fi
echo 'Đã đồng bộ source lên server.'

DEPLOY_STAGE='cài dependencies hoặc chạy POST_DEPLOY_COMMAND trên server'
printf -v REMOTE_ARGS '%q ' "$REMOTE_DIR" "$PYTHON_BIN" "$INSTALL_DEPS" "$POST_DEPLOY_COMMAND"
ssh "${SSH_ARGS[@]}" "$SSH_HOST" "bash -s -- $REMOTE_ARGS" <<'REMOTE'
set -euo pipefail
REMOTE_STAGE='chuẩn bị môi trường'
trap 'status=$?; echo "Lỗi trên server ở bước: $REMOTE_STAGE (exit $status)." >&2; exit "$status"' ERR
cd "$1"
if [[ "$3" == 1 ]]; then
    REMOTE_STAGE='cài và kiểm tra dependencies'
    "$2" -c 'import sys; assert sys.version_info >= (3, 10), "Cần Python >= 3.10"'
    if [[ ! -x .venv/bin/python ]]; then "$2" -m venv .venv; fi
    .venv/bin/python -m pip install -r requirements.txt -r webui/requirements.txt
    .venv/bin/python -m pip check
    .venv/bin/python -m webui.api_token
fi
if [[ -n "$4" ]]; then
    REMOTE_STAGE='POST_DEPLOY_COMMAND'
    echo 'Đang chạy POST_DEPLOY_COMMAND trên server...'
    # Lệnh do người deploy cấu hình, chạy từ thư mục project.
    bash -e -o pipefail -c "$4"
    echo 'POST_DEPLOY_COMMAND đã hoàn tất thành công.'
else
    echo 'POST_DEPLOY_COMMAND rỗng: không restart API.'
fi
REMOTE
echo 'Deploy hoàn tất.'
