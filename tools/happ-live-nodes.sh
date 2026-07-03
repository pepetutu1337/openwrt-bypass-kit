#!/bin/sh
# ============================================================
#  happ-live-nodes.sh — Дека-сайд монитор.
#  Показывает, к каким нодам Happ (xray) подключён ПРЯМО СЕЙЧАС,
#  и есть ли эти сервера на роутере. Нужен, чтобы знать, КОГДА
#  обновлять ноды на роутере (полностью автоматом из Happ нельзя —
#  подписка/конфиг зашифрованы, см. README).
#
#  Запуск:  sh happ-live-nodes.sh
#  (внутри спросит sudo-пароль ОДИН раз — только чтобы прочитать соединения
#   xray, который работает от root; ssh к роутеру идёт от твоего пользователя)
# ============================================================
KIT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
[ -f "$KIT_DIR/config/kit.conf" ] && . "$KIT_DIR/config/kit.conf"
: "${ROUTER_IP:=192.168.1.1}"; : "${ROUTER_SSH_USER:=root}"; : "${ROUTER_SSH_PORT:=22}"
SSH="ssh -p $ROUTER_SSH_PORT -o ConnectTimeout=8 ${ROUTER_SSH_USER}@${ROUTER_IP}"

echo ":: живые ноды Happ (к чему xray подключён сейчас)"
live=$(sudo ss -tnp 2>/dev/null | grep -E 'ESTAB.*xray' | awk '{print $5}' | grep -vE '^127\.|^\[::1\]' | sort -u)
[ -n "$live" ] || { echo "  Happ не подключён / не запущен"; exit 1; }

# ноды, которые знает роутер (IP из его sing-box)
routed=$($SSH 'grep -oE "\"server\": *\"[0-9.]+\"" /etc/sing-box/config.json 2>/dev/null | grep -oE "[0-9.]+"' 2>/dev/null)

miss=0
for ep in $live; do
  ip="${ep%:*}"
  if printf '%s\n' "$routed" | grep -qx "$ip"; then
    echo "  ✓ $ep — есть на роутере"
  else
    echo "  ⚠ $ep — на роутере НЕТ (обнови: возьми ссылку из Happ «поделиться» → netctl nodes add-url)"
    miss=1
  fi
done
echo
if [ "$miss" = 0 ]; then
  echo ":: всё синхронно — роутер знает ноды, которыми Happ пользуется сейчас."
else
  echo ":: Happ нашёл ноду, которой у роутера нет. Как обновить:"
  echo "   1) в Happ у нужного сервера — «Поделиться» / «Share» → скопируй vless:// ссылку"
  echo "   2) netctl nodes add-url 'vless://...'   (или впиши в config/nodes.list + netctl apply)"
fi
