import { Group, Panel, Separator, useDefaultLayout, usePanelRef } from "react-resizable-panels";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { AssistantSidebar } from "../components/workspace/AssistantSidebar";
import { WorkspaceProvider } from "../state/WorkspaceContext";
import { DocsPage } from "./DocsPage";

/** AI 协同工作区（2026-10-20）。
 *
 *  左边是原来的「项目文档」整页，右边是常驻助手。拆成两栏而不是在 DocsPage
 *  里塞一个 aside，是为了让「文档」和「助手」两个路由复用同一套布局与状态：
 *  /projects/:pid/docs 与 /projects/:pid/assistant 都落到这里。
 *
 *  react-resizable-panels v4 的组件名是 Group / Separator（v3 的
 *  PanelGroup / PanelResizeHandle 已移除），尺寸也改成带单位的字符串，
 *  布局持久化改由 useDefaultLayout 负责。
 */
export function WorkspacePage() {
  const { t } = useTranslation();
  const assistantRef = usePanelRef();
  const [assistantCollapsed, setAssistantCollapsed] = useState(false);
  const { defaultLayout, onLayoutChanged } = useDefaultLayout({
    id: "tp-workspace-split",
    panelIds: ["docs", "assistant"],
  });

  return (
    <WorkspaceProvider>
      <Group
        id="tp-workspace-split"
        orientation="horizontal"
        defaultLayout={defaultLayout}
        onLayoutChanged={onLayoutChanged}
        className="tp-workspace min-h-[calc(100vh-7rem)]"
      >
        <Panel id="docs" defaultSize="65%" minSize="38%" className="min-w-0 pr-1.5">
          <DocsPage />
        </Panel>
        <Separator className="tp-ws-handle group relative w-2 rounded bg-transparent transition-colors hover:bg-brand-200">
          <span className="absolute left-1/2 top-1/2 h-8 w-0.5 -translate-x-1/2 -translate-y-1/2 rounded bg-[var(--line)] group-hover:bg-brand-500" />
          {/* 2026-10-20 用户要求「一键折叠/展开右侧助手」。
              按钮压在分隔条正中；stopPropagation 防止拖拽分隔条时把点击吃掉。 */}
          <button
            type="button"
            onPointerDown={(e) => e.stopPropagation()}
            onClick={() => {
              const panel = assistantRef.current;
              if (!panel) return;
              if (panel.isCollapsed()) panel.expand();
              else panel.collapse();
            }}
            title={assistantCollapsed ? t("展开 AI 助手") : t("收起 AI 助手")}
            aria-label={assistantCollapsed ? t("展开 AI 助手") : t("收起 AI 助手")}
            className="absolute left-1/2 top-1/2 z-20 flex h-8 w-5 -translate-x-1/2 -translate-y-1/2 items-center justify-center rounded-full border border-[var(--line)] bg-[var(--panel)] text-ink-500 shadow-sm outline-none transition-colors hover:border-brand-300 hover:text-brand-700 focus-visible:ring-2 focus-visible:ring-brand-100"
          >
            {assistantCollapsed ? (
              <ChevronLeft className="h-3.5 w-3.5" />
            ) : (
              <ChevronRight className="h-3.5 w-3.5" />
            )}
          </button>
        </Separator>
        <Panel
          id="assistant"
          defaultSize="35%"
          minSize="22%"
          collapsible
          collapsedSize="0%"
          panelRef={assistantRef}
          onResize={(size) => setAssistantCollapsed(size.inPixels < 1)}
          className="min-w-0 pl-1.5"
        >
          <AssistantSidebar />
        </Panel>
      </Group>
    </WorkspaceProvider>
  );
}
