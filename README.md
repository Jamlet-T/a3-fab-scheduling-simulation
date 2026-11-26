# fabcosim｜晶圆厂生产—运输联合仿真

清华大学工业工程系《半导体制造系统的智能调度》课程设计（A3 组）的仿真平台。生产侧用 SimPy 承担离散事件与主时钟，运输侧用 SUMO／libsumo 显式模拟天车（OHT）运动，两侧通过端口交接物料。生产数据取自 SMT2020 的 LVHM_E 场景，空间布局与搬运数据取自 SMAT2022 的对应场景。

本仓库面向合作小组，只包含运行仿真所需的代码、配置、测试与数据；汇报材料、研究记录和历史运行结果不在其中。

## 仓库结构

| 目录 | 内容 |
| --- | --- |
| `src/fabcosim/` | 仿真核心：`production/`（工序、成批、换型、准入与设备状态）、`transport/`（SUMO 车队、固定时延与记录）、`cosim/`（联合主流程、指标、事件日志、检查点、回放）、`policies/`（派工、选机、目的地与规则注册）、`data/`（工作簿与路网接入）、`viz/`（只读结果网页） |
| `configs/` | 场景与实验配置。纯生产配置的关键字段见各自 `assumptions`；`network` 段声明空间数据来源 |
| `tests/` | 关键行为测试：时间顺序、物料不丢失不重复、成批与换型、CQT 窗口、端到端小场景等 |
| `scripts/` | `verify_collaboration_data.py`：核验随仓库提供的数据与冻结清单一致 |
| `03-数据/` | 两份工作簿与四份冻结派生 CSV，说明见[协作数据准备](03-数据/协作数据说明.md) |
| `04-研究/` | 只保留冻结基准的生成与核验脚本（`freeze_data_baseline.py`、`verify_data_baseline.py`） |

## 环境与安装

需要 Python 3.11 及以上。只用纯生产模型时不必安装 SUMO 相关依赖。

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev,transport,sumo-tools]"
```

`transport` 与 `sumo-tools` 两档提供 libsumo／traci／sumolib 与 SUMO 可执行文件，只在联合模型（真实车辆运动）时需要。

## 快速验证

```powershell
# 六份运行输入与冻结清单的 SHA256 核对
.venv\Scripts\python.exe scripts/verify_collaboration_data.py

# 两设施、一辆 OHT、一个 lot 的最小闭环
.venv\Scripts\python.exe -m fabcosim demo --config configs/smat_two_tools.json

# 核心测试
.venv\Scripts\python.exe -m pytest -q
```

长场景示例：

```powershell
# 纯生产：完整十产品路线，无车辆
.venv\Scripts\python.exe -m fabcosim full --config configs/production_only_30d_110d.json --output runs/production-30d-110d

# 联合模型：完整路线 + 真实 OHT
.venv\Scripts\python.exe -m fabcosim full --config configs/smat_full_product1.json --output runs/full-product1
```

每次运行的配置、事件、指标和回放写入 `--output` 指定的新目录，已有目录不会被覆盖。

## 模型范围

- **纯生产模型**：十产品完整工艺路线（含 Delay 范围声明）、严格成批、换型、抽检、返工、PM 与故障、真实投片日历与交期、输入缓冲准入；不包含车辆、路网与运输任务，跨设备交接按配置的固定时延近似。
- **联合模型**：在纯生产基础上增加 SMAT2022 路网、OHT 车队、端口交接与车辆运动；每 0.2 s 推进一次交通，车辆状态与物料归属逐步核查。
- **可调决策点**：选机（设备组内选具体设备）、机台作业排序（FIFO／SPT／EDD／CR 等）、开批组顺序、运输任务排序与选车。派工默认在机台可用时决策（`dispatch_mode: deferred`）。
- **边界**：本仓库是课程范围内可复现的评测平台，不是经完整标定的晶圆厂模型，也不是 SMT2020／SMAT2022 原文结果的原样复现。快速档（`wafer_detail`）只保留批次末片的逐片事件，不能用于定量研究基线；固定时延运输近似只支撑生产派工类结论，不用于运输调度、拥堵与安全间距结论。各配置的 `assumptions` 字段列出对应场景的具体假设。

## 与设施布局协作的接口

- **空间数据**：`network.type` 为 `smat2022` 时，模型读取工作簿中的 `Address`（节点坐标）、`Rail`（轨道起止节点）、`Equipment`、`Port`、`Vehicle`、`VehicleType` 六张表，由 `src/fabcosim/data/smat_network.py` 直接生成 SUMO 原生路网 `network.net.xml`（不做道路转换器处理，不合并源节点）。工作簿坐标乘以 `network.coordinate_scale`（当前配置为 0.001）换算为米。
- **生产数据**：设备组、路线与投片日历来自 `03-数据/基准-v1/` 的四份 CSV 与 `manifest.json`；设备组名称需与该映射一致，替换数据时保持文件名与哈希清单同步。
- **替换为自制布局**：保持上述工作表字段语义，或提供等价的 `network` 适配层；设备组与工序编号仍需与生产侧路线对齐。需要按新布局重算时，建议新建数据版本目录，不要就地修改冻结文件。

## 数据来源

- 生产数据：SMT2020 官方场景包中的 LVHM_E（dataset 4）。
- 空间与搬运数据：SMAT2022 的 LVHM_E（dataset 4），上游仓库与提交号记录在 `03-数据/基准-v1/manifest.json`。
- 仓库只随代码提供运行所需的两个工作簿与四份派生 CSV；完整上游压缩包、文档与校验脚本见[协作数据准备](03-数据/协作数据说明.md)。

## 测试与协作

- `pytest` 覆盖仿真核心行为；依赖大工作簿的用例在文件缺失时自动跳过。研究分析类脚本与其测试未包含在本仓库。
- 协作方式：从 `main` 建分支提交，推送后向 `main` 发起 Pull Request，说明改动内容、运行命令与验证结果。
- 第三方数据沿用各自上游的许可与引用要求；本仓库代码的许可以课程与组内约定为准。
