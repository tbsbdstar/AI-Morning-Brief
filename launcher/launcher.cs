// AI早知道 启动器：双击后不弹黑窗，后台起 console.py，再用 Edge/Chrome 的「应用窗口」模式打开（没有地址栏和标签页）。
// 整个文件夹拷到任何一台 Windows 电脑都能用：先找文件夹里自带的 runtime\python，再找本机安装的 Python。
// 重新编译：双击 launcher\build.bat
using System;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Threading;
using System.Windows.Forms;
using Microsoft.Win32;

[assembly: System.Reflection.AssemblyTitle("AI早知道")]
[assembly: System.Reflection.AssemblyProduct("AI早知道")]
[assembly: System.Reflection.AssemblyDescription("每日 AI / 信息安全 / 产品经理资讯早报")]
[assembly: System.Reflection.AssemblyVersion("1.0.0.0")]

static class Launcher
{
    const string AppId = "ai-brief-console";

    [STAThread]
    static void Main()
    {
        string root = AppDomain.CurrentDomain.BaseDirectory;
        int port = FindRunning();
        if (port == 0)
        {
            string py = FindPython(root);
            if (py == null)
            {
                var r = MessageBox.Show(
                    "这台电脑还没有安装 Python（需要 3.8 或更高版本）。\n\n点「确定」打开 Python 官网下载页，安装时请勾选「Add Python to PATH」，装好后再双击 AI早知道。",
                    "AI早知道", MessageBoxButtons.OKCancel, MessageBoxIcon.Information);
                if (r == DialogResult.OK) Process.Start("https://www.python.org/downloads/");
                return;
            }
            var psi = new ProcessStartInfo(py, "\"" + Path.Combine(root, "console.py") + "\" --no-browser");
            psi.WorkingDirectory = root;
            psi.UseShellExecute = false;
            psi.CreateNoWindow = true;
            try { Process.Start(psi); }
            catch (Exception e)
            {
                MessageBox.Show("后台启动失败：" + e.Message, "AI早知道", MessageBoxButtons.OK, MessageBoxIcon.Error);
                return;
            }
            for (int i = 0; i < 80 && port == 0; i++) { Thread.Sleep(250); port = FindRunning(); }
            if (port == 0)
            {
                MessageBox.Show("后台没有启动成功。\n可以查看文件夹里的 logs\\console.log 了解原因。", "AI早知道",
                    MessageBoxButtons.OK, MessageBoxIcon.Warning);
                return;
            }
        }
        OpenWindow("http://127.0.0.1:" + port + "/");
    }

    // 已经在运行的控制台（只认自己：/api/ping 返回 ai-brief-console）
    static int FindRunning()
    {
        for (int p = 8765; p <= 8775; p++)
        {
            try
            {
                var req = (HttpWebRequest)WebRequest.Create("http://127.0.0.1:" + p + "/api/ping");
                req.Timeout = 500;
                req.Proxy = null;
                using (var resp = req.GetResponse())
                using (var sr = new StreamReader(resp.GetResponseStream()))
                {
                    if (sr.ReadToEnd().Contains(AppId)) return p;
                }
            }
            catch { }
        }
        return 0;
    }

    static string FindPython(string root)
    {
        // 1) 文件夹自带的运行环境
        string bundled = Path.Combine(root, @"runtime\python\pythonw.exe");
        if (File.Exists(bundled)) return bundled;
        // 2) 注册表里登记的 Python（用户级优先，取版本最高的）
        string best = null; Version bestVer = null;
        foreach (var hive in new[] { Registry.CurrentUser, Registry.LocalMachine })
        {
            foreach (var view in new[] { RegistryView.Registry64, RegistryView.Registry32 })
            {
                try
                {
                    using (var baseKey = RegistryKey.OpenBaseKey(hive == Registry.CurrentUser ? RegistryHive.CurrentUser : RegistryHive.LocalMachine, view))
                    using (var core = baseKey.OpenSubKey(@"Software\Python\PythonCore"))
                    {
                        if (core == null) continue;
                        foreach (var name in core.GetSubKeyNames())
                        {
                            Version v;
                            if (!Version.TryParse(name.Split('-')[0], out v) || v < new Version(3, 8)) continue;
                            using (var ip = core.OpenSubKey(name + @"\InstallPath"))
                            {
                                if (ip == null) continue;
                                string w = ip.GetValue("WindowedExecutablePath") as string;
                                if (string.IsNullOrEmpty(w))
                                {
                                    string dir = ip.GetValue("") as string;
                                    if (!string.IsNullOrEmpty(dir)) w = Path.Combine(dir, "pythonw.exe");
                                }
                                if (!string.IsNullOrEmpty(w) && File.Exists(w) && (bestVer == null || v > bestVer)) { best = w; bestVer = v; }
                            }
                        }
                    }
                }
                catch { }
            }
        }
        if (best != null) return best;
        // 3) 常见安装位置
        foreach (var baseDir in new[] {
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), @"Programs\Python"),
            Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles) })
        {
            try
            {
                foreach (var d in Directory.GetDirectories(baseDir, "Python3*"))
                {
                    string w = Path.Combine(d, "pythonw.exe");
                    if (File.Exists(w)) best = w;
                }
            }
            catch { }
        }
        if (best != null) return best;
        // 4) PATH（跳过 WindowsApps 里的应用商店占位程序）
        foreach (var dir in (Environment.GetEnvironmentVariable("PATH") ?? "").Split(';'))
        {
            if (dir.Length == 0 || dir.IndexOf("WindowsApps", StringComparison.OrdinalIgnoreCase) >= 0) continue;
            try
            {
                string w = Path.Combine(dir.Trim(), "pythonw.exe");
                if (File.Exists(w)) return w;
            }
            catch { }
        }
        return null;
    }

    static string AppPath(string exe, string[] fallbacks)
    {
        foreach (var hive in new[] { Registry.CurrentUser, Registry.LocalMachine })
        {
            try
            {
                using (var k = hive.OpenSubKey(@"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\" + exe))
                {
                    string p = k == null ? null : k.GetValue("") as string;
                    if (!string.IsNullOrEmpty(p) && File.Exists(p)) return p;
                }
            }
            catch { }
        }
        foreach (var f in fallbacks)
        {
            string p = Environment.ExpandEnvironmentVariables(f);
            if (File.Exists(p)) return p;
        }
        return null;
    }

    static void OpenWindow(string url)
    {
        string args = "--app=" + url + " --window-size=1380,900";
        string edge = AppPath("msedge.exe", new[] {
            @"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
            @"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe" });
        string chrome = AppPath("chrome.exe", new[] {
            @"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
            @"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
            @"%LocalAppData%\Google\Chrome\Application\chrome.exe" });
        foreach (var b in new[] { edge, chrome })
        {
            if (b == null) continue;
            try { Process.Start(b, args); return; } catch { }
        }
        Process.Start(url); // 都没有就用默认浏览器
    }
}
