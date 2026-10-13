import { AlertTriangle, ChevronDown, ChevronRight, Loader2, Plug, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { api, type ApiEndpoint, type ApiProbeResult } from "../lib/api";
import { Badge, Button, Card } from "./ui";
import { useErrorToast } from "./toast";

/** 后端接口探索（2026-10-09）。
 *
 *  ## 为什么要有这个界面
 *
 *  用户的原话：「我希望这个系统也具有探索后端的能力……他需要有探索后端的能力去拿到
 *  这个菜单或者相关的数据。」
 *
 *  接口清单是**跑用例时顺带观察**出来的（页面为了渲染必然要调后端），所以：
 *  没跑过用例就是空的 —— 这一点必须在界面上说清楚，否则用户会以为功能坏了。
 *
 *  ## 为什么把"读"和"写"分得这么显眼
 *
 *  用户选定的边界是「探索只读，写操作必须显式声明」。写接口 POST/PUT/DELETE
 *  在这个界面上**不能点**（后端也会拒），而且用醒目的颜色标出来 ——
 *  因为一张混着写接口的清单，让人误点一次的代价是在测试环境里建/删真数据。
 *  清单里保留它们是有价值的（知道系统有哪些写接口），但只能是"知道"。
 */
/** 默认只渲染这么多个（2026-10-13）：实测 35+ 条会占 1836px。 */
const ENDPOINT_PREVIEW = 8;

export function ApiEndpointsPanel({ pid }: { pid: number }) {
  const { t } = useTranslation();
  const fail = useErrorToast();

  const [data, setData] = useState<{ endpoints: ApiEndpoint[]; total: number; note: string } | null>(
    null,
  );
  const [loading, setLoading] = useState(true);
  const [open, setOpen] = useState<number | null>(null);
  // ★ 列表默认只显示前几个（2026-10-13）：这份清单实测能到 35+ 条、占 **1836px**
  //   （概览页一共 3807px —— 它一个人占掉一半）。默认收起大半，要看全的按一下。
  const [showAll, setShowAll] = useState(false);
  const [probe, setProbe] = useState<Record<number, ApiProbeResult | "loading">>({});

  const load = useCallback(
    () =>
      api
        .listApiEndpoints(pid)
        .then(setData)
        .catch(fail)
        .finally(() => setLoading(false)),
    [pid, fail],
  );

  useEffect(() => {
    load();
  }, [load]);

  const run = async (e: ApiEndpoint) => {
    setProbe((p) => ({ ...p, [e.id]: "loading" }));
    try {
      const r = await api.probeApiEndpoint(pid, { method: e.method, path: e.path });
      setProbe((p) => ({ ...p, [e.id]: r }));
    } catch (err) {
      fail(err);
      setProbe((p) => {
        const next = { ...p };
        delete next[e.id];
        return next;
      });
    }
  };

  const read = data?.endpoints.filter((e) => !e.writable) ?? [];
  const write = data?.endpoints.filter((e) => e.writable) ?? [];
  const all = [...read, ...write];
  const shown = showAll ? all : all.slice(0, ENDPOINT_PREVIEW);

  return (
    <Card className="p-4">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
        <div>
          <div className="flex items-center gap-2 text-[15px] font-semibold text-ink-900">
            <Plug className="h-4 w-4 text-brand-600" />
            {t("Backend API discovered from page traffic")}
          </div>
          <div className="mt-0.5 text-xs text-ink-500">
            {t("Learned by watching what the page actually calls — nothing is scanned or guessed.")}
          </div>
        </div>
        <Button variant="ghost" size="sm" onClick={load} disabled={loading}>
          <RefreshCw className={"h-3.5 w-3.5 " + (loading ? "animate-spin" : "")} />
          {t("Refresh")}
        </Button>
      </div>

      {loading ? (
        <div className="flex items-center gap-2 py-6 text-sm text-ink-500">
          <Loader2 className="h-4 w-4 animate-spin" /> {t("Loading…")}
        </div>
      ) : !data || data.total === 0 ? (
        <div className="rounded-lg border border-dashed border-[var(--line)] px-4 py-6 text-sm text-ink-500">
          {data?.note ??
            t("Run some cases first — the page's own requests are how this list gets built.")}
        </div>
      ) : (
        <>
          <div className="mb-3 flex flex-wrap items-center gap-3 text-xs text-ink-500">
            <span>{t("{{n}} endpoints observed", { n: data.total })}</span>
            <span>
              {t("{{r}} readable · {{w}} writable (view only)", { r: read.length, w: write.length })}
            </span>
          </div>
          <div className="space-y-1">
            {shown.map((e) => {
              const isOpen = open === e.id;
              const pr = probe[e.id];
              return (
                <div key={e.id} className="rounded-lg border border-[var(--line)]">
                  <div className="flex items-center gap-2 px-2.5 py-1.5">
                    <button
                      onClick={() => setOpen(isOpen ? null : e.id)}
                      className="flex min-w-0 flex-1 items-center gap-2 text-left"
                    >
                      {isOpen ? (
                        <ChevronDown className="h-3.5 w-3.5 shrink-0 text-ink-400" />
                      ) : (
                        <ChevronRight className="h-3.5 w-3.5 shrink-0 text-ink-400" />
                      )}
                      {/* Badge 的样式由 status 决定（既有约定），写接口用 failed 的红色系
                          让它一眼可辨 —— 用户不该误以为写接口也能点。 */}
                      <Badge status={e.writable ? "failed" : undefined}>{e.method}</Badge>
                      <span className="min-w-0 flex-1 truncate font-mono text-[12.5px] text-ink-900">
                        {e.path}
                      </span>
                      {Object.keys(e.query).length > 0 && (
                        <span className="hidden shrink-0 font-mono text-[11px] text-ink-400 sm:inline">
                          ?{Object.keys(e.query).join(",")}
                        </span>
                      )}
                      <span className="shrink-0 text-[11px] text-ink-400">×{e.hits}</span>
                    </button>
                    {e.writable ? (
                      <span
                        className="shrink-0 text-[11px] text-[var(--warn-fg)]"
                        title={t("Write endpoints can be observed but never called by tests.")}
                      >
                        {t("view only")}
                      </span>
                    ) : (
                      <Button
                        variant="outline"
                        size="sm"
                        className="h-7 shrink-0"
                        disabled={pr === "loading"}
                        onClick={() => run(e)}
                      >
                        {pr === "loading" ? (
                          <Loader2 className="h-3 w-3 animate-spin" />
                        ) : (
                          t("Read data")
                        )}
                      </Button>
                    )}
                  </div>

                  {isOpen && (
                    <div className="border-t border-[var(--line)] px-3 py-2 text-[12px]">
                      {e.sample && (
                        <div className="mb-1.5 break-all font-mono text-[11px] text-ink-500">
                          {t("sample")}: {e.sample}
                        </div>
                      )}
                      {Object.keys(e.query).length > 0 && (
                        <div className="mb-1.5 text-ink-600">
                          {t("params")}:{" "}
                          {Object.entries(e.query).map(([k, shape]) => (
                            <span key={k} className="mr-2 font-mono text-[11px]">
                              {k}:<span className="text-ink-400">{shape}</span>
                            </span>
                          ))}
                          <div className="mt-0.5 text-[11px] text-ink-400">
                            {t("Only the shape is stored — never the value.")}
                          </div>
                        </div>
                      )}
                      {e.body_fields.length > 0 && (
                        <div className="mb-1.5 text-ink-600">
                          {t("body fields")}: {e.body_fields.join(", ")}
                        </div>
                      )}
                      {e.writable && (
                        <div className="tp-alert tp-alert-warn mt-1.5 flex items-start gap-2 text-[11.5px]">
                          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                          <span>
                            {t("This is a write endpoint. Exploration is read-only — tests must change data through the UI, not by calling this.")}
                          </span>
                        </div>
                      )}
                    </div>
                  )}

                  {pr && pr !== "loading" && (
                    <div className="border-t border-[var(--line)] px-3 py-2">
                      <div className="mb-1 flex items-center gap-2 text-[11.5px]">
                        <Badge status={pr.ok ? "passed" : "failed"}>
                          {pr.status || (pr.ok ? "OK" : t("failed"))}
                        </Badge>
                        <span className="text-ink-500">{pr.elapsed_ms}ms</span>
                        {pr.error && <span className="text-[var(--bad-fg)]">{pr.error}</span>}
                      </div>
                      {pr.url && (
                        <div className="mb-1 break-all font-mono text-[11px] text-ink-400">
                          {pr.url}
                        </div>
                      )}
                      {pr.text && (
                        <pre className="max-h-64 overflow-auto rounded bg-[var(--panel2)] p-2 font-mono text-[11px] leading-relaxed text-ink-700">
                          {pr.json ? JSON.stringify(pr.json, null, 2) : pr.text}
                        </pre>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
          {all.length > ENDPOINT_PREVIEW && (
            <button
              onClick={() => setShowAll((v) => !v)}
              className="mt-2 w-full rounded-lg border border-dashed border-[var(--line-strong)] py-1.5 text-[12px] text-ink-500 outline-none transition-colors hover:border-brand-200 hover:text-brand-700"
            >
              {showAll ? t("Show fewer") : t("Show all {{n}}", { n: all.length })}
            </button>
          )}
        </>
      )}
    </Card>
  );
}
