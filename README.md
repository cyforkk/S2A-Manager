# S2A Manager

`S2A Manager` 是面向 [sub2api](https://github.com/Wei-Shaw/sub2api) 管理接口的 **Windows 可视化管理工具**，基于 Python + Tkinter，适合运维或非技术同学直接使用。

当前版本见根目录 `VERSION`（窗口标题与检查更新共用）。

## 功能一览

- 管理员 API Key 连接站点（可保存到本机配置）
- 同步账号、分组、代理
- 批量导入 / 导出账号与代理
- 转换账号 JSON 为 sub2api 兼容格式
- 批量调整账号（分组、状态、代理等）
- **账号检测**（按分组）
  - 可配置 **5 小时 / 7 天额度阈值**（百分比，可持久化）
  - 检测达阈值、401/403，或完整检测
  - 关闭达阈值账号（`inactive` + 关闭调度）
  - 本分组全部关闭 / 全部开启
  - 显示检测结果、删除问题账号
- ChatGPT 注册机集成（可选外部目录）
- 检查 GitHub 更新

## 目录结构

```text
S2A-Manager/
├─ tools/
│  ├─ s2a_manager.py              # 主程序（GUI + CLI）
│  ├─ chatgpt_register_adapter.py
│  └─ chatgpt_register_gui.py
├─ docs/                          # 功能与接口说明
├─ pyinstaller_hooks/
├─ pyproject.toml                 # uv 项目配置
├─ uv.lock
├─ .python-version                # Python 3.12
├─ S2A-Manager.spec               # PyInstaller 打包
├─ VERSION
└─ README.md
```

## 直接使用（发布包）

若已有可执行文件：

```powershell
release\S2A-Manager.exe
# 或
dist\S2A-Manager.exe
```

首次使用：

1. 填写 **网站地址**、**管理员 API Key**
2. **检查连接** → **保存配置** → **同步数据**
3. 到 **账号检测** 等页签进行运维操作

本机配置保存在：`%APPDATA%\S2A Manager\gui-config.json`  
（含网站地址、API Key、并发数、**额度阈值** 等；关闭窗口也会自动保存）

## 从源码运行（推荐 uv）

本项目使用 [uv](https://docs.astral.sh/uv/) 管理 Python 与依赖。

要求：

- 已安装 `uv`
- Python `>=3.12`（`.python-version` 为 `3.12`，`uv sync` 可自动下载）

```powershell
# 安装环境
uv sync

# 需要打包时
uv sync --group dev

# 启动 GUI（无参数直接打开界面）
uv run python tools\s2a_manager.py
```

## 账号检测简要流程

1. **① 准备**：同步账号和分组，选择检测分组，设置 5 小时 / 7 天阈值  
2. **② 检测**：检测达阈值 / 401/403 / 完整检测（只记录结果，不自动删关）  
3. **③ 处理**：显示结果 → 关闭达阈值账号，或删除问题账号；也可对本分组全部关闭/开启  
4. **④ 列表**：核对结果（会弹窗 + 列表区展示）

- **关闭账号** = 设为 `inactive` 且 `schedulable=false`（停用并禁止调度，不是删除）  
- **全部开启** = `active` 且 `schedulable=true`

## 重新打包

```powershell
uv sync --group dev
uv run pyinstaller S2A-Manager.spec --noconfirm
```

输出：`dist\S2A-Manager.exe`

## 版本与发布

- 版本号：根目录 `VERSION`（如 `v0.2.2`）
- `pyproject.toml` 中 `version` 应与之对应（去掉 `v` 前缀）
- 推送 `v*` tag 时，GitHub Actions 在 Windows 上构建 EXE 并发布 Release（tag 必须与 `VERSION` 一致）

## 环境变量（可选）

兼容原有命名：

| 变量 | 说明 |
|------|------|
| `SUB2API_BASE_URL` | 站点地址 |
| `SUB2API_ADMIN_API_KEY` | 管理员 API Key |
| `SUB2API_USER_AGENT` | 请求 User-Agent |

另支持 CLI 的 Bearer / 邮箱密码登录等，详见 `python tools\s2a_manager.py -h`。

## CLI 说明

无参数启动 GUI；也可使用子命令，例如：

```powershell
uv run python tools\s2a_manager.py gui-login
uv run python tools\s2a_manager.py --admin-api-key xxx delete-accounts --ids 1 2
uv run python tools\s2a_manager.py --admin-api-key xxx import-accounts-data --file bundle.json
```

## 文档

`docs/` 目录包含模块接口与实现记录，例如：

- 账号检测额度阈值与关闭开关
- uv 迁移说明
- 相关 bug 记录

## 说明

- 账号导入导出格式与 sub2api 兼容（数据类型字段未改名）
- 业务逻辑主要依赖标准库；打包依赖 `pyinstaller`（dev 组）
- ChatGPT 注册机为可选外部目录，不强制随本仓库分发
