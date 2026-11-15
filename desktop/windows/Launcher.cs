using System;
using System.Collections;
using System.Collections.Generic;
using System.ComponentModel;
using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Linq;
using System.Reflection;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;
using System.Windows.Data;
using System.Windows.Markup;
using System.Windows.Media.Imaging;
using System.Windows.Threading;
using Forms = System.Windows.Forms;
using Payload = System.Collections.Generic.Dictionary<string, object>;

internal sealed class Companion : IDisposable
{
    private readonly Process process;
    private readonly JavaScriptSerializer json = new JavaScriptSerializer();
    private readonly Dictionary<int, TaskCompletionSource<Payload>> pending =
        new Dictionary<int, TaskCompletionSource<Payload>>();
    private readonly TaskCompletionSource<bool> ready = new TaskCompletionSource<bool>();
    private readonly SemaphoreSlim writeLock = new SemaphoreSlim(1);
    private int nextRequest;

    public Companion(string config)
    {
        string executable = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "runtime", "GravelleServer.exe");
        if (!File.Exists(executable))
            throw new FileNotFoundException("找不到阅读服务。请完整解压或重新安装 Gravelle。");
        var options = new ProcessStartInfo(executable, "--launcher-bridge" +
            (config == null ? "" : " --config " + QuoteArgument(config))) {
            UseShellExecute = false, CreateNoWindow = true, RedirectStandardInput = true,
            RedirectStandardOutput = true, StandardOutputEncoding = Encoding.UTF8
        };
        process = Process.Start(options);
        process.StandardInput.AutoFlush = true;
        ReadResponses();
    }

    private static string QuoteArgument(string value)
    {
        // Windows paths cannot contain quotation marks. Duplicate trailing
        // slashes so they do not escape the closing quote.
        int trailing = value.Length - value.TrimEnd('\\').Length;
        return "\"" + value + new string('\\', trailing) + "\"";
    }

    private async void ReadResponses()
    {
        try
        {
            for (string line; (line = await process.StandardOutput.ReadLineAsync()) != null;)
            {
                var response = json.Deserialize<Payload>(line);
                bool ok = (bool)response["ok"];
                Exception error = ok ? null : new InvalidOperationException((string)response["error"]);
                object id;
                if (!response.TryGetValue("id", out id))
                {
                    if (ok) ready.TrySetResult(true);
                    else ready.TrySetException(error);
                    continue;
                }
                TaskCompletionSource<Payload> reply;
                if (!pending.TryGetValue((int)id, out reply)) continue;
                pending.Remove((int)id);
                if (ok) reply.TrySetResult((Payload)response["result"]);
                else reply.TrySetException(error);
            }
            Fail(new IOException("启动器服务已关闭。"));
        }
        catch (Exception error) { Fail(error); }
    }

    private void Fail(Exception error)
    {
        ready.TrySetException(error);
        var requests = pending.Values.ToArray();
        pending.Clear();
        foreach (var request in requests) request.TrySetException(error);
    }

    public async Task<Payload> Request(string operation, Payload payload = null)
    {
        await ready.Task;
        int id = ++nextRequest;
        var reply = new TaskCompletionSource<Payload>();
        pending.Add(id, reply);
        try
        {
            string request = json.Serialize(new Payload { { "id", id }, { "operation", operation }, { "payload", payload } });
            // Use an ASCII wire format and avoid Windows console code page conversion.
            var wire = new StringBuilder(request.Length);
            foreach (char c in request)
                if (c <= 127) wire.Append(c);
                else wire.Append("\\u").Append(((int)c).ToString("x4", CultureInfo.InvariantCulture));
            await writeLock.WaitAsync();
            try { await process.StandardInput.WriteLineAsync(wire.ToString()); }
            finally { writeLock.Release(); }
            return await reply.Task;
        }
        finally { pending.Remove(id); }
    }

    public void Dispose()
    {
        try { process.StandardInput.Close(); }
        catch (IOException) { }
        // EOF stops the owned reader even if the window exits unexpectedly.
    }
}

internal sealed class Launcher
{
    private readonly Window window;
    private readonly List<TextBox> roots = new List<TextBox>();
    private readonly List<Button> removeButtons = new List<Button>();
    private readonly DispatcherTimer timer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(1) };
    private Companion companion;
    private string storage;
    private bool initialized, running, busy, checking, closing, polling, mayClose;
    private string updateAction = "check_update";

    [STAThread]
    private static void Main(string[] args)
    {
        var app = new Application();
        try
        {
            if (args.Length == 2 && args[0] == "--config")
                app.Run(new Launcher(Path.GetFullPath(args[1])).window);
            else if (args.Length == 0)
                app.Run(new Launcher(null).window);
            else
                MessageBox.Show("启动器支持 --config 路径。维护命令请使用 runtime\\GravelleServer.exe。", "Gravelle");
        }
        catch (Exception error) { MessageBox.Show(error.Message, "Gravelle", MessageBoxButton.OK, MessageBoxImage.Error); }
    }

    private Launcher(string config)
    {
        var assembly = Assembly.GetExecutingAssembly();
        using (Stream layout = assembly.GetManifestResourceStream("Launcher.xaml"))
            window = (Window)XamlReader.Load(layout);
        Element<Image>("Wordmark").Source = Picture("Wordmark.png");
        window.Icon = Picture("Gravelle.png");
        var workArea = SystemParameters.WorkArea;
        window.MinHeight = Math.Min(window.MinHeight, workArea.Height);
        window.Width = Math.Max(window.MinWidth, Math.Min(window.Width, workArea.Width - 24));
        window.Height = Math.Max(window.MinHeight, Math.Min(window.Height, workArea.Height - 24));
        Element<RadioButton>("HomeTab").Checked += delegate { ShowPage(false); };
        Element<RadioButton>("AdvancedTab").Checked += delegate { ShowPage(true); };
        ActionButton("AddFolder", AddFolder);
        ActionButton("DiagnoseButton", Diagnose);
        ActionButton("StartButton", Start);
        ActionButton("OpenButton", OpenReader);
        ActionButton("StopButton", Stop);
        ActionButton("EntryButton", ResetEntry);
        ActionButton("PasswordButton", ResetPassword);
        ActionButton("UpdateButton", Update);
        ActionButton("LogButton", delegate { return OpenPath("logs", "server.log"); });
        ActionButton("DataButton", delegate { return OpenPath("data"); });
        window.Closing += Close;
        window.Closed += delegate { timer.Stop(); if (companion != null) companion.Dispose(); };
        timer.Tick += async delegate { await Poll(); };
        Refresh();
        window.ContentRendered += async delegate
        {
            if (companion != null || closing) return;
            try
            {
                companion = new Companion(config);
                var result = await companion.Request("initialize");
                Fill(result);
                initialized = true;
                Refresh();
                timer.Start();
                await Update();
            }
            catch (Exception error) { Error(error); }
        };
    }

    private static BitmapImage Picture(string name)
    {
        using (Stream stream = Assembly.GetExecutingAssembly().GetManifestResourceStream(name))
        {
            var image = new BitmapImage();
            image.BeginInit();
            image.CacheOption = BitmapCacheOption.OnLoad;
            image.StreamSource = stream;
            image.EndInit();
            image.Freeze();
            return image;
        }
    }

    private T Element<T>(string name) where T : FrameworkElement { return (T)window.FindName(name); }
    private void Text(string name, string value) { Element<TextBlock>(name).Text = value; }
    private void Input(string name, object value) { Element<TextBox>(name).Text = Convert.ToString(value, CultureInfo.InvariantCulture); }
    private void Error(Exception error) { Text("Status", error.Message); }
    private Button Button(string name) { return Element<Button>(name); }

    private void ActionButton(string name, Func<Task> handler)
    {
        Button(name).Click += async delegate
        {
            try { await handler(); }
            catch (Exception error) { Error(error); }
        };
    }

    private void Fill(Payload result)
    {
        var config = (Payload)result["config"];
        storage = (string)result["storage"];
        Text("Version", "v" + result["version"]);
        Text("Address", (string)result["address"]);
        Input("Port", config["port"]);
        Input("SharePort", config["share_port"]);
        Input("HttpsPort", config["https_port"]);
        Element<CheckBox>("Lan").IsChecked = (bool)config["lan_enabled"];
        foreach (string path in AsStrings(config["library_paths"])) AddRow(path);
        string error = (string)result["error"];
        Text("Status", error.Length == 0 ? "选择写真根文件夹后即可启动阅读器。" : error);
    }

    private static IEnumerable<string> AsStrings(object value) { return ((IEnumerable)value).Cast<string>(); }

    private void ShowPage(bool advanced)
    {
        Element<StackPanel>("Home").Visibility = advanced ? Visibility.Collapsed : Visibility.Visible;
        Element<StackPanel>("Advanced").Visibility = advanced ? Visibility.Visible : Visibility.Collapsed;
        Element<ScrollViewer>("PageScroll").ScrollToTop();
    }

    private void Refresh()
    {
        bool free = initialized && !busy && !closing;
        bool edit = free && !running;
        Element<StackPanel>("Folders").IsEnabled = edit;
        Button("AddFolder").IsEnabled = edit && roots.Count < 20;
        foreach (Button button in removeButtons) button.IsEnabled = roots.Count > 1;
        foreach (var name in new[] { "Port", "SharePort", "HttpsPort", "Lan" })
            Element<Control>(name).IsEnabled = edit;
        foreach (var name in new[] { "DiagnoseButton", "PasswordButton", "EntryButton" })
            Button(name).IsEnabled = free;
        foreach (var name in new[] { "LogButton", "DataButton" })
            Button(name).IsEnabled = initialized;
        Button("StartButton").IsEnabled = edit;
        Button("StopButton").IsEnabled = free && running;
        Button("OpenButton").IsEnabled = free && running;
        Button("UpdateButton").IsEnabled = initialized && !checking && !closing;
        Text("ServiceStatus", busy ? "处理中" : running ? "运行中" : "未启动");
    }

    private void AddRow(string path)
    {
        var panel = Element<StackPanel>("Folders");
        var row = new DockPanel { Margin = new Thickness(0, 0, 0, 7) };
        var remove = new Button { Content = "移除", Margin = new Thickness(7, 0, 0, 0) };
        var browse = new Button { Content = "浏览", Margin = new Thickness(7, 0, 0, 0) };
        var input = new TextBox { Text = path, VerticalAlignment = VerticalAlignment.Stretch };
        input.SetBinding(FrameworkElement.ToolTipProperty, new Binding("Text") { Source = input });
        AutomationProperties.SetName(input, "写真根文件夹");
        var actions = new StackPanel { Orientation = Orientation.Horizontal };
        DockPanel.SetDock(actions, Dock.Right);
        actions.Children.Add(browse);
        actions.Children.Add(remove);
        row.Children.Add(actions);
        row.Children.Add(input);
        panel.Children.Add(row);
        roots.Add(input);
        removeButtons.Add(remove);
        remove.Click += delegate
        {
            if (roots.Count <= 1) return;
            roots.Remove(input);
            removeButtons.Remove(remove);
            panel.Children.Remove(row);
            Refresh();
        };
        browse.Click += delegate { string selected = Browse(input.Text); if (selected != null) input.Text = selected; };
    }

    private string Browse(string initial)
    {
        using (var dialog = new Forms.FolderBrowserDialog { Description = "选择写真根文件夹", SelectedPath = initial,
                                                          ShowNewFolderButton = false })
            return dialog.ShowDialog() == Forms.DialogResult.OK ? dialog.SelectedPath : null;
    }

    private async Task AddFolder()
    {
        string selected = Browse(roots.Last().Text);
        if (selected == null) return;
        var existing = roots.Select(root => root.Text).ToArray();
        var result = await Run("choose_folder", new Payload { { "library_paths", existing }, { "selected", selected } });
        if (result == null) return;
        if ((bool)result["duplicate"])
        {
            Text("Status", "这个写真根文件夹已经在列表中。");
            return;
        }
        AddRow(selected);
        Refresh();
    }

    private int Port(string name, string label)
    {
        int value;
        if (!int.TryParse(Element<TextBox>(name).Text.Trim(), NumberStyles.None, CultureInfo.InvariantCulture, out value)
            || value < 1 || value > 65535)
            throw new InvalidOperationException(label + "需要是 1 至 65535 之间的整数。");
        return value;
    }

    private Payload Configuration()
    {
        return new Payload {
            { "library_paths", roots.Select(root => root.Text).ToArray() },
            { "port", Port("Port", "服务端口") },
            { "share_port", Port("SharePort", "分享端口") },
            { "https_port", Port("HttpsPort", "HTTPS 端口") },
            { "lan_enabled", Element<CheckBox>("Lan").IsChecked == true }
        };
    }

    private async Task<Payload> Run(string operation, Payload payload = null, string progress = null)
    {
        if (busy || closing) return null;
        try
        {
            busy = true;
            Refresh();
            if (progress != null) Text("Status", progress);
            return await companion.Request(operation, payload);
        }
        catch (Exception error) { Error(error); return null; }
        finally { busy = false; Refresh(); }
    }

    private async Task Poll()
    {
        if (!initialized || !running || busy || closing || polling) return;
        polling = true;
        try
        {
            var state = await companion.Request("status");
            if (!busy && !closing && !(bool)state["running"])
            {
                running = false;
                Text("Status", "阅读器已停止，请查看服务日志后重新启动。");
                Refresh();
            }
        }
        catch (Exception error)
        {
            running = false;
            Error(error);
            Refresh();
        }
        finally { polling = false; }
    }

    private async Task Start()
    {
        var result = await Run("start", Configuration(), "正在启动阅读器…");
        if (result == null) return;
        running = true;
        string address = (string)result["address"];
        Text("Address", address);
        Text("Status", "阅读器已启动。");
        Refresh();
        Launch(address);
        window.WindowState = WindowState.Minimized;
        await Diagnose();
    }

    private async Task Stop()
    {
        if (await Run("stop", null, "正在停止阅读器…") == null) return;
        running = false;
        Text("Status", "阅读器已停止。");
        Refresh();
    }

    private async Task OpenReader()
    {
        var result = await Run("open_reader");
        if (result != null) Launch((string)result["address"]);
    }

    private async Task Diagnose()
    {
        var result = await Run("diagnose", running ? null : Configuration(), "正在检查目录、端口与内网地址…");
        if (result == null) return;
        var addresses = AsStrings(result["lan_urls"]).ToArray();
        Text("Status", (string)result["message"]);
        Text("Address", "本机：" + result["local_url"] +
            (addresses.Length == 0 ? "" : "\n局域网：" + string.Join("\n", addresses)));
    }

    private bool Confirm(string title, string message)
    {
        return MessageBox.Show(window, message, title, MessageBoxButton.YesNo, MessageBoxImage.Question) == MessageBoxResult.Yes;
    }

    private async Task ResetEntry()
    {
        const string title = "关闭安全入口";
        if (!Confirm(title, "此操作会停止本启动器管理的阅读器，备份配置并关闭安全入口。外部启动的阅读器请先手动停止。是否继续？")) return;
        var result = await Run("reset_entry", null, "正在关闭安全入口…");
        await UpdateRunning();
        if (result == null) return;
        Text("Status", "安全入口已关闭。请重新启动阅读器。");
        var address = (string)result["address"];
        if (address.Length > 0) Text("Address", address);
        var warnings = AsStrings(result["warnings"]).ToArray();
        MessageBox.Show(window, "安全入口已关闭，原配置已备份。" +
            (warnings.Length == 0 ? "" : "\n\n" + string.Join("\n", warnings)), title);
    }

    private Window CreatePasswordDialog(out PasswordBox first, out PasswordBox second)
    {
        const string title = "重置管理员密码";
        var dialog = new Window { Title = title, WindowStartupLocation = WindowStartupLocation.CenterOwner,
            SizeToContent = SizeToContent.Height, Width = 410, ResizeMode = ResizeMode.NoResize, ShowInTaskbar = false,
            FontFamily = window.FontFamily, FontSize = window.FontSize, UseLayoutRounding = true };
        var stack = new StackPanel { Margin = new Thickness(22) };
        stack.Resources.Add(typeof(Button), window.FindResource(typeof(Button)));
        dialog.Content = stack;
        stack.Children.Add(new TextBlock { Text = "此操作会停止本启动器管理的阅读器。重置后请使用新密码登录。",
            TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 0, 0, 12) });
        first = PasswordField(stack, "新密码（8 至 256 个字符）");
        second = PasswordField(stack, "再次输入新密码");
        var buttons = new StackPanel { Orientation = Orientation.Horizontal, HorizontalAlignment = HorizontalAlignment.Right };
        var cancel = new Button { Content = "取消", IsCancel = true, MinWidth = 70, Margin = new Thickness(0, 0, 8, 0) };
        var save = new Button { Content = "保存新密码", IsDefault = true, MinWidth = 90 };
        save.Click += delegate { dialog.DialogResult = true; };
        buttons.Children.Add(cancel);
        buttons.Children.Add(save);
        stack.Children.Add(buttons);
        var initialFocus = first;
        dialog.Loaded += delegate { initialFocus.Focus(); };
        return dialog;
    }

    private async Task ResetPassword()
    {
        const string title = "重置管理员密码";
        PasswordBox first, second;
        var dialog = CreatePasswordDialog(out first, out second);
        dialog.Owner = window;
        try
        {
            if (dialog.ShowDialog() != true) return;
            var result = await Run("reset_password", new Payload { { "password", first.Password },
                { "confirmation", second.Password } }, "正在备份资料并重置管理员密码…");
            await UpdateRunning();
            if (result == null) return;
            Text("Status", "管理员密码已重置，请重新启动阅读器。");
            MessageBox.Show(window, "管理员密码已重置，旧登录已退出。", title);
        }
        finally { first.Clear(); second.Clear(); }
    }

    private PasswordBox PasswordField(Panel panel, string title)
    {
        var input = new PasswordBox { Style = (Style)window.FindResource("PasswordInput"),
                                      Margin = new Thickness(0, 0, 0, 12) };
        panel.Children.Add(new Label { Content = title, Target = input, Padding = new Thickness(0, 0, 0, 5) });
        panel.Children.Add(input);
        return input;
    }

    private async Task UpdateRunning()
    {
        try { running = (bool)(await companion.Request("status"))["running"]; }
        catch (Exception error) { running = false; Error(error); }
        Refresh();
    }

    private async Task Update()
    {
        if (!initialized || checking || closing) return;
        if (updateAction == "install_update" && (busy ||
            !Confirm("安装更新", "安装前会停止阅读器并退出启动器。是否继续？"))) return;
        checking = true;
        Refresh();
        bool refreshAfterFailure = false;
        try
        {
            if (updateAction == "install_update")
            {
                closing = true;
                Refresh();
                Text("UpdateStatus", "正在停止服务并启动安装程序…");
                var handoff = await companion.Request("install_update");
                if (handoff.ContainsKey("retry_download"))
                {
                    updateAction = "download_update";
                    Button("UpdateButton").Content = "重试下载";
                    Text("UpdateStatus", "安装程序校验失败，请重新下载。");
                    closing = false;
                    return;
                }
                if (handoff.ContainsKey("error")) MessageBox.Show(window, (string)handoff["error"], "安装更新");
                mayClose = true;
                window.Close();
            }
            else if (updateAction == "download_update")
            {
                Text("UpdateStatus", "正在下载并校验安装程序…");
                var result = await companion.Request(updateAction);
                Text("UpdateStatus", "版本 " + result["version"] + " 已下载并通过 SHA-256 校验。");
                updateAction = "install_update";
                Button("UpdateButton").Content = "安装更新";
            }
            else
            {
                Text("UpdateStatus", "正在检查更新…");
                var result = await companion.Request(updateAction);
                Text("UpdateStatus", (string)result["message"]);
                bool available = (bool)result["available"];
                updateAction = available ? "download_update" : "check_update";
                Button("UpdateButton").Content = available ? "下载更新" : "检查更新";
            }
        }
        catch (Exception error)
        {
            Text("UpdateStatus", error.Message);
            if (updateAction == "download_update") Button("UpdateButton").Content = "重试下载";
            if (closing)
            {
                closing = false;
                refreshAfterFailure = true;
            }
        }
        finally { checking = false; Refresh(); }
        if (refreshAfterFailure) await UpdateRunning();
    }

    private async void Close(object sender, CancelEventArgs args)
    {
        if (mayClose || !initialized) return;
        args.Cancel = true;
        if (closing) return;
        if (busy) { Text("Status", "操作进行中，请稍候再关闭窗口。"); return; }
        if (running && !Confirm("退出 Gravelle", "退出启动器会停止阅读器。是否退出？")) return;
        closing = true;
        Refresh();
        try
        {
            await companion.Request("exit");
            mayClose = true;
            window.Close();
        }
        catch (Exception)
        {
            // Closing the pipe also asks the companion to stop its owned
            // reader if it can no longer return an explicit exit response.
            mayClose = true;
            window.Close();
        }
    }

    private Task OpenPath(params string[] parts)
    {
        string path = Path.Combine(new[] { storage }.Concat(parts).ToArray());
        if (!File.Exists(path) && !Directory.Exists(path))
            Text("Status", "首次启动阅读器后可查看此位置。");
        else Launch(path);
        return Task.FromResult(0);
    }

    private static void Launch(string target)
    {
        Process.Start(new ProcessStartInfo(target) { UseShellExecute = true });
    }
}
