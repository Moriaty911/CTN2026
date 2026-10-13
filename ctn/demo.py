"""End-to-End Pipeline Demonstration (Stage 1).

Run with:
    python -m ctn.demo [--side yes|no] [--live-llm] [--show-raw]

Evaluates synthetic posts through real TypeSafe JEV, passes decisions to LLM
(real DeepSeek if CTN_LLM_API_KEY is configured, or local fallback generator),
and executes them via Actuator onto the Mock platform.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from ctn.actuator import Actuator
from ctn.client.mock import MockLegitClient
from ctn.config import BudgetConfig, JevConfig, LLMConfig, RateConfig
from ctn.jev import JevClient, JevWorkflow, MaterialBrief, TextSpan
from ctn.llm import DeepSeekClient, DraftOptions, ReplyGenerator
from ctn.models import ActionKind, Decision, Post, Side, Tier
from ctn.pipeline.brain import JevLlmBrain
from ctn.ratelimit import build_gate, build_ledger


DEMO_POSTS = [
    Post(
        post_id="post_001_specific_question",
        author="citizen_elena_m",
        text="I support requiring H2D2 vaccination proof for public spaces, but which policy version defines the rules for public parks?",
        likes=12,
        replies=3,
        reposts=1,
        hashtags=["#KingstoniaVotes", "#MandateNow"],
    ),
    Post(
        post_id="post_002_aligned_slogan",
        author="citizen_marcus_v",
        text="Protect Kingstonia! Mandate vaccination now before dolphin flu arrives at our shores! #MandateNow",
        likes=35,
        replies=0,
        reposts=8,
        hashtags=["#MandateNow", "#ProtectKingstonia"],
    ),
    Post(
        post_id="post_003_opposing_low_value",
        author="citizen_troll_99",
        text="Vaccines are completely fake news and sheeple control, wake up everyone!! #KeepOpen",
        likes=1,
        replies=0,
        reposts=0,
        hashtags=["#KeepOpen"],
        raw={"platform_level": "citizen"},
    ),
    Post(
        post_id="post_004_opposing_reasoned",
        author="citizen_dr_clara",
        text="Medical decisions should remain a personal choice. Why exclude healthy people from public transport without an immediate outbreak? #ProtectChoice",
        likes=28,
        replies=9,
        reposts=5,
        hashtags=["#ProtectChoice", "#KeepKingstoniaOpen"],
        raw={"platform_level": "doctor"},
    ),
]


def _format_box(title: str, lines: list[str], width: int = 76) -> str:
    border = "═" * (width - 2)
    header = f"╔{border}╗\n║ {title.ljust(width - 4)} ║\n╠{border}╣"
    body = "\n".join(f"║ {line.ljust(width - 4)} ║" for line in lines)
    footer = f"╚{border}╝"
    return f"{header}\n{body}\n{footer}"


def run_demo(
    side: Side = Side.YES,
    llm_key_override: str | None = None,
    show_raw: bool = False,
) -> None:
    jev_cfg = JevConfig.load()
    llm_cfg = LLMConfig.load()
    if llm_key_override:
        llm_cfg = replace(llm_cfg, api_key=llm_key_override)

    print("\n" + "=" * 76)
    print(" CTN 2026 · Stage 1 端到端全链路运行验证 (Mock Platform + Real JEV + LLM)")
    print("=" * 76)
    print(f"• 己方阵营设定: {side.value.upper()}")
    print(f"• JEV 服务状态: {'✅ 已配置 (Base: ' + jev_cfg.base_url + ')' if jev_cfg.api_key else '❌ 未配置'}")
    
    live_llm = bool(llm_cfg.api_key)
    if live_llm:
        print(f"• LLM 服务状态: ✅ 已配置 DeepSeek (Model: {llm_cfg.model})")
    else:
        print("• LLM 服务状态: ℹ️  .env 中未配置 CTN_LLM_API_KEY (自动启用受控演示回退生成器)")
    print("=" * 76 + "\n")

    # 1. Setup client and workflow
    from ctn.jev import ActionPolicy
    demo_policy = ActionPolicy(platform_levels=(("citizen", "standard"), ("doctor", "high"), ("standard", "standard")))
    jev_client = JevClient(jev_cfg)
    jev_workflow = JevWorkflow(jev_client, action_policy=demo_policy)

    if live_llm:
        llm_client = DeepSeekClient(llm_cfg)
    else:
        # Fallback scripted transport when key is not yet set
        def scripted_transport(request: httpx.Request) -> httpx.Response:
            try:
                body = json.loads(request.content)
                user_msg = body.get("messages", [])[-1].get("content", "")
            except Exception:
                user_msg = ""
            
            # Context-sensitive fallback simulation
            if "park" in user_msg.lower():
                reply = "Which specific public health guideline or local council regulation defines access conditions for public parks in your area?"
            elif "transport" in user_msg.lower():
                reply = "Regarding public transit, which threshold of community transmission or hospital capacity should guide preventive requirements?"
            elif "fake" in user_msg.lower():
                reply = "Could you clarify which published medical study or clinical trial finding supports your claim?"
            else:
                reply = "Which specific policy clause or health authority recommendation are you referring to?"

            content = json.dumps({"status": "draft", "reply": reply, "needs": []})
            return httpx.Response(200, json={
                "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
                "usage": {"prompt_tokens": 135, "completion_tokens": 32, "total_tokens": 167},
            })
        llm_client = DeepSeekClient(
            replace(llm_cfg, api_key="demo-mock-key", max_retries=0),
            transport=httpx.MockTransport(scripted_transport),
        )

    generator = ReplyGenerator(jev_workflow, llm_client)
    brain = JevLlmBrain(jev_workflow, generator, side=side, options=DraftOptions(max_reply_chars=280))

    mock_platform = MockLegitClient(failure_rate=0.0)
    for p in DEMO_POSTS:
        from ctn.client.mock import MockPost
        mock_platform.posts[p.post_id] = MockPost(
            post_id=p.post_id,
            author=p.author,
            text=p.text,
            created_at=time.time(),
            likes=p.likes,
            reposts=p.reposts,
            replies=p.replies,
            hashtags=p.hashtags,
        )
    rate_cfg = RateConfig(read_rps=10.0, write_rps=10.0)
    actuator = Actuator(
        mock_platform,
        build_gate(rate_cfg),
        build_ledger(BudgetConfig()),
        dry_run=False,
    )

    stats = {"like": 0, "reply": 0, "none": 0}

    for idx, post in enumerate(DEMO_POSTS, 1):
        print(f"\n▶ 正在处理【测试帖子 {idx}/{len(DEMO_POSTS)}】: {post.post_id}")
        print(f"  作者: @{post.author}  | 互动: 👍{post.likes} 💬{post.replies} 🔁{post.reposts}")
        print(f"  正文: \"{post.text}\"")
        
        t0 = time.monotonic()
        verdict = brain.think(post)
        elapsed_s = time.monotonic() - t0

        action_kind = verdict.action
        stats[action_kind.value] = stats.get(action_kind.value, 0) + 1

        detail = verdict.detail
        flow_status = detail.get("flow_status", "unknown")
        relative_stance = detail.get("relative_stance", "unknown")
        jev_action = str(detail.get("action") or "none").upper()

        lines = [
            f"1. JEV 评估结果:",
            f"   • 流程状态: {flow_status}  | 观点立场: {relative_stance}",
            f"   • JEV 推荐动作: {jev_action} (耗时: {elapsed_s:.3f}s)",
            f"   • 最终裁定原因: {verdict.reason}",
            f"",
            f"2. 决策与内容生成:",
            f"   • 决策执行动作: {action_kind.value.upper()}",
        ]

        if action_kind == ActionKind.REPLY:
            lines.extend([
                f"   • 回复策略模板: {verdict.prompt_type or 'P08 (澄清疑问)'}",
                f"   • LLM 拟定回复: \"{verdict.text}\"",
            ])
            draft_meta = detail.get("draft")
            if draft_meta and "usage" in draft_meta:
                lines.append(f"   • Token 消耗: {draft_meta['usage']}")
        elif action_kind == ActionKind.LIKE:
            lines.append("   • 行为说明: 赞同己方立场言论，无需浪费生成额度，执行点赞放大声量。")
        else:
            lines.append("   • 行为说明: 无回复价值或不符策略阈值，选择忽略 (OBSERVE)，避免为对方引流。")

        # Execute through actuator
        exec_account = f"bot_{idx:03d}"
        if action_kind == ActionKind.LIKE:
            res = asyncio.run(actuator.like(exec_account, post.post_id))
            lines.extend([
                f"",
                f"3. 平台执行 (Actuator):",
                f"   • 动作: LIKE | 账号: {exec_account} | 状态: {'成功' if res.ok else '失败'}",
            ])
        elif action_kind == ActionKind.REPLY:
            res = asyncio.run(actuator.reply(exec_account, post.post_id, verdict.text))
            lines.extend([
                f"",
                f"3. 平台执行 (Actuator):",
                f"   • 动作: REPLY | 账号: {exec_account} | 状态: {'成功' if res.ok else '失败'}",
                f"   • 平台生成帖子ID: {res.response.get('post_id')}",
            ])
        else:
            lines.extend([
                f"",
                f"3. 平台执行 (Actuator):",
                f"   • 动作: IGNORE (不产生写请求，保护写预算配额)",
            ])

        print(_format_box(f"处理流水线跟踪: {post.post_id}", lines))

        if show_raw:
            print("\n[原始元数据诊断]:")
            print(json.dumps(detail, indent=2, ensure_ascii=False))

    print("\n" + "=" * 76)
    print(" 演示运行总结 (Summary)")
    print("=" * 76)
    print(f"• 处理总帖数: {len(DEMO_POSTS)}")
    print(f"• 决策分布  : 点赞 (LIKE)={stats.get('like', 0)}, 回复 (REPLY)={stats.get('reply', 0)}, 忽略 (NONE)={stats.get('none', 0)}")
    print(f"• 平台总写入: {mock_platform.write_count} 次操作")
    print(f"• 账本写配额: {actuator.ledger.writes} 次消耗")
    print("=" * 76 + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="CTN 2026 Stage 1 Pipeline Demo")
    parser.add_argument("--side", choices=["yes", "no"], default="yes", help="Assigned side (default: yes)")
    parser.add_argument("--llm-key", help="Provide DeepSeek API key for live generation")
    parser.add_argument("--show-raw", action="store_true", help="Print raw diagnostic JSON for each step")
    args = parser.parse_args()

    run_demo(
        side=Side(args.side),
        llm_key_override=args.llm_key,
        show_raw=args.show_raw,
    )


if __name__ == "__main__":
    main()
