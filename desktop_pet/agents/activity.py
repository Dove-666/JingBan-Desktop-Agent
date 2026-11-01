"""Public execution labels, independent of model reasoning and tool arguments."""
TOOL_LABELS = {
    "system_info": "查看系统信息", "discover_applications": "查找已安装应用",
    "inspect_system": "检查电脑状态", "inspect_disk_usage": "检查磁盘占用", "inspect_directory": "查看目录信息",
    "read_file": "读取文件", "write_file": "写入文件", "search_files": "查找文件",
    "web_fetch": "读取网页", "computer_use": "执行电脑操作", "open_application": "打开应用",
    "calculate": "精确计算", "analyze_csv": "核算表格数据", "verify_artifact": "验收输出结果",
    "update_plan": "更新任务计划", "finish_task": "检查任务完成条件",
    "desktop_observe": "读取界面控件", "desktop_act": "操作界面控件",
    "desktop_verify_state": "核对控件状态", "verify_desktop_goal": "确认操作结果",
}
ACTION_LABELS = {
    "observe": "查看当前画面", "click": "点击界面", "left_click": "点击界面",
    "left_single": "点击界面", "left_double": "双击界面", "double_click": "双击界面",
    "right_click": "右键点击", "right_single": "右键点击", "middle_click": "中键点击",
    "drag": "拖动界面元素", "left_click_drag": "拖动界面元素", "select": "选择界面元素",
    "hover": "移动指针", "mouse_move": "移动指针", "type": "输入文字",
    "hotkey": "使用快捷键", "scroll": "滚动界面", "wait": "等待界面响应",
    "finished": "电脑操作返回结果", "call_user": "需要手动接手",
    "verify": "确认操作结果",
    "error_env": "检查操作环境", "user_stop": "停止电脑操作",
}

def public_activity(frame):
    """Construct the entire callback value; discard all untrusted extra fields."""
    labels = TOOL_LABELS if frame["kind"] == "tool" else ACTION_LABELS
    value = {"id": frame["event_id"], "kind": frame["kind"], "label": labels[frame["name"]],
             "status": frame["status"]}
    if "step" in frame:
        value["step"] = frame["step"]
    return value
