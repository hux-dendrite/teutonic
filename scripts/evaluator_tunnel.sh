#!/usr/bin/env bash
set -euo pipefail

# The validator-host PM2 process supplies all connection values from .env.
required=(
  TEUTONIC_EVAL_SSH_HOST
  TEUTONIC_EVAL_SSH_USER
)
for name in "${required[@]}"; do
  if [[ -z "${!name:-}" ]]; then
    echo "$name is required" >&2
    exit 1
  fi
done

ssh_host="$TEUTONIC_EVAL_SSH_HOST"
ssh_user="$TEUTONIC_EVAL_SSH_USER"
ssh_port="${TEUTONIC_EVAL_SSH_PORT:-22}"
local_host="${TEUTONIC_EVAL_TUNNEL_LOCAL_HOST:-127.0.0.1}"
local_port="${TEUTONIC_EVAL_TUNNEL_LOCAL_PORT:-9000}"
remote_host="${TEUTONIC_EVAL_TUNNEL_REMOTE_HOST:-127.0.0.1}"
remote_port="${TEUTONIC_EVAL_TUNNEL_REMOTE_PORT:-9000}"

if [[ ! "$ssh_host" =~ ^[A-Za-z0-9._:-]+$ ]]; then
  echo "TEUTONIC_EVAL_SSH_HOST contains unsupported characters" >&2
  exit 1
fi
if [[ ! "$ssh_user" =~ ^[A-Za-z0-9._-]+$ ]]; then
  echo "TEUTONIC_EVAL_SSH_USER contains unsupported characters" >&2
  exit 1
fi
if [[ ! "$remote_host" =~ ^[A-Za-z0-9._:-]+$ ]]; then
  echo "TEUTONIC_EVAL_TUNNEL_REMOTE_HOST contains unsupported characters" >&2
  exit 1
fi
if [[ "$local_host" != "127.0.0.1" && "$local_host" != "::1" && "$local_host" != "localhost" ]]; then
  echo "TEUTONIC_EVAL_TUNNEL_LOCAL_HOST must be loopback" >&2
  exit 1
fi
for port in "$ssh_port" "$local_port" "$remote_port"; do
  if [[ ! "$port" =~ ^[0-9]+$ ]] || ((port < 1 || port > 65535)); then
    echo "SSH tunnel ports must be integers from 1 through 65535" >&2
    exit 1
  fi
done

ssh_args=(
  -N
  -L "${local_host}:${local_port}:${remote_host}:${remote_port}"
  -p "$ssh_port"
  -o BatchMode=yes
  -o ConnectTimeout="${TEUTONIC_EVAL_SSH_CONNECT_TIMEOUT_SECONDS:-15}"
  -o ExitOnForwardFailure=yes
  -o ServerAliveInterval="${TEUTONIC_EVAL_SSH_SERVER_ALIVE_INTERVAL_SECONDS:-30}"
  -o ServerAliveCountMax="${TEUTONIC_EVAL_SSH_SERVER_ALIVE_COUNT_MAX:-3}"
  -o StrictHostKeyChecking="${TEUTONIC_EVAL_SSH_STRICT_HOST_KEY_CHECKING:-accept-new}"
)

if [[ -n "${TEUTONIC_EVAL_SSH_IDENTITY_FILE:-}" ]]; then
  ssh_args+=( -i "$TEUTONIC_EVAL_SSH_IDENTITY_FILE" )
fi
if [[ -n "${TEUTONIC_EVAL_SSH_KNOWN_HOSTS_FILE:-}" ]]; then
  ssh_args+=( -o "UserKnownHostsFile=${TEUTONIC_EVAL_SSH_KNOWN_HOSTS_FILE}" )
fi

exec ssh "${ssh_args[@]}" "${ssh_user}@${ssh_host}"
