import type { Editor } from "@tiptap/react";
import { create } from "zustand";

/** 编辑器当前选区。`text` 是纯文本（ProseMirror textBetween），用于给 AI 当上下文。 */
export type SelectionInfo = { from: number; to: number; text: string; empty: boolean };

export const EMPTY_SELECTION: SelectionInfo = { from: 0, to: 0, text: "", empty: true };

export type WorkspaceDocMeta = { id: number; kind: string; title: string } | null;

/** 侧栏写入正文后的一次性信号。DocsPage 用它刷新本地 html 并标记未保存。 */
export type ContentWrite = { html: string; seq: number } | null;

interface WorkspaceState {
  /** 当前文档元信息。AI 侧栏只订阅这个字段，正文变化不会带动它重渲染。 */
  currentDoc: WorkspaceDocMeta;
  /** 正文快照。仅用于 getContext / 整体替换，订阅它的组件只应是自己需要全文的组件。 */
  editorContent: string;
  /** 编辑器实时选区（UI 展示用）。 */
  selection: SelectionInfo;
  /** 最后一次**非空**选区，发给 AI 作为重点上下文。 */
  lastSelection: SelectionInfo;
  editor: Editor | null;
  streaming: boolean;
  isAiPanelOpen: boolean;
  /** AI 侧栏注册的「流式重绘流程图」入口，供文档工具栏复用。 */
  flowTrigger: ((instruction?: string) => void) | null;
  contentWrite: ContentWrite;

  setCurrentDoc: (doc: WorkspaceDocMeta) => void;
  setEditorContent: (html: string) => void;
  setSelection: (sel: SelectionInfo) => void;
  resetSelection: () => void;
  setEditor: (editor: Editor | null) => void;
  setStreaming: (streaming: boolean) => void;
  setAiPanelOpen: (open: boolean) => void;
  setFlowTrigger: (trigger: ((instruction?: string) => void) | null) => void;

  /** 给 AI 侧栏取实时上下文。正文/选区都从 store 现取，避免逐键同步整个文档。 */
  getContext: () => { doc: WorkspaceDocMeta; html: string; selection: SelectionInfo };
  /** 用 AI 建议替换左侧文档正文。 */
  replaceContent: (html: string) => void;
  /** 把建议插到编辑器实时光标处；流程图等没有富文本编辑器的文档返回 false。 */
  insertContent: (html: string) => boolean;
}

let writeSeq = 0;

function nextWrite(html: string): ContentWrite {
  return { html, seq: ++writeSeq };
}

export const useWorkspaceStore = create<WorkspaceState>((set, get) => ({
  currentDoc: null,
  editorContent: "",
  selection: EMPTY_SELECTION,
  lastSelection: EMPTY_SELECTION,
  editor: null,
  streaming: false,
  isAiPanelOpen: true,
  flowTrigger: null,
  contentWrite: null,

  setCurrentDoc: (doc) => set({ currentDoc: doc }),
  setEditorContent: (html) => set({ editorContent: html }),
  setSelection: (sel) =>
    set((state) => ({
      selection: sel,
      // 拖拽/键入过程中会有空选区；保留最后一次非空选区，
      // 这样用户选中一段、再点侧栏输入框时 AI 仍知道他说的是哪段。
      lastSelection: !sel.empty && sel.text.trim() ? sel : state.lastSelection,
    })),
  resetSelection: () => set({ selection: EMPTY_SELECTION, lastSelection: EMPTY_SELECTION }),
  setEditor: (editor) => set({ editor }),
  setStreaming: (streaming) => set({ streaming }),
  setAiPanelOpen: (open) => set({ isAiPanelOpen: open }),
  setFlowTrigger: (trigger) => set({ flowTrigger: trigger }),

  getContext: () => {
    const state = get();
    return {
      doc: state.currentDoc,
      html: state.editorContent,
      selection: state.lastSelection,
    };
  },

  replaceContent: (html) => {
    const editor = get().editor;
    if (editor) {
      // 用「全选 + 插入」而不是 setContent：前者走正常的 ProseMirror 事务，
      // 整篇替换后仍可 Ctrl+Z 撤销，也不会清空撤销历史。
      const ok = editor.chain().focus().selectAll().insertContent(html).run();
      if (!ok) editor.commands.setContent(html, { emitUpdate: false });
      const next = editor.getHTML();
      set({ editorContent: next, contentWrite: nextWrite(next) });
      return;
    }
    set({ editorContent: html, contentWrite: nextWrite(html) });
  },

  insertContent: (html) => {
    const { editor, currentDoc } = get();
    if (!editor || currentDoc?.kind === "flow") return false;
    // 取编辑器实时选区，而不是缓存的最后选区：用户点侧栏输入框后
    // ProseMirror 的 selection 仍然保留，而缓存可能是旧文档/旧位置。
    const pos = Math.min(editor.state.selection.to, editor.state.doc.content.size);
    // 传 HTML 字符串给 insertContentAt，TipTap 会解析成节点；
    // updateSelection 让光标落到插入内容末尾，用户可以直接接着改。
    const ok = editor.chain().focus().insertContentAt(pos, html, { updateSelection: true }).run();
    if (!ok) return false;
    const next = editor.getHTML();
    set({ editorContent: next, contentWrite: nextWrite(next) });
    return true;
  },
}));
