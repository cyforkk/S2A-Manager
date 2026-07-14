# uv 迁移实现文档

## 目标

将 S2A Manager 从「系统 Python / pip 临时安装」改为用 **uv** 管理解释器、虚拟环境与依赖，统一本地开发与 GitHub Release 构建方式。

## 变更内容

| 文件 | 说明 |
|------|------|
| `pyproject.toml` | 项目元数据、`requires-python >=3.12`、dev 依赖组（pyinstaller） |
| `uv.lock` | 锁定依赖（`uv lock` / `uv sync` 生成） |
| `.python-version` | 锁定 Python 3.12 |
| `.gitignore` | 补充缓存与 egg-info 等忽略项 |
| `README.md` | 运行/打包命令改为 `uv run` / `uv sync` |
| `.github/workflows/release.yml` | 使用 `astral-sh/setup-uv` + `uv sync --group dev` |

运行时代码仍以标准库为主，**无业务第三方依赖**；打包工具 `pyinstaller` 放在 **dev 依赖组**。

## 本地命令

```powershell
# 创建 .venv 并安装（默认不装 dev）
uv sync

# 开发/打包环境
uv sync --group dev

# 启动
uv run python tools\s2a_manager.py

# 打包
uv run pyinstaller S2A-Manager.spec --noconfirm
```

## 版本约定

- 程序窗口/检查更新：读根目录 `VERSION`
- `pyproject.toml` 的 `version` 应与 `VERSION` 语义一致（当前 `0.2.2` ↔ `v0.2.2`）
- 发版时：改 `VERSION` 与 `pyproject.toml` version，再打匹配 tag

## 说明

- `tool.uv.package = false`：应用型仓库，不发布为可安装库
- ChatGPT 注册机仍通过外部目录动态加载，不强制写入本项目 dependencies
