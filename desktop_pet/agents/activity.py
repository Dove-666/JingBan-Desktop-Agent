"""Public execution labels, independent of model reasoning and tool arguments."""
# Historical labels remain readable; only DSH_TOOL_NAMES are advertised.
TOOL_LABELS = {'read_file': '读取文件',
 'write_file': '写入文件',
 'search_files': '查找文件',
 'calculate': '精确计算',
 'analyze_csv': '核算表格数据',
 'verify_artifact': '验收输出结果',
 'update_plan': '更新任务计划',
 'finish_task': '检查任务完成条件',
 'web_fetch': '阅读网页',
 'discover_applications': '查找已安装应用',
 'computer_use': '执行电脑操作',
 'desktop_view': '查看电脑画面',
 'desktop_observe': '读取界面控件',
 'desktop_act': '操作界面控件',
 'desktop_verify_state': '核对控件状态',
 'verify_desktop_goal': '确认操作结果',
 'open_application': '打开应用',
 'system_info': '查看系统信息',
 'inspect_system': '检查电脑状态',
 'inspect_disk_usage': '检查磁盘占用',
 'inspect_directory': '查看目录信息',
 'edit_file': '修改文件',
 'read_document': '读取文档',
 'create_document': '生成文档',
 'describe_artifact': '查看交付文件',
 'recall_context': '读取任务记录',
 'ask_user_question': '等待你的答复',
 'request_file_access': '等待文件授权',
 'web_search': '搜索网页',
 'inspect_image': '查看历史图片',
 'desktop_windows': '查找电脑窗口',
 'desktop_activate': '切换电脑窗口',
 'browser_navigate': '打开网页',
 'browser_observe': '读取网页结构',
 'browser_act': '操作网页控件',
 'browser_tabs': '查看网页标签',
 'browser_switch': '切换网页标签',
 'render_document': '渲染文档',
 'read': '读取文件',
 'write': '写入文件',
 'edit': '编辑文件',
 'glob': '查找文件',
 'grep': '搜索文件内容',
 'pwsh': '执行命令',
 'bash': '执行命令',
 'read_image': '查看图片',
 'working_directory': '查看工作目录',
 'present': '交付文件',
 'todo_write': '更新任务计划',
 'subagent': '分派任务',
 'subagent_fork': '分派任务',
 'send_message': '协调子任务',
 'list_agents': '查看子任务',
 'wait_agent': '等待子任务',
 'interrupt_agent': '停止子任务',
 'cua_run_code': '操作电脑',
 'cua': '操作电脑',
 'dsh_tool': '使用任务工具'}

COMPUTER_TOOLS = frozenset(['computer_use', 'desktop_act', 'open_application', 'desktop_activate', 'cua_run_code', 'cua'])
DSH_TOOL_NAMES = (
    "read", "write", "edit", "glob", "grep", "pwsh", "bash", "read_image", "working_directory",
    "present", "ask_user_question", "todo_write", "web_fetch", "web_search", "subagent",
    "subagent_fork", "send_message", "list_agents", "wait_agent", "interrupt_agent", "cua",
)

ACTION_LABELS = {
    "observe": "查看当前画面", "click": "点击界面", "left_click": "点击界面",
    "left_single": "点击界面", "left_double": "双击界面", "double_click": "双击界面",
    "right_click": "右键点击", "right_single": "右键点击", "middle_click": "中键点击",
    "drag": "拖动界面元素", "left_click_drag": "拖动界面元素", "select": "选择界面元素",
    "hover": "移动指针", "mouse_move": "移动指针", "type": "输入文字",
    "hotkey": "使用快捷键", "scroll": "滚动界面", "wait": "等待界面响应",
    "finished": "电脑操作返回结果", "call_user": "需要手动接手",
    "verify": "确认操作结果",
    "tool_request": "检查工具请求",
    "error_env": "检查操作环境", "user_stop": "停止电脑操作",
}

def public_activity(frame):
    """Construct the entire callback value; discard all untrusted extra fields."""
    labels = TOOL_LABELS if frame["kind"] == "tool" or frame.get("name") in TOOL_LABELS else ACTION_LABELS
    value = {"id": frame["event_id"], "kind": frame["kind"], "label": labels.get(frame["name"], TOOL_LABELS["dsh_tool"]),
             "status": frame["status"]}
    if "step" in frame:
        value["step"] = frame["step"]
    return value
