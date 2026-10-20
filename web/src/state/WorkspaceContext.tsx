import { createContext, useContext, useRef, useState, type MutableRefObject, type ReactNode } from "react";

/** 编辑器当前选区。`text` 是纯文本（ProseMirror textBetween），用于给 AI 当上下文。 */
export type SelectionInfo = { from: number; to: number; text: string; empty: boolean };

export const EMPTY_SELECTION: SelectionInfo = { from: 0, to: 0, text: "", empty: true };

export type WorkspaceDocMeta = { id: number; kind: string; title: string } | null;

export interface WorkspaceApi {
  /** 给 AI 侧栏取实时上下文。内容/选区都从编辑器现取，避免逐键同步整个文档。 */
  getContext: () => { doc: WorkspaceDocMeta; html: string; selection: SelectionInfo };
  /** 用 AI 建议替换左侧文档正文。 */
  replaceContent: (html: string) => void;
  /** 把建议插到最近一次光标处；流程图等没有富文本编辑器的文档返回 false。 */
  insertContent: (html: string) => boolean;
}

interface WorkspaceValue {
  doc: WorkspaceDocMeta;
  selection: SelectionInfo;
  streaming: boolean;
  setDoc: (d: WorkspaceDocMeta) => void;
  setSelection: (s: SelectionInfo) => void;
  setStreaming: (b: boolean) => void;
  /** DocsPage 注册的实时操作接口。 */
  apiRef: MutableRefObject<WorkspaceApi | null>;
  /** AI 侧栏注册的「流式重绘流程图」入口，供文档工具栏复用。 */
  flowTriggerRef: MutableRefObject<((instruction?: string) => void) | null>;
}

const Ctx = createContext<WorkspaceValue | null>(null);

export function WorkspaceProvider({ children }: { children: ReactNode }) {
  const [doc, setDoc] = useState<WorkspaceDocMeta>(null);
  const [selection, setSelection] = useState<SelectionInfo>(EMPTY_SELECTION);
  const [streaming, setStreaming] = useState(false);
  const apiRef = useRef<WorkspaceApi | null>(null);
  const flowTriggerRef = useRef<((instruction?: string) => void) | null>(null);

  return (
    <Ctx.Provider value={{ doc, selection, streaming, setDoc, setSelection, setStreaming, apiRef, flowTriggerRef }}>
      {children}
    </Ctx.Provider>
  );
}

export function useWorkspace(): WorkspaceValue {
  const v = useContext(Ctx);
  if (!v) throw new Error("useWorkspace must be used inside WorkspaceProvider");
  return v;
}
