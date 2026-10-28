# Weeknd 香港站盯票（快达票 + 微信提醒）

这个项目在云端定时检查快达票页面是否已经进入可购票状态。检测到放票时，会通过 **Server 酱**推送微信通知；电脑关机也会继续运行。

## 1. 准备微信通知

1. 打开 [Server 酱](https://sct.ftqq.com/) 并登录。
2. 扫码关注它的微信服务号，创建一个 `SendKey`。
3. 在 GitHub 仓库中打开 `Settings -> Secrets and variables -> Actions -> Secrets`，新增：
   - `SERVERCHAN_SENDKEY`：粘贴 SendKey。
4. 点击 `Actions`，选择 `Weeknd Hong Kong ticket monitor`，再点 `Run workflow` 做一次测试。

如果希望同时发邮件，再增加以下 Secrets：

- `SMTP_HOST`
- `SMTP_PORT`（例如 `587`）
- `SMTP_USER`
- `SMTP_PASSWORD`（建议使用 Gmail App Password 等专用密码）
- `EMAIL_TO`

## 2. 填入快达票页面

把快达票上 Weeknd 香港站的活动页/购票页 URL 填入 `ticket_urls.txt`，每行一个 URL；或者在 `Settings -> Secrets and variables -> Actions -> Variables` 新增 `TICKET_URLS`，值为 URL。

示例：

```text
https://www.hkticketing.com/.../event-page
```

## 3. 运行方式

- GitHub Actions 每 5 分钟自动检查一次。
- 也可以在 `Actions -> Weeknd Hong Kong ticket monitor` 手动点 `Run workflow`。
- 只有从“未放票”变成“已放票”时才会推送；重复放票提醒默认 6 小时最多一次，可通过 Repository variable `COOLDOWN_MINUTES` 修改。

## 4. 本地测试

```powershell
$env:SERVERCHAN_SENDKEY = "你的 SendKey"
python ticket_monitor.py --test-notification
python ticket_monitor.py
```

## 说明

- 这是网页状态监测器，不会自动购票，也不会绕过验证码、排队或限购。
- 快达票若使用复杂的 JavaScript/反爬验证，可能需要把实际购票页 URL 或接口地址填入 `ticket_urls.txt`；脚本会优先识别页面中的 `onSale` / `isAvailable` / `salesStatus` 等状态字段。
- GitHub Actions 的定时任务在繁忙时可能延迟几分钟，但不依赖你的电脑保持开机。

## 5. 电脑端每 10 秒检测

把项目文件夹放到桌面后，在文件夹内执行：

1. 将 `ticket_config.ps1.example` 复制并重命名为 `ticket_config.ps1`
2. 填写 `SERVERCHAN_SENDKEY` 和快达票 URL
3. 双击 `start-monitor.cmd`

它会每 10 秒检测一次，只在电脑开机且脚本运行时有效。电脑关机时的提醒仍由 GitHub Actions 云端方案提供。
