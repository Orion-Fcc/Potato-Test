"""LLM clients (OpenAI-compatible gateway).

Base URL / API key / models resolve from admin-managed AppSetting overrides
(System settings → LLM model) with the env values as fallback, so the platform's
underlying model can be changed at runtime without a redeploy. Overrides are
cached per process for a short TTL; workers pick up changes on their next case.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx
from openai import AsyncOpenAI

from app.config import get_settings

# AppSetting keys (written by PUT /admin/settings/llm)
LLM_BASE_URL_KEY = "llm_base_url"
LLM_API_KEY_KEY = "llm_api_key"  # stored encrypted
LLM_MODEL_KEY = "llm_model"
LLM_AGENT_MODEL_KEY = "llm_agent_model"
LLM_PROVIDER_SETTING_KEY = "llm_provider"
_KEYS = (LLM_BASE_URL_KEY, LLM_API_KEY_KEY, LLM_MODEL_KEY, LLM_AGENT_MODEL_KEY, LLM_PROVIDER_SETTING_KEY)

# 按用途分模型（2026-10-11）。真库实测：近 46 条结果共 444 万 token / 271 次调用，
# 其中 **agent 每步调用**吃掉约 95% —— 判定器每条用例只调用一次、输入约 3~4k token，
# 占总量的个位数百分比。所以"全平台用一个最贵的模型"是把钱花在不需要的地方：
# 该省的（量大的执行步）和该保的（质量敏感的判定/生成）必须能分开配。
LLM_ROLES_KEY = "llm_roles"  # encrypted blob: {role: {model, base_url, api_key, provider}}
# 两级判定开关（"1" = 开）。默认关：它会在失败/证据可疑时多花一次强模型调用，
# 得由人显式打开并选好复核模型 —— 悄悄打开等于悄悄加钱。
LLM_JUDGE_TWO_TIER_KEY = "llm_judge_two_tier"
# 平台级「高端模型」（2026-10-11）。encrypted blob：
# {enabled, model, base_url, api_key, provider, roles: [...]}
#
# 为什么要在"按用途分模型"之外再加一层：那张卡要求逐行手填六个用途的网关/模型/钥匙，
# 而用户的真实意图通常只是一句话 ——"要不要为质量多花点钱"。所以把这件事收敛成一个
# 显式选择：**开或不开一支高端模型，开了再勾它管哪几个环节**。不开就全部回落到便宜的
# 池第 1 套（等于一分钱不多花），开了才把贵的那支用在质量敏感的环节上。
#
# 与"按用途分模型"的关系是**优先级**而不是替代：那张卡仍然保留，且更具体（单独指名
# 某个用途）⇒ 它盖过这一层。这一层负责的是"一次性把高级模型铺到几个环节"。
LLM_PREMIUM_KEY = "llm_premium"

ROLES: tuple[str, ...] = (
    "agent",
    "judge",
    "judge_escalate",  # 复核层：初判失败/闸门报警时才动用（更贵的模型放这里）
    "narrative",
    "specgen",
    "assistant",
)

# 高端模型可以挂到哪些用途上。
#
# agent 也在列 —— 但界面默认不勾、并且勾上要警告：它占全部 token 的约 95%，
# 把执行步换贵等于把省钱的**主旋钮**关掉。允许它是尊重"我就想要最高质量"，
# 默认不勾是不让人一开总开关就悄悄多花一大笔。
PREMIUM_ROLES: tuple[str, ...] = (
    "judge_escalate",
    "judge",
    "specgen",
    "narrative",
    "assistant",
    "agent",
)

# 一开开关就默认勾上的用途：都是"量小、质量关键"的那几个（每条用例零到一次，
# 输入几 k）。judge 不在里面 —— 判定的初判刻意留给便宜模型，那正是两级判定的意义。
PREMIUM_DEFAULT_ROLES: tuple[str, ...] = ("judge_escalate", "specgen", "narrative")

# 各角色 token 占比的**结构估算**（2026-10-12）。
#
# 用途：设置页在「高端模型接管」每个用途勾选项旁显示「这个用途占全部 token 的百分之几」，
# 让用户一眼看清"勾哪个最烧钱"。优先用真实实测（执行流里按角色拆分的 by_role 账本聚合，
# 见 app/executor.py / app/judge.py）；**只有该角色没有真实数据时**才回退到这个估算，
# 且界面会明确标 ≈，绝不把估算当真实显示。
#
# 依据（不是拍脑袋，是系统工作方式）：agent 每条用例跑 N 步（N=浏览器操作数，可达 5~33），
# 每次 LLM 往返约 1.6 万输入 token；judge 每条用例固定一次（~3-4k 输入）、
# judge_escalate 仅在失败/闸门报警时才调用、narrative 仅在失败时、specgen 各一次、
# assistant 偶发。所以 agent 占绝对多数 —— 这正是"省钱主旋钮在 agent"的量化依据。
# 六个值之和必须为 100（前端按它做兜底占比）。
ROLE_TOKEN_SHARE_ESTIMATE: dict[str, float] = {
    "agent": 94.0,
    "judge": 2.5,
    "judge_escalate": 1.5,
    "specgen": 1.0,
    "narrative": 0.5,
    "assistant": 0.5,
}



@dataclass(frozen=True)
class RoleOverride:
    """One purpose's model override. 空字符串一律表示"继承默认配置"。

    只存"这个用途要改的部分"：多数场景只需要换个模型名（同一网关不同模型），
    要换厂商时才填 base_url/api_key/provider。
    """

    model: str = ""
    base_url: str = ""
    api_key: str = ""
    provider: str = ""

    @property
    def empty(self) -> bool:
        return not (self.model or self.base_url or self.api_key or self.provider)


@dataclass(frozen=True)
class PremiumModel:
    """平台级的「高端模型」槽位（贵、聪明，可选）。

    语义刻意做得很窄，因为它的价值就在"一句话说清"：**开或不开**，开了管哪几个环节。
    所以只需要一份连接信息 + 一份用途名单，而不是每个用途各存一套。

    不生效的三种情形都必须老老实实回落，不能"看起来配了其实没用"：
      · enabled 关；
      · 没填 model（只填了网关/钥匙等于说不出要调什么模型）；
      · roles 空（开了却没说管哪几件事）。
    """

    enabled: bool = False
    model: str = ""
    base_url: str = ""
    api_key: str = ""
    provider: str = ""
    roles: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        """能不能真的派上用场（三个条件缺一不可）。"""
        return bool(self.enabled and self.model and self.roles)

    def covers(self, role: str) -> bool:
        return self.usable and role in self.roles

    @property
    def empty(self) -> bool:
        return not (self.enabled or self.model or self.base_url or self.api_key or self.roles)


@dataclass(frozen=True)
class LLMConfig:
    base_url: str
    api_key: str
    model: str  # judge + default agent model
    agent_model: str  # "" => use model
    provider: str = "auto"  # auto | openai | anthropic | google | ollama (见 llm_protocol)


@dataclass(frozen=True)
class LLMProfile:
    """One concrete LLM gateway in the pool (a "set of credentials").

    The pool lets an operator configure several gateways (or several keys of the
    same gateway) and have the runner fall over to the next one when the active
    key is rate-limited / exhausted — the exact failure that tanked a 226-case
    full run on 2026-10-09 (quota ceiling hit, 429 across the board).

    provider 决定"用什么协议说话"，默认 auto（自动识别，见 app/llm_protocol.py）：
    绝大多数网关是 OpenAI 兼容，填 base_url/key/model 就够；Claude/Gemini/Ollama
    写清 provider 即可，调用方代码不需要为换厂商改一行。
    """

    name: str
    base_url: str
    api_key: str
    model: str
    agent_model: str = ""  # "" => use model
    provider: str = "auto"

    @property
    def effective_model(self) -> str:
        return self.agent_model or self.model


# AppSetting key holding the pool. A single encrypted blob: a JSON array where each
# element is {"name","base_url","api_key","model","agent_model","provider"}. The whole
# blob is stored secret=True so every api_key inside it is encrypted at rest.
LLM_POOL_KEY = "llm_pool"
LLM_PROVIDER_KEY = "llm_provider"  # legacy single-profile protocol (admin-overridable)


_cached: LLMConfig | None = None
_cached_at: float = 0.0
_roles_cached: dict[str, "RoleOverride"] | None = None
_roles_cached_at: float = 0.0
_two_tier_cached: bool | None = None
_two_tier_cached_at: float = 0.0
_premium_cached: "PremiumModel | None" = None
_premium_cached_at: float = 0.0
_TTL_S = 30.0  # ponytail: pull-based refresh; push invalidation across processes if 30s lag hurts


def invalidate_llm_cache() -> None:
    """Drop this process's cache (called after an admin update).

    协议/能力探测结果一起清掉：换了 base_url 或模型之后，"旧端点支持什么"
    对新端点毫无参考价值，留着只会让新配置静默继承旧结论。
    """
    global _cached, _roles_cached, _two_tier_cached, _premium_cached
    _cached = None
    _roles_cached = None
    _two_tier_cached = None
    _premium_cached = None
    from app.llm_protocol import invalidate_capabilities

    invalidate_capabilities()


async def llm_config() -> LLMConfig:
    """Effective LLM config: AppSetting overrides over env defaults, briefly cached."""
    global _cached, _cached_at
    if _cached is not None and time.monotonic() - _cached_at < _TTL_S:
        return _cached
    s = get_settings()
    over: dict[str, str | None] = {}
    try:
        from app.db import db_session
        from app.settings_store import get_setting

        async with db_session() as sess:
            for key in _KEYS:
                over[key] = await get_setting(sess, key)
    except Exception:  # DB unavailable (early boot, bare tests) -> env values only
        over = {}
    _cached = LLMConfig(
        base_url=over.get(LLM_BASE_URL_KEY) or s.gateway_base_url,
        api_key=over.get(LLM_API_KEY_KEY) or s.gateway_api_key,
        model=over.get(LLM_MODEL_KEY) or s.gateway_model,
        agent_model=over.get(LLM_AGENT_MODEL_KEY) or s.agent_model,
        provider=over.get(LLM_PROVIDER_SETTING_KEY) or s.gateway_provider,
    )
    _cached_at = time.monotonic()
    return _cached


async def active_profile() -> LLMProfile:
    """平台默认那套 —— 也就是**真正在跑**的那一套。

    ★ 2026-10-11 语义修正。以前它返回的是 legacy 单套（AppSetting `llm_model` 等 +
    env），而那五个字段在**池非空时完全不生效**（load_pool 会整体取代它们）。
    于是所有"没传用途"的调用点（助手、飞书、能力探测）都在拿一套没在用的配置说话，
    而设置页还把它当成"正在使用"显示出来 —— 显示值和实际值对不上，且不报错。

    现在它 = `pool_for_role("agent")[0]`，与 executor 实际用的那一套逐字节一致。
    "谁在生效"只有这一个口径。
    """
    return (await pool_for_role("agent"))[0]


async def _load_pool_raw() -> tuple[list[LLMProfile], str]:
    """(候选序列, 来源)。来源取值：

      "pool" —— 池里有完整条目，这是真正在用的配置；
      "env"  —— 池为空或条目都不完整，回落到 env / 旧单套字段（首次启动、DB 不可用）。

    把来源单独回出来，是为了让界面能如实说明"现在生效的是哪一层"。
    没有这个字段，设置页就只能猜，而猜错的表现正是显示一个没在跑的模型名。
    """
    import json

    # Legacy single profile (also the fallback shape).
    cfg = await llm_config()
    legacy = LLMProfile(
        name="env",
        base_url=cfg.base_url,
        api_key=cfg.api_key,
        model=cfg.model,
        agent_model=cfg.agent_model,
        provider=cfg.provider,
    )
    try:
        from app.db import db_session
        from app.settings_store import get_setting

        async with db_session() as sess:
            raw = await get_setting(sess, LLM_POOL_KEY)
    except Exception:  # DB unavailable (early boot / bare test) -> legacy only
        raw = None
    if not raw:
        return [legacy], "env"
    try:
        items = json.loads(raw)
        profiles: list[LLMProfile] = []
        for i, it in enumerate(items or []):
            if not isinstance(it, dict):
                continue
            bu = (it.get("base_url") or "").strip()
            key = (it.get("api_key") or "").strip()
            model = (it.get("model") or "").strip()
            if not bu or not model or not key:
                continue  # an incomplete entry is useless, skip it
            profiles.append(
                LLMProfile(
                    name=(it.get("name") or f"llm{i + 1}").strip(),
                    base_url=bu,
                    api_key=key,
                    model=model,
                    agent_model=(it.get("agent_model") or "").strip(),
                    provider=(it.get("provider") or "auto").strip() or "auto",
                )
            )
        if profiles:
            return profiles, "pool"
    except Exception:  # malformed JSON -> don't take the whole platform down
        pass
    return [legacy], "env"


async def load_pool() -> list[LLMProfile]:
    """The active LLM pool, ordered by priority (first = preferred).

    Source of truth is the encrypted `llm_pool` AppSetting blob. When it is absent
    or empty, fall back to the env values so a pre-existing install keeps working
    with one implicit profile. Never raises: a bad blob degrades to that profile
    rather than breaking every LLM call. Pool is re-read on every call — the
    alternative (a process-wide cache) would make "saved but not applied" possible,
    which is exactly the class of bug this platform has been fighting.
    """
    return (await _load_pool_raw())[0]


async def pool_origin() -> str:
    """"pool" = 生效配置来自模型池；"env" = 池为空，回落到 env 里的那一套。

    界面靠它说清"你正在编辑的那一层，是不是当前生效的那一层"。
    """
    return (await _load_pool_raw())[1]


async def load_roles() -> dict[str, RoleOverride]:
    """按用途的模型覆盖，读自加密 AppSetting `llm_roles`。**永不抛异常**：

    配置坏了只该退化成"全用默认模型"，绝不能让每一次 LLM 调用都失败。
    """
    global _roles_cached, _roles_cached_at
    if _roles_cached is not None and time.monotonic() - _roles_cached_at < _TTL_S:
        return _roles_cached

    import json

    try:
        from app.db import db_session
        from app.settings_store import get_setting

        async with db_session() as sess:
            raw = await get_setting(sess, LLM_ROLES_KEY)
    except Exception:  # DB unavailable (early boot / bare test)
        raw = None

    out: dict[str, RoleOverride] = {}
    if raw:
        try:
            for role, it in (json.loads(raw) or {}).items():
                if role not in ROLES or not isinstance(it, dict):
                    continue
                out[role] = RoleOverride(
                    model=(it.get("model") or "").strip(),
                    base_url=(it.get("base_url") or "").strip(),
                    api_key=(it.get("api_key") or "").strip(),
                    provider=(it.get("provider") or "").strip(),
                )
        except Exception:
            out = {}
    _roles_cached = out
    _roles_cached_at = time.monotonic()
    return out


async def load_premium() -> PremiumModel:
    """平台级「高端模型」配置，读自加密 AppSetting `llm_premium`。**永不抛异常**。

    与 load_roles 同样的原则：配置坏了只该退化成"没有高端模型"，
    绝不能让每一次 LLM 调用都失败 —— 一个可选的加速/提质层不该有把平台打挂的能力。
    """
    global _premium_cached, _premium_cached_at
    if _premium_cached is not None and time.monotonic() - _premium_cached_at < _TTL_S:
        return _premium_cached

    import json

    try:
        from app.db import db_session
        from app.settings_store import get_setting

        async with db_session() as sess:
            raw = await get_setting(sess, LLM_PREMIUM_KEY)
    except Exception:  # DB unavailable (early boot / bare test)
        raw = None

    out = PremiumModel()
    if raw:
        try:
            it = json.loads(raw) or {}
            if isinstance(it, dict):
                added = [
                    r
                    for r in (it.get("roles") or [])
                    if isinstance(r, str) and r in PREMIUM_ROLES
                ]
                out = PremiumModel(
                    enabled=bool(it.get("enabled")),
                    model=(it.get("model") or "").strip(),
                    base_url=(it.get("base_url") or "").strip(),
                    api_key=(it.get("api_key") or "").strip(),
                    provider=(it.get("provider") or "").strip(),
                    # 按 PREMIUM_ROLES 的固定顺序归一，界面勾选顺序不该影响存储内容
                    roles=tuple(r for r in PREMIUM_ROLES if r in set(added)),
                )
        except Exception:  # malformed JSON -> "没有高端模型"
            out = PremiumModel()
    _premium_cached = out
    _premium_cached_at = time.monotonic()
    return out


async def premium_for_ui() -> dict:
    """给设置页看的高端模型配置（钥匙只回"有没有存过"，不回明文）。"""
    prem = await load_premium()
    return {
        "enabled": prem.enabled,
        "model": prem.model,
        "base_url": prem.base_url,
        "provider": prem.provider,
        "api_key_set": bool(prem.api_key),
        "roles": list(prem.roles),
        # 配了但用不起来（缺 model 或一个用途都没勾）—— 界面必须说清，否则用户
        # 会以为"开了就生效了"，实际一分钱没花、质量也没提升。
        "usable": prem.usable,
        "applies_to": [r for r in ROLES if prem.covers(r)],
    }


async def judge_two_tier_enabled() -> bool:
    """两级判定的开关（admin 可改，默认关）。

    关着的时候行为与以前**逐字节一致** —— 这是刻意的：这个开关会在"初判失败/证据可疑"
    时多花一次强模型调用，属于"明确花钱买判定质量"的选择，只能由人打开（悄悄打开
    等于悄悄加钱）。开关本身也存在 AppSetting 里，改了立刻生效。
    """
    global _two_tier_cached, _two_tier_cached_at
    if _two_tier_cached is not None and time.monotonic() - _two_tier_cached_at < _TTL_S:
        return _two_tier_cached

    val: str | None = None
    try:
        from app.db import db_session
        from app.settings_store import get_setting

        async with db_session() as sess:
            val = await get_setting(sess, LLM_JUDGE_TWO_TIER_KEY)
    except Exception:  # DB unavailable (early boot / bare test) -> env default
        val = None

    if val is None or val == "":
        enabled = bool(getattr(get_settings(), "judge_two_tier", False))
    else:
        enabled = str(val).strip().lower() in ("1", "true", "yes", "on")
    _two_tier_cached = enabled
    _two_tier_cached_at = time.monotonic()
    return enabled


async def role_profile(role: str) -> LLMProfile:
    """这个用途实际该用的 profile。三层，越具体越优先：

      ① 池的第 1 套（平台默认，也就是**实际在跑**的那套）
      ② 平台级「高端模型」（如果开着，且这个用途被勾选）
      ③ 「按用途分模型」里这个用途自己的覆盖（最具体 ⇒ 盖过 ①②）

    "默认"取池里优先级最高那套（没有池就是 legacy 单套）—— 也就是平台**实际在跑**
    的那个模型，而不是设置页上可能已经不再生效的旧字段。

    池与用途是**正交**的两件事：池回答"这套 key 用完了换哪套"（429 兜底），
    用途回答"这件事该用哪个模型"。混在一起会让"省钱的模型"被 429 兜底悄悄换成贵的。

    ②与③的关系是优先级而不是替代：②的价值是"一次勾选管几个环节"，③的价值是
    "单独指名某个环节"。所以③赢 —— 但也正因为③赢，界面在两者同时命中同一个用途时
    必须提示，否则用户会觉得高速模型"配了没生效"。
    """
    pool = await load_pool()
    base = pool[0]

    prem = await load_premium()
    if prem.covers(role):
        # 只写 model 也能用：网关/钥匙没填就沿用池第 1 套的（多数人是同一个网关换模型名）。
        base = LLMProfile(
            name=f"premium:{prem.model}",
            base_url=prem.base_url or base.base_url,
            api_key=prem.api_key or base.api_key,
            model=prem.model,
            # 换了模型就不再沿用默认的 agent_model —— 那个默认名在新模型上多半不存在。
            agent_model="",
            provider=prem.provider or base.provider,
        )

    ov = (await load_roles()).get(role)
    if ov is None or ov.empty:
        return base
    return LLMProfile(
        name=f"{role}:{ov.model or base.model}",
        base_url=ov.base_url or base.base_url,
        api_key=ov.api_key or base.api_key,
        model=ov.model or base.model,
        # 换了模型就不再沿用默认的 agent_model —— 那个默认名在新模型上多半不存在。
        agent_model="" if ov.model else base.agent_model,
        provider=ov.provider or base.provider,
    )


async def role_source(role: str) -> str:
    """这个用途的**生效值来自哪一层**：pool / premium / override。

    界面靠它说清"我配的高端模型到底有没有管到这里"。没有这个字段，用户只能看着
    一个模型名猜它从哪来 —— 而猜错的表现正是"我明明开了高端模型却没生效"。
    """
    ov = (await load_roles()).get(role)
    if ov is not None and not ov.empty:
        return "override"
    if (await load_premium()).covers(role):
        return "premium"
    return "pool"


async def pool_for_role(role: str) -> list[LLMProfile]:
    """该用途的候选序列：用途 profile 优先，其余池条目仍做 429 兜底。

    兜底**保留**（这是池的原意：配额耗尽不能把整轮跑废），但对"用途换了模型"
    的那一套要去重，否则同一个模型会被试两遍。
    """
    pool = await load_pool()
    rp = await role_profile(role)
    out = [rp]
    seen = {(rp.base_url, rp.model, rp.api_key)}
    for p in pool:
        k = (p.base_url, p.model, p.api_key)
        if k in seen:
            continue
        seen.add(k)
        out.append(p)
    return out


async def roles_for_ui() -> dict[str, dict]:
    """给设置页看的角色配置（不带钥匙明文，只回"有没有单独配过钥匙"）。

    生效值一律走 `role_profile(role)` 取，而不是在这里自己推算继承 —— 三层优先级
    （池 → 高端模型 → 按用途覆盖）只该有一处实现，两处各算一遍迟早会漂移，
    而漂移的表现就是"界面显示一个没在跑的模型名"。
    """
    roles = await load_roles()
    out: dict[str, dict] = {}
    for role in ROLES:
        ov = roles.get(role)
        eff = await role_profile(role)
        out[role] = {
            "model": (ov.model if ov else "") or "",
            "base_url": (ov.base_url if ov else "") or "",
            "provider": (ov.provider if ov else "") or "",
            "api_key_set": bool(ov and ov.api_key),
            # 生效值：界面直接显示"这个用途现在实际用哪个模型"，而不是让人自己推算继承。
            "effective_model": eff.model,
            "effective_base_url": eff.base_url,
            # 生效值来自哪一层（pool / premium / override）。
            "source": await role_source(role),
        }
    return out


def _is_rate_limited(exc: BaseException) -> bool:
    """True when the exception is an HTTP 429 from the gateway (any provider wording).

    Both openai-python's RateLimitError and a raw 404/429 status qualify. We only
    match the rate-limit class or a response whose status_code == 429; anything else
    (401 bad key, 404 wrong model) is NOT a quota issue and must NOT trigger a
    fall-over — those are misconfiguration, and rotating to the next key will just
    fail the same way.
    """
    code = getattr(exc, "status_code", None)
    if code == 429:
        return True
    name = type(exc).__name__
    return name in ("RateLimitError", "RateLimit")


async def probe_profile(profile: LLMProfile) -> bool:
    """One 1-token completion against a single profile; True iff it succeeds (200).

    This is the liveness check used both by the "which key do I start this case
    with" pre-flight and by judge's fall-over. Mirrors the real client's proxy and
    TLS policy exactly, otherwise the probe says OK while the actual call still
    fails — the most misleading outcome for a probe. A 429 means "this key is
    exhausted, try the next"; any other error means "this entry is broken, try
    the next." Either way a failed probe just moves on.

    协议无关：先读（带缓存的）能力结论拿到协议，再按该协议发最小存活请求。
    能力探测本身不在每个用例上重跑 —— 它被 TTL 缓存，这里的开销仍然是 1 次调用。
    """
    from app.llm_protocol import capabilities_for

    s = get_settings()
    if not profile.base_url or not profile.model:
        return False
    caps = await capabilities_for(profile)
    http = httpx.AsyncClient(
        verify=s.gateway_verify_ssl,
        timeout=httpx.Timeout(30.0),
        trust_env=not s.gateway_ignore_proxy,
    )
    client = None
    try:
        if caps.protocol == "anthropic":
            from anthropic import AsyncAnthropic

            client = AsyncAnthropic(
                api_key=profile.api_key, base_url=profile.base_url or None,
                http_client=http, max_retries=0,
            )
            await client.messages.create(
                model=profile.effective_model, max_tokens=1,
                messages=[{"role": "user", "content": "ping"}],
            )
            return True
        if caps.protocol == "google":
            from google import genai
            from google.genai import types as gtypes

            gc = genai.Client(
                api_key=profile.api_key,
                http_options=gtypes.HttpOptions(httpx_async_client=http, timeout=30000),
            )
            try:
                await gc.aio.models.get(model=profile.effective_model)
                return True
            finally:
                try:
                    await gc.aio.aclose()
                except Exception:  # noqa: BLE001
                    pass
        from openai import AsyncOpenAI

        base = profile.base_url
        if caps.protocol == "ollama" and not base.rstrip("/").endswith("/v1"):
            base = base.rstrip("/") + "/v1"
        client = AsyncOpenAI(
            base_url=base,
            api_key=profile.api_key or "not-needed",
            http_client=http,
            max_retries=0,  # no backoff on a probe — fail fast, fall over
        )
        await client.chat.completions.create(
            model=profile.effective_model,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        if _is_rate_limited(exc):
            pass  # exhausted -> not usable, but NOT a misconfig
        return False
    finally:
        if client is not None:
            await client.close()
        elif caps.protocol in ("anthropic", "google"):
            await http.aclose()


async def pick_active_profile() -> tuple[LLMProfile, list[LLMProfile]]:
    """Pre-flight router: return (chosen_profile, full_candidate_list).

    Walks the candidates in priority order and picks the FIRST profile whose probe
    succeeds (200). If every candidate fails (all rate-limited / all broken), fall
    back to the first one anyway — the case will fail with its own clear
    error rather than being silently skipped, and at least we don't hide a total
    outage behind a "no key" message.

    候选序列来自 `pool_for_role("agent")`：**agent 每步调用占全部 token 的约 95%**
    （真库实测），所以"执行用哪个模型"是唯一能显著省钱的旋钮；判定/生成等
    质量敏感但量小的用途另有各自的角色配置。
    """
    pool = await pool_for_role("agent")
    if len(pool) == 1:
        # Single-set install: no fall-over possible, and a pre-flight probe would just
        # add ~4s to every case for nothing. Use it directly (preserves today's
        # behaviour byte-for-byte for the common 1-key case).
        return pool[0], pool
    for prof in pool:
        if await probe_profile(prof):
            return prof, pool
    # nothing live right now — return the preferred one; callers that want to know
    # "this is degraded" can compare against pool[0].
    return pool[0], pool


async def client_for(profile: LLMProfile, *, timeout: float = 60.0) -> AsyncOpenAI:
    """An AsyncOpenAI client bound to one specific pool profile (proxy/TLS mirrored)."""
    s = get_settings()
    http = httpx.AsyncClient(
        verify=s.gateway_verify_ssl, timeout=timeout, trust_env=not s.gateway_ignore_proxy
    )
    return AsyncOpenAI(
        base_url=profile.base_url,
        api_key=profile.api_key,
        http_client=http,
        max_retries=s.llm_max_retries,
    )


def _structured_output_override() -> bool | None:
    """config.agent_json_schema 三态 → True(强制) / False(永不) / None(auto，靠探测)。"""
    v = str(getattr(get_settings(), "agent_json_schema", "auto") or "auto").strip().lower()
    if v in ("true", "1", "yes", "on", "force"):
        return True
    if v in ("false", "0", "no", "off", "never"):
        return False
    return None


async def agent_llm_for(profile: LLMProfile):
    """A browser-use chat client bound to one pool profile (the per-step agent model).

    按 profile 的协议自动选类（ChatOpenAI / ChatAnthropic / ChatGoogle / ChatOllama），
    并把"要不要发 response_format=json_schema"交给能力探测 —— 换厂商不用改代码。
    """
    return await _build_agent_llm(profile)


async def _build_agent_llm(profile: LLMProfile):
    """一个协议无关的 browser-use 客户端。所有分支都保留同一套代理/TLS 策略。"""
    from app.llm_protocol import capabilities_for

    s = get_settings()
    caps = await capabilities_for(profile)
    http = httpx.AsyncClient(
        verify=s.gateway_verify_ssl, timeout=120, trust_env=not s.gateway_ignore_proxy
    )
    max_tokens = s.agent_max_tokens or s.gateway_max_tokens

    if caps.protocol == "anthropic":
        from browser_use import ChatAnthropic

        return ChatAnthropic(
            model=profile.effective_model,
            api_key=profile.api_key,
            base_url=profile.base_url or None,
            http_client=http,
            # Anthropic 的 max_tokens 必填；代理每一步的输出不长，但 thinking 会吃额度
            max_tokens=max_tokens,
            max_retries=s.llm_max_retries,
        )

    if caps.protocol == "google":
        from browser_use import ChatGoogle
        from google.genai import types as gtypes

        return ChatGoogle(
            model=profile.effective_model,
            api_key=profile.api_key,
            max_output_tokens=max_tokens,
            # Gemini 的原生结构化输出走 responseSchema，兼容层标记由 browser-use 处理
            supports_structured_output=caps.json_schema,
            http_options=gtypes.HttpOptions(
                base_url=(profile.base_url or "").strip().rstrip("/") or None,
                httpx_async_client=http,
            ),
            max_retries=s.llm_max_retries,
        )

    if caps.protocol == "ollama":
        from browser_use import ChatOllama

        base = (profile.base_url or "http://127.0.0.1:11434").strip().rstrip("/")
        if base.endswith("/v1"):
            base = base[: -len("/v1")]
        await http.aclose()
        return ChatOllama(
            model=profile.effective_model,
            host=base,
            timeout=120,
        )

    from browser_use import ChatOpenAI

    override = _structured_output_override()
    force_schema = caps.json_schema if override is None else override
    return ChatOpenAI(
        model=profile.effective_model,
        base_url=profile.base_url,
        api_key=profile.api_key or "not-needed",
        http_client=http,
        # complex pages produce long structured output; the 4096 default truncates it
        # into invalid JSON. raise the ceiling and also put the schema in the system
        # prompt so the model reliably returns the right shape.
        max_completion_tokens=max_tokens,
        # ★ 2026-10-10：`add_schema_to_system_prompt` 只是**额外**把 schema 写进 system
        # prompt，并不能阻止 browser-use 发 response_format（源码 chat.py:225-256）。
        # 真正决定发不发的是 dont_force_structured_output —— 由能力探测的结果驱动：
        # 网关拒 json_schema（400 This response_format type is unavailable now）时关掉，
        # 否则每一步都 400，用例表现为"只导航、零动作"（run17 TP-CCR-001/005）。
        add_schema_to_system_prompt=True,
        dont_force_structured_output=not force_schema,
        # ★ 429 重试次数 —— 2026-10-06 实测这是当前最大的时间黑洞。
        #
        # 现象：一条只跑 3 步、还登录失败的用例，`判定描述` 阶段耗了 10.6s，
        # 日志里是 **8 次连续 429**（"您已达到免费用户的 API 速率限制"），
        # 全部来自 OpenAI SDK 的自动重试 + 指数退避。
        #
        # browser-use 的 ChatOpenAI 自己把默认值抬到了 **5**（源码注释：
        # "Increase default retries for automation reliability"），比 SDK 默认的 2 更高。
        # 对"偶发网络抖动"重试是对的；但对"配额已耗尽"这种**必然失败**的状态，
        # 重试只是把 1 次失败拖成 5 次退避等待 —— 时间花完了，结果还是失败。
        #
        # 所以这里设 0：429 由上层（步骤重试 / 判定降级）按语义处理，
        # 不让传输层盲目重试。要恢复成SDK 默认就设 2，要更激进设 5（=browser-use 原值）。
        max_retries=s.llm_max_retries,
    )


# ---------------------------------------------------------------------------
# 门面转发：调用方只 import app.llm 就够了，不必记住模块分工。
# 放在文件末尾是为了避免与 app.llm_chat 的延迟导入绕成环。
# ---------------------------------------------------------------------------
from app.llm_chat import ChatResult, chat, chat_with_pool, empty_usage, extract_json  # noqa: E402
from app.llm_protocol import Capabilities, capabilities_for  # noqa: E402

__all__ = [
    "PREMIUM_DEFAULT_ROLES",
    "PREMIUM_ROLES",
    "ROLES",
    "LLMConfig",
    "LLMProfile",
    "PremiumModel",
    "RoleOverride",
    "ChatResult",
    "Capabilities",
    "active_profile",
    "agent_llm_for",
    "browser_use_llm",
    "capabilities_for",
    "chat",
    "chat_with_pool",
    "client_for",
    "empty_usage",
    "extract_json",
    "llm_config",
    "load_pool",
    "load_premium",
    "judge_two_tier_enabled",
    "load_roles",
    "openai_client",
    "pick_active_profile",
    "pool_for_role",
    "pool_origin",
    "premium_for_ui",
    "probe_profile",
    "role_profile",
    "role_source",
    "roles_for_ui",
]


async def openai_client() -> AsyncOpenAI:
    """Async OpenAI client for the judge. verify=False tolerates a self-signed gateway cert."""
    s = get_settings()
    cfg = await llm_config()
    http = httpx.AsyncClient(
        verify=s.gateway_verify_ssl, timeout=60, trust_env=not s.gateway_ignore_proxy
    )
    return AsyncOpenAI(
        base_url=cfg.base_url,
        api_key=cfg.api_key,
        http_client=http,
        max_retries=s.llm_max_retries,
    )


async def browser_use_llm():
    """LLM handed to the browser-use Agent. Imported lazily so `app.engine` stays import-safe.

    单套配置路径：把当前生效配置包成一个 profile，再走同一套协议分发。
    """
    return await _build_agent_llm(await active_profile())
