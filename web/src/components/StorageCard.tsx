import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "../lib/api";
import { Button, Card, Checkbox, Field, Input } from "../components/ui";
import { useErrorToast, useToast } from "../components/toast";

/**
 * 文件存放位置（2026-10-19 用户要求）。
 *
 * 视频、截图、浏览器 profile 默认落在**程序目录旁边**。C 盘紧张、或者想把录像集中到数据盘时，
 * 可以在这里改到任意目录 —— 保存后**重启应用生效**（路径在进程启动时就被解析成绝对路径了，
 * 运行中改它只会让新旧文件散在两个地方）。
 *
 * 几个刻意的选择：
 *  * **不提供文件夹选择对话框**：桌面窗口是 WebView2，原生选目录要走 pywebview 暴露的 API，
 *    那条链路一断就是"点按钮没反应"。一个可粘贴的路径输入框反而更可靠（也能填 UNC 路径）。
 *  * **留空 = 恢复默认**：不需要额外的"重置"按钮，语义也直白。
 *  * **「把现有文件也搬过去」默认不勾**：搬可能要很久（跨盘 = 复制），
 *    不该在用户只想想改个路径时顺手干这么重的事。
 */
type StorageInfo = {
  storage_dir: string;
  artifact_dir: string;
  profile_dir: string;
  default_dir: string;
  app_root: string;
};

export function StorageCard() {
  const { t } = useTranslation();
  const toast = useToast();
  const fail = useErrorToast();
  const [info, setInfo] = useState<StorageInfo | null>(null);
  const [dir, setDir] = useState("");
  const [move, setMove] = useState(false);
  const [saving, setSaving] = useState(false);

  const load = async () => {
    try {
      const r = await api.getAppStorage();
      setInfo(r);
      setDir(r.storage_dir || "");
    } catch (e: any) {
      fail(e);
    }
  };

  useEffect(() => {
    void load();
  }, []);

  const save = async () => {
    setSaving(true);
    try {
      const r = await api.setAppStorage(dir.trim(), move);
      toast("success", r.message || t("Saved"));
      await load();
    } catch (e: any) {
      fail(e);
    } finally {
      setSaving(false);
    }
  };

  return (
    <Card className="max-w-2xl space-y-3 p-4">
      <div>
        <div className="text-sm font-medium text-ink-900">{t("File storage location")}</div>
        <div className="text-xs text-ink-500">
          {t("Videos, screenshots and browser profiles live next to the app by default. Point them at another drive if C: is tight. Takes effect after restarting the app.")}
        </div>
      </div>

      <div className="rounded-lg bg-[var(--panel2)] px-3 py-2 text-[12px] text-ink-600">
        <div>
          {t("Currently writing to")}: <span className="font-mono break-all">{info?.artifact_dir ?? "—"}</span>
        </div>
        <div className="mt-1">
          {t("Browser profiles")}: <span className="font-mono break-all">{info?.profile_dir ?? "—"}</span>
        </div>
        <div className="mt-1 text-ink-400">
          {t("Default (inside the app folder)")}: <span className="font-mono break-all">{info?.default_dir ?? "—"}</span>
        </div>
      </div>

      <Field label={t("New location (blank = back to default)")}>
        <Input
          value={dir}
          onChange={(e) => setDir(e.target.value)}
          placeholder={info?.default_dir || "D:\\PotatoTestData"}
        />
      </Field>

      <label className="flex items-center gap-2 text-sm text-ink-700">
        <Checkbox checked={move} onChange={(e) => setMove(e.target.checked)} />
        {t("Also move the existing files (fast on the same drive, slow across drives)")}
      </label>

      <div className="flex items-center gap-3">
        <Button onClick={save} disabled={saving}>
          {saving ? t("Saving…") : t("Save")}
        </Button>
        <span className="text-[11px] text-ink-500">
          {t("Takes effect after restarting the app.")}
        </span>
      </div>
    </Card>
  );
}
