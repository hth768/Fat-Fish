# -*- coding: utf-8 -*-
"""异步任务调度器：后台定时任务，和 QQ 聊天主循环并行运行。

这是"多功能智能体"架构的骨架：让肥鱼娘能同时做多项活动。
当前实现：DeepSeek 余额监控。
"""
import asyncio
import json

import httpx

import config


async def query_deepseek_balance() -> dict:
    """查询 DeepSeek 账户余额。返回 {"is_available": bool, "total": float, "granted": float, "topped_up": float, "currency": str}"""
    url = "https://api.deepseek.com/user/balance"
    headers = {"Authorization": f"Bearer {config.DEEPSEEK_API_KEY}"}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, headers=headers)
        if resp.status_code != 200:
            raise RuntimeError(f"查询余额失败 {resp.status_code}: {resp.text[:200]}")
        data = resp.json()

    result = {"is_available": data.get("is_available", False)}
    # 余额按币种分别返回，通常 CNY
    infos = data.get("balance_infos", [])
    if infos:
        info = infos[0]  # 取第一个币种（通常 CNY）
        result["currency"] = info.get("currency", "CNY")
        result["total"] = float(info.get("total_balance", 0) or 0)
        result["granted"] = float(info.get("granted_balance", 0) or 0)
        result["topped_up"] = float(info.get("topped_up_balance", 0) or 0)
    return result


async def send_alert_message(user_id, message: str):
    """主动发送私聊消息（用于余额提醒）。通过统一 MessageSender 发送。"""
    from message_bus import get_sender
    sender = get_sender()
    if sender is None:
        print(f"[WARN] 没有可用的平台 sender，提醒无法发送: {message[:50]}")
        return
    await sender.send_private(user_id, message)


async def balance_monitor_loop():
    """余额监控主循环：定时查余额，低于阈值提醒。"""
    print(f"[INFO] 余额监控已启动，每 {config.BALANCE_CHECK_INTERVAL} 秒查一次")
    alerted = False  # 避免重复提醒
    while True:
        try:
            result = await query_deepseek_balance()
            total = result.get("total", 0)
            currency = result.get("currency", "CNY")
            granted = result.get("granted", 0)
            topped_up = result.get("topped_up", 0)

            print(f"[INFO] DeepSeek 余额: {total} {currency}（充值 {topped_up} + 赠金 {granted}）")

            # 低余额提醒
            if total < config.BALANCE_ALERT_THRESHOLD and not alerted:
                if config.BALANCE_ALERT_USER_ID:
                    msg = (
                        f"⚠️ 主人，我的 DeepSeek 账户余额不足啦！\n"
                        f"当前余额：{total} {currency}\n"
                        f"（充值 {topped_up} + 赠金 {granted}）\n"
                        f"低于阈值 {config.BALANCE_ALERT_THRESHOLD} {currency}，请及时充值，不然我要饿死啦！"
                    )
                    try:
                        await send_alert_message(config.BALANCE_ALERT_USER_ID, msg)
                        alerted = True  # 提醒过了，不再重复
                    except Exception as e:
                        print(f"[WARN] 余额提醒发送失败: {e}")
            elif total >= config.BALANCE_ALERT_THRESHOLD:
                alerted = False  # 余额恢复，重置提醒状态
        except Exception as e:
            print(f"[WARN] 余额查询失败: {e}")

        # 等待下一次检查
        await asyncio.sleep(config.BALANCE_CHECK_INTERVAL)
