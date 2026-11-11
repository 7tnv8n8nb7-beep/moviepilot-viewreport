#!/usr/bin/env bash
# 一键把本目录发布为公开的 GitHub 仓库（供 MoviePilot 方式 B 安装）
# 用法（Git Bash / WSL / macOS 终端，cd 到本目录后执行）：
#   bash publish.sh
set -e

REPO="moviepilot-viewreport"

# 1) 初始化并提交
git init -q 2>/dev/null || true
git add -A
git commit -q -m "init: MoviePilot 观影报告推送插件 (viewreport)" 2>/dev/null || echo "（本地已是最新，无新提交）"
git branch -M main 2>/dev/null || true

# 2) 优先用 gh 创建并推送
if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
  echo "==> 检测到已登录的 gh，自动创建公开仓库并推送..."
  if gh repo view "$REPO" >/dev/null 2>&1; then
    echo "仓库已存在，仅推送。"
  else
    gh repo create "$REPO" --public \
      --description "MoviePilot 插件：观影报告推送（零素材依赖，自动生成 Emby 观影排行图并推送到微信/Telegram）" \
      --source=. --remote=origin --push
  fi
  git push -u origin main 2>/dev/null || git push -u origin main
  echo "✅ 完成！仓库地址：https://github.com/$(gh api user --jq .login)/$REPO"
  exit 0
fi

# 3) 没有 gh：给出手动步骤
echo "未检测到已登录的 gh CLI。二选一："
echo "--------------------------------------------------------------"
echo "方式一（推荐）：装 gh 并登录后重跑本脚本"
echo "  Windows: winget install --id GitHub.cli"
echo "  登录:    gh auth login"
echo "  重跑:    bash publish.sh"
echo "--------------------------------------------------------------"
echo "方式二：GitHub 网页新建公开仓库 '$REPO'，然后执行："
echo "  git remote add origin https://github.com/<你的用户名>/$REPO.git"
echo "  git push -u origin main"
echo "--------------------------------------------------------------"
echo "仓库结构（已就绪）："
echo "  data/viewreport/__init__.py   <-- 插件本体（MP 安装入口）"
echo "  README.md"
