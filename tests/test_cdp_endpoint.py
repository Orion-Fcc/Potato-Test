"""How a BROWSER_CDP_ENDPOINT string turns into an attach target — and what it must refuse.

The dangerous failure here is silent: attach to the wrong endpoint, drive someone else's
browser, or silently disable the feature on a typo and never say so. Most of these tests
are therefore about the refusals and the "degrades to the old path" behaviour.
"""

from __future__ import annotations

from app import cdp_endpoint as ce


def test_empty_means_feature_off():
    """The default must keep the old behaviour: "" launches our own browser."""
    assert ce.normalize_endpoint("") is None
    assert ce.normalize_endpoint("   ") is None
    assert ce.normalize_endpoint(None) is None


def test_bare_port_becomes_loopback():
    assert ce.normalize_endpoint("9222") == "127.0.0.1:9222"
    assert ce.normalize_endpoint(" 9333 ") == "127.0.0.1:9333"


def test_host_port_passes_through():
    assert ce.normalize_endpoint("127.0.0.1:9222") == "127.0.0.1:9222"
    assert ce.normalize_endpoint("localhost:9222") == "localhost:9222"


def test_scheme_is_accepted_and_dropped():
    """People write both forms; both must work."""
    assert ce.normalize_endpoint("http://127.0.0.1:9222") == "127.0.0.1:9222"
    assert ce.normalize_endpoint("http://localhost:9222") == "localhost:9222"


def test_host_without_port_falls_back_to_the_documented_default():
    """Explicit beats implicit: we say where we think we are going."""
    assert ce.normalize_endpoint("localhost") == f"localhost:{ce.DEFAULT_CDP_PORT}"
    assert ce.normalize_endpoint("127.0.0.1") == f"127.0.0.1:{ce.DEFAULT_CDP_PORT}"


def test_remote_host_is_refused():
    """Driving someone else's browser over the network must not be one .env typo away.

    This is the one refusal that is a policy decision rather than a parse error, so it
    is asserted directly instead of being folded into a generic "bad value" case.
    """
    # 用 TEST-NET-1（192.0.2.0/24，RFC 5737 专供文档/示例）而不是真实内网地址：
    # 本仓库是公开的，测试夹具没有理由把内网拓扑写进去，而这个断言只关心
    # "非回环主机一律拒绝"，具体是哪个地址无关紧要。
    assert ce.normalize_endpoint("192.0.2.10:9222") is None
    assert ce.normalize_endpoint("http://evil.example.com:9222") is None


def test_out_of_range_port_disables_rather_than_raising():
    """A typo in .env degrades to the well-tested launch path."""
    assert ce.normalize_endpoint("99999") is None
    assert ce.normalize_endpoint("0") is None


def test_garbage_disables_rather_than_raising():
    """normalize_endpoint must never raise — it runs while reading settings."""
    for bad in ("9222abc", "not a port", "://", "http://", ":::"):
        assert ce.normalize_endpoint(bad) is None, bad


def test_describe_states_the_intent():
    """The log line has to make it obvious we are not launching, and not closing."""
    on = ce.describe("127.0.0.1:9222")
    assert "attach" in on and "9222" in on
    assert "不启动" in on and "不关闭" in on
    off = ce.describe(None)
    assert "自启动" in off
