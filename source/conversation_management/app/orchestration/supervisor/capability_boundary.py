from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

# Follow-up requests that refer to an already returned answer/result are not, by
# themselves, requests to reacquire business data. This module intentionally stays
# domain-agnostic so the same rule applies to health, alarm, trend and other results.
_CONTEXT_REFERENCE_RE = re.compile(
    r"(?:这个|这份|这些|该|上述|上面|刚才|前面|上一轮|刚刚)(?:的)?"
    r"(?:数据|结果|查询结果|回答|回复|内容|结论|信息|健康度数据|健康度结果|"
    r"健康评分|报警数据|报警结果|趋势数据|分析结果)"
)
_CONTEXT_ANALYSIS_RE = re.compile(
    r"(?:分析(?:一下|下)?|解读(?:一下|下)?|解释(?:一下|下)?|说明(?:一下|下)?|"
    r"总结(?:一下|下)?|评价(?:一下|下)?|怎么看|怎么理解|意味着什么|有什么特点|"
    r"有什么问题|给出看法|谈谈|帮我看看)"
)

# An explicit request for new external facts/calculation overrides context-only reuse.
# "当前" alone is deliberately not included because users often say "分析当前结果".
_NEW_EVIDENCE_RE = re.compile(
    r"(?:查询|查一下|查下|检索|获取|读取|拉取|刷新|更新|重新查询|重新查|再查询|再查|"
    r"最新(?:数据|记录|结果|情况)|实时(?:数据|记录|情况)|过去\s*\d+\s*天|最近\s*\d+\s*天|"
    r"昨天|前天|上周|上个月|历史(?:数据|记录|趋势))"
)

# These are explicit professional-computation requests. They must not be downgraded to
# generic explanation merely because the sentence also references previous context.
_PROFESSIONAL_ACTION_RE = re.compile(
    r"(?:诊断一下|诊断下|帮我诊断|重新诊断|综合诊断|完整诊断|故障诊断|"
    r"判断故障|判断一下故障|是什么故障|什么故障|故障原因|原因分析|"
    r"重新分析波形|重新分析频谱|重新做诊断)"
)

# Health-state follow-ups often refer to the *device* rather than saying “这个结果”.
# Example: after “健康等级：重点关注”, the user asks
# “分析一下这个设备需要重点关注的原因”. This is contextual interpretation of
# an already returned health state unless the user explicitly asks for new evidence or
# a professional diagnosis.
_HEALTH_RESULT_EVIDENCE_RE = re.compile(
    r"(?:总健康分|健康(?:度|分|评分)\s*(?:为|是|：|:)?\s*\d|健康等级\s*(?:为|是|：|:)|"
    r"阈值(?:模型)?(?:得分|分)\s*(?:为|是|：|:)?\s*\d|"
    r"趋势(?:模型)?(?:得分|分)\s*(?:为|是|：|:)?\s*\d|"
    r"AI(?:模型)?(?:得分|分)\s*(?:为|是|：|:)?\s*\d|"
    r"机理(?:模型)?(?:得分|分)\s*(?:为|是|：|:)?\s*\d|"
    r"健康等级.{0,8}(?:重点关注|早期关注|良好|优秀))",
    re.IGNORECASE,
)
_HEALTH_STATUS_FOLLOWUP_RE = re.compile(
    r"(?:(?:这个|该|这台|当前)?(?:设备|机组|机器)|它)?.{0,12}"
    r"(?:重点关注|早期关注|健康等级|健康状态).{0,12}"
    r"(?:为什么|为何|原因|怎么回事|分析|解释|说明|怎么看)"
    r"|(?:为什么|为何|原因|怎么回事|分析|解释|说明|怎么看).{0,12}"
    r"(?:重点关注|早期关注|健康等级|健康状态)"
)
_HEALTH_CHANGE_OR_NEW_DIAGNOSIS_RE = re.compile(
    r"(?:健康度|健康分|健康评分).{0,8}(?:下降|降低|变差|恶化)|"
    r"(?:下降|降低|变差|恶化).{0,8}(?:健康度|健康分|健康评分)|"
    r"(?:故障|报警|波形|频谱|振动异常|重新诊断|专业诊断)"
)

# Score-composition questions are explanations of a result already shown to the user,
# even when natural language omits a demonstrative such as “这个/刚才”.  They must not
# be routed back to entity resolution merely because the word “健康度” occurs again.
_HEALTH_SCORE_FOLLOWUP_RE = re.compile(
    r"(?:健康度|健康分|健康评分|总健康分|健康总分|阈值(?:模型)?(?:得分|分)?|"
    r"趋势(?:模型)?(?:得分|分)?|AI(?:模型)?(?:得分|分)?|"
    r"机理(?:模型)?(?:得分|分)?).{0,18}"
    r"(?:为什么|为何|原因|不是满分|没有满分|没满分|未满分|不满分|扣分|"
    r"怎么算|如何计算|怎么得出|如何得出|解释|说明|怎么看|意味着什么)|"
    r"(?:为什么|为何|原因|扣分).{0,18}"
    r"(?:健康度|健康分|健康评分|总健康分|健康总分|阈值(?:模型)?(?:得分|分)?|"
    r"趋势(?:模型)?(?:得分|分)?|AI(?:模型)?(?:得分|分)?|"
    r"机理(?:模型)?(?:得分|分)?)|"
    r"(?:为什么|为何)(?:健康度)?(?:不是|没有|没到|未到)?满分|"
    r"(?:哪个|哪项|哪些|哪里).{0,10}(?:维度|模型|指标|分项|项)?.{0,8}(?:扣分|没满)|"
    r"(?:主要)?扣分项|总分.{0,8}(?:怎么算|如何计算|怎么得出|如何得出)"
)

# A concrete code or numbered equipment in the current turn means the user supplied a
# fresh target.  Even if recent context contains another health result, do not leak that
# result into the newly named object.
_EXPLICIT_ENTITY_CODE_RE = re.compile(
    r"(?<![A-Za-z0-9])(?=[A-Za-z0-9_-]{6,}(?![A-Za-z0-9]))"
    r"(?=[A-Za-z0-9_-]*[A-Za-z])(?=[A-Za-z0-9_-]*\d)"
    r"[A-Za-z][A-Za-z0-9_-]{5,}(?![A-Za-z0-9])",
    re.IGNORECASE,
)
_EXPLICIT_NUMBERED_EQUIPMENT_RE = re.compile(
    r"(?:\d{1,3}|[零〇一二两三四五六七八九十百]{1,5})\s*"
    r"(?:(?:号|#|＃)\s*)?(?:架|台)?\s*"
    r"(?:精轧机|轧机|飞剪|鼓风机|引风机|风机|水泵|泵机|电机|减速机|压缩机|磨机|机组|设备)"
)

_ENTITY_REFERENTIAL_FOLLOWUP_RE = re.compile(
    r"(?:这个设备|该设备|这台设备|本设备|这个机组|该机组|"
    r"这台机器|该机器|这个测点|该测点)"
)


def is_referential_entity_followup(query: str) -> bool:
    """Whether the query explicitly refers back to the currently active entity."""

    text = re.sub(r"\s+", "", str(query or ""))
    return bool(text and _ENTITY_REFERENTIAL_FOLLOWUP_RE.search(text))


def has_recent_assistant_context(recent_messages: Sequence[Mapping[str, Any]] | None) -> bool:
    for item in reversed(list(recent_messages or [])):
        role = str(item.get("role") or "").strip().lower()
        content = str(item.get("content") or "").strip()
        if role == "assistant" and content:
            return True
    return False


def has_recent_health_result_context(
    recent_messages: Sequence[Mapping[str, Any]] | None,
) -> bool:
    """Return whether an assistant message contains an actual health result.

    A mere mention of “健康度” is intentionally insufficient: a previous user
    question or an assistant error is not evidence that can support score analysis.
    """

    for item in reversed(list(recent_messages or [])):
        if str(item.get("role") or "").strip().lower() != "assistant":
            continue
        content = re.sub(r"\s+", "", str(item.get("content") or ""))
        if content and _HEALTH_RESULT_EVIDENCE_RE.search(content):
            return True
    return False


def is_health_result_context_followup(
    query: str,
    recent_messages: Sequence[Mapping[str, Any]] | None,
) -> bool:
    """Whether this turn only explains an already returned health result.

    The decision uses both the current question and verifiable assistant context. It
    deliberately rejects explicit refresh/diagnosis requests, health deterioration,
    and a newly named equipment identity because those require new business evidence.
    """

    text = re.sub(r"\s+", "", str(query or ""))
    if not text or not has_recent_health_result_context(recent_messages):
        return False
    if _PROFESSIONAL_ACTION_RE.search(text) or _NEW_EVIDENCE_RE.search(text):
        return False
    if _HEALTH_CHANGE_OR_NEW_DIAGNOSIS_RE.search(text):
        return False
    if _EXPLICIT_ENTITY_CODE_RE.search(text) or _EXPLICIT_NUMBERED_EQUIPMENT_RE.search(text):
        return False
    return bool(
        _HEALTH_SCORE_FOLLOWUP_RE.search(text)
        or _HEALTH_STATUS_FOLLOWUP_RE.search(text)
        or (_CONTEXT_REFERENCE_RE.search(text) and _CONTEXT_ANALYSIS_RE.search(text))
    )


def looks_like_health_status_explanation(query: str) -> bool:
    """Query-only guard for health-state label explanations.

    It is intentionally narrow so “分析一下这个设备” remains a professional
    diagnosis request, while “分析一下这个设备需要重点关注的原因” does not.
    """

    text = re.sub(r"\s+", "", str(query or ""))
    if not text:
        return False
    if _PROFESSIONAL_ACTION_RE.search(text) or _NEW_EVIDENCE_RE.search(text):
        return False
    if _HEALTH_CHANGE_OR_NEW_DIAGNOSIS_RE.search(text):
        return False
    return bool(_HEALTH_STATUS_FOLLOWUP_RE.search(text))


def is_health_status_context_followup(
    query: str,
    recent_messages: Sequence[Mapping[str, Any]] | None,
) -> bool:
    """Whether the user is asking to explain a health-state label already returned.

    This deliberately does *not* treat health deterioration, alarm/fault causes, or
    explicit diagnosis requests as context-only. Those require fresh professional
    evidence.
    """

    if not has_recent_health_result_context(recent_messages):
        return False
    return looks_like_health_status_explanation(query)


def is_context_only_analysis_request(
    query: str,
    recent_messages: Sequence[Mapping[str, Any]] | None,
) -> bool:
    """Return True when the user asks to interpret already available conversation data.

    This is a capability-boundary guard, not an intent classifier. It prevents the
    supervisor from inventing a business-tool workflow when the previous assistant
    answer already contains the material the user wants explained.
    """

    text = re.sub(r"\s+", "", str(query or ""))
    if not text or not has_recent_assistant_context(recent_messages):
        return False
    if _PROFESSIONAL_ACTION_RE.search(text):
        return False
    if _NEW_EVIDENCE_RE.search(text):
        return False
    if is_health_result_context_followup(query, recent_messages):
        return True
    return bool(_CONTEXT_REFERENCE_RE.search(text) and _CONTEXT_ANALYSIS_RE.search(text))


def general_context_objective(query: str) -> str:
    return (
        "仅基于当前对话中已经取得的结果/回答进行解释、总结或分析；"
        "不要重新查询业务系统，不要声称执行了新的专业诊断。"
        f" 用户当前问题：{str(query or '').strip()}"
    )
