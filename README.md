# S2A Manager

`S2A Manager` 是一个面向 `sub2api` 管理接口的 Windows 可视化管理工具，基于 Python + Tkinter 构建，适合直接给运维或非技术用户使用。

当前项目已经内置以下能力：

- 管理员 API Key 连接站点
- 同步账号、分组、代理数据
- 批量导入账号与代理
- 转换账号 JSON 为兼容导入格式
- 批量调整账号
- 账号检测、手动刷新状态、批量删除问题账号
- 导出站内账号到本地 JSON

## 目录结构

```text
S2A Manager/
├─ tools/
│  └─ s2a_manager.py
├─ pyinstaller_hooks/
│  └─ pre_find_module_path/
│     └─ hook-tkinter.py
├─ release/
│  └─ S2A-Manager.exe
├─ S2A-Manager.spec
└─ README.md
```

## 直接使用

直接运行：

```powershell
release\S2A-Manager.exe
```

首次使用时，在界面中填写：

- 网站地址
- 管理员 API Key

然后点击：

1. `检查连接`
2. `保存配置`
3. `同步数据`

## 从源码运行

推荐 Python 版本：

- `3.12`

运行命令：

```powershell
python tools\s2a_manager.py
```

## 重新打包

```powershell
python -m PyInstaller S2A-Manager.spec --noconfirm
```

默认输出：

- `dist\S2A-Manager.exe`

## 版本文件

项目版本号统一保存在根目录的 `VERSION` 文件中。

- 程序窗口标题读取这个版本号
- 检查更新时会用它和 GitHub 最新 tag 做比较

发布新版本时，优先修改 `VERSION`

## 环境变量

程序仍兼容原有环境变量命名：

- `SUB2API_BASE_URL`
- `SUB2API_ADMIN_API_KEY`
- `SUB2API_USER_AGENT`

## 说明

- 本项目是从当前工作区中的管理工具独立抽出后的单独交付目录。
- 账号导入导出格式仍保持与 `sub2api` 兼容，因此数据类型字段没有改名。
