// AstraForge-Setup.exe - a single distributable file: unpacks the application (the embedded payload.zip) into the chosen
// folder and starts the "Install AstraForge.cmd" wizard, which downloads the programs and models.
// Built by installer\build-release.ps1 with the compiler (csc) that ships with Windows (.NET Framework 4.x, C# 5).
//   AstraForge-Setup.exe                  folder selection window
//   AstraForge-Setup.exe /extract <folder>  only unpack, no window and no wizard (build check)
using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Reflection;
using System.Threading;
using System.Windows.Forms;

[assembly: AssemblyTitle("AstraForge Setup")]
[assembly: AssemblyProduct("AstraForge")]
[assembly: AssemblyCopyright("Copyright (c) 2026 Snodrod, MIT License")]
[assembly: AssemblyVersion("0.3.0.0")]
[assembly: AssemblyFileVersion("0.3.0.0")]

static class Setup
{
    public const string Wizard = "Install AstraForge.cmd";

    [STAThread]
    static int Main(string[] args)
    {
        if (args.Length == 2 && args[0] == "/extract")
        {
            try { Extract(args[1], null); return 0; }
            catch (Exception e)
            {
                File.WriteAllText(Path.GetFullPath(args[1]).TrimEnd('\\') + ".error.txt", e.ToString());
                return 1;
            }
        }
        Application.EnableVisualStyles();
        Application.SetCompatibleTextRenderingDefault(false);
        Application.Run(new SetupForm());
        return 0;
    }

    public static void Extract(string target, Action<int, int> progress)
    {
        string root = Path.GetFullPath(target).TrimEnd('\\') + "\\";
        using (Stream s = Assembly.GetExecutingAssembly().GetManifestResourceStream("payload.zip"))
        using (ZipArchive zip = new ZipArchive(s, ZipArchiveMode.Read))
        {
            int i = 0, n = zip.Entries.Count;
            foreach (ZipArchiveEntry e in zip.Entries)
            {
                string name = e.FullName.Replace('/', '\\');
                string dst = Path.GetFullPath(Path.Combine(root, name));
                if (!dst.StartsWith(root, StringComparison.OrdinalIgnoreCase))
                    throw new IOException("Invalid path in the archive: " + e.FullName);
                if (name.EndsWith("\\")) Directory.CreateDirectory(dst);
                else
                {
                    Directory.CreateDirectory(Path.GetDirectoryName(dst));
                    e.ExtractToFile(dst, true);
                }
                i++;
                if (progress != null) progress(i, n);
            }
        }
    }
}

class SetupForm : Form
{
    readonly TextBox folder;
    readonly Button browse, install, cancel;
    readonly ProgressBar bar;
    readonly Label status;

    public SetupForm()
    {
        Text = "AstraForge 0.3 Setup";
        try { Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath); } catch { }
        FormBorderStyle = FormBorderStyle.FixedDialog;
        MaximizeBox = false;
        StartPosition = FormStartPosition.CenterScreen;
        AutoScaleMode = AutoScaleMode.Dpi;
        Font = new Font("Segoe UI", 9.5f);
        ClientSize = new Size(580, 250);

        Label title = new Label { Text = "AstraForge - local 3D character studio", AutoSize = true,
            Font = new Font("Segoe UI", 13f, FontStyle.Bold), Location = new Point(20, 16) };
        Label hint = new Label { AutoSize = true, Location = new Point(20, 54),
            Text = "The application will be unpacked into this folder. Then the setup wizard will check your computer\n" +
                   "and download the missing programs and models (20-40 GB of free space is needed,\n" +
                   "and about 50 GB more with the optional Pixal3D)." };
        folder = new TextBox { Text = DefaultFolder(), Location = new Point(20, 104), Width = 440 };
        browse = new Button { Text = "Browse...", Location = new Point(470, 102), Width = 90, Height = 28 };
        bar = new ProgressBar { Location = new Point(20, 146), Width = 540, Height = 16 };
        status = new Label { AutoSize = true, Location = new Point(20, 170), ForeColor = SystemColors.GrayText,
            Text = "MIT license. Third-party components are listed in THIRD_PARTY_NOTICES.md." };
        install = new Button { Text = "Install", Location = new Point(350, 204), Width = 120, Height = 32 };
        cancel = new Button { Text = "Cancel", Location = new Point(480, 204), Width = 80, Height = 32 };
        Controls.AddRange(new Control[] { title, hint, folder, browse, bar, status, install, cancel });
        AcceptButton = install;
        CancelButton = cancel;

        browse.Click += delegate
        {
            using (FolderBrowserDialog d = new FolderBrowserDialog())
            {
                d.Description = "Folder for AstraForge";
                d.SelectedPath = folder.Text;
                if (d.ShowDialog(this) == DialogResult.OK)
                    folder.Text = Path.Combine(d.SelectedPath, "AstraForge");
            }
        };
        cancel.Click += delegate { Close(); };
        install.Click += delegate { Start(); };
    }

    static string DefaultFolder()
    {
        foreach (string c in new[] { @"C:\AstraForge", @"C:\AstraForgeStudio" })
            if (!Directory.Exists(c) || File.Exists(Path.Combine(c, "Start.cmd"))) return c;
        return @"C:\AstraForgeStudio";
    }

    bool Confirm(string text)
    {
        return MessageBox.Show(this, text, "AstraForge", MessageBoxButtons.YesNo, MessageBoxIcon.Warning) == DialogResult.Yes;
    }

    void Start()
    {
        string dir = folder.Text.Trim();
        if (dir.Length == 0 || !Path.IsPathRooted(dir))
        {
            MessageBox.Show(this, "Enter a full folder path, for example C:\\AstraForge.", "AstraForge");
            return;
        }
        // paths inside the folder reach ~100 characters (node_modules), Python/PyTorch in runtime\ are even longer,
        // and most Windows programs cannot open paths longer than 260 characters
        if (Path.GetFullPath(dir).Length > 60)
        {
            MessageBox.Show(this, "The folder path is too long - choose a shorter one, for example C:\\AstraForge " +
                "(at most 60 characters, now " + Path.GetFullPath(dir).Length + ").", "AstraForge");
            return;
        }
        foreach (char ch in dir)
            if (ch > 127)
            {
                if (!Confirm("The path contains non-Latin characters. Blender and Python sometimes " +
                             "misbehave with such paths. It is better to choose a Latin-only path, for example C:\\AstraForge.\n\nContinue anyway?")) return;
                break;
            }
        try
        {
            DriveInfo drive = new DriveInfo(Path.GetPathRoot(Path.GetFullPath(dir)));
            if (drive.AvailableFreeSpace < 25L * 1000 * 1000 * 1000 &&
                !Confirm(string.Format("Drive {0} has {1:N1} GB free, but the models need 20-40 GB (and about 50 GB more with the optional Pixal3D).\n\nContinue anyway?",
                    drive.Name, drive.AvailableFreeSpace / 1e9))) return;
        }
        catch { }
        if (Directory.Exists(dir) && !File.Exists(Path.Combine(dir, "Start.cmd")) &&
            Directory.GetFileSystemEntries(dir).Length > 0 &&
            !Confirm("The folder is not empty. Unpack AstraForge into it?")) return;

        foreach (Control c in new Control[] { folder, browse, install, cancel }) c.Enabled = false;
        status.ForeColor = SystemColors.ControlText;
        status.Text = "Unpacking...";
        Thread t = new Thread(delegate ()
        {
            try
            {
                Setup.Extract(dir, delegate (int i, int n)
                {
                    if (i % 25 == 0 || i == n)
                        BeginInvoke((Action)delegate { bar.Maximum = n; bar.Value = i; status.Text = string.Format("Unpacking... {0} of {1}", i, n); });
                });
                BeginInvoke((Action)delegate { Done(dir); });
            }
            catch (Exception e)
            {
                BeginInvoke((Action)delegate
                {
                    MessageBox.Show(this, "Could not unpack: " + e.Message, "AstraForge", MessageBoxButtons.OK, MessageBoxIcon.Error);
                    foreach (Control c in new Control[] { folder, browse, install, cancel }) c.Enabled = true;
                    status.Text = "";
                });
            }
        });
        t.IsBackground = true;
        t.Start();
    }

    void Done(string dir)
    {
        status.Text = "Done. Starting the setup wizard...";
        try
        {
            Process.Start(new ProcessStartInfo(Path.Combine(dir, Setup.Wizard)) { WorkingDirectory = dir, UseShellExecute = true });
        }
        catch (Exception e)
        {
            MessageBox.Show(this, "The application was unpacked into " + dir + ", but the wizard did not start: " + e.Message +
                "\n\nRun \"" + Setup.Wizard + "\" in this folder manually.", "AstraForge");
        }
        Close();
    }
}
