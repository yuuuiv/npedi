# npedi 数据画像：结论、依据与实现

这份报告说明 `analyze.py` 得出的每条结论**依据是什么**、**怎么算出来的**，以及哪些结论现在已经不成立。

报告里的数字来自 2026-07-28 的库快照：542,701 行明细、848 个航次、两轮采集（一轮 backfill + 一轮 incremental）。数字会随采集变化，重跑 `python analyze.py all` 可以刷新。

## 先说结论

七条结论，按"对下游用数据的人有多重要"排序：

| 结论                                   | 依据                                         | 影响                                |
| -------------------------------------- | -------------------------------------------- | ----------------------------------- |
| 这是核放比对系统，不是物流跟踪系统     | 三个 Remark 列各只有 3 种固定措辞            | 决定了整批数据该怎么读              |
| `compareTime` 是"还在动"的指示器       | 越新的桶，完成度反而越低                     | 拿它当时间轴排序会得出反向结论      |
| 87% 的"变更"是空转                     | 58,106 条变更里 50,592 条只有 compareTime 变 | 已修正入库逻辑，变更 CSV 缩小十倍   |
| 放行不等于三证齐全                     | 45,032 行三证不全却已放行                    | 别把 passFlag 当三个 flag 的与      |
| 同一个箱子会登记在两个码头             | 11,281 组里 87.2% 只有一边真收到货           | 按箱聚合前必须滤空壳行              |
| 38 列里 8 列全空                       | 非空率 0                                     | 建模时直接忽略                      |
| 五列组合**已经不再唯一**（原结论作废） | 258 行重复                                   | 见[需要修正的地方](#需要修正的地方) |

## 数据从哪来

`analyze.py` 只读 SQLite，不联网：

```python
conn = sqlite3.connect(f"file:{cfg.db_path}?mode=ro", uri=True)
```

`mode=ro` 是有意的——画像跑在采集之间，只读连接保证它绝不会写脏正在被增量更新的库。三张表分工不同：

- `containers`：当前态快照，回答"现在长什么样"。前五节都读它。
- `container_history`：字段级变更，回答"这段时间动了什么"。`changes` 节读它。
- `sync_runs`：每轮的收成统计，用来看采集本身健不健康。

前五节看快照，`changes` 节看变更。**后者才是每天跑三轮增量换来的东西**，单看快照看不到。

## 两个贯穿全文的写法

**`ne(col)` 判断"这列有值"。** 接口的空值有两种形态——`NULL` 和空字符串，混在一起：

```python
def ne(col: str) -> str:
    return f"TRIM(COALESCE(\"{col}\", '')) <> ''"
```

只写 `IS NOT NULL` 会把成千上万的空串算成有值，非空率全线虚高。所有涉及"有没有值"的判断都走这个函数。

**计数用 SQL 聚合，不拉数据进 Python。** 54 万行、38 列，全部靠 `COUNT(*) FROM containers WHERE ...` 在 SQLite 里算完，Python 只接收标量。代价是每节要跑几十条查询，好处是内存占用与数据量无关。

## 一、三个 Remark 列暴露了系统的真实用途

**依据。** `sldRemark`、`matouRemark`、`customRemark` 三列，每列的不同值都恰好是 **3**：

```
sldRemark      19.6%   3   缺少电子口岸三联单。/ 货代舱单提单数量多于电子口岸三联单提单数量。
matouRemark    10.4%   3   缺少码头运抵报告。/ 货代舱单提单与码头运抵提单的数量不一致。
customRemark   11.0%   3   缺少海关放行信息。/ 货代舱单提单数量多于海关放行提单数量。
```

三种措辞全是"货代舱单 vs 某个外部数据源对不上"。这不是自由文本备注，是枚举化的失败原因。

**结论。** 系统在拿货代舱单比对三个外部数据源，齐了才发送、才放行、才装船：

| 校验位       | 原因说明       | 比对的数据源   |
| ------------ | -------------- | -------------- |
| `sldFlag`    | `sldRemark`    | 电子口岸三联单 |
| `matouFlag`  | `matouRemark`  | 码头运抵报告   |
| `customFlag` | `customRemark` | 海关放行信息   |

**实现。** `columns()` 对每个业务列跑三条查询——非空数、`COUNT(DISTINCT)`、按频次取前两个值。基数低到个位数的列会立刻暴露自己是枚举而非自由文本。

## 二、38 个业务列里有 8 列全空

**依据。** 非空率为 0 的列：`linecode`、`agent`、`agentcode`、`reason`、`portclosetime`、`type`、`companyName`、`flag`。

这 8 列在接口返回里存在、在建表语句里占着位置，但一行都没有值。`stayFlag` 也接近死列——非空率 0.2%，且只有 `N` 一种取值。

**为什么要专门标出来。** 死列在函数依赖检验里会制造假阳性："给定 containerno 唯一确定 linecode"这类结论看着成立，其实只是因为两边都是空。报告把死列单列一行，就是提醒读到函数依赖那节时别当真。

**实现。** `columns()` 里非空数为 0 的列直接进 `dead` 列表、跳过后续统计；非空率低于 1% 的进 `thin` 列表。

## 三、compareTime 越新，流程阶段反而越早

这是全篇最容易搞反的一条。

**依据。** 按 `compareTime` 分桶，看各阶段完成度：

```
compareTime 桶        行数 |  三联单   已运抵   海关放   已放行   已装船   未运抵
最近 1 天            96861 |  39.8%   43.1%   42.0%   30.1%    1.0%   69.3%
1~4 天前             68272 |  91.4%   95.4%   98.9%   94.9%   23.0%    0.5%
4~8 天前            121914 |  89.6%   97.9%   99.2%   91.3%   72.4%    0.3%
8~15 天前           175676 |  89.0%   98.9%   99.2%   91.7%   89.7%    0.0%
15~30 天前           78881 |  86.1%   98.7%   99.3%   91.5%   91.0%    0.0%
```

最新的一桶完成度最低（已装船 1.0%、未运抵 69.3%），越往前完成度越高（已装船爬到 91.0%）。

**为什么是这样。** 箱子走完流程就不再被比对，`compareTime` 停在最后一次比对的时刻。所以 `compareTime` 新 = 这行还在被反复比对 = 还没走完；`compareTime` 旧 = 早就定型了。

**怎么用。** `compareTime` 是"这行还在动"的指示器，不是业务时间轴。最新那一桶（近 1 天、约 9.7 万行）就是当前活跃工作面。想按时间排业务进度，用 `receivetime` / `sendTime` / `loadtime`。

**实现。** 锚点取库内最大的 `compareTime` 而不是 `datetime.now()`：

```python
newest = self.conn.execute(
    f"SELECT MAX(compareTime) FROM containers WHERE {ne('compareTime')}").fetchone()[0]
anchor = datetime.strptime(newest, "%Y%m%d%H%M%S")
```

用当前时间当锚点的话，隔几天再跑同一个库，所有行都会往后漂一个桶，横截面就没法跨次比较了。桶边界靠 14 位时间戳的字符串比较划分——定长数字串的字典序等同数值序，不用转类型。

## 四、三条不变量成立，但"放行=三证齐全"不成立

**成立的三条：**

| 不变量                               | 反例数                  |
| ------------------------------------ | ----------------------- |
| `passFlag` 有值 ⟺ `receivetime` 有值 | 0 / 0                   |
| `flag=Y` 时对应 Remark 必为空        | 0（三组都是）           |
| 有 `sendTime` ⇒ 三个校验位全 Y       | 0（378,468 行全部满足） |

第一条说明没运抵就不会有放行判定，两列同生共死。第三条说明**发送**确实是三证齐全的结果。

**不成立的直觉：**

- 三证不全却已放行：**45,032** 行（其中 45,031 行 `remark=放行成功`）
- 三证齐全却未放行：**26,221** 行

放行是独立判定，不是三个校验位的与。把 `passFlag` 当成 `sldFlag AND matouFlag AND customFlag` 会同时错两个方向。

**一个反向的小发现。** `flag=Y` 时 Remark 必为空严格成立，但反过来 `flag=N` 却没有说明的有 856 / 7,557 / 171 行。Remark 是 `flag=N` 的原因，不是每个 N 都配了原因。

**实现。** `rules()` 把每条不变量写成"反例计数"而不是布尔判断：

```python
a = self.count(f"{ne('passFlag')} AND NOT {ne('receivetime')}")
b = self.count(f"NOT {ne('passFlag')} AND {ne('receivetime')}")
```

计数比布尔有用——规则破了能立刻知道破得多厉害，是 3 行的脏数据还是 4 万行的错误假设。三证不全却放行那 45,032 行就是这么浮出来的，顺带按 `remark` 取前三名解释了原因。

## 五、同一个箱子会登记在两个码头，只有一个真收到货

**依据。** 同箱、同票、同航次却有两行的组共 **11,281** 组。逐列看两行之间的不一致比例：

```
matou          97.8%      ← 码头本身
matouFlag      89.0%
passFlag       87.3%
receivetime    87.3%
compareFlag    82.2%
loadtime       58.4%
sendFlag       55.2%
customFlag     29.0%
rktime          3.9%
sldFlag         0.6%
status          0.0%      ← 箱子固有属性
```

分层很干净：码头级状态（运抵、放行）几乎必然不同，箱子固有属性（`status` 完全一致、`sldFlag` 只有 0.6% 不同）几乎必然相同。

**决定性的一条：** 恰好一边有 `receivetime` 的组占 **87.2%**（9,834 / 11,281）。

**结论。** 这不是两套并行状态，是同一个箱子在两个码头都登记了、只有一个真正收到它。另一行是空壳。**按箱聚合前必须先滤掉空壳行**，否则同一个箱子会被算两次。

**实现。** `terminals()` 用 `containerno+billno+unvessel+voyage` 分组、`HAVING COUNT(*) = 2` 取成对记录，然后对每列数 `COUNT(DISTINCT COALESCE(col,'')) > 1` 的组数。用不一致**比例**排序，而不是逐列讲故事——分层自己就浮出来了。

## 六、变更画像：87% 的"变更"是平台在空转

这一节读 `container_history`，是唯一回答"这段时间发生了什么"的部分。

**依据。** 一轮增量（run #2，856 次请求）产生 58,106 条字段级变更。按字段拆开：

```
compareTime      57629   99.2%
customRemark      5164    8.9%
sldRemark         4996    8.6%
customFlag        4895    8.4%
sldFlag           4741    8.2%
receivetime       4600    7.9%
...
billno               5    0.0%
```

`compareTime` 出现在 99.2% 的变更里。进一步查"只有 compareTime 变了"的记录：

```sql
SELECT COUNT(*) FROM (SELECT h.hist_id FROM container_history h,
json_each(h.changed_fields) je GROUP BY h.hist_id
HAVING COUNT(*) = 1 AND MAX(je.key) = 'compareTime')
```

结果 **50,592 条，占 87.1%**。平台重新比对了一遍，业务内容一个字没动。

**已经改了什么。** 这个发现直接改了入库逻辑。`store.py` 现在把变更分三路：

| 路径        | 条件                          | 行为                                   |
| ----------- | ----------------------------- | -------------------------------------- |
| `unchanged` | hash 一样                     | 完全不碰库                             |
| `touched`   | 只有 `TOUCH_ONLY_FIELDS` 变了 | 写新值保持数据新鲜，不记历史、不进 CSV |
| `updated`   | 有业务字段变了                | 更新 + 记字段级历史 + 进变更 CSV       |

不这么改的话，真正的 7,514 条业务变更会被 5 万条噪声淹掉，`changes_*.csv` 也会膨胀近十倍。库里现存那 87% 是改动之前的历史遗留。

**状态位的流转方向。** 把 `{"字段": [旧, 新]}` 摊平后按旧值→新值分组：

```
passFlag     4598   (空)→Y 4436，(空)→N 96，N→Y 54，Y→N 12
sendFlag     3706   N→Y 3706
sldFlag      4741   N→Y 4741
matouFlag    2252   N→Y 2223，Y→N 29
customFlag   4895   N→Y 4893，Y→N 2
compareFlag  4504   N→Y 4504
```

绝大多数是 N→Y，流程在往前推。**反向的 Y→N 少但要紧**——`matouFlag` 29 次、`passFlag` 12 次、`customFlag` 2 次，那是已经确认的状态又被推翻。这类行值得单独盯。

**实现。** 变更差异以 JSON 存在 `changed_fields` 列里，靠 SQLite 的 `json_each` 摊平成一行一个字段：

```sql
SELECT je.key, COUNT(*) n FROM container_history h, json_each(h.changed_fields) je
GROUP BY je.key ORDER BY n DESC
```

方向流转则用 `json_extract(changed_fields,'$."passFlag"[0]')` 取旧值、`[1]` 取新值。全程不解析 JSON 到 Python。

`--csv` 会导出字段级明细（一行一个字段的变更），供下游做更细的追踪。

**一个健康度指标。** 每个箱子被改过几次：目前 58,106 个箱子都只改过 1 次。攒够几轮之后，这个分布会变成判断"哪些箱子在反复翻烧饼"的入口。

## 七、唯一键：五列组合已经不再唯一

**原结论（现在是错的）。** 代码注释写着"实测这五列组合在全表唯一，少任何一列都不唯一"。

**实测。**

```
containerno                                  491254   重复 51447
containerno+billno                           492858   重复 49843
containerno+billno+unvessel                  531188   重复 11513
containerno+billno+unvessel+voyage           531406   重复 11295
containerno+billno+unvessel+voyage+matou     542443   重复 258   ← 不再唯一
```

五列组合还剩 **258 行重复**。这个结论在 backfill 那批小样本上成立，数据涨到 54 万行之后被推翻了。

**结论。** 业务上没有可靠的自然主键，**只有接口返回的 `id` 能当主键**——采集层本来就是这么做的（`upsert_containers` 按 `id` upsert），所以数据没受影响，受影响的只有那条注释。

**实现。** `keys()` 逐步加列做 `COUNT(DISTINCT)`，和总行数比。函数依赖那部分用 `HAVING COUNT(DISTINCT col) > 1` 数违例组数，为 0 即函数依赖成立——但读结果时要记得[第二节](#二38-个业务列里有-8-列全空)说的死列假阳性。

这节要全表扫十几趟，所以默认不跑，需要时用 `python analyze.py keys`。

## 需要修正的地方

三条，按紧急程度：

**1. 默认控制台会崩在 `rules()`。** Windows 中文控制台是 GBK，`rules()` 里的 `⟺` 字符编不出来，直接抛 `UnicodeEncodeError`，后面几节全跑不到：

```
UnicodeEncodeError: 'gbk' codec can't encode character '⟺'
```

临时绕法是设 `PYTHONIOENCODING=utf-8`。彻底的修法是把 `⟺` 换成 ASCII 的 `<=>`，或者在 `main()` 开头对 stdout 做一次 `reconfigure(encoding="utf-8", errors="replace")`。

**2. `BUSINESS_KEY` 的注释与实测不符。** 见[第七节](#七唯一键五列组合已经不再唯一)，注释里"全表唯一"的说法要改成"仍有 258 行重复，主键只能用 `id`"。

**3. 变更画像目前只有一轮增量的样本。** 58,106 条变更全部来自 run #2，且都在 `TOUCH_ONLY` 逻辑生效之前。攒够几轮之后，"哪些字段在动"和"每个箱子改过几次"两张表才有趋势意义，现在只能当单次快照读。

## 怎么复现

```
python analyze.py            # 除 keys 外的全部
python analyze.py changes    # 只看变更画像
python analyze.py all        # 全部，含函数依赖检验（慢）
```

导出 CSV 供下游用：

```
python analyze.py lifecycle --csv export/lifecycle.csv
python analyze.py changes --csv export/changes_detail.csv
```

Windows 上如果撞见上面那个编码错误，先设一次环境变量：

```
$env:PYTHONIOENCODING = "utf-8"
```
