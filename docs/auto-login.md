# 自动短信登录

默认仍是 token 失效即停止。只有完成本页全部配置并设置 `AUTO_LOGIN=true` 后，客户端才会自动登录。代码变更不会影响已经启动的爬虫进程。

## 已确认的站点协议

当前公开前端使用以下接口：

- `GET /onesite-api/getSms?mobile=...`：发送短信验证码；
- `GET /onesite-api/captchaImage`：返回 `data.img`（Base64 JPEG）和 `data.uuid`；
- `POST /onesite-api/login`：query 参数为 `mobile`、`code`、`password`、`uuid`；
- `code` 是图片验证码答案，`password` 是短信验证码，成功 token 位于 `data.token`。

接口变化时，可在本机导出登录 HAR，然后运行：

```powershell
python scripts/extract_login_contract.py login.har --output login-contract.json
```

输出只有路径和字段名，不包含手机号、验证码、Cookie 或 token。原始 `*.har` 已被 `.gitignore` 排除。

## 第一步：配置 Telegram 双 Bot

SmsForwarder 使用 Bot A 发送短信，本程序使用 Bot B 读取。两个位置不能使用同一个 token，因为 Bot 不能通过自己的 `getUpdates` 读取自己发送的消息。

1. 创建私有 Telegram 群，把 Bot A 和 Bot B 都加入；
2. 在 BotFather 中为 Bot B 开启 Bot-to-Bot Communication Mode；
3. 将 Bot B 设为群管理员，或关闭它的 Group Privacy Mode；
4. SmsForwarder 的发送通道配置 Bot A，并将 NPEDI 短信转发到该群；
5. 转发规则应限制短信发送方或包含“NPEDI/验证码”的正文；
6. 在 `.env` 填 Bot B token、群 ID、Bot A 的数字 ID。

读取器会同时核对群 ID、发送 Bot ID 和消息时间，旧验证码不会被复用。默认接受 4–8 位独立数字，可通过 `SMS_CODE_PATTERN` 收紧到实际短信文案。

填好 `.env` 中三个 Telegram 配置后，先运行通道测试，再让 SmsForwarder 的 Bot A 向该群发送一条包含 4–8 位数字的测试消息：

```powershell
& .\.venv\Scripts\python.exe scripts\test_telegram_otp.py
```

测试只读取新消息，不会请求 NPEDI 发送短信，也不会打印验证码内容或 Bot token。

## 第二步：标注 NPEDI 图片验证码

验证码模型改用 `anexplore/cnn_for_captcha` 的定长三层 CNN 结构，参考版本固定为：

```text
02bfba9c2767ab4842ff8baf45d806fa4cddea3e
```

NPEDI 当前图片是 111×36、4 位大写字母/数字。仓库里的 `captcha_cnn/npedi.json` 会严格校验尺寸和标签，站点样式变化时会直接失败，不会静默输出错误答案。

服务端不会返回正确图片答案，因此第一次训练必须人工标注。每次默认只取 20 张，逐张打开系统图片查看器；输入四位答案，`s` 跳过，`q` 退出：

```powershell
& .\.venv\Scripts\python.exe scripts\label_npedi_captcha.py --count 20
```

标注结果保存为 `captcha-data/labeled/答案_uuid.jpg`，样本目录已被 Git 忽略。可多次运行逐步增加样本。先做小批量验证流程，再继续标注；不要在短时间内高频请求接口。

## 第三步：安装独立 CNN 环境并训练

TensorFlow 不安装到当前爬虫的 `.venv`。先准备 Python 3.11，再运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup_npedi_captcha_cnn.ps1
& .\.captcha-cnn-venv\Scripts\python.exe scripts\train_npedi_captcha_cnn.py
```

训练采用固定随机种子切分训练集和验证集，每轮输出 `val_whole_captcha_accuracy`，并把整张四位验证码准确率最高的权重保存到 `captcha-model/npedi.weights.h5`。如果已有权重，再运行训练命令会从现有权重继续。

不要只看逐字符或 `binary_accuracy`。启用无人值守之前，应让独立验证集的整图准确率稳定达到你能接受的水平。实际需要多少标注取决于字符覆盖和验证码变化，不能预先保证固定数量。

## 第四步：单独测试推理

从已经标注的图片中任选一张：

```powershell
& .\.captcha-cnn-venv\Scripts\python.exe scripts\solve_npedi_captcha_cnn.py .\captcha-data\labeled\99HP_example.jpg
```

stdout 最后一行应只包含四位答案。自动登录会以同样方式调用：

```text
<CAPTCHA_SOLVER_COMMAND> <temporary-jpeg-path>
```

临时登录图片在命令结束后立即删除。

## 第五步：填写配置，最后再启用

在 `.env` 填写：

```dotenv
AUTO_LOGIN=false
NPEDI_MOBILE=
CAPTCHA_SOLVER_COMMAND=.captcha-cnn-venv\Scripts\python.exe scripts\solve_npedi_captcha_cnn.py
TELEGRAM_READER_BOT_TOKEN=
TELEGRAM_CHAT_ID=
TELEGRAM_SMS_SENDER_BOT_ID=
```

确认 CNN 推理和 Telegram 短信读取分别测试通过后，最后才改为：

```dotenv
AUTO_LOGIN=true
```

不要把手机号、Bot token、短信验证码或 Web-Token 发到聊天、提交到 Git，或写进命令行参数。

## 运行时保护

- 只有 401/403 或明确的认证失效响应才触发自动登录；
- 多进程通过 `.auth-refresh.lock` 保证只发送一条短信；
- 等锁期间如果另一个进程已更新 `.env`，当前进程直接采用新 token；
- 一次登录只发一条短信，图片识别失败只获取新图片；
- 新 token 只重试原 API 请求一次，再失败立即停止；
- token 原子写回 `.env`，日志不记录 token 值。
