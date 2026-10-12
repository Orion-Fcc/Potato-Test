import { EditorContent, useEditor, type Editor } from "@tiptap/react";
import { Extension, type CommandProps } from "@tiptap/core";
import Color from "@tiptap/extension-color";
import Highlight from "@tiptap/extension-highlight";
import Image from "@tiptap/extension-image";
import Link from "@tiptap/extension-link";
import { Table, TableCell, TableHeader, TableRow } from "@tiptap/extension-table";
import TextAlign from "@tiptap/extension-text-align";
import { TextStyle } from "@tiptap/extension-text-style";
import Underline from "@tiptap/extension-underline";
import StarterKit from "@tiptap/starter-kit";
import {
  AlignCenter,
  AlignLeft,
  AlignRight,
  Bold,
  Code,
  Heading2,
  Heading3,
  Highlighter,
  ImagePlus,
  Italic,
  Link2,
  List,
  ListOrdered,
  Minus,
  Quote,
  Redo2,
  Strikethrough,
  Table as TableIcon,
  Underline as UnderlineIcon,
  Undo2,
} from "lucide-react";
import { useCallback, useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";
import { useErrorToast } from "./toast";

// 把自定义命令补进 TipTap 的类型里，这样 `editor.chain().setFontSize(...)` 有类型检查。
// 不做这一步的话调用处只能 `as any`，而 `as any` 会把"命令名打错"这类错误放到运行时。
declare module "@tiptap/core" {
  interface Commands<ReturnType> {
    fontSize: {
      setFontSize: (size: string) => ReturnType;
      unsetFontSize: () => ReturnType;
    };
  }
}

/** 字号扩展（2026-10-12）。
 *
 *  TipTap 的 `TextStyle` 只带 color/fontFamily 一类通用 style，**没有 fontSize** ——
 *  字号的取值空间太自由，它留给使用者自己定义。用户明确要求"字体大小"，
 *  所以这里加一个小扩展：给 `textStyle` mark 多认一个 `fontSize` 属性，
 *  渲染成 `<span style="font-size: 18px">`。
 *
 *  为什么用 `<span style>` 而不是 class：正文要能原样存进 HTML 再读回来，
 *  而 class 需要配套的样式表 —— 那会让"导出/迁移文档"多一份依赖。
 */
const FontSize = Extension.create({
  name: "fontSize",
  addGlobalAttributes() {
    return [
      {
        types: ["textStyle"],
        attributes: {
          fontSize: {
            default: null,
            parseHTML: (el) => el.style.fontSize || null,
            renderHTML: (attrs) => (attrs.fontSize ? { style: `font-size: ${attrs.fontSize}` } : {}),
          },
        },
      },
    ];
  },
  addCommands() {
    return {
      // 返回类型必须是 `Command`（`({chain}) => chain()…run()`），
      // 而不是 `RawCommands["setMark"]` —— 后者是**命令本身**，签名完全不同。
      setFontSize:
        (size: string) =>
        ({ chain }: CommandProps) =>
          chain().setMark("textStyle", { fontSize: size }).run(),
      unsetFontSize:
        () =>
        ({ chain }: CommandProps) =>
          chain().setMark("textStyle", { fontSize: null }).removeEmptyTextStyle().run(),
    };
  },
});

/** 富文本编辑器（2026-10-12）。
 *
 *  ## 为什么用 TipTap
 *
 *  用户要求「页面有很多格式可以编辑，包括字体，照片，字体颜色大小等等」。
 *  手搓 contenteditable 的富文本是个无底洞（选区、粘贴、撤销栈、表格全都是坑），
 *  而 TipTap 是 ProseMirror 上的成熟封装，产出的就是 HTML —— 正好是我们要存的格式。
 *
 *  ## 字号与字体为什么是 <select> 而不是下拉菜单
 *
 *  它们套在 `<span style="...">` 上（TextStyle 扩展）。看起来"原始"，但这是**故意的**：
 *  富文本编辑器里字号/字体的取值本来就是有限枚举，用原生 select 反而键盘可达、
 *  移动端可用，而且不用再引入一个弹出层组件。
 */
export function RichTextEditor({
  value,
  onChange,
  onUploadImage,
  placeholder,
  editable = true,
}: {
  value: string;
  onChange: (html: string) => void;
  /** 上传图片，返回 URL。不传则不显示"插入图片"按钮。 */
  onUploadImage?: (file: File) => Promise<string>;
  placeholder?: string;
  /** false = 只读（2026-10-12）。用在"只有我能改"以外的场景；
   *  AI 只读这件事本身仍由服务端拦，这里只是不让人误以为还能编辑。 */
  editable?: boolean;
}) {
  const { t } = useTranslation();
  const fail = useErrorToast();
  const fileRef = useRef<HTMLInputElement>(null);

  const editor = useEditor({
    extensions: [
      StarterKit.configure({
        heading: { levels: [1, 2, 3, 4] },
        // 代码块在需求文档里几乎用不到，而它会吃掉 Tab/回车，容易让人卡住。
        codeBlock: false,
      }),
      FontSize,
      Underline,
      TextStyle,
      Color,
      Highlight.configure({ multicolor: true }),
      TextAlign.configure({ types: ["heading", "paragraph"] }),
      Link.configure({ openOnClick: false, autolink: true }),
      Image.configure({ inline: false, allowBase64: true }),
      Table.configure({ resizable: false }),
      TableRow,
      TableHeader,
      TableCell,
    ],
    content: value || "",
    // 只读时 TipTap 会自己把 contenteditable 关掉，不必手动改 DOM。
    editable,
    editorProps: {
      attributes: {
        // 富文本编辑区的最小高度与排版。用 class 而不是内联样式，
        // 这样它和只读渲染共用同一套 `.tp-prose` 排版（见 index.css）。
        class: "tp-prose min-h-[420px] px-4 py-3 outline-none",
        "data-placeholder": placeholder ?? "",
      },
    },
    onUpdate: ({ editor: e }) => onChange(e.getHTML()),
  });

  const pickImage = useCallback(() => fileRef.current?.click(), []);

  // ★ 外部内容变了要灌进编辑器（2026-10-12 修，用户报"没正常生成"）。
  //
  // `useEditor({content})` 只在**创建时**读一次。而这个组件的 value 是异步来的：
  //   * 打开页面 → 文档还在请求中，先渲染空编辑器 → 内容到了却没人告诉编辑器；
  //   * 点「AI 分析与生成」→ 生成完 setHtml(doc.html) 同样进不去。
  // 结果就是：库里明明有 24033 字符（12 个 h2、30 个 h3、4 张表），
  // 编辑区却一片空白 —— 用户看到的就是"没生成出来"。
  //
  // 两个守卫都必须有：
  //   1. `editor.getHTML() !== value` —— 不判的话，用户每敲一个字都会触发
  //      setContent，光标被重置到开头，根本没法打字（onUpdate → onChange →
  //      value 变 → effect → setContent 的死循环）。
  //   2. `emitUpdate: false` —— 程序性灌入不该反过来触发 onChange，
  //      否则会把"外部内容"当成"用户编辑"，把 dirty 标记弄脏。
  useEffect(() => {
    if (!editor) return;
    const current = editor.getHTML();
    if (current === value) return;
    // 只归一化空白后比较，避免"仅空格差异"导致无意义的重设
    if (current.replace(/\s+/g, " ") === (value || "").replace(/\s+/g, " ")) return;
    editor.commands.setContent(value || "", { emitUpdate: false });
    // 依赖里只放 value 与 editor：onChange 是父组件每次渲染新建的函数，
    // 放进来会让这个 effect 每渲染都跑一遍。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editor, value]);

  // ★ `editable` 变了要同步到已存在的编辑器（2026-10-12）。
  //
  // 和 content 一样，`useEditor({editable})` 只在创建时读一次。而 editable 会**变**：
  // 用户在「项目文档」页勾上/取消「只有我能改」时，编辑器必须跟着切成只读/可写，
  // 否则界面上的状态和实际能不能打字是两回事。
  useEffect(() => {
    if (!editor) return;
    if (editor.isEditable !== editable) editor.setEditable(editable);
  }, [editor, editable]);

  const insertImage = useCallback(
    async (file: File) => {
      if (!onUploadImage || !editor) return;
      try {
        const url = await onUploadImage(file);
        editor.chain().focus().setImage({ src: url, alt: file.name }).run();
      } catch (e) {
        fail(e);
      }
    },
    [editor, onUploadImage, fail],
  );

  if (!editor) return null;

  return (
    <div className="rounded-lg border border-[var(--line)] bg-[var(--panel)]">
      <Toolbar editor={editor} onPickImage={onUploadImage ? pickImage : undefined} />
      <input
        ref={fileRef}
        type="file"
        accept="image/png,image/jpeg,image/gif,image/webp,image/bmp"
        className="hidden"
        onChange={(e) => {
          const f = e.target.files?.[0];
          e.target.value = ""; // 同一张图再选一次也要能触发
          if (f) insertImage(f);
        }}
      />
      <div className="max-h-[62vh] overflow-y-auto border-t border-[var(--line)]">
        <EditorContent editor={editor} />
      </div>
      <div className="flex flex-wrap items-center gap-2 border-t border-[var(--line)] px-3 py-1.5 text-[11px] text-ink-400">
        <span>{t("Tip: paste from Word/网页会尽量保留格式；脚本等危险内容会被自动去掉。")}</span>
      </div>
    </div>
  );
}

/** 工具栏。抽出来是为了让主组件短一点，也便于以后单独调。 */
function Toolbar({
  editor,
  onPickImage,
}: {
  editor: Editor;
  onPickImage?: () => void;
}) {
  const { t } = useTranslation();
  const btn =
    "inline-flex h-7 min-w-7 items-center justify-center rounded px-1.5 text-ink-700 transition-colors hover:bg-[var(--panel2)] disabled:opacity-40";
  const on = "bg-brand-50 text-brand-700";
  const sep = <span className="mx-0.5 h-5 w-px bg-[var(--line)]" />;

  const FONTS = [
    { label: t("默认字体"), v: "" },
    { label: "思源黑体 / 系统", v: "system-ui, 'Microsoft YaHei', sans-serif" },
    { label: "宋体", v: "SimSun, serif" },
    { label: "黑体", v: "'Microsoft YaHei', sans-serif" },
    { label: "楷体", v: "KaiTi, serif" },
    { label: "等宽", v: "ui-monospace, Consolas, monospace" },
  ];
  const SIZES = ["12px", "14px", "16px", "18px", "20px", "24px", "30px"];
  const COLORS = [
    "#111827", "#374151", "#6b7280", "#b91c1c", "#c2410c", "#a16207",
    "#15803d", "#0e7490", "#1d4ed8", "#6d28d9", "#a21caf", "#be185d",
  ];

  // 读当前选区上的字体/字号，让下拉框显示真实值（而不是永远显示"默认"）
  const cur = (editor.getAttributes("textStyle") ?? {}) as Record<string, string>;
  const fmatch = FONTS.find((f) => f.v && (cur.fontFamily || "").includes(f.v.split(",")[0]));
  const smatch = SIZES.find((s) => (cur.fontSize || "") === s);

  return (
    <div className="flex flex-wrap items-center gap-0.5 px-2 py-1.5">
      <button className={btn} title={t("撤销")} onClick={() => editor.chain().focus().undo().run()} disabled={!editor.can().undo()}>
        <Undo2 className="h-4 w-4" />
      </button>
      <button className={btn} title={t("重做")} onClick={() => editor.chain().focus().redo().run()} disabled={!editor.can().redo()}>
        <Redo2 className="h-4 w-4" />
      </button>
      {sep}

      {/* 字体 / 字号 / 颜色 */}
      <select
        className="h-7 rounded border border-[var(--line)] bg-[var(--panel)] px-1 text-[11px] text-ink-700"
        title={t("字体")}
        value={fmatch?.v ?? ""}
        onChange={(e) => {
          const v = e.target.value;
          if (!v) editor.chain().focus().unsetFontFamily?.().run();
          else editor.chain().focus().setFontFamily(v).run();
        }}
      >
        {FONTS.map((f) => (
          <option key={f.label} value={f.v}>
            {f.label}
          </option>
        ))}
      </select>
      <select
        className="h-7 rounded border border-[var(--line)] bg-[var(--panel)] px-1 text-[11px] text-ink-700"
        title={t("字号")}
        value={smatch ?? ""}
        onChange={(e) => {
          const v = e.target.value;
          if (!v) editor.chain().focus().unsetFontSize().run();
          else editor.chain().focus().setFontSize(v).run();
        }}
      >
        <option value="">{t("字号")}</option>
        {SIZES.map((s) => (
          <option key={s} value={s}>
            {s.replace("px", "")}
          </option>
        ))}
      </select>
      <label className="inline-flex h-7 cursor-pointer items-center gap-1 rounded px-1.5 hover:bg-[var(--panel2)]" title={t("文字颜色")}>
        <span className="h-3.5 w-3.5 rounded-sm border border-[var(--line)]" style={{ background: cur.color || "#111827" }} />
        <input
          type="color"
          className="h-0 w-0 opacity-0"
          value={cur.color || "#111827"}
          onChange={(e) => editor.chain().focus().setColor(e.target.value).run()}
        />
      </label>
      <select
        className="h-7 w-8 rounded border border-[var(--line)] bg-[var(--panel)] px-0 text-[11px]"
        title={t("常用颜色")}
        value=""
        onChange={(e) => e.target.value && editor.chain().focus().setColor(e.target.value).run()}
      >
        <option value="">▾</option>
        {COLORS.map((c) => (
          <option key={c} value={c}>
            {c}
          </option>
        ))}
      </select>
      {sep}

      <button className={btn + (editor.isActive("bold") ? " " + on : "")} title={t("加粗")} onClick={() => editor.chain().focus().toggleBold().run()}>
        <Bold className="h-4 w-4" />
      </button>
      <button className={btn + (editor.isActive("italic") ? " " + on : "")} title={t("斜体")} onClick={() => editor.chain().focus().toggleItalic().run()}>
        <Italic className="h-4 w-4" />
      </button>
      <button className={btn + (editor.isActive("underline") ? " " + on : "")} title={t("下划线")} onClick={() => editor.chain().focus().toggleUnderline().run()}>
        <UnderlineIcon className="h-4 w-4" />
      </button>
      <button className={btn + (editor.isActive("strike") ? " " + on : "")} title={t("删除线")} onClick={() => editor.chain().focus().toggleStrike().run()}>
        <Strikethrough className="h-4 w-4" />
      </button>
      <button className={btn + (editor.isActive("highlight") ? " " + on : "")} title={t("高亮")} onClick={() => editor.chain().focus().toggleHighlight().run()}>
        <Highlighter className="h-4 w-4" />
      </button>
      <button className={btn + (editor.isActive("code") ? " " + on : "")} title={t("行内代码")} onClick={() => editor.chain().focus().toggleCode().run()}>
        <Code className="h-4 w-4" />
      </button>
      {sep}

      <button className={btn + (editor.isActive("heading", { level: 2 }) ? " " + on : "")} title={t("标题")} onClick={() => editor.chain().focus().toggleHeading({ level: 2 }).run()}>
        <Heading2 className="h-4 w-4" />
      </button>
      <button className={btn + (editor.isActive("heading", { level: 3 }) ? " " + on : "")} title={t("小标题")} onClick={() => editor.chain().focus().toggleHeading({ level: 3 }).run()}>
        <Heading3 className="h-4 w-4" />
      </button>
      <button className={btn + (editor.isActive("bulletList") ? " " + on : "")} title={t("无序列表")} onClick={() => editor.chain().focus().toggleBulletList().run()}>
        <List className="h-4 w-4" />
      </button>
      <button className={btn + (editor.isActive("orderedList") ? " " + on : "")} title={t("有序列表")} onClick={() => editor.chain().focus().toggleOrderedList().run()}>
        <ListOrdered className="h-4 w-4" />
      </button>
      <button className={btn + (editor.isActive("blockquote") ? " " + on : "")} title={t("引用")} onClick={() => editor.chain().focus().toggleBlockquote().run()}>
        <Quote className="h-4 w-4" />
      </button>
      <button className={btn} title={t("分割线")} onClick={() => editor.chain().focus().setHorizontalRule().run()}>
        <Minus className="h-4 w-4" />
      </button>
      {sep}

      <button className={btn + (editor.isActive({ textAlign: "left" }) ? " " + on : "")} title={t("左对齐")} onClick={() => editor.chain().focus().setTextAlign("left").run()}>
        <AlignLeft className="h-4 w-4" />
      </button>
      <button className={btn + (editor.isActive({ textAlign: "center" }) ? " " + on : "")} title={t("居中")} onClick={() => editor.chain().focus().setTextAlign("center").run()}>
        <AlignCenter className="h-4 w-4" />
      </button>
      <button className={btn + (editor.isActive({ textAlign: "right" }) ? " " + on : "")} title={t("右对齐")} onClick={() => editor.chain().focus().setTextAlign("right").run()}>
        <AlignRight className="h-4 w-4" />
      </button>
      {sep}

      <button className={btn} title={t("插入表格")} onClick={() => editor.chain().focus().insertTable({ rows: 3, cols: 3, withHeaderRow: true }).run()}>
        <TableIcon className="h-4 w-4" />
      </button>
      <button
        className={btn}
        title={t("插入链接")}
        onClick={() => {
          const prev = editor.getAttributes("link").href as string | undefined;
          const url = window.prompt(t("链接地址"), prev ?? "https://");
          if (url === null) return;
          if (!url.trim()) editor.chain().focus().unsetLink().run();
          else editor.chain().focus().setLink({ href: url.trim() }).run();
        }}
      >
        <Link2 className="h-4 w-4" />
      </button>
      {onPickImage && (
        <button className={btn} title={t("插入图片")} onClick={onPickImage}>
          <ImagePlus className="h-4 w-4" />
        </button>
      )}
    </div>
  );
}
