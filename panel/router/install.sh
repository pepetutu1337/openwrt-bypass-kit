#!/bin/sh
# Ставит панель на САМ роутер: rctl + CGI + статика под uhttpd.
# Запускается с Дека, ходит по SSH. Читает panel.conf из папки panel/.
#
#   ./router/install.sh                 — поставить без пароля (только домашняя сеть)
#   ./router/install.sh --pass СЕКРЕТ   — поставить с паролем на вход
#   ./router/install.sh --no-cron       — без авто-починки по расписанию
#   ./router/install.sh --bot-token T --bot-allow "ID ID"  — включить Telegram-бота
#   ./router/install.sh --uninstall     — снести всё, что поставили
#
# Панель слушает ТОЛЬКО LAN-адрес роутера, наружу не торчит.
set -eu

DIR="$(cd "$(dirname "$0")/.." && pwd)"
CONF="$DIR/panel.conf"
[ -f "$CONF" ] || { echo "нет $CONF"; exit 1; }
# shellcheck disable=SC1090
. "$CONF"

PORT="${PANEL_ROUTER_PORT:-8080}"
PASS=""
CRON=1
MODE=install
BOT_TOKEN=""
BOT_ALLOW=""

while [ $# -gt 0 ]; do
  case "$1" in
    --pass) PASS="${2:-}"; shift 2 ;;
    --no-cron) CRON=0; shift ;;
    --bot-token) BOT_TOKEN="${2:-}"; shift 2 ;;
    --bot-allow) BOT_ALLOW="${2:-}"; shift 2 ;;
    --uninstall) MODE=uninstall; shift ;;
    *) echo "неизвестный ключ: $1"; exit 1 ;;
  esac
done

KEY_OPT=""; [ -n "${ROUTER_SSH_KEY:-}" ] && KEY_OPT="-i $ROUTER_SSH_KEY"
MUX="${TMPDIR:-/tmp}/rctl-mux-$(id -u)"; mkdir -p "$MUX"; chmod 700 "$MUX"
SSH="ssh $KEY_OPT -p ${ROUTER_SSH_PORT:-22} -o ConnectTimeout=8 -o BatchMode=yes -o ControlMaster=auto -o ControlPath=$MUX/%r@%h:%p -o ControlPersist=60s ${ROUTER_SSH_USER}@${ROUTER_IP}"

say() { echo ":: $*"; }

if [ "$MODE" = uninstall ]; then
  say "сношу панель с роутера"
  $SSH '
    uci -q delete uhttpd.panel && uci commit uhttpd
    /etc/init.d/uhttpd restart >/dev/null 2>&1
    [ -x /etc/init.d/rctl-bot ] && { /etc/init.d/rctl-bot stop >/dev/null 2>&1; /etc/init.d/rctl-bot disable >/dev/null 2>&1; }
    rm -rf /www/panel /etc/uhttpd-panel.auth /usr/sbin/rctl /usr/sbin/zapret-tune \
           /usr/sbin/zapret-sweep /usr/sbin/svcprobe /etc/svcprobe.conf \
           /usr/sbin/ytwatch /usr/sbin/dnsforce /etc/nftables.d/22-dns-force.nft \
           /usr/sbin/rctl-bot /etc/init.d/rctl-bot /etc/rctl-bot.conf /etc/rctl-bot.acl
    crontab -l 2>/dev/null | grep -v "rctl fix" | crontab -
    /etc/init.d/cron restart >/dev/null 2>&1
    echo "✓ панель, rctl, бот и крон убраны (uhttpd на 80/443 не тронут)"
  '
  exit 0
fi

say "заливаю rctl"
$SSH 'cat > /usr/sbin/rctl.new' < "$DIR/rctl"
$SSH 'sh -n /usr/sbin/rctl.new && mv /usr/sbin/rctl.new /usr/sbin/rctl && chmod +x /usr/sbin/rctl' \
  || { echo "✗ rctl не прошёл проверку синтаксиса на роутере"; exit 1; }

say "заливаю подборщик стратегии zapret"
$SSH 'cat > /usr/sbin/zapret-tune.new' < "$DIR/router/zapret-tune"
$SSH 'sh -n /usr/sbin/zapret-tune.new && mv /usr/sbin/zapret-tune.new /usr/sbin/zapret-tune && chmod +x /usr/sbin/zapret-tune' \
  || { echo "✗ zapret-tune не прошёл проверку синтаксиса"; exit 1; }

say "заливаю свип стратегий, пробер сервисов, дневник ютуба и перехват DNS"
for f in zapret-sweep svcprobe ytwatch dnsforce; do
  $SSH "cat > /usr/sbin/$f.new" < "$DIR/router/$f"
  $SSH "sh -n /usr/sbin/$f.new && mv /usr/sbin/$f.new /usr/sbin/$f && chmod +x /usr/sbin/$f" \
    || { echo "✗ $f не прошёл проверку синтаксиса"; exit 1; }
done

say "заливаю панель"
$SSH 'mkdir -p /www/panel/cgi-bin /www/panel/fonts'
$SSH 'cat > /www/panel/index.html' < "$DIR/router/index.html"
# шрифт панели (Onest, переменный, кириллица+латиница ~46 КБ на двоих)
for f in "$DIR"/router/fonts/*.woff2; do
  [ -f "$f" ] || continue
  $SSH "cat > /www/panel/fonts/$(basename "$f")" < "$f"
done
$SSH 'cat > /www/panel/cgi-bin/api.new' < "$DIR/router/api"
$SSH 'sh -n /www/panel/cgi-bin/api.new && mv /www/panel/cgi-bin/api.new /www/panel/cgi-bin/api && chmod +x /www/panel/cgi-bin/api' \
  || { echo "✗ CGI не прошёл проверку синтаксиса"; exit 1; }

if [ -n "$PASS" ]; then
  say "ставлю пароль на вход"
  # uhttpd -m считает md5crypt-хеш, пароль в открытом виде на роутер не кладём
  H=$($SSH "uhttpd -m '$PASS'" 2>/dev/null | tail -1)
  [ -n "$H" ] || { echo "✗ не удалось посчитать хеш пароля"; exit 1; }
  printf '/:panel:%s\n' "$H" | $SSH 'cat > /etc/uhttpd-panel.auth; chmod 600 /etc/uhttpd-panel.auth'
  AUTH=1
else
  $SSH 'rm -f /etc/uhttpd-panel.auth'
  AUTH=0
fi

say "поднимаю отдельный uhttpd на порту $PORT (только LAN)"
$SSH "
  LAN=\$(uci -q get network.lan.ipaddr | cut -d/ -f1)
  [ -n \"\$LAN\" ] || { echo '✗ не нашёл LAN-адрес'; exit 1; }
  uci -q delete uhttpd.panel
  uci set uhttpd.panel=uhttpd
  uci add_list uhttpd.panel.listen_http=\"\$LAN:$PORT\"
  uci set uhttpd.panel.home='/www/panel'
  uci set uhttpd.panel.cgi_prefix='/cgi-bin'
  uci set uhttpd.panel.script_timeout='120'
  uci set uhttpd.panel.network_timeout='120'
  uci set uhttpd.panel.redirect_https='0'
  uci set uhttpd.panel.rfc1918_filter='1'
  [ '$AUTH' = 1 ] && uci set uhttpd.panel.config='/etc/uhttpd-panel.auth' || uci -q delete uhttpd.panel.config
  uci commit uhttpd
  /etc/init.d/uhttpd restart >/dev/null 2>&1
  sleep 1
  echo \"адрес панели: http://\$LAN:$PORT/\"
"

if [ "$CRON" = 1 ]; then
  # rctl fix сюда больше не ставим: он переключал ноду по одному пингу и дрался
  # со svcprobe, который выбирает ноду по доказанной работе сервисов.
  say "включаю авто-проверку сервисов"
  $SSH '
    ( crontab -l 2>/dev/null | grep -vE "rctl fix|svcprobe|ytwatch"
      echo "*/5 * * * * SVCPROBE_SKIP_SPEED=1 /usr/sbin/svcprobe auto >/dev/null 2>&1"
      echo "7 * * * * /usr/sbin/svcprobe score >/dev/null 2>&1"
      echo "*/10 * * * * /usr/sbin/ytwatch sample light >/dev/null 2>&1"
      echo "23 */2 * * * /usr/sbin/ytwatch sample >/dev/null 2>&1"
      echo "17 */1 * * * /usr/sbin/ytwatch heal >/dev/null 2>&1" ) | crontab -
    /etc/init.d/cron restart >/dev/null 2>&1
    echo "✓ крон: */5 svcprobe auto, матрица нод, дневник ютуба + самолечение"
  '
fi

say "ставлю Telegram-бота"
$SSH 'cat > /usr/sbin/rctl-bot.new' < "$DIR/router/rctl-bot"
$SSH 'sh -n /usr/sbin/rctl-bot.new && mv /usr/sbin/rctl-bot.new /usr/sbin/rctl-bot && chmod +x /usr/sbin/rctl-bot' \
  || { echo "✗ бот не прошёл проверку синтаксиса"; exit 1; }
$SSH 'cat > /etc/init.d/rctl-bot; chmod +x /etc/init.d/rctl-bot' < "$DIR/router/rctl-bot.init"

if [ -n "$BOT_TOKEN" ]; then
  # токен пишем через stdin, чтобы он не светился в списке процессов роутера
  printf 'BOT_TOKEN=%s\nBOT_ALLOW="%s"\nBOT_PROXY=127.0.0.1:1180\n' "$BOT_TOKEN" "$BOT_ALLOW" \
    | $SSH 'cat > /etc/rctl-bot.conf; chmod 600 /etc/rctl-bot.conf'
  $SSH '/etc/init.d/rctl-bot enable; /etc/init.d/rctl-bot restart; sleep 2
        pgrep -f rctl-bot >/dev/null && echo "✓ бот запущен" || echo "✗ бот не поднялся, смотри logread -e rctl-bot"'
else
  # токен не передали: если он уже прописан — просто перезапускаем с новым кодом
  $SSH '[ -f /etc/rctl-bot.conf ] || { printf "BOT_TOKEN=\nBOT_ALLOW=\"\"\nBOT_PROXY=127.0.0.1:1180\n" > /etc/rctl-bot.conf; chmod 600 /etc/rctl-bot.conf; }
        . /etc/rctl-bot.conf
        if [ -n "${BOT_TOKEN:-}" ]; then
          /etc/init.d/rctl-bot enable 2>/dev/null
          /etc/init.d/rctl-bot restart >/dev/null 2>&1; sleep 2
          pgrep -f rctl-bot >/dev/null && echo "✓ бот перезапущен с новым кодом" || echo "✗ бот не поднялся, смотри logread -e rctl-bot"
        else
          echo "· бот залит, но без токена не стартует. Вписать: /etc/rctl-bot.conf, затем /etc/init.d/rctl-bot enable; /etc/init.d/rctl-bot start"
        fi'
fi

say "проверяю, что панель отвечает"
# панель слушает только LAN-адрес, поэтому и стучимся по нему, а не в 127.0.0.1
$SSH "LAN=\$(uci -q get network.lan.ipaddr | cut -d/ -f1)
      curl -sS -m 10 -o /dev/null -w 'страница: %{http_code}\n' \"http://\$LAN:$PORT/\" 2>&1 || true
      curl -sS -m 15 -o /dev/null -w 'API: %{http_code}\n' \"http://\$LAN:$PORT/cgi-bin/api?a=meta\" 2>&1 || true"
echo "готово"
