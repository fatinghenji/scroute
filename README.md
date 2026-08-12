# scroute · 星际公民货运路线速查

一条命令告诉你：现在买什么货、去哪买、拉到哪卖、一趟赚多少、折合时薪多少。
数据来自 UEX 玩家实时众包价格，不用开浏览器，不用翻表格。

![使用方案卡片](docs/scroute-usage-card.png)

## 三分钟上手

**第 1 步：装**

```bash
git clone https://github.com/fatinghenji/scroute.git
cd scroute
ln -s "$PWD/scroute" ~/.local/bin/scroute   # 之后任意目录都能直接敲 scroute
```

需要 Python 3.10+ 和 curl，Linux / macOS 都自带。Windows 直接用 `python plan_route.py` 跑，参数见下文「高级参数」。

**第 2 步：改成你的船和本金**

打开 `scroute` 文件，改开头三行：

```bash
SHIP="Railen"      # 你的船名，写个大概就行（如 "Hull B"），会自动匹配舱容
CAPITAL=7000000    # 你账上有多少钱（aUEC）
MIN_ROI=25         # 利润率低于多少不考虑
```

**第 3 步：跑**

```bash
scroute
```

## 跑完会看到什么

```
| # | 商品 | 买入站 @价(库存) | 卖出站 @价 | 距离 | 装/卸 | ROI | 利润 | 时薪 | 验证 |
| 1 | Quartz | Shubin SM0-22 @2,964(1,050) | Pyro Gateway @5,400 | 68Gm | 手动/自动 | 82.2% | 155.9W | 211W/h | ✓82% |
```

一行就是一条能直接执行的路线，从左到右读：

- **商品 + 买入站 @价(库存)**：去 Shubin SM0-22 买 Quartz，单价 2,964，站里现货 1,050 SCU，装得满你一船
- **卖出站 @价**：拉到 Pyro Gateway 卖 5,400
- **ROI**：这趟投入的利润率为 82.2%
- **利润 / 时薪**：满仓一趟净赚约 156 万；把量子巡航、对接、装卸、空驶返程全算进去，折合约 211 万/小时
- **验证 ✓**：已和官方 routes 数据交叉核对过，数字对得上

表上还会标注：⚠️跨星系（要跳星门）、⚠️违禁品、「手动」表示装卸要自己动手搬箱（比自动装卸慢）。

> 本金默认只押一半（留一半防意外），想全押加 `--full`。

## 最常用的四个命令

```bash
scroute                        # 我现在不知道去哪 → 给我全局最赚的 15 条
scroute from "Patch City"      # 我人在 Patch City → 从这出发买什么、卖到哪
scroute to "Patch City"        # 我要回 Patch City → 半路买什么带回去卖
scroute loop "Patch City"      # 我要跑往返 → 去程+返程一起算好
```

**出发前必做**：库存和价格半小时就变，临起飞前加 `fresh` 强制刷新复核一遍：

```bash
scroute fresh from "Patch City"
```

## 可选：双源核验（dual）

UEX 和 SC Trade Tools 两家数据源互相对照，防止被单一来源的过期价格坑：

```bash
pip install pycryptodome        # 仅 dual 需要，多装这一个包
scroute dual "Patch City"
```

站点改版可能导致对照失效，修复方法写在 `sctt_routes.py` 开头注释里。

## 高级参数

想精细控制时，用 `scroute raw` 把参数原样传给核心脚本（Windows 用户也是用这套参数直接跑 `python plan_route.py`）：

```bash
--ship "Hull B" / --scu 640        船名（模糊匹配自动取舱容）或直接给舱容 SCU 数
--capital 7000000 [--full]         本金；默认只用一半，--full 全押
--min-roi 30                       最低利润率 %
--origin-system Pyro               只在 Pyro 星系出发
--dest-system Stanton              只卖到 Stanton 星系
--space-only                       只看空间站（排除要手动搬箱的地面哨站）
--qd-speed 262                     你的量子巡航速度 Mm/s（影响时薪估算）
--refresh                          不用缓存，强制拉最新数据
```

例：Pyro 出发、350 万全押、只停空间站：

```bash
scroute raw --scu 640 --capital 3500000 --full --origin-system Pyro --space-only
```

临时换船/换本金不用改文件，直接加参数：`scroute --ship "Hull B" --capital 5000000 from Baijini`

## 它的推荐逻辑（一句话版）

同时满足三个条件才会出现在表里：**装得满一船**（买入站现货 ≥ 你的舱容）、**全额买得起**、**目的地卖得掉**（目的地的「预测需求 − 现有库存」还装得下你一船货）。装不满或卖不掉的路线直接不推荐，避免白跑。

## 数据说明

价格来自 UEX 玩家众包上传，天然有延迟和误差。脚本的保守时薪是把空驶返程都算进去的下限，找到返程货实际会更高。**众包数据仅供参考，出发前务必 `fresh` 复核。** 本项目与 CIG / UEX 无隶属关系。

## License

MIT
