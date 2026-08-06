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

## 第一步：配置短信验证码读取源

### 推荐：SmsForwarder 转发到 temp-mail

当前实现按 [`yuuuiv/temp-mail`](https://github.com/yuuuiv/temp-mail) 的 `agent-email.md` 只读协议接入，核对版本 `945cd95843fd54e106335e55589f32a58efd5965`：

- `GET /api/settings` 校验邮箱专属 Address JWT；
- `GET /api/parsed_mails?limit=20&offset=0` 轮询已解析邮件；
- 列表缺正文时才调用 `GET /api/parsed_mail/{id}`；
- `Authorization: Bearer <Address JWT>`，私人站点可附加 `x-custom-auth`；
- 不调用删除、发信、管理或地址变更接口。

在 temp-mail 前端进入目标邮箱设置，复制该地址自己的 **Address JWT**，不要使用统一账户 JWT。交互式配置器会隐藏 JWT 与可选站点密码输入：

```powershell
& .\.venv\Scripts\python.exe scripts\configure_auto_login.py
```

选择 `temp_mail`，然后填写 Worker API 基地址、Address JWT 和接收转发短信的邮箱地址。默认安全过滤为：

如果暂时不配置 NPEDI 手机号，可以先只配置邮箱读取器：

```powershell
& .\.venv\Scripts\python.exe scripts\configure_auto_login.py --mail-only --backend temp_mail
```

```dotenv
OTP_READER_BACKEND=temp_mail
TEMP_MAIL_ALLOWED_SENDER=support@neofantasy.online
TEMP_MAIL_REQUIRED_TEXT=宁波舟山港
TEMP_MAIL_REQUIRED_COPIES=2
TEMP_MAIL_POLL_SECONDS=3
```

读取器只接受 NPEDI 发短信之后到达的邮件，同时核对收件人、发件人和正文标记。由于 SmsForwarder 对同一短信发送两封邮件，必须在两个不同邮件 ID 中提取到同一个验证码才返回；重复邮件用于确认，不会触发两次登录。

先测试只读凭证，不显示任何邮件内容：

```powershell
& .\.venv\Scripts\python.exe scripts\test_temp_mail_connection.py
```

需要单独验证 OTP 过滤时，先启动下列命令，再让 SmsForwarder 转发一条新的宁波舟山港验证码邮件：

```powershell
& .\.venv\Scripts\python.exe scripts\test_temp_mail_otp.py
```

### 备选：Telegram 双 Bot

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

NPEDI 当前图片是 111×36、4 位大写字母/数字。354 张人工标注样本覆盖的实际字符集为 `123456789ABCDEFGHJKLMNPQRSTUVWXY`；`0/I/O/Z` 在 1,416 个字符中均未出现，因此模型不为这些站点未使用的类别分配输出。仓库里的 `captcha_cnn/npedi.json` 会严格校验尺寸和标签，站点样式变化时会直接失败，不会静默输出错误答案。

服务端不会返回正确图片答案，因此第一次训练必须人工标注。每次默认只取 20 张，逐张打开系统图片查看器；输入四位答案，`s` 跳过，`q` 退出：

```powershell
& .\.venv\Scripts\python.exe scripts\label_npedi_captcha.py --count 20
```

标注结果保存为 `captcha-data/labeled/答案_uuid.jpg`，样本目录已被 Git 忽略。可多次运行逐步增加样本。先做小批量验证流程，再继续标注；不要在短时间内高频请求接口。

## 第三步：安装独立 CNN 环境并训练

TensorFlow 不安装到当前爬虫的 `.venv`。先准备 Python 3.11，再运行：

```powershell
powershell -ExecutionPolicy Bypass -File .\scripts\setup_npedi_captcha_cnn.ps1
& .\.captcha-cnn-venv\Scripts\python.exe scripts\train_npedi_captcha_cnn.py --fresh
& .\.captcha-cnn-venv\Scripts\python.exe scripts\evaluate_npedi_captcha_cnn.py
```

训练先按整张验证码、固定随机种子划分训练集和验证集，再把每张图展开为四个有重叠的字符区域。四个位置共享同一个字符分类器，因此验证集字符不会通过裁剪泄漏进训练集。每轮输出 `val_whole_captcha_accuracy`，并按较稳定的验证集单字符准确率保存最佳权重到 `captcha-model/npedi.weights.h5`。省略 `--fresh` 会从已有权重继续训练。

当前 354 张标注样本的固定留出集有 71 张。2026-08-06 的基准结果为：单字符 `86.97%`，整张四位验证码 `57.75%`（41/71）。这个指标可以配合登录时刷新图片重试，但还不适合假设一次必定成功；默认最多尝试 3 张不同图片，短信只请求一次。继续增加标注样本后，应重新执行上面的训练和评估命令，只以留出集结果判断是否改善。

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

推荐运行交互式配置器。Bot token 使用隐藏输入，手机号和密钥不会进入命令行历史；该命令会强制保持 `AUTO_LOGIN=false`：

```powershell
& .\.venv\Scripts\python.exe scripts\configure_auto_login.py
& .\.venv\Scripts\python.exe scripts\check_auto_login.py
```

在 `.env` 填写：

```dotenv
AUTO_LOGIN=false
NPEDI_MOBILE=
CAPTCHA_SOLVER_COMMAND=.captcha-cnn-venv\Scripts\python.exe scripts\solve_npedi_captcha_cnn.py
OTP_READER_BACKEND=temp_mail
TEMP_MAIL_BASE_URL=
TEMP_MAIL_ADDRESS_JWT=
TEMP_MAIL_SITE_PASSWORD=
TEMP_MAIL_RECIPIENT=
TEMP_MAIL_ALLOWED_SENDER=support@neofantasy.online
TEMP_MAIL_REQUIRED_TEXT=宁波舟山港
TEMP_MAIL_REQUIRED_COPIES=2
```

确认 CNN 推理和所选短信读取源分别测试通过后，最后才改为：

```dotenv
AUTO_LOGIN=true
```

也可以用一次显式端到端测试完成这一步。它会请求一条 NPEDI 短信；只有登录成功才保存新 token 并开启自动登录：

```powershell
& .\.venv\Scripts\python.exe scripts\test_auto_login.py --request-sms --enable
```

登录恢复后可运行一次 VGM 批量过滤探针。它只读取一个候选航次的第一页，不写数据库：

```powershell
& .\.venv\Scripts\python.exe scripts\probe_vgm_batch.py
```

不要把手机号、Address JWT、站点密码、Bot token、短信验证码或 Web-Token 发到聊天、提交到 Git，或写进命令行参数。

## 运行时保护

- 只有 401/403 或明确的认证失效响应才触发自动登录；
- 多进程通过 `.auth-refresh.lock` 保证只发送一条短信；
- 等锁期间如果另一个进程已更新 `.env`，当前进程直接采用新 token；
- 一次登录只发一条短信，图片识别失败只获取新图片；
- 新 token 只重试原 API 请求一次，再失败立即停止；
- token 原子写回 `.env`，日志不记录 token 值。
