# scroute · 星际公民货运路线速查

零配置的 UEX 实时货运路线规划器：一条命令告诉你买什么、在哪买、卖到哪、赚多少、时薪多少。

![使用方案卡片](docs/scroute-usage-card.png)

## 特性

- **实时数据**：UEX API 2.0 玩家众包价格（本地缓存 25 分钟，服务端缓存约 30 分钟）
- **满仓校验**：只推荐「装得满一船、全额买得起、目的地卖得掉」的路线
- **剩余需求过滤**：按官方口径 `预测需求 − 站点库存` 计算「需剩」，装不下一船的路线直接砍掉
- **时薪模型**：含量子巡航（可按船标定 QD 速度）、点火、对接/降落、自动装卸/手动搬箱、空驶返程的保守时薪
- **实用标记**：自动/手动装卸与箱型、违禁品 ⚠️、跨星系 ⚠️、官方 routes 交叉验证 ✓
- **星系过滤 / 仅空间站**：`--origin-system Pyro --space-only`

## 依赖

- Python ≥ 3.10（仅标准库）
- `curl`（Linux/macOS 自带；Windows 10+ 自带 `curl.exe`）
- 可选：`sctt_routes.py`（SC Trade Tools 双源对照）需 `pip install pycryptodome`

## 安装

```bash
git clone https://github.com/fatinghenji/scroute.git
cd scroute
# Linux/macOS：软链到 PATH
ln -s "$PWD/scroute" ~/.local/bin/scroute
# Windows：直接用 python 跑核心脚本
#   python plan_route.py --scu 640 --capital 7000000
```

编辑 `scroute` 开头的 `SHIP` / `CAPITAL` / `MIN_ROI` 三行改成自己的船和本金。

## 用法

```bash
scroute                          # 默认船+本金，全局 Top 15
scroute from "Patch City"        # 去程：从某站出发买什么
scroute to "Patch City"          # 返程：买什么拉回某站卖
scroute loop "Patch City"        # 环形：去程+返程一次出
scroute fresh ...                # 忽略缓存强制刷新（出发前复核库存）
scroute raw <参数>               # 透传全部高级参数
```

高级参数（`scroute raw` 或 `python plan_route.py`）：

```
--ship "Hull B" / --scu 640        船名（模糊匹配自动取舱容）或直接 SCU
--capital 7000000 [--full]         本金；默认只用一半（半仓红线），--full 押满
--min-roi 30                       最低 ROI%
--origin-system Pyro               限定出发星系
--dest-system Stanton              限定目的星系
--space-only                       仅空间站终端（排除地面手动搬箱哨站）
--qd-speed 262                     量子巡航速度 Mm/s（影响时薪）
--refresh                          强制刷新缓存
```

示例（Pyro 出发、350 万全押、仅空间站）：

```bash
scroute raw --scu 640 --capital 3500000 --full --origin-system Pyro --space-only
```

## 内置过滤规则

- 买入：`status_buy ≥ 5` 且 `scu_buy ≥ 舱容` 且 `买价 ≤ 预算 ÷ 舱容`
- 卖出：`status_sell ≤ 2` 且 `需剩（scu_sell − scu_sell_stock）≥ 舱容`
- 全局：ROI ≥ 30%（可调）；本金默认最多押一半

## 数据说明

价格来自 UEX 玩家众包上传，有延迟和误差，**出发前务必 `fresh` 复核库存**。本项目与 CIG / UEX 无隶属关系。

## License

MIT
