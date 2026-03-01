# VF 仿真器

## 1. 设计目标

根据给定的 VF 程序代码，快速进行 VF 仿真模拟，模拟 vector 核内部指令级别的数据流动，得到 VF 运算总时间；支持 dump 每个 cycle 开始执行的指令、每个 cycle 执行完成的指令，方便与实际测试结果进行对比，便于后续代码维护与开发。

## 2. 功能覆盖

当前版本支持如下能力：

- 支持双发射：一个 cycle 内最多可以开始执行 2 个 VLD 指令、2 个计算指令、1 个 VST 指令。
- 支持寄存器重命名，支持模拟物理寄存器数目限制。
- 支持单循环、多循环、单指令、多指令场景。
- 支持循环 unroll 展开。

## 3. Vector 核内微架构信息

### (1) 架构信息（uarch.json）

```json
{
  "name": "Ascend_VF",
  "issue_ports": 2,
  "load_ports": 2,
  "store_ports": 1,
  "IDU_window_width": 6,
  "IDU_issue_width": 5,
  "OoO_window_width": 64,
  "vreg_num": 68
}
```

参数意义如下：

- **issue_ports**：一个 cycle 内最多能开始执行的计算指令数目。
- **load_ports**：一个 cycle 内最多能开始执行的 VLD 指令数目。
- **store_ports**：一个 cycle 内最多能开始执行的 VST 指令数目。
- **IDU_window_width**：IDU 的指令窗口宽度。
- **IDU_issue_width**：IDU 每个 cycle 最多能发射给 OoO 的指令数目。
- **OoO_window_width**：OoO 队列宽度。
- **vreg_num**：物理寄存器数目。

文件：[`uarch.json`](uarch.json)

### (2) 指令信息（isa.json）

指令信息包含两部分，以下以 `VADDS` 为例：

```json
{
  "defaults": {
    "vf_startup_cost": 23,
    "vf_drain_cost": 12
  },
  "instructions": {
    "VADDS": {
      "fp32": {
        "pipeline_startup_cost": 6,
        "latency": 7,
        "throughput": 1,
        "pipeline_drain_cost": 5,
        "data_load_cost": 9,
        "data_store_cost": 10
      }
    }
  }
}
```

参数意义如下：

- **vf_startup_cost**：VF 启动开销。指从开始运行 VF 结构体到能够发射第一个 VLD 指令所需时间。
- **vf_drain_cost**：VF 排空开销。指最后一个 VST 指令运行完毕到 VF 结构体结束所需时间。
- **pipeline_startup_cost**：
  - 对于单操作数指令（单输入）：指 VLD 指令开始执行到计算指令可以开始执行的时间；
  - 对于双操作数指令（两个输入）：指第二个 VLD 指令开始执行到计算指令可以开始执行的时间。
- **latency**：计算指令完整计算完所需时间。
- **throughput**：硬件 ALU 对于该计算指令执行完 throughput 时间后可开始执行下一条指令。
- **pipeline_drain_cost**：计算指令开始计算到该指令的 dst 执行 VST 的最小时间间隔。
- **data_load_cost**：该计算指令的 VLD 搬运数据所需完整时间。
- **data_store_cost**：该计算指令的 VST 搬运数据所需完整时间。

文件：[`isa.json`](isa.json)

> 目前仅测试了 fp32 数据，其他数据格式信息暂未测试。

## 4. 仿真器结构

### (1) test.json 文件

该文件包含 VF 代码信息。以单循环为例：

```json
{
  "dtype": "fp32",
  "params": {
    "I": 32,
    "U": 2
  },
  "program": [
    {
      "type": "loop",
      "iters": "I",
      "unroll": "U",
      "body": [
        { "type": "inst", "op": "VLD", "dst": ["V1"], "src": ["memA"] },
        { "type": "inst", "op": "VLD", "dst": ["V2"], "src": ["memB"] },
        { "type": "inst", "op": "VADD", "dst": ["V3"], "src": ["V1", "V2"] },
        { "type": "inst", "op": "VST", "dst": ["memB"], "src": ["V3"] }
      ]
    }
  ]
}
```

- 使用 `type` 标记 node 类型（循环体或指令）。
- 虚拟寄存器用 `V` 开头，UB 地址用 `mem` 开头。
- `loop` 支持循环次数 `iters` 与 unroll 次数 `U`。未写 `unroll` 时默认 `1`。

示例文件：[`VADD_oneloop.json`](VADD_oneloop.json)、[`VADD_twoloop.json`](VADD_twoloop.json)

### (2) IFU

- 读取 test.json 文件，识别循环体、识别 unroll。
- 将 VLD、VST 和计算指令按 loop 和 unroll 次数展开。
- unroll 必须是 2 的幂且为循环次数因子，否则报错。
- unroll 仅支持最内层循环。
- 为每个 loop 命名，记录每条指令所属循环，便于后续处理。

#### IFU 中 unroll 描述

`unroll(2) for {A, B, C} -> for {A, A, B, B, C, C}`

### (3) IDU

- 每个 cycle 接收来自 IFU 的指令，窗口宽度为 `IDU_window_width = 6`。
- 每个 cycle 最多发射 5 条指令给 OoO。
- 发射需遵循跨循环条件：若 `iters=0` 的指令在 `t0` cycle 开始发射，则 `iters=1` 最早在 `t0+1`，`iters=n` 最早在 `t0+n`。
- 读取 Freelist（空闲寄存器信息）后先进行检查，仅在有足够空闲寄存器时发射。

### (4) OoO

- 每个 cycle 接收来自 IDU 的指令放入队列，队列深度 `64`；若队列已满，暂停接收。
- 寄存器重命名通过 RAT（Register Alias Table）完成，映射需考虑 WAW/WAR/RAW。
- 读取 `isa.json` 与 `uarch.json` 获取发射执行信息。
- 每个 cycle 决定执行指令，可乱序执行。
- 执行需考虑依赖；后继指令可通过 forwarding 提前启动，不必等待前序完全完成。
- 指令释放按队列 FIFO 顺序：执行可乱序，释放需顺序。
- 寄存器 retire：当该物理寄存器对应的虚拟寄存器被后续指令写入，且后续指令释放后，旧物理寄存器才可退休。
- 多重循环下，指令开始执行还受 loop begin 对应执行时间限制。该事件在日志中以 `VLOOPv2` 形式出现。

#### OoO 约束说明

##### (a) 指令释放

OoO 指令执行可乱序，但释放需按先入先出：后面的即使先完成，也必须等待前面已释放。

##### (b) 寄存器重命名

- RAW：虚拟寄存器可映射到同一物理寄存器。
- WAW/WAR：需映射到不同物理寄存器。
- Freelist 记录空闲物理寄存器，RAT 记录映射关系。

**retire（无循环）示意**

- 第一条 VADD：`RAT(V1)=P11, RAT(V2)=P12, RAT(V0)=P30`。
- 第二条 VADD：`RAT(V3)=P30, RAT(V0)=P31`。
- 当 `V0` 被新值写到 `P31` 后，旧 `P30` 仍不能立刻退休；需等第二条 VADD 执行并从队列释放后才可退休。

**retire（有循环、无 unroll）示意**

```asm
for {
    VLD(dst: V0, src: memA)
    VADDS(dst: V1, src: V0)
    VST(dst: memB, src: V1)
}
```

- `iters=0`：`RAT(V0)=P0, RAT(V1)=P1`。
- `iters=1`：`RAT(V0)=P2, RAT(V1)=P3`。
- `P0/P1` 退休条件：`iters=1` 对应的覆盖写指令释放后。

**retire（有循环、有 unroll）示意**

`unroll=2` 展开后，为区分 lane，需要在 IFU 阶段区分寄存器，例如：

```asm
unroll(2)
for {
    VLD(dst: V0_lane0, src: memA)
    VLD(dst: V0_lane1, src: memA)
    VADDS(dst: V1_lane0, src: V0)
    VADDS(dst: V1_lane1, src: V0)
    VST(dst: memB, src: V1_lane0)
    VST(dst: memB, src: V1_lane1)
}
```

##### (c) VLOOPv2 限制

- VLOOPv2 主要影响多层嵌套循环。
- 每次遇到 `LOOP_BEGIN` 都会转化执行一次 `VLOOPv2` 来更新循环参数（起始地址、offset、mask 等）。
- VLOOPv2 不走 OoO 流程，但会限制其对应 loop 内指令是否可执行。
- 某内层 loop 的指令最早开始执行时间 = 对应 VLOOPv2 开始时间 + 4。

两层循环下，内层 `VLOOPv2` 触发间隔近似满足：

- `gap0 = N + 3`

三层循环下，近似满足：

- `gap1 = gap0 + 5`

四层循环下，近似满足：

- `gap2 = gap1 + 5`

此外：

- `t0 = VF 结构体开始时间 + 19`


## 5. 运行方式（当前实现）

当前仓库提供一个可运行的 Python 仿真器实现：`simulator.py`。

```bash
python simulator.py --program VADD_oneloop.json --uarch uarch.json --isa isa.json --pretty
```

可选参数：

- `--program`：VF 程序 JSON（默认 `VADD_oneloop.json`）
- `--uarch`：微架构参数 JSON（默认 `uarch.json`）
- `--isa`：ISA 时序参数 JSON（默认 `isa.json`）
- `--pretty`：以人类可读日志格式输出每个 cycle 的开始/完成指令

### 当前实现已覆盖

- IFU：循环展开、最内层 unroll 展开、lane 寄存器重写（`Vx -> Vx_laneN`）。
- IDU：窗口限制、每 cycle 最大发射宽度、基于空闲物理寄存器数量的发射阻塞。
- OoO：
  - 队列容量限制；
  - 乱序开始执行（受端口数与依赖限制）；
  - 顺序释放（FIFO retire/release）；
  - RAT + Freelist 的寄存器重命名与旧版本物理寄存器回收。
- 输出：
  - `total_cycles`（含 startup/drain）；
  - 每个 cycle 的开始执行指令与完成执行指令。

### 规则对齐（已实现）

- 跨循环发射约束作用于 **IDU 发射到 OoO** 的时刻：对于同一 loop，`iters=n` 的指令最早在 `t0+n` 发射（`t0` 为该 loop `iters=0` 首次发射周期）。
- `pipeline_startup_cost` 作用于 VLD->compute 依赖：producer `VLD` 在 `t0` 开始执行后，consumer compute 最早在 `t0 + pipeline_startup_cost` 可启动。
- `data_load_cost/data_store_cost` 按算子 profile 绑定：每条指令会绑定到对应 compute 算子的 profile 读取 load/store 成本。
- `VLOOPv2` 事件已记录到每 cycle 开始执行日志（`started_per_cycle`）。
- 物理寄存器 retire 采用“旧映射在覆盖写指令释放后回收”的统一规则，不区分 loop 维度。

### 当前实现假设（待后续细化）

- ISA 时序参数当前使用单一 profile（优先 `VADDS/<dtype>`）作为通用计算/搬运成本。
- forwarding 采用 `latency - 3` 的近似规则（下限 1 cycle）。
- 尚未完整实现多层循环 `VLOOPv2` 的层级时间门控，仅保留基础循环展开与执行。

