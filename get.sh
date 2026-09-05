#!/bin/sh
# Скачать кит и подготовить к настройке. Одной командой:
#
#   curl -fsSL https://raw.githubusercontent.com/pepetutu1337/openwrt-bypass-kit/main/get.sh | sh
#
# Ничего на роутер не ставит — только кладёт кит рядом и говорит, что
# заполнить. Установка на роутер остаётся отдельным осознанным шагом:
# она меняет DNS и фаервол, и делать это молча за спиной нельзя.
#
# Переменные:
#   KIT_DIR=~/openwrt-bypass-kit   куда положить (по умолчанию сюда же)
set -eu

REPO="pepetutu1337/openwrt-bypass-kit"
DIR="${KIT_DIR:-$HOME/openwrt-bypass-kit}"

red()  { printf '\033[31m%s\033[0m\n' "$1" >&2; }
say()  { printf '%s\n' "$1"; }
bold() { printf '\033[1m%s\033[0m\n' "$1"; }
die()  { red "$1"; exit 1; }

# GitHub из России отдаёт то нормально, то по сотне байт в секунду. Пробуем
# напрямую, потом через зеркала — кит нужен ровно тем, у кого прямой путь
# и не работает.
fetch() {
  _url="$1"; _out="$2"
  for _m in ${KIT_MIRROR:-} "" https://gh-proxy.com/ https://ghfast.top/; do
    curl -fsL --connect-timeout 8 --max-time 120 --speed-time 20 --speed-limit 1024 \
      -o "$_out" "$_m$_url" && return 0
  done
  return 1
}

command -v curl >/dev/null || die "нужен curl"
command -v tar  >/dev/null || die "нужен tar"

[ -e "$DIR" ] && die "$DIR уже есть — обнови его сам (git pull) или задай KIT_DIR=другая-папка"

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

say "Качаю кит..."
fetch "https://codeload.github.com/$REPO/tar.gz/refs/heads/main" "$tmp/kit.tar.gz" \
  || die "не скачалось. Попробуй своё зеркало: KIT_MIRROR=https://зеркало/ sh get.sh"

mkdir -p "$DIR"
tar xzf "$tmp/kit.tar.gz" -C "$DIR" --strip-components=1
chmod +x "$DIR/bin/netctl" "$DIR/bin/netgui"* "$DIR/router/netkit" "$DIR/panel/rctl" 2>/dev/null || true
cp -n "$DIR/config/nodes.list.example" "$DIR/config/nodes.list"
# panel.conf в .gitignore, а без него panel/router/install.sh падает на входе
# и сторожа (zapret-guard, rudns-guard, svcprobe, ytwatch, rescue) на роутер
# не попадают вовсе — вся самопочинка остаётся не поставленной.
cp -n "$DIR/panel/panel.conf.example" "$DIR/panel/panel.conf" 2>/dev/null || true

say ""
bold "Кит лежит в $DIR"
say ""
say "Дальше три шага:"
say ""
say "  1. Забросить ключ на роутер, чтобы заходило без пароля:"
say "       ssh-copy-id root@192.168.1.1"
say ""
say "  2. Вписать своё:"
say "       \$EDITOR $DIR/config/kit.conf     — адрес роутера, если не 192.168.1.1"
say "       \$EDITOR $DIR/config/nodes.list   — свои vless://-ссылки, по одной на строку"
say "     Нет своих нод — оставь nodes.list пустым и поставь PROXY_ENABLE=\"0\":"
say "     встанет только zapret, обход DPI без прокси."
say ""
say "  3. Поставить на роутер и проверить:"
say "       $DIR/bin/netctl install"
say "       $DIR/bin/netctl doctor"
say ""
say "  4. Сторожа и панель (самопочинка: следят и лечат сами):"
say "       $DIR/panel/router/install.sh"
say "     Без этого шага роутер работает, но чинить поломки придётся руками."
say ""
say "Подробно — $DIR/INSTALL.md"
