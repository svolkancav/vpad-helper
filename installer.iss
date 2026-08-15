; Inno Setup script for the V-Pad Helper Windows installer.
;
; Why an installer at all, when the app is a single tray icon:
;
;   * A bare .exe downloaded from the internet has no reputation, no
;     publisher, no uninstaller, and no entry in Add/Remove Programs. That
;     is the profile SmartScreen and every ML antivirus scores worst, and
;     it is also what makes a cautious user close the download.
;   * It lets the payload ship as a folder instead of a self-extracting
;     one-file archive. A one-file build unpacks ~68 MB into %TEMP% on
;     every single launch and runs from there — slow, and precisely the
;     behaviour heuristic scanners are trained on. Installed files just
;     sit in place. See vpad-helper.spec.
;   * One artifact to sign, later, instead of persuading users past a
;     warning. Nothing here depends on a certificate; the signing step in
;     CI is skipped when no certificate is configured.
;
; Build (after `VPAD_ONEDIR=1 pyinstaller vpad-helper.spec`):
;     iscc /DAppVersion=0.2.2 installer.iss
;
; AppVersion is passed in by CI so it cannot drift from vpad_helper.py's
; __version__; the fallback below only exists for a local run.

#ifndef AppVersion
  #define AppVersion "0.0.0-dev"
#endif

#define AppName "V-Pad Helper"
#define Publisher "V-Pad"
#define HomePage "https://vpadcontroller.com/"
#define RepoPage "https://github.com/svolkancav/vpad-helper"

[Setup]
; Never change AppId: it is what lets an upgrade replace the previous
; install instead of piling up a second copy in Add/Remove Programs.
AppId={{04F23C34-51EB-4B95-BAB3-A954A9ED84FB}
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
AppPublisher={#Publisher}
AppPublisherURL={#HomePage}
AppSupportURL={#RepoPage}/issues
AppUpdatesURL={#RepoPage}/releases
VersionInfoVersion={#AppVersion}
VersionInfoCompany={#Publisher}
VersionInfoDescription={#AppName} Setup

; Per-user install, so the installer itself never raises a UAC prompt.
; This matters more than it looks: the one elevation this app genuinely
; needs is the ViGEmBus driver, and a user who has already clicked through
; an unexplained UAC dialog to install a tray icon reads the second one as
; more of the same. {autopf} resolves to {localappdata}\Programs here.
PrivilegesRequired=lowest
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
; Deliberately NOT versioned. The website and the README both link at
; .../releases/latest/download/V-Pad-Helper-Setup.exe, and that URL only
; resolves if the asset name is identical in every release. The version is
; still visible: in the wizard's title bar, in the file's Properties, and
; in Add/Remove Programs afterwards.
OutputBaseFilename=V-Pad-Helper-Setup
OutputDir=dist-installer

; The payload is 64-bit Python; there is no 32-bit build to fall back to.
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
SetupIconFile=vpad-helper.ico
UninstallDisplayIcon={app}\{#AppName}.exe
UninstallDisplayName={#AppName}

; Restart Manager is deliberately OFF; [Code] closes the app instead.
;
; Measured, not assumed. With CloseApplications=yes, upgrading over a
; running helper fails with exit code 5. The chain is documented on both
; ends: Restart Manager can only shut down a process that answers its
; request, and this one is a pystray tray app whose window never does; the
; file stays locked, Setup raises an Abort/Retry box, and /SUPPRESSMSGBOXES
; answers those with "Abort" — which is exactly what exit code 5 means
; ("user clicked Cancel during the actual installation"). Every user
; updating the helper would have hit it, and silently.
;
; CloseApplications=force was tried as the simpler fix and is NOT enough.
; It does repair the install path (exit 0), but the uninstaller does not
; honour it: uninstalling a running helper returned exit 0 while leaving
; the app running and 56 files behind. A cleanup that reports success and
; half-removes itself is worse than one that fails loudly. One mechanism
; that covers both paths beats two that disagree.
CloseApplications=no

LicenseFile=LICENSE

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; Flags: unchecked

[Files]
; The whole one-dir bundle, executable and _internal alike.
Source: "dist-onedir\{#AppName}\*"; DestDir: "{app}"; \
  Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppName}.exe"
Name: "{group}\{#AppName} on the web"; Filename: "{#HomePage}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppName}.exe"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppName}.exe"; Description: "Start {#AppName} now"; \
  Flags: nowait postinstall skipifsilent

[UninstallDelete]
; Log folder written by vpad_helper.log_dir(). Left behind it is a stray
; directory in LocalAppData that nothing will ever clean up.
Type: filesandordirs; Name: "{localappdata}\{#AppName}"

[Code]
procedure StopHelper();
var
  rc: Integer;
begin
  // Graceful first: without /F, taskkill posts WM_CLOSE, which pystray
  // turns into a normal tray shutdown. Then force whatever is left — the
  // helper keeps no state worth saving, and the ViGEmBus pad it owns is
  // released by the driver when the process handle closes either way.
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/IM "{#AppName}.exe"',
       '', SW_HIDE, ewWaitUntilTerminated, rc);
  Sleep(1200);
  Exec(ExpandConstant('{sys}\taskkill.exe'), '/IM "{#AppName}.exe" /F',
       '', SW_HIDE, ewWaitUntilTerminated, rc);
  Sleep(400);
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  // Runs just before the first file is written. An upgrade over a running
  // helper otherwise dies on a locked _internal\python3xx.dll.
  StopHelper();
  Result := '';
end;

function InitializeUninstall(): Boolean;
begin
  // Same lock, same fix: uninstalling while the tray icon is up would
  // leave _internal behind and report a partial removal.
  StopHelper();
  Result := True;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  // The tray's "Start with Windows" toggle writes this value (see
  // vpad_helper.set_autostart). Uninstalling without clearing it leaves
  // Windows trying to launch a deleted .exe at every logon.
  //
  // Done here rather than with a [Registry] entry and uninsdeletevalue:
  // that flag is documented as removing a value Setup itself created, and
  // this value is created at runtime by the app, if the user ever asks
  // for it. Deleting it explicitly is unambiguous either way.
  if CurUninstallStep = usPostUninstall then
    RegDeleteValue(HKEY_CURRENT_USER,
      'Software\Microsoft\Windows\CurrentVersion\Run', '{#AppName}');
end;
