#!/usr/bin/env bash
# TrendHunter installer for Ubuntu servers (AWS Lightsail etc.)
# Usage:  bash ~/trendhunter/install.sh [UPDATE_ZIP_URL]
set -uo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="${TH_DATA_DIR:-$HOME/trendhunter-data}"
UPDATE_URL="${1:-}"
SERVICE="trendhunter"
UNIT_DIR="${TH_SYSTEMD_DIR:-/etc/systemd/system}"
BIN_DIR="${TH_BIN_DIR:-/usr/local/bin}"
if (exec </dev/tty) 2>/dev/null; then TTY=/dev/tty; else TTY=/dev/stdin; fi

B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; RD=$'\033[31m'; N=$'\033[0m'
step() { printf '\n%s▶ %s%s\n' "$B" "$*" "$N"; }
ok()   { printf '%s✓ %s%s\n' "$G" "$*" "$N"; }
warn() { printf '%s! %s%s\n' "$Y" "$*" "$N"; }
die()  { printf '%s✗ %s%s\n' "$RD" "$*" "$N"; exit 1; }

ask() {  # ask VAR "prompt" [secret]
  local __var=$1 __prompt=$2 __secret=${3:-} __val=""
  if [ -n "$__secret" ]; then
    read -r -s -p "$__prompt" __val <"$TTY" || die "입력을 읽지 못했습니다. Termius 같은 터미널에서 직접 실행하세요."
    echo
  else
    read -r -p "$__prompt" __val <"$TTY" || die "입력을 읽지 못했습니다. Termius 같은 터미널에서 직접 실행하세요."
  fi
  __val="$(printf '%s' "$__val" | tr -d '[:space:]')"
  printf -v "$__var" '%s' "$__val"
}
valid() { TH_VALUE="$2" python3 "$APP_DIR/setup_helper.py" validate "$1"; }

printf '%s\n' "${B}TrendHunter 설치${N} — 앱 폴더: $APP_DIR"

step "1/6 환경 확인"
if ! command -v python3 >/dev/null; then
  sudo apt-get update -y && sudo apt-get install -y python3 || die "python3 설치 실패"
fi
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' || die "python3 3.8 이상이 필요합니다"
ok "$(python3 --version)"
command -v systemctl >/dev/null || die "systemd 가 없는 환경입니다. 우분투 서버에서 실행하세요."
mkdir -p "$DATA_DIR" && chmod 700 "$DATA_DIR"
ok "데이터 폴더: $DATA_DIR"
if timedatectl show -p NTPSynchronized --value 2>/dev/null | grep -q yes; then
  ok "서버 시간 동기화 정상"
else
  warn "시간 동기화 상태를 확인하지 못했습니다(대부분 문제없음)."
fi
IP="$(python3 "$APP_DIR/setup_helper.py" public-ip 2>/dev/null || true)"
[ -n "$IP" ] && ok "이 서버의 공인 IP: $IP  ← 바이낸스 API 키의 '신뢰할 수 있는 IP'에 이 값을 등록"

step "2/6 키 입력"
if [ -s "$DATA_DIR/secrets.json" ] && [ "${TH_RECONFIGURE:-0}" != "1" ]; then
  ok "저장된 키가 있어 건너뜁니다 (다시 입력하려면: th keys)"
else
  while :; do
    ask BK "바이낸스 API Key 붙여넣기: "
    valid key "$BK" && break
    warn "64자리 영문·숫자가 아닙니다. 다시 붙여넣으세요."
  done
  while :; do
    ask BS "바이낸스 Secret Key 붙여넣기(입력해도 화면엔 안 보임): " secret
    if valid secret "$BS"; then ok "Secret 입력됨 (${#BS}자)"; break; fi
    warn "64자리 영문·숫자가 아닙니다. 다시 붙여넣으세요."
  done
  while :; do
    ask TT "텔레그램 봇 토큰 붙여넣기: "
    valid token "$TT" && break
    warn "형식이 다릅니다(예: 1234567890:AAH...). 다시 붙여넣으세요."
  done
  echo "→ 지금 텔레그램에서 새 봇 대화방을 열고 '안녕'처럼 아무 메시지나 보내세요. (최대 60초 대기)"
  CID="$(TH_TT="$TT" python3 "$APP_DIR/setup_helper.py" tg-detect)" \
    || die "채팅 ID를 찾지 못했습니다. 봇에게 메시지를 보낸 뒤 설치 명령을 다시 실행하세요."
  ok "텔레그램 채팅 ID: $CID"
  TH_BK="$BK" TH_BS="$BS" TH_TT="$TT" TH_CID="$CID" \
    python3 "$APP_DIR/setup_helper.py" save-secrets --data "$DATA_DIR" >/dev/null || die "키 저장 실패"
  ok "키 저장 완료 (본인만 읽을 수 있는 파일)"
  if TH_TT="$TT" TH_CID="$CID" python3 "$APP_DIR/setup_helper.py" tg-test; then
    ok "텔레그램에 테스트 메시지를 보냈습니다"
  else
    warn "텔레그램 테스트 메시지 실패"
  fi
  unset BK BS TT
fi

step "3/6 업데이트 주소"
if [ -n "$UPDATE_URL" ]; then
  python3 "$APP_DIR/setup_helper.py" set-setting --data "$DATA_DIR" update_url "$UPDATE_URL" && ok "$UPDATE_URL"
else
  SAVED="$(python3 -c "import json,sys;print(json.load(open(sys.argv[1])).get('update_url',''))" "$DATA_DIR/settings.json" 2>/dev/null || true)"
  if [ -n "$SAVED" ]; then ok "기존 주소 유지: $SAVED"; else warn "업데이트 주소 없음 — 나중에 텔레그램 /set update_url 로 저장"; fi
fi

step "4/6 바이낸스 점검(읽기 전용)"
python3 "$APP_DIR/selftest.py" --data "$DATA_DIR" \
  || warn "일부 항목이 실패했습니다. 모의 모드는 그대로 동작하니 설치는 계속합니다. 텔레그램 /selftest 로 다시 확인하세요."

step "5/6 자동 실행 등록(systemd)"
chmod +x "$APP_DIR"/*.sh "$APP_DIR"/*.py "$APP_DIR/th" 2>/dev/null || true
PY="$(command -v python3)"
sudo tee "$UNIT_DIR/$SERVICE.service" >/dev/null <<EOF
[Unit]
Description=TrendHunter trading bot
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=0

[Service]
Type=simple
User=$(id -un)
WorkingDirectory=$APP_DIR
ExecStart=$PY -u $APP_DIR/bot.py --data $DATA_DIR
Restart=always
RestartSec=10
TimeoutStopSec=30
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE" >/dev/null 2>&1
sudo systemctl restart "$SERVICE"
sleep 4
if systemctl is-active --quiet "$SERVICE"; then
  ok "봇 실행 중 (서버가 재부팅돼도 자동 시작)"
else
  warn "봇이 아직 시작되지 않았습니다. th logs 로 확인하세요."
fi

step "6/6 관리 명령(th) 설치"
sed -e "s#__APP_DIR__#$APP_DIR#g" -e "s#__DATA_DIR__#$DATA_DIR#g" "$APP_DIR/th" | sudo tee "$BIN_DIR/th" >/dev/null
sudo chmod 755 "$BIN_DIR/th"
ok "th status · th logs · th restart · th update 사용 가능"

cat <<EOF

${G}${B}설치 완료!${N}
1) 텔레그램에 '🤖 TrendHunter 시작 — 모의(PAPER)' 메시지가 왔는지 확인하세요.
2) 텔레그램에서 /backtest 365 로 과거 성과를, /scan 으로 현재 신호 상태를 보세요.
3) 실전 전 /selftest live (아주 작은 실주문 왕복 테스트) → 이상 없으면 /live
EOF
