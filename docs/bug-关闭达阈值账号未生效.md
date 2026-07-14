# Bug：关闭达阈值账号实际未关闭

## 现象

在「账号检测」页点击 **关闭达阈值账号**（或全部关闭）后，接口看似成功，但账号在站点侧仍像可调度/未关闭。

## 根因

1. 工具原先只调用：

```json
{ "account_ids": [...], "status": "inactive" }
```

2. sub2api 侧：
   - 管理端允许的 status 为 `active` / `inactive` / `error`
   - 调度列表按 `status == active` 过滤，理论上 `inactive` 不应再入选
   - 但 **调度缓存即时同步** 条件是 `status == error|disabled` 或 `schedulable == false`
   - 仅写 `inactive` 时，`schedulable` 常仍为 `true`，后台「参与调度」开关仍亮，运行时缓存也可能滞后

3. 线上抽样：大量 `status=inactive` 账号仍为 `schedulable=true`。

## 修复

关闭时同时提交：

```json
{
  "account_ids": [...],
  "status": "inactive",
  "schedulable": false
}
```

开启时：

```json
{
  "account_ids": [...],
  "status": "active",
  "schedulable": true
}
```

并增强成功判定：服务端未返回成功更新时抛错，避免“假成功”。

## 验证

- 对单个 active 账号 bulk-update 后，`GET /admin/accounts/{id}` 应为 `status=inactive` 且 `schedulable=false`
- 再 bulk-update 恢复为 `active` + `schedulable=true`
