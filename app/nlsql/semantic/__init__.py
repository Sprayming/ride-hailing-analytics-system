"""语义查询层：受控计划 → 程序校验 → 人工确认 → 参数化编译 → 只读执行。

模块：
    catalog       指标目录（模型能看到的唯一事实来源）
    plan_contract 计划归一化与合法性校验
    query_rules   决策校验、原话依据、澄清与点选
    compiler      计划 → 参数化 SQL
    executor      只读执行（mode=ro + 超时 + 行数上限）
    planner       模型调用与结构化输出解析
    workflow      LangGraph 状态机与会话存取
"""
