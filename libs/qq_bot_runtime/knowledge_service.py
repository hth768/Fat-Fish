# -*- coding: utf-8 -*-
"""知识库服务：所有平台/功能插件统一的知识库访问入口（平台无关）。

聊天层面：chat_service 自动走这一层——任何平台插件（QQ / 控制台 / 未来的 B站...）
经 handle_message() 都自动享有「盲区 → 搜索 → 沉淀 → 复用」闭环，平台零改动。

插件编程访问：插件在 __init__ 拿到 core 引用，直接用 self.core.knowledge：

    kb = self.core.knowledge
    kb.learn_async("问题", "一段文本")          # 从文本提炼知识入库（后台，不阻塞）
    ctx = kb.recall("问题")                     # 查库，返回可注入 AI 的上下文（无命中为空串）
    kb.record("主题", ["事实1"], ["关键词"])      # 已有结构化内容时直接记录（不过 LLM）
    kb.search("问题")                           # 查库，返回原始条目自己加工
    kb.view() / kb.remove("关键词")              # 查看 / 删除

知识全局共享：QQ 聊天里学会的知识，B 站插件同样能查到（knowledge_store 按主题存，
不分平台/用户）。LLM 提炼用 DeepSeek 日常模型，存储层在 knowledge_store.py。
"""
import asyncio
from typing import Dict, List, Optional

import config
import knowledge_store
from ai_provider import get_llm


class KnowledgeService:
    """知识库服务门面：提炼（LLM 学习）、召回（复用）、记录、管理。"""

    def __init__(self):
        # 后台学习任务集合（持有引用防止任务被垃圾回收）
        self._tasks = set()

    # ---------------- 学习（写） ----------------
    async def learn(self, query: str, text: str) -> Optional[Dict]:
        """从一段文本提炼通用知识入库（LLM 调用，可 await）。

        query：这段文本回答的是什么问题（用于定主题/关键词）；
        text：文本本身，可以是搜索结果、网页正文、视频字幕、日志等。
        没有值得长期记住的通用知识时返回 None。
        """
        prompt = (
            "下面是一次联网搜索的「问题」和「搜索结果」。请判断结果里是否包含值得长期记住的通用知识"
            "（技术特性、原理、事实、数据、方法等，以后再遇到相关问题时还会用到）。\n"
            "【不要记录】用户个人的临时问题、纯闲聊、天气/股价/比分/新闻热点这类马上过时的即时信息、"
            "与问题无关的网页噪声。\n"
            "如果有，输出严格 JSON（不要 markdown 代码块）:\n"
            '{"topic": "简短主题名", "keywords": ["关键词1", "关键词2"], "facts": ["事实1", "事实2"], "sources": ["https://..."]}\n'
            "keywords 给 3-8 个用户以后可能提问的具体说法（中英文都可以，必须具体，"
            "不要「数据库」「版本」这种宽泛词）；facts 提炼 1-5 条具体、自包含的事实；"
            "sources 从文本里挑可靠的原文链接，没有就给空数组。\n"
            "如果没有值得记录的通用知识，只输出一个字：无\n\n"
            f"[问题] {query}\n\n[搜索结果]\n{text[:6000]}"
        )
        client = get_llm()
        try:
            output = await client.chat([{"role": "user", "content": prompt}], capability="chat")
        except Exception as e:
            print(f"[KB] 知识提炼调用失败: {e}")
            return None
        try:
            entry = knowledge_store.parse_distill_output(output)
            if not entry:
                return None
            r = knowledge_store.add_entry(entry["topic"], entry["facts"],
                                          entry["keywords"], entry["sources"])
            if r.get("ok") and r.get("new_facts"):
                print(f"[KB] 已沉淀知识「{entry['topic']}」（新增 {r['new_facts']} 条事实，共 {r['total']} 个主题）")
            return r
        except Exception as e:
            print(f"[KB] 知识沉淀失败: {e}")
            return None

    def learn_async(self, query: str, text: str):
        """把学习丢到后台执行（需在事件循环内调用），不阻塞当前流程。"""
        if not getattr(config, "ENABLE_KNOWLEDGE_LEARN", False):
            return
        try:
            task = asyncio.get_running_loop().create_task(self.learn(query, text))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        except Exception as e:
            print(f"[KB] 知识学习任务启动失败: {e}")

    def record(self, topic: str, facts: List[str],
               keywords: List[str] = None, sources: List[str] = None) -> Dict:
        """直接记录结构化知识（插件已知要记什么时用，不过 LLM）。

        同主题自动合并、事实去重、总量超限淘汰最旧，详见 knowledge_store.add_entry。
        """
        return knowledge_store.add_entry(topic, facts, keywords, sources)

    # ---------------- 复用（读） ----------------
    def recall(self, query: str, limit: int = 2) -> str:
        """查知识库，返回可直接注入给 AI 的上下文；无命中返回空串。

        注入文本自带学习日期与「可能过时」提醒。ENABLE_KNOWLEDGE_RECALL
        关闭时恒返回空串（聊天流程会照常走联网判断）。
        """
        if not getattr(config, "ENABLE_KNOWLEDGE_RECALL", False):
            return ""
        return knowledge_store.build_recall_context(query, limit)

    def search(self, query: str, limit: int = 2) -> List[Dict]:
        """查知识库，返回命中的原始条目（插件自己加工用）。"""
        return knowledge_store.search_entries(query, limit)

    # ---------------- 管理 ----------------
    def view(self, keyword: str = "") -> str:
        """知识库概览文本（keyword 可选过滤）。空库返回空串。"""
        return knowledge_store.list_for_view(keyword)

    def remove(self, keyword: str) -> Dict:
        """按关键词删除知识条目。"""
        return knowledge_store.delete_by_keyword(keyword)


# 全局单例：所有插件/模块共享同一个知识库服务
_service: Optional[KnowledgeService] = None


def get_knowledge() -> KnowledgeService:
    global _service
    if _service is None:
        _service = KnowledgeService()
    return _service
