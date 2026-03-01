#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class UArch:
    issue_ports: int
    load_ports: int
    store_ports: int
    idu_window_width: int
    idu_issue_width: int
    ooo_window_width: int
    vreg_num: int


@dataclass
class ISAProfile:
    pipeline_startup_cost: int
    latency: int
    throughput: int
    pipeline_drain_cost: int
    data_load_cost: int
    data_store_cost: int


@dataclass
class ISAData:
    vf_startup_cost: int
    vf_drain_cost: int
    profiles: dict[str, dict[str, ISAProfile]]


@dataclass
class Inst:
    seq: int
    op: str
    src: list[str]
    dst: list[str]
    loop_ids: tuple[str, ...]
    loop_iters: tuple[int, ...]
    compute_bind_op: str | None = None

    dispatch_cycle: int | None = None
    start_cycle: int | None = None
    done_cycle: int | None = None
    forward_cycle: int | None = None
    state: str = "new"

    src_phys: list[str] = field(default_factory=list)
    dst_phys: list[str] = field(default_factory=list)
    old_dst_phys: list[str] = field(default_factory=list)

    def kind(self) -> str:
        if self.op == "VLD":
            return "load"
        if self.op == "VST":
            return "store"
        return "compute"


class IFU:
    def __init__(self, test_json: dict[str, Any]):
        self.dtype = test_json["dtype"]
        self.params = test_json.get("params", {})
        self.program = test_json["program"]

    def _resolve(self, value: Any) -> int:
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            if value.isdigit():
                return int(value)
            if value in self.params:
                return int(self.params[value])
        raise ValueError(f"Cannot resolve loop param: {value}")

    @staticmethod
    def _is_pow2(n: int) -> bool:
        return n > 0 and (n & (n - 1)) == 0

    @staticmethod
    def _rewrite_reg(name: str, lane: int) -> str:
        if name.startswith("V"):
            return f"{name}_lane{lane}"
        return name

    @staticmethod
    def _bind_compute_op_for_body(body: list[dict[str, Any]]) -> str | None:
        # 优先绑定到本 body 中首个 compute 指令
        for node in body:
            if node["type"] == "inst" and node["op"] not in {"VLD", "VST"}:
                return node["op"]
        return None

    def expand(self) -> list[Inst]:
        out: list[Inst] = []
        seq = 0

        def emit_inst(node: dict[str, Any], loop_ids: list[str], loop_iters: list[int], lane: int | None, bind_op: str | None) -> None:
            nonlocal seq
            src = list(node.get("src", []))
            dst = list(node.get("dst", []))
            if lane is not None:
                src = [self._rewrite_reg(s, lane) for s in src]
                dst = [self._rewrite_reg(d, lane) for d in dst]
            out.append(
                Inst(
                    seq=seq,
                    op=node["op"],
                    src=src,
                    dst=dst,
                    loop_ids=tuple(loop_ids),
                    loop_iters=tuple(loop_iters),
                    compute_bind_op=bind_op,
                )
            )
            seq += 1

        def walk(nodes: list[dict[str, Any]], loop_ids: list[str], loop_iters: list[int]) -> None:
            for node in nodes:
                ntype = node["type"]
                if ntype == "inst":
                    emit_inst(node, loop_ids, loop_iters, lane=None, bind_op=node["op"])
                    continue

                if ntype != "loop":
                    raise ValueError(f"Unsupported node type: {ntype}")

                iters = self._resolve(node["iters"])
                unroll = self._resolve(node.get("unroll", 1))
                if iters % unroll != 0 or not self._is_pow2(unroll):
                    raise ValueError(
                        f"Invalid unroll={unroll} for iters={iters}, must be power-of-two and divisor"
                    )

                loop_name = f"L{len(loop_ids)}"
                bind_op = self._bind_compute_op_for_body(node["body"])
                for it in range(iters // unroll):
                    if unroll == 1:
                        # 递归保留非最内层 loop
                        sub_has_loop = any(ch["type"] == "loop" for ch in node["body"])
                        if sub_has_loop:
                            walk(node["body"], loop_ids + [loop_name], loop_iters + [it])
                        else:
                            for child in node["body"]:
                                if child["type"] != "inst":
                                    raise ValueError("Unexpected non-inst in non-recursive loop body")
                                emit_inst(child, loop_ids + [loop_name], loop_iters + [it], lane=None, bind_op=bind_op)
                    else:
                        # unroll(2) for {A,B,C} -> for {A0,A1,B0,B1,C0,C1}
                        for child in node["body"]:
                            if child["type"] != "inst":
                                raise ValueError("unroll only supports innermost instruction-only body")
                            for lane in range(unroll):
                                emit_inst(child, loop_ids + [loop_name], loop_iters + [it], lane=lane, bind_op=bind_op)

        walk(self.program, [], [])
        return out


class Simulator:
    def __init__(self, uarch: UArch, isa: ISAData, dtype: str, insts: list[Inst]):
        self.uarch = uarch
        self.isa = isa
        self.dtype = dtype
        self.insts = insts

        self.cycle = 0
        self.fetch_idx = 0
        self.idu_window: deque[Inst] = deque()
        self.ooo_queue: deque[Inst] = deque()

        self.rat: dict[str, str] = {}
        self.free_phys: deque[str] = deque([f"P{i}" for i in range(uarch.vreg_num)])

        self.ready_cycle: dict[str, int] = defaultdict(lambda: 0)
        self.started_log: dict[int, list[str]] = defaultdict(list)
        self.done_log: dict[int, list[str]] = defaultdict(list)

        # 跨循环 IDU 发射约束基线：earliest_issue = base + iter
        self.loop_issue_base: dict[tuple[str, ...], int] = {}

        # 用于日志中插入 VLOOPv2 事件（每层 loop 每个迭代上下文记录一次）
        self.seen_vloop_events: set[tuple[tuple[str, ...], tuple[int, ...], int]] = set()

    def _normalize_op(self, op: str) -> str:
        if op in self.isa.profiles and self.dtype in self.isa.profiles[op]:
            return op
        # 常见别名：VADD -> VADDS
        if f"{op}S" in self.isa.profiles and self.dtype in self.isa.profiles[f"{op}S"]:
            return f"{op}S"
        return op

    def _profile_for_compute(self, op: str) -> ISAProfile:
        nop = self._normalize_op(op)
        if nop not in self.isa.profiles or self.dtype not in self.isa.profiles[nop]:
            raise ValueError(f"ISA profile missing for op={op}, dtype={self.dtype}")
        return self.isa.profiles[nop][self.dtype]

    def _profile_for_inst(self, inst: Inst) -> ISAProfile:
        op = inst.compute_bind_op or inst.op
        return self._profile_for_compute(op)

    def _dispatch_from_ifu(self) -> None:
        while self.fetch_idx < len(self.insts) and len(self.idu_window) < self.uarch.idu_window_width:
            self.idu_window.append(self.insts[self.fetch_idx])
            self.fetch_idx += 1

    def _allocate_phys(self, vreg: str) -> tuple[str, str | None]:
        if not self.free_phys:
            raise RuntimeError("No free physical registers available")
        newp = self.free_phys.popleft()
        oldp = self.rat.get(vreg)
        self.rat[vreg] = newp
        return newp, oldp

    def _loop_issue_ok(self, inst: Inst) -> bool:
        # 规则：iters=n 最早在 t0+n 发射（约束在 IDU->OoO 发射时刻）
        for depth in range(len(inst.loop_ids)):
            lid_path = inst.loop_ids[: depth + 1]
            iter_v = inst.loop_iters[depth]
            base = self.loop_issue_base.get(lid_path)
            if base is None:
                continue
            if self.cycle < base + iter_v:
                return False
        return True

    def _record_loop_issue_base(self, inst: Inst) -> None:
        for depth in range(len(inst.loop_ids)):
            lid_path = inst.loop_ids[: depth + 1]
            iter_v = inst.loop_iters[depth]
            if lid_path not in self.loop_issue_base:
                self.loop_issue_base[lid_path] = self.cycle - iter_v

    def _maybe_log_vloop(self, inst: Inst) -> None:
        for depth in range(len(inst.loop_ids)):
            loop_path = inst.loop_ids[: depth + 1]
            iter_path = inst.loop_iters[: depth + 1]
            key = (loop_path, iter_path, depth)
            if key in self.seen_vloop_events:
                continue
            self.seen_vloop_events.add(key)
            self.started_log[self.cycle].append(f"VLOOPv2({loop_path[-1]}, iter={iter_path[-1]})")

    def _idu_issue(self) -> None:
        can_issue = min(
            self.uarch.idu_issue_width,
            len(self.idu_window),
            self.uarch.ooo_window_width - len(self.ooo_queue),
        )
        issued = 0
        stalled: deque[Inst] = deque()

        while self.idu_window and issued < can_issue:
            inst = self.idu_window.popleft()

            if not self._loop_issue_ok(inst):
                stalled.append(inst)
                continue

            need_regs = sum(1 for d in inst.dst if d.startswith("V"))
            if need_regs > len(self.free_phys):
                stalled.append(inst)
                continue

            for s in inst.src:
                if s.startswith("V"):
                    inst.src_phys.append(self.rat.get(s, "UNMAPPED"))
                else:
                    inst.src_phys.append(s)

            for d in inst.dst:
                if d.startswith("V"):
                    newp, oldp = self._allocate_phys(d)
                    inst.dst_phys.append(newp)
                    inst.old_dst_phys.append(oldp or "")
                else:
                    inst.dst_phys.append(d)
                    inst.old_dst_phys.append("")

            self._record_loop_issue_base(inst)
            self._maybe_log_vloop(inst)

            inst.dispatch_cycle = self.cycle
            inst.state = "queued"
            self.ooo_queue.append(inst)
            issued += 1

        while self.idu_window:
            stalled.append(self.idu_window.popleft())
        self.idu_window = stalled

    def _inst_can_start(self, inst: Inst) -> bool:
        for s in inst.src_phys:
            if s == "UNMAPPED":
                return False
            if s.startswith("P") and self.ready_cycle[s] > self.cycle:
                return False
        return True

    def _inst_duration(self, inst: Inst) -> int:
        profile = self._profile_for_inst(inst)
        if inst.kind() == "load":
            return profile.data_load_cost
        if inst.kind() == "store":
            return profile.data_store_cost
        return profile.latency

    def _forward_ready_cycle(self, inst: Inst) -> int:
        profile = self._profile_for_inst(inst)
        if inst.kind() == "load":
            # producer VLD 在 t0 开始后，consumer compute 最早 t0 + pipeline_startup_cost
            return (inst.start_cycle or 0) + profile.pipeline_startup_cost
        if inst.kind() == "compute":
            return (inst.start_cycle or 0) + max(1, profile.latency - 3)
        return inst.done_cycle or 0

    def _start_exec(self) -> None:
        load_slots = self.uarch.load_ports
        store_slots = self.uarch.store_ports
        comp_slots = self.uarch.issue_ports

        for inst in self.ooo_queue:
            if inst.state != "queued":
                continue
            if not self._inst_can_start(inst):
                continue

            k = inst.kind()
            if k == "load" and load_slots <= 0:
                continue
            if k == "store" and store_slots <= 0:
                continue
            if k == "compute" and comp_slots <= 0:
                continue

            dur = self._inst_duration(inst)
            inst.start_cycle = self.cycle
            inst.done_cycle = self.cycle + dur
            inst.forward_cycle = self._forward_ready_cycle(inst)
            inst.state = "executing"

            for d in inst.dst_phys:
                if d.startswith("P"):
                    self.ready_cycle[d] = inst.forward_cycle

            self.started_log[self.cycle].append(f"#{inst.seq}:{inst.op}")
            if k == "load":
                load_slots -= 1
            elif k == "store":
                store_slots -= 1
            else:
                comp_slots -= 1

    def _mark_done(self) -> None:
        for inst in self.ooo_queue:
            if inst.state == "executing" and inst.done_cycle == self.cycle:
                inst.state = "done"
                self.done_log[self.cycle].append(f"#{inst.seq}:{inst.op}")

    def _release_fifo(self) -> None:
        while self.ooo_queue and self.ooo_queue[0].state == "done":
            inst = self.ooo_queue.popleft()
            inst.state = "released"
            # retire 规则：旧映射在新写对应指令释放后回收，不区分 loop 维度
            for oldp in inst.old_dst_phys:
                if oldp:
                    self.free_phys.append(oldp)

    def run(self) -> dict[str, Any]:
        self.cycle = self.isa.vf_startup_cost

        while True:
            self._mark_done()
            self._release_fifo()
            self._dispatch_from_ifu()
            self._idu_issue()
            self._start_exec()

            finished = self.fetch_idx >= len(self.insts) and not self.idu_window and not self.ooo_queue
            if finished:
                break
            self.cycle += 1

        total = self.cycle + self.isa.vf_drain_cost
        return {
            "total_cycles": total,
            "start_cycle": self.isa.vf_startup_cost,
            "end_cycle": self.cycle,
            "issued_instruction_count": len(self.insts),
            "started_per_cycle": dict(sorted(self.started_log.items())),
            "done_per_cycle": dict(sorted(self.done_log.items())),
        }


def load_uarch(path: Path) -> UArch:
    d = json.loads(path.read_text())
    return UArch(
        issue_ports=int(d["issue_ports"]),
        load_ports=int(d["load_ports"]),
        store_ports=int(d["store_ports"]),
        idu_window_width=int(d["IDU_window_width"]),
        idu_issue_width=int(d["IDU_issue_width"]),
        ooo_window_width=int(d["OoO_window_width"]),
        vreg_num=int(d["vreg_num"]),
    )


def load_isa(path: Path) -> ISAData:
    d = json.loads(path.read_text())
    defaults = d["defaults"]

    profiles: dict[str, dict[str, ISAProfile]] = {}
    for op, by_dtype in d["instructions"].items():
        profiles[op] = {}
        for dtype, p in by_dtype.items():
            profiles[op][dtype] = ISAProfile(
                pipeline_startup_cost=int(p["pipeline_startup_cost"]),
                latency=int(p["latency"]),
                throughput=int(p["throughput"]),
                pipeline_drain_cost=int(p["pipeline_drain_cost"]),
                data_load_cost=int(p["data_load_cost"]),
                data_store_cost=int(p["data_store_cost"]),
            )

    return ISAData(
        vf_startup_cost=int(defaults["vf_startup_cost"]),
        vf_drain_cost=int(defaults["vf_drain_cost"]),
        profiles=profiles,
    )


def main() -> None:
    ap = argparse.ArgumentParser(description="VF simulator (instruction-level, cycle-approx)")
    ap.add_argument("--program", default="VADD_oneloop.json")
    ap.add_argument("--uarch", default="uarch.json")
    ap.add_argument("--isa", default="isa.json")
    ap.add_argument("--pretty", action="store_true")
    args = ap.parse_args()

    test = json.loads(Path(args.program).read_text())
    ifu = IFU(test)
    insts = ifu.expand()

    uarch = load_uarch(Path(args.uarch))
    isa = load_isa(Path(args.isa))

    sim = Simulator(uarch, isa, ifu.dtype, insts)
    result = sim.run()

    if args.pretty:
        print(f"total_cycles={result['total_cycles']}")
        print(f"start_cycle={result['start_cycle']}, end_cycle={result['end_cycle']}")
        print(f"issued_instruction_count={result['issued_instruction_count']}")
        print("\ncycle start-exec:")
        for c, items in result["started_per_cycle"].items():
            print(f"  t={c}: {', '.join(items)}")
        print("\ncycle done:")
        for c, items in result["done_per_cycle"].items():
            print(f"  t={c}: {', '.join(items)}")
    else:
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
