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
 *  * **原生文件夹选择 + 可编辑文本框**：窗口由 pywebview 创建，`desktop.py` 通过 js_api
 *    暴露 `pick_folder`，点「浏览…」走系统原生选择框，初始目录就是当前路径。
 *    文本框仍然保留：兜底窗口（Edge --app / 浏览器）没有 pywebview API，而且用户可能
 *    要粘贴 UNC 路径（\\server\share\...）或手输网络位置，这些原生对话框不一定方便选。
 *  * **选完只回填、不自动保存**：保存仍由用户点「保存」，避免误改路径。
 *  * **留空 = 恢复默认**：不需要额外的"重置"按钮，语义也直白。
 *  * **「把现有文件也搬过去」默认不勾**：搬可能要很久（跨盘 = 复制），
 *    不该在用户只想想改个路径时顺手干这么重的事。
 */
declare global {
  interface Window {
    pywebview?: {
      api?: {
        pick_folder?: (start?: string) => Promise<string>;
      };
    };
  }
}

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
  const [picking, setPicking] = useState(false);

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

  const pickFolder = async () => {
    const picker = window.pywebview?.api?.pick_folder;
    if (!picker) {
      toast(
        "error",
        t("Folder picker is only available in the desktop app window. Type or paste the path instead."),
      );
      return;
    }
    setPicking(true);
    try {
      // 输入框空着表示「用默认位置」，此时仍要以当前实际存放目录作为对话框起点。
      const picked = await picker(dir.trim() || info?.artifact_dir || "");
      // 取消时 pywebview 返回空串：保持输入框原值，不报错。
      if (typeof picked === "string" && picked.trim()) setDir(picked);
    } catch (e) {
      // 弹窗失败也必须让用户知道，并且输入框继续可用（可手输/粘贴 UNC 路径）。
      fail(e, t("Could not open the folder picker"));
    } finally {
      setPicking(false);
    }
  };

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
        <div className="flex items-center gap-2">
          <Input
            className="min-w-0 flex-1"
            value={dir}
            onChange={(e) => setDir(e.target.value)}
            placeholder={info?.default_dir || "D:\\PotatoTestData"}
          />
          <Button type="button" variant="outline" onClick={pickFolder} disabled={picking}>
            {picking ? t("Opening…") : t("Browse…")}
          </Button>
        </div>
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
