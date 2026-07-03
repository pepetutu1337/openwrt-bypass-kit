#!/bin/sh
# ============================================================
#  xray-json-to-vless.sh — конвертит экспортированный из Happ
#  xray-JSON (файл ИЛИ несколько) в vless:// ссылки (по одной на строку).
#  Happ часто отдаёт ноду не ссылкой, а таким JSON-конфигом — тут все параметры.
#  Использование:  sh xray-json-to-vless.sh файл1 [файл2 …]
#                  cat конфиг | sh xray-json-to-vless.sh -   (или без аргументов = stdin)
#  Требует jq.
# ============================================================
command -v jq >/dev/null 2>&1 || { echo "нужен jq" >&2; exit 1; }

# без файлов или '-' → читаем stdin (для вставки текста из буфера)
if [ $# -eq 0 ] || [ "$1" = "-" ]; then
  tmp=$(mktemp); cat > "$tmp"; set -- "$tmp"
fi

for f in "$@"; do
  [ -f "$f" ] || { echo "нет файла: $f" >&2; continue; }
  jq -r '
    .outbounds[]? | select(.protocol=="vless") as $o
    | ($o.settings.vnext[0]) as $v
    | ($v.users[0]) as $u
    | ($o.streamSettings // {}) as $s
    | (($s.realitySettings // $s.tlsSettings) // {}) as $t
    | ($s.network // "tcp") as $net
    | [ "vless://\($u.id)@\($v.address):\($v.port)?",
        "type=\($net)&security=\($s.security // "reality")",
        (if $t.serverName    then "&sni=\($t.serverName)"        else "" end),
        (if $t.publicKey     then "&pbk=\($t.publicKey)"         else "" end),
        (if ($t.shortId // "")!="" then "&sid=\($t.shortId)"     else "" end),
        (if $t.fingerprint   then "&fp=\($t.fingerprint)"        else "" end),
        (if (($u.flow // "")!="") and $net!="grpc" then "&flow=\($u.flow)" else "" end),
        (if $net=="grpc"     then "&serviceName=\($s.grpcSettings.serviceName // "vless")" else "" end),
        "#\(($o.tag // $v.address) | gsub("[[:space:]]";""))"
      ] | join("")
  ' "$f" 2>/dev/null
done | awk '!seen[$0]++'   # дедуп
