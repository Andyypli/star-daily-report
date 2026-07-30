#!/usr/bin/env bash
# 一键激活云端定时邮件（GitHub Actions）
# 用法：WF_TOKEN=<带workflow权限的token> bash activate_cloud.sh
# 说明：
#   - WF_TOKEN 必须有 repo + workflow 权限（细粒度 token 需 Actions:write + Contents:write）
#   - 本脚本会：推全部代码 → 设置 6 个 Secrets → 触发首次运行
set -euo pipefail

CLOUD="/Users/andy/WorkBuddy/2026-07-14-11-42-09/cloud_deploy"
SRC="/Users/andy/WorkBuddy/2026-07-14-11-42-09"
OWNER="Andyypli"
REPO="star-daily-report"
API="https://api.github.com"
PY="/Users/andy/.workbuddy/binaries/python/versions/3.14.3/bin/python3"

: "${WF_TOKEN:?需要设置 WF_TOKEN 环境变量（带 workflow 权限的 GitHub token）}"

echo "===== 0. 校验 token 权限 ====="
SCOPES=$(curl -sI -H "Authorization: Bearer $WF_TOKEN" "$API/user" | grep -i "x-oauth-scopes:" | tr -d '\r' || true)
echo "  scopes: $SCOPES"

echo "===== 1. 用 git 推送全部云端代码 ====="
cd "$CLOUD"
rm -rf .git
git init -q
git checkout -q -b main
git config user.email "andyypli@users.noreply.github.com"
git config user.name "Andyypli"
git add -A
git commit -q -m "feat: cloud daily star report (GitHub Actions, T+0, 7x24)"
git remote add origin "https://${OWNER}:${WF_TOKEN}@github.com/${OWNER}/${REPO}.git"
git push -f -u origin main
echo "  ✅ 代码已推送"

echo "===== 2. 设置 GitHub Secrets（加密上传）====="
# 取仓库公钥
KEYJSON=$(curl -s -H "Authorization: Bearer $WF_TOKEN" -H "Accept: application/vnd.github+json" \
  "$API/repos/$OWNER/$REPO/actions/secrets/public-key")
KEY_ID=$($PY -c "import sys,json;print(json.loads(sys.argv[1])['key_id'])" "$KEYJSON")
PUB_KEY=$($PY -c "import sys,json;print(json.loads(sys.argv[1])['key'])" "$KEYJSON")

# 读本地邮件配置
MAILCFG="$SRC/.mail_config.json"
GH_STAR_TOKEN=$(cat "$SRC/.gh_token")
SMTP_HOST=$($PY -c "import json;print(json.load(open('$MAILCFG'))['smtp_host'])")
SMTP_PORT=$($PY -c "import json;print(json.load(open('$MAILCFG')).get('smtp_port',465))")
SMTP_USER=$($PY -c "import json;print(json.load(open('$MAILCFG'))['username'])")
SMTP_PASS=$($PY -c "import json;print(json.load(open('$MAILCFG'))['password'])")
MAIL_FROM=$($PY -c "import json;print(json.load(open('$MAILCFG')).get('from_addr',''))")
MAIL_TO=$($PY -c "import json;print(','.join(json.load(open('$MAILCFG'))['to_addrs']))")

# 用 PyNaCl 加密（若无则装到隔离venv）
VENV="/Users/andy/.workbuddy/binaries/python/envs/nacl"
if [ ! -x "$VENV/bin/python" ]; then
  $PY -m venv "$VENV"
  "$VENV/bin/pip" -q install pynacl
fi

put_secret() {
  local NAME="$1"; local VALUE="$2"
  local ENC
  ENC=$("$VENV/bin/python" - "$PUB_KEY" "$VALUE" <<'PYENC'
import sys, base64
from nacl import encoding, public
pk = public.PublicKey(sys.argv[1].encode(), encoding.Base64Encoder())
sealed = public.SealedBox(pk).encrypt(sys.argv[2].encode())
print(base64.b64encode(sealed).decode())
PYENC
)
  curl -s -o /dev/null -w "  [$NAME] HTTP %{http_code}\n" -X PUT \
    -H "Authorization: Bearer $WF_TOKEN" -H "Accept: application/vnd.github+json" \
    "$API/repos/$OWNER/$REPO/actions/secrets/$NAME" \
    -d "{\"encrypted_value\":\"$ENC\",\"key_id\":\"$KEY_ID\"}"
}

put_secret "GH_STAR_TOKEN" "$GH_STAR_TOKEN"
put_secret "SMTP_HOST" "$SMTP_HOST"
put_secret "SMTP_PORT" "$SMTP_PORT"
put_secret "SMTP_USER" "$SMTP_USER"
put_secret "SMTP_PASS" "$SMTP_PASS"
put_secret "MAIL_FROM" "$MAIL_FROM"
put_secret "MAIL_TO" "$MAIL_TO"

echo "===== 3. 触发首次运行（验证端到端）====="
sleep 3
curl -s -o /dev/null -w "  dispatch HTTP %{http_code}\n" -X POST \
  -H "Authorization: Bearer $WF_TOKEN" -H "Accept: application/vnd.github+json" \
  "$API/repos/$OWNER/$REPO/actions/workflows/daily.yml/dispatches" \
  -d '{"ref":"main"}'

echo ""
echo "✅ 激活完成。查看运行状态："
echo "   https://github.com/$OWNER/$REPO/actions"
