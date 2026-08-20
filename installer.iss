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
;     iscc /DAppVersion=0.3.0 installer.iss
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
; Always offer the destination page. Inno's default is "auto", which hides
; it whenever the same AppId is already registered — so the ONE time a
; user is most likely to want a different folder (installed once, disliked
; where it went, ran Setup again) is exactly when they could not choose.
; Reproduced on a dev machine with 0.4.0 registered: no folder page at
; all, no explanation. Showing it on an upgrade is harmless while the
; folder stays the same (UsePreviousAppDir pre-fills it); what happens
; when it CHANGES is handled in [Code] — see MoveOldInstall.
DisableDirPage=no
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

; Device ledger written by vpad_devices.default_store_path(). A DIFFERENT
; folder — "VPad", not "{#AppName}" — and that mismatch is the whole bug:
; uninstalling used to leave it behind, holding a 32-byte permanent key per
; paired phone.
;
; Two ways that hurt. Support: "uninstall and reinstall" is the most common
; advice we give, and it reset nothing — every previously paired phone came
; straight back in over RESUME, no QR asked. Privacy: someone selling or
; handing over the PC uninstalls and reasonably believes the pairings went
; with it. They did not.
;
; Deleting it here is the honest reading of "uninstall": the pairings are
; app state, not user documents. An in-place UPGRADE is unaffected —
; Inno processes [UninstallDelete] only on actual uninstallation.
Type: filesandordirs; Name: "{localappdata}\VPad"

[Code]
const
  RunKey = 'Software\Microsoft\Windows\CurrentVersion\Run';

var
  // Where the previous install lived, or '' on a fresh install. Read ONCE,
  // before anything is written: Setup rewrites this very registry value
  // with the new folder during the file-copy step (log: "Writing uninstall
  // key values"), so reading it in ssPostInstall returns the NEW path and
  // the move goes undetected. Measured, not assumed.
  PrevAppDir: String;

function InitializeSetup(): Boolean;
var
  dir: String;
begin
  // Same value Inno itself reads for UsePreviousAppDir. HKCU, not HKA:
  // PrivilegesRequired=lowest with no override allowed means Setup never
  // runs in administrative mode, so the key is always per-user.
  // ExpandConstant turns the "{{" AppId escape into the single brace the
  // registry key actually carries.
  if not RegQueryStringValue(HKEY_CURRENT_USER,
       ExpandConstant('Software\Microsoft\Windows\CurrentVersion\Uninstall\{#SetupSetting("AppId")}_is1'),
       'Inno Setup: App Path', dir) then
    dir := '';
  PrevAppDir := RemoveBackslashUnlessRoot(dir);
  Result := True;
end;

function SameDir(const A, B: String): Boolean;
begin
  Result := CompareText(RemoveBackslashUnlessRoot(A),
                        RemoveBackslashUnlessRoot(B)) = 0;
end;

function IsSubDirOf(const Child, Parent: String): Boolean;
var
  p: String;
begin
  // "Child is strictly inside Parent": C:\A\B is inside C:\A, C:\AB is not.
  p := AddBackslash(RemoveBackslashUnlessRoot(Parent));
  Result := (Length(Child) > Length(p)) and
            (CompareText(Copy(Child, 1, Length(p)), p) = 0);
end;

procedure MoveOldInstall();
var
  old, newDir, newExe, run: String;
begin
  // Only reached when the user picked a different folder on an upgrade.
  // Inno has already repointed the Add/Remove Programs entry at the new
  // folder; the old one is now an orphan nothing will ever remove — and a
  // dangerous orphan, because the tray's "Start with Windows" value (see
  // vpad_helper.set_autostart) is written once, on toggle, as the absolute
  // path of the .exe that was running. Left alone, Windows would start the
  // OLD version at every logon; it would take the single-instance mutex,
  // and the one the user actually installed would exit silently. So: fix
  // the Run value, then remove the old tree. The helper was stopped in
  // PrepareToInstall, so nothing in it is locked.
  old := PrevAppDir;
  newDir := RemoveBackslashUnlessRoot(ExpandConstant('{app}'));
  if (old = '') or SameDir(old, newDir) then exit;
  newExe := AddBackslash(newDir) + '{#AppName}.exe';

  if RegQueryStringValue(HKEY_CURRENT_USER, RunKey, '{#AppName}', run) then
  begin
    Log('Autostart value pointed at ' + run + '; repointing at ' + newExe);
    RegWriteStringValue(HKEY_CURRENT_USER, RunKey, '{#AppName}', '"' + newExe + '"');
  end;

  // A new folder INSIDE the old one cannot be cleaned up: removing the old
  // tree would take the new install with it. Checked here rather than on
  // the wizard page because a silent /DIR= install never shows the page.
  // The stale copy is only disk residue once the Run value is fixed.
  if IsSubDirOf(newDir, old) then
  begin
    Log('New folder is inside the previous one (' + old + '); leaving it');
    exit;
  end;

  // Proof that the registry still describes a real install of OURS before
  // deleting anything: the helper's own .exe must be there. Guards against
  // a stale or hand-edited value pointing somewhere it should not.
  if FileExists(AddBackslash(old) + '{#AppName}.exe') then
  begin
    Log('Removing previous install at ' + old);
    if not DelTree(old, True, True, True) then
      Log('Could not fully remove ' + old + ' - left for the user');
  end
  else
    Log('Previous path ' + old + ' has no {#AppName}.exe; not touching it');
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    MoveOldInstall();
end;

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
