import { mergeAttributes, Node, type NodeViewProps } from "@tiptap/core";
import { NodeViewWrapper, ReactNodeViewRenderer } from "@tiptap/react";
import { Check, Pencil, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { MermaidBlock } from "../MermaidBlock";

const MERMAID_HEAD =
  /^\s*(flowchart|graph|sequenceDiagram|classDiagram|stateDiagram(?:-v2)?|erDiagram|gantt|pie|journey|mindmap|timeline)\b/i;

/** 编辑器里的 Mermaid 块：默认显示图，点击切成源码编辑。
 *
 *  序列化仍是 `<pre class="tp-mermaid">源码</pre>`，和后端、导出、AI 建议
 *  使用的格式完全一致 —— NodeView 只改变**编辑时**的呈现，不改存储。
 */
function MermaidNodeView({ node, updateAttributes, editor }: NodeViewProps) {
  const { t } = useTranslation();
  const source = String(node.attrs.source ?? "");
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(source);
  const [preview, setPreview] = useState(source);
  const areaRef = useRef<HTMLTextAreaElement>(null);
  // 进入编辑时的源码。自动写回会改掉 `source`，Esc 想"取消"就必须回到这里，
  // 否则取消后看到的仍是刚敲进去的内容。
  const editStartRef = useRef(source);

  // 外部替换（AI 生成 / 撤销 / 切文档）时同步进来，不能停在旧源码。
  useEffect(() => {
    setDraft(source);
    setPreview(source);
  }, [source]);

  // 编辑时 500ms 防抖预览，和 FlowPane 的手感保持一致。
  useEffect(() => {
    if (!editing) return;
    const timer = setTimeout(() => setPreview(draft), 500);
    return () => clearTimeout(timer);
  }, [draft, editing]);

  useEffect(() => {
    if (editing) areaRef.current?.focus();
  }, [editing]);

  // 边改边存：用户可能改完直接点页面右上角「保存」而不是点「完成」。
  // 只靠按钮提交会让保存按钮读到旧正文。350ms 防抖，足够轻。
  useEffect(() => {
    if (!editing || draft === source) return;
    const timer = setTimeout(() => updateAttributes({ source: draft }), 350);
    return () => clearTimeout(timer);
  }, [draft, editing, source, updateAttributes]);

  const commit = () => {
    updateAttributes({ source: draft.trim() });
    setEditing(false);
  };

  const cancel = () => {
    const original = editStartRef.current;
    // 自动写回可能已经落库；取消时显式改回去，文档才算真的没变。
    if (source !== original) updateAttributes({ source: original });
    setDraft(original);
    setPreview(original);
    setEditing(false);
  };

  const editable = editor.isEditable;

  return (
    <NodeViewWrapper
      className={
        "tp-mermaid-node my-3 rounded-lg border border-[var(--line)] bg-[var(--panel2)] p-2 " +
        (editing ? "border-brand-300" : "")
      }
      data-type="mermaid"
      contentEditable={false}
    >
      {editing ? (
        <div className="space-y-2">
          <div className="flex items-center gap-2 text-[11px] text-ink-500">
            <Pencil className="h-3 w-3" />
            {t("Mermaid 源码")}
            <span className="ml-auto">{t("Ctrl+Enter 完成 · Esc 取消")}</span>
          </div>
          <textarea
            ref={areaRef}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Escape") {
                e.preventDefault();
                cancel();
              } else if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
                e.preventDefault();
                commit();
              }
            }}
            spellCheck={false}
            className="min-h-[220px] w-full resize-y rounded-md border border-[var(--line)] bg-[var(--panel)] px-2.5 py-2 font-mono text-[12px] leading-relaxed text-ink-800 outline-none focus:border-brand-300"
          />
          {preview.trim() && <MermaidBlock source={preview} zoomable={false} />}
          <div className="flex justify-end gap-1.5">
            <button
              type="button"
              className="inline-flex items-center gap-1 rounded-md border border-[var(--line)] px-2 py-1 text-[11px] text-ink-600 hover:bg-[var(--panel2)]"
              onClick={cancel}
            >
              <X className="h-3 w-3" />
              {t("取消")}
            </button>
            <button
              type="button"
              className="inline-flex items-center gap-1 rounded-md bg-brand-600 px-2 py-1 text-[11px] text-white hover:bg-brand-700"
              onClick={commit}
            >
              <Check className="h-3 w-3" />
              {t("完成")}
            </button>
          </div>
        </div>
      ) : (
        <div
          className={editable ? "cursor-text" : ""}
          onClick={() => {
            if (!editable) return;
            editStartRef.current = source;
            setDraft(source);
            setPreview(source);
            setEditing(true);
          }}
          title={editable ? t("点击编辑 Mermaid 源码") : undefined}
        >
          {source.trim() ? (
            <MermaidBlock source={source} zoomable={false} />
          ) : (
            <p className="px-2 py-4 text-center text-[12px] text-ink-400">
              {t("空流程图，点击编辑源码。")}
            </p>
          )}
          {editable && (
            <p className="mt-1 flex items-center justify-end gap-1 text-[10.5px] text-ink-400">
              <Pencil className="h-3 w-3" />
              {t("点击编辑源码")}
            </p>
          )}
        </div>
      )}
    </NodeViewWrapper>
  );
}

export const MermaidNode = Node.create({
  name: "mermaidBlock",
  group: "block",
  atom: true,
  selectable: true,
  draggable: false,
  // 必须高于 CodeBlock 的 `pre` 规则，否则 `<pre class="tp-mermaid">`
  // 会先被普通代码块吃掉，NodeView 永远不出现。
  priority: 1000,

  addAttributes() {
    return {
      source: {
        default: "",
        parseHTML: (el) => (el.textContent || "").replace(/\r\n/g, "\n").trim(),
        renderHTML: () => ({}),
      },
    };
  },

  parseHTML() {
    return [
      { tag: "pre.tp-mermaid" },
      {
        tag: "pre",
        getAttrs: (el) => {
          const text = (el.textContent || "").trim();
          return MERMAID_HEAD.test(text) ? { source: text } : false;
        },
      },
    ];
  },

  renderHTML({ node, HTMLAttributes }) {
    return [
      "pre",
      mergeAttributes(HTMLAttributes, { class: "tp-mermaid" }),
      String(node.attrs.source ?? ""),
    ];
  },

  addNodeView() {
    return ReactNodeViewRenderer(MermaidNodeView);
  },
});
