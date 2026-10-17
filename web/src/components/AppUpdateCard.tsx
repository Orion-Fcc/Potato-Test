import { useState } from "react";
import { useTranslation } from "react-i18next";
import { api, type AppUpdateInfo } from "../lib/api";
import { Button, Card } from "../components/ui";
import { useErrorToast, useToast } from "../components/toast";

/**
 * 版本与更新（2026-10-17）。
 *
 * 位置：设置页最后一张卡。做两件事 —— 查版本、一键更新。
 *
 * 几条刻意的取舍：
 *  * **打开页面不自动联网**。很多软件一进设置就偷偷请求更新接口，断网时还甩一条红字；
 *    这里只在用户点「检查更新」时才请求一次。被动检查留给以后（需要的话加个 24h 节流）。
 *  * **网页版只提示、不安装**。`install_supported=false` 时只给"去 Release 页"的按钮 ——
 *    网页版跑在服务器上，改不了用户机器上的程序。
 *  * **查不到用灰字，不用红字**。断网 / 还没发过 Release 是正常状态，
 *    标成错误会让应用看起来坏了。
 */
export function AppUpdateCard() {
  const { t } = useTranslation();
  const toast = useToast();
  const fail = useErrorToast();
  const [info, setInfo] = useState<AppUpdateInfo | null>(null);
  const [checking, setChecking] = useState(false);
  const [installing, setInstalling] = useState(false);

  const check = async (force: boolean) => {
    setChecking(true);
    try {
      setInfo(await api.getAppUpdate(force));
    } catch (e: any) {
      fail(e);
    } finally {
      setChecking(false);
    }
  };

  const install = async () => {
    if (!info?.download_url) return;
    setInstalling(true);
    try {
      const r = await api.installAppUpdate(info.download_url);
      toast("success", r.message || t("The app will close and install the update automatically."));
      // 桌面版这里会退出安装；窗口关掉 = 进程结束。网页端不会走到这一步（后端已拒绝）。
      setTimeout(() => window.close(), 800);
    } catch (e: any) {
      fail(e);
    } finally {
      setInstalling(false);
    }
  };

  return (
    <Card className="max-w-2xl space-y-3 p-4">
      <div>
        <div className="text-sm font-medium text-ink-900">{t("Version")}</div>
        <div className="text-xs text-ink-500">
          {t("Current version")}: <span className="font-mono">{info?.current ?? "—"}</span>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <Button onClick={() => check(true)} disabled={checking || installing}>
          {checking ? t("Checking…") : t("Check for updates")}
        </Button>
        {info?.has_update && info.latest && (
          <span className="text-xs text-ink-700">
            {t("New version available")}: <span className="font-mono">{info.latest}</span>
          </span>
        )}
        {info && !info.has_update && !info.error && (
          <span className="text-xs text-ink-500">{t("You're on the latest version.")}</span>
        )}
        {info?.error && <span className="text-xs text-ink-500">{info.error}</span>}
      </div>

      {info?.has_update && (
        <div className="flex flex-wrap items-center gap-3">
          {info.install_supported && info.download_url ? (
            <Button onClick={install} disabled={installing}>
              {installing ? t("Downloading…") : t("Download and install")}
            </Button>
          ) : (
            <a
              className="text-xs text-brand-700 hover:underline"
              href={info.page_url}
              target="_blank"
              rel="noreferrer"
            >
              {t("Open the release page")}
            </a>
          )}
        </div>
      )}

      {info?.has_update && info.notes && (
        <div className="rounded-lg bg-[var(--panel2)] px-3 py-2">
          <div className="text-[11px] font-medium text-ink-700">{t("Release notes")}</div>
          <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap text-[11px] text-ink-600">
            {info.notes}
          </pre>
        </div>
      )}

      <p className="text-[11px] text-ink-500">
        {info && !info.install_supported
          ? t("Only the desktop app can update in place. For the web version, ask the person who runs the server.")
          : t("Updates are downloaded from this project's GitHub releases and installed in place; your data is kept.")}
      </p>
    </Card>
  );
}
