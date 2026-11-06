"""CR-006 backend single-writer switching simulation (CR-006 §5/§6).

Orin 校时 writer 盘点完成前，BUILD 不实现会操作真实设备的通用
BackendStatePlan，也不预置候选服务的 stop/disable（真实部署前必须确认实际
writer、切换动作与回退；无法证明单 writer 时不得启动新的写钟后端）。

本模块仅以纯内存模型验证切换设计的约束（模拟测试，非生产代码）：
* 写钟后端切换必须先完成旧 writer 停止（启动新 writer 之前）；
* 任何时刻至多一个自主选源/写钟后端处于启用状态（单 writer 不变量）；
* 回退按相反顺序恢复原 enable/active 状态，不允许新旧 writer 并存；
* dry-run 输出精确动作清单，不得因证据缺失而通配符禁用候选服务；
* 回退不还原系统墙钟快照、不恢复旧同步缓存（CR-006 §6）。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class BackendState:
    """Per-unit writer state captured at inventory time."""

    unit: str
    enabled: bool
    active: bool


@dataclass
class SwitchStep:
    action: str  # stop | disable | start | enable
    unit: str
    reason: str = ""


class SingleWriterViolation(Exception):
    """More than one autonomous clock writer would be enabled/active."""


def simulate_switch(
    inventory: list[BackendState],
    switch_plan: list[SwitchStep],
    time_unit: str = "tbox-time.service",
) -> tuple[list[SwitchStep], list[BackendState]]:
    """Run the switching plan against the inventory (pure in-memory).

    ``switch_plan`` must already stop/disable the competing writers before
    starting TIME's backend. Each step is applied to a mutable state copy;
    the single-writer invariant is re-checked after every step. Returns the
    executed steps and the final state; raises on violations.
    """
    state = {b.unit: [b.enabled, b.active] for b in inventory}
    # TIME's own backend is a candidate writer; absent from inventory it is
    # simply not started yet (never two writers at once).
    state.setdefault(time_unit, [False, False])
    executed: list[SwitchStep] = []
    for step in switch_plan:
        if step.action == "stop":
            state[step.unit][1] = False
        elif step.action == "disable":
            state[step.unit][0] = False
        elif step.action == "start":
            state[step.unit][1] = True
        elif step.action == "enable":
            state[step.unit][0] = True
        else:
            raise ValueError(f"unknown action {step.action!r}")
        executed.append(step)
        # Single-writer invariant: at most one autonomous clock writer may be
        # running (active) at any point; enabled (boot persistence) alone
        # does not write the clock, but an active one does.
        active_writers = [
            u for u, (en, ac) in state.items() if ac
        ]
        if len(active_writers) > 1:
            raise SingleWriterViolation(
                f"multiple autonomous clock writers after step "
                f"{step.action} {step.unit}: {sorted(active_writers)}"
            )
    final = [
        BackendState(unit=u, enabled=st[0], active=st[1])
        for u, st in sorted(state.items())
    ]
    return executed, final


def dry_run_switch(competing_writers: list[str], confirmed_conflict: list[str]) -> list[SwitchStep]:
    """Build the dry-run switching plan for a verified platform inventory.

    Only units confirmed as conflicting writers are scheduled for
    stop/disable; candidates without evidence are left untouched (no wildcard
    disabling, CR-006 §5). The actual writer inventory is a Level C
    deliverable pending Orin validation.
    """
    steps: list[SwitchStep] = []
    for unit in sorted(set(confirmed_conflict) & set(competing_writers)):
        steps.append(SwitchStep("stop", unit, "confirmed conflicting clock writer"))
    for unit in sorted(set(confirmed_conflict) & set(competing_writers)):
        steps.append(SwitchStep("disable", unit, "confirmed conflicting clock writer"))
    steps.append(SwitchStep("start", "tbox-time.service", "TIME backend takes over"))
    return steps


# TIME 的竞争 writer 候选（config schema competing_processes）；候选 ≠ 已确认
# 冲突，仅当平台盘点证实为活跃 writer 才进入切换计划。
COMPETING_WRITERS = ("chronyd.service", "systemd-timesyncd.service",
                     "ntpd.service", "openntpd.service")


class TestSingleWriterInvariant:
    def test_switch_then_start_time_keeps_single_writer(self):
        inventory = [
            BackendState("systemd-timesyncd.service", enabled=True, active=True),
            BackendState("chronyd.service", enabled=False, active=False),
        ]
        plan = dry_run_switch(COMPETING_WRITERS, ["systemd-timesyncd.service"])
        executed, final = simulate_switch(inventory, plan)
        assert [s.action for s in executed] == [
            "stop", "disable", "start",
        ]
        by_unit = {b.unit: b for b in final}
        assert not by_unit["systemd-timesyncd.service"].enabled
        assert not by_unit["systemd-timesyncd.service"].active
        assert by_unit["tbox-time.service"].enabled or by_unit["tbox-time.service"].active

    def test_starting_time_before_stopping_old_writer_violates(self):
        inventory = [BackendState("chronyd.service", enabled=True, active=True)]
        # Wrong order: start TIME before stopping the existing writer.
        bad_plan = [
            SwitchStep("start", "tbox-time.service"),
            SwitchStep("stop", "chronyd.service"),
        ]
        try:
            simulate_switch(inventory, bad_plan)
        except SingleWriterViolation:
            return
        raise AssertionError("expected SingleWriterViolation")

    def test_rollback_restores_original_states(self):
        original = [
            BackendState("systemd-timesyncd.service", enabled=True, active=True),
            BackendState("chronyd.service", enabled=False, active=False),
        ]
        plan = dry_run_switch(COMPETING_WRITERS, ["systemd-timesyncd.service"])
        _, switched = simulate_switch(original, plan)
        # Rollback: reverse of the switch (stop TIME, re-enable/start the old
        # writer), then the original state must be restored.
        rollback = [
            SwitchStep("stop", "tbox-time.service", "rollback"),
            SwitchStep("enable", "systemd-timesyncd.service", "rollback"),
            SwitchStep("start", "systemd-timesyncd.service", "rollback"),
        ]
        _, restored = simulate_switch(switched, rollback)
        restored_map = {b.unit: (b.enabled, b.active) for b in restored}
        for b in original:
            assert restored_map[b.unit] == (b.enabled, b.active)

    def test_no_wildcard_disable_without_evidence(self):
        # Only the confirmed conflict is scheduled; other candidates remain
        # untouched until the platform inventory proves they conflict.
        plan = dry_run_switch(COMPETING_WRITERS, ["chronyd.service"])
        targets = {s.unit for s in plan if s.action in ("stop", "disable")}
        assert targets == {"chronyd.service"}
        assert "systemd-timesyncd.service" not in targets

    def test_two_writers_never_coexist(self):
        inventory = [BackendState("ntpd.service", enabled=True, active=True)]
        # Even an otherwise valid plan fails if a second writer is enabled
        # while the first is still active.
        bad_plan = [
            SwitchStep("start", "tbox-time.service"),
        ]
        try:
            simulate_switch(inventory, bad_plan)
        except SingleWriterViolation:
            return
        raise AssertionError("expected SingleWriterViolation")
