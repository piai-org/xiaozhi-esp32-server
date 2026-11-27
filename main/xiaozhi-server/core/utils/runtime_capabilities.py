"""Allowlisted request capabilities; never serialize provider settings or credentials."""


def build_runtime_capabilities(config, functions=None, memory_str=None, intent_type=None,
                               input_origin="unknown", read_only=False):
    selected = config.get("selected_module", {})
    memory_id = selected.get("Memory")
    memory_config = config.get("Memory", {}).get(memory_id, {})
    memory_type = memory_config.get("type") or memory_id or "unspecified"
    no_memory = str(memory_type).lower() in ("nomem", "memory_nomem")
    names = []
    for definition in functions or []:
        function = definition.get("function", definition)
        name = function.get("name")
        # direct_answer selects an ordinary answer; it performs no real-world action.
        if name and name != "direct_answer" and name not in names:
            names.append(name)
    origin = input_origin if input_origin in ("asr", "user_text", "automatic_wakeup") else "unknown"
    capabilities = {
        "input": {"origin": origin,
                  "representation": "asr_transcript" if origin == "asr" else "text",
                  "dialect": "unknown", "recognition_confidence": "not_provided",
                  "audio_prosody_available_to_language_model": False},
        "tools_available_this_request": names,
        "tool_execution_evidence": "仅实际工具结果或应用明确提供的执行结果；助手承诺不是结果",
        "intent_routing": "前置意图路由，结果须有实际来源" if intent_type == "intent_llm" else "本轮工具定义为准",
        "memory_provider": str(memory_type),
        "cross_session_recall": "unavailable" if no_memory else "仅依实际检索结果，未保证完整历史或未来保存",
        "memory_returned_this_request": bool(memory_str and str(memory_str).strip()),
        "realtime_evidence": "仅上下文中实际提供的地点、日期及匹配资料或真实查询结果；未提供即未知",
        "conversation_retention_policy": "not_provided；不能由记忆设置推断日志或隐私政策",
    }
    if read_only:
        capabilities["information_lookup"] = {
            "available_topics": ["news", "weather", "general"],
            "status": "可发起只读查询；是否查到以本轮实际结果为准",
            "side_effect_tools": "unavailable",
        }
    return capabilities
