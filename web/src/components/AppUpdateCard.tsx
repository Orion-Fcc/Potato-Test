import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { api, type AppUpdateInfo, type AppUpdateInstallState } from "../lib/api";
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
const mb = (n: number) => (n / 1024 / 1024).toFixed(1);

export function AppUpdateCard() {
  const { t } = useTranslation();
  const toast = useToast();
  const fail = useErrorToast();
  const [info, setInfo] = useState<AppUpdateInfo | null>(null);
  const [checking, setChecking] = useState(false);
  const [installing, setInstalling] = useState(false);
  const [installState, setInstallState] = useState<AppUpdateInstallState | null>(null);
  const pollTimer = useRef<number | null>(null);
  const pollFails = useRef(0);
  // 最近一次成功拿到状态的时间。单次状态请求超时不等于下载失败：后端只是读内存，
  // 偶尔忙/渲染卡顿都会让它晚一点回。连续两分钟拿不到状态才认为真的失联。
  const lastOkAt = useRef(Date.now());

  const stopPolling = () => {
    if (pollTimer.current != null) {
      window.clearTimeout(pollTimer.current);
      pollTimer.current = null;
    }
  };

  // 组件卸载后别再往死状态里写（设置页切走很常见）；重新进入时若后台还在下载，
  // 自动接上进度，而不是让用户以为任务丢了。
  useEffect(() => {
    let alive = true;
    api
      .getAppUpdateInstall()
      .then((s) => {
        if (!alive) return;
        if (s.phase === "downloading" || s.phase === "verifying" || s.phase === "installing") {
          setInstallState(s);
          setInstalling(true);
          pollFails.current = 0;
          pollProgress();
        }
      })
      .catch(() => {
        /* 没有进行中的任务 / 后端还没起来：保持静默 */
      });
    return () => {
      alive = false;
      stopPolling();
    };
  }, []);

  /** 每秒问一次后台任务。下载 480 MB 可能要几分钟，靠轮询而不是长请求扛住。 */
  const pollProgress = () => {
    pollTimer.current = window.setTimeout(async () => {
      try {
        const s = await api.getAppUpdateInstall();
        pollFails.current = 0;
        lastOkAt.current = Date.now();
        setInstallState(s);
        if (s.phase === "error") {
          stopPolling();
          setInstalling(false);
          fail(s.error || t("Update failed"));
          return;
        }
        if (s.phase === "installing" || s.phase === "done") {
          stopPolling();
          toast("success", s.message || t("The app will close and install the update automatically."));
          // 桌面版这里会退出安装；窗口关掉 = 进程结束。
          setTimeout(() => window.close(), 800);
          return;
        }
        if (s.phase === "downloading" || s.phase === "verifying") {
          pollProgress();
          return;
        }
        stopPolling();
        setInstalling(false);
      } catch {
        // 状态接口只是读内存。短暂超时/卡顿不是下载失败 —— 两分钟内继续静默重试，
        // 进度条保持原位，别把「request timed out」甩到用户脸上。
        pollFails.current += 1;
        if (Date.now() - lastOkAt.current >= 120_000) {
          stopPolling();
          setInstalling(false);
          fail(t("Lost contact with the update task. Reopen Settings to check the progress."));
          return;
        }
        pollProgress();
      }
    }, 1000);
  };

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
      // 立即返回，真正的下载在后台；进度由 pollProgress 轮询。
      const r = await api.installAppUpdate(info.download_url);
      setInstallState(r.state);
      pollFails.current = 0;
      pollProgress();
    } catch (e: any) {
      setInstalling(false);
      fail(e);
    }
  };

  const installLabel =
    installState?.phase === "verifying"
      ? t("Verifying…")
      : installState?.phase === "installing"
        ? t("Installing…")
        : t("Downloading…");

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
              {installing ? installLabel : t("Download and install")}
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

      {installing && installState &&
        (installState.phase === "downloading" || installState.phase === "verifying") && (
          <div className="space-y-1.5">
            <div className="h-2 w-full overflow-hidden rounded-full bg-[var(--panel2)]">
              {installState.phase === "verifying" || installState.percent == null ? (
                // 总大小未知（分块传输）或正在求哈希：给不确定态，别假装知道百分比。
                <div className="tp-indeterminate h-full w-2/5 rounded-full bg-brand-600" />
              ) : (
                <div
                  className="h-full rounded-full bg-brand-600 transition-[width] duration-500"
                  style={{ width: `${installState.percent}%` }}
                />
              )}
            </div>
            <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 text-xs tabular-nums text-ink-500">
              <span>
                {installState.phase === "verifying"
                  ? t("Verifying…")
                  : `${t("Downloading…")}${installState.percent != null ? ` ${installState.percent}%` : ""}`}
              </span>
              {installState.phase === "downloading" && (
                <span>
                  {installState.total > 0
                    ? `${mb(installState.downloaded)} / ${mb(installState.total)} MB`
                    : `${mb(installState.downloaded)} MB`}
                  {installState.speed > 0 && (
                    <>
                      {" · "}
                      {mb(installState.speed)} MB/s
                    </>
                  )}
                </span>
              )}
            </div>
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
