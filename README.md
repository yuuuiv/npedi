# NPEDI 验证码识别：标注 + 训练

这是 `npedi` 项目里验证码那部分单独拆出来的分支（孤儿分支 `train`，和 `main` 没有共同历史）。
里面只有**标注工具、训练/评估代码，和已经标注好的数据集**，不含爬虫、数据库、任何 token 或 `.env`。

多个标注者直接以这个分支为准同步数据：拉下来标注、提交、推回去。

## 目录结构

```
README.md                              本文件
requirements-captcha-cnn.txt           Python 3.11 依赖
captcha_cnn/
  model.py                             CNN 结构、数据加载、切字、解码、Predictor
  npedi.json                           全部超参和路径配置
  UPSTREAM.md                          上游项目与 Apache-2.0 许可说明
scripts/
  setup_npedi_captcha_cnn.ps1          一键建 .captcha-cnn-venv 虚拟环境
  label_npedi_captcha.py               ① 抓图 + 人工标注
  check_captcha_dataset.py             ② 提交前校验数据集（多人协作必跑）
  train_npedi_captcha_cnn.py           ③ 训练
  evaluate_npedi_captcha_cnn.py        ④ 在固定验证集上评估
  solve_npedi_captcha_cnn.py           单张图片推理，打印 4 位答案
captcha-data/
  labeled/                             数据集本体，已有 354 张（入库）
  review/                              标注时的临时预览目录（已 gitignore）
captcha-model/                         训练产物 npedi.weights.h5（已 gitignore，各自本地生成）
```

## 数据集现状

| 项 | 值 |
|---|---|
| 已标注图片 | 354 张 |
| 字符样本数 | 1416（354 × 4 位） |
| 图片规格 | 111 × 36 JPG，训练时转灰度 |
| 字符集 | `123456789ABCDEFGHJKLMNPQRSTUVWXY`（32 个，去掉了易混的 `0 I O Z`） |
| 每个字符的样本数 | 33 ~ 58，32 个字符全部有覆盖 |
| 文件命名 | `<4位大写答案>_<服务端uuid>.jpg`，例如 `12BU_22700b494a814625a8ba4304df2ac7dc.jpg` |

标签直接从文件名解析（`captcha_cnn/model.py` 的 `label_from_path`），**没有单独的标注文件**。
所以改标签 = 改文件名，删样本 = 删文件。

## 环境准备

需要 **Python 3.11**（TensorFlow 2.15 不支持 3.12+）。不要装进爬虫主项目的 venv。

Windows：

```powershell
git clone -b train https://github.com/yuuuiv/npedi.git npedi-train
cd npedi-train
.\scripts\setup_npedi_captcha_cnn.ps1
```

脚本会在仓库根目录建 `.captcha-cnn-venv\` 并装好依赖。之后所有命令都用这个解释器：

```powershell
$py = ".\.captcha-cnn-venv\Scripts\python.exe"
```

其他平台手动装：

```bash
python3.11 -m venv .captcha-cnn-venv
.captcha-cnn-venv/bin/pip install -r requirements-captcha-cnn.txt
```

> 只做标注不训练的话其实只需要 `httpx`，不必装 TensorFlow，但校验脚本需要 `Pillow`。

## ① 标注

```powershell
& $py .\scripts\label_npedi_captcha.py --count 30
```

流程：脚本从 `https://www.npedi.com/onesite-api/captchaImage` 取一张图 → 存到
`captcha-data/review/<uuid>.jpg` → 弹出一个 tkinter 预览窗口（同一窗口内更新）→ 你在终端敲答案。

终端提示 `Label (4 uppercase letters/digits), s=skip, q=quit:`：

- 输 4 位答案（自动转大写）→ 存成 `captcha-data/labeled/<答案>_<uuid>.jpg`
- `s` → 跳过这张（图还留在 `review/`，不进数据集）
- `q` → 直接退出

参数：

| 参数 | 默认 | 说明 |
|---|---|---|
| `--count` | 20 | 本批抓几张，限 1–200 |
| `--delay` | 0.8 | 每次请求间隔秒数，下限 0.5，别调低 |
| `--output` | `captcha-data` | 数据目录 |
| `--base-url` | `https://www.npedi.com` | 站点地址 |

标注约定：

- 拿不准的直接 `s` 跳过。错标比少标伤害大得多——一个错标签会同时污染 4 个字符样本。
- 字符集里没有 `0 I O Z`，看着像 `0` 的一定是 `Q` 或 `D`，像 `I` 的是 `1`，像 `O` 的是 `Q`/`D`，像 `Z` 的是 `2`。
- 图里只有 4 位；如果渲染异常导致位数不对，跳过。

## ② 多人协作：标注前后各做一次

文件名带服务端 uuid，不同人抓到的是不同图，所以**正常情况下合并不会冲突**，都是纯新增。

每批开工前先同步：

```powershell
git pull --rebase origin train
```

标完提交前先校验：

```powershell
& $py .\scripts\check_captcha_dataset.py
```

它会检查文件名格式、标签是否落在字符集内、图片尺寸是否 111×36，以及**同一个 uuid 被两个人标成了不同答案**（真出现了就是有人标错，人工裁决后删掉错的那份）。退出码非 0 表示有问题，输出示例：

```json
{
  "images": 354,
  "unique_captchas": 354,
  "conflicting_captchas": 0,
  "characters_never_seen": [],
  "characters_below_minimum": [],
  "problems": []
}
```

`--min-per-character N` 可以调低样本数告警线（默认 20），用来看哪些字符还需要补样本。

确认干净后推回去：

```powershell
git add captcha-data/labeled
git commit -m "labeled: +30 captchas"
git push origin train
```

> 注意：`captcha-model/` 和 `captcha-data/review/` 都在 `.gitignore` 里，权重和临时预览图不会被提交。

## ③ 训练

```powershell
& $py .\scripts\train_npedi_captcha_cnn.py
```

默认**接着 `captcha-model/npedi.weights.h5` 继续练**；要从零开始加 `--fresh`。

做法不是「一张图 → 4 个位置的大输出层」，而是把每张图横向切成 4 个 36×36 的字符窗口
（带重叠，容得下倾斜字形），共用同一个单字符 CNN。354 张图因此变成 1416 个训练样本。

超参全在 `captcha_cnn/npedi.json`：

| 键 | 当前值 | 说明 |
|---|---|---|
| `image_height` / `image_width` | 36 / 111 | 必须和实际图片一致，否则加载时报错 |
| `fixed_length` | 4 | 验证码位数 |
| `labels` | 32 个字符 | 改动会让已有权重失效 |
| `batch_size` | 128 | |
| `learning_rate` | 0.001 | |
| `dropout_rate` | 0.10 | |
| `epochs` | 100 | 配 EarlyStopping，通常提前停 |
| `validation_fraction` | 0.2 | 验证集比例 |
| `random_seed` | 20260805 | 决定训练/验证集怎么切 |
| `model_weights` | `../captcha-model/npedi.weights.h5` | 相对 `npedi.json` 解析 |
| `labeled_image_dir` | `../captcha-data/labeled` | 同上 |

训练时按 `val_categorical_accuracy` 存最优权重，25 轮不涨就 EarlyStopping 并回滚到最优。
每轮额外打一行 `val_whole_captcha_accuracy`——**4 位全对**的比例，这才是上线真正关心的指标。

## ④ 评估

```powershell
& $py .\scripts\evaluate_npedi_captcha_cnn.py
```

输出 JSON：验证集张数、全对张数、`whole_accuracy`（整张全对率）、`character_accuracy`（单字符正确率），
外加每一条错例的 `expected` / `predicted`，方便回头查是不是标错了。

> ⚠️ 验证集是用 `random_seed` 对**排序后的全部文件**做固定切分的。数据集一旦新增图片，切分就变了，
> 新旧两次的准确率数字不能直接对比。要横向比较请固定数据集版本（例如比较同一个 commit）。

## 单张推理

```powershell
& $py .\scripts\solve_npedi_captcha_cnn.py path\to\captcha.jpg
```

成功打印 4 位答案并退出 0，失败往 stderr 打错误、退出 1。主项目的 `CommandCaptchaSolver`
就是这样调它的，配置路径可用环境变量 `NPEDI_CAPTCHA_CNN_CONFIG` 覆盖。

## 常见问题

**`at least 10 labeled CAPTCHA images are required`** — `labeled/` 里图太少，或者 `npedi.json` 里
`labeled_image_dir` 指错了。

**`xxx.jpg is 120x40; expected 111x36`** — 站点改了验证码尺寸，或者混进了别处来的图。先跑
`check_captcha_dataset.py` 定位。

**`model weights not found`** — 还没训练过。先跑 `train_npedi_captcha_cnn.py --fresh`。

**装不上 TensorFlow** — 确认解释器是 3.11：`& $py -V`。

## 许可

`captcha_cnn/model.py` 的网络结构改编自
[`anexplore/cnn_for_captcha`](https://github.com/anexplore/cnn_for_captcha)（Apache-2.0），
详见 `captcha_cnn/UPSTREAM.md`。
