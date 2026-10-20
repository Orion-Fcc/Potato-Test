import { memo, useMemo } from "react";

import { markdownToHtml } from "../../lib/markdown";

/** 流式气泡和确认卡片共用的 Markdown 渲染。memo 只比较 content，
 *  避免输入框等无关状态变化时重复解析。 */
export const MarkdownBody = memo(function MarkdownBody({
  content,
  className = "",
}: {
  content: string;
  className?: string;
}) {
  const html = useMemo(() => markdownToHtml(content), [content]);
  return <div className={"tp-md " + className} dangerouslySetInnerHTML={{ __html: html }} />;
});
