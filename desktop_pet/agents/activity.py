"""Public execution labels, independent of model reasoning and tool arguments."""
from agent_backend.nanobot.tool_catalog import TOOL_LABELS
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
