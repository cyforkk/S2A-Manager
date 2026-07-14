---
name: skill-github-release
description: �
��于配置 GitHub Actions 跨平台构建和
自动发布。涵盖 gh CLI 操作、Windows
/macOS/Linux 工作流配置、Release 发布
和 CI/CD 故障排查。
---

# GitHub 跨�
�台发布流水线

使用 gh CLI 和 GitHub
 Actions 实现跨平台构建和 Release 自
动发布。适用于任何需要构建平台
特定产物的项目（Tauri、Electron、Go
、Rust 原生等）。

**为什么选择 gh
 + GitHub Actions：** 无需外部服务，�
��部运行在 GitHub 基础设施上。一�
� `gh release create` 命令即可触发所�
�平台构建，产物自动上传到 Release
 页面。

**核心流程：** 打标签触�
��工作流，工作流构建并上传产物�
��用户从 Release 页面下载。初始设�
��完成后无需手动操作。

## 安装


将 skill 复制到项目的 `skills/` 或 `.
claude/skills/` 目录：

```bash
git clone 
https://github.com/cyforkk/skill-github-relea
se.git
cp skill-github-release/SKILL.md your-
project/skills/skill-github-release/
```

安
装后，对 Claude Code 说：`使用 skill-
github-release skill`

## 前置要求

- 安
装 gh CLI：`brew install gh` / `winget inst
all GitHub.cli`
- 已认证：`gh auth login`

- 仓库已存在：`gh repo create <name> -
-public`

## 阶段一：初始化配置

###
 步骤 1：认证 gh CLI

```bash
gh auth lo
gin
```

选择 "GitHub.com" -> "HTTPS" -> "L
ogin with browser"。

验证：
```bash
gh a
uth status
```

### 步骤 2：创建仓库�
�如需要）

```bash
gh repo create <repo-n
ame> --description "描述" --public
```

###
 步骤 3：配置工作流权限

`GITHUB_TO
KEN` 需要写入权限才能上传 Release �
��物。

GitHub 网页 -> 仓库 -> Settings
 -> Actions -> General -> Workflow permission
s -> **Read and write**

### 步骤 4：创�
�工作流文件

在 `.github/workflows/rele
ase.yml` 中创建，模板见阶段二。

#
# 阶段二：工作流模板

### 跨平台�
��建矩阵

```yaml
name: Release

on:
  rel
ease:
    types: [published]

jobs:
  build:

    strategy:
      fail-fast: false
      ma
trix:
        include:
          - platform: 
'windows-latest'
            target: ''
     
       name: 'app-x64.exe'
            path: 
'target/release/app.exe'
          - platform
: 'macos-latest'
            target: 'aarch64
-apple-darwin'
            name: 'app-aarch64
.dmg'
            path: 'target/aarch64-apple
-darwin/release/bundle/dmg/*.dmg'
          -
 platform: 'macos-latest'
            target:
 'x86_64-apple-darwin'
            name: 'app
-x64.dmg'
            path: 'target/x86_64-ap
ple-darwin/release/bundle/dmg/*.dmg'
        
  - platform: 'ubuntu-22.04'
            targ
et: ''
            name: 'app-x64.AppImage'
 
           path: 'target/release/bundle/appim
age/*.AppImage'

    runs-on: ${{ matrix.plat
form }}

    steps:
      - uses: actions/che
ckout@v4

      - name: Setup Node.js
       
 uses: actions/setup-node@v4
        with:
  
        node-version: 20

      - name: Insta
ll Rust
        uses: dtolnay/rust-toolchain@
stable
        with:
          targets: ${{ m
atrix.target }}

      - name: Install Linux 
dependencies
        if: matrix.platform == '
ubuntu-22.04'
        run: |
          sudo a
pt-get update
          sudo apt-get install 
-y libwebkit2gtk-4.1-dev libappindicator3-dev
 librsvg2-dev patchelf

      - name: Install
 dependencies
        run: npm install

     
 - name: Build (Windows)
        if: matrix.p
latform == 'windows-latest'
        run: npm 
run build

      - name: Build (macOS)
      
  if: matrix.platform == 'macos-latest'
     
   run: npm run build -- --target ${{ matrix.
target }}

      - name: Build (Linux)
      
  if: matrix.platform == 'ubuntu-22.04'
     
   run: npm run build -- --bundles appimage


      - name: Upload release asset
        us
es: softprops/action-gh-release@v2
        wi
th:
          files: ${{ matrix.path }}
     
     tag_name: ${{ github.ref_name }}
       
 env:
          GITHUB_TOKEN: ${{ secrets.GIT
HUB_TOKEN }}
```

### 适配你的项目

替
换以下占位符：

| 占位符 | 设置�
� |
|---|---|
| `npm run build` | 你的构�
�命令（如 `npm run tauri build`、`make`�
��`go build`） |
| `npm run build -- --targe
t ...` | 交叉编译的目标三元组 |
| `
npm run build -- --bundles appimage` | Linux 
下覆盖 bundle 类型 |
| `target/release/a
pp.exe` | Windows 产物路径 |
| `target/*/
release/bundle/dmg/*.dmg` | macOS 产物路�
� |
| `target/release/bundle/appimage/*.AppIm
age` | Linux 产物路径 |
| `libwebkit2gtk-
4.1-dev ...` | Linux 依赖（不需要则删
除） |

## 阶段三：发布流程

```bas
h
# 1. 提交代码
git add . && git commit -
m "feat: 描述" && git push

# 2. 创建并�
��送版本标签
git tag v1.0.0 && git push 
origin v1.0.0

# 3. 创建 Release（触发 C
I）
gh release create v1.0.0 \
  --title "v1
.0.0" \
  --notes "## 更新内容\n- 功能A
\n- 功能B\n\n## 下载\n- Windows: app-x64.
exe\n- macOS: app-x64.dmg / app-aarch64.dmg\n
- Linux: app-x64.AppImage"

# 4. 查看构建
进度
gh run list

# 5. 查看失败日志
g
h run view <run-id> --log-failed
```

## 故�
��排查

### 问题：Resource not accessibl
e by integration

**现象：** Release 上�
�报错 `Resource not accessible by integrati
on`

**原因：** 默认 `GITHUB_TOKEN` 只�
��读权限

**解决：** 仓库 Settings ->
 Actions -> General -> Workflow permissions -
> **Read and write**

### 问题：构建参�
��报错 unexpected argument

**现象：** `
npm run build --target aarch64-apple-darwin` 
失败

**原因：** `--target` 需要通过
 `--` 传给底层构建工具

**解决：**

```yaml
run: npm run build -- --target aarch
64-apple-darwin
```

### 问题：--bundle �
�数无法识别

**现象：** `--bundle app
image` 报错 `unexpected argument`

**原因
：** CLI 使用复数形式 `--bundles`

**�
��决：**
```yaml
run: npm run build -- --bu
ndles appimage
```

### 问题：macOS dmg �
�出现在 Release

**现象：** macOS 构�
�成功但 Release 页面没有 dmg

**原因
：** 项目配置中未包含 dmg 格式

**
解决：** 在项目 bundle targets 配置�
�添加 `dmg`（如 `tauri.conf.json` -> `bun
dle.targets`）

### 问题：Action 参数�
�变更

**现象：** `softprops/action-gh-r
elease` 报错 `Unexpected input(s) 'tag'`

*
*原因：** v2 中 `tag` 参数已更名为 
`tag_name`

**解决：**
```yaml
with:
  tag
_name: ${{ github.ref_name }}
```

### 问题
：工作流未触发

**现象：** 推送�
�码后工作流不执行

**原因：** 文�
��位置错误或 YAML 语法问题

**解决
：**
- 确保文件在 `.github/workflows/re
lease.yml`
- 验证 YAML 语法
- 检查仓�
� Settings -> Actions 中工作流是否启�
�

## 最佳实践

1. **使用 `fail-fast: f
alse`**：某个平台失败不影响其他�
�台继续构建
2. **锁定 Action 版本**�
��`@v4`、`@v2`）：避免破坏性变更
3.
 **使用 `--` 分隔符**：通过 npm 脚�
�传参给底层工具时
4. **本地先测�
�**：用 `act` CLI 在本地运行 workflow

5. **语义化版本标签**：`v1.0.0`、`v1
.1.0`、`v2.0.0`
6. **Release 说明简洁**�
��按平台列出下载项
7. **显式设置 
`GITHUB_TOKEN`**：在上传步骤中明确�
�明

## 关联工具

**配套 skill：**
- 
**superpowers:writing-plans** — 构建前�
�划功能时使用
- **superpowers:subagent-
driven-development** — 多平台并行构�
�时使用

**相关 CLI 工具：**
- `gh` �
�� GitHub CLI，仓库和 Release 管理
- `a
ct` — 本地运行 GitHub Actions 工作流



