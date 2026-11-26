"""生产模型范围：保留源步骤身份，明确排除SMT2020的Delay_32抽象阶段。"""


def apply_production_scope(config: dict) -> None:
    """原地声明纯生产执行范围；联合模型缺省输入保持原样。

    不过滤或重新编号operations，返工/专机/CQT继续按原source_step引用。
    指向被排除阶段的约束没有可靠的自动迁移语义，故在启动前拒绝，而非静默改指。
    """
    pure = config.get("transport", {}).get("model") == "production_only"
    production = config["production"]
    policy = production.get("delay32_policy", "exclude" if pure else "retain")
    if policy not in ("exclude", "retain"):
        raise ValueError("production.delay32_policy必须为exclude或retain")
    if not pure:
        if policy != "retain":
            raise ValueError("Delay_32排除范围目前仅支持production_only")
        return
    production["delay32_policy"] = policy
    for name, route in config["routes"].items():
        ops = route["operations"]
        excluded = {
            op["source_step"]
            for op in ops
            if policy == "exclude" and op["tool_group"] == "Delay_32"
        }
        for op in ops:
            if op["source_step"] in excluded:
                if not op["virtual"]:
                    raise ValueError(f"{name}步骤{op['source_step']}的Delay_32未标为虚拟阶段")
                if any(
                    op.get(field) for field in ("dedication_step", "cqt_step", "rework_probability")
                ):
                    raise ValueError(f"{name}被排除Delay_32自身含生产约束，需明确迁移语义")
            for field in ("dedication_step", "cqt_step", "rework_step"):
                if op.get(field, 0) in excluded:
                    raise ValueError(f"{name}步骤{op['source_step']}的{field}指向被排除Delay_32")
        route["excluded_source_steps"] = sorted(excluded)
        route["execution_step_count"] = len(ops) - len(excluded)
        for op in ops:
            if op["source_step"] in excluded:
                op["excluded_from_execution"] = True
            else:
                op.pop("excluded_from_execution", None)
        if excluded:
            remaining = 0.0
            suffix = {}
            for op in reversed(ops):
                if op["source_step"] not in excluded:
                    remaining += op["processing_s"]
                suffix[op["source_step"]] = remaining
            route["remaining_s"] = dict(reversed(list(suffix.items())))
            route["rpt_s"] = remaining
