#!/bin/bash
#
# Local environment setup for ClusterShell tests.
#
# Run as root. Prepares the system so that the tree-mode test suite
# in tests/TreeWorkerTest.py can talk to 127.0.0.[2-7] over SSH as if
# they were remote nodes. This mirrors what .github/workflows/nosetests.yml
# does on the CI runner, plus a few extras for local idempotency.
#
# Usage:
#   sudo ./setup_env_root.sh [target_user]
#
# If target_user is omitted, falls back to $SUDO_USER.
#
# What it does:
#   1. Installs openssh-server, openssh-client, pdsh (apt / dnf / yum)
#   2. Enables and starts sshd
#   3. Sets /etc/pdsh/rcmd_default=ssh
#   4. As target_user:
#      - Generates ~/.ssh/id_rsa if missing
#      - Adds the pub key to ~/.ssh/authorized_keys (dedup)
#      - Appends an idempotent marker block to ~/.ssh/config that
#        disables host-key checking for 127.0.0.* and localhost only
#        (does NOT touch other Host entries)
#      - Adds tests/bin to PATH via ~/.bashrc (needed so that
#        `ssh 127.0.0.2 hostname` returns "127.0.0.2", per the
#        tests/bin/hostname override script)
#   5. Verifies `ssh 127.0.0.2 hostname` works
#
# The script is idempotent: re-running it is safe and will report
# "already done" for steps that have already been applied.
#
# To revert:
#   - Delete the block between
#       # clustershell-local-tests BEGIN ... END
#     in ~/.ssh/config and ~/.bashrc.
#   - Remove the pub key from ~/.ssh/authorized_keys if you want.
#   - apt remove pdsh (optional)
#

set -euo pipefail

# ---------- arg parsing ----------

if [[ $# -gt 1 ]]; then
    echo "Usage: sudo $0 [target_user]" >&2
    exit 2
fi

TARGET_USER="${1:-${SUDO_USER:-}}"
if [[ -z "$TARGET_USER" ]]; then
    echo "ERROR: cannot determine target user; pass it as an argument:" >&2
    echo "  sudo $0 <username>" >&2
    exit 2
fi

# ---------- sanity ----------

if [[ "$EUID" -ne 0 ]]; then
    echo "ERROR: this script must be run as root (use sudo)." >&2
    exit 1
fi

if ! id -u "$TARGET_USER" >/dev/null 2>&1; then
    echo "ERROR: user '$TARGET_USER' does not exist." >&2
    exit 1
fi

TARGET_HOME=$(getent passwd "$TARGET_USER" | cut -d: -f6)
if [[ ! -d "$TARGET_HOME" ]]; then
    echo "ERROR: home directory '$TARGET_HOME' for $TARGET_USER does not exist." >&2
    exit 1
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../.." &> /dev/null && pwd)"
TESTS_BIN="$REPO_ROOT/tests/bin"

log() { printf '[setup-env] %s\n' "$*"; }
warn() { printf '[setup-env] WARNING: %s\n' "$*" >&2; }
fail() { printf '[setup-env] ERROR: %s\n' "$*" >&2; exit 1; }
run_as_user() { sudo -u "$TARGET_USER" -H bash -c "$1"; }

log "Target user      : $TARGET_USER"
log "Target home      : $TARGET_HOME"
log "Repository root  : $REPO_ROOT"
log "tests/bin path   : $TESTS_BIN"

[[ -x "$TESTS_BIN/hostname" ]] || warn "$TESTS_BIN/hostname not found or not executable"

# ---------- step 1: install packages ----------

log "Installing openssh-server, openssh-client, pdsh..."
if command -v apt-get >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    # pdsh on Debian/Ubuntu ships with the ssh module bundled
    apt-get install -y -qq openssh-server openssh-client pdsh
elif command -v dnf >/dev/null 2>&1; then
    # On Rocky/RHEL, pdsh-rcmd-ssh is a separate package and is needed for
    # `pdsh -R ssh` to work. Without it pdsh defaults to rsh.
    dnf install -y -q openssh-server openssh-clients pdsh pdsh-rcmd-ssh
elif command -v yum >/dev/null 2>&1; then
    yum install -y -q openssh-server openssh-clients pdsh pdsh-rcmd-ssh
else
    warn "no supported package manager found; skipping installs"
fi

# ---------- step 2: enable sshd ----------

SSHD_UNIT=""
for unit in ssh sshd; do
    if systemctl list-unit-files "$unit.service" 2>/dev/null | grep -q "^$unit.service"; then
        SSHD_UNIT=$unit
        break
    fi
done

if [[ -n "$SSHD_UNIT" ]]; then
    log "Enabling and starting $SSHD_UNIT..."
    systemctl enable --now "$SSHD_UNIT"
else
    warn "could not find an sshd systemd unit; start it manually if needed"
fi

# ---------- step 3: configure pdsh default rcmd module ----------
#
# pdsh's default rcmd module is set differently on different distros:
#   - Debian/Ubuntu pdsh has a patch that reads /etc/pdsh/rcmd_default
#   - Rocky/RHEL/Fedora pdsh ignores that file; the standard mechanism
#     is the PDSH_RCMD_TYPE env var (we set it in user .bashrc, step 5).
# We write the file anyway: harmless on RHEL, picked up on Debian.
if command -v pdsh >/dev/null 2>&1; then
    mkdir -p /etc/pdsh
    current=$(cat /etc/pdsh/rcmd_default 2>/dev/null || true)
    if [[ "$current" != "ssh" ]]; then
        log "Writing /etc/pdsh/rcmd_default = ssh (Debian path; harmless elsewhere)"
        echo ssh > /etc/pdsh/rcmd_default
    else
        log "/etc/pdsh/rcmd_default already = ssh"
    fi
else
    warn "pdsh not on PATH; skipping rcmd_default setup"
fi

# ---------- step 4: home perms ----------

log "Tightening $TARGET_HOME perms for sshd..."
chmod og-rw "$TARGET_HOME"

# ---------- step 5: user-side SSH setup ----------

log "Configuring SSH for $TARGET_USER (key, authorized_keys, ssh config, PATH)..."

# Use a quoted heredoc so root's shell does NO expansion on the body.
# The user-side script takes TESTS_BIN as its single argument.
USER_SCRIPT=$(mktemp -t cs-setup-user-XXXXXX.sh)
trap 'rm -f "$USER_SCRIPT"' EXIT

cat > "$USER_SCRIPT" <<'USERSCRIPT'
#!/bin/bash
set -euo pipefail
TESTS_BIN="$1"
BEGIN='# clustershell-local-tests BEGIN'
END='# clustershell-local-tests END'

SSH_DIR="$HOME/.ssh"
mkdir -p "$SSH_DIR"
chmod 700 "$SSH_DIR"

# (a) keypair
if [[ ! -f "$SSH_DIR/id_rsa" ]]; then
    echo '[setup-env]   generating new RSA key at ~/.ssh/id_rsa'
    ssh-keygen -f "$SSH_DIR/id_rsa" -N '' -t rsa -q
else
    echo '[setup-env]   reusing existing ~/.ssh/id_rsa'
fi

# (b) authorize own pub key
PUBKEY=$(cat "$SSH_DIR/id_rsa.pub")
touch "$SSH_DIR/authorized_keys"
chmod 600 "$SSH_DIR/authorized_keys"
if ! grep -qxF "$PUBKEY" "$SSH_DIR/authorized_keys"; then
    echo "$PUBKEY" >> "$SSH_DIR/authorized_keys"
    echo '[setup-env]   appended pub key to ~/.ssh/authorized_keys'
else
    echo '[setup-env]   pub key already in ~/.ssh/authorized_keys'
fi

# (c) ssh config block (scoped to 127.0.0.* and localhost only)
CONFIG_FILE="$SSH_DIR/config"
touch "$CONFIG_FILE"
chmod 600 "$CONFIG_FILE"
if ! grep -qF "$BEGIN" "$CONFIG_FILE"; then
    {
        echo ''
        echo "$BEGIN"
        echo 'Host 127.0.0.* localhost'
        echo '    StrictHostKeyChecking no'
        echo '    UserKnownHostsFile /dev/null'
        echo '    LogLevel ERROR'
        echo "$END"
    } >> "$CONFIG_FILE"
    echo '[setup-env]   appended marker block to ~/.ssh/config'
else
    echo '[setup-env]   ~/.ssh/config already has marker block'
fi

# (d) Put tests/bin on PATH via ~/.bashrc, *prepended* so it runs BEFORE
#     the standard non-interactive early-return guard that most distro
#     bashrc files have ('case $- in *i*) ;; *) return ;; esac').
#     This is the same trick .github/workflows/nosetests.yml uses with sed 1i.
#     For idempotency: strip any existing marker block first, then prepend.
BASHRC="$HOME/.bashrc"
touch "$BASHRC"
if grep -qF "$BEGIN" "$BASHRC"; then
    sed -i "/$BEGIN/,/$END/d" "$BASHRC"
fi
TMP=$(mktemp)
{
    echo "$BEGIN"
    # Literal $PATH so it expands each time .bashrc is sourced.
    echo "export PATH=\"$TESTS_BIN:\$PATH\""
    # PDSH_RCMD_TYPE is the standard cross-distro way to default pdsh to ssh,
    # in case the upstream pdsh-backed tests are run (TaskDistantPdshTest.py).
    echo 'export PDSH_RCMD_TYPE=ssh'
    echo "$END"
    echo ''
    cat "$BASHRC"
} > "$TMP"
mv "$TMP" "$BASHRC"
echo '[setup-env]   prepended PATH + PDSH_RCMD_TYPE block to ~/.bashrc'
USERSCRIPT

chmod 755 "$USER_SCRIPT"
sudo -u "$TARGET_USER" -H bash "$USER_SCRIPT" "$TESTS_BIN"

# ---------- step 6: verify ----------

log "Verifying loopback SSH..."
ok_count=0
fail_count=0
for ip in 127.0.0.2 127.0.0.3 127.0.0.4 127.0.0.5 127.0.0.6 127.0.0.7; do
    if run_as_user "ssh -o ConnectTimeout=5 -o BatchMode=yes $ip true" >/dev/null 2>&1; then
        ok_count=$((ok_count + 1))
    else
        fail_count=$((fail_count + 1))
        warn "ssh $ip failed"
    fi
done
log "Loopback SSH check: $ok_count ok / $fail_count failed"

# Also verify the hostname-override roundtrip
if [[ -x "$TESTS_BIN/hostname" ]]; then
    got=$(run_as_user "ssh -o BatchMode=yes 127.0.0.2 hostname" 2>/dev/null || echo "")
    if [[ "$got" == "127.0.0.2" ]]; then
        log "hostname-override roundtrip: OK (got '$got')"
    else
        warn "hostname-override roundtrip returned '$got' (expected '127.0.0.2');"
        warn "the user's interactive shell probably loads ~/.bashrc, but sshd's"
        warn "non-interactive command shell may not. If that's the case, ensure"
        warn "the user's ~/.bash_profile sources ~/.bashrc, or move the PATH"
        warn "export into ~/.ssh/environment with PermitUserEnvironment yes."
    fi
fi

# ---------- summary ----------

cat <<EOF

================================================================
Setup complete for user '$TARGET_USER'.

Next steps (run as $TARGET_USER, NOT as root):

  cd $REPO_ROOT
  source .venv/bin/activate
  export CLUSTERSHELL_GW_PYTHON_EXECUTABLE=\$(which python)
  pytest tests/TreeWorkerTest.py \\
         --cov=ClusterShell.Worker.Tree --cov-branch \\
         --cov-report=term-missing

To revert:
  - Delete the block between
      # clustershell-local-tests BEGIN ... END
    in $TARGET_HOME/.ssh/config and $TARGET_HOME/.bashrc
  - Optionally remove the pub key from authorized_keys
  - apt remove pdsh (if you don't need it elsewhere)
================================================================
EOF
