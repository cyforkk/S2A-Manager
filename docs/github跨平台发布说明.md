# GitHub 跨平台发布说明

遵循项目 skill：`skill-github-release`（来源：https://github.com/cyforkk/skill-github-release）。

## 目标

通过 GitHub Actions 矩阵构建，在 **Windows / macOS / Linux** 产出可运行包，并上传到 GitHub Release。

## 产物

| 平台 | Runner | 产物文件名 |
|------|--------|------------|
| Windows x64 | `windows-2022` | `S2A-Manager-windows-x64.exe` |
| macOS arm64 | `macos-15` + `target_arch=arm64` | `S2A-Manager-macos-arm64` |
| macOS x64 | `macos-15` + `target_arch=x86_64`（Rosetta，**不用 macos-13**，避免排队无 runner） | `S2A-Manager-macos-x64` |
| Linux x64 | `ubuntu-24.04` | `S2A-Manager-linux-x64` |

构建工具：PyInstaller + uv（dev 依赖组）。

## 仓库设置（一次性）

1. 安装并登录 gh：`gh auth login`
2. 仓库 **Settings → Actions → General → Workflow permissions**  
   选择 **Read and write permissions**（否则上传 Release 会报 `Resource not accessible by integration`）
3. 确保 Actions 已启用

## 发布流程（推荐）

```bash
# 1. 改版本
# VERSION 文件写成 v0.2.3
# pyproject.toml 的 version 写成 0.2.3

# 2. 提交并推送
git add VERSION pyproject.toml
git commit -m "chore: 发布 v0.2.3"
git push origin master

# 3. 打标签并推送（会触发 workflow）
git tag v0.2.3
git push origin v0.2.3

# 4. 查看构建
gh run list --workflow=Release
gh run watch
```

也可先建空 Release 再等 CI 上传（`on.release.published` 同样支持）：

```bash
gh release create v0.2.3 --title "S2A Manager v0.2.3" --notes "跨平台构建中…" --draft=false
```

手动重跑某 tag：

```bash
gh workflow run Release -f tag=v0.2.3
```

## 约束

- **标签必须与 `VERSION` 文件内容完全一致**（含 `v` 前缀）
- 某一平台失败不会取消其他平台（`fail-fast: false`）
- Linux 使用系统 `python3.12` + `python3.12-tk`，避免 uv 托管解释器缺 Tk

## 本地打包（可选）

```powershell
uv sync --group dev
uv run pyinstaller S2A-Manager.spec --noconfirm
# 输出 dist/S2A-Manager 或 dist/S2A-Manager.exe
```

## 故障排查（摘自 skill）

| 现象 | 处理 |
|------|------|
| Resource not accessible by integration | Workflow 权限改为 Read and write |
| 工作流未触发 | 确认 tag 为 `v*`，文件在 `.github/workflows/release.yml` |
| Linux 缺 tkinter | CI 已装 `python3.12-tk`；本地需系统 Tk |
| macOS 无法打开 | 隐私与安全性允许；`chmod +x` |

## 相关文件

- `.github/workflows/release.yml`
- `S2A-Manager.spec`
- `.claude/skills/skill-github-release/SKILL.md`
