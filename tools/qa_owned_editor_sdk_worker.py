"""Private standalone SDK planner for the owned-editor QA broker only.

Exactly two tools exist in the real registry. No AgentLoop/default loaders,
sessions, memory, filesystem, GUI executor or user configuration are loaded.
Model text/reasoning never goes to stdout or a persistent store.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import sys
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
SDK_HELPERS=ROOT/"agent_backend/nanobot"
sys.path.insert(0,str(SDK_HELPERS))


def unique_members(pairs):
    value={}
    for key,item in pairs:
        if key in value: raise ValueError("EDITOR_SDK_PROTOCOL_INVALID")
        value[key]=item
    return value


def emit(value):
    sys.stdout.buffer.write(json.dumps(value,ensure_ascii=False,allow_nan=False).encode("utf-8")+b"\n")
    sys.stdout.buffer.flush()


def frame():
    raw=sys.stdin.buffer.readline(131073)
    if not raw.endswith(b"\n") or len(raw)>131072: raise ValueError("EDITOR_SDK_PROTOCOL_INVALID")
    value=json.loads(raw,object_pairs_hook=unique_members)
    if type(value) is not dict: raise ValueError("EDITOR_SDK_PROTOCOL_INVALID")
    return value


class ModelFailed(RuntimeError): pass


async def run(request):
    from loguru import logger
    logger.remove()
    from nanobot.agent.runner import AgentRunSpec
    from nanobot.agent.tools.registry import ToolRegistry
    from nanobot.agent.tools.base import Tool
    from nanobot.providers.openai_compat_provider import OpenAICompatProvider
    from nanobot.providers.model_api import ModelAPICapabilities
    from nanobot.providers.generation import GenerationSettings
    from nanobot.utils.llm_runtime import LLMRuntime
    from sdk_iterations import unlimited_iterations_runner

    token=request["token"];config=request["config"];calls=0
    class SingleAttemptProvider(OpenAICompatProvider):
        @staticmethod
        def default_output(args,kwargs): return kwargs.get("max_tokens",args[3] if len(args)>3 else None)==0
        def _build_kwargs(self,*args,**kwargs):
            value=super()._build_kwargs(*args,**kwargs)
            if self.default_output(args,kwargs):
                for name in ("max_tokens","max_completion_tokens","max_output_tokens"):value.pop(name,None)
            return value
        def _build_responses_body(self,*args,**kwargs):
            value=super()._build_responses_body(*args,**kwargs)
            if self.default_output(args,kwargs):value.pop("max_output_tokens",None)
            return value
        async def checked(self,method,messages,kwargs):
            emit({"type":"model_request","token":token})
            try:
                response=await method(messages,**kwargs)
                if response.finish_reason in {"error","length"} or not response.tool_calls and not response.content:
                    raise ModelFailed()
            except asyncio.CancelledError:raise
            except Exception:raise ModelFailed() from None
            emit({"type":"model_response","token":token})
            return response
        async def chat(self,messages,**kwargs):return await self.checked(super().chat,messages,kwargs)
        async def chat_stream(self,messages,**kwargs):return await self.checked(super().chat_stream,messages,kwargs)

    async def broker(name,arguments):
        nonlocal calls
        identity=uuid4().hex
        emit({"type":"tool","token":token,"request_id":identity,"name":name,"arguments":arguments})
        reply=await asyncio.to_thread(frame)
        if (set(reply)!={"type","token","request_id","value"} or reply["type"]!="tool_reply"
                or reply["token"]!=token or reply["request_id"]!=identity or type(reply["value"]) is not dict):
            raise ValueError("EDITOR_SDK_PROTOCOL_INVALID")
        calls+=1
        if request["fault"]=="eof_after_first_tool" and calls==1:os._exit(0)
        return json.dumps(reply["value"],ensure_ascii=False,allow_nan=False)

    class Observe(Tool):
        @property
        def name(self):return "owned_editor_observe"
        @property
        def description(self):return "Read the current controls of this single owned QA editor. Every refresh replaces previous refs."
        @property
        def read_only(self):return True
        @property
        def parameters(self):return {"type":"object","additionalProperties":False,"properties":{}}
        async def execute(self):return await broker(self.name,{})
    class Act(Tool):
        @property
        def name(self):return "owned_editor_act"
        @property
        def description(self):return "Act on one current owned-editor ref, then return a fresh observation. Invoke alone is not task success. Never automatically repeat an uncertain operation."
        @property
        def parameters(self):return {"type":"object","additionalProperties":False,"properties":{
            "ref":{"type":"string"},"action":{"type":"string","enum":["set_value","invoke"]},
            "value":{"type":"string","maxLength":1024}},"required":["ref","action"]}
        async def execute(self,ref,action,value=""):return await broker(self.name,{"ref":ref,"action":action,"value":value})

    registry=ToolRegistry();registry.register(Observe());registry.register(Act())
    provider=SingleAttemptProvider(api_key=config["api_key"],api_base=config["base_url"],default_model=config["model"],
        model_api=ModelAPICapabilities(),provider_name="custom")
    async def no_rewrite(*args,**kwargs):raise ModelFailed()
    policy=("You operate only an owned synthetic editor through the two registered tools. "
        "Observe the actual controls and choose actions from fresh refs. A tool's executed flag or a Saved label alone does not prove the goal. "
        "Every observation, including the one returned after an action, invalidates older refs. "
        "Do not invent access to files, the desktop, arbitrary applications, code, screenshots or clipboard. "
        "Stop after the requested document has actually been reopened and its current text read back. Report a limitation if the tools fail; never pretend completion.")
    task=("请在这个专用测试编辑器中输入下列 marker 的完整原文，通过界面的 Save document 按钮保存，"
        "再通过 Close document 关闭文档、Reopen document 重新打开，检查当前编辑框确实读回完整内容，完成后简短说明。"
        "这是测试窗口，不需要也不能操作其他应用。")
    spec=AgentRunSpec(initial_messages=[{"role":"system","content":policy},{"role":"user","content":json.dumps({
        "task":task,"marker":request["marker"]},ensure_ascii=False)}],tools=registry,
        runtime=LLMRuntime(provider,config["model"],GenerationSettings(max_tokens=0,temperature=.1),0),
        max_iterations=0,max_tool_result_chars=14000,concurrent_tools=False,consolidate_history=no_rewrite)
    try:
        result=await unlimited_iterations_runner().run(spec)
        if result.error or not isinstance(result.final_content,str) or not result.final_content.strip():raise ModelFailed()
        # No final text, transcript, hidden reasoning or provider error leaves
        # this process. Host observations and file bytes decide the goal.
        emit({"type":"done","token":token,"final_text_present":True})
    except asyncio.CancelledError:raise
    except Exception:emit({"type":"error","token":token,"code":"EDITOR_MODEL_FAILED"})
    finally:
        close=getattr(provider,"close",None)
        if close is not None:
            try:await close()
            except Exception:pass


def main():
    emit({"type":"ready","protocol":1,"role":"owned_editor_planner"})
    try:
        value=frame()
        if (set(value)!={"type","token","marker","config","state_dir","fault"} or value["type"]!="run"
                or type(value["token"]) is not str or not re.fullmatch("[a-f0-9]{32}",value["token"])
                or type(value["marker"]) is not str or not 1<=len(value["marker"])<=200
                or type(value["config"]) is not dict or set(value["config"])!={"base_url","model","api_key"}
                or any(type(item) is not str or not item for item in value["config"].values())
                or value["fault"] not in {"none","eof_after_first_tool"}):raise ValueError()
        state=Path(value["state_dir"])
        if not state.is_absolute() or not state.is_dir():raise ValueError()
        from nanobot.config.home import set_home_path
        set_home_path(state)
        os.chdir(state)
        asyncio.run(run(value))
    except (BrokenPipeError,KeyboardInterrupt):pass
    except Exception:emit({"type":"error","code":"EDITOR_SDK_INVALID"})


if __name__=="__main__":main()
