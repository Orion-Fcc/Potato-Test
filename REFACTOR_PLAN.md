# 前端重构方案（REFACTOR_PLAN）

> 生成日期：2026-10-21
> 用途：上下文压缩或换会话后，读取本文件即可恢复完整重构方案并继续执行。
> 状态：方案已批准，等待执行 Step 1。

---

## 0. 总体目标

把「项目工作台」从“页面各自持有状态 + 可被拖没的右侧栏 + 下拉选文档”重构为：

1. **Zustand 全局状态**：`currentDocId`、`editorContent`、`selectedText`、`isAiPanelOpen` 等跨组件状态集中管理，消除 Context 层层透传与无谓重渲染。
2. **react-resizable-panels 分屏**：左侧文档区 + 右侧 AI 面板。右侧**强制** `minSize="20%"`、`maxSize="50%"`，任何情况下不允许被拖到消失。
3. **删除“助手”独立路由/菜单**：左侧导航只保留【项目工作台】，AI 能力内嵌在右侧面板。
4. **顶栏 Tabs**：删除文档下拉框，用 shadcn/ui 风格 Tabs 渲染水平文档标签页。
5. **TipTap 双向绑定**：`onUpdate` / `onSelectionUpdate` 同步 Store；AI 流式输出走 SSE，插入走 TipTap 官方 API（`insertContent` / `setContent`），禁止 `innerText`、`dangerouslySetInnerHTML`。

---

## 1. 痛点核对（方案提出时 vs 当前代码）

| # | 方案描述的痛点 | 当前实际状态 | 结论 |
|---|---|---|---|
| 1 | “助手”是独立路由，切走会丢上下文 | 已合并：`Sidebar` 仅保留 `docs`（Project Workspace），`/assistant` 路由 `Navigate` 到 `../docs` | 已完成，无需重复改 |
| 2 | 右侧 AI 面板可被拖到 0，无法恢复 | `WorkspacePage.tsx` 仍使用 `collapsible collapsedSize="0%"` | **待修（核心）** |
| 3 | 状态用 Context 透传，流式输出导致整树重渲染 | 仍是 `WorkspaceContext.tsx` | **待修（核心）** |
| 4 | 文档切换是下拉框，缺少 Tab 体验 | 上一轮按要求改成了 Radix `Select` | **待改回 Tabs** |
| 5 | AI 输出非流式 / 插入破坏格式 | 已是 SSE + 50ms 合批；插入已走 `apiRef.insertContent` / `replaceContent` | 基本完成，重构后保持 |
| 6 | TipTap 未与全局状态同步 | `RichTextEditor` 已有 `onUpdate` 与 `selectionUpdate`，但结果只回调页面局部 state | **待接入 Store** |

---

## 2. 依赖清单（npm 实查版本）

> 以下版本通过 `npm view <pkg> version` 核实，不采用方案里可能过时的写法。

### 2.1 升级

| 包 | 当前 | 目标 | 说明 |
|---|---|---|---|
| react / react-dom | 18.3.1 | 19.3.1 | 大版本，需 `@types/react` 19 |
| vite | 5.4.8 | 8.3.4 | 大版本，需 Node 20.19+ / 22.12+（当前 Node 24.19.0 满足） |
| typescript | 5.6.2 | 7.0.2 | 大版本，先确认 `tsc -b` 行为 |
| tailwindcss | 3.4.13 | 4.3.3 | 大版本，配置方式变化 |
| @vitejs/plugin-react | 4.x | latest | 与 Vite 8 匹配 |
| @tiptap/* | 3.31.4 | 3.32.0 | 小版本 |

### 2.2 新增

| 包 | 版本 | 用途 |
|---|---|---|
| zustand | 5.0.15 | 全局 Store |
| @radix-ui/react-tabs | 1.2.1 | Tabs 无头组件 |
| class-variance-authority | latest | shadcn/ui 风格组件变体 |
| tailwind-merge | latest | Tabs 的 `cn()` 合并 |
| clsx | latest（若未安装） | `cn()` 基础 |
| @tailwindcss/vite | 4.3.3 | Tailwind 4 的 Vite 插件 |

### 2.3 已存在、无需新增

- `react-resizable-panels` 4.14.x：已是 v4 API（`Group` / `Panel` / `Separator`）。
- `@radix-ui/react-select` 2.3.7：本轮 Tabs 改造后仍保留给其他页面使用。
- `lucide-react`、`i18next`、`mermaid`。

### 2.4 shadcn/ui 说明

shadcn/ui **不是运行时依赖**，npm 上的 `shadcn` 是 CLI（latest 4.22.1）。本项目直接落地其组件源码（Radix + CVA + `cn()`），不引入 CLI 生成流程，避免覆盖现有主题。

---

## 3. Zustand Store 设计

文件：`web/src/state/workspaceStore.ts`

```ts
import type { Editor } from "@tiptap/core";
import { create } from "zustand";

export type SelectionInfo = {
  from: number;
  to: number;
  text: string;
  empty: boolean;
};

export const EMPTY_SELECTION: SelectionInfo = {
  from: 0,
  to: 0,
  text: "",
  empty: true,
};

export type WorkspaceDocMeta = { id: number; kind: string; title: string } | null;

type WorkspaceState = {
  currentDocId: string | null;
  currentDoc: WorkspaceDocMeta;
  editorContent: string;
  selectedText: string;
  selection: SelectionInfo;
  isAiPanelOpen: boolean;
  editor: Editor | null;
  streaming: boolean;
  flowTrigger: ((instruction?: string) => void) | null;

  setCurrentDoc(doc: WorkspaceDocMeta): void;
  setEditor(editor: Editor | null): void;
  setEditorContent(html: string): void;
  setSelection(sel: SelectionInfo): void;
  setSelectedText(text: string): void;
  setAiPanelOpen(open: boolean): void;
  setStreaming(value: boolean): void;
  setFlowTrigger(fn: ((instruction?: string) => void) | null): void;

  /** 用 TipTap API 整体替换正文（AI 覆盖生成）。 */
  replaceContent(html: string): void;
  /** 在光标/选区处插入 HTML（AI 追加）。 */
  insertContent(html: string): boolean;
};

export const useWorkspaceStore = create<WorkspaceState>((set, get) => ({
  // ...初始值
}));
```

设计要点：

- Store 持有 TipTap `Editor` 实例，`replaceContent` / `insertContent` 内部用 `editor.chain().focus()`，组件不再经 `apiRef` 转发。
- AI 侧读取上下文用 `useWorkspaceStore.getState()`，避免每个 token 触发整棵组件树重渲染。
- `editorContent` 只在 `onUpdate` 时写入；`RichTextEditor` 回写内容前比较 `editor.getHTML() !== value`，防止光标重置。
- `selectedText` 由 `onSelectionUpdate` 用 `editor.state.doc.textBetween(from, to, "\n")` 计算；同时保存 `selection` 供定位。
- `isAiPanelOpen` 与 panels 布局联动（折叠按钮 / 拖拽到达下限）。

---

## 4. 文件修改清单

| 文件 | 动作 | 内容 |
|---|---|---|
| `web/package.json` | 修改 | 升级 React 19、Vite 8、TS 7、Tailwind 4；新增 zustand、@radix-ui/react-tabs、cva、tailwind-merge、@tailwindcss/vite |
| `web/vite.config.ts` | 修改 | `plugins: [react(), tailwindcss()]`；保留 proxy 与 manualChunks |
| `web/postcss.config.js` | 修改 | Tailwind 4 使用 `@tailwindcss/postcss`，或删除该文件改用 Vite 插件 |
| `web/tailwind.config.js` | 修改 | 保留 `content` 与主题扩展；Tailwind 4 通过 `@config` 引用 |
| `web/src/index.css` | 修改 | `@tailwind base/components/utilities` → `@import "tailwindcss";` + `@config "../tailwind.config.js";` |
| `web/src/state/workspaceStore.ts` | **新建** | 上述 Zustand Store |
| `web/src/state/WorkspaceContext.tsx` | **删除** | 由 Store 取代；确认无剩余引用后删除 |
| `web/src/pages/WorkspacePage.tsx` | 修改 | 删除 `collapsible`、`collapsedSize="0%"`；右侧 `minSize="20%" maxSize="50%"`；折叠改为显式按钮控制 `isAiPanelOpen` |
| `web/src/components/workspace/AssistantSidebar.tsx` | 修改 | `useWorkspace()` → `useWorkspaceStore`；`apiRef` → Store 方法；流式与插入逻辑保持 |
| `web/src/components/RichTextEditor.tsx` | 修改 | `onReady` 写入 Store；`onUpdate` / `onSelectionUpdate` 同步 Store；保留防光标重置逻辑 |
| `web/src/pages/DocsPage.tsx` | 修改 | Radix `Select` → Tabs；文档切换写 `currentDocId` |
| `web/src/components/ui/tabs.tsx` | **新建** | shadcn/ui 风格 Tabs（Radix Tabs + CVA + `cn()`） |
| `web/src/main.tsx` | 检查 | `/assistant` 已重定向到 `/docs`，无需再改 |
| `web/src/components/Sidebar.tsx` | 检查 | 已只剩【项目工作台】，无需再改 |

---

## 5. Step 1 执行顺序

1. 修改依赖与构建配置（`package.json`、`vite.config.ts`、`postcss.config.js`、`tailwind.config.js`、`index.css`）。
2. 执行 `cd web && npm install`。
3. 立即验证：`npx tsc -b` + `npm run build`。
   - 若出现 peer 依赖冲突或大面积构建错误：**止损**，回滚 React 18 + Tailwind 3，保留其余功能重构。
4. 新建 `workspaceStore.ts`。
5. 改造 `WorkspacePage.tsx`：删除 `collapsible` / `collapsedSize="0%"`，写死 `minSize="20%"`、`maxSize="50%"`。
6. 改造 `RichTextEditor.tsx`、`AssistantSidebar.tsx`、`DocsPage.tsx` 接入 Store。
7. 新建 `components/ui/tabs.tsx`，`DocsPage.tsx` 下拉换 Tabs。
8. 删除 `WorkspaceContext.tsx` 及其引用。
9. 再次 `npx tsc -b` + `npm run build`。

---

## 6. 验收标准

- [ ] 右侧 AI 面板最小 20%、最大 50%，无法被拖到 0，刷新后布局保持。
- [ ] 左侧导航仅有【项目工作台】；访问 `/assistant` 自动跳回 `/docs`。
- [ ] 顶栏为水平 Tabs，可切换文档，文档多时可横向滚动。
- [ ] 编辑正文后 `editorContent` 实时更新；选中文本后 `selectedText` 正确。
- [ ] AI 流式输出可见打字机效果；插入/覆盖不破坏 TipTap 文档结构与格式。
- [ ] `npx tsc -b` 与 `npm run build` 均通过。
- [ ] 无残留 `useWorkspace` / `WorkspaceProvider` 引用。

---

## 7. 风险与回滚

| 风险 | 处理 |
|---|---|
| React 19 与现有第三方库 peer 冲突 | 优先 `npm install --legacy-peer-deps` 验证；仍失败则回滚 React 18 |
| Tailwind 4 配置迁移导致样式丢失 | 回滚 Tailwind 3；功能重构不受影响 |
| TS 7 与现有类型不兼容 | 回滚 TS 5.6，保留其余升级 |
| `react-resizable-panels` v4 API 差异 | 当前已是 v4，沿用 `Group` / `Panel` / `Separator` |
| 删除 Context 后遗漏引用 | 构建前全局搜索 `useWorkspace(` / `WorkspaceProvider` |

**回滚点**：`chore: 重构前置备份` 提交。

**止损原则**：依赖大版本升级失败时，功能重构优先，版本升级可以放弃。

Phase 3 已完成，采用 ChatTransport 适配自定义 SSE，替换了手写流解析。
