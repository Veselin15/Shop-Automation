#!/usr/bin/env bash
# Инсталация на shopbot върху Ubuntu (домашния сървър).
# Пуска се от корена на проекта:  bash deploy/install.sh
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_USER="${SUDO_USER:-$USER}"

echo "==> Проект: $PROJECT_DIR"
echo "==> Потребител: $SERVICE_USER"

echo "==> Системни пакети"
sudo apt-get update -qq
sudo apt-get install -y python3-venv python3-pip

echo "==> Виртуална среда"
cd "$PROJECT_DIR"
python3 -m venv .venv
./.venv/bin/pip install --upgrade pip
./.venv/bin/pip install -e .

echo "==> Chromium за Playwright (тегли ~150 MB първия път)"
sudo ./.venv/bin/playwright install-deps chromium
./.venv/bin/playwright install chromium

if [ ! -f .env ]; then
  cp .env.example .env
  echo "==> Създадох .env — попълни го преди първото пускане."
fi

mkdir -p data logs

echo "==> systemd услуга"
sed -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" -e "s|__USER__|$SERVICE_USER|g" \
    deploy/shopbot.service | sudo tee /etc/systemd/system/shopbot.service > /dev/null
sudo systemctl daemon-reload

cat <<'DONE'

Готово. Остават три неща:

  1. Попълни .env (BAZAR_EMAIL, BAZAR_PASSWORD, по избор TELEGRAM_*).

  2. Еднократен ръчен вход в BestSecret. Сървърът е без екран, затова
     или влез с проброс на графиката:

         ssh -X veski4a@192.168.0.101
         cd ~/Shop-Automation && ./.venv/bin/shopbot login bestsecret

     или направи входа на компютъра си и копирай профила:

         scp -r data/profiles/bestsecret veski4a@192.168.0.101:~/Shop-Automation/data/profiles/

  3. Пробен цикъл, преди да пуснеш услугата:

         ./.venv/bin/shopbot once --dry-run -v

     После:

         sudo systemctl enable --now shopbot
         journalctl -u shopbot -f

DONE
