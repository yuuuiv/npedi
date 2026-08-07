# 自动登录演示录制

录制 `docs/auto-login-demo.webp`：左侧终端叙述流程，右侧真实登录页被自动填写。

## 真实性边界

- **是真的**：验证码由站点当场下发，用生产的 CNN（`scripts/solve_npedi_captcha_cnn.py`）识别；
  短信验证码来自生产的 temp-mail 读取器；登录是真登录，返回的 token 写回 `.env` 并验证。
- **与生产不同**：爬虫平时直接 POST `/portal-api/login`，不经浏览器。这里为了让每一步可见，
  改由真实表单提交。演示时应主动说明这一点。

屏幕上手机号和短信验证码显示为圆点，token 只显示头尾各 8 位。

## 准备环境

需要 Python 3.11、ffmpeg、Chrome、Windows Terminal。演示用的 venv 独立于爬虫环境：

```powershell
py -3.11 -m venv .demo-venv
.\.demo-venv\Scripts\python.exe -m pip install playwright httpx
```

## 先空跑，确认取景

不点「获取验证码」，不发短信，只截一张图检查排版：

```powershell
.\scripts\demo\record_browser.ps1 -Dry
```

看 `scripts/demo/record/dry-frame.png`。窗口几何、登录弹窗是否完整、验证码识别结果是否
和图片一致，都在这一步确认。

## 正式录制

会最小化所有窗口、接管屏幕约两分钟，**并真实发送一条短信**。期间不要操作电脑。

```powershell
.\scripts\demo\record_browser.ps1
```

输出 `scripts/demo/record/browser.mp4`。已有的录像会自动改名备份，不会被覆盖。

## 转成可分发格式

```powershell
$src = "scripts\demo\record\browser.mp4"
# 裁掉右边缘登录后露出的手机号前缀，和右下角 Windows 水印
$crop = "crop=2450:1100:0:0"
ffmpeg -i $src -vf "$crop,setpts=PTS/3,fps=10" "scripts\demo\record\frames\%04d.png"
.\.captcha-cnn-venv\Scripts\python.exe scripts\demo\make_webp.py 1 80
```

WebP 用 Pillow 编码而不是 ffmpeg：这个 ffmpeg 构建的 `libwebp_anim` 输出可用，但无法用
`ffprobe` 核对帧时长。校验用 `webp_chunks.py` 直接读容器里的 ANMF 字段：

```powershell
.\.captcha-cnn-venv\Scripts\python.exe scripts\demo\webp_chunks.py docs\auto-login-demo.webp
```

应输出 `loop_count=0`、总时长约 24.8 秒。注意 Pillow **读不出** WebP 帧时长（一律返回
`None`），不要拿它判断文件好坏。

## 几个踩过的坑

- `CAPTCHA_SOLVER_COMMAND` 是仓库相对路径，脚本里 `os.chdir(PROJECT_ROOT)` 固定工作目录，
  否则从别处启动会找不到求解器。
- Windows PowerShell 5.1 按 ANSI 读 `.ps1`，所以 `.ps1` 里只用 ASCII，中文放在 Python 侧。
- Windows Terminal 把命令里的 `;` 当分栏符，`--title` 带空格会让参数溢出到命令行。
- PowerShell 是 DPI 非感知进程，窗口坐标是逻辑像素；ffmpeg gdigrab 抓物理像素。
  本机 125% 缩放，捕获区域需乘 `$SCALE`，否则右侧窗口被切。
- Playwright 会覆盖 Chrome 的 `--window-size`，必须用 CDP `Browser.setWindowBounds` 设定；
  用 `SetWindowPos` 事后改尺寸会让渲染区和窗口错位。
