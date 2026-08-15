#!/usr/bin/env bash
# ============================================================
# Freebuff-cloud 一键同步推送脚本
# 功能：拉取远程改动 → 提交本地改动 → 推送到 GitHub
# 用法：双击运行，或在终端执行  bash scripts/push.sh
# ============================================================

set -e
cd "$(dirname "$0")/.."   # 进入项目根目录

echo ""
echo "=========================================="
echo "  🚀 Freebuff-cloud 一键推送"
echo "=========================================="

# 1. 检查是否有未提交改动
if [ -z "$(git status --porcelain)" ]; then
    echo "  ℹ️  没有任何本地改动，跳过提交"
else
    echo ""
    echo "  📦 检测到以下改动："
    git status --short | sed 's/^/     /'
    echo ""
    read -r -p "  是否提交并推送？[Y/n] " answer
    if [ "${answer,,}" != "n" ] && [ "${answer,,}" != "no" ]; then
        # 提交（用当前时间作为提交信息的一部分）
        read -r -p "  提交说明（直接回车用默认）：" msg
        if [ -z "$msg" ]; then
            msg="update: $(date '+%Y-%m-%d %H:%M')"
        fi
        git add -A
        git commit -m "$msg"
        echo "  ✅ 已提交：$msg"
    else
        echo "  ⏭️  已跳过提交"
        exit 0
    fi
fi

# 2. 拉取远程（自动合并）
echo ""
echo "  🔄 拉取远程改动..."
git pull origin main --no-edit

# 3. 推送到远程
echo ""
echo "  📤 推送到 GitHub..."
git push origin main

echo ""
echo "=========================================="
echo "  🎉 同步完成！"
echo "=========================================="
echo ""
