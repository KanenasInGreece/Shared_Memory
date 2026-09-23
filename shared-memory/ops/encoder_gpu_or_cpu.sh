#!/bin/sh
# Compose passes the GPU llama-server arguments, including flash attention.
# Vulkan with flash attention on keeps the compute buffer small (measured
# on the RX 580 and the Arc B580). The same flag on CPU raises resident
# memory, so a GPU crash other than SIGTERM/SIGINT restarts this container's
# server on CPU with flash attention removed and layer offload set to 0.
# The port does not move, so the CPU pair is not started beside it.
server="${LLAMA_SERVER:-/app/llama-server}"
pid=""
term() {
  if [ -n "$pid" ]; then
    kill -TERM "$pid" 2>/dev/null
    wait "$pid" 2>/dev/null
  fi
  exit 0
}
trap term TERM INT

"$server" "$@" &
pid=$!
wait "$pid"
status=$?
if [ "$status" -eq 143 ] || [ "$status" -eq 130 ] || [ "$status" -eq 0 ]; then
  exit "$status"
fi
echo "GPU encoder exited $status; CPU failover" >&2

args_file="/tmp/encoder-cpu-args.$$"
: > "$args_file"
skip=0
for arg in "$@"; do
  if [ "$skip" = 1 ]; then
    skip=0
    continue
  fi
  case "$arg" in
    -fa|--flash-attn)
      skip=1
      ;;
    -ngl|--n-gpu-layers)
      printf '%s\n' "$arg" >> "$args_file"
      printf '%s\n' 0 >> "$args_file"
      skip=1
      ;;
    *)
      printf '%s\n' "$arg" >> "$args_file"
      ;;
  esac
done

set --
while IFS= read -r line; do
  set -- "$@" "$line"
done < "$args_file"
pid=""
exec "$server" "$@"
