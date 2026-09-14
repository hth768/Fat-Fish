# -*- coding: utf-8 -*-
"""屏幕感知模块：定期截屏理解用户当前活动，供主动交互使用。

参考 N.E.K.O. 猫娘计划的环境感知设计：
- 定期截屏（可配置间隔）
- 视觉模型理解屏幕内容
- 提取用户活动状态（正在用什么软件、做什么）
- 供主动说话器参考，生成更贴合场景的话题

与 pc_agent 的区别：
- pc_agent 是任务驱动的操控智能体（用户下发任务）
- screen_awareness 是被动的感知层（定期观察，不操控）
"""
import asyncio
import time
from typing import Dict, Optional, List
from dataclasses import dataclass, field

import config


@dataclass
class ScreenState:
    """屏幕状态快照"""
    timestamp: float = 0.0
    activity: str = ""           # 用户活动描述（如"正在写代码"、"在看视频"）
    app_name: str = ""           # 当前活跃应用名称
    details: str = ""            # 更多细节（如"在 VS Code 里编辑 Python 文件"）
    mood_hint: str = ""          # 情绪线索（如"看起来很专注"、"在放松"）
    raw_description: str = ""    # 视觉模型的原始描述
    
    def is_fresh(self, max_age_seconds: float = 300) -> bool:
        """状态是否新鲜（默认 5 分钟内）"""
        return (time.time() - self.timestamp) < max_age_seconds
    
    def to_hint(self) -> str:
        """转为可注入的提示文本"""
        if not self.activity:
            return ""
        lines = [f"【屏幕感知】用户当前状态：{self.activity}"]
        if self.app_name:
            lines.append(f"活跃应用：{self.app_name}")
        if self.details:
            lines.append(f"细节：{self.details}")
        if self.mood_hint:
            lines.append(f"状态线索：{self.mood_hint}")
        return "\n".join(lines)


class ScreenAwareness:
    """屏幕感知服务"""
    
    def __init__(self):
        self._enabled = getattr(config, "ENABLE_SCREEN_AWARENESS", False)
        self._interval = getattr(config, "SCREEN_AWARENESS_INTERVAL", 300)  # 默认 5 分钟
        self._last_capture_time = 0.0
        self._current_state = ScreenState()
        self._history: List[ScreenState] = []
        self._max_history = 20
        self._capturing = False
        self._task: Optional[asyncio.Task] = None
        
    @property
    def enabled(self) -> bool:
        return self._enabled
    
    @property
    def current_state(self) -> ScreenState:
        return self._current_state
    
    def get_state(self) -> ScreenState:
        """获取当前屏幕状态"""
        return self._current_state
    
    def get_state_hint(self) -> str:
        """获取可注入的状态提示（如果状态新鲜）"""
        if self._current_state.is_fresh():
            return self._current_state.to_hint()
        return ""
    
    def get_recent_activities(self, minutes: int = 30) -> List[ScreenState]:
        """获取最近 N 分钟的活动历史"""
        cutoff = time.time() - minutes * 60
        return [s for s in self._history if s.timestamp >= cutoff]
    
    async def capture_once(self) -> ScreenState:
        """执行一次屏幕捕获和分析"""
        if not self._enabled:
            return ScreenState()
        
        try:
            # 截屏
            from pc_control import capture_screen
            jpeg_bytes, (w, h) = await asyncio.to_thread(capture_screen)
            
            if not jpeg_bytes:
                print("[SCREEN] 截屏失败")
                return ScreenState()
            
            # 视觉分析
            from glm_client import GLMClient
            vision = GLMClient()
            
            prompt = """分析这张屏幕截图，提取以下信息（用 JSON 格式返回）：
{
    "activity": "用户正在做什么（一句话概括，如'在写代码'、'看视频'、'浏览网页'）",
    "app_name": "当前活跃的应用程序名称",
    "details": "更多细节描述（20字以内）",
    "mood_hint": "从屏幕内容推测的用户状态/情绪线索（如'专注工作'、'休闲放松'、'学习研究'）"
}

只返回 JSON，不要其他文字。"""
            
            response = await vision.describe_image(jpeg_bytes, prompt)
            
            # 解析响应
            import json
            response = response.strip()
            if response.startswith("```"):
                response = response.split("```")[1]
                if response.startswith("json"):
                    response = response[4:]
            
            data = json.loads(response)
            
            # 更新状态
            new_state = ScreenState(
                timestamp=time.time(),
                activity=data.get("activity", ""),
                app_name=data.get("app_name", ""),
                details=data.get("details", ""),
                mood_hint=data.get("mood_hint", ""),
                raw_description=response
            )
            
            self._current_state = new_state
            self._history.append(new_state)
            if len(self._history) > self._max_history:
                self._history = self._history[-self._max_history:]
            
            print(f"[SCREEN] 屏幕感知更新：{new_state.activity}")
            return new_state
            
        except Exception as e:
            print(f"[SCREEN] 屏幕感知失败: {e}")
            return ScreenState()
    
    async def start_periodic_capture(self):
        """启动定期截屏任务"""
        if not self._enabled:
            print("[SCREEN] 屏幕感知未启用")
            return
        
        if self._task is not None:
            print("[SCREEN] 已在运行")
            return
        
        async def _loop():
            while self._enabled:
                try:
                    await self.capture_once()
                except Exception as e:
                    print(f"[SCREEN] 周期捕获异常: {e}")
                await asyncio.sleep(self._interval)
        
        self._task = asyncio.create_task(_loop())
        print(f"[SCREEN] 屏幕感知已启动，间隔 {self._interval} 秒")
    
    def stop(self):
        """停止定期截屏"""
        if self._task:
            self._task.cancel()
            self._task = None
            print("[SCREEN] 屏幕感知已停止")


# 全局单例
_awareness_instance: Optional[ScreenAwareness] = None


def get_screen_awareness() -> ScreenAwareness:
    global _awareness_instance
    if _awareness_instance is None:
        _awareness_instance = ScreenAwareness()
    return _awareness_instance
